#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use reqwest::blocking::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader, Read},
    process::{Child, Command, Stdio},
    sync::Mutex,
    time::Duration,
};
use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Manager,
};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_opener::OpenerExt;

#[derive(Clone)]
struct Connection {
    client: Client,
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

impl Drop for PendingChild {
    fn drop(&mut self) {
        if let Some(child) = self.0.as_mut() {
            let _ = child.kill();
            let _ = child.wait();
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
}
#[derive(Serialize)]
struct Reply {
    status: u16,
    body: String,
}

fn client() -> Result<Client, String> {
    Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .cookie_store(true)
        .timeout(Duration::from_secs(210))
        .build()
        .map_err(|_| "Cannot create connection".into())
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
    if !valid_api_path(path) {
        return Err("Invalid API path".into());
    }
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
    let response = request
        .send()
        .map_err(|_| "Cannot connect to the selected runtime".to_string())?;
    let status = response.status().as_u16();
    let mut data = Vec::new();
    response
        .take(16 * 1024 * 1024 + 1)
        .read_to_end(&mut data)
        .map_err(|_| "Cannot read response")?;
    if data.len() > 16 * 1024 * 1024 {
        return Err("Response too large".into());
    }
    Ok(Reply {
        status,
        body: String::from_utf8(data).map_err(|_| "Invalid response encoding")?,
    })
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
    let child = command
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
    let (sender, receiver) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut line = String::new();
        let result = BufReader::new(stdout)
            .take(65536)
            .read_line(&mut line)
            .map(|_| line);
        let _ = sender.send(result);
    });
    let handshake = receiver
        .recv_timeout(Duration::from_secs(30))
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
    let connection = Connection {
        client: client()?,
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
    let health = call(&connection, "health", None)?;
    if health.status != 200 {
        return Err("Runtime authentication failed".into());
    }
    let state = app.state::<RuntimeState>();
    *state.local.lock().unwrap() = Some(connection.clone());
    *state.target.lock().unwrap() = Some(connection);
    *state.child.lock().unwrap() = Some(child.take());
    Ok(())
}
#[tauri::command]
async fn request(
    state: tauri::State<'_, RuntimeState>,
    path: String,
    body: Option<Value>,
) -> Result<Reply, String> {
    let connection = state
        .target
        .lock()
        .unwrap()
        .clone()
        .ok_or_else(|| state.error.lock().unwrap().clone())?;
    tauri::async_runtime::spawn_blocking(move || call(&connection, &path, body))
        .await
        .map_err(|_| "Request interrupted".to_string())?
}
#[tauri::command]
fn runtime_info(state: tauri::State<RuntimeState>) -> Value {
    match state.target.lock().unwrap().as_ref() {
        Some(c) => {
            json!({"name":c.name,"scope":c.origin,"connected":true,"kind":if c.origin.starts_with("http://127.0.0.1:"){"local"}else{"remote"},"owned":c.owned})
        }
        None => json!({"connected":false,"error":state.error.lock().unwrap().clone()}),
    }
}
#[tauri::command]
async fn connect(app: tauri::AppHandle, url: Option<String>) -> Result<Value, String> {
    let state = app.state::<RuntimeState>();
    if url.as_deref().unwrap_or("").is_empty() {
        let local = state
            .local
            .lock()
            .unwrap()
            .clone()
            .ok_or("Local runtime is unavailable")?;
        *state.target.lock().unwrap() = Some(local);
        return Ok(runtime_info(state));
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
    let connection = tauri::async_runtime::spawn_blocking(move || {
        let client = client()?;
        let response = client
            .get(parsed)
            .send()
            .map_err(|_| "Server connection failed".to_string())?;
        if ![200, 302, 303].contains(&response.status().as_u16()) {
            return Err("Server did not accept this connection".into());
        }
        let mut c = Connection {
            client,
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
        if health.status != 200 || value.get("api_version").and_then(Value::as_u64) != Some(1) {
            return Err("Update the remote runtime before connecting".into());
        }
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
    *state.target.lock().unwrap() = Some(connection);
    Ok(runtime_info(state))
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
fn notify(app: tauri::AppHandle, title: String, body: String) -> Result<(), String> {
    app.notification()
        .builder()
        .title(title.chars().take(100).collect::<String>())
        .body(body.chars().take(500).collect::<String>())
        .show()
        .map_err(|_| "Cannot show notification".into())
}

fn main() {
    let app = tauri::Builder::default()
        .manage(RuntimeState::default())
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
            runtime_info,
            connect,
            choose_folder,
            open_url,
            notify
        ])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .setup(|app| {
            let open = MenuItem::with_id(app, "open", "打开 Adaptive RSI", true, None::<&str>)?;
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
                        "quit" => app.exit(0),
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
                drop(child.stdin.take());
                let _ = child.wait();
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::valid_api_path;
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
        let c = super::Connection {
            client: super::client().unwrap(),
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
