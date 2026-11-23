use super::*;
use std::{collections::HashMap, sync::Arc, time::Instant};

#[derive(Clone)]
pub struct Transport(Arc<reqwest::Client>);

impl Transport {
    pub(super) fn same_connection(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.0, &other.0)
    }
}

pub fn clients() -> Result<(Client, Transport), String> {
    let cookies = Arc::new(reqwest::cookie::Jar::default());
    let blocking = Client::builder().no_proxy().redirect(reqwest::redirect::Policy::none())
        .cookie_provider(cookies.clone()).timeout(Duration::from_secs(210)).build()
        .map_err(|_| "Cannot create connection")?;
    let client = reqwest::Client::builder().no_proxy().redirect(reqwest::redirect::Policy::none())
        .cookie_provider(cookies).build().map_err(|_| "Cannot create connection")?;
    Ok((blocking, Transport(Arc::new(client))))
}

#[derive(Clone)]
struct Target {
    transport: Transport,
    origin: String,
    csrf_origin: String,
    cookie: Option<String>,
    csrf: String,
}

impl From<&Connection> for Target {
    fn from(value: &Connection) -> Self {
        Self { transport: value.transport.clone(), origin: value.origin.clone(),
            csrf_origin: value.csrf_origin.clone(), cookie: value.cookie.clone(), csrf: value.csrf.clone() }
    }
}

#[derive(Default)]
pub struct Control {
    pub gate: tokio::sync::RwLock<()>,
    pub connection_change: tokio::sync::Mutex<()>,
    rotation: tokio::sync::Mutex<()>,
    requests: Mutex<Requests>,
}

#[derive(Default)]
struct Requests {
    entries: HashMap<String, (Arc<AtomicBool>, bool, Instant)>,
    overflow_until: Option<Instant>,
}

impl Control {
    pub fn cancel(&self, id: &str) {
        if !valid_id(id) { return; }
        let mut requests = self.requests.lock().unwrap();
        requests.entries.retain(|_, (_, active, at)| *active || at.elapsed() < Duration::from_secs(30));
        if let Some((cancel, _, _)) = requests.entries.get(id) {
            cancel.store(true, Ordering::SeqCst);
        } else if requests.entries.len() < 128 {
            requests.entries.insert(id.into(), (Arc::new(AtomicBool::new(true)), false, Instant::now()));
        } else {
            // During overflow reject unknown IDs that may have been cancelled before arrival.
            requests.overflow_until = Some(Instant::now() + Duration::from_secs(30));
        }
    }

    pub fn cancel_all(&self) {
        for (cancel, _, _) in self.requests.lock().unwrap().entries.values() {
            cancel.store(true, Ordering::SeqCst);
        }
    }

    pub fn active(&self) -> usize {
        self.requests.lock().unwrap().entries.values().filter(|(_, active, _)| *active).count()
    }
}

fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 100 && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
}

struct Lease<'a> { control: &'a Control, id: String, cancel: Arc<AtomicBool> }
impl Drop for Lease<'_> {
    fn drop(&mut self) { self.control.requests.lock().unwrap().entries.remove(&self.id); }
}

const BACKUP_IDLE_TIMEOUT: Duration = Duration::from_secs(30);
const SQLITE_SIGNATURE: &[u8] = b"SQLite format 3\0";
const SQLITE_HEADER_BYTES: usize = 100;

fn valid_sqlite_header(header: &[u8; SQLITE_HEADER_BYTES], length: u64) -> bool {
    let page_size = match u16::from_be_bytes([header[16], header[17]]) {
        1 => 65536,
        size => u64::from(size),
    };
    header.starts_with(SQLITE_SIGNATURE)
        && (512..=65536).contains(&page_size)
        && page_size.is_power_of_two()
        && length % page_size == 0
        && matches!(header[18], 1 | 2)
        && matches!(header[19], 1 | 2)
        && header[21..24] == [64, 32, 32]
}

fn backup_transfer_budget(bytes: u64) -> Duration {
    Duration::from_secs(120 + bytes.div_ceil(512 * 1024))
}

pub(super) async fn save_backup(connection: &Connection, id: &str, destination: &std::path::Path) -> Result<(), String> {
    save_backup_with_available_space(connection, id, destination, |folder| fs2::available_space(folder)).await
}

