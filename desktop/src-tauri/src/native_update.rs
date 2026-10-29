use super::*;
use fs2::available_space;
use std::{fs::File, path::{Path, PathBuf}, time::Instant};
use tauri_plugin_updater::UpdaterExt;

const FEED: &str = "https://github.com/Haoog/Adaptive-RSI/releases/latest/download/desktop-latest.json";
pub(super) const MAX_PACKAGE: usize = 512 * 1024 * 1024;

/// Persisted download segment. Resuming reads its length; a completed package is
/// renamed to `CACHED_NAME` only after the signature has been verified.
pub(super) const PART_NAME: &str = "staged-update.part";
pub(super) const CACHED_NAME: &str = "staged-update.bin";
pub(super) const PART_META_NAME: &str = "staged-update.part.json";

// Transfer deadlines are split so a stalled phase reports its own cause. They are
// deliberately not loosened: total time stays at the previous 600s bound.
pub(super) const UPDATE_CONNECT_TIMEOUT: Duration = Duration::from_secs(15);
pub(super) const UPDATE_RESPONSE_TIMEOUT: Duration = Duration::from_secs(30);
pub(super) const UPDATE_IDLE_TIMEOUT: Duration = Duration::from_secs(30);
pub(super) const UPDATE_TOTAL_TIMEOUT: Duration = Duration::from_secs(600);
pub(super) const PROGRESS_INTERVAL: Duration = Duration::from_millis(250);

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

pub(super) fn part_path(cache: &Path) -> PathBuf { cache.join(PART_NAME) }
pub(super) fn cached_path(cache: &Path) -> PathBuf { cache.join(CACHED_NAME) }
pub(super) fn part_meta_path(cache: &Path) -> PathBuf { cache.join(PART_META_NAME) }

/// A segment belongs to one exact feed artifact, never just to a byte count.
pub(super) fn part_identity(version: &str, url: &str) -> String {
    format!("{version}\n{url}\n")
}

pub(super) fn identity_matches(stored: Option<&str>, version: &str, url: &str) -> bool {
    stored.is_some_and(|value| value == part_identity(version, url))
}

/// Replaces the identity file without following a planted symlink or writing
/// through a hard link; the same link checks used for the segment apply here.
pub(super) fn write_part_identity(path: &Path, value: &str) -> Result<(), String> {
    let _ = std::fs::remove_file(path);
    #[cfg(unix)]
    {
        use std::io::Write;
        use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(path)
            .map_err(|_| "update_cache_unavailable")?;
        let opened = file.metadata().map_err(|_| "update_cache_unavailable")?;
        let linked = std::fs::symlink_metadata(path).ok();
        if !opened.is_file() || opened.nlink() != 1
            || linked.as_ref().map_or(true, |meta| meta.dev() != opened.dev() || meta.ino() != opened.ino())
        {
            drop(file);
            let _ = std::fs::remove_file(path);
            return Err("update_cache_unavailable".into());
        }
        file.write_all(value.as_bytes()).map_err(|_| "update_cache_unavailable")?;
        file.sync_all().map_err(|_| "update_cache_unavailable")?;
        return Ok(());
    }
    #[cfg(not(unix))]
    {
        std::fs::write(path, value).map_err(|_| "update_cache_unavailable".into())
    }
}

/// Resume from the bytes already on disk, but never trust a segment that is
/// larger than the package bound or larger than the size the server declares.
pub(super) fn resume_offset(existing: u64, total: Option<u64>) -> u64 {
    if existing > MAX_PACKAGE as u64 { return 0; }
    if total.is_some_and(|total| existing > total) { return 0; }
    existing
}

/// A resume request must start exactly at the persisted offset.
pub(super) fn range_header(offset: u64) -> Option<String> {
    (offset > 0).then(|| format!("bytes={offset}-"))
}

#[derive(Debug, PartialEq, Eq)]
pub(super) struct ContentRange {
    pub start: u64,
    pub total: Option<u64>,
}

pub(super) fn parse_content_range(value: &str) -> Option<ContentRange> {
    let rest = value.trim().strip_prefix("bytes")?.trim_start();
    let (range, total) = rest.split_once('/')?;
    let (start, _end) = range.trim().split_once('-')?;
    Some(ContentRange { start: start.trim().parse().ok()?, total: total.trim().parse().ok() })
}

#[derive(Debug, PartialEq, Eq)]
pub(super) enum SegmentAction {
    /// 206 with a matching Content-Range start: keep the bytes on disk.
    Append,
    /// 200 (server ignored Range) or a mismatched 206: truncate and start over.
    Restart,
    /// 416 and other non-success statuses: the segment is unusable.
    Rejected,
}

