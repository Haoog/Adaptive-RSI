#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use reqwest::blocking::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    fs::{self, File},
    io::{self, BufRead, IsTerminal, Read, Write},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering},
    sync::{Mutex, MutexGuard},
    time::Duration,
};
use tauri::{
    menu::{Menu, MenuItem, Submenu},
    tray::TrayIconBuilder,
    Manager,
};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_opener::OpenerExt;

mod native_update;
mod update_receipt;
#[cfg(test)]
mod update_boundary_tests;
mod transport;
mod update_check_race;

#[derive(Clone)]
struct Connection {
    client: Client,
    transport: transport::Transport,
    origin: String,
    csrf_origin: String,
    cookie: Option<String>,
    csrf: String,
    name: String,
    owned: bool,
}
#[derive(Default)]
struct RuntimeState {
    target: Mutex<Option<Connection>>,
    local: Mutex<Option<Connection>>,
    local_start_gate: Mutex<()>,
    child: Mutex<Option<Child>>,
    error: Mutex<String>,
    update: Mutex<Option<DesktopUpdate>>,
    download_starts: AtomicU64,
    transport: transport::Control,
    connection_generation: AtomicU64,
    generation: AtomicU64,
    pid: AtomicU32,
    recovering: AtomicBool,
    remote_meta: Mutex<Value>,
}

///桌面应用自身的更新状态；percent 存百分比×100，避免浮点原子。
struct DesktopUpdate {
    version: String,
    notes: String,
    url: String,
    sha_url: String,
    error: String,
    error_code: String,
    percent: AtomicU64,
    downloading: AtomicBool,
    opened: AtomicBool,
}

impl DesktopUpdate {
    fn json(&self) -> Value {
        json!({
            "version": self.version,
            "notes": self.notes,
            "downloading": self.downloading.load(Ordering::Relaxed),
            "percent": self.percent.load(Ordering::Relaxed) as f64 / 100.0,
            "opened": self.opened.load(Ordering::Relaxed),
            "error": self.error,
            "error_code": self.error_code,
        })
    }
}

const UPDATE_REPO: &str = "Haoog/Adaptive-RSI";

// 进程级下载闸门：由下载线程持有直到结束。双下载防护因此不依赖
// DesktopUpdate.downloading 这个可被并发状态替换清零的标志。
static DOWNLOAD_GATE: Mutex<()> = Mutex::new(());
const BACKUP_IDLE: u8 = 0;
const BACKUP_DOWNLOADING: u8 = 1;
const BACKUP_CANCELLED: u8 = 2;
const BACKUP_PUBLISHING: u8 = 3;
static BACKUP_ACTIVITY: std::sync::atomic::AtomicU8 = std::sync::atomic::AtomicU8::new(BACKUP_IDLE);
static BACKUP_BYTES: AtomicU64 = AtomicU64::new(0);
static BACKUP_TOTAL: AtomicU64 = AtomicU64::new(0);
static QUITTING: AtomicBool = AtomicBool::new(false);

const LOCAL_API_VERSION: u64 = 2;

/// Remote runtimes may expose API v1 or v2.
fn supported_remote_api(version: u64) -> bool {
    matches!(version, 1 | 2)
}

fn update_redirect_allowed(url: &reqwest::Url, hops: usize) -> bool {
    url.scheme() == "https" && hops <= 5 && url.host_str().is_some_and(|host| {
        let name = host.trim_end_matches('.');
        if name == "localhost" || name.ends_with(".localhost") { return false; }
        !host.trim_matches(['[', ']']).parse::<std::net::IpAddr>().is_ok_and(|ip| {
            let ip = match ip {
                std::net::IpAddr::V6(ip) => ip.to_ipv4_mapped().map_or(ip.into(), Into::into),
                ip => ip,
            };
            ip.is_loopback() || ip.is_unspecified()
        })
    })
}

fn update_redirect_policy() -> reqwest::redirect::Policy {
    reqwest::redirect::Policy::custom(|attempt| {
        if update_redirect_allowed(attempt.url(), attempt.previous().len()) {
            attempt.follow()
        } else {
            attempt.error("updater redirect refused")
        }
    })
}

fn update_client() -> Result<Client, String> {
    Client::builder()
        .no_proxy()
        .https_only(true)
        .redirect(update_redirect_policy())
        .timeout(Duration::from_secs(600))
        .build()
        .map_err(|_| "Cannot create updater connection".into())
}

fn parse_version(value: &str) -> Option<(u64, u64, u64)> {
    let mut numbers = value.trim().split('.');
    let number = |part: &str| {
        if part.is_empty()
            || !part.bytes().all(|c| c.is_ascii_digit())
            || (part.len() > 1 && part.starts_with('0'))
        {
            None
        } else {
            part.parse::<u64>().ok()
        }
    };
    let major = number(numbers.next()?)?;
    let minor = number(numbers.next()?)?;
    let patch = number(numbers.next()?)?;
    if numbers.next().is_some() {
        return None;
    }
    Some((major, minor, patch))
}

// 下载中的状态任何情况下都必须保留：force 重查若替换状态，会把 downloading 清零、
// 让前端误判可重新下载，并绕过下载闸门留下的状态一致性。已结束（成功打开/失败）
// 的状态保留给轮询，直到显式强制刷新。
fn should_preserve_update(current: &DesktopUpdate, force: bool) -> bool {
    current.downloading.load(Ordering::Relaxed)
        || (!force && (current.opened.load(Ordering::Relaxed) || !current.error.is_empty()))
}

fn check_desktop_update_blocking(app: &tauri::AppHandle, force: bool) -> Option<Value> {
    let state = app.state::<RuntimeState>();
    static CHECK_GATE: Mutex<()> = Mutex::new(());
    let Ok(_check) = CHECK_GATE.try_lock() else {
        return state.update.lock().unwrap().as_ref().map(DesktopUpdate::json);
    };
    let (preserved, observed) = update_check_race::inspect(&state.update, &state.download_starts,
        |current| should_preserve_update(current, force).then(|| current.json()));
    if preserved.is_some() { return preserved; }
    // All outcomes, including network errors, pass through the same late-result guard.
    let result = (|| -> Result<Option<DesktopUpdate>, ()> {
        let client = update_client().map_err(|_| ())?;
        let response = client
            .get(format!("https://api.github.com/repos/{}/releases/latest", UPDATE_REPO))
            .header("User-Agent", "Adaptive-RSI-updater")
            .header("Accept", "application/vnd.github+json")
            .send().map_err(|_| ())?;
        if !response.status().is_success() { return Err(()); }
        let release: Value = response.json().map_err(|_| ())?;
        desktop_release(&release, &app.package_info().version.to_string())
    })().map_err(|()| json!({"check_error": true, "error": "check_failed"}));
    update_check_race::publish(&state.update, &state.download_starts, observed, result,
        |current| should_preserve_update(current, force), DesktopUpdate::json)
}

fn desktop_release(release: &Value, current: &str) -> Result<Option<DesktopUpdate>, ()> {
    let tag = release.get("tag_name").and_then(Value::as_str).ok_or(())?;
    let tag = tag.strip_prefix('v').unwrap_or(tag);
    let target = parse_version(tag).ok_or(())?;
    let now = parse_version(current).ok_or(())?;
    if target <= now {
        return Ok(None);
    }
    let arch = match std::env::consts::ARCH {
        "x86_64" => "x86_64",
        _ => "arm64",
    };
    let dmg_name = format!("adaptive-rsi-{}-macos-{}-adhoc.dmg", tag, arch);
    let mut url = None;
    let mut sha_url = None;
    if let Some(assets) = release.get("assets").and_then(Value::as_array) {
        for asset in assets {
            let name = asset.get("name").and_then(Value::as_str).unwrap_or("");
            let link = asset.get("browser_download_url").and_then(Value::as_str);
            if name == dmg_name {
                url = link.map(str::to_string);
            } else if name == format!("{}.sha256", dmg_name) {
                sha_url = link.map(str::to_string);
            }
        }
    }
    let valid_asset = |url: Option<String>| -> Result<String, ()> {
        let url = url.ok_or(())?;
        let parsed = reqwest::Url::parse(&url).map_err(|_| ())?;
        if parsed.scheme() != "https"
            || parsed.host_str().is_none()
            || !parsed.username().is_empty()
            || parsed.password().is_some()
        {
            return Err(());
        }
        Ok(url)
    };
    let (url, sha_url) = (valid_asset(url)?, valid_asset(sha_url)?);
    let notes = release
        .get("body")
        .and_then(Value::as_str)
        .unwrap_or("")
        .chars()
        .take(16000)
        .collect::<String>();
    Ok(Some(DesktopUpdate {
        version: tag.to_string(),
        notes,
        url,
        sha_url,
        error: String::new(),
        error_code: String::new(),
        percent: AtomicU64::new(0),
        downloading: AtomicBool::new(false),
        opened: AtomicBool::new(false),
    }))
}

// 先占住下载闸门，再进更新锁检查并置位 downloading；返回的闸门由下载线程
// 持有直到结束，任何路径都不可能有两个写线程并发写同一个安装镜像。
fn begin_desktop_download<'a>(
    state: &RuntimeState,
    gate: &'a Mutex<()>,
) -> Result<(String, String, String, MutexGuard<'a, ()>), String> {
    let gate = gate
        .try_lock()
        .map_err(|_| "Desktop update is already downloading".to_string())?;
    let mut guard = state.update.lock().unwrap();
    match guard.as_mut() {
        Some(update) if !update.downloading.load(Ordering::Relaxed) => {
            update.downloading.store(true, Ordering::Relaxed);
            update.percent.store(0, Ordering::Relaxed);
            update.error.clear();
            update.error_code.clear();
            update.opened.store(false, Ordering::Relaxed);
            update_check_race::started(&state.download_starts);
            Ok((
                update.url.clone(),
                update.sha_url.clone(),
                update.version.clone(),
                gate,
            ))
        }
        _ => Err("No desktop update available".into()),
    }
}

fn start_desktop_download(app: tauri::AppHandle) -> Result<(), String> {
    let (sender, receiver) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        // 先占住进程级下载闸门再进更新锁置位 downloading；闸门由本线程持有
        // 直到下载结束，双下载防护因此不依赖可被并发状态替换清零的标志。
        // MutexGuard 不能跨线程移动，闸门获取结果经 channel 同步回报给调用方。
        let state = app.state::<RuntimeState>();
        // 闸门 guard 以 _gate 绑定持有到线程结束（下划线绑定不会提前释放）。
        let (url, sha_url, version, _gate) = match begin_desktop_download(&state, &DOWNLOAD_GATE) {
            Ok(started) => {
                let _ = sender.send(Ok(()));
                started
            }
            Err(error) => {
                let _ = sender.send(Err(error));
                return;
            }
        };
        let fail = |message: &str, code: &str| complete_desktop_download(&state, Err((message, code)));
        let client = match update_client() {
            Ok(client) => client,
            Err(error) => {
                fail(&error, "");
                return;
            }
        };
        let expected = match client
            .get(&sha_url)
            .header("User-Agent", "Adaptive-RSI-updater")
            .send()
        {
            Ok(response) if response.status().is_success() => {
                let mut text = String::new();
                if response.take(4097).read_to_string(&mut text).is_err() || text.len() > 4096 {
                    fail("Checksum download failed", "checksum_unavailable");
                    return;
                }
                text.split_whitespace().next().unwrap_or("").to_lowercase()
            }
            _ => {
                fail("Checksum download failed", "checksum_unavailable");
                return;
            }
        };
        if expected.len() != 64 || !expected.bytes().all(|b| b.is_ascii_hexdigit()) {
            fail("Checksum unavailable", "checksum_unavailable");
            return;
        }
        let arch = match std::env::consts::ARCH {
            "x86_64" => "x86_64",
            _ => "arm64",
        };
        let download_dir = std::env::var("HOME")
            .ok()
            .filter(|h| !h.is_empty())
            .map(|h| std::path::PathBuf::from(h).join("Downloads"))
            .unwrap_or_else(std::env::temp_dir);
        let path = download_dir.join(format!("adaptive-rsi-{}-macos-{}-adhoc.dmg", version, arch));
        let download = |staged: &mut File| -> Result<(), &'static str> {
            let mut response = client.get(&url)
                .header("User-Agent", "Adaptive-RSI-updater")
                .send().map_err(|_| "Download failed")?;
            if response.status() != reqwest::StatusCode::OK {
                return Err("Download failed");
            }
            let total = response.content_length().unwrap_or(0);
            if total > native_update::MAX_PACKAGE as u64 { return Err("Download exceeds size limit"); }
            let mut downloaded = 0u64;
            let mut buffer = [0u8; 65536];
            loop {
                let read = response
                    .read(&mut buffer)
                    .map_err(|_| "Download interrupted")?;
                if read == 0 {
                    break;
                }
                if downloaded.saturating_add(read as u64) > native_update::MAX_PACKAGE as u64 {
                    return Err("Download exceeds size limit");
                }
                staged.write_all(&buffer[..read])
                    .map_err(|_| "Cannot write download")?;
                downloaded += read as u64;
                if total > 0 {
                    let state = app.state::<RuntimeState>();
                    let guard = state.update.lock().unwrap();
                    if let Some(update) = guard.as_ref() {
                        update
                            .percent
                            .store(downloaded * 10000 / total, Ordering::Relaxed);
                    }
                }
            }
            Ok(())
        };
        let result = stage_legacy_dmg(&path, &expected, download, attach_legacy_dmg);
        complete_desktop_download(&state, result);
    });
    // 闸门 try_lock 即刻完成，这里的短暂等待只把启动结果同步给前端。
    receiver
        .recv()
        .unwrap_or_else(|_| Err("Download did not start".into()))
}

fn complete_desktop_download(state: &RuntimeState, result: Result<(), (&str, &str)>) {
    let mut guard = state.update.lock().unwrap();
    if let Some(update) = guard.as_mut() {
        match result {
            Ok(()) => {
                update.percent.store(10000, Ordering::Relaxed);
                update.opened.store(true, Ordering::Relaxed);
            }
            Err((message, code)) => {
                update.error = message.into();
                update.error_code = code.into();
            }
        }
        update.downloading.store(false, Ordering::Relaxed);
    }
}

