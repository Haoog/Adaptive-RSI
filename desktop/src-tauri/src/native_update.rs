use super::*;
use fs2::available_space;
use tauri_plugin_updater::UpdaterExt;

const FEED: &str = "https://github.com/Haoog/Adaptive-RSI/releases/latest/download/desktop-latest.json";
const MAX_PACKAGE: usize = 512 * 1024 * 1024;

pub struct NativeUpdater {
    pub state: Mutex<Value>,
    busy: AtomicBool,
    cancel: AtomicBool,
    staged: Mutex<Option<(tauri_plugin_updater::Update, Vec<u8>)>>,
}

impl Default for NativeUpdater {
    fn default() -> Self {
        Self { state: Mutex::new(json!({"state":"idle"})), busy: AtomicBool::new(false),
               cancel: AtomicBool::new(false), staged: Mutex::new(None) }
    }
}

impl NativeUpdater {
    fn set(&self, value: Value) {
        *self.state.lock().unwrap() = value;
    }
}

fn configured(app: &tauri::AppHandle) -> Result<tauri_plugin_updater::Updater, String> {
    let key = option_env!("RSI_UPDATER_PUBLIC_KEY").unwrap_or("");
    if key.is_empty() {
        return Err("updater_key_missing".into());
    }
    app.updater_builder()
        .pubkey(key)
        .endpoints(vec![FEED.parse().map_err(|_| "invalid_update_feed")?])
        .map_err(|_| "invalid_update_feed")?
        .timeout(Duration::from_secs(600))
        .build()
        .map_err(|_| "invalid_update_configuration".into())
}

pub fn snapshot(app: &tauri::AppHandle) -> Value {
    app.state::<NativeUpdater>().state.lock().unwrap().clone()
}

pub async fn check(app: tauri::AppHandle, download: bool) -> Result<Value, String> {
    let updater = app.state::<NativeUpdater>();
    if updater.busy.swap(true, Ordering::SeqCst) {
        return Ok(snapshot(&app));
    }
    updater.cancel.store(false, Ordering::SeqCst);
    let outcome = async {
        if updater.staged.lock().unwrap().is_some() {
            return Ok(snapshot(&app));
        }
        updater.set(json!({"state":"checking"}));
        let update = configured(&app)?.check().await.map_err(|_| "update_check_failed")?;
        let Some(update) = update else {
            updater.set(json!({"state":"latest"}));
            return Ok(snapshot(&app));
        };
        if parse_version(&update.version).is_none() {
            return Err("unstable_update_rejected".into());
        }
        let version = update.version.clone();
        let notes = update.body.clone().unwrap_or_default();
        updater.set(json!({"state":"available","version":version,"notes":notes}));
        if !download {
            return Ok(snapshot(&app));
        }
        let cache = app.path().app_cache_dir().map_err(|_| "update_cache_unavailable")?;
        std::fs::create_dir_all(&cache).map_err(|_| "update_cache_unavailable")?;
        if available_space(&cache).map_err(|_| "update_disk_unavailable")? < (MAX_PACKAGE as u64) * 3 {
            return Err("update_disk_space".into());
        }
        let bytes = AtomicU64::new(0);
        let future = update.download(|chunk, total| {
            let count = bytes.fetch_add(chunk as u64, Ordering::SeqCst) + chunk as u64;
            if count > MAX_PACKAGE as u64 || total.is_some_and(|size| size > MAX_PACKAGE as u64) {
                updater.cancel.store(true, Ordering::SeqCst);
            }
            updater.set(json!({"state":"downloading","version":version,"notes":notes,
                               "bytes":count,"total":total}));
        }, || {});
        let mut future = std::pin::pin!(future);
        let downloaded = loop {
            if updater.cancel.load(Ordering::SeqCst) {
                return Err(if bytes.load(Ordering::SeqCst) > MAX_PACKAGE as u64 { "update_size_limit" } else { "update_cancelled" }.into());
            }
            match tokio::select! {
                    value = &mut future => Some(value),
                    _ = tokio::time::sleep(Duration::from_millis(100)) => None,
                } {
                Some(value) => break value.map_err(|_| "update_download_or_signature_failed")?,
                None => continue,
            }
        };
        if downloaded.len() > MAX_PACKAGE {
            return Err("update_size_limit".into());
        }
        drop(future);
        *updater.staged.lock().unwrap() = Some((update.clone(), downloaded));
        updater.set(json!({"state":"ready","version":version,"notes":notes}));
        Ok(snapshot(&app))
    }.await;
    if let Err(ref code) = outcome {
        updater.set(json!({"state":"error","error_code":code}));
    }
    updater.busy.store(false, Ordering::SeqCst);
    outcome
}