pub(super) fn segment_action(existing: u64, status: u16, content_range: Option<&ContentRange>) -> SegmentAction {
    match status {
        206 if existing > 0 && content_range.is_some_and(|range| range.start == existing) => SegmentAction::Append,
        206 | 200 => SegmentAction::Restart,
        _ => SegmentAction::Rejected,
    }
}

/// A segment must not be resumed after a failure that makes its bytes untrusted.
pub(super) fn discards_segment(error: &str) -> bool {
    matches!(error, "update_size_limit" | "update_download_or_signature_failed" | "update_range_rejected")
}

#[cfg(unix)]
pub(super) fn single_link(metadata: &std::fs::Metadata) -> bool {
    use std::os::unix::fs::MetadataExt;
    metadata.nlink() == 1
}
#[cfg(not(unix))]
pub(super) fn single_link(_: &std::fs::Metadata) -> bool { true }

#[cfg(unix)]
pub(super) fn same_identity(before: &std::fs::Metadata, after: &std::fs::Metadata) -> bool {
    use std::os::unix::fs::MetadataExt;
    before.dev() == after.dev() && before.ino() == after.ino()
}
#[cfg(not(unix))]
pub(super) fn same_identity(_: &std::fs::Metadata, _: &std::fs::Metadata) -> bool { true }

pub(super) fn part_bytes(path: &Path) -> u64 {
    match std::fs::symlink_metadata(path) {
        Ok(metadata) if metadata.is_file() && single_link(&metadata) => {
            if metadata.len() > MAX_PACKAGE as u64 { discard_part(path); 0 } else { metadata.len() }
        }
        Ok(_) => { discard_part(path); 0 }
        Err(_) => 0,
    }
}

/// Removes anything squatting on the segment path, including directories, so a
/// later resume can never write through a link or fail on a stale entry.
pub(super) fn discard_part(path: &Path) {
    if path.symlink_metadata().is_err() { return; }
    if std::fs::remove_file(path).is_err() { let _ = std::fs::remove_dir(path); }
}

#[cfg(unix)]
pub(super) fn open_part(path: &Path, offset: u64) -> Result<File, String> {
    // A zero offset means a fresh download: an empty or reset segment cannot be
    // reused, and exclusive creation must start from a missing path.
    let before = if offset == 0 {
        discard_part(path);
        None
    } else {
        match std::fs::symlink_metadata(path) {
            Ok(metadata) if metadata.is_file() && single_link(&metadata) => Some(metadata),
            Ok(_) => { discard_part(path); None }
            Err(_) => None,
        }
    };
    let mut options = std::fs::OpenOptions::new();
    options.write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        // Reject a symlink at open time and never block on a swapped FIFO;
        // the metadata identity check below remains as the second layer.
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    if offset > 0 {
        // Resume must never create a new path; a missing segment is a failed
        // resume, not permission to follow whatever now occupies the name.
        options.append(true);
    } else {
        // Exclusive creation never follows a pre-existing symlink/hard link.
        options.create_new(true);
    }
    let file = match options.open(path) {
        Ok(file) => file,
        Err(_) => {
            // Remove a competing symlink/hard link before the next attempt; the
            // link target itself is never opened or modified.
            discard_part(path);
            return Err("update_cache_unavailable".into());
        }
    };
    // Always verify the object that was actually opened. A missing segment at
    // the first metadata check must not skip this: an attacker could create a
    // symlink or hard link before open and set_len would otherwise follow it.
    let after = std::fs::symlink_metadata(path).map_err(|_| "update_cache_unavailable")?;
    let opened = file.metadata().map_err(|_| "update_cache_unavailable")?;
    let identity_ok = after.is_file() && single_link(&after) && same_identity(&opened, &after)
        && before.as_ref().map_or(true, |value| same_identity(value, &after));
    if !identity_ok {
        drop(file);
        discard_part(path);
        return Err("update_range_rejected".into());
    }
    if offset == 0 {
        file.set_len(0).map_err(|_| "update_cache_unavailable")?;
    }
    if offset > 0 {
        let length = file.metadata().map_err(|_| "update_cache_unavailable")?.len();
        if length != offset {
            drop(file);
            discard_part(path);
            return Err("update_range_rejected".into());
        }
    }
    Ok(file)
}