fn stage_legacy_dmg(
    path: &Path,
    expected: &str,
    download: impl FnOnce(&mut File) -> Result<(), &'static str>,
    attach: impl FnOnce(&Path) -> bool,
) -> Result<(), (&'static str, &'static str)> {
    const EXISTS: &str = "A download with this name already exists";
    const CANNOT_WRITE: &str = "Cannot write download";
    const CANNOT_READ: &str = "Cannot read existing installer";

    // Previous versions may have published a verified image before attach failed.
    // Open that image defensively and mount a fresh private copy after rechecking it.
    let existing = match path.symlink_metadata() {
        Ok(_) => Some(
            native_update::open_bounded_file(path, native_update::MAX_PACKAGE as u64)
                .map_err(|_| (EXISTS, ""))?,
        ),
        Err(error) if error.kind() == io::ErrorKind::NotFound => None,
        Err(_) => return Err((CANNOT_WRITE, "")),
    };
    let already_published = existing.is_some();
    let download_dir = path.parent().ok_or((CANNOT_WRITE, ""))?;
    let staging = tempfile::Builder::new()
        .prefix(".adaptive-rsi-")
        .tempdir_in(download_dir)
        .map_err(|_| (CANNOT_WRITE, ""))?;
    let mut staged = tempfile::Builder::new()
        .suffix(".dmg")
        .tempfile_in(staging.path())
        .map_err(|_| (CANNOT_WRITE, ""))?;
    if let Some((file, size)) = existing {
        let count = io::copy(&mut file.take(size + 1), &mut staged).map_err(|_| (CANNOT_READ, ""))?;
        if count != size {
            return Err((CANNOT_READ, ""));
        }
    } else {
        download(staged.as_file_mut()).map_err(|message| (message, ""))?;
    }
    staged.flush().map_err(|_| (CANNOT_WRITE, ""))?;

    let digest = Command::new("/usr/bin/shasum")
        .arg("-a")
        .arg("256")
        .arg(staged.path())
        .output()
        .ok()
        .filter(|output| output.status.success())
        .and_then(|output| String::from_utf8(output.stdout).ok())
        .and_then(|text| text.split_whitespace().next().map(str::to_lowercase))
        .unwrap_or_default();
    if digest != expected {
        return Err(("Checksum verification failed", "checksum"));
    }
    if !attach(staged.path()) {
        return Err(("Cannot open installer", ""));
    }
    if !already_published {
        staged.persist_noclobber(path).map_err(|error| {
            if error.error.kind() == io::ErrorKind::AlreadyExists {
                (EXISTS, "")
            } else {
                (CANNOT_WRITE, "")
            }
        })?;
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn attach_legacy_dmg(path: &Path) -> bool {
    let Ok(mut child) = Command::new("/usr/bin/hdiutil").arg("attach").arg(path).spawn() else {
        return false;
    };
    if !wait_for_child(&mut child, Duration::from_secs(60)) {
        let _ = child.kill();
        let _ = child.wait();
        return false;
    }
    child.wait().is_ok_and(|status| status.success())
}

#[cfg(not(target_os = "macos"))]
fn attach_legacy_dmg(_: &Path) -> bool {
    true
}

struct PendingChild(Option<Child>);

impl PendingChild {
    fn new(child: Child) -> Self {
        Self(Some(child))
    }

    fn get_mut(&mut self) -> &mut Child {
        self.0.as_mut().expect("pending runtime child")
    }

    fn take(&mut self) -> Child {
        self.0.take().expect("pending runtime child")
    }
}

// try_wait 同时负责回收；kill 失败或系统迟迟不回收时也不能无限 wait。
fn wait_for_child(child: &mut Child, timeout: Duration) -> bool {
    let deadline = std::time::Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return true,
            Err(_) => return false,
            Ok(None) => {}
        }
        let remaining = deadline.saturating_duration_since(std::time::Instant::now());
        if remaining.is_zero() {
            return false;
        }
        std::thread::sleep(remaining.min(Duration::from_millis(100)));
    }
}

fn stop_child(child: &mut Child, grace: Duration) {
    drop(child.stdin.take());
    if !wait_for_child(child, grace) {
        let _ = child.kill();
        let _ = wait_for_child(child, Duration::from_secs(2));
    }
}

///持续读取私有握手管道；每次只接受一条有界消息，读取端销毁后线程自然结束。
fn handshake_stream<R: Read + Send + 'static>(
    reader: R,
) -> std::sync::mpsc::Receiver<std::io::Result<String>> {
    const MAX_HANDSHAKE_LINE: usize = 64 * 1024;
    let (sender, receiver) = std::sync::mpsc::sync_channel(4);
    std::thread::spawn(move || {
        let mut reader = reader;
        let mut pending = vec![0; MAX_HANDSHAKE_LINE + 1];
        let mut len = 0;
        loop {
            // Pass the remaining allowance directly to Read, including one byte
            // to distinguish a full line from an oversized line without waiting for EOF.
            let count = match reader.read(&mut pending[len..]) {
                Ok(0) => {
                    if len != 0 {
                        let _ = sender.send(Ok(String::from_utf8_lossy(&pending[..len]).into_owned()));
                    }
                    break;
                }
                Ok(count) => count,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => {
                    let _ = sender.send(Err(error));
                    break;
                }
            };
            let mut start = 0;
            let end = len + count;
            for index in len..end {
                if index - start == MAX_HANDSHAKE_LINE {
                    let _ = sender.send(Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "runtime handshake line exceeds bound",
                    )));
                    return;
                }
                if pending[index] == b'\n' {
                    let line = String::from_utf8_lossy(&pending[start..=index]).into_owned();
                    if sender.send(Ok(line)).is_err() {
                        return;
                    }
                    start = index + 1;
                }
            }
            if start != 0 {
                pending.copy_within(start..end, 0);
            }
            len = end - start;
        }
    });
    receiver
}

///只接受同一服务代次、同一进程及其原有连接的轮换回执。
fn publish_runtime_session(state: &RuntimeState, generation: u64, pid: u32, session: Session) -> bool {
    if !session.rotated || session.pid != Some(pid) {
        return false;
    }
    let origin = session
        .local_origin
        .clone()
        .unwrap_or_else(|| session.origin.clone());
    let name = session.cookie.split('=').next().unwrap_or("");
    if name.is_empty() {
        return false;
    }
    let mut target = state.target.lock().unwrap();
    let mut local = state.local.lock().unwrap();
    if generation != state.generation.load(Ordering::SeqCst) {
        return false;
    }
    let Some(current) = local.as_mut() else {
        return false;
    };
    let cookie_name = current.cookie.as_deref().and_then(|cookie| cookie.split_once('='));
    if origin != current.origin
        || session.origin != current.csrf_origin
        || session.owned != current.owned
        || cookie_name.map(|(name, _)| name) != Some(name)
    {
        return false;
    }
    current.csrf = session.csrf;
    current.cookie = Some(format!("{name}={}", session.token));
    if let Some(selected) = target.as_mut().filter(|selected| selected.transport.same_connection(&current.transport)) {
        if state.remote_meta.lock().unwrap().is_null() {
            selected.csrf = current.csrf.clone();
            selected.cookie = current.cookie.clone();
        }
    }
    true
}

///请求旧运行器退出。超时后保留仍在退出的子进程，但立即撤销可用连接，
///避免后续 API 或更新回执继续使用已失去控制管道的服务。
fn shutdown_runtime(state: &RuntimeState, grace: Duration) -> Result<Child, String> {
    let mut child = state
        .child
        .lock()
        .unwrap()
        .take()
        .ok_or("Runtime is not running")?;
    if let Some(mut stdin) = child.stdin.take() {
        let _ = stdin.flush();
    }
    let mut target = state.target.lock().unwrap();
    let mut local = state.local.lock().unwrap();
    let mut meta = state.remote_meta.lock().unwrap();
    if meta.is_null() && target.as_ref().is_some_and(|connection| connection.owned) {
        state.transport.cancel_all();
        state.connection_generation.fetch_add(1, Ordering::SeqCst);
        *target = None;
    }
    if local.as_ref().is_some_and(|connection| connection.owned) {
        *local = None;
    }
    if target.is_none() {
        *meta = Value::Null;
    }
    // The previous connection is gone even if the child exits within the grace period.
    state.recovering.store(true, Ordering::SeqCst);
    drop((meta, local, target));
    wait_for_child(&mut child, grace);
    Ok(child)
}

// Call with local_start_gate held while recovering from an update.
fn reap_update_runtime(state: &RuntimeState) -> bool {
    let mut child = state.child.lock().unwrap();
    if child.as_mut().is_some_and(|child| !matches!(child.try_wait(), Ok(Some(_)))) {
        return false;
    }
    child.take();
    true
}

fn publish_local_runtime(state: &RuntimeState, child: Child, connection: Connection, select: bool) -> u64 {
    let pid = child.id();
    let mut running = state.child.lock().unwrap();
    let mut target = state.target.lock().unwrap();
    let mut local = state.local.lock().unwrap();
    let meta = state.remote_meta.lock().unwrap();
    *running = Some(child);
    *local = Some(connection.clone());
    if select && meta.is_null() {
        if state.recovering.load(Ordering::SeqCst) {
            state.connection_generation.fetch_add(1, Ordering::SeqCst);
        }
        *target = Some(connection);
    }
    let generation = state.generation.fetch_add(1, Ordering::SeqCst) + 1;
    state.pid.store(pid, Ordering::SeqCst);
    state.recovering.store(false, Ordering::SeqCst);
    generation
}

// An attached local service can outlive its watcher, so only an owned
// child's exit invalidates the local connection.
fn invalidate_exited_local(state: &RuntimeState) {
    let Ok(mut running) = state.child.try_lock() else {
        return;
    };
    let Some(child) = running.as_mut() else {
        return;
    };
    if !matches!(child.try_wait(), Ok(Some(_))) {
        return;
    }
    let mut target = state.target.lock().unwrap();
    let mut local = state.local.lock().unwrap();
    let Some(connection) = local.as_ref().filter(|connection| connection.owned) else {
        return;
    };
    let stale_target = if target
        .as_ref()
        .is_some_and(|current| current.owned && current.origin == connection.origin)
    {
        *state.remote_meta.lock().unwrap() = Value::Null;
        target.take()
    } else {
        None
    };
    *state.error.lock().unwrap() = "Local runtime exited".into();
    let stale_local = local.take();
    drop(local);
    drop(target);
    drop(running);
    drop((stale_target, stale_local));
}

impl Drop for PendingChild {
    fn drop(&mut self) {
        if let Some(child) = self.0.as_mut() {
            stop_child(child, Duration::ZERO);
        }
    }
}

#[derive(Deserialize)]
struct Session {
    origin: String,
    local_origin: Option<String>,
    cookie: String,
    token: String,
    csrf: String,
    owned: bool,
    #[serde(default)]
    pid: Option<u32>,
    #[serde(default)]
    rotated: bool,
}
#[derive(Serialize)]
struct Reply {
    status: u16,
    body: String,
    #[serde(rename = "requestId", skip_serializing_if = "Option::is_none")]
    request_id: Option<String>,
}

fn valid_api_path(path: &str) -> bool {
    let endpoint = path.split('?').next().unwrap_or("");
    !endpoint.is_empty()
        && path.len() <= 4096
        && !path.starts_with('/')
        && !path.contains('\\')
        && !path.contains('#')
        && !endpoint.split('/').any(|p| p == ".." || p == ".")
        && endpoint
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '/' || c == '_' || c == '-')
}
fn call(c: &Connection, path: &str, body: Option<Value>) -> Result<Reply, String> {
    call_with_session(c, path, body, None).map(|(reply, _)| reply)
}

fn call_with_session(
    c: &Connection,
    path: &str,
    body: Option<Value>,
    timeout: Option<Duration>,
) -> Result<(Reply, Option<(String, String)>), String> {
    if !valid_api_path(path) {
        return Err("Invalid API path".into());
    }
    let rotating = path.split('?').next() == Some("session/rotate") && body.is_some();
    let mut request = if let Some(data) = body {
        c.client
            .post(format!("{}/api/{}", c.origin, path))
            .json(&data)
            .header("Origin", &c.csrf_origin)
            .header("X-RSI-CSRF", &c.csrf)
    } else {
        c.client.get(format!("{}/api/{}", c.origin, path))
    };
    if let Some(cookie) = &c.cookie {
        request = request.header("Cookie", cookie);
    }
    if let Some(timeout) = timeout {
        request = request.timeout(timeout);
    }
    let response = request
        .send()
        .map_err(|_| "Cannot connect to the selected runtime".to_string())?;
    let status = response.status().as_u16();
    let request_id = response
        .headers()
        .get("X-RSI-Request-ID")
        .and_then(|value| value.to_str().ok())
        .map(str::to_string);
    let session_cookie = if rotating && response.status().is_success() {
        response
            .cookies()
            .find(|cookie| {
                c.cookie
                    .as_deref()
                    .and_then(|value| value.split_once('='))
                    .map(|(name, _)| cookie.name() == name)
                    .unwrap_or_else(|| cookie.name().starts_with("rsi_"))
            })
            .map(|cookie| format!("{}={}", cookie.name(), cookie.value()))
    } else {
        None
    };
    let mut data = Vec::new();
    response
        .take(16 * 1024 * 1024 + 1)
        .read_to_end(&mut data)
        .map_err(|_| "Cannot read response")?;
    if data.len() > 16 * 1024 * 1024 {
        return Err("Response too large".into());
    }
    let reply = Reply {
        status,
        body: String::from_utf8(data).map_err(|_| "Invalid response encoding")?,
        request_id,
    };
    let credentials = if rotating && (200..300).contains(&status) {
        let value: Value =
            serde_json::from_str(&reply.body).map_err(|_| "Invalid session response")?;
        let csrf = value
            .get("csrf")
            .and_then(Value::as_str)
            .filter(|csrf| !csrf.is_empty())
            .ok_or("Missing runtime authentication")?;
        Some((
            session_cookie.ok_or("Missing runtime session cookie")?,
            csrf.to_string(),
        ))
    } else {
        None
    };
    Ok((reply, credentials))
}


fn bootstrap(app: &tauri::AppHandle) -> Result<(), String> {
    bootstrap_local(app, true)
}

fn startup_budget(deadline: std::time::Instant) -> Result<Duration, String> {
    let remaining = deadline.saturating_duration_since(std::time::Instant::now());
    if remaining.is_zero() {
        return Err("Runtime did not become healthy within 60 seconds".into());
    }
    Ok(remaining)
}

fn prepare_runtime_log_directory(path: &Path) -> io::Result<PathBuf> {
    let mut builder = fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder.create(path)?;
    let invalid = || io::Error::new(io::ErrorKind::InvalidInput, "Invalid runtime log directory");
    if !fs::symlink_metadata(path)?.is_dir() {
        return Err(invalid());
    }
    // Resolve system aliases in the parent without accepting a redirected app directory.
    let parent = path.parent().ok_or_else(invalid)?.canonicalize()?;
    let expected = parent.join(path.file_name().ok_or_else(invalid)?);
    let directory = path.canonicalize()?;
    if directory != expected {
        return Err(invalid());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
        let dir = fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW)
            .open(&directory)?;
        if dir.metadata()?.permissions().mode() & 0o777 != 0o700 {
            dir.set_permissions(fs::Permissions::from_mode(0o700))?;
        }
    }
    Ok(directory)
}

fn runtime_log_directory(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let path = app.path().app_log_dir().map_err(|_| "Cannot locate runtime log")?;
    prepare_runtime_log_directory(&path).map_err(|_| "Cannot prepare runtime log directory".into())
}

