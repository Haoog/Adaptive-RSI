use super::*;
use fs2::available_space;
use std::{fs::File, path::{Path, PathBuf}};
use tauri_plugin_updater::UpdaterExt;

const FEED: &str = "https://github.com/Haoog/Adaptive-RSI/releases/latest/download/desktop-latest.json";
pub(super) const MAX_PACKAGE: usize = 512 * 1024 * 1024;

pub(super) fn require_same_artifact(approved: &Value, current: Option<&Value>) -> Result<(), String> {
    if current == Some(approved) { Ok(()) } else { Err("update_target_changed".into()) }
}

fn artifact(update: &tauri_plugin_updater::Update) -> Value {
    json!({"version":update.version,"url":update.download_url.as_str(),"signature":update.signature})
}

pub(super) fn staged_artifact(app: &tauri::AppHandle) -> Result<Value, String> {
    app.state::<NativeUpdater>().staged.lock().unwrap().as_ref()
        .map(|(update, _)| artifact(update)).ok_or_else(|| "update_not_downloaded".into())
}

pub(super) fn require_space<F>(paths: &[&Path], required: u64, mut available: F) -> Result<(), String>
where F: FnMut(&Path) -> std::io::Result<u64> {
    for path in paths {
        if available(path).map_err(|_| "update_disk_unavailable")? < required {
            return Err("update_disk_space".into());
        }
    }
    Ok(())
}

fn preflight_space(cache: &Path, package: u64) -> Result<(), String> {
    let executable = std::env::current_exe().map_err(|_| "update_disk_unavailable")?;
    let temporary = std::env::temp_dir();
    let extraction = extraction_root(&executable, &temporary)?;
    require_space(&[cache, executable.parent().ok_or("update_disk_unavailable")?, &extraction],
                  package.saturating_mul(3), |path| available_space(path))
}

/// Use the size announced by the feed; an unknown size keeps the conservative bound.
pub(super) fn announced_size(feed: &Value) -> u64 {
    feed.get("size")
        .and_then(Value::as_u64)
        .filter(|size| *size > 0)
        .unwrap_or(MAX_PACKAGE as u64)
}

#[cfg(unix)]
pub(super) fn extraction_root(executable: &Path, temporary: &Path) -> Result<PathBuf, String> {
    use std::os::unix::fs::MetadataExt;
    let bundle = executable.ancestors().find(|path| path.extension().is_some_and(|ext| ext == "app"))
        .ok_or("update_disk_unavailable")?;
    let device = std::fs::symlink_metadata(bundle).map_err(|_| "update_disk_unavailable")?.dev();
    [Some(temporary), bundle.parent()].into_iter().flatten()
        .find(|path| path.metadata().is_ok_and(|metadata| metadata.dev() == device))
        .map(Path::to_path_buf).ok_or_else(|| "update_disk_unavailable".into())
}

#[cfg(not(unix))]
fn extraction_root(_executable: &Path, temporary: &Path) -> Result<PathBuf, String> {
    Ok(temporary.to_path_buf())
}

fn revalidate(app: &tauri::AppHandle, approved: &Value) -> Result<(), String> {
    let current = tauri::async_runtime::block_on(async {
        let updater = configured(app)?;
        cancellable(updater.check(), &app.state::<NativeUpdater>().cancel, "update_check_failed").await
    })?;
    require_same_artifact(approved, current.as_ref().map(artifact).as_ref())
}

pub(super) async fn cancellable<T, E>(operation: impl std::future::Future<Output = Result<T, E>>,
                                     cancel: &AtomicBool, error: &str) -> Result<T, String> {
    tokio::pin!(operation);
    loop {
        if cancel.load(Ordering::SeqCst) { return Err("update_cancelled".into()); }
        tokio::select! {
            result = &mut operation => return result.map_err(|_| error.to_string()),
            _ = tokio::time::sleep(Duration::from_millis(100)) => {}
        }
    }
}