#[cfg(not(unix))]
pub(super) fn open_part(_: &Path, _: u64) -> Result<File, String> {
    // Desktop updates are only produced and verified for macOS; refusing here
    // keeps a non-Unix build from silently weakening the link checks.
    Err("update_unsupported_platform".into())
}

/// Throttles progress writes to roughly one every `PROGRESS_INTERVAL`.
pub(super) struct Progress {
    started: Instant,
    last: Instant,
    base: u64,
}

impl Progress {
    pub(super) fn new(now: Instant, base: u64) -> Self {
        Self { started: now, last: now, base }
    }

    pub(super) fn due(&mut self, now: Instant) -> bool {
        if now.saturating_duration_since(self.last) >= PROGRESS_INTERVAL {
            self.last = now;
            true
        } else {
            false
        }
    }

    pub(super) fn rate(&self, bytes: u64, now: Instant) -> Option<u64> {
        bytes_per_second(bytes.saturating_sub(self.base), now.saturating_duration_since(self.started))
    }
}

/// `None` when the speed cannot be derived yet, which the UI renders as unknown.
pub(super) fn bytes_per_second(bytes: u64, elapsed: Duration) -> Option<u64> {
    if bytes == 0 || elapsed.is_zero() { return None; }
    let rate = (bytes as u128).saturating_mul(1_000_000_000) / elapsed.as_nanos();
    Some(rate.min(u64::MAX as u128) as u64)
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(super) enum Platform { Macos, Windows, Linux }

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(super) enum Arch { Aarch64, X86_64 }

const PLATFORM_MARKERS: [(&str, Platform); 4] =
    [("macos", Platform::Macos), ("darwin", Platform::Macos), ("windows", Platform::Windows), ("linux", Platform::Linux)];
const ARCH_MARKERS: [(&str, Arch); 4] =
    [("aarch64", Arch::Aarch64), ("arm64", Arch::Aarch64), ("x86-64", Arch::X86_64), ("amd64", Arch::X86_64)];

pub(super) fn host_platform() -> &'static str { std::env::consts::OS }
pub(super) fn host_arch() -> &'static str { std::env::consts::ARCH }

pub(super) fn host_target() -> (Option<Platform>, Option<Arch>) {
    let platform = match host_platform() {
        "macos" => Some(Platform::Macos),
        "windows" => Some(Platform::Windows),
        "linux" => Some(Platform::Linux),
        _ => None,
    };
    let arch = match host_arch() {
        "aarch64" => Some(Arch::Aarch64),
        "x86_64" => Some(Arch::X86_64),
        _ => None,
    };
    (platform, arch)
}

fn has_marker(text: &str, marker: &str) -> bool {
    let mut from = 0;
    while let Some(index) = text[from..].find(marker) {
        let start = from + index;
        let end = start + marker.len();
        let before = text[..start].chars().next_back().map_or(true, |value| !value.is_ascii_alphanumeric());
        let after = text[end..].chars().next().map_or(true, |value| !value.is_ascii_alphanumeric());
        if before && after { return true; }
        from = end;
    }
    false
}

/// Unknown or ambiguous multi-target markers stay `None` so an unlabelled or
/// universal asset is never rejected blindly.
pub(super) fn detect_target(label: &str) -> (Option<Platform>, Option<Arch>) {
    let text = label.to_ascii_lowercase().replace('_', "-");
    let mut platforms = Vec::new();
    for (marker, value) in PLATFORM_MARKERS {
        if has_marker(&text, marker) && !platforms.contains(&value) { platforms.push(value); }
    }
    let mut arches = Vec::new();
    for (marker, value) in ARCH_MARKERS {
        if has_marker(&text, marker) && !arches.contains(&value) { arches.push(value); }
    }
    let platform = if platforms.len() == 1 { platforms.first().copied() } else { None };
    let arch = if arches.len() == 1 { arches.first().copied() } else { None };
    (platform, arch)
}

/// Only the asset file name is inspected, so a host or repository name cannot
/// masquerade as a platform marker.
pub(super) fn asset_label(url: &str, name: Option<&str>) -> String {
    let path = url.split('#').next().unwrap_or(url);
    let path = path.split('?').next().unwrap_or(path);
    let file = path.rsplit('/').next().unwrap_or(path);
    match name {
        Some(name) if !name.is_empty() => format!("{file} {name}"),
        _ => file.to_string(),
    }
}

pub(super) fn require_compatible_asset(url: &str, name: Option<&str>,
                                       host: (Option<Platform>, Option<Arch>)) -> Result<(), String> {
    let (platform, arch) = detect_target(&asset_label(url, name));
    let platform_mismatch = host.0.is_some_and(|host| platform.is_some_and(|found| found != host));
    let arch_mismatch = host.1.is_some_and(|host| arch.is_some_and(|found| found != host));
    if platform_mismatch || arch_mismatch { Err("update_incompatible_platform".into()) } else { Ok(()) }
}

fn asset_name(feed: &Value) -> Option<&str> {
    ["name", "asset", "asset_name"].iter()
        .find_map(|key| feed.get(*key).and_then(Value::as_str))
}

/// Adds the progress/identity fields the panel reads, with explicit nulls so an
/// unknown total or an idle stage never looks like a number.
pub(super) fn describe_snapshot(state: &mut Value, current_version: &str, platform: &str, arch: &str) {
    for key in ["stage", "bytes", "total", "bytes_per_second"] {
        if state.get(key).is_none() { state[key] = Value::Null; }
    }
    state["current_version"] = json!(current_version);
    state["platform"] = json!(platform);
    state["arch"] = json!(arch);
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
    authorization: Mutex<Authorization>,
    waiting: Mutex<Option<u64>>,
    revoke: std::sync::Arc<AtomicBool>,
}

/// The user-visible update settings, versioned so an install can be bound to the
/// exact authorization it was approved under.
#[derive(Clone, PartialEq, Eq, Debug)]
pub(super) struct Authorization {
    pub(super) revision: u64,
    pub(super) policy: String,
    pub(super) scope: String,
    pub(super) window: String,
}

impl Default for Authorization {
    fn default() -> Self {
        Self { revision: 0, policy: "download".into(), scope: "patch".into(), window: "any".into() }
    }
}

impl Authorization {
    pub(super) fn parse(revision: u64, policy: &str, scope: &str, window: &str) -> Result<Self, String> {
        let allowed = |value: &str, values: [&str; 3]| values.contains(&value);
        if !allowed(policy, ["manual", "download", "automatic"])
            || !allowed(scope, ["patch", "minor", "stable"])
            || !allowed(window, ["any", "night", "weekend"])
        {
            return Err("update_authorization_invalid".into());
        }
        Ok(Self { revision, policy: policy.into(), scope: scope.into(), window: window.into() })
    }
}

/// One install request exactly as the panel sent it. `revision` stays optional so
/// a missing value can be rejected explicitly instead of borrowing the live one.
pub(super) struct InstallRequest<'a> {
    pub(super) revision: Option<u64>,
    pub(super) automatic: bool,
    pub(super) window: &'a str,
    pub(super) current_version: &'a str,
    pub(super) target_version: &'a str,
}

