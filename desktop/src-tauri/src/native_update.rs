use super::*;
use fs2::available_space;
use std::{fs::File, path::{Path, PathBuf}};
use tauri_plugin_updater::UpdaterExt;

const FEED: &str = "https://github.com/Haoog/Adaptive-RSI/releases/latest/download/desktop-latest.json";
const MAX_PACKAGE: usize = 512 * 1024 * 1024;

pub struct NativeUpdater {
    pub state: Mutex<Value>,
    busy: AtomicBool,
    cancel: AtomicBool,
    staged: Mutex<Option<(tauri_plugin_updater::Update, PathBuf)>>,
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

fn read_package(path: &Path) -> Result<Vec<u8>, String> {
    use std::io::Read;
    let metadata = std::fs::symlink_metadata(path).map_err(|_| "update_cache_unavailable")?;
    if !metadata.is_file() || metadata.len() > MAX_PACKAGE as u64 {
        return Err("update_size_limit".into());
    }
    let mut file = File::open(path).map_err(|_| "update_cache_unavailable")?;
    if file.metadata().map_err(|_| "update_cache_unavailable")?.len() != metadata.len() {
        return Err("update_cache_unavailable".into());
    }
    let mut bytes = vec![0; metadata.len() as usize];
    file.read_exact(&mut bytes).map_err(|_| "update_cache_unavailable")?;
    if file.read(&mut [0u8; 1]).map_err(|_| "update_cache_unavailable")? != 0 {
        return Err("update_cache_unavailable".into());
    }
    Ok(bytes)
}

#[cfg(test)]
mod cache_tests {
    use super::*;