fn rotate_runtime_log(directory: &Path) -> io::Result<File> {
    let current = directory.join("runtime.log");
    let previous = directory.join("runtime.log.1");
    // Stage a private file before touching either generation. If the final
    // publish fails, the last run is still available under the previous name.
    let fresh = tempfile::NamedTempFile::new_in(directory)?;
    match fs::symlink_metadata(&current) {
        Ok(entry) if entry.is_file() => {
            #[cfg(unix)]
            {
                use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
                let old = fs::OpenOptions::new()
                    .read(true)
                    .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
                    .open(&current)?;
                let metadata = old.metadata()?;
                if !metadata.is_file()
                    || metadata.nlink() != 1
                    || metadata.dev() != entry.dev()
                    || metadata.ino() != entry.ino()
                {
                    return Err(io::Error::new(io::ErrorKind::InvalidInput, "Invalid runtime log"));
                }
                old.set_permissions(fs::Permissions::from_mode(0o600))?;
            }
            fs::rename(&current, &previous)?;
        }
        Ok(_) => return Err(io::Error::new(io::ErrorKind::InvalidInput, "Invalid runtime log")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    fresh.persist_noclobber(current).map_err(|error| error.error)
}

const WINDOWS_REMOTE_ONLY: &str = "Use a Linux/macOS server connection on Windows";

fn bootstrap_local(app: &tauri::AppHandle, select: bool) -> Result<(), String> {
    if cfg!(target_os = "windows") {
        return Err(WINDOWS_REMOTE_ONLY.into());
    }
    let exe = std::env::current_exe().map_err(|_| "Cannot locate app")?;
    let binary = exe
        .parent()
        .ok_or("Cannot locate engine")?
        .join("adaptive-rsi-engine");
    let mut command = if binary.exists() {
        Command::new(binary)
    } else if cfg!(debug_assertions) {
        let mut command = Command::new("python3");
        command.args(["-m", "adaptive_rsi"]);
        command.current_dir(std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."));
        command
    } else {
        return Err("Bundled runtime is missing".into());
    };
    let mut paths: Vec<std::path::PathBuf> =
        std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()).collect();
    if let Some(home) = std::env::var_os("HOME") {
        let home = std::path::PathBuf::from(home);
        for suffix in [".local/bin", ".opencode/bin", ".cargo/bin", ".volta/bin"] {
            let path = home.join(suffix);
            if path.is_dir() && !paths.contains(&path) {
                paths.push(path);
            }
        }
    }
    for prefix in ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"] {
        let path = std::path::PathBuf::from(prefix);
        if path.is_dir() && !paths.contains(&path) {
            paths.push(path);
        }
    }
    command.env(
        "PATH",
        std::env::join_paths(paths).map_err(|_| "Cannot prepare tool paths")?,
    );
    let log_dir = runtime_log_directory(app)?;
    let log = rotate_runtime_log(&log_dir).map_err(|_| "Cannot create runtime log")?;
    let health_deadline = std::time::Instant::now() + Duration::from_secs(60);
    let child = command
        .env("ADAPTIVE_RSI_DESKTOP_OWNER_PID", std::process::id().to_string())
        .arg("desktop-runtime")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::from(log))
        .spawn()
        .map_err(|_| "Cannot start bundled runtime")?;
    let desktop_version = app.package_info().version.to_string();
    let (mut child, connection, service_pid, receiver) =
        handshake_local_runtime(PendingChild::new(child), &desktop_version, health_deadline)?;
    let state = app.state::<RuntimeState>();
    startup_budget(health_deadline)?;
    let generation = publish_local_runtime(&state, child.take(), connection, select);
    let handle = app.clone();
    std::thread::spawn(move || {
        while let Ok(Ok(line)) = receiver.recv() {
            let Ok(session) = serde_json::from_str::<Session>(&line) else {
                continue;
            };
            if session.rotated {
                let state = handle.state::<RuntimeState>();
                let _ = publish_runtime_session(&state, generation, service_pid, session);
            }
        }
    });
    Ok(())
}

type HandshakeLines = std::sync::mpsc::Receiver<io::Result<String>>;

fn handshake_local_runtime(
    mut child: PendingChild,
    desktop_version: &str,
    health_deadline: std::time::Instant,
) -> Result<(PendingChild, Connection, u32, HandshakeLines), String> {
    let stdout = child
        .get_mut()
        .stdout
        .take()
        .ok_or("Cannot read runtime handshake")?;
    // Keep the private channel open: the reused service publishes later
    // rotations on the same stream, not only the first handshake.
    let receiver = handshake_stream(stdout);
    let handshake = receiver
        .recv_timeout(health_deadline.saturating_duration_since(std::time::Instant::now()))
        .ok()
        .and_then(Result::ok);
    let session = handshake.and_then(|line| serde_json::from_str::<Session>(&line).ok());
    let Some(session) = session else {
        return Err("Runtime did not return a valid handshake; inspect runtime state".into());
    };
    let service_pid = session.pid.ok_or("Missing local runtime identity")?;
    if service_pid == 0 || session.rotated {
        return Err("Invalid local runtime identity".into());
    }
    let origin = session
        .local_origin
        .unwrap_or_else(|| session.origin.clone());
    let parsed = reqwest::Url::parse(&origin).map_err(|_| "Invalid runtime origin")?;
    if parsed.scheme() != "http" || parsed.host_str() != Some("127.0.0.1") {
        return Err("Invalid local runtime address".into());
    }
    let (client, transport) = transport::clients()?;
    let connection = Connection {
        client,
        transport,
        csrf_origin: session.origin,
        origin,
        cookie: Some(format!("{}={}", session.cookie, session.token)),
        csrf: session.csrf,
        owned: session.owned,
        name: if session.owned {
            "本机"
        } else {
            "本机 · 已有服务"
        }
        .into(),
    };
    loop {
        let remaining = startup_budget(health_deadline)?;
        match call_with_session(
            &connection,
            "health",
            None,
            Some(remaining.min(Duration::from_secs(2))),
        ) {
            Ok((health, _)) if health.status == 200 => {
                let health: Value = serde_json::from_str(&health.body).map_err(|_| "Invalid runtime health")?;
                if health["pid"].as_u64() != Some(u64::from(service_pid)) {
                    return Err("Local runtime identity changed".into());
                }
                let version = health["api_version"].as_u64().unwrap_or(0);
                if connection.owned {
                    if version != LOCAL_API_VERSION || health["version"].as_str() != Some(desktop_version) {
                        return Err("Bundled runtime version or API is incompatible".into());
                    }
                } else if version != LOCAL_API_VERSION {
                    return Err("Existing local runtime API is incompatible".into());
                }
                if connection.owned {
                    let (status, _) = call_with_session(&connection, "desktop-update",
                        Some(json!({"action":"status"})), Some(startup_budget(health_deadline)?))?;
                    if status.status != 200 { return Err("Cannot inspect update admission".into()); }
                    let status: Value = serde_json::from_str(&status.body).map_err(|_| "Invalid update admission")?;
                    if status["barrier"].is_object() {
                        let lease = &status["barrier"]["lease"];
                        if status["barrier"]["version"] == health["version"] {
                            let (complete, _) = call_with_session(&connection, "desktop-update",
                                Some(json!({"action":"complete","lease":lease})),
                                Some(startup_budget(health_deadline)?))?;
                            if complete.status != 200 { return Err("Cannot complete desktop update".into()); }
                        } else {
                            native_update::cancel_startup_barrier(&connection, lease, health_deadline)?;
                        }
                    }
                }
                break;
            },
            Ok((health, _)) if health.status == 401 || health.status == 403 => {
                return Err("Runtime authentication failed".into());
            }
            _ => {}
        }
        if child
            .get_mut()
            .try_wait()
            .map_err(|_| "Cannot inspect runtime")?
            .is_some()
        {
            return Err("Runtime exited before becoming healthy".into());
        }
        std::thread::sleep(
            Duration::from_millis(200)
                .min(health_deadline.saturating_duration_since(std::time::Instant::now())),
        );
    }
    Ok((child, connection, service_pid, receiver))
}
#[tauri::command]
fn backup_download_status(cancel: bool) -> Value {
    if cancel {
        let _ = BACKUP_ACTIVITY.compare_exchange(BACKUP_DOWNLOADING, BACKUP_CANCELLED, Ordering::SeqCst, Ordering::SeqCst);
    }
    json!({"active": BACKUP_ACTIVITY.load(Ordering::SeqCst) != BACKUP_IDLE,
           "bytes": BACKUP_BYTES.load(Ordering::Relaxed), "total": BACKUP_TOTAL.load(Ordering::Relaxed)})
}

async fn backup_cancelled() {
    while BACKUP_ACTIVITY.load(Ordering::SeqCst) != BACKUP_CANCELLED {
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
}

#[tauri::command]
async fn download_backup(app: tauri::AppHandle, id: String) -> Result<bool, String> {
    if id.len() != 16
        || !id.bytes().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
    {
        return Err("Invalid backup ID".into());
    }
    tauri::async_runtime::spawn_blocking(move || {
        if BACKUP_ACTIVITY.compare_exchange(BACKUP_IDLE, BACKUP_DOWNLOADING, Ordering::SeqCst, Ordering::SeqCst).is_err() {
            return Err("Backup download is already active".into());
        }
        struct ResetActivity;
        impl Drop for ResetActivity {
            fn drop(&mut self) { BACKUP_ACTIVITY.store(BACKUP_IDLE, Ordering::SeqCst); }
        }
        let _activity = ResetActivity;
        BACKUP_BYTES.store(0, Ordering::Relaxed);
        BACKUP_TOTAL.store(0, Ordering::Relaxed);

        let state = app.state::<RuntimeState>();
        invalidate_exited_local(&state);
        let transport = state.target.lock().unwrap().as_ref()
            .map(|connection| connection.transport.clone()).ok_or("Runtime is unavailable")?;
        let selected = app.dialog().file().set_file_name(format!("adaptive-rsi-{id}.sqlite3"))
            .blocking_save_file();
        let Some(selected) = selected else { return Ok(false); };
        let destination = selected.into_path().map_err(|_| "Invalid backup destination")?;
        if destination.exists() {
            return Err("Choose a new backup filename".into());
        }
        invalidate_exited_local(&state);
        let connection = state.target.lock().unwrap().as_ref()
            .filter(|connection| connection.transport.same_connection(&transport))
            .cloned().ok_or("Runtime changed before backup download")?;
        tauri::async_runtime::block_on(async {
            tokio::select! {
                biased;
                _ = backup_cancelled() => Err("Backup download cancelled".into()),
                result = transport::save_backup(&connection, &id, &destination) => result.map(|()| true),
            }
        })
    })
    .await
    .map_err(|_| "Backup download interrupted".to_string())?
}

#[tauri::command]
async fn request(
    app: tauri::AppHandle,
    path: String,
    body: Option<Value>,
    request_id: Option<String>,
    connection_generation: u64,
) -> Result<Reply, String> {
    static NEXT: AtomicU64 = AtomicU64::new(0);
    let id = request_id.unwrap_or_else(|| format!("legacy-{}", NEXT.fetch_add(1, Ordering::SeqCst)));
    transport::request_for_connection(&app.state::<RuntimeState>(), &path, body, id, connection_generation).await
}
#[tauri::command]
fn cancel_request(state: tauri::State<RuntimeState>, id: String) {
    state.transport.cancel(&id);
}
#[tauri::command]
fn runtime_info(state: tauri::State<RuntimeState>) -> Value {
    runtime_snapshot(&state)
}

fn runtime_snapshot(state: &RuntimeState) -> Value {
    invalidate_exited_local(state);
    // Always lock the target before its metadata; connect commits both under
    // the same order so a reader never sees mismatched server capabilities.
    let target = state.target.lock().unwrap();
    let connection_generation = state.connection_generation.load(Ordering::SeqCst);
    match target.as_ref() {
        Some(c) => {
            let meta = state.remote_meta.lock().unwrap();
            let kind = if meta.is_null() && c.origin.starts_with("http://127.0.0.1:") {
                "local"
            } else {
                "remote"
            };
            let scope = meta["scope"].as_str().unwrap_or(&c.origin);
            json!({"name":c.name,"scope":scope,"connected":true,"kind":kind,"owned":c.owned,
                   "connection_generation":connection_generation,
                   "api_version":meta.get("api_version").cloned().unwrap_or(Value::Null),
                   "capabilities":meta.get("capabilities").cloned().unwrap_or_else(|| json!([]))})
        }
        None => json!({"connected":false,"connection_generation":connection_generation,
                      "error":state.error.lock().unwrap().clone()}),
    }
}
#[tauri::command]
fn set_native_language(app: tauri::AppHandle, language: String) {
    let english = language.to_ascii_lowercase().starts_with("en");
    if let Some(tray) = app.tray_by_id("runtime-tray") {
        let _ = tray_menu(&app, english).and_then(|menu| tray.set_menu(Some(menu)));
    }
    let Some(menu) = app.menu() else { return };
    let Some(commands_item) = menu.get("commands") else { return };
    let Some(commands) = commands_item.as_submenu() else { return };
    for (id, zh, en) in [
        ("app-open", "打开主窗口", "Open Main Window"),
        ("app-check-update", "检查更新", "Check for Updates"),
        ("app-diagnostics", "导出诊断包", "Export Diagnostics"),
    ] {
        if let Some(item) = commands.get(id) {
            if let Some(item) = item.as_menuitem() {
                let _ = item.set_text(if english { en } else { zh });
            }
        }
    }
    let _ = commands.set_text(if english { "Commands" } else { "命令" });
    let Ok(items) = menu.items() else { return };
    if let Some(app_menu) = items.first().and_then(|item| item.as_submenu()) {
        if let Some(quit_item) = app_menu.get("app-quit") {
            if let Some(quit) = quit_item.as_menuitem() {
                let _ = quit.set_text(if english { "Quit Adaptive RSI" } else { "退出 Adaptive RSI" });
            }
        }
    }
}
static QUIT_PROGRESS: Mutex<&'static str> = Mutex::new("preparing");

#[tauri::command]
fn quit_progress() -> &'static str {
    *QUIT_PROGRESS.lock().unwrap()
}

#[tauri::command]
fn quit_gracefully(app: tauri::AppHandle) -> Result<(), String> {
    let replacement = native_update::REPLACEMENT_GATE.try_lock().map_err(|_| "update_busy")?;
    if QUITTING.swap(true, Ordering::SeqCst) {
        return Ok(());
    }
    let (child, owns_local_service) = {
        let state = app.state::<RuntimeState>();
        let mut child = state.child.lock().unwrap();
        let local = state.local.lock().unwrap();
        (child.take(), local.as_ref().is_some_and(|connection| connection.owned))
    };
    drop(replacement);
    let Some(mut child) = child else {
        app.exit(0);
        return Ok(());
    };
    // An attached service outlives its watcher. Only an owned child can report service shutdown.
    let progress_window = app.get_webview_window("main").filter(|_| owns_local_service);
    let show_progress = progress_window.is_some();
    if let Some(window) = progress_window {
        let _ = window.show();
        let _ = window.set_focus();
        let _ = window.eval(
            "window.dispatchEvent(new CustomEvent('rsi-app-shutdown'))",
        );
    }
    std::thread::spawn(move || {
        if show_progress {
            // Give the webview time to render before closing the child's control pipe.
            std::thread::sleep(Duration::from_millis(350));
        }
        drop(child.stdin.take());
        if owns_local_service {
            *QUIT_PROGRESS.lock().unwrap() = "stopping";
        }
        let stopped = wait_for_child(&mut child, Duration::from_secs(30)) || {
            let _ = child.kill();
            wait_for_child(&mut child, Duration::from_secs(2))
        };
        if owns_local_service {
            *QUIT_PROGRESS.lock().unwrap() = if stopped { "stopped" } else { "failed" };
        }
        app.exit(0);
    });
    Ok(())
}
const MAX_CONNECTION_URL_BYTES: usize = 8192;
const REMOTE_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(5);
const SSH_TUNNEL_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(12);
const SSH_TUNNEL_STDIN_TIMEOUT: Duration = Duration::from_secs(15);
const SSH_TUNNEL_STDIN_ARG: &str = "--connect-ssh-tunnel-stdin";