/// What one replacement is allowed to do, derived from the stored authorization.
#[derive(Clone, PartialEq, Eq, Debug)]
pub(super) struct InstallPermit {
    pub(super) revision: u64,
    pub(super) window: String,
}

/// An install without an approved revision is refused: revision 0 means the panel
/// never pushed settings, and borrowing the stored revision would let a caller
/// that never consented replace the app.
pub(super) fn required_revision(revision: Option<u64>) -> Result<u64, String> {
    revision.filter(|value| *value > 0).ok_or_else(|| "update_authorization_invalid".into())
}

/// The same boundary the panel applies, re-derived natively: only a strictly
/// newer target inside the stored scope may be installed automatically.
pub(super) fn scope_allows(scope: &str, current: &str, target: &str) -> bool {
    let (Some((major, minor, patch)), Some((next_major, next_minor, next_patch))) =
        (parse_version(current), parse_version(target)) else { return false; };
    if (next_major, next_minor, next_patch) <= (major, minor, patch) { return false; }
    match scope {
        "patch" => next_major == major && next_minor == minor,
        "minor" => next_major == major,
        "stable" => true,
        _ => false,
    }
}

/// The single decision point for a replacement. An automatic install runs only
/// under the policy, window and scope the user stored: the forwarded window is
/// compared against the stored one and never trusted on its own. A manual click
/// is the user's own consent, so it may replace in any window, but it still needs
/// the exact revision it was approved under.
pub(super) fn install_permit(stored: &Authorization, request: &InstallRequest<'_>) -> Result<InstallPermit, String> {
    let revision = required_revision(request.revision)?;
    if stored.revision != revision { return Err("update_authorization_changed".into()); }
    if !request.automatic {
        return Ok(InstallPermit { revision, window: "any".into() });
    }
    if stored.policy != "automatic" || stored.window != request.window {
        return Err("update_authorization_changed".into());
    }
    if !scope_allows(&stored.scope, request.current_version, request.target_version) {
        return Err("update_target_changed".into());
    }
    Ok(InstallPermit { revision, window: stored.window.clone() })
}