pub struct NativeUpdater {
    pub state: Mutex<Value>,
    busy: std::sync::Arc<AtomicBool>,
    cancel: std::sync::Arc<AtomicBool>,
    staged: Mutex<Option<(tauri_plugin_updater::Update, PathBuf)>>,
}

impl Default for NativeUpdater {
    fn default() -> Self {
        Self { state: Mutex::new(json!({"state":"idle"})), busy: Default::default(),
               cancel: Default::default(), staged: Mutex::new(None) }
    }
}

impl NativeUpdater {
    fn begin(&self) -> Result<UpdateOperation, String> {
        if self.busy.swap(true, Ordering::SeqCst) { return Err("update_busy".into()); }
        self.cancel.store(false, Ordering::SeqCst);
        Ok(UpdateOperation { busy: self.busy.clone(), cancel: self.cancel.clone() })
    }

    fn set(&self, value: Value) {
        *self.state.lock().unwrap() = value;
    }
}

// Own the slot across async preflight and the blocking worker, including worker errors.
struct UpdateOperation {
    busy: std::sync::Arc<AtomicBool>,
    cancel: std::sync::Arc<AtomicBool>,
}

impl UpdateOperation {
    fn run_install(self, install: impl FnOnce() -> Result<(), String>) -> Result<(), String> {
        if self.cancel.load(Ordering::SeqCst) { return Err("update_cancelled".into()); }
        install()
    }
}

impl Drop for UpdateOperation {
    fn drop(&mut self) { self.busy.store(false, Ordering::SeqCst); }
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
    fn cancellation_at_install_handoff_never_stops_or_replaces() {
        let updater = NativeUpdater::default();
        let operation = updater.begin().unwrap();
        let (handoff, received) = std::sync::mpsc::channel();
        let (resume, resumed) = std::sync::mpsc::channel();
        let stopped = AtomicBool::new(false);
        let replaced = AtomicBool::new(false);
        std::thread::scope(|scope| {
            let stopped = &stopped;
            let replaced = &replaced;
            let worker = scope.spawn(move || {
                handoff.send(()).unwrap();
                resumed.recv().unwrap();
                operation.run_install(|| {
                    stopped.store(true, Ordering::SeqCst);
                    replaced.store(true, Ordering::SeqCst);
                    Ok(())
                })
            });
            received.recv().unwrap();
            assert!(updater.busy.load(Ordering::SeqCst));
            assert!(updater.begin().is_err());
            updater.cancel.store(true, Ordering::SeqCst);
            assert!(updater.begin().is_err());
            assert!(updater.cancel.load(Ordering::SeqCst));
            resume.send(()).unwrap();
            assert_eq!(worker.join().unwrap().unwrap_err(), "update_cancelled");
        });
        assert!(!stopped.load(Ordering::SeqCst));
        assert!(!replaced.load(Ordering::SeqCst));
        assert!(!updater.busy.load(Ordering::SeqCst));
        let next = updater.begin().unwrap();
        assert!(!updater.cancel.load(Ordering::SeqCst));
        next.run_install(|| Ok(())).unwrap();
        assert!(!updater.busy.load(Ordering::SeqCst));
    }

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
        .timeout(Duration::from_secs(30))
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
    let Ok(_operation) = updater.begin() else { return Ok(snapshot(&app)); };
    check_in_operation(&app, download).await
}

