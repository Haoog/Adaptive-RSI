#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use reqwest::blocking::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader, Read, Seek, SeekFrom, Write},
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
#[cfg(test)]
mod update_boundary_tests;
mod transport;

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
    child: Mutex<Option<Child>>,
    error: Mutex<String>,
    update: Mutex<Option<DesktopUpdate>>,
    transport: transport::Control,
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
static BACKUP_ACTIVE: AtomicBool = AtomicBool::new(false);
static BACKUP_CANCEL: AtomicBool = AtomicBool::new(false);
static BACKUP_BYTES: AtomicU64 = AtomicU64::new(0);
static BACKUP_TOTAL: AtomicU64 = AtomicU64::new(0);
static QUITTING: AtomicBool = AtomicBool::new(false);

/// Remote runtimes may expose API v1 or v2; the bundled local runtime must
/// match the client and provide the v2 capability contract.
fn supported_remote_api(version: u64) -> bool {
    matches!(version, 1 | 2)
}

fn update_client() -> Result<Client, String> {
    // 资产下载会经过 GitHub 的 https 重定向；只跟随 https，且限制跳数。
    Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::custom(|attempt| {
            if attempt.previous().len() > 5 {
                attempt.error("too many redirects")
            } else if attempt.url().scheme() == "https" {
                attempt.follow()
            } else {
                attempt.stop()
            }
        }))
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
    {
        let state = app.state::<RuntimeState>();
        let guard = state.update.lock().unwrap();
        if let Some(current) = guard.as_ref() {
            if should_preserve_update(current, force) {
                return Some(current.json());
            }
        }
    }
    // 检查失败必须与「没有新版」区分：返回带 check_error 的结构，由前端提示重试。
    let check_failed =
        || -> Option<Value> { Some(json!({"check_error": true, "error": "check_failed"})) };
    let client = match update_client() {
        Ok(client) => client,
        Err(_) => return check_failed(),
    };
    let response = match client
        .get(format!(
            "https://api.github.com/repos/{}/releases/latest",
            UPDATE_REPO
        ))
        .header("User-Agent", "Adaptive-RSI-updater")
        .header("Accept", "application/vnd.github+json")
        .send()
    {
        Ok(response) => response,
        Err(_) => return check_failed(),
    };
    if !response.status().is_success() {
        return check_failed();
    }
    let release: Value = match response.json() {
        Ok(value) => value,
        Err(_) => return check_failed(),
    };
    let update = match desktop_release(&release, &app.package_info().version.to_string()) {
        Ok(update) => update,
        Err(()) => return check_failed(),
    };
    let state = app.state::<RuntimeState>();
    let mut guard = state.update.lock().unwrap();
    *guard = update;
    guard.as_ref().map(DesktopUpdate::json)
}