struct RemoteTarget {
    url: reqwest::Url,
    ssh_tunnel: bool,
}

impl RemoteTarget {
    fn parse(input: &str, ssh_tunnel: bool) -> Result<Self, String> {
        let url = if ssh_tunnel {
            parse_ssh_tunnel_url(input)?
        } else {
            parse_remote_url(input)?
        };
        Ok(Self { url, ssh_tunnel })
    }
}

fn parse_connection_target(url: Option<&str>, ssh_tunnel: bool) -> Result<Option<RemoteTarget>, String> {
    if ssh_tunnel && url.is_none() {
        return Err("SSH tunnel mode requires an authenticated URL".into());
    }
    url.map(|input| RemoteTarget::parse(input, ssh_tunnel)).transpose()
}

#[tauri::command]
fn validate_connection_target(url: Option<String>, ssh_tunnel: Option<bool>) -> bool {
    parse_connection_target(url.as_deref(), ssh_tunnel.unwrap_or(false)).is_ok()
}

#[tauri::command]
async fn connect(
    app: tauri::AppHandle,
    url: Option<String>,
    ssh_tunnel: Option<bool>,
) -> Result<Value, String> {
    let remote = parse_connection_target(url.as_deref(), ssh_tunnel.unwrap_or(false))?;
    drop(url);
    let state = app.state::<RuntimeState>();
    let _switch = state.transport.connection_change.lock().await;
    if remote.is_none() {
        invalidate_exited_local(&state);
        let needs_local = state.local.lock().unwrap().is_none();
        if needs_local {
            let handle = app.clone();
            tauri::async_runtime::spawn_blocking(move || {
                let state = handle.state::<RuntimeState>();
                let _start = state.local_start_gate.try_lock().map_err(|_| "Local runtime is unavailable during an update")?;
                if QUITTING.load(Ordering::SeqCst) {
                    return Err("Local runtime is unavailable".into());
                }
                if state.local.lock().unwrap().is_some() { return Ok(()); }
                if state.recovering.load(Ordering::SeqCst) && !reap_update_runtime(&state) {
                    return Err("Local runtime is unavailable".into());
                }
                bootstrap_local(&handle, false)
            })
                .await
                .map_err(|_| "Local runtime startup interrupted")??;
        }
    }
    switch_runtime_locked(&state, remote).await
}

fn parse_ssh_tunnel_url(input: &str) -> Result<reqwest::Url, String> {
    const INVALID: &str = "Enter the authenticated http://127.0.0.1:<port>/ root URL with its token";
    if input.len() > MAX_CONNECTION_URL_BYTES {
        return Err("Connection URL is too long".into());
    }
    if input
        .bytes()
        .any(|b| b.is_ascii_control() || b.is_ascii_whitespace() || b == b'\\')
    {
        return Err(INVALID.into());
    }
    // Check the raw authority before URL parsing can normalize IPv4 aliases or drop :80.
    let after_host = input.strip_prefix("http://127.0.0.1:").ok_or(INVALID)?;
    let port_end = after_host.find(['/', '?']).ok_or(INVALID)?;
    let (port, root_query) = after_host.split_at(port_end);
    if !port.bytes().all(|b| b.is_ascii_digit())
        || port.parse::<u16>().map_or(true, |port| port == 0)
        || !(root_query.starts_with("/?") || root_query.starts_with('?'))
    {
        return Err(INVALID.into());
    }
    let parsed = reqwest::Url::parse(input).map_err(|_| INVALID)?;
    let mut tokens = parsed.query_pairs().filter(|(name, _)| name == "token");
    if parsed.fragment().is_some()
        || !tokens.next().is_some_and(|(_, token)| !token.trim().is_empty())
        || tokens.next().is_some()
    {
        return Err(INVALID.into());
    }
    Ok(parsed)
}

fn parse_remote_url(input: &str) -> Result<reqwest::Url, String> {
    if input.len() > MAX_CONNECTION_URL_BYTES {
        return Err("Connection URL is too long".into());
    }
    let parsed =
        reqwest::Url::parse(input).map_err(|_| "Enter the authenticated HTTPS server URL")?;
    if parsed.scheme() != "https"
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.path() != "/"
        || parsed.fragment().is_some()
        || !parsed
            .query_pairs()
            .any(|(k, v)| k == "token" && !v.is_empty())
    {
        return Err("Enter the authenticated HTTPS server root URL with its token".into());
    }
    Ok(parsed)
}

fn remote_connection(target: RemoteTarget) -> Result<(Connection, Value), String> {
    let RemoteTarget { url, ssh_tunnel } = target;
    let timeout = if ssh_tunnel { SSH_TUNNEL_HANDSHAKE_TIMEOUT } else { REMOTE_HANDSHAKE_TIMEOUT };
    let deadline = std::time::Instant::now() + timeout;
    let origin = url.origin().ascii_serialization();
    let (client, transport) = transport::clients()?;
    let handshake_timeout = || -> Result<Duration, String> {
        let remaining = deadline.saturating_duration_since(std::time::Instant::now());
        if remaining.is_zero() {
            return Err(if ssh_tunnel { "SSH tunnel handshake timed out" } else { "Server connection failed" }.into());
        }
        Ok(remaining)
    };
    let response = client.get(url)
        .timeout(handshake_timeout()?)
        .send()
        .map_err(|_| "Server connection failed".to_string())?;
    let mut tunnel_cookie = None;
    if ssh_tunnel {
        // Keep the acknowledged cookie and send it with the matching CSRF token.
        let mut locations = response.headers().get_all(reqwest::header::LOCATION).iter();
        let mut cookies = response.cookies().filter(|cookie| {
            cookie.name().starts_with("rsi_") && !cookie.value().is_empty()
        });
        tunnel_cookie = cookies.next().map(|cookie| format!("{}={}", cookie.name(), cookie.value()));
        if response.status() != reqwest::StatusCode::SEE_OTHER
            || locations.next().map(|value| value.as_bytes()) != Some(b"/")
            || locations.next().is_some()
            || tunnel_cookie.is_none()
            || cookies.next().is_some()
        {
            return Err("SSH tunnel authentication was rejected".into());
        }
    } else if ![200, 302, 303].contains(&response.status().as_u16()) {
        return Err("Server did not accept this connection".into());
    }
    let mut connection = Connection {
        client,
        transport,
        csrf_origin: origin.clone(),
        origin: origin.clone(),
        cookie: tunnel_cookie,
        csrf: String::new(),
        name: origin,
        owned: false,
    };
    let (health, _) = call_with_session(&connection, "health", None, Some(handshake_timeout()?))?;
    if health.status != 200 {
        return Err(format!("Runtime health check failed (HTTP {})", health.status));
    }
    let value: Value =
        serde_json::from_str(&health.body).map_err(|_| "Invalid runtime handshake")?;
    let version = value["api_version"].as_u64().ok_or("Invalid runtime handshake")?;
    if !supported_remote_api(version) {
        return Err("Update the remote runtime before connecting".into());
    }
    let mut meta = json!({
        "api_version": version,
        "capabilities": value.get("capabilities").cloned().unwrap_or_else(|| json!([])),
    });
    let (response, _) =
        call_with_session(&connection, "state?sections=core", None, Some(handshake_timeout()?))?;
    if response.status != 200 {
        return Err(format!("Runtime state check failed (HTTP {})", response.status));
    }
    let value: Value =
        serde_json::from_str(&response.body).map_err(|_| "Invalid runtime state")?;
    connection.csrf = value["csrf"]
        .as_str()
        .filter(|csrf| {
            !csrf.trim().is_empty() && reqwest::header::HeaderValue::from_str(csrf).is_ok()
        })
        .ok_or("Missing runtime authentication")?
        .into();
    if ssh_tunnel {
        let instance = value["instance"].as_str().filter(|instance| {
            instance.len() == 16 && instance.bytes().all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        });
        meta["scope"] = json!(match instance {
            Some(instance) => format!("ssh:instance:{instance}"),
            None => format!("ssh:{}", connection.origin),
        });
    }
    Ok((connection, meta))
}

async fn switch_runtime(state: &RuntimeState, remote: Option<RemoteTarget>) -> Result<Value, String> {
    let _switch = state.transport.connection_change.lock().await;
    switch_runtime_locked(state, remote).await
}

async fn switch_runtime_locked(state: &RuntimeState, remote: Option<RemoteTarget>) -> Result<Value, String> {
    // Keep the current target while the candidate authenticates with its own cookie jar.
    let (candidate, checked_local) = if let Some(url) = remote {
        let candidate = tauri::async_runtime::spawn_blocking(move || remote_connection(url))
            .await
            .map_err(|_| "Connection interrupted")??;
        (Some(candidate), None)
    } else {
        invalidate_exited_local(state);
        let local = state.local.lock().unwrap().clone().ok_or("Local runtime is unavailable")?;
        let checked_local = local.transport.clone();
        let healthy = tauri::async_runtime::spawn_blocking(move || {
            let Ok((reply, _)) = call_with_session(&local, "health", None, Some(Duration::from_secs(2))) else {
                return false;
            };
            reply.status == 200 && serde_json::from_str::<Value>(&reply.body).ok()
                .and_then(|health| health["api_version"].as_u64())
                == Some(LOCAL_API_VERSION)
        }).await.map_err(|_| "Local runtime check interrupted")?;
        if !healthy {
            return Err("Local runtime is unavailable".into());
        }
        (None, Some(checked_local))
    };

    let connection_change = state.transport.gate.write().await;
    if checked_local.is_some() {
        invalidate_exited_local(state);
    }
    let previous = {
        let mut target = state.target.lock().unwrap();
        let replacement = match candidate {
            Some(remote) => Some(remote),
            None => {
                let local = state.local.lock().unwrap();
                let local = local.as_ref().filter(|local| {
                    checked_local.as_ref().is_some_and(|checked| local.transport.same_connection(checked))
                }).ok_or("Local runtime is unavailable")?;
                if target.as_ref().is_some_and(|selected| selected.transport.same_connection(&local.transport))
                    && state.remote_meta.lock().unwrap().is_null()
                {
                    None
                } else {
                    Some((local.clone(), Value::Null))
                }
            }
        };
        replacement.and_then(|(connection, meta)| {
            // Only cancel once every fallible step has succeeded. Publish the generation
            // under the same lock as the request's target selection.
            state.transport.cancel_all();
            *state.remote_meta.lock().unwrap() = meta;
            state.connection_generation.fetch_add(1, Ordering::SeqCst);
            target.replace(connection)
        })
    };
    let snapshot = runtime_snapshot(state);
    drop(connection_change);
    // The last reference to a blocking client can join its runtime thread.
    tauri::async_runtime::spawn_blocking(move || drop(previous));
    Ok(snapshot)
}
#[tauri::command]
async fn check_desktop_update(app: tauri::AppHandle, force: Option<bool>) -> Option<Value> {
    let force = force.unwrap_or(false);
    tauri::async_runtime::spawn_blocking(move || check_desktop_update_blocking(&app, force))
        .await
        .unwrap_or_else(|_| Some(json!({"check_error": true, "error": "check_failed"})))
}
#[tauri::command]
async fn download_desktop_update(app: tauri::AppHandle) -> Result<(), String> {
    let handle = app.clone();
    tauri::async_runtime::spawn_blocking(move || start_desktop_download(handle))
        .await
        .map_err(|_| "Request interrupted".to_string())?
}
#[tauri::command]
async fn native_update(app: tauri::AppHandle, action: String, installation_window: Option<String>,
                       policy: Option<String>, version_scope: Option<String>,
                       revision: Option<u64>, automatic: Option<bool>) -> Result<Value, String> {
    match action.as_str() {
        "status" => Ok(native_update::snapshot(&app)),
        "check" => native_update::check(app, false).await,
        "download" => native_update::check(app, true).await,
        "cancel" => { native_update::cancel(&app); Ok(native_update::snapshot(&app)) },
        // Settings are versioned so an approved install cannot replace under a
        // policy, scope or window the user has since changed.
        "authorize" => native_update::authorize(
            &app,
            revision.ok_or("update_authorization_invalid")?,
            policy.as_deref().unwrap_or(""),
            version_scope.as_deref().unwrap_or(""),
            installation_window.as_deref().unwrap_or(""),
        ),
        // Exported receipts come from the persisted transaction, never from the panel.
        "receipt" => Ok(update_receipt::export(&app)),
        "install" => {
            // `automatic` is the panel's own background install; a manual click
            // arrives with the explicit "any" window. The revision is required
            // either way, so a call that never saw an authorization is refused.
            native_update::install(
                app,
                installation_window.unwrap_or_else(|| "any".into()),
                native_update::required_revision(revision)?,
                automatic.unwrap_or(false),
            ).await?;
            Ok(json!({"state":"restarting"}))
        },
        _ => Err("invalid_update_action".into()),
    }
}
#[tauri::command]
async fn choose_folder(app: tauri::AppHandle) -> Result<Option<String>, String> {
    let (send, receive) = tokio::sync::oneshot::channel();
    app.dialog().file().pick_folder(move |selection| {
        let _ = send.send(selection);
    });
    receive.await.map_err(|_| "Folder selection interrupted")?
        .map(|p| {
            p.into_path()
                .map(|p| p.to_string_lossy().into_owned())
                .map_err(|_| "Invalid folder".into())
        })
        .transpose()
}
#[tauri::command]
fn open_runtime_log_directory(app: tauri::AppHandle) -> Result<(), String> {
    let directory = runtime_log_directory(&app)?;
    app.opener()
        .open_path(directory.to_string_lossy().into_owned(), None::<&str>)
        .map_err(|_| "Cannot open runtime log directory".into())
}
#[tauri::command]
fn open_url(app: tauri::AppHandle, url: String) -> Result<(), String> {
    let parsed = reqwest::Url::parse(&url).map_err(|_| "Invalid URL")?;
    if parsed.scheme() != "https"
        && !(parsed.scheme() == "http" && parsed.host_str() == Some("127.0.0.1"))
    {
        return Err("Only HTTPS links can be opened".into());
    }
    app.opener()
        .open_url(url, None::<&str>)
        .map_err(|_| "Cannot open link".into())
}
#[tauri::command]
fn notify(
    app: tauri::AppHandle,
    title: String,
    body: String,
    task: Option<String>,
    project: Option<String>,
) -> Result<(), String> {
    let scope = runtime_snapshot(&app.state::<RuntimeState>())["scope"].clone();
    app.notification()
        .builder()
        .title(title.chars().take(100).collect::<String>())
        .body(body.chars().take(500).collect::<String>())
        .extra("task", task)
        .extra("project", project)
        .extra("scope", scope)
        .show()
        .map_err(|_| "Cannot show notification".into())
}