pub(super) async fn save_backup_with_available_space(
    connection: &Connection,
    id: &str,
    destination: &std::path::Path,
    available_space: impl FnOnce(&std::path::Path) -> std::io::Result<u64>,
) -> Result<(), String> {
    let mut request = connection.transport.0.get(format!("{}/api/backup?id={id}", connection.origin));
    if let Some(cookie) = &connection.cookie {
        request = request.header("Cookie", cookie);
    }
    let mut response = tokio::time::timeout(BACKUP_IDLE_TIMEOUT, request.send()).await
        .map_err(|_| "Backup response timed out")?.map_err(|_| "Cannot download backup")?;
    if response.status() != reqwest::StatusCode::OK
        || response.headers().get(reqwest::header::CONTENT_TYPE).map(|value| value.as_bytes())
            != Some(b"application/vnd.sqlite3")
    {
        return Err("Invalid backup response".into());
    }
    let length = response.headers().get(reqwest::header::CONTENT_LENGTH)
        .and_then(|value| value.to_str().ok()?.parse::<u64>().ok())
        .filter(|&length| length >= 512 && response.content_length() == Some(length))
        .ok_or("Invalid backup size")?;
    let folder = destination.parent().filter(|path| !path.as_os_str().is_empty())
        .unwrap_or_else(|| std::path::Path::new("."));
    let free = available_space(folder).map_err(|_| "Cannot check available backup disk space")?;
    if free < length {
        return Err("Not enough disk space for backup".into());
    }
    BACKUP_TOTAL.store(length, Ordering::Relaxed);
    let mut staging = tempfile::Builder::new().prefix(".adaptive-rsi-backup-").suffix(".partial")
        .tempfile_in(folder).map_err(|_| "Cannot create backup destination")?;

    tokio::time::timeout(backup_transfer_budget(length), async {
        let mut done = 0_u64;
        let mut header = [0; SQLITE_HEADER_BYTES];
        loop {
            let chunk = tokio::time::timeout(BACKUP_IDLE_TIMEOUT, response.chunk()).await
                .map_err(|_| "Backup response stalled")?.map_err(|_| "Cannot read backup")?;
            let Some(chunk) = chunk else { break; };
            let chunk_bytes = chunk.len() as u64;
            if chunk_bytes > length - done {
                return Err("Backup exceeded expected size");
            }
            if done < SQLITE_HEADER_BYTES as u64 {
                let start = done as usize;
                let count = chunk.len().min(SQLITE_HEADER_BYTES - start);
                let end = start + count;
                header[start..end].copy_from_slice(&chunk[..count]);
                if end == SQLITE_HEADER_BYTES && !valid_sqlite_header(&header, length) {
                    return Err("Backup response is not a SQLite database");
                }
            }
            staging.write_all(&chunk).map_err(|_| "Cannot write backup")?;
            done += chunk_bytes;
            BACKUP_BYTES.store(done, Ordering::Relaxed);
        }
        if done != length {
            return Err("Backup download was incomplete");
        }
        Ok(())
    }).await.map_err(|_| "Backup download timed out")??;
    staging.as_file().sync_all().map_err(|_| "Cannot flush backup")?;
    if BACKUP_ACTIVITY.compare_exchange(
        BACKUP_DOWNLOADING, BACKUP_PUBLISHING, Ordering::SeqCst, Ordering::SeqCst,
    ).is_err() {
        return Err("Backup download cancelled".into());
    }
    staging.persist_noclobber(destination).map_err(|_| "Cannot publish backup; choose a new filename")?;
    Ok(())
}

pub(super) fn request_timeout(path: &str) -> Duration {
    Duration::from_secs(match path.split('?').next() {
        Some("model-center/probe") => 960,
        Some("projects/import") => 210,
        _ => 60,
    })
}