fn desktop_release(release: &Value, current: &str) -> Result<Option<DesktopUpdate>, ()> {
    let tag = release.get("tag_name").and_then(Value::as_str).ok_or(())?;
    let tag = tag.strip_prefix('v').unwrap_or(tag);
    let target = parse_version(tag).ok_or(())?;
    let now = parse_version(current).ok_or(())?;
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
    if target <= now {
        return Ok(None);
    }
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
        let fail = |message: String, code: &str| {
            let state = app.state::<RuntimeState>();
            let mut guard = state.update.lock().unwrap();
            if let Some(update) = guard.as_mut() {
                update.downloading.store(false, Ordering::Relaxed);
                update.error = message;
                update.error_code = code.to_string();
            }
        };
        let client = match update_client() {
            Ok(client) => client,
            Err(error) => {
                fail(error, "");
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
                    fail("Checksum download failed".into(), "checksum_unavailable");
                    return;
                }
                text.split_whitespace().next().unwrap_or("").to_lowercase()
            }
            _ => {
                fail("Checksum download failed".into(), "checksum_unavailable");
                return;
            }
        };
        if expected.len() != 64 || !expected.bytes().all(|b| b.is_ascii_hexdigit()) {
            fail("Checksum unavailable".into(), "checksum_unavailable");
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
        let part = download_dir.join(format!(".adaptive-rsi-{}-macos-{}.dmg.part", version, arch));
        let mut staged = match std::fs::OpenOptions::new().create(true).read(true).write(true).open(&part) {
            Ok(file) => file,
            Err(_) => { fail("Cannot write download".into(), ""); return; }
        };
        let mut offset = staged.metadata().map(|meta| meta.len()).unwrap_or(0);
        let outcome = (|| -> Result<(), String> {
            let mut request = client
                .get(&url)
                .header("User-Agent", "Adaptive-RSI-updater");
            if offset > 0 {
                request = request.header("Range", format!("bytes={}-", offset));
            }
            let mut response = request.send().map_err(|_| "Download failed".to_string())?;
            let resumed = response.status() == reqwest::StatusCode::PARTIAL_CONTENT;
            if response.status() == reqwest::StatusCode::RANGE_NOT_SATISFIABLE {
                // The local part already covers the artifact; verification decides.
                return Ok(());
            }
            if !response.status().is_success() {
                return Err("Download failed".into());
            }
            if offset > 0 && !resumed {
                // The server ignored Range: appending would corrupt the artifact.
                staged.set_len(0).map_err(|_| "Cannot write download".to_string())?;
                staged.seek(SeekFrom::Start(0)).map_err(|_| "Cannot write download".to_string())?;
                offset = 0;
            } else if resumed {
                staged.seek(SeekFrom::Start(offset)).map_err(|_| "Cannot write download".to_string())?;
            } else {
                staged.seek(SeekFrom::Start(0)).map_err(|_| "Cannot write download".to_string())?;
            }
            let remaining = response.content_length().unwrap_or(0);
            let total = if resumed { offset.saturating_add(remaining) } else { remaining };
            if total > 512 * 1024 * 1024 { return Err("Download exceeds size limit".into()); }
            let mut downloaded: u64 = offset;
            let mut buffer = [0u8; 65536];
            loop {
                let read = response
                    .read(&mut buffer)
                    .map_err(|_| "Download interrupted".to_string())?;
                if read == 0 {
                    break;
                }
                if downloaded.saturating_add(read as u64) > 512 * 1024 * 1024 {
                    return Err("Download exceeds size limit".into());
                }
                staged.write_all(&buffer[..read])
                    .map_err(|_| "Cannot write download".to_string())?;
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
            staged.flush().map_err(|_| "Cannot write download".to_string())?;
            Ok(())
        })();
        if let Err(message) = outcome {
            fail(message, "");
            return;
        }
        let digest = Command::new("/usr/bin/shasum")
            .arg("-a")
            .arg("256")
            .arg(&part)
            .output()
            .ok()
            .and_then(|output| String::from_utf8(output.stdout).ok())
            .and_then(|text| text.split_whitespace().next().map(str::to_lowercase))
            .unwrap_or_default();
        {
            let state = app.state::<RuntimeState>();
            let mut guard = state.update.lock().unwrap();
            if let Some(update) = guard.as_mut() {
                update.downloading.store(false, Ordering::Relaxed);
            }
        }
        if digest.is_empty() || digest != expected {
            // A corrupt part must not be resumed on the next attempt.
            let _ = std::fs::remove_file(&part);
            fail("Checksum verification failed".into(), "checksum");
            return;
        }
        if path.exists() {
            fail("A download with this name already exists".into(), "");
            return;
        }
        if std::fs::rename(&part, &path).is_err() {
            fail("A download with this name already exists".into(), "");
            return;
        }
        #[cfg(target_os = "macos")]
        {
            let mut child = match Command::new("hdiutil").arg("attach").arg(&path).spawn() {
                Ok(child) => child,
                Err(_) => { fail("Cannot open installer".into(), ""); return; }
            };
            if !wait_for_child(&mut child, Duration::from_secs(60)) ||
                !child.try_wait().ok().flatten().is_some_and(|status| status.success()) {
                fail("Cannot open installer".into(), "");
                return;
            }
        }
        {
            let state = app.state::<RuntimeState>();
            let mut guard = state.update.lock().unwrap();
            if let Some(update) = guard.as_mut() {
                update.percent.store(10000, Ordering::Relaxed);
                update.opened.store(true, Ordering::Relaxed);
            }
        }
        #[cfg(not(target_os = "macos"))]
        let _ = &path;
    });
    // 闸门 try_lock 即刻完成，这里的短暂等待只把启动结果同步给前端。
    receiver
        .recv()
        .unwrap_or_else(|_| Err("Download did not start".into()))
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
    let (sender, receiver) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut reader = BufReader::new(reader);
        loop {
            let mut bytes = Vec::new();
            match reader.read_until(b'\n', &mut bytes) {
                Ok(0) => break,
                Ok(_) if bytes.len() > 65536 => {
                    let _ = sender.send(Err(std::io::Error::new(
                        std::io::ErrorKind::InvalidData,
                        "runtime handshake line exceeds bound",
                    )));
                    break;
                }
                Ok(_) => {
                    let line = String::from_utf8_lossy(&bytes).into_owned();
                    if sender.send(Ok(line)).is_err() {
                        break;
                    }
                }
                Err(error) => {
                    let _ = sender.send(Err(error));
                    break;
                }
            }
        }
    });
    receiver
}