    #[test]
    fn package_cache_is_bounded_and_rejects_links() {
        let folder = tempfile::tempdir().unwrap();
        let path = folder.path().join("package");
        std::fs::write(&path, b"signed fixture").unwrap();
        assert_eq!(read_package(&path).unwrap(), b"signed fixture");
        File::options().write(true).open(&path).unwrap().set_len(MAX_PACKAGE as u64 + 1).unwrap();
        assert_eq!(read_package(&path).unwrap_err(), "update_size_limit");
        #[cfg(unix)] {
            let link = folder.path().join("link");
            std::os::unix::fs::symlink(&path, &link).unwrap();
            assert!(read_package(&link).is_err());
        }
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
    let mut state = app.state::<NativeUpdater>().state.lock().unwrap().clone();
    state["current_version"] = json!(app.package_info().version.to_string());
    state
}

pub async fn check(app: tauri::AppHandle, download: bool) -> Result<Value, String> {
    let updater = app.state::<NativeUpdater>();
    if updater.busy.swap(true, Ordering::SeqCst) {
        return Ok(snapshot(&app));
    }
    updater.cancel.store(false, Ordering::SeqCst);
    let outcome = async {
        updater.set(json!({"state":"checking"}));
        let update = configured(&app)?.check().await.map_err(|_| "update_check_failed")?;
        let Some(update) = update else {
            *updater.staged.lock().unwrap() = None;
            updater.set(json!({"state":"latest"}));
            return Ok(snapshot(&app));
        };
        if parse_version(&update.version).is_none() {
            return Err("unstable_update_rejected".into());
        }
        let version = update.version.clone();
        let notes = update.body.clone().unwrap_or_default();
        updater.set(json!({"state":"available","version":version,"notes":notes}));
        let cache = app.path().app_cache_dir().map_err(|_| "update_cache_unavailable")?;
        std::fs::create_dir_all(&cache).map_err(|_| "update_cache_unavailable")?;
        let cached = cache.join("staged-update.bin");
        // A fresh feed and signature check are required even when using a persisted package.
        if let Ok(bytes) = read_package(&cached) {
            if update.verify(&bytes).is_ok() {
                *updater.staged.lock().unwrap() = Some((update.clone(), cached));
                updater.set(json!({"state":"ready","version":version,"notes":notes}));
                return Ok(snapshot(&app));
            }
        }
        *updater.staged.lock().unwrap() = None;
        if !download { return Ok(snapshot(&app)); }
        let executable = std::env::current_exe().map_err(|_| "update_disk_unavailable")?;
        for volume in [cache.as_path(), executable.parent().ok_or("update_disk_unavailable")?] {
            if available_space(volume).map_err(|_| "update_disk_unavailable")? < (MAX_PACKAGE as u64) * 3 {
                return Err("update_disk_space".into());
            }
        }
        let client = reqwest::Client::builder().connect_timeout(Duration::from_secs(15))
            .timeout(Duration::from_secs(600)).build().map_err(|_| "update_check_failed")?;
        if update.download_url.scheme() != "https" { return Err("invalid_update_feed".into()); }
        let send = client.get(update.download_url.clone()).headers(update.headers.clone()).send();
        tokio::pin!(send);
        let mut response = loop {
            if updater.cancel.load(Ordering::SeqCst) { return Err("update_cancelled".into()); }
            tokio::select! {
                value = &mut send => break value.map_err(|_| "update_connection_failed")?,
                _ = tokio::time::sleep(Duration::from_millis(100)) => {}
            }
        };
        if !response.status().is_success() { return Err("update_download_or_signature_failed".into()); }
        let total = response.content_length();
        if total.is_some_and(|size| size > MAX_PACKAGE as u64) { return Err("update_size_limit".into()); }
        let mut output = tempfile::NamedTempFile::new_in(&cache).map_err(|_| "update_cache_unavailable")?;
        let mut count = 0u64;
        loop {
            let receive = tokio::time::timeout(Duration::from_secs(30), response.chunk());
            tokio::pin!(receive);
            let chunk = loop {
                if updater.cancel.load(Ordering::SeqCst) { return Err("update_cancelled".into()); }
                tokio::select! {
                    value = &mut receive => break value.map_err(|_| "update_transfer_stalled")?
                        .map_err(|_| "update_download_or_signature_failed")?,
                    _ = tokio::time::sleep(Duration::from_millis(100)) => {}
                }
            };
            let Some(chunk) = chunk else { break; };
            count += chunk.len() as u64;
            if count > MAX_PACKAGE as u64 { return Err("update_size_limit".into()); }
            output.write_all(&chunk).map_err(|_| "update_cache_unavailable")?;
            updater.set(json!({"state":"downloading","version":version,"notes":notes,"bytes":count,"total":total}));
        }
        output.as_file().sync_all().map_err(|_| "update_cache_unavailable")?;
        let bytes = read_package(output.path())?;
        update.verify(&bytes).map_err(|_| "update_download_or_signature_failed")?;
        drop(bytes);
        output.persist(&cached).map_err(|_| "update_cache_unavailable")?;
        *updater.staged.lock().unwrap() = Some((update.clone(), cached));
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

fn authorized_window(window: &str, weekday: u32, hour: u32) -> bool {
    match window {
        "any" => true,
        "night" => hour >= 22 || hour < 6,
        "weekend" => weekday >= 6,
        _ => false,
    }
}

fn require_window(window: &str) -> Result<(), String> {
    use chrono::{Datelike, Timelike};
    let now = chrono::Local::now();
    if authorized_window(window, now.weekday().number_from_monday(), now.hour()) { Ok(()) }
    else { Err("update_window_closed".into()) }
}

pub fn install(app: tauri::AppHandle, window: String) -> Result<(), String> {
    let updater = app.state::<NativeUpdater>();
    if updater.busy.swap(true, Ordering::SeqCst) {
        return Err("update_busy".into());
    }
    updater.cancel.store(false, Ordering::SeqCst);
    let local = app.state::<RuntimeState>().local.lock().unwrap().clone();
    let mut lease = Value::Null;
    let mut engine_stopped = false;
    let result: Result<(), String> = (|| {
        require_window(&window)?;
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
                require_window(&window)?;
                updater.set(json!({"state":"waiting_idle","version":version,"reasons":ready["reasons"]}));
                if updater.cancel.load(Ordering::SeqCst) || std::time::Instant::now() >= deadline {
                    let _ = call(connection, "desktop-update", Some(json!({"action":"cancel","lease":lease})));
                    return Err("update_wait_cancelled".into());
                }
                std::thread::sleep(Duration::from_secs(1));
                let reply = call(connection, "desktop-update", Some(json!({"action":"prepare","version":version,"lease":lease})))?;
                if reply.status != 200 { return Err("update_drain_failed".into()); }
                ready = serde_json::from_str(&reply.body).map_err(|_| "update_drain_failed")?;
                if ready["barrier"]["lease"] != lease { return Err("update_lease_expired".into()); }
            }
            require_window(&window)?;
            let reply = call(connection, "desktop-update", Some(json!({"action":"commit","lease":lease})))?;
            if reply.status != 200 { return Err("update_drain_failed".into()); }
        }
        if updater.cancel.load(Ordering::SeqCst) { return Err("update_wait_cancelled".into()); }
        require_window(&window)?;
        updater.set(json!({"state":"installing","version":version}));
        // Replacement starts only after owned work has drained. Other services are not stopped.
        let child = runtime.child.lock().unwrap().take();
        if let Some(mut child) = child {
            if let Some(mut stdin) = child.stdin.take() { let _ = stdin.flush(); }
            if !wait_for_child(&mut child, Duration::from_secs(30)) {
                *runtime.child.lock().unwrap() = Some(child);
                return Err("update_runtime_shutdown_failed".into());
            }
            engine_stopped = true;
        }
        require_window(&window)?;
        let staged = updater.staged.lock().unwrap().take().ok_or("update_not_downloaded")?;
        let installation = read_package(&staged.1).and_then(|bytes| {
            require_window(&window)?;
            staged.0.install(&bytes).map_err(|_| "update_install_failed".to_string())
        });
        if installation.is_err() {
            *updater.staged.lock().unwrap() = Some(staged);
            if let Some(connection) = owned {
                let _ = call(connection, "desktop-update", Some(json!({"action":"cancel","lease":lease})));
            }
            return installation;
        }
        app.restart();
    })();
    if let Err(ref error) = result {
        if engine_stopped { let _ = bootstrap(&app); }
        let recovery = app.state::<RuntimeState>().local.lock().unwrap().clone();
        if let Some(connection) = recovery.as_ref().or(local.as_ref()).filter(|connection| connection.owned) {
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

#[cfg(test)]
mod window_tests {
    use super::*;
    #[test]
    fn gates_are_checked_again_after_drain_crosses_a_boundary() {
        assert!(authorized_window("night", 2, 5));
        assert!(!authorized_window("night", 2, 6));
        assert!(authorized_window("night", 2, 22));
        assert!(authorized_window("weekend", 7, 23));
        assert!(!authorized_window("weekend", 1, 0));
        assert!(!authorized_window("unknown", 7, 23));
    }
}