async fn call(target: &Target, path: &str, body: Option<Value>) -> Result<(Reply, Option<(String, String)>), String> {
    if !valid_api_path(path) { return Err("Invalid API path".into()); }
    let rotating = path.split('?').next() == Some("session/rotate") && body.is_some();
    let timeout = request_timeout(path);
    let url = format!("{}/api/{}", target.origin, path);
    let mut request = if let Some(data) = body {
        target.transport.0.post(url).json(&data).header("Origin", &target.csrf_origin).header("X-RSI-CSRF", &target.csrf)
    } else { target.transport.0.get(url) };
    if let Some(cookie) = &target.cookie { request = request.header("Cookie", cookie); }
    let mut response = request.timeout(timeout).send().await
        .map_err(|_| "Cannot connect to the selected runtime")?;
    let status = response.status().as_u16();
    let request_id = response.headers().get("X-RSI-Request-ID")
        .and_then(|value| value.to_str().ok()).map(str::to_string);
    let cookie = if rotating { response.cookies().find(|cookie| {
        target.cookie.as_deref().and_then(|value| value.split_once('='))
            .map(|(name, _)| cookie.name() == name).unwrap_or_else(|| cookie.name().starts_with("rsi_"))
    }).map(|cookie| format!("{}={}", cookie.name(), cookie.value())) } else { None };
    let mut data = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(|_| "Cannot read response")? {
        if data.len() + chunk.len() > 16 * 1024 * 1024 { return Err("Response too large".into()); }
        data.extend_from_slice(&chunk);
    }
    let reply = Reply { status, body: String::from_utf8(data).map_err(|_| "Invalid response encoding")?, request_id };
    let credentials = if rotating && (200..300).contains(&status) {
        let value: Value = serde_json::from_str(&reply.body).map_err(|_| "Invalid session response")?;
        Some((cookie.ok_or("Missing runtime session cookie")?, value["csrf"].as_str()
            .filter(|value| !value.is_empty()).ok_or("Missing runtime authentication")?.into()))
    } else { None };
    Ok((reply, credentials))
}

#[cfg(test)]
pub async fn request(state: &RuntimeState, path: &str, body: Option<Value>, id: String) -> Result<Reply, String> {
    let generation = state.connection_generation.load(Ordering::SeqCst);
    request_for_connection(state, path, body, id, generation).await
}

pub(super) async fn request_for_connection(
    state: &RuntimeState,
    path: &str,
    body: Option<Value>,
    id: String,
    generation: u64,
) -> Result<Reply, String> {
    if !valid_id(&id) { return Err("Invalid request ID".into()); }
    let cancel = {
        let mut requests = state.transport.requests.lock().unwrap();
        requests.entries.retain(|_, (_, active, at)| *active || at.elapsed() < Duration::from_secs(30));
        if requests.entries.values().filter(|(_, active, _)| *active).count() >= 64 { return Err("Requests busy".into()); }
        if requests.entries.get(&id).is_some_and(|(_, active, _)| *active) { return Err("Duplicate request ID".into()); }
        if requests.entries.remove(&id).is_some() || requests.overflow_until.is_some_and(|until| Instant::now() < until) {
            return Err("Request cancelled".into());
        }
        let cancel = Arc::new(AtomicBool::new(false));
        requests.entries.insert(id.clone(), (cancel.clone(), true, Instant::now()));
        cancel
    };
    let lease = Lease { control: &state.transport, id, cancel };
    let connection = state.target.lock().unwrap().as_ref().map(|current| current.transport.clone());
    let rotating = path.split('?').next() == Some("session/rotate") && body.is_some();
    let operation = async {
        let rotation = state.transport.rotation.lock().await;
        let read = state.transport.gate.read().await;
        if lease.cancel.load(Ordering::SeqCst) {
            return Err("Request cancelled".into());
        }
        invalidate_exited_local(state);
        let target = {
            let selected = state.target.lock().unwrap();
            if state.connection_generation.load(Ordering::SeqCst) != generation {
                return Err("Runtime connection changed".into());
            }
            selected.as_ref().map(Target::from).ok_or("Runtime unavailable")?
        };
        if !connection.is_some_and(|connection| target.transport.same_connection(&connection)) {
            return Err("Request cancelled".into());
        }
        // A rotation holds this lock until its new credentials are published. Ordinary
        // requests only hold it while taking their credential snapshot.
        let _rotation = if rotating {
            Some((rotation, read))
        } else {
            drop((rotation, read));
            None
        };
        let (reply, credentials) = call(&target, path, body).await?;
        if let Some((cookie, csrf)) = credentials {
            for slot in [&state.target, &state.local] {
                if let Some(current) = slot.lock().unwrap().as_mut() {
                    if current.transport.same_connection(&target.transport) && current.csrf == target.csrf {
                        current.cookie = Some(cookie.clone()); current.csrf = csrf.clone();
                    }
                }
            }
        }
        Ok(reply)
    };
    // Rotation must finish publishing credentials even if the page stops waiting.
    if rotating { return operation.await; }
    let mut operation = Box::pin(operation);
    loop {
        if lease.cancel.load(Ordering::SeqCst) {
            drop(operation);
            tokio::task::yield_now().await;
            return Err("Request cancelled".into());
        }
        tokio::select! {
            result = &mut operation => {
                if lease.cancel.load(Ordering::SeqCst) {
                    return Err("Request cancelled".into());
                }
                return result;
            },
            _ = tokio::time::sleep(Duration::from_millis(25)) => {}
        }
    }
}