impl NativeUpdater {
    pub(super) fn authorization(&self) -> Authorization {
        self.authorization.lock().unwrap().clone()
    }

    /// Accepts only a newer authorization. A revision that is equal or older is a
    /// duplicate or an out-of-order call and must never roll back live settings.
    pub(super) fn authorize(&self, next: Authorization) -> Result<bool, String> {
        {
            let mut current = self.authorization.lock().unwrap();
            if next.revision <= current.revision {
                return Ok(false);
            }
            *current = next.clone();
        }
        // A waiting install was approved under the previous revision; revoke it
        // so the replacement is re-authorized instead of silently proceeding.
        if let Some(waiting) = *self.waiting.lock().unwrap() {
            if waiting != next.revision {
                self.revoke.store(true, Ordering::SeqCst);
            }
        }
        Ok(true)
    }

    pub(super) fn begin_waiting(&self, revision: u64) -> WaitingInstall<'_> {
        *self.waiting.lock().unwrap() = Some(revision);
        WaitingInstall { updater: self, revision }
    }

    pub(super) fn require_authorization(&self, revision: u64) -> Result<(), String> {
        if self.revoke.load(Ordering::SeqCst) || self.authorization.lock().unwrap().revision != revision {
            return Err("update_authorization_changed".into());
        }
        Ok(())
    }

    /// Re-derives the permit at a replacement boundary, so a changed policy,
    /// scope or window withdraws the replacement even mid-drain.
    pub(super) fn require_install_permit(&self, request: &InstallRequest<'_>) -> Result<InstallPermit, String> {
        self.require_authorization(required_revision(request.revision)?)?;
        install_permit(&self.authorization(), request)
    }

    #[cfg(test)]
    pub(super) fn cancelled(&self) -> bool {
        self.cancel.load(Ordering::SeqCst)
    }
}

pub(super) struct WaitingInstall<'a> {
    updater: &'a NativeUpdater,
    revision: u64,
}

impl Drop for WaitingInstall<'_> {
    fn drop(&mut self) {
        let mut waiting = self.updater.waiting.lock().unwrap();
        if *waiting == Some(self.revision) {
            *waiting = None;
        }
    }
}

impl Default for NativeUpdater {
    fn default() -> Self {
        Self { state: Mutex::new(json!({"state":"idle"})), busy: Default::default(),
               cancel: Default::default(), staged: Mutex::new(None),
               authorization: Mutex::new(Authorization::default()),
               waiting: Mutex::new(None), revoke: Default::default() }
    }
}

impl NativeUpdater {
    pub(super) fn begin(&self) -> Result<UpdateOperation, String> {
        if self.busy.swap(true, Ordering::SeqCst) { return Err("update_busy".into()); }
        self.cancel.store(false, Ordering::SeqCst);
        self.revoke.store(false, Ordering::SeqCst);
        *self.waiting.lock().unwrap() = None;
        Ok(UpdateOperation { busy: self.busy.clone(), cancel: self.cancel.clone() })
    }

    fn set(&self, value: Value) {
        *self.state.lock().unwrap() = value;
    }
}

// Own the slot across async preflight and the blocking worker, including worker errors.
pub(super) struct UpdateOperation {
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
    let updater = app.state::<NativeUpdater>();
    let mut state = updater.state.lock().unwrap().clone();
    describe_snapshot(&mut state, &app.package_info().version.to_string(), host_platform(), host_arch());
    // The panel binds every install to this revision, so it must always be present.
    state["authorization_revision"] = json!(updater.authorization().revision);
    state
}

/// Stores the user-visible settings under a newer revision. A changed revision
/// revokes a waiting install instead of letting it replace under stale consent.
pub fn authorize(app: &tauri::AppHandle, revision: u64, policy: &str, scope: &str, window: &str) -> Result<Value, String> {
    let next = Authorization::parse(revision, policy, scope, window)?;
    app.state::<NativeUpdater>().authorize(next)?;
    Ok(snapshot(app))
}

pub async fn check(app: tauri::AppHandle, download: bool) -> Result<Value, String> {
    let updater = app.state::<NativeUpdater>();
    let Ok(_operation) = updater.begin() else { return Ok(snapshot(&app)); };
    check_in_operation(&app, download).await
}