fn read_ssh_tunnel_target(reader: impl BufRead) -> Result<RemoteTarget, String> {
    let mut line = Vec::new();
    reader
        .take((MAX_CONNECTION_URL_BYTES + 2) as u64)
        .read_until(b'\n', &mut line)
        .map_err(|_| "Cannot read SSH tunnel URL")?;
    if line.last() == Some(&b'\n') {
        line.pop();
        if line.last() == Some(&b'\r') {
            line.pop();
        }
    }
    let input = std::str::from_utf8(&line).map_err(|_| "Invalid SSH tunnel URL encoding")?;
    RemoteTarget::parse(input, true)
}

fn ssh_tunnel_stdin_target() -> Result<RemoteTarget, String> {
    let stdin = std::io::stdin();
    if stdin.is_terminal() {
        return Err("Provide the SSH tunnel URL through a pipe".into());
    }
    let (send, receive) = std::sync::mpsc::sync_channel(1);
    std::thread::Builder::new()
        .spawn(move || {
            let _ = send.send(read_ssh_tunnel_target(stdin.lock()));
        })
        .map_err(|_| "Cannot read SSH tunnel URL")?;
    // If input stalls, startup exits and the OS closes the blocked pipe read.
    receive
        .recv_timeout(SSH_TUNNEL_STDIN_TIMEOUT)
        .map_err(|_| "SSH tunnel input timed out")?
}

const SSH_TUNNEL_STARTUP_FAILURE: &[u8] = b"SSH tunnel connection failed\n";
static SSH_TUNNEL_STARTUP_PENDING: AtomicBool = AtomicBool::new(false);

extern "C" fn report_unfinished_ssh_tunnel_startup() {
    if SSH_TUNNEL_STARTUP_PENDING.swap(false, Ordering::Relaxed) {
        let _ = std::io::stderr().write_all(SSH_TUNNEL_STARTUP_FAILURE);
    }
}

fn tray_menu(app: &tauri::AppHandle, english: bool) -> tauri::Result<Menu<tauri::Wry>> {
    let open = if english { "Open Adaptive RSI" } else { "打开 Adaptive RSI" };
    let quit = if english { "Quit Adaptive RSI" } else { "退出 Adaptive RSI" };
    let open = MenuItem::with_id(app, "open", open, true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", quit, true, None::<&str>)?;
    Menu::with_items(app, &[&open, &quit])
}

fn main() {
    let ssh_tunnel_startup = std::env::args_os()
        .skip(1)
        .any(|arg| arg == SSH_TUNNEL_STDIN_ARG);
    if ssh_tunnel_startup {
        // The single-instance plugin can call process::exit before setup runs.
        // Register a fixed failure report for that path as well as setup errors.
        // SAFETY: The C callback has static lifetime and does not unwind.
        if unsafe { libc::atexit(report_unfinished_ssh_tunnel_startup) } != 0 {
            let _ = std::io::stderr().write_all(SSH_TUNNEL_STARTUP_FAILURE);
            std::process::exit(1);
        }
        SSH_TUNNEL_STARTUP_PENDING.store(true, Ordering::Relaxed);
        if std::env::args_os().len() != 2 {
            std::process::exit(1);
        }
    }
    let builder = tauri::Builder::default()
        .manage(RuntimeState::default())
        .manage(native_update::NativeUpdater::default())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_single_instance::init(|app, args, _| {
            if args.iter().skip(1).any(|arg| arg == SSH_TUNNEL_STDIN_ARG) {
                return;
            }
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.set_focus();
            }
        }));
    let builder = if ssh_tunnel_startup {
        // Plugin setup runs after single-instance detection and before windows
        // are created, so the frontend's first runtime_info sees this target.
        builder.plugin(
            tauri::plugin::Builder::<tauri::Wry>::new("ssh-tunnel-startup")
                .setup(|app, _| {
                    let target = ssh_tunnel_stdin_target()?;
                    let state = app.state::<RuntimeState>();
                    tauri::async_runtime::block_on(switch_runtime(&state, Some(target)))?;
                    Ok(())
                })
                .build(),
        )
    } else {
        builder
    };
    let app = builder
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            request,
            cancel_request,
            runtime_info,
            validate_connection_target,
            connect,
            choose_folder,
            open_runtime_log_directory,
            open_url,
            notify,
            check_desktop_update,
            download_desktop_update
            ,native_update
            ,download_backup
            ,backup_download_status
            ,quit_gracefully
            ,quit_progress
            ,set_native_language
        ])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .setup(move |app| {
            let handle = app.handle().clone();
            app.notification().on_action(move |action| {
                if action.action_id() != "tap" { return; }
                if let Some(notification) = action.notification() {
                    let extra = notification.extra();
                    let snapshot = runtime_snapshot(&handle.state::<RuntimeState>());
                    if snapshot["scope"].is_null() || extra.get("scope") != snapshot.get("scope") { return; }
                    if let (Some(task), Some(project)) = (extra.get("task").and_then(Value::as_str), extra.get("project").and_then(Value::as_str)) {
                        if let Some(window) = handle.get_webview_window("main") {
                            let payload = json!({"task": task, "project": project});
                            let _ = window.show();
                            let _ = window.unminimize();
                            let _ = window.set_focus();
                            let _ = window.eval(format!("window.dispatchEvent(new CustomEvent('rsi-notification-open',{{detail:{payload}}}))"));
                        }
                    }
                }
            })?;
            let app_open = MenuItem::with_id(app, "app-open", "打开主窗口", true, Some("CmdOrCtrl+Shift+O"))?;
            let app_update = MenuItem::with_id(app, "app-check-update", "检查更新", true, Some("CmdOrCtrl+Shift+U"))?;
            let app_diagnostics = MenuItem::with_id(app, "app-diagnostics", "导出诊断包", true, Some("CmdOrCtrl+Shift+D"))?;
            let commands = Submenu::with_id_and_items(app, "commands", "命令", true,
                &[&app_open, &app_update, &app_diagnostics])?;
            // Keep the platform default menus (Edit/Window/Help and text-editing
            // shortcuts). Only macOS has the app submenu whose trailing Quit we
            // may replace; other platforms must never lose Edit > Select All.
            let menu = Menu::default(app.handle())?;
            #[cfg(target_os = "macos")]
            {
                let app_quit = MenuItem::with_id(app, "app-quit", "退出 Adaptive RSI", true, Some("CmdOrCtrl+Q"))?;
                if let Some(app_submenu) = menu.items()?.first().and_then(|item| item.as_submenu()) {
                    let count = app_submenu.items()?.len();
                    if count > 0 { let _ = app_submenu.remove_at(count - 1); }
                    app_submenu.append(&app_quit)?;
                }
            }
            menu.insert(&commands, 1)?;
            app.set_menu(menu)?;
            app.on_menu_event(|app, event| match event.id().as_ref() {
                "app-open" => {
                    if let Some(w) = app.get_webview_window("main") {
                        let _ = w.show();
                        let _ = w.set_focus();
                    }
                }
                "app-check-update" => {
                    if let Some(w) = app.get_webview_window("main") {
                        let _ = w.show();
                        let _ = w.set_focus();
                        let _ = w.eval("window.dispatchEvent(new CustomEvent('rsi-native-command',{detail:{command:'check-update'}}))");
                    }
                }
                "app-diagnostics" => {
                    if let Some(w) = app.get_webview_window("main") {
                        let _ = w.show();
                        let _ = w.set_focus();
                        let _ = w.eval("window.dispatchEvent(new CustomEvent('rsi-native-command',{detail:{command:'export-diagnostics'}}))");
                    }
                }
                "app-quit" => { let _ = quit_gracefully(app.clone()); }
                _ => {}
            });
            let menu = tray_menu(app.handle(), false)?;
            let mut tray =
                TrayIconBuilder::with_id("runtime-tray")
                    .menu(&menu)
                    .on_menu_event(|app, event| match event.id().as_ref() {
                        "open" => {
                            if let Some(w) = app.get_webview_window("main") {
                                let _ = w.show();
                                let _ = w.set_focus();
                            }
                        }
                        "quit" => { let _ = quit_gracefully(app.clone()); }
                        _ => {}
                    });
            if let Some(icon) = app.default_window_icon() {
                tray = tray.icon(icon.clone());
            }
            tray.build(app)?;
            if !ssh_tunnel_startup {
                use update_receipt::StartupHealth;

                let health = if cfg!(target_os = "windows") {
                    // Windows is remote-only and has no bundled runtime to probe.
                    *app.state::<RuntimeState>().error.lock().unwrap() = WINDOWS_REMOTE_ONLY.into();
                    StartupHealth::Unavailable
                } else {
                    match bootstrap(app.handle()) {
                        Ok(()) => StartupHealth::Healthy,
                        Err(error) => {
                            *app.state::<RuntimeState>().error.lock().unwrap() = error;
                            StartupHealth::Failed
                        }
                    }
                };
                let _ = update_receipt::reconcile_app(app.handle(), &app.package_info().version.to_string(), health);
            }
            Ok(())
        })
        .build(tauri::generate_context!());
    let app = match app {
        Ok(app) => app,
        Err(_) if ssh_tunnel_startup => std::process::exit(1),
        Err(error) => panic!("Cannot initialize desktop app: {error:?}"),
    };
    app.run(move |app, event| {
        if ssh_tunnel_startup && matches!(event, tauri::RunEvent::Ready) {
            let mut stdout = std::io::stdout().lock();
            if stdout
                .write_all(b"SSH tunnel connected\n")
                .and_then(|_| stdout.flush())
                .is_ok()
            {
                SSH_TUNNEL_STARTUP_PENDING.store(false, Ordering::Relaxed);
            }
        }
        #[cfg(target_os = "macos")]
        if let tauri::RunEvent::Reopen { .. } = event {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.set_focus();
            }
        }
        if let tauri::RunEvent::ExitRequested { code, api, .. } = event {
            if code == Some(tauri::RESTART_EXIT_CODE) {
                return;
            }
            let Ok(_replacement) = native_update::REPLACEMENT_GATE.try_lock() else {
                api.prevent_exit();
                return;
            };
            QUITTING.store(true, Ordering::SeqCst);
            if let Some(mut child) = app.state::<RuntimeState>().child.lock().unwrap().take() {
                // 正常关闭最多十秒，强杀后回收也有独立上限。
                stop_child(&mut child, Duration::from_secs(10));
            }
        }
    });
}

#[cfg(test)]
mod regression_tests;

#[cfg(test)]
mod tests {
    use super::*;
    use std::{io::BufReader, net::TcpListener, sync::Arc};

    static BACKUP_TEST_GATE: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

    struct BackupTestActivity;

    impl BackupTestActivity {
        fn begin() -> Self {
            BACKUP_ACTIVITY.store(BACKUP_DOWNLOADING, Ordering::SeqCst);
            BACKUP_BYTES.store(0, Ordering::Relaxed);
            BACKUP_TOTAL.store(0, Ordering::Relaxed);
            Self
        }
    }

    impl Drop for BackupTestActivity {
        fn drop(&mut self) {
            BACKUP_ACTIVITY.store(BACKUP_IDLE, Ordering::SeqCst);
        }
    }

