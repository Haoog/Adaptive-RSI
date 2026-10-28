use super::*;
use std::{collections::HashMap, sync::Arc, time::Instant};

#[derive(Clone)]
pub struct Transport(reqwest::Client);

pub fn clients() -> Result<(Client, Transport), String> {
    let cookies = Arc::new(reqwest::cookie::Jar::default());
    let blocking = Client::builder().no_proxy().redirect(reqwest::redirect::Policy::none())
        .cookie_provider(cookies.clone()).timeout(Duration::from_secs(210)).build()
        .map_err(|_| "Cannot create connection")?;
    let client = reqwest::Client::builder().no_proxy().redirect(reqwest::redirect::Policy::none())
        .cookie_provider(cookies).build().map_err(|_| "Cannot create connection")?;
    Ok((blocking, Transport(client)))
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
    requests: Mutex<HashMap<String, (Arc<AtomicBool>, bool, Instant)>>,
}

impl Control {
    pub fn cancel(&self, id: &str) {
        if !valid_id(id) { return; }
        let mut requests = self.requests.lock().unwrap();
        requests.retain(|_, (_, active, at)| *active || at.elapsed() < Duration::from_secs(30));
        if let Some((cancel, _, _)) = requests.get(id) {
            cancel.store(true, Ordering::SeqCst);
        } else if requests.len() < 128 {
            requests.insert(id.into(), (Arc::new(AtomicBool::new(true)), false, Instant::now()));
        }
    }

    pub fn cancel_all(&self) {
        for (cancel, _, _) in self.requests.lock().unwrap().values() {
            cancel.store(true, Ordering::SeqCst);
        }
    }

    pub fn active(&self) -> usize {
        self.requests.lock().unwrap().values().filter(|(_, active, _)| *active).count()
    }
}

fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 100 && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
}

struct Lease<'a> { control: &'a Control, id: String, cancel: Arc<AtomicBool> }
impl Drop for Lease<'_> {
    fn drop(&mut self) { self.control.requests.lock().unwrap().remove(&self.id); }
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
    let cookie = if rotating { response.cookies().find(|cookie| {
        target.cookie.as_deref().and_then(|value| value.split_once('='))
            .map(|(name, _)| cookie.name() == name).unwrap_or_else(|| cookie.name().starts_with("rsi_"))
    }).map(|cookie| format!("{}={}", cookie.name(), cookie.value())) } else { None };
    let mut data = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(|_| "Cannot read response")? {
        if data.len() + chunk.len() > 16 * 1024 * 1024 { return Err("Response too large".into()); }
        data.extend_from_slice(&chunk);
    }
    let reply = Reply { status, body: String::from_utf8(data).map_err(|_| "Invalid response encoding")? };
    let credentials = if rotating && (200..300).contains(&status) {
        let value: Value = serde_json::from_str(&reply.body).map_err(|_| "Invalid session response")?;
        Some((cookie.ok_or("Missing runtime session cookie")?, value["csrf"].as_str()
            .filter(|value| !value.is_empty()).ok_or("Missing runtime authentication")?.into()))
    } else { None };
    Ok((reply, credentials))
}

pub async fn request(state: &RuntimeState, path: &str, body: Option<Value>, id: String) -> Result<Reply, String> {
    if !valid_id(&id) { return Err("Invalid request ID".into()); }
    let cancel = {
        let mut requests = state.transport.requests.lock().unwrap();
        if requests.values().filter(|(_, active, _)| *active).count() >= 64 { return Err("Requests busy".into()); }
        if requests.get(&id).is_some_and(|(_, active, _)| *active) { return Err("Duplicate request ID".into()); }
        let cancel = requests.remove(&id).map(|(cancel, _, _)| cancel).unwrap_or_else(|| Arc::new(AtomicBool::new(false)));
        requests.insert(id.clone(), (cancel.clone(), true, Instant::now()));
        cancel
    };
    let lease = Lease { control: &state.transport, id, cancel };
    let rotating = path.split('?').next() == Some("session/rotate") && body.is_some();
    let operation = async {
        let _write = if rotating { Some(state.transport.gate.write().await) } else { None };
        let _read = if rotating { None } else { Some(state.transport.gate.read().await) };
        let target = state.target.lock().unwrap().as_ref().map(Target::from).ok_or("Runtime unavailable")?;
        let (reply, credentials) = call(&target, path, body).await?;
        if let Some((cookie, csrf)) = credentials {
            for slot in [&state.target, &state.local] {
                if let Some(current) = slot.lock().unwrap().as_mut() {
                    if current.origin == target.origin && current.csrf == target.csrf {
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
            result = &mut operation => return result,
            _ = tokio::time::sleep(Duration::from_millis(25)) => {}
        }
    }
}