/// Streams the package into `staged-update.part`, resuming from the bytes that a
/// previous run (or a cancelled run) left behind.
async fn transfer(updater: &NativeUpdater, update: &tauri_plugin_updater::Update, part: &Path,
                  mut offset: u64, total: &mut Option<u64>, version: &str, notes: &str) -> Result<u64, String> {
    let client = reqwest::Client::builder()
        .connect_timeout(UPDATE_CONNECT_TIMEOUT)
        .build()
        .map_err(|_| "update_check_failed")?;
    let started = Instant::now();
    let mut attempt = 0u8;
    loop {
        attempt += 1;
        if updater.cancel.load(Ordering::SeqCst) { return Err("update_cancelled".into()); }
        let request_started = Instant::now();
        let mut request = client.get(update.download_url.clone()).headers(update.headers.clone());
        if let Some(range) = range_header(offset) {
            request = request.header(reqwest::header::RANGE, range);
        }
        let send = request.send();
        tokio::pin!(send);
        let mut response = loop {
            if updater.cancel.load(Ordering::SeqCst) { return Err("update_cancelled".into()); }
            if request_started.elapsed() >= UPDATE_RESPONSE_TIMEOUT { return Err("update_response_timeout".into()); }
            if started.elapsed() >= UPDATE_TOTAL_TIMEOUT { return Err("update_download_timeout".into()); }
            tokio::select! {
                value = &mut send => break value.map_err(|error| {
                    if error.is_timeout() || error.is_connect() {
                        "update_connect_timeout".to_string()
                    } else {
                        "update_connection_failed".to_string()
                    }
                })?,
                _ = tokio::time::sleep(Duration::from_millis(100)) => {}
            }
        };
        let status = response.status().as_u16();
        if status == 416 {
            // An unsatisfiable range with a non-zero offset usually means the
            // persisted segment already holds the complete package. Verify it
            // before deciding to discard it.
            if offset > 0 { return Ok(offset); }
            discard_part(part);
            return Err("update_range_rejected".into());
        }
        if !(200..300).contains(&status) { return Err("update_download_or_signature_failed".into()); }
        let content_range = response.headers().get(reqwest::header::CONTENT_RANGE)
            .and_then(|value| value.to_str().ok())
            .and_then(parse_content_range);
        match segment_action(offset, status, content_range.as_ref()) {
            SegmentAction::Rejected => {
                // 416 is handled above; any other unexpected success status means
                // the body cannot be trusted as a segment for this package.
                discard_part(part);
                return Err("update_download_or_signature_failed".into());
            }
            SegmentAction::Restart if offset > 0 => {
                // The server ignored our offset (200) or answered for another start.
                discard_part(part);
                offset = 0;
                if attempt < 2 { continue; }
                // A second inconsistent answer is not written as if it were a
                // full body; the segment is already gone and the caller retries.
                return Err("update_download_or_signature_failed".into());
            }
            SegmentAction::Append | SegmentAction::Restart => {}
        }
        let declared = content_range.as_ref().and_then(|range| range.total)
            .or_else(|| response.content_length().map(|length| offset.saturating_add(length)));
        if declared.is_some_and(|value| value > MAX_PACKAGE as u64) {
            discard_part(part);
            return Err("update_size_limit".into());
        }
        if declared.is_some_and(|value| value < offset) {
            discard_part(part);
            return Err("update_download_or_signature_failed".into());
        }
        if let Some(value) = declared { *total = Some(value); }
        let mut file = open_part(part, offset)?;
        let mut progress = Progress::new(Instant::now(), offset);
        let mut count = offset;
        updater.set(json!({"state":"downloading","stage":"downloading","version":version,"notes":notes,
            "bytes":count,"total":*total,"bytes_per_second":Value::Null}));
        loop {
            if updater.cancel.load(Ordering::SeqCst) { return Err("update_cancelled".into()); }
            if started.elapsed() >= UPDATE_TOTAL_TIMEOUT { return Err("update_download_timeout".into()); }
            let receive = tokio::time::timeout(UPDATE_IDLE_TIMEOUT, response.chunk());
            tokio::pin!(receive);
            let chunk = loop {
                if updater.cancel.load(Ordering::SeqCst) { return Err("update_cancelled".into()); }
                if started.elapsed() >= UPDATE_TOTAL_TIMEOUT { return Err("update_download_timeout".into()); }
                tokio::select! {
                    value = &mut receive => break value.map_err(|_| "update_transfer_stalled")?
                        .map_err(|_| "update_download_or_signature_failed")?,
                    _ = tokio::time::sleep(Duration::from_millis(100)) => {}
                }
            };
            let Some(chunk) = chunk else { break; };
            let next = count.saturating_add(chunk.len() as u64);
            if next > MAX_PACKAGE as u64 {
                discard_part(part);
                return Err("update_size_limit".into());
            }
            file.write_all(&chunk).map_err(|_| "update_cache_unavailable")?;
            count = next;
            let now = Instant::now();
            if progress.due(now) {
                updater.set(json!({"state":"downloading","stage":"downloading","version":version,"notes":notes,
                    "bytes":count,"total":*total,"bytes_per_second":progress.rate(count, now)}));
            }
        }
        file.sync_all().map_err(|_| "update_cache_unavailable")?;
        if total.is_some_and(|value| count < value) {
            // A transient truncation gets one more range request from the
            // current offset; a deterministic truncation discards the segment
            // so the next attempt can restart instead of looping forever.
            if attempt < 2 { offset = count; continue; }
            discard_part(part);
            return Err("update_download_incomplete".into());
        }
        return Ok(count);
    }
}