    #[test]
    fn startup_log_keeps_only_current_and_previous_runs() {
        let temp = tempfile::tempdir().unwrap();
        let directory = prepare_runtime_log_directory(&temp.path().join("runtime-logs")).unwrap();
        let runs: [&[u8]; 3] = [b"first", b"second", b"third"];
        for run in runs {
            rotate_runtime_log(&directory).unwrap().write_all(run).unwrap();
        }
        assert_eq!(fs::read(directory.join("runtime.log")).unwrap(), b"third");
        assert_eq!(fs::read(directory.join("runtime.log.1")).unwrap(), b"second");
        assert_eq!(fs::read_dir(&directory).unwrap().count(), 2);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(fs::metadata(&directory).unwrap().permissions().mode() & 0o777, 0o700);
            for name in ["runtime.log", "runtime.log.1"] {
                assert_eq!(fs::metadata(directory.join(name)).unwrap().permissions().mode() & 0o777, 0o600);
            }
        }
    }

    #[test]
    fn failed_rotation_preserves_the_existing_log() {
        let temp = tempfile::tempdir().unwrap();
        let directory = temp.path();
        fs::write(directory.join("runtime.log"), b"last startup").unwrap();
        fs::create_dir(directory.join("runtime.log.1")).unwrap();
        assert!(rotate_runtime_log(directory).is_err());
        assert_eq!(fs::read(directory.join("runtime.log")).unwrap(), b"last startup");
        assert_eq!(fs::read_dir(directory).unwrap().count(), 2);
    }

    #[cfg(unix)]
    #[test]
    fn startup_logs_refuse_redirected_directory_and_current_log() {
        use std::os::unix::fs::symlink;
        let temp = tempfile::tempdir().unwrap();
        let target = temp.path().join("target");
        fs::create_dir(&target).unwrap();
        let redirect = temp.path().join("redirect");
        symlink(&target, &redirect).unwrap();
        assert!(prepare_runtime_log_directory(&redirect).is_err());
        assert_eq!(fs::read_dir(&target).unwrap().count(), 0);

        let directory = prepare_runtime_log_directory(&temp.path().join("runtime-logs")).unwrap();
        let unrelated = target.join("unrelated");
        fs::write(&unrelated, b"unrelated data").unwrap();
        fs::write(directory.join("runtime.log.1"), b"previous startup").unwrap();
        symlink(&unrelated, directory.join("runtime.log")).unwrap();
        assert!(rotate_runtime_log(&directory).is_err());
        assert_eq!(fs::read(unrelated).unwrap(), b"unrelated data");
        assert_eq!(fs::read(directory.join("runtime.log.1")).unwrap(), b"previous startup");
        assert_eq!(fs::read_dir(directory).unwrap().count(), 2);
    }

    fn receive_request(listener: &TcpListener, path: &str) -> (std::net::TcpStream, String) {
        let (stream, _) = listener.accept().unwrap();
        stream.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
        let mut reader = BufReader::new(&stream);
        let mut first = String::new();
        reader.read_line(&mut first).unwrap();
        assert_eq!(first.split_whitespace().nth(1), Some(path));
        let mut headers = String::new();
        loop {
            let mut line = String::new();
            assert_ne!(reader.read_line(&mut line).unwrap(), 0);
            if line == "\r\n" {
                break;
            }
            headers.push_str(&line);
        }
        (stream, headers)
    }

    fn answer_request(mut stream: std::net::TcpStream, status: u16, headers: &str, body: &str) {
        write!(stream, "HTTP/1.1 {status} Fixture\r\n{headers}Content-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).unwrap();
    }

    #[test]
    fn remote_url_requires_an_explicit_authenticated_https_root() {
        for input in ["", " \t\n", "https://example.test/", "https://example.test/?token=",
                      "http://example.test/?token=test", "https://example.test/path?token=test"] {
            assert!(parse_remote_url(input).is_err(), "{input:?}");
        }
        assert!(parse_remote_url("https://example.test/?token=test").is_ok());
    }

    #[test]
    fn ssh_tunnel_url_requires_literal_loopback_and_a_root_token() {
        for input in [
            "http://203.0.113.2:6090/?token=fixture",
            "http://10.0.0.2:6090/?token=fixture",
            "http://192.168.0.2:6090/?token=fixture",
            "http://localhost:6090/?token=fixture",
            "http://127.1:6090/?token=fixture",
            "http://127.0.0.2:6090/?token=fixture",
            "http://2130706433:6090/?token=fixture",
            "http://0177.0.0.1:6090/?token=fixture",
            "http://[::1]:6090/?token=fixture",
            "http://%31%32%37.0.0.1:6090/?token=fixture",
            "http://127.0.0.1/?token=fixture",
            "http://127.0.0.1:0/?token=fixture",
            "http://127.0.0.1:65536/?token=fixture",
            "http://127.0.0.1:6090@elsewhere.test/?token=fixture",
            "http://user@127.0.0.1:6090/?token=fixture",
            "http://127.0.0.1:6090/other/..?token=fixture",
            "http://127.0.0.1:6090/?token=fixture#fragment",
            "http://127.0.0.1:6090/?token=%20",
            "http://127.0.0.1:6090/?token=fixture&token=duplicate",
            "http://127.0.0.1:6090/",
            "https://127.0.0.1:6090/?token=fixture",
        ] {
            assert!(RemoteTarget::parse(input, true).is_err(), "{input}");
        }
        for port in [80, 6090, 65535] {
            let input = format!("http://127.0.0.1:{port}/?token=fixture");
            assert!(RemoteTarget::parse(&input, true).is_ok());
            assert!(RemoteTarget::parse(&input, false).is_err());
        }
    }

    #[test]
    fn ssh_tunnel_stdin_reads_one_bounded_line_and_redacts_errors() {
        let input = "http://127.0.0.1:6090/?token=fixture";
        let lines = format!("{input}\r\nnot another input");
        let mut bytes = lines.as_bytes();
        assert_eq!(read_ssh_tunnel_target(&mut bytes).unwrap().url.as_str(), input);
        assert_eq!(bytes, b"not another input");
        assert!(read_ssh_tunnel_target(&b"\n"[..]).is_err());
        assert!(read_ssh_tunnel_target(&b"\xff\n"[..]).is_err());

        let address = format!("{input}{}", "a".repeat(MAX_CONNECTION_URL_BYTES - input.len()));
        assert!(read_ssh_tunnel_target(format!("{address}\r\n").as_bytes()).is_ok());
        let oversize = format!("{address}a\r\n");
        let mut bytes = oversize.as_bytes();
        assert_eq!(read_ssh_tunnel_target(&mut bytes).err().unwrap(), "Connection URL is too long");
        assert_eq!(bytes, b"\n");
        let invalid = b"http://localhost:6090/?token=private-fixture\n";
        let error = read_ssh_tunnel_target(&invalid[..]).err().unwrap();
        assert!(!error.contains("private-fixture"));
    }

    #[tokio::test]
    async fn ssh_tunnel_switch_verifies_session_and_uses_the_loopback_origin() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let authority = listener.local_addr().unwrap().to_string();
        let origin = format!("http://{authority}");
        let input = format!("{origin}/?token=fixture\n");
        let remote = read_ssh_tunnel_target(input.as_bytes()).unwrap();
        let expected_origin = origin.clone();
        let server = std::thread::spawn(move || {
            let (stream, headers) = receive_request(&listener, "/?token=fixture");
            assert!(headers.contains(&format!("host: {authority}\r\n")));
            answer_request(stream, 303, "Location: /\r\nSet-Cookie: rsi_fixture=session; Path=/\r\n", "");
            for (path, body) in [
                ("/api/health", r#"{"api_version":2,"capabilities":[]}"#),
                ("/api/state?sections=core", r#"{"csrf":"tunnel-csrf","instance":"111122223333aaaa"}"#),
                ("/api/config", "{}"),
            ] {
                let (stream, headers) = receive_request(&listener, path);
                assert!(headers.contains(&format!("host: {authority}\r\n")));
                assert!(headers.contains("cookie: rsi_fixture=session\r\n"));
                if path == "/api/config" {
                    assert!(headers.contains(&format!("origin: {expected_origin}\r\n")));
                    assert!(headers.contains("x-rsi-csrf: tunnel-csrf\r\n"));
                }
                answer_request(stream, 200, "", body);
            }
        });
        let state = RuntimeState::default();
        let snapshot = switch_runtime(&state, Some(remote)).await.unwrap();
        assert_eq!(runtime_snapshot(&state), snapshot);
        assert_eq!(snapshot["scope"], "ssh:instance:111122223333aaaa");
        assert_eq!(snapshot["kind"], "remote");
        assert_eq!(snapshot["owned"], false);
        assert_eq!(snapshot["api_version"], 2);
        assert_eq!(transport::request(&state, "config", Some(json!({})), "tunnel".into()).await.unwrap().status, 200);
        server.join().unwrap();
        // Drop the blocking client outside the async test runtime.
        tauri::async_runtime::spawn_blocking(move || drop(state)).await.unwrap();
    }

    #[tokio::test]
    async fn ssh_tunnel_rejects_redirects_and_invalid_credentials_before_switching() {
        let redirect_target = TcpListener::bind("127.0.0.1:0").unwrap();
        redirect_target.set_nonblocking(true).unwrap();
        let cross_origin = format!(
            "Location: http://{}/?token=fixture\r\nSet-Cookie: rsi_fixture=session; Path=/\r\n",
            redirect_target.local_addr().unwrap()
        );
        for (status, headers) in [
            (303, cross_origin.as_str()),
            (303, "Location: /elsewhere\r\nSet-Cookie: rsi_fixture=session; Path=/\r\n"),
            (303, "Location: /\r\n"),
            (401, ""),
        ] {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let remote = RemoteTarget::parse(
                &format!("http://{}/?token=fixture", listener.local_addr().unwrap()), true
            ).unwrap();
            let headers = headers.to_owned();
            let server = std::thread::spawn(move || {
                let (stream, _) = receive_request(&listener, "/?token=fixture");
                answer_request(stream, status, &headers, "token=fixture");
                listener
            });
            let state = RuntimeState::default();
            let before = runtime_snapshot(&state);
            let error = switch_runtime(&state, Some(remote)).await.unwrap_err();
            assert_eq!(error, "SSH tunnel authentication was rejected");
            assert_eq!(runtime_snapshot(&state), before);
            let listener = server.join().unwrap();
            listener.set_nonblocking(true).unwrap();
            assert_eq!(listener.accept().unwrap_err().kind(), std::io::ErrorKind::WouldBlock);
            assert_eq!(redirect_target.accept().unwrap_err().kind(), std::io::ErrorKind::WouldBlock);
        }
    }

    #[test]
    fn ordinary_remote_handshake_shares_one_deadline_across_root_health_and_state() {
        let steps = [
            ("/?token=fixture", ""),
            ("/api/health", r#"{"api_version":1}"#),
            ("/api/state?sections=core", r#"{"csrf":"fixture"}"#),
        ];
        for (stall_at, expected_error) in ["Server connection failed", "Cannot read response", "Cannot read response"].into_iter().enumerate() {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            // Loopback HTTP exercises the ordinary remote handshake without a TLS fixture.
            let target = RemoteTarget {
                url: reqwest::Url::parse(&format!("http://{}/?token=fixture", listener.local_addr().unwrap())).unwrap(),
                ssh_tunnel: false,
            };
            let worker = std::thread::spawn(move || {
                for &(path, body) in &steps[..stall_at] {
                    let (stream, _) = receive_request(&listener, path);
                    std::thread::sleep(Duration::from_millis(1500));
                    answer_request(stream, 200, "", body);
                }
                let (mut stream, _) = receive_request(&listener, steps[stall_at].0);
                if stall_at != 0 {
                    stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n{").unwrap();
                }
                stream.set_read_timeout(Some(Duration::from_secs(8))).unwrap();
                assert_eq!(stream.read(&mut [0]).unwrap(), 0);
            });
            let started = std::time::Instant::now();
            let error = remote_connection(target).err().unwrap();
            assert_eq!(error, expected_error);
            assert!((Duration::from_secs(5)..Duration::from_secs(6)).contains(&started.elapsed()));
            worker.join().unwrap();
        }
    }

    #[tokio::test]
    async fn remote_switch_validates_handshake_before_cancelling_or_replacing_the_target() {
        const HEALTH: &str = r#"{"api_version":2,"capabilities":["fixture"]}"#;
        const STATE: &str = r#"{"csrf":"new-csrf"}"#;
        for (health, core, expected_error) in [
            ((401, HEALTH), None, Some("Runtime health check failed (HTTP 401)")),
            ((503, "<html>Unavailable</html>"), None, Some("Runtime health check failed (HTTP 503)")),
            ((200, r#"{"api_version":3}"#), None, Some("Update the remote runtime before connecting")),
            ((200, HEALTH), Some((503, STATE)), Some("Runtime state check failed (HTTP 503)")),
            ((200, HEALTH), Some((200, r#"{"csrf":""}"#)), Some("Missing runtime authentication")),
            ((200, HEALTH), Some((200, r#"{"csrf":" "}"#)), Some("Missing runtime authentication")),
            ((200, HEALTH), Some((200, r#"{"csrf":"bad\nvalue"}"#)), Some("Missing runtime authentication")),
            ((200, HEALTH), Some((200, STATE)), None),
            ((200, r#"{"api_version":1,"capabilities":["fixture"]}"#), Some((200, STATE)), None),
        ] {
            let old = TcpListener::bind("127.0.0.1:0").unwrap();
            let old_origin = format!("http://{}", old.local_addr().unwrap());
            let candidate = TcpListener::bind("127.0.0.1:0").unwrap();
            let candidate_origin = format!("http://{}", candidate.local_addr().unwrap());
            let remote = RemoteTarget {
                url: reqwest::Url::parse(&format!("{candidate_origin}/?token=fixture")).unwrap(),
                ssh_tunnel: false,
            };
            let (entered, handshake_paused) = tokio::sync::oneshot::channel();
            let (release, wait_for_release) = std::sync::mpsc::channel();
            let candidate_worker = std::thread::spawn(move || {
                let (stream, _) = receive_request(&candidate, "/?token=fixture");
                entered.send(()).unwrap();
                wait_for_release.recv_timeout(Duration::from_secs(3)).unwrap();
                answer_request(stream, 303, "Set-Cookie: rsi_fixture=remote; Path=/\r\nLocation: /\r\n", "");
                let (stream, headers) = receive_request(&candidate, "/api/health");
                assert!(headers.contains("cookie: rsi_fixture=remote\r\n"));
                answer_request(stream, health.0, "", health.1);
                if let Some((status, body)) = core {
                    let (stream, headers) = receive_request(&candidate, "/api/state?sections=core");
                    assert!(headers.contains("cookie: rsi_fixture=remote\r\n"));
                    answer_request(stream, status, "", body);
                }
                if expected_error.is_none() {
                    let (stream, headers) = receive_request(&candidate, "/api/config");
                    assert!(headers.contains("cookie: rsi_fixture=remote\r\n"));
                    assert!(headers.contains("x-rsi-csrf: new-csrf\r\n"));
                    answer_request(stream, 200, "", "{}");
                }
            });
            let (old_entered, old_request_received) = tokio::sync::oneshot::channel();
            let old_worker = std::thread::spawn(move || {
                let (mut slow, _) = receive_request(&old, "/api/slow");
                old_entered.send(()).unwrap();
                let (probe, _) = receive_request(&old, "/api/probe");
                answer_request(probe, 200, "", "{}");
                if expected_error.is_none() {
                    assert_eq!(slow.read(&mut [0]).unwrap(), 0);
                } else {
                    let (probe, _) = receive_request(&old, "/api/probe");
                    answer_request(probe, 200, "", "{}");
                    answer_request(slow, 200, "", "{}");
                }
            });
            let state = Arc::new(RuntimeState::default());
            let (client, transport) = tauri::async_runtime::spawn_blocking(transport::clients)
                .await.unwrap().unwrap();
            *state.target.lock().unwrap() = Some(Connection {
                client, transport, origin: old_origin.clone(), csrf_origin: old_origin.clone(),
                cookie: None, csrf: "old-csrf".into(), name: "old".into(), owned: false,
            });
            *state.remote_meta.lock().unwrap() = json!({"api_version":1,"capabilities":["old"]});
            let before = runtime_snapshot(&state);
            let old_state = state.clone();
            let slow = tokio::spawn(async move {
                transport::request(&old_state, "slow", None, "slow".into()).await
            });
            tokio::time::timeout(Duration::from_secs(3), old_request_received)
                .await.unwrap().unwrap();
            let switch_state = state.clone();
            let switch = tokio::spawn(async move { switch_runtime(&switch_state, Some(remote)).await });
            tokio::time::timeout(Duration::from_secs(3), handshake_paused).await.unwrap().unwrap();
            assert_eq!(runtime_snapshot(&state), before);
            assert_eq!(tokio::time::timeout(Duration::from_secs(3),
                transport::request(&state, "probe", None, "during".into())
            ).await.unwrap().unwrap().status, 200);
            assert!(!slow.is_finished());
            release.send(()).unwrap();
            let result = tokio::time::timeout(Duration::from_secs(3), switch).await.unwrap().unwrap();
            if let Some(error) = expected_error {
                assert_eq!(result.unwrap_err(), error);
                assert_eq!(runtime_snapshot(&state), before);
                assert_eq!(transport::request(&state, "probe", None, "after".into()).await.unwrap().status, 200);
                assert_eq!(slow.await.unwrap().unwrap().status, 200);
            } else {
                let snapshot = result.unwrap();
                assert_eq!(snapshot["scope"], candidate_origin);
                let health: Value = serde_json::from_str(health.1).unwrap();
                assert_eq!(snapshot["api_version"], health["api_version"]);
                assert_eq!(snapshot["capabilities"], json!(["fixture"]));
                assert_eq!(runtime_snapshot(&state), snapshot);
                assert_eq!(slow.await.unwrap().err().as_deref(), Some("Request cancelled"));
                let previous_generation = before["connection_generation"].as_u64().unwrap();
                assert_eq!(snapshot["connection_generation"], previous_generation + 1);
                let stale = transport::request_for_connection(
                    &state, "config", Some(json!({})), "late".into(), previous_generation,
                ).await;
                assert_eq!(stale.err().as_deref(), Some("Runtime connection changed"));
                assert_eq!(transport::request(&state, "config", Some(json!({})), "new".into()).await.unwrap().status, 200);
            }
            candidate_worker.join().unwrap();
            old_worker.join().unwrap();
        }
    }
    #[cfg(unix)]
    #[tokio::test]
    async fn exited_owned_child_invalidates_only_its_local_connection() {
        for (owned, remote_selected) in [(true, false), (true, true), (false, false)] {
            let state = RuntimeState::default();
            let (client, transport) = tauri::async_runtime::spawn_blocking(transport::clients)
                .await.unwrap().unwrap();
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let origin = format!("http://{}", listener.local_addr().unwrap());
            let local = Connection {
                client, transport, origin: origin.clone(), csrf_origin: origin, cookie: None,
                csrf: "local-csrf".into(), name: "local".into(), owned,
            };
            *state.local.lock().unwrap() = Some(local.clone());
            let selected = if remote_selected {
                *state.remote_meta.lock().unwrap() = json!({"api_version":2});
                Connection { origin: "https://remote.example.test".into(), owned: false, ..local }
            } else {
                local
            };
            *state.target.lock().unwrap() = Some(selected);
            *state.child.lock().unwrap() = Some(Command::new("sleep").arg("30").spawn().unwrap());
            let before = runtime_snapshot(&state);
            assert_eq!(before["connected"], true);
            {
                let mut child = state.child.lock().unwrap();
                let child = child.as_mut().unwrap();
                child.kill().unwrap();
                assert!(wait_for_child(child, Duration::from_secs(3)));
            }
            let after = runtime_snapshot(&state);
            if remote_selected || !owned {
                assert_eq!(after, before);
            } else {
                assert_eq!(after, json!({"connected":false,"error":"Local runtime exited",
                                         "connection_generation":before["connection_generation"]}));
                let result = transport::request(&state, "health", None, "after-exit".into()).await;
                assert_eq!(result.err().as_deref(), Some("Runtime unavailable"));
            }
            if owned {
                assert!(state.local.lock().unwrap().is_none());
                assert_eq!(switch_runtime(&state, None).await.unwrap_err(), "Local runtime is unavailable");
                assert_eq!(runtime_snapshot(&state), after);
            } else {
                let worker = std::thread::spawn(move || {
                    let (stream, _) = receive_request(&listener, "/api/health");
                    answer_request(stream, 200, "", r#"{"api_version":2}"#);
                });
                assert_eq!(switch_runtime(&state, None).await.unwrap(), before);
                worker.join().unwrap();
            }
        }
    }

    #[tokio::test]
    async fn return_to_local_validates_the_cached_service_before_switching() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let origin = format!("http://{}", listener.local_addr().unwrap());
        let worker = std::thread::spawn(move || {
            for (status, body) in [(503, "{}"), (200, r#"{"api_version":1}"#), (200, r#"{"api_version":2}"#)] {
                let (stream, headers) = receive_request(&listener, "/api/health");
                assert!(headers.contains("cookie: rsi_fixture=local\r\n"));
                answer_request(stream, status, "", body);
            }
        });
        let state = RuntimeState::default();
        let (client, transport) = tauri::async_runtime::spawn_blocking(transport::clients).await.unwrap().unwrap();
        let (remote_client, remote_transport) = tauri::async_runtime::spawn_blocking(transport::clients).await.unwrap().unwrap();
        *state.local.lock().unwrap() = Some(Connection {
            client, transport, origin: origin.clone(), csrf_origin: origin.clone(),
            cookie: Some("rsi_fixture=local".into()), csrf: "local".into(), name: "local".into(), owned: false,
        });
        *state.target.lock().unwrap() = Some(Connection {
            client: remote_client, transport: remote_transport, origin: origin.clone(), csrf_origin: origin,
            cookie: None, csrf: "remote".into(), name: "remote".into(), owned: false,
        });
        *state.remote_meta.lock().unwrap() = json!({"scope":"ssh:fixture","api_version":2});
        let before = runtime_snapshot(&state);
        for _ in 0..2 {
            assert_eq!(switch_runtime(&state, None).await.unwrap_err(), "Local runtime is unavailable");
            assert_eq!(runtime_snapshot(&state), before);
        }
        let after = switch_runtime(&state, None).await.unwrap();
        assert_eq!(after["kind"], "local");
        assert_eq!(after["name"], "local");
        worker.join().unwrap();
    }

    #[tokio::test]
    async fn rotation_finishes_while_an_old_probe_is_still_waiting() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let origin = format!("http://{}", listener.local_addr().unwrap());
        let (started, received) = tokio::sync::oneshot::channel();
        let worker = std::thread::spawn(move || {
            let (probe, _) = receive_request(&listener, "/api/model-center/probe");
            started.send(()).unwrap();
            let (rotate, headers) = receive_request(&listener, "/api/session/rotate");
            assert!(headers.contains("cookie: rsi_fixture=old\r\n"));
            answer_request(rotate, 200, "Set-Cookie: rsi_fixture=new; Path=/\r\n", r#"{"csrf":"new"}"#);
            let (config, headers) = receive_request(&listener, "/api/config");
            assert!(headers.contains("cookie: rsi_fixture=new\r\n"));
            assert!(headers.contains("x-rsi-csrf: new\r\n"));
            answer_request(config, 200, "", "{}");
            answer_request(probe, 200, "", "{}");
        });
        let state = Arc::new(RuntimeState::default());
        let (client, transport) = tauri::async_runtime::spawn_blocking(transport::clients).await.unwrap().unwrap();
        *state.target.lock().unwrap() = Some(Connection {
            client, transport, origin: origin.clone(), csrf_origin: origin,
            cookie: Some("rsi_fixture=old".into()), csrf: "old".into(), name: "fixture".into(), owned: false,
        });
        let probe_state = state.clone();
        let probe = tokio::spawn(async move {
            transport::request(&probe_state, "model-center/probe", None, "old-probe".into()).await
        });
        tokio::time::timeout(Duration::from_secs(2), received).await.unwrap().unwrap();
        let rotated = tokio::time::timeout(Duration::from_secs(2),
            transport::request(&state, "session/rotate", Some(json!({})), "rotate".into())
        ).await.unwrap().unwrap();
        assert_eq!(rotated.status, 200);
        assert!(!probe.is_finished());
        let updated = transport::request(&state, "config", Some(json!({})), "new-config".into()).await.unwrap();
        assert_eq!(updated.status, 200);
        assert_eq!(probe.await.unwrap().unwrap().status, 200);
        worker.join().unwrap();
    }

    #[tokio::test]
    async fn request_cancelled_before_registration_survives_capacity_overflow() {
        let state = RuntimeState::default();
        for index in 0..128 {
            state.transport.cancel(&format!("pending-{index}"));
        }
        state.transport.cancel("overflow");
        for id in ["pending-0", "overflow"] {
            let result = transport::request(&state, "session/rotate", Some(json!({})), id.into()).await;
            assert_eq!(result.err().as_deref(), Some("Request cancelled"));
        }
        assert_eq!(state.transport.active(), 0);
    }

    #[tokio::test]
    async fn backup_validates_response_cancels_stalled_bodies_and_preserves_existing_files() {
        let _gate = BACKUP_TEST_GATE.lock().await;
        let _activity = BackupTestActivity::begin();
        let persist = |staging: tempfile::NamedTempFile, path: &Path| staging.persist_noclobber(path);
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let origin = format!("http://{}", listener.local_addr().unwrap());
        let (stalled, ready) = tokio::sync::oneshot::channel();
        let (finish, stop) = std::sync::mpsc::channel();
        let valid = backup_fixture(512);
        let body = valid.clone();
        let large_length = 1_u64 << 30;
        let worker = std::thread::spawn(move || {
            for (status, mime, data) in [
                (204, "application/vnd.sqlite3", ""),
                (200, "application/json", "{}"),
                (200, "application/vnd.sqlite3", ""),
                (200, "application/vnd.sqlite3", &"x".repeat(512)),
            ] {
                let (stream, _) = receive_request(&listener, "/api/backup?id=fixture");
                answer_request(stream, status, &format!("Content-Type: {mime}\r\n"), data);
            }
            let (mut stream, _) = receive_request(&listener, "/api/backup?id=fixture");
            write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/vnd.sqlite3\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", large_length + 512).unwrap();
            drop(stream);
            for data in [&body[..0], &body[..0], body.as_str()] {
                let (mut stream, _) = receive_request(&listener, "/api/backup?id=fixture");
                write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/vnd.sqlite3\r\nContent-Length: {large_length}\r\nConnection: close\r\n\r\n{data}").unwrap();
            }
            let (stream, _) = receive_request(&listener, "/api/backup?id=fixture");
            answer_request(stream, 200, "Content-Type: application/vnd.sqlite3\r\n", &body);
            let (mut stream, _) = receive_request(&listener, "/api/backup?id=fixture");
            write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/vnd.sqlite3\r\nContent-Length: {large_length}\r\n\r\n{body}").unwrap();
            stalled.send(()).unwrap();
            let _ = stop.recv_timeout(Duration::from_secs(3));
        });
        let (client, transport) = tauri::async_runtime::spawn_blocking(transport::clients).await.unwrap().unwrap();
        let connection = Connection {
            client, transport, origin: origin.clone(), csrf_origin: origin,
            cookie: None, csrf: String::new(), name: "fixture".into(), owned: false,
        };
        let folder = tempfile::tempdir().unwrap();
        let destination = folder.path().join("backup.sqlite3");
        for error in ["Invalid backup response", "Invalid backup response", "Invalid backup size", "Backup response is not a SQLite database", "Invalid backup size"] {
            assert_eq!(transport::save_backup(&connection, "fixture", &destination).await.unwrap_err(), error);
            assert_eq!(folder.path().read_dir().unwrap().count(), 0);
        }
        for (free, expected) in [
            (Ok(large_length - 1), "Not enough disk space for backup"),
            (Err(std::io::ErrorKind::PermissionDenied.into()), "Cannot check available backup disk space"),
            (Ok(large_length), "Cannot read backup"),
        ] {
            let error = transport::save_backup_with(&connection, "fixture", &destination, |path| {
                assert_eq!(path, folder.path());
                free
            }, persist).await.unwrap_err();
            assert_eq!(error, expected);
            assert_eq!(folder.path().read_dir().unwrap().count(), 0);
        }
        assert_eq!(BACKUP_TOTAL.load(Ordering::Relaxed), large_length);
        assert_eq!(BACKUP_BYTES.load(Ordering::Relaxed), 512);
        transport::save_backup(&connection, "fixture", &destination).await.unwrap();
        assert_eq!(std::fs::read(&destination).unwrap(), valid.as_bytes());
        std::fs::write(&destination, b"existing").unwrap();
        BACKUP_ACTIVITY.store(BACKUP_DOWNLOADING, Ordering::SeqCst);
        assert_eq!(transport::save_backup(&connection, "fixture", &destination).await.unwrap_err(),
            "Choose a new backup filename");
        assert_eq!(std::fs::read(&destination).unwrap(), b"existing");

        let cancelled_destination = folder.path().join("cancelled.sqlite3");
        BACKUP_ACTIVITY.store(BACKUP_DOWNLOADING, Ordering::SeqCst);
        BACKUP_BYTES.store(0, Ordering::Relaxed);
        let download = async {
            tokio::select! {
                biased;
                _ = backup_cancelled() => true,
                _ = transport::save_backup_with(&connection, "fixture", &cancelled_destination, |_| Ok(large_length), persist) => false,
            }
        };
        let cancel = async {
            ready.await.unwrap();
            while BACKUP_BYTES.load(Ordering::Relaxed) < 512 {
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
            assert_eq!(BACKUP_TOTAL.load(Ordering::Relaxed), large_length);
            assert_eq!(folder.path().read_dir().unwrap().count(), 2);
            backup_download_status(true);
        };
        let result = tokio::time::timeout(Duration::from_secs(2), async { tokio::join!(download, cancel) }).await;
        BACKUP_ACTIVITY.store(BACKUP_IDLE, Ordering::SeqCst);
        finish.send(()).unwrap();
        worker.join().unwrap();
        assert!(result.unwrap().0);
        assert_eq!(folder.path().read_dir().unwrap().count(), 1);
    }

    fn backup_fixture(size: usize) -> String {
        format!("SQLite format 3\0\x02\0\x01\x01\0\x40\x20\x20{}", "\0".repeat(size - 24))
    }

    async fn backup_connection(listener: &TcpListener) -> Connection {
        let origin = format!("http://{}", listener.local_addr().unwrap());
        let (client, transport) = tauri::async_runtime::spawn_blocking(transport::clients).await.unwrap().unwrap();
        Connection {
            client, transport, origin: origin.clone(), csrf_origin: origin,
            cookie: None, csrf: String::new(), name: "fixture".into(), owned: false,
        }
    }

    #[tokio::test]
    async fn backup_refuses_existing_destination_before_sending_a_request() {
        let _gate = BACKUP_TEST_GATE.lock().await;
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let connection = backup_connection(&listener).await;
        let folder = tempfile::tempdir().unwrap();
        let destination = folder.path().join("existing.sqlite3");
        fs::write(&destination, b"existing").unwrap();
        assert_eq!(transport::save_backup(&connection, "fixture", &destination).await.unwrap_err(),
            "Choose a new backup filename");
        assert_eq!(fs::read(&destination).unwrap(), b"existing");
        #[cfg(unix)]
        {
            let link = folder.path().join("link.sqlite3");
            std::os::unix::fs::symlink(folder.path().join("missing"), &link).unwrap();
            assert_eq!(transport::save_backup(&connection, "fixture", &link).await.unwrap_err(),
                "Choose a new backup filename");
        }
        listener.set_nonblocking(true).unwrap();
        assert_eq!(listener.accept().unwrap_err().kind(), io::ErrorKind::WouldBlock);
        tauri::async_runtime::spawn_blocking(move || drop(connection)).await.unwrap();
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn backup_publication_falls_back_on_link_errors_and_cleans_staging() {
        use io::ErrorKind;

        let _gate = BACKUP_TEST_GATE.lock().await;
        let cases = [
            (Some(ErrorKind::Unsupported.into()), false, false, true),
            (Some(io::Error::from_raw_os_error(libc::ENOTSUP)), false, false, true),
            (Some(ErrorKind::CrossesDevices.into()), false, false, true),
            (Some(ErrorKind::PermissionDenied.into()), false, false, true),
            (Some(ErrorKind::Unsupported.into()), true, false, false),
            (None, true, false, false),
            (Some(ErrorKind::Other.into()), false, false, false),
            (Some(ErrorKind::Unsupported.into()), false, true, false),
        ];
        let case_count = cases.len();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let connection = backup_connection(&listener).await;
        let valid = backup_fixture(512);
        let body = valid.clone();
        let worker = std::thread::spawn(move || {
            for _ in 0..case_count {
                let (stream, _) = receive_request(&listener, "/api/backup?id=fixture");
                answer_request(stream, 200, "Content-Type: application/vnd.sqlite3\r\n", &body);
            }
        });
        for (link_error, claimed, unreadable, succeeds) in cases {
            let _activity = BackupTestActivity::begin();
            let folder = tempfile::tempdir().unwrap();
            let destination = folder.path().join("backup.sqlite3");
            let result = transport::save_backup_with(&connection, "fixture", &destination, |_| Ok(4096), |staging, path| {
                if claimed {
                    fs::write(path, b"claimed during download").unwrap();
                }
                let staging = if unreadable {
                    let file = fs::OpenOptions::new().write(true).open(staging.path()).unwrap();
                    let (_, temp_path) = staging.into_parts();
                    tempfile::NamedTempFile::from_parts(file, temp_path)
                } else {
                    staging
                };
                match link_error {
                    Some(error) => Err(tempfile::PersistError { error, file: staging }),
                    None => staging.persist_noclobber(path),
                }
            }).await;
            if succeeds {
                result.unwrap();
                assert_eq!(fs::read(&destination).unwrap(), valid.as_bytes());
            } else {
                assert_eq!(result.unwrap_err(), "Cannot publish backup; choose a new filename");
                if claimed {
                    assert_eq!(fs::read(&destination).unwrap(), b"claimed during download");
                }
            }
            assert_eq!(folder.path().read_dir().unwrap().count(), usize::from(succeeds || claimed));
        }
        worker.join().unwrap();
        tauri::async_runtime::spawn_blocking(move || drop(connection)).await.unwrap();
    }

    #[cfg(unix)]
    #[tokio::test]
    #[ignore = "requires RSI_TEST_NO_HARDLINK_DIR on a writable volume without hard links"]
    async fn backup_saves_on_real_volume_without_hard_links() {
        let _gate = BACKUP_TEST_GATE.lock().await;
        let volume = std::env::var_os("RSI_TEST_NO_HARDLINK_DIR").expect("set the mounted volume path");
        let folder = tempfile::tempdir_in(volume).unwrap();
        let destination = folder.path().join("backup.sqlite3");
        let probe = folder.path().join("probe");
        fs::write(&probe, b"probe").unwrap();
        let link_error = fs::hard_link(&probe, &destination).expect_err("volume supports hard links");
        assert!(link_error.raw_os_error() == Some(libc::ENOTSUP)
            || matches!(link_error.kind(), io::ErrorKind::Unsupported | io::ErrorKind::PermissionDenied),
            "unexpected hard-link error: {link_error}");
        fs::remove_file(probe).unwrap();

        let fixture_name = std::env::var_os("RSI_TEST_BACKUP_FIXTURE").expect("set the SQLite fixture path");
        let fixture = fs::read(fixture_name).unwrap();
        let body = fixture.clone();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let connection = backup_connection(&listener).await;
        let claimed_path = destination.clone();
        let worker = std::thread::spawn(move || {
            for claim in [false, true] {
                let (mut stream, _) = receive_request(&listener, "/api/backup?id=fixture");
                if claim {
                    fs::write(&claimed_path, b"claimed during download").unwrap();
                }
                write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/vnd.sqlite3\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len()).unwrap();
                stream.write_all(&body).unwrap();
            }
        });
        {
            let _activity = BackupTestActivity::begin();
            transport::save_backup(&connection, "fixture", &destination).await.unwrap();
            assert_eq!(fs::read(&destination).unwrap(), fixture);
            fs::remove_file(&destination).unwrap();
            assert_eq!(folder.path().read_dir().unwrap().count(), 0);
        }
        {
            let _activity = BackupTestActivity::begin();
            assert_eq!(transport::save_backup(&connection, "fixture", &destination).await.unwrap_err(),
                "Cannot publish backup; choose a new filename");
            assert_eq!(fs::read(&destination).unwrap(), b"claimed during download");
            fs::remove_file(&destination).unwrap();
            assert_eq!(folder.path().read_dir().unwrap().count(), 0);
        }
        worker.join().unwrap();
        tauri::async_runtime::spawn_blocking(move || drop(connection)).await.unwrap();
    }

    #[tokio::test]
    async fn backup_with_progress_can_finish_after_120_seconds() {
        let _gate = BACKUP_TEST_GATE.lock().await;
        let _activity = BackupTestActivity::begin();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let connection = backup_connection(&listener).await;
        let body = backup_fixture(8 * 1024 * 1024);
        let worker = std::thread::spawn(move || {
            let (mut stream, _) = receive_request(&listener, "/api/backup?id=fixture");
            write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/vnd.sqlite3\r\nContent-Length: {}\r\n\r\n", body.len()).unwrap();
            let started = std::time::Instant::now();
            stream.write_all(&body.as_bytes()[..512]).unwrap();
            for index in 1..=5 {
                let next = started + Duration::from_secs(25 * index);
                std::thread::sleep(next.saturating_duration_since(std::time::Instant::now()));
                let offset = (index as usize) * 512;
                let end = if index == 5 { body.len() } else { offset + 512 };
                stream.write_all(&body.as_bytes()[offset..end]).unwrap();
            }
            body
        });
        let folder = tempfile::tempdir().unwrap();
        let destination = folder.path().join("backup.sqlite3");
        let started = std::time::Instant::now();
        transport::save_backup(&connection, "fixture", &destination).await.unwrap();
        assert!(started.elapsed() > Duration::from_secs(120));
        assert_eq!(fs::read(&destination).unwrap(), worker.join().unwrap().as_bytes());
        assert_eq!(folder.path().read_dir().unwrap().count(), 1);
        tauri::async_runtime::spawn_blocking(move || drop(connection)).await.unwrap();
    }

    #[tokio::test]
    async fn backup_idle_timeout_removes_staging_when_progress_stops() {
        let _gate = BACKUP_TEST_GATE.lock().await;
        let _activity = BackupTestActivity::begin();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let connection = backup_connection(&listener).await;
        let (release, hold) = std::sync::mpsc::channel();
        let worker = std::thread::spawn(move || {
            let (mut stream, _) = receive_request(&listener, "/api/backup?id=fixture");
            write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/vnd.sqlite3\r\nContent-Length: 1024\r\n\r\n{}", backup_fixture(512)).unwrap();
            hold.recv_timeout(Duration::from_secs(40)).unwrap();
        });
        let folder = tempfile::tempdir().unwrap();
        let destination = folder.path().join("backup.sqlite3");
        let started = std::time::Instant::now();
        let result = transport::save_backup(&connection, "fixture", &destination).await;
        let elapsed = started.elapsed();
        release.send(()).unwrap();
        assert_eq!(result.unwrap_err(), "Backup response stalled");
        assert!((Duration::from_secs(30)..Duration::from_secs(40)).contains(&elapsed));
        assert_eq!(BACKUP_BYTES.load(Ordering::Relaxed), 512);
        assert_eq!(folder.path().read_dir().unwrap().count(), 0);
        worker.join().unwrap();
        tauri::async_runtime::spawn_blocking(move || drop(connection)).await.unwrap();
    }

    #[test]
    fn remote_runtime_accepts_only_supported_api_versions() {
        assert!(supported_remote_api(1));
        assert!(supported_remote_api(2));
        assert!(!supported_remote_api(0));
        assert!(!supported_remote_api(3));
    }
    #[test]
    fn api_paths_cannot_escape_authenticated_origin() {
        for path in [
            "health",
            "state?project=a",
            "usage?q=hello%20world",
            "model-center/model",
        ] {
            assert!(valid_api_path(path), "{path}");
        }
        for path in [
            "",
            "/health",
            "//example.com",
            "../health",
            "x/../health",
            "%2e%2e/health?q=a",
            "https://example.com",
            "health#x",
            "x\\health",
        ] {
            assert!(!valid_api_path(path), "{path}");
        }
    }
    #[test]
    fn local_proxy_uses_public_origin_for_csrf() {
        use std::io::{BufRead, BufReader, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let origin = format!("http://{}", listener.local_addr().unwrap());
        let worker = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut headers = String::new();
            loop {
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                if line == "\r\n" {
                    break;
                }
                headers.push_str(&line.to_lowercase());
            }
            assert!(headers.contains("origin: https://rsi.example.test\r\n"));
            assert!(headers.contains("x-rsi-csrf: fixture\r\n"));
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
                .unwrap();
        });
        let (client, transport) = super::transport::clients().unwrap();
        let c = super::Connection {
            client,
            transport,
            origin,
            csrf_origin: "https://rsi.example.test".into(),
            cookie: None,
            csrf: "fixture".into(),
            name: "fixture".into(),
            owned: false,
        };
        assert_eq!(
            super::call(&c, "config", Some(serde_json::json!({})))
                .unwrap()
                .status,
            200
        );
        worker.join().unwrap();
    }

    #[cfg(unix)]
    fn fake_local_runtime(listener: &TcpListener, owned: bool) -> (PendingChild, u32) {
        let script = r#"
            printf '{"origin":"%s","cookie":"rsi_fixture","token":"fixture","csrf":"fixture","owned":%s,"pid":%s}\n' "$1" "$2" "$$"
            exec /bin/sleep 30
        "#;
        let child = Command::new("/bin/sh")
            .args(["-c", script, "fixture-runtime"])
            .arg(format!("http://{}", listener.local_addr().unwrap()))
            .arg(owned.to_string())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn().unwrap();
        let pid = child.id();
        (PendingChild::new(child), pid)
    }

    #[cfg(unix)]
    #[test]
    fn local_startup_requires_v2_and_checks_the_owned_runtime_version_exactly() {
        for (owned, api, version, expected_error) in [
            (false, 1, "1.2.3", Some("Existing local runtime API is incompatible")),
            (false, 2, "1.2.2", None),
            (true, 1, "1.2.3", Some("Bundled runtime version or API is incompatible")),
            (true, 2, "1.2.2", Some("Bundled runtime version or API is incompatible")),
            (true, 2, "1.2.3", None),
        ] {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let (child, pid) = fake_local_runtime(&listener, owned);
            let worker = std::thread::spawn(move || {
                let (stream, headers) = receive_request(&listener, "/api/health");
                assert!(headers.contains("cookie: rsi_fixture=fixture\r\n"));
                answer_request(stream, 200, "", &json!({"api_version":api,"version":version,"pid":pid}).to_string());
                if owned && expected_error.is_none() {
                    let (stream, _) = receive_request(&listener, "/api/desktop-update");
                    answer_request(stream, 200, "", "{}");
                }
            });
            let deadline = std::time::Instant::now() + Duration::from_secs(4);
            let result = handshake_local_runtime(child, "1.2.3", deadline);
            assert_eq!(result.err().as_deref(), expected_error);
            worker.join().unwrap();
        }
    }

    #[cfg(unix)]
    #[test]
    fn local_startup_retries_cancel_until_the_barrier_is_gone() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let (child, pid) = fake_local_runtime(&listener, true);
        let (done, completed) = std::sync::mpsc::channel();
        let worker = std::thread::spawn(move || {
            let (stream, _) = receive_request(&listener, "/api/health");
            answer_request(stream, 200, "", &json!({"api_version":2,"version":"1.2.3","pid":pid}).to_string());
            let (stream, _) = receive_request(&listener, "/api/desktop-update");
            let held = r#"{"barrier":{"version":"1.2.4","lease":"fixture"}}"#;
            answer_request(stream, 200, "", held);
            for (status, response) in [(503, "{}"), (200, held), (200, r#"{"barrier":null}"#)] {
                let (stream, headers) = receive_request(&listener, "/api/desktop-update");
                assert!(headers.contains("x-rsi-csrf: fixture\r\n"));
                answer_request(stream, status, "", response);
            }
            done.send(()).unwrap();
        });
        let deadline = std::time::Instant::now() + Duration::from_secs(4);
        let result = handshake_local_runtime(child, "1.2.3", deadline);
        assert!(result.is_ok());
        completed.recv_timeout(Duration::from_secs(1)).unwrap();
        worker.join().unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn local_startup_reaps_the_child_when_update_status_or_completion_stalls() {
        for stall_on_complete in [false, true] {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let (child, pid) = fake_local_runtime(&listener, true);
            let (release, hold) = std::sync::mpsc::channel();
            let worker = std::thread::spawn(move || {
                let (stream, _) = receive_request(&listener, "/api/health");
                if !stall_on_complete {
                    std::thread::sleep(Duration::from_millis(600));
                }
                answer_request(stream, 200, "", &json!({"api_version":2,"version":"1.2.3","pid":pid}).to_string());
                let (stream, headers) = receive_request(&listener, "/api/desktop-update");
                assert!(headers.contains("x-rsi-csrf: fixture\r\n"));
                let mut stalled = if stall_on_complete {
                    std::thread::sleep(Duration::from_millis(600));
                    answer_request(stream, 200, "", r#"{"barrier":{"version":"1.2.3","lease":"fixture"}}"#);
                    receive_request(&listener, "/api/desktop-update").0
                } else {
                    stream
                };
                if stall_on_complete {
                    stalled.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n{").unwrap();
                }
                hold.recv_timeout(Duration::from_secs(3)).unwrap();
            });
            let started = std::time::Instant::now();
            let result = handshake_local_runtime(child, "1.2.3", started + Duration::from_secs(1));
            let elapsed = started.elapsed();
            release.send(()).unwrap();
            worker.join().unwrap();
            let expected = if stall_on_complete { "Cannot read response" } else { "Cannot connect to the selected runtime" };
            assert_eq!(result.err().as_deref(), Some(expected));
            assert!((Duration::from_secs(1)..Duration::from_millis(1500)).contains(&elapsed), "startup took {elapsed:?}");
            assert_eq!(unsafe { libc::kill(pid as libc::pid_t, 0) }, -1);
            assert_eq!(io::Error::last_os_error().raw_os_error(), Some(libc::ESRCH));
        }
    }

    #[cfg(unix)]
    #[test]
    fn pending_runtime_is_killed_and_reaped_on_startup_failure() {
        use std::process::Command;
        let child = Command::new("sleep").arg("30").spawn().unwrap();
        let pid = child.id();
        drop(super::PendingChild::new(child));
        let result = Command::new("/bin/kill")
            .arg("-0")
            .arg(pid.to_string())
            .output()
            .unwrap();
        assert!(
            !result.status.success(),
            "failed runtime process {pid} was left running"
        );
    }

    #[cfg(unix)]
    #[test]
    fn accepted_runtime_is_transferred_out_of_startup_guard() {
        use std::process::Command;
        let child = Command::new("sleep").arg("30").spawn().unwrap();
        let pid = child.id();
        let mut pending = super::PendingChild::new(child);
        let mut accepted = pending.take();
        drop(pending);
        let result = Command::new("/bin/kill")
            .arg("-0")
            .arg(pid.to_string())
            .output()
            .unwrap();
        assert!(
            result.status.success(),
            "accepted runtime process {pid} was terminated by startup cleanup"
        );
        let _ = accepted.kill();
        let _ = accepted.wait();
    }
}