pub fn cancel(app: &tauri::AppHandle) {
    app.state::<NativeUpdater>().cancel.store(true, Ordering::SeqCst);
}

pub fn install(app: tauri::AppHandle) -> Result<(), String> {
    let updater = app.state::<NativeUpdater>();
    if updater.busy.swap(true, Ordering::SeqCst) {
        return Err("update_busy".into());
    }
    updater.cancel.store(false, Ordering::SeqCst);
    let local = app.state::<RuntimeState>().local.lock().unwrap().clone();
    let mut lease = Value::Null;
    let result: Result<(), String> = (|| {
        let version = updater.staged.lock().unwrap().as_ref().map(|(update, _)| update.version.clone())
            .ok_or("update_not_downloaded")?;
        let runtime = app.state::<RuntimeState>();
        let owned = local.as_ref().filter(|connection| connection.owned);
        if let Some(connection) = owned {
            let reply = call(connection, "desktop-update", Some(json!({"action":"prepare","version":version})))?;
            if reply.status != 200 { return Err("update_drain_failed".into()); }
            let mut ready: Value = serde_json::from_str(&reply.body).map_err(|_| "update_drain_failed")?;
            lease = ready["barrier"]["lease"].clone();
            let deadline = std::time::Instant::now() + Duration::from_secs(3600);
            while ready["ready"] != true {
                updater.set(json!({"state":"waiting_idle","version":version,"reasons":ready["reasons"]}));
                if updater.cancel.load(Ordering::SeqCst) || std::time::Instant::now() >= deadline {
                    let _ = call(connection, "desktop-update", Some(json!({"action":"cancel","lease":lease})));
                    return Err("update_wait_cancelled".into());
                }
                std::thread::sleep(Duration::from_secs(1));
                let reply = call(connection, "desktop-update", Some(json!({"action":"status"})))?;
                if reply.status != 200 { return Err("update_drain_failed".into()); }
                ready = serde_json::from_str(&reply.body).map_err(|_| "update_drain_failed")?;
                if ready["barrier"]["lease"] != lease { return Err("update_lease_expired".into()); }
            }
            let reply = call(connection, "desktop-update", Some(json!({"action":"commit","lease":lease})))?;
            if reply.status != 200 { return Err("update_drain_failed".into()); }
        }
        if updater.cancel.load(Ordering::SeqCst) { return Err("update_wait_cancelled".into()); }
        updater.set(json!({"state":"installing","version":version}));
        // Replacement starts only after owned work has drained. Other services are not stopped.
        let child = runtime.child.lock().unwrap().take();
        if let Some(mut child) = child {
            if let Some(mut stdin) = child.stdin.take() { let _ = stdin.flush(); }
            if !wait_for_child(&mut child, Duration::from_secs(30)) {
                *runtime.child.lock().unwrap() = Some(child);
                return Err("update_runtime_shutdown_failed".into());
            }
        }
        let staged = updater.staged.lock().unwrap().take().ok_or("update_not_downloaded")?;
        if staged.0.install(&staged.1).is_err() {
            *updater.staged.lock().unwrap() = Some(staged);
            if let Some(connection) = owned {
                let _ = call(connection, "desktop-update", Some(json!({"action":"cancel","lease":lease})));
            }
            let _ = bootstrap(&app);
            return Err("update_install_failed".into());
        }
        app.restart();
    })();
    if let Err(ref error) = result {
        if let Some(connection) = local.as_ref().filter(|connection| connection.owned) {
            if lease.is_string() {
                let _ = call(connection, "desktop-update", Some(json!({"action":"cancel","lease":lease})));
            }
        }
        let staged = updater.staged.lock().unwrap();
        if let Some((update, _)) = staged.as_ref() {
            updater.set(json!({"state":"ready","version":update.version,"notes":update.body,"error_code":error}));
        } else {
            updater.set(json!({"state":"error","error_code":error}));
        }
    }
    updater.busy.store(false, Ordering::SeqCst);
    result
}