async fn download_package(updater: &NativeUpdater, update: &tauri_plugin_updater::Update,
                          cache: &Path, version: &str, notes: &str) -> Result<(), String> {
    let cached = cached_path(cache);
    let part = part_path(cache);
    let meta = part_meta_path(cache);
    let announced = announced_size(&update.raw_json);
    let url = update.download_url.as_str();
    let stored = std::fs::read_to_string(&meta).ok();
    if !identity_matches(stored.as_deref(), version, url) {
        // Never append a new release to a segment left by another artifact.
        discard_part(&part);
        write_part_identity(&meta, &part_identity(version, url))?;
    }
    let offset = resume_offset(part_bytes(&part), (announced < MAX_PACKAGE as u64).then_some(announced));
    // The cache, application and extraction volumes all need room for the
    // complete package; checking only the remaining bytes under-reserved them.
    preflight_space(cache, announced)?;
    let mut total = (announced < MAX_PACKAGE as u64).then_some(announced);
    let mut count = offset;
    // A segment that already holds the announced size needs no request at all.
    if announced == MAX_PACKAGE as u64 || offset < announced {
        count = transfer(updater, update, &part, offset, &mut total, version, notes).await?;
    }
    updater.set(json!({"state":"verifying","stage":"verifying","version":version,"notes":notes,
        "bytes":count,"total":total,"bytes_per_second":Value::Null}));
    let verified = read_package(&part).and_then(|bytes| {
        update.verify(&bytes).map_err(|_| "update_download_or_signature_failed".to_string())
    });
    if let Err(error) = verified {
        if discards_segment(&error) { discard_part(&part); }
        return Err(error);
    }
    // Atomic hand-off: the verified bytes only become the cached package once
    // the signature matched, so a crash can never stage an unverified file.
    if std::fs::rename(&part, &cached).is_err() {
        discard_part(&part);
        return Err("update_cache_unavailable".into());
    }
    let _ = std::fs::remove_file(&meta);
    Ok(())
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
        // Reject a clearly foreign asset before any byte is requested. Unlabelled
        // assets stay eligible, so the conservative default is unchanged.
        require_compatible_asset(update.download_url.as_str(), asset_name(&update.raw_json), host_target())?;
        if update.download_url.scheme() != "https" { return Err("invalid_update_feed".into()); }
        let version = update.version.clone();
        let notes = update.body.clone().unwrap_or_default();
        updater.set(json!({"state":"available","version":version,"notes":notes}));
        let cache = app.path().app_cache_dir().map_err(|_| "update_cache_unavailable")?;
        std::fs::create_dir_all(&cache).map_err(|_| "update_cache_unavailable")?;
        let cached = cached_path(&cache);
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
        download_package(&updater, &update, &cache, &version, &notes).await?;
        *updater.staged.lock().unwrap() = Some((update, cached));
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

pub async fn install(app: tauri::AppHandle, window: String, revision: u64, automatic: bool) -> Result<(), String> {
    let operation = app.state::<NativeUpdater>().begin()?;
    let updater = app.state::<NativeUpdater>();
    let approved = staged_artifact(&app)?;
    let target = approved.get("version").and_then(Value::as_str).ok_or("update_not_downloaded")?.to_string();
    let current = app.package_info().version.to_string();
    // Bind the whole replacement to the stored authorization. The revision is
    // required: a missing one is refused instead of borrowing the live one, and
    // the window is derived here rather than taken from the caller.
    let request = InstallRequest { revision: Some(revision), automatic, window: &window,
        current_version: &current, target_version: &target };
    let permit = updater.require_install_permit(&request)?;
    let eligibility = check_in_operation(&app, false).await?;
    if eligibility["state"] != "ready" { return Err("update_not_downloaded".into()); }
    require_same_artifact(&approved, Some(&staged_artifact(&app)?))?;
    updater.require_install_permit(&request)?;
    tauri::async_runtime::spawn_blocking(move || {
        operation.run_install(|| install_in_operation(app, permit, automatic, approved, current, target))
    }).await.map_err(|_| "update_worker_failed".to_string())?
}

fn install_in_operation(app: tauri::AppHandle, permit: InstallPermit, automatic: bool, approved: Value,
                        current_version: String, target_version: String) -> Result<(), String> {
    let updater = app.state::<NativeUpdater>();
    let local = app.state::<RuntimeState>().local.lock().unwrap().clone();
    let revision = permit.revision;
    let window = permit.window;
    let request = InstallRequest { revision: Some(revision), automatic, window: &window,
        current_version: &current_version, target_version: &target_version };
    // Every replacement boundary re-derives the permit from stored consent, so a
    // changed revision, policy, window or scope withdraws it before any replace.
    let boundary = || -> Result<(), String> {
        require_window(&window)?;
        updater.require_install_permit(&request).map(|_| ())
    };
    // Every exit path clears this, and a newer authorization revokes it.
    let _waiting = updater.begin_waiting(revision);
    let mut lease = Value::Null;
    let mut engine_stopped = false;
    let result: Result<(), String> = (|| {
        boundary()?;
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
                // A policy/scope/window change revokes this waiting install; the
                // barrier lease is released so the runtime keeps running normally.
                if let Err(error) = boundary() {
                    let _ = call(connection, "desktop-update", Some(json!({"action":"cancel","lease":lease})));
                    return Err(error);
                }
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
            boundary()?;
            revalidate(&app, &approved)?;
            boundary()?;
            let reply = call(connection, "desktop-update", Some(json!({"action":"commit","lease":lease})))?;
            if reply.status != 200 { return Err("update_drain_failed".into()); }
        }
        if updater.cancel.load(Ordering::SeqCst) { return Err("update_wait_cancelled".into()); }
        boundary()?;
        if owned.is_none() { revalidate(&app, &approved)?; }
        let cache = app.path().app_cache_dir().map_err(|_| "update_cache_unavailable")?;
        preflight_space(&cache, announced_size(&approved))?;
        if updater.cancel.load(Ordering::SeqCst) { return Err("update_wait_cancelled".into()); }
        updater.set(json!({"state":"installing","stage":"installing","version":version}));
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
        boundary()?;
        let staged = updater.staged.lock().unwrap().take().ok_or("update_not_downloaded")?;
        let installation = read_package(&staged.1).and_then(|bytes| {
            boundary()?;
            if updater.cancel.load(Ordering::SeqCst) { return Err("update_wait_cancelled".into()); }
            // Verify the exact bytes being installed so both the persisted receipt
            // and the replacement decision come from a real signature check.
            let verified = staged.0.verify(&bytes);
            let receipt = update_receipt::prepare_app(&app)?;
            update_receipt::begin_pending(&receipt, &update_receipt::Transaction {
                operation_id: update_receipt::operation_id(),
                from_version: app.package_info().version.to_string(),
                to_version: staged.0.version.clone(),
                artifact_sha256: update_receipt::sha256_hex(&bytes),
                signature_sha256: update_receipt::sha256_hex(staged.0.signature.as_bytes()),
                signature_verified: verified.is_ok(),
                artifact_url: staged.0.download_url.as_str().to_string(),
                authorization_revision: revision,
                install_window: window.clone(),
                platform: host_platform().into(),
                arch: host_arch().into(),
            })?;
            if verified.is_err() {
                // Keep the failed check as evidence; nothing was replaced.
                let _ = update_receipt::transition(&receipt, "failed", Some("update_download_or_signature_failed"), None);
                return Err("update_download_or_signature_failed".into());
            }
            if staged.0.install(&bytes).is_err() {
                // Record the failed attempt, but keep the install error as the
                // primary fact. If the record cannot be updated it stays pending
                // and is settled by the next start; it is never read as applied.
                let _ = update_receipt::transition(&receipt, "failed", Some("update_install_failed"), None);
                return Err("update_install_failed".into());
            }
            Ok(())
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