///只接受同一服务代次、同一进程及其原有连接的轮换回执。
fn publish_runtime_session(state: &RuntimeState, generation: u64, pid: u32, session: Session) -> bool {
    if !session.rotated
        || session.pid != Some(pid)
        || generation != state.generation.load(Ordering::SeqCst)
    {
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
    let owned = {
        let mut local = state.local.lock().unwrap();
        let Some(current) = local.as_mut() else {
            return false;
        };
        if origin != current.origin
            || session.owned != current.owned
            || current.cookie.as_deref().map(|value| value.split('=').next().unwrap_or("")) != Some(name)
        {
            return false;
        }
        current.csrf = session.csrf.clone();
        current.cookie = Some(format!("{}={}", name, session.token));
        current.owned
    };
    if let Some(target) = state.target.lock().unwrap().as_mut() {
        if target.origin == origin && target.owned == owned {
            target.csrf = session.csrf;
            target.cookie = Some(format!("{}={}", name, session.token));
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
    if !wait_for_child(&mut child, grace) {
        *state.local.lock().unwrap() = None;
        *state.target.lock().unwrap() = None;
        state.recovering.store(true, Ordering::SeqCst);
    }
    Ok(child)
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
    if cfg!(target_os = "windows") {
        return Err("Use a Linux/macOS server connection on Windows".into());
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
    let log_dir = app
        .path()
        .app_log_dir()
        .map_err(|_| "Cannot locate runtime log")?;
    std::fs::create_dir_all(&log_dir).map_err(|_| "Cannot create runtime log directory")?;
    let mut options = std::fs::OpenOptions::new();
    options.create(true).write(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let log = options
        .open(log_dir.join("runtime.log"))
        .map_err(|_| "Cannot create runtime log")?;
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
    let health_deadline = std::time::Instant::now() + Duration::from_secs(60);
    let child = command
        .env("ADAPTIVE_RSI_DESKTOP_OWNER_PID", std::process::id().to_string())
        .arg("desktop-runtime")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::from(log))
        .spawn()
        .map_err(|_| "Cannot start bundled runtime")?;
    let mut child = PendingChild::new(child);
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
        let remaining = health_deadline.saturating_duration_since(std::time::Instant::now());
        if remaining.is_zero() {
            return Err("Runtime did not become healthy within 60 seconds".into());
        }
        match call_with_session(
            &connection,
            "health",
            None,
            Some(remaining.min(Duration::from_secs(2))),
        ) {
            Ok((health, _)) if health.status == 200 => {
                let health: Value = serde_json::from_str(&health.body).map_err(|_| "Invalid runtime health")?;
                if connection.owned && (health["version"].as_str() != Some(app.package_info().version.to_string().as_str())
                    || health["api_version"].as_u64().unwrap_or(0) < 2) {
                    return Err("Bundled runtime version or API is incompatible".into());
                }
                if connection.owned {
                    let status = call(&connection, "desktop-update", Some(json!({"action":"status"})))?;
                    if status.status != 200 { return Err("Cannot inspect update admission".into()); }
                    let status: Value = serde_json::from_str(&status.body).map_err(|_| "Invalid update admission")?;
                    if status["barrier"].is_object() {
                        let action = if status["barrier"]["version"] == health["version"] { "complete" } else { "cancel" };
                        let complete = call(&connection, "desktop-update", Some(json!({"action":action,"lease":status["barrier"]["lease"]})))?;
                        if complete.status != 200 { return Err("Cannot complete desktop update".into()); }
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
    let state = app.state::<RuntimeState>();
    *state.local.lock().unwrap() = Some(connection.clone());
    *state.target.lock().unwrap() = Some(connection);
    let pid = child.get_mut().id();
    *state.child.lock().unwrap() = Some(child.take());
    let generation = state.generation.fetch_add(1, Ordering::SeqCst) + 1;
    state.pid.store(pid, Ordering::SeqCst);
    state.recovering.store(false, Ordering::SeqCst);
    let handle = app.clone();
    std::thread::spawn(move || {
        while let Ok(Ok(line)) = receiver.recv() {
            let Ok(session) = serde_json::from_str::<Session>(&line) else {
                continue;
            };
            if session.rotated {
                let state = handle.state::<RuntimeState>();
                let _ = publish_runtime_session(&state, generation, pid, session);
            }
        }
    });
    Ok(())
}
#[tauri::command]
fn backup_download_status(cancel: bool) -> Value {
    if cancel {
        BACKUP_CANCEL.store(true, Ordering::Relaxed);
    }
    json!({"active": BACKUP_ACTIVE.load(Ordering::Relaxed),
           "bytes": BACKUP_BYTES.load(Ordering::Relaxed), "total": BACKUP_TOTAL.load(Ordering::Relaxed)})
}

#[tauri::command]
async fn download_backup(app: tauri::AppHandle, id: String) -> Result<bool, String> {
    if id.len() != 16
        || !id
            .bytes()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
    {
        return Err("Invalid backup ID".into());
    }
    tauri::async_runtime::spawn_blocking(move || {
        if BACKUP_ACTIVE
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_err()
        {
            return Err("Backup download is already active".into());
        }
        BACKUP_CANCEL.store(false, Ordering::Relaxed);
        BACKUP_BYTES.store(0, Ordering::Relaxed);
        BACKUP_TOTAL.store(0, Ordering::Relaxed);
        let result = (|| {
            let selected = app
                .dialog()
                .file()
                .set_file_name(format!("adaptive-rsi-{id}.sqlite3"))
                .blocking_save_file();
            let Some(selected) = selected else {
                return Ok(false);
            };
            let destination = selected
                .into_path()
                .map_err(|_| "Invalid backup destination")?;
            if destination.exists() {
                return Err("Choose a new backup filename".to_string());
            }
            let state = app.state::<RuntimeState>();
            let connection = state
                .target
                .lock()
                .unwrap()
                .clone()
                .ok_or("Runtime is unavailable")?;
            let mut request = connection
                .client
                .get(format!("{}/api/backup?id={id}", connection.origin))
                .timeout(Duration::from_secs(120));
            if let Some(cookie) = connection.cookie {
                request = request.header("Cookie", cookie);
            }
            let mut response = request.send().map_err(|_| "Cannot download backup")?;
            if !response.status().is_success() {
                return Err("Backup request failed".into());
            }
            let length = response
                .content_length()
                .filter(|n| *n <= 1024 * 1024 * 1024)
                .ok_or("Invalid backup size")?;
            BACKUP_TOTAL.store(length, Ordering::Relaxed);
            let staging =
                destination.with_extension(format!("sqlite3.{}.partial", std::process::id()));
            let mut options = std::fs::OpenOptions::new();
            options.write(true).create_new(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                options.mode(0o600);
            }
            let mut file = options
                .open(&staging)
                .map_err(|_| "Cannot create backup destination")?;
            let saved = (|| {
                let mut buffer = [0_u8; 65536];
                loop {
                    if BACKUP_CANCEL.load(Ordering::Relaxed) {
                        return Err("Backup download cancelled".to_string());
                    }
                    let count = response
                        .read(&mut buffer)
                        .map_err(|_| "Cannot read backup")?;
                    if count == 0 {
                        break;
                    }
                    let done =
                        BACKUP_BYTES.fetch_add(count as u64, Ordering::Relaxed) + count as u64;
                    if done > length {
                        return Err("Backup exceeded expected size".into());
                    }
                    file.write_all(&buffer[..count])
                        .map_err(|_| "Cannot write backup")?;
                }
                if BACKUP_BYTES.load(Ordering::Relaxed) != length {
                    return Err("Backup download was incomplete".into());
                }
                file.sync_all().map_err(|_| "Cannot flush backup")?;
                drop(file);
                if BACKUP_CANCEL.load(Ordering::Relaxed) {
                    return Err("Backup download cancelled".to_string());
                }
                std::fs::hard_link(&staging, &destination)
                    .map_err(|_| "Cannot publish backup; choose a new filename")?;
                Ok(true)
            })();
            let _ = std::fs::remove_file(staging);
            saved
        })();
        BACKUP_ACTIVE.store(false, Ordering::SeqCst);
        result
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
) -> Result<Reply, String> {
    static NEXT: AtomicU64 = AtomicU64::new(0);
    let id = request_id.unwrap_or_else(|| format!("legacy-{}", NEXT.fetch_add(1, Ordering::SeqCst)));
    transport::request(&app.state::<RuntimeState>(), &path, body, id).await
}
#[tauri::command]
fn cancel_request(state: tauri::State<RuntimeState>, id: String) {
    state.transport.cancel(&id);
}
#[tauri::command]
fn runtime_info(state: tauri::State<RuntimeState>) -> Value {
    let meta = state.remote_meta.lock().unwrap().clone();
    match state.target.lock().unwrap().as_ref() {
        Some(c) => {
            json!({"name":c.name,"scope":c.origin,"connected":true,"kind":if c.origin.starts_with("http://127.0.0.1:"){"local"}else{"remote"},"owned":c.owned,
                   "api_version":meta.get("api_version").cloned().unwrap_or(Value::Null),
                   "capabilities":meta.get("capabilities").cloned().unwrap_or_else(|| json!([]))})
        }
        None => json!({"connected":false,"error":state.error.lock().unwrap().clone()}),
    }
}
#[tauri::command]
fn set_native_language(app: tauri::AppHandle, language: String) {
    let english = language.to_ascii_lowercase().starts_with("en");
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
#[tauri::command]
fn quit_gracefully(app: tauri::AppHandle) -> Result<(), String> {
    if QUITTING.swap(true, Ordering::SeqCst) {
        return Ok(());
    }
    if app.state::<RuntimeState>().child.lock().unwrap().is_none() {
        app.exit(0);
        return Ok(());
    }
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
        let _ = window.eval(
            "window.dispatchEvent(new CustomEvent('rsi-app-shutdown'))",
        );
    }
    std::thread::spawn(move || {
        // Give the webview one frame to render the shutdown overlay before the
        // runtime starts draining. The overlay polls /api/shutdown-status while
        // this thread waits for the child, then the app exits.
        std::thread::sleep(Duration::from_millis(350));
        let state = app.state::<RuntimeState>();
        let mut child = state.child.lock().unwrap().take();
        if let Some(child) = child.as_mut() {
            let _ = child.stdin.take();
            if !wait_for_child(child, Duration::from_secs(30)) {
                let _ = child.kill();
                let _ = wait_for_child(child, Duration::from_secs(2));
            }
        }
        app.exit(0);
    });
    Ok(())
}
#[tauri::command]
async fn connect(app: tauri::AppHandle, url: Option<String>) -> Result<Value, String> {
    let state = app.state::<RuntimeState>();
    state.transport.cancel_all();
    *state.remote_meta.lock().unwrap() = Value::Null;
    let _connection_change = state.transport.gate.write().await;
    if url.as_deref().unwrap_or("").is_empty() {
        let handle = app.clone();
        return tauri::async_runtime::spawn_blocking(move || {
            let state = handle.state::<RuntimeState>();
            let local = state
                .local
                .lock()
                .unwrap()
                .clone()
                .ok_or("Local runtime is unavailable")?;
            *state.target.lock().unwrap() = Some(local);
            *state.remote_meta.lock().unwrap() = Value::Null;
            Ok(runtime_info(state.clone()))
        })
        .await
        .map_err(|_| "Connection interrupted".to_string())?;
    }
    let input = url.unwrap();
    if input.len() > 8192 {
        return Err("Connection URL is too long".into());
    }
    let parsed =
        reqwest::Url::parse(&input).map_err(|_| "Enter the authenticated HTTPS server URL")?;
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
    let origin = parsed.origin().ascii_serialization();
    let app_handle = app.clone();
    let connection = tauri::async_runtime::spawn_blocking(move || {
        let (client, transport) = transport::clients()?;
        let response = client
            .get(parsed)
            .send()
            .map_err(|_| "Server connection failed".to_string())?;
        if ![200, 302, 303].contains(&response.status().as_u16()) {
            return Err("Server did not accept this connection".into());
        }
        let mut c = Connection {
            client,
            transport,
            csrf_origin: origin.clone(),
            origin,
            cookie: None,
            csrf: String::new(),
            name: String::new(),
            owned: false,
        };
        let health = call(&c, "health", None)?;
        let value: Value =
            serde_json::from_str(&health.body).map_err(|_| "Invalid runtime handshake")?;
        if health.status != 200 || !value.get("api_version").and_then(Value::as_u64).is_some_and(supported_remote_api) {
            return Err("Update the remote runtime before connecting".into());
        }
        *app_handle.state::<RuntimeState>().remote_meta.lock().unwrap() = json!({
            "api_version": value.get("api_version").cloned().unwrap_or(Value::Null),
            "capabilities": value.get("capabilities").cloned().unwrap_or_else(|| json!([])),
        });
        let response = call(&c, "state?sections=core", None)?;
        let value: Value =
            serde_json::from_str(&response.body).map_err(|_| "Invalid runtime state")?;
        c.csrf = value
            .get("csrf")
            .and_then(Value::as_str)
            .ok_or("Missing runtime authentication")?
            .into();
        c.name = c.origin.clone();
        Ok::<_, String>(c)
    })
    .await
    .map_err(|_| "Connection interrupted")??;
    let handle = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let state = handle.state::<RuntimeState>();
        *state.target.lock().unwrap() = Some(connection);
        runtime_info(state.clone())
    })
    .await
    .map_err(|_| "Connection interrupted".to_string())
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
async fn native_update(app: tauri::AppHandle, action: String, installation_window: Option<String>) -> Result<Value, String> {
    match action.as_str() {
        "status" => Ok(native_update::snapshot(&app)),
        "check" => native_update::check(app, false).await,
        "download" => native_update::check(app, true).await,
        "cancel" => { native_update::cancel(&app); Ok(native_update::snapshot(&app)) },
        "install" => {
            native_update::install(app, installation_window.unwrap_or_else(|| "any".into())).await?;
            Ok(json!({"state":"restarting"}))
        },
        _ => Err("invalid_update_action".into()),
    }
}
#[tauri::command]
fn choose_folder(app: tauri::AppHandle) -> Result<Option<String>, String> {
    app.dialog()
        .file()
        .blocking_pick_folder()
        .map(|p| {
            p.into_path()
                .map(|p| p.to_string_lossy().into_owned())
                .map_err(|_| "Invalid folder".into())
        })
        .transpose()
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
    let origin = app
        .state::<RuntimeState>()
        .target
        .lock()
        .unwrap()
        .as_ref()
        .map(|c| c.origin.clone());
    app.notification()
        .builder()
        .title(title.chars().take(100).collect::<String>())
        .body(body.chars().take(500).collect::<String>())
        .extra("task", task)
        .extra("project", project)
        .extra("origin", origin)
        .show()
        .map_err(|_| "Cannot show notification".into())
}

fn main() {
    let app = tauri::Builder::default()
        .manage(RuntimeState::default())
        .manage(native_update::NativeUpdater::default())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            request,
            cancel_request,
            runtime_info,
            connect,
            choose_folder,
            open_url,
            notify,
            check_desktop_update,
            download_desktop_update
            ,native_update
            ,download_backup
            ,backup_download_status
            ,quit_gracefully
            ,set_native_language
        ])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .setup(|app| {
            let handle = app.handle().clone();
            app.notification().on_action(move |action| {
                if action.action_id() != "tap" { return; }
                if let Some(notification) = action.notification() {
                    let extra = notification.extra();
                    let origin = handle.state::<RuntimeState>().target.lock().unwrap().as_ref().map(|c| c.origin.clone());
                    if extra.get("origin").and_then(Value::as_str) != origin.as_deref() || origin.is_none() { return; }
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
            let open = MenuItem::with_id(app, "open", "打开 Adaptive RSI", true, None::<&str>)?;
            let app_open = MenuItem::with_id(app, "app-open", "打开主窗口", true, Some("CmdOrCtrl+Shift+O"))?;
            let app_update = MenuItem::with_id(app, "app-check-update", "检查更新", true, Some("CmdOrCtrl+Shift+U"))?;
            let app_diagnostics = MenuItem::with_id(app, "app-diagnostics", "导出诊断包", true, Some("CmdOrCtrl+Shift+D"))?;
            let app_quit = MenuItem::with_id(app, "app-quit", "退出 Adaptive RSI", true, Some("CmdOrCtrl+Q"))?;
            let commands = Submenu::with_id_and_items(app, "commands", "命令", true,
                &[&app_open, &app_update, &app_diagnostics])?;
            // Keep the platform default menus (Edit/Window/Help and text-editing
            // shortcuts) and replace only the app menu's quit entry with the
            // draining quit command.
            let menu = Menu::default(app.handle())?;
            if let Some(app_submenu) = menu.items()?.first().and_then(|item| item.as_submenu()) {
                let count = app_submenu.items()?.len();
                if count > 0 { let _ = app_submenu.remove_at(count - 1); }
                app_submenu.append(&app_quit)?;
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
            let quit = MenuItem::with_id(app, "quit", "退出 Adaptive RSI", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open, &quit])?;
            let mut tray =
                TrayIconBuilder::new()
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
            if let Err(error) = bootstrap(app.handle()) {
                *app.state::<RuntimeState>().error.lock().unwrap() = error;
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Cannot initialize desktop app");
    app.run(|app, event| {
        #[cfg(target_os = "macos")]
        if let tauri::RunEvent::Reopen { .. } = event {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.set_focus();
            }
        }
        if let tauri::RunEvent::ExitRequested { .. } = event {
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
    use super::{supported_remote_api, valid_api_path};
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