async fn check_in_operation(app: &tauri::AppHandle, download: bool) -> Result<Value, String> {
    let updater = app.state::<NativeUpdater>();
    let outcome = async {
        updater.set(json!({"state":"checking"}));
        let client = configured(&app)?;
        let update = cancellable(client.check(), &updater.cancel, "update_check_failed").await?;
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
        preflight_space(&cache, announced_size(&update.raw_json))?;
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

pub async fn install(app: tauri::AppHandle, window: String) -> Result<(), String> {
    let operation = app.state::<NativeUpdater>().begin()?;
    let approved = staged_artifact(&app)?;
    let eligibility = check_in_operation(&app, false).await?;
    if eligibility["state"] != "ready" { return Err("update_not_downloaded".into()); }
    require_same_artifact(&approved, Some(&staged_artifact(&app)?))?;
    tauri::async_runtime::spawn_blocking(move || {
        operation.run_install(|| install_in_operation(app, window, approved))
    }).await.map_err(|_| "update_worker_failed".to_string())?
}

fn install_in_operation(app: tauri::AppHandle, window: String, approved: Value) -> Result<(), String> {
    let updater = app.state::<NativeUpdater>();
    let local = app.state::<RuntimeState>().local.lock().unwrap().clone();
    let mut lease = Value::Null;
    let mut engine_stopped = false;
    let result: Result<(), String> = (|| {
        require_window(&window)?;
        let version = updater.staged.lock().unwrap().as_ref().map(|(update, _)| update.version.clone())
            .ok_or("update_not_downloaded")?;
        require_same_artifact(&approved, Some(&staged_artifact(&app)?))?;
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
            revalidate(&app, &approved)?;
            let reply = call(connection, "desktop-update", Some(json!({"action":"commit","lease":lease})))?;
            if reply.status != 200 { return Err("update_drain_failed".into()); }
        }
        if updater.cancel.load(Ordering::SeqCst) { return Err("update_wait_cancelled".into()); }
        require_window(&window)?;
        if owned.is_none() { revalidate(&app, &approved)?; }
        let cache = app.path().app_cache_dir().map_err(|_| "update_cache_unavailable")?;
        preflight_space(&cache, announced_size(&approved))?;
        if updater.cancel.load(Ordering::SeqCst) { return Err("update_wait_cancelled".into()); }
        updater.set(json!({"state":"installing","version":version}));
        // Replacement starts only after owned work has drained. Other services are not stopped.
        // A stop request is irreversible: never treat a child whose stdin was already
        // closed as a usable connection, even if it has not exited within the grace period.
        if runtime.child.lock().unwrap().is_some() {
            match super::shutdown_runtime(runtime.inner(), Duration::from_secs(30)) {
                Ok(mut exiting) => {
                    if !super::wait_for_child(&mut exiting, Duration::from_secs(10)) {
                        *runtime.child.lock().unwrap() = Some(exiting);
                        return Err("update_runtime_shutdown_failed".into());
                    }
                    engine_stopped = true;
                }
                Err(error) => return Err(error),
            }
        }
        require_window(&window)?;
        let staged = updater.staged.lock().unwrap().take().ok_or("update_not_downloaded")?;
        let installation = read_package(&staged.1).and_then(|bytes| {
            require_window(&window)?;
            if updater.cancel.load(Ordering::SeqCst) { return Err("update_wait_cancelled".into()); }
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
        let runtime_state = app.state::<RuntimeState>();
        if runtime_state.recovering.load(Ordering::SeqCst) {
            // The old service may still be exiting after the grace period. Watch for
            // its real exit, then restore a fresh owned runtime instead of exposing
            // a stale connection or reopening the update barrier.
            let handle = app.clone();
            std::thread::spawn(move || {
                let deadline = std::time::Instant::now() + Duration::from_secs(180);
                loop {
                    let state = handle.state::<RuntimeState>();
                    let exited = {
                        let mut guard = state.child.lock().unwrap();
                        match guard.as_mut() {
                            Some(child) => matches!(child.try_wait(), Ok(Some(_))),
                            None => true,
                        }
                    };
                    if exited {
                        *state.child.lock().unwrap() = None;
                        let _ = bootstrap(&handle);
                        break;
                    }
                    if std::time::Instant::now() >= deadline { break; }
                    std::thread::sleep(Duration::from_millis(500));
                }
            });
        }
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
