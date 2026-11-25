use super::*;
use fs2::available_space;
use std::{fs::File, path::{Path, PathBuf}, time::Instant};
use tauri_plugin_updater::UpdaterExt;

const FEED: &str = "https://github.com/Haoog/Adaptive-RSI/releases/latest/download/desktop-latest.json";
pub(super) static REPLACEMENT_GATE: Mutex<()> = Mutex::new(());
pub(super) use crate::updater_signature::MAX_PACKAGE;
#[cfg(unix)]
const EXTRACTION_SPACE_MULTIPLIER: u64 = 2;

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

#[cfg(unix)]
fn space_after_snapshot(
    cache: &Path, snapshot_bytes: u64, extraction: &Path,
    available: impl FnOnce(&Path) -> std::io::Result<u64>,
) -> std::io::Result<u64> {
    use std::os::unix::fs::MetadataExt;

    let free = available(extraction)?;
    if snapshot_bytes > 0 && cache.metadata()?.dev() == extraction.metadata()?.dev() {
        return Ok(free.saturating_sub(snapshot_bytes));
    }
    Ok(free)
}

fn preflight_install_space(app: &tauri::AppHandle, cache: &Path, package: u64, snapshot_pending: bool) -> Result<(), String> {
    // The staged package and current installation already occupy disk space.
    // Tauri renames the Unix backup on the same filesystem. The new extraction
    // may be larger than the archive, so keep two package lengths of headroom.
    #[cfg(unix)]
    let (extraction_bytes, available) = {
        let snapshot_bytes = if snapshot_pending { package } else { 0 };
        if snapshot_bytes > 0 {
            require_space(&[cache], snapshot_bytes, |path| available_space(path))?;
        }
        (package.saturating_mul(EXTRACTION_SPACE_MULTIPLIER), move |path: &Path| {
            space_after_snapshot(cache, snapshot_bytes, path, |path| available_space(path))
        })
    };
    let temporary = std::env::temp_dir();
    #[cfg(target_os = "linux")]
    {
        use tauri::utils::{config::BundleType, platform::{bundle_type, current_exe}};

        // UpdaterExt uses the original AppImage, rather than its mounted executable.
        let executable = match app.env().appimage {
            Some(path) => PathBuf::from(path),
            None => current_exe().map_err(|_| "update_disk_unavailable")?,
        };
        let user_cache = app.path().cache_dir().ok();
        let app_dir = executable.parent().ok_or("update_disk_unavailable")?;
        let candidates = [Some(temporary.as_path()), user_cache.as_deref(), Some(app_dir)];
        let package_manager = matches!(bundle_type(), Some(BundleType::Deb | BundleType::Rpm));
        // deb/rpm fall back when writing the package fails. AppImage selects
        // the first usable same-device directory before writing the new image.
        let extraction_bytes = if package_manager { package } else { extraction_bytes };
        linux_extraction_space(&executable, candidates, package_manager, extraction_bytes, available)
    }
    #[cfg(all(unix, not(target_os = "linux")))]
    {
        let _ = app;
        let executable = std::env::current_exe().map_err(|_| "update_disk_unavailable")?;
        let extraction = extraction_root(&executable, &temporary)?;
        require_space(&[&extraction], extraction_bytes, available)
    }
    #[cfg(not(unix))]
    {
        // The in-memory snapshot uses no cache space. The external installer
        // manages its own extraction and rollback, so keep its existing margin.
        let _ = (app, cache, snapshot_pending);
        let executable = std::env::current_exe().map_err(|_| "update_disk_unavailable")?;
        require_space(&[executable.parent().ok_or("update_disk_unavailable")?, &temporary],
                      package.saturating_mul(3), |path| available_space(path))
    }
}

#[cfg(all(unix, any(target_os = "linux", test)))]
pub(super) fn linux_extraction_space(
    executable: &Path, candidates: [Option<&Path>; 3], package_manager: bool, required: u64,
    mut available: impl FnMut(&Path) -> std::io::Result<u64>,
) -> Result<(), String> {
    use std::os::unix::fs::MetadataExt;

    let device = if package_manager { None } else {
        Some(executable.metadata().map_err(|_| "update_disk_unavailable")?.dev())
    };
    let mut insufficient = false;
    for candidate in candidates.into_iter().flatten() {
        // Tauri skips candidates where it cannot create its temporary directory.
        let Ok(probe) = tempfile::Builder::new().prefix("rsi-update-preflight").tempdir_in(candidate)
            else { continue; };
        if device.is_some_and(|device| !probe.path().metadata().is_ok_and(|meta| meta.dev() == device)) {
            continue;
        }
        match available(probe.path()) {
            Ok(bytes) if bytes >= required => return Ok(()),
            Ok(_) if !package_manager => return Err("update_disk_space".into()),
            Ok(_) => insufficient = true,
            Err(_) if !package_manager => return Err("update_disk_unavailable".into()),
            Err(_) => {},
        }
    }
    Err(if insufficient { "update_disk_space" } else { "update_disk_unavailable" }.into())
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

const MAX_PART_IDENTITY: u64 = 16 * 1024;

fn read_part_identity(path: &Path) -> Result<Option<String>, String> {
    match std::fs::symlink_metadata(path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err("update_cache_unavailable".into()),
        Ok(_) => {}
    }
    let (file, _) = open_bounded_file(path, MAX_PART_IDENTITY).map_err(|_| "update_cache_unavailable")?;
    let mut value = String::new();
    file.take(MAX_PART_IDENTITY + 1).read_to_string(&mut value).map_err(|_| "update_cache_unavailable")?;
    if value.len() as u64 > MAX_PART_IDENTITY {
        return Err("update_cache_unavailable".into());
    }
    Ok(Some(value))
}

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
    if value.len() > MAX_PART_IDENTITY as usize { return Err("update_cache_unavailable".into()); }
    discard_part(path);
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

#[cfg(test)]
pub(super) fn parse_content_range(value: &str) -> Option<ContentRange> {
    parse_segment_range(value).map(|(range, _)| range)
}

pub(super) fn parse_segment_range(value: &str) -> Option<(ContentRange, u64)> {
    let (range, total) = value.trim().strip_prefix("bytes ")?.split_once('/')?;
    let (start, end) = range.split_once('-')?;
    let start = start.parse::<u64>().ok()?;
    let end = end.parse::<u64>().ok()?.checked_add(1)?;
    let total = match total {
        "*" => None,
        value => Some(value.parse().ok()?),
    };
    (end > start && total.is_none_or(|total| end <= total))
        .then_some((ContentRange { start, total }, end))
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
    let Ok(metadata) = path.symlink_metadata() else { return; };
    if !metadata.is_dir() {
        let _ = std::fs::remove_file(path);
    } else if std::fs::remove_dir(path).is_err() {
        // Move a non-empty directory out of the cache slot and preserve its contents.
        if let Some(parent) = path.parent() {
            if let Ok(quarantine) = tempfile::Builder::new().prefix("staged-update-quarantine-").tempdir_in(parent) {
                if std::fs::rename(path, quarantine.path().join("entry")).is_ok() {
                    let _ = quarantine.keep();
                }
            }
        }
    }
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

#[cfg(all(unix, not(target_os = "linux")))]
pub(super) fn extraction_root(executable: &Path, temporary: &Path) -> Result<PathBuf, String> {
    use std::os::unix::fs::MetadataExt;
    let bundle = executable.ancestors().find(|path| path.extension().is_some_and(|ext| ext == "app"))
        .ok_or("update_disk_unavailable")?;
    let device = std::fs::symlink_metadata(bundle).map_err(|_| "update_disk_unavailable")?.dev();
    [Some(temporary), bundle.parent()].into_iter().flatten()
        .find(|path| path.metadata().is_ok_and(|metadata| metadata.dev() == device))
        .map(Path::to_path_buf).ok_or_else(|| "update_disk_unavailable".into())
}

/// Only a feed transport or parse failure counts as being offline. Cancellations
/// and configuration problems keep their meaning and never unlock the fallback.
pub(super) fn offline_check_failure(code: &str) -> bool { code == "update_check_failed" }

fn revalidate(app: &tauri::AppHandle, approved: &Value) -> Result<(), String> {
    let current = tauri::async_runtime::block_on(async {
        let updater = configured(app)?;
        match cancellable(updater.check(), &app.state::<NativeUpdater>().cancel, "update_check_failed").await {
            Ok(current) => Ok(current.as_ref().map(artifact)),
            // A feed that cannot be reached does not withdraw an already staged
            // package: the exact bytes are signature-verified again before the
            // replacement, so the comparison falls back to the staged artifact.
            Err(code) if offline_check_failure(&code) => Ok(Some(staged_artifact(app)?)),
            Err(code) => Err(code),
        }
    })?;
    require_same_artifact(approved, current.as_ref())
}

/// Feed unavailable at install time: an already staged package may still install
/// offline, but only when it is the exact artifact the panel approved. Nothing
/// here skips verification — the install itself re-verifies every byte.
fn offline_install_fallback(app: &tauri::AppHandle, approved: &Value,
                            feed_error: &str) -> Result<Value, String> {
    if !offline_check_failure(feed_error) { return Err(feed_error.into()); }
    let updater = app.state::<NativeUpdater>();
    let staged = updater.staged.lock().unwrap().clone().ok_or_else(|| feed_error.to_string())?;
    require_same_artifact(approved, Some(&artifact(&staged.0)))?;
    updater.set(json!({"state":"ready","version":staged.0.version,
        "notes":staged.0.body.clone().unwrap_or_default()}));
    Ok(snapshot(app))
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
            if next.revision == current.revision && next != *current {
                return Err("update_authorization_changed".into());
            }
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

pub(super) fn open_bounded_file(path: &Path, limit: u64) -> Result<(File, u64), String> {
    let before = std::fs::symlink_metadata(path).map_err(|_| "update_cache_unavailable")?;
    if !before.is_file() || !single_link(&before) { return Err("update_cache_unavailable".into()); }
    if before.len() > limit { return Err("update_size_limit".into()); }
    let mut options = File::options();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
        options.custom_flags(FILE_FLAG_OPEN_REPARSE_POINT);
    }
    let file = options.open(path).map_err(|_| "update_cache_unavailable")?;
    let opened = file.metadata().map_err(|_| "update_cache_unavailable")?;
    if !opened.is_file() || !single_link(&opened) || !same_identity(&before, &opened)
        || opened.len() != before.len()
    {
        return Err("update_cache_unavailable".into());
    }
    Ok((file, opened.len()))
}

#[cfg(unix)]
struct PackageMapping {
    address: std::ptr::NonNull<u8>,
    len: usize,
}

#[cfg(unix)]
impl Drop for PackageMapping {
    fn drop(&mut self) {
        // SAFETY: This instance owns the mapping established for exactly this range.
        unsafe { libc::munmap(self.address.as_ptr().cast(), self.len) };
    }
}

#[cfg(unix)]
fn install_snapshot(mut source: File, size: u64, cache: &Path) -> Result<impl AsRef<[u8]>, String> {
    use std::os::{fd::AsRawFd, unix::fs::MetadataExt};

    if size > MAX_PACKAGE as u64 { return Err("update_size_limit".into()); }
    let cache_error = |_| "update_cache_unavailable";
    let mut copy = tempfile::tempfile_in(cache).map_err(cache_error)?;
    // Check that the tempfile fallback also successfully unlinked the file.
    if copy.metadata().map_err(cache_error)?.nlink() != 0 {
        return Err("update_cache_unavailable".into());
    }
    let copied = std::io::copy(&mut (&mut source).take(size), &mut copy).map_err(cache_error)?;
    if copied != size || source.read(&mut [0u8; 1]).map_err(cache_error)? != 0 {
        return Err("update_cache_unavailable".into());
    }
    if size == 0 { return Ok(InstallSnapshot(None)); }
    // Surface delayed allocation/write errors before exposing the mapped slice.
    copy.sync_all().map_err(cache_error)?;
    let len = size as usize;
    // SAFETY: The unlinked file has exactly `len` bytes, the offset is aligned,
    // and the mapping is read-only. The sole writable handle is dropped below.
    let address = unsafe {
        libc::mmap(std::ptr::null_mut(), len, libc::PROT_READ, libc::MAP_PRIVATE, copy.as_raw_fd(), 0)
    };
    if address == libc::MAP_FAILED { return Err("update_cache_unavailable".into()); }
    let Some(address_ptr) = std::ptr::NonNull::new(address.cast()) else {
        // SAFETY: mmap succeeded, but Rust slices require a non-null pointer.
        unsafe { libc::munmap(address, len) };
        return Err("update_cache_unavailable".into());
    };
    drop(copy);
    Ok(InstallSnapshot(Some(PackageMapping { address: address_ptr, len })))
}

#[cfg(unix)]
struct InstallSnapshot(Option<PackageMapping>);

#[cfg(unix)]
impl AsRef<[u8]> for InstallSnapshot {
    fn as_ref(&self) -> &[u8] {
        match &self.0 {
            Some(mapping) => {
                // SAFETY: The private read-only mapping remains valid while borrowed.
                // The bytes have no writable handle and len is bounded by MAX_PACKAGE.
                unsafe { std::slice::from_raw_parts(mapping.address.as_ptr(), mapping.len) }
            }
            None => &[],
        }
    }
}

#[cfg(not(unix))]
fn install_snapshot(mut file: File, size: u64, _cache: &Path) -> Result<impl AsRef<[u8]>, String> {
    // Keep the verified bytes independent of the cache file for the whole install.
    let mut bytes = vec![0; size as usize];
    file.read_exact(&mut bytes).map_err(|_| "update_cache_unavailable")?;
    if file.read(&mut [0u8; 1]).map_err(|_| "update_cache_unavailable")? != 0 {
        return Err("update_cache_unavailable".into());
    }
    Ok(bytes)
}

fn verify_cached_package(update: &tauri_plugin_updater::Update, path: &Path) -> Result<(), String> {
    let (file, size) = open_bounded_file(path, MAX_PACKAGE as u64)?;
    super::updater_signature::verify_stream(
        file, size, option_env!("RSI_UPDATER_PUBLIC_KEY").unwrap_or(""), &update.signature, &update.version,
    ).map_err(str::to_string)
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
        let (file, size) = open_bounded_file(&path, MAX_PACKAGE as u64).unwrap();
        assert_eq!(install_snapshot(file, size, folder.path()).unwrap().as_ref(), b"signed fixture");
        #[cfg(unix)] {
            let link = folder.path().join("link");
            std::os::unix::fs::symlink(&path, &link).unwrap();
            assert_eq!(open_bounded_file(&link, MAX_PACKAGE as u64).unwrap_err(), "update_cache_unavailable");
            std::fs::remove_file(&link).unwrap();
            std::fs::hard_link(&path, &link).unwrap();
            assert_eq!(open_bounded_file(&path, MAX_PACKAGE as u64).unwrap_err(), "update_cache_unavailable");
            std::fs::remove_file(&link).unwrap();
        }
        File::options().write(true).open(&path).unwrap().set_len(MAX_PACKAGE as u64 + 1).unwrap();
        assert_eq!(open_bounded_file(&path, MAX_PACKAGE as u64).unwrap_err(), "update_size_limit");
    }

    #[cfg(unix)]
    #[test]
    fn part_identity_reads_only_bounded_regular_single_link_files() {
        let folder = tempfile::tempdir().unwrap();
        let path = part_meta_path(folder.path());
        let regular = folder.path().join("regular");
        let identity = part_identity("1.2.4", "https://example.test/package");
        assert_eq!(read_part_identity(&path).unwrap(), None);
        write_part_identity(&path, &identity).unwrap();
        assert_eq!(read_part_identity(&path).unwrap(), Some(identity.clone()));
        std::fs::rename(&path, &regular).unwrap();

        std::os::unix::fs::symlink(&regular, &path).unwrap();
        assert_eq!(read_part_identity(&path).unwrap_err(), "update_cache_unavailable");
        std::fs::remove_file(&path).unwrap();
        std::fs::hard_link(&regular, &path).unwrap();
        assert_eq!(read_part_identity(&path).unwrap_err(), "update_cache_unavailable");
        std::fs::remove_file(&path).unwrap();
        assert_eq!(std::fs::read_to_string(&regular).unwrap(), identity);

        std::fs::write(&path, vec![b'x'; MAX_PART_IDENTITY as usize + 1]).unwrap();
        assert_eq!(read_part_identity(&path).unwrap_err(), "update_cache_unavailable");
        std::fs::remove_file(&path).unwrap();
        assert!(std::process::Command::new("mkfifo").arg(&path).status().unwrap().success());
        let (send, receive) = std::sync::mpsc::channel();
        std::thread::spawn(move || send.send(read_part_identity(&path)).ok());
        let result = receive.recv_timeout(Duration::from_secs(2)).expect("FIFO check must not block");
        assert_eq!(result.unwrap_err(), "update_cache_unavailable");
    }

    #[cfg(unix)]
    #[test]
    fn install_snapshot_is_independent_of_the_staged_path_and_inode() {
        let staged = tempfile::tempdir().unwrap();
        let cache = tempfile::tempdir().unwrap();
        let path = staged.path().join("package");
        let original = staged.path().join("original");
        let package = vec![0x5a; 96 * 1024];
        std::fs::write(&path, &package).unwrap();
        let (file, size) = open_bounded_file(&path, MAX_PACKAGE as u64).unwrap();

        std::fs::rename(&path, &original).unwrap();
        std::fs::write(&path, vec![0x61; package.len()]).unwrap();
        let snapshot = install_snapshot(file, size, cache.path()).unwrap();
        assert_eq!(snapshot.as_ref(), package.as_slice());
        assert!(cache.path().read_dir().unwrap().next().is_none());

        std::fs::write(&original, b"truncated").unwrap();
        std::fs::remove_file(&path).unwrap();
        assert_eq!(snapshot.as_ref(), package.as_slice());
        drop(snapshot);
        assert!(cache.path().read_dir().unwrap().next().is_none());
    }

    #[cfg(unix)]
    #[test]
    fn install_snapshot_cleans_up_after_a_length_mismatch() {
        let staged = tempfile::tempdir().unwrap();
        let cache = tempfile::tempdir().unwrap();
        let path = staged.path().join("package");
        for changed_len in [1, 20] {
            std::fs::write(&path, b"signed fixture").unwrap();
            let (file, size) = open_bounded_file(&path, MAX_PACKAGE as u64).unwrap();
            File::options().write(true).open(&path).unwrap().set_len(changed_len).unwrap();

            assert_eq!(install_snapshot(file, size, cache.path()).err().unwrap(), "update_cache_unavailable");
            assert!(cache.path().read_dir().unwrap().next().is_none());
            assert_eq!(path.metadata().unwrap().len(), changed_len);
        }
    }

    #[cfg(unix)]
    #[test]
    fn empty_install_snapshot_needs_no_mapping() {
        let staged = tempfile::tempfile().unwrap();
        let cache = tempfile::tempdir().unwrap();
        assert!(install_snapshot(staged, 0, cache.path()).unwrap().as_ref().is_empty());
        assert!(cache.path().read_dir().unwrap().next().is_none());
    }

    #[cfg(unix)]
    #[test]
    fn preflight_charges_staged_size_on_the_shared_volume() {
        let folder = tempfile::tempdir().unwrap();
        let path = cached_path(folder.path());
        let package = 1024 * 1024;
        File::create(&path).unwrap().set_len(package).unwrap();
        let (_, staged_size) = open_bounded_file(&path, MAX_PACKAGE as u64).unwrap();
        let extraction = folder.path().join("extract");
        std::fs::create_dir(&extraction).unwrap();
        let check = |free, snapshot_bytes| {
            require_space(&[&extraction], staged_size * EXTRACTION_SPACE_MULTIPLIER, |path| {
                space_after_snapshot(folder.path(), snapshot_bytes, path, |_| Ok(free))
            })
        };
        assert!(check(3 * package + 1, staged_size).is_ok());
        assert_eq!(check(3 * package - 1, staged_size), Err("update_disk_space".into()));
        assert!(check(2 * package + 1, 0).is_ok());
        assert_eq!(check(2 * package - 1, 0), Err("update_disk_space".into()));
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
        .configure_client(|client| client.https_only(true))
        .build()
        .map_err(|_| "invalid_update_configuration".into())
}

pub fn snapshot(app: &tauri::AppHandle) -> Value {
    let updater = app.state::<NativeUpdater>();
    let mut state = updater.state.lock().unwrap().clone();
    describe_snapshot(&mut state, &app.package_info().version.to_string(), host_platform(), host_arch());
    // The panel binds every install to this revision, so it must always be present.
    let authorization = updater.authorization();
    state["authorization_revision"] = json!(authorization.revision);
    state["authorization"] = json!({
        "policy": authorization.policy,
        "scope": authorization.scope,
        "window": authorization.window,
    });
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

fn native_download_client() -> reqwest::ClientBuilder {
    reqwest::Client::builder()
        .no_proxy()
        .https_only(true)
        .redirect(update_redirect_policy())
        .connect_timeout(UPDATE_CONNECT_TIMEOUT)
}

/// Streams the package into `staged-update.part`, resuming from the bytes that a
/// previous run (or a cancelled run) left behind.
async fn transfer(updater: &NativeUpdater, update: &tauri_plugin_updater::Update, part: &Path,
                  offset: u64, total: &mut Option<u64>, version: &str, notes: &str) -> Result<u64, String> {
    let client = native_download_client().build().map_err(|_| "update_check_failed")?;
    transfer_from(updater, || client.get(update.download_url.clone()).headers(update.headers.clone()),
        part, offset, total, version, notes).await
}

async fn transfer_from(updater: &NativeUpdater, request: impl Fn() -> reqwest::RequestBuilder,
                       part: &Path, mut offset: u64, total: &mut Option<u64>,
                       version: &str, notes: &str) -> Result<u64, String> {
    let started = Instant::now();
    let mut restarted = false;
    let announced = *total;
    loop {
        if updater.cancel.load(Ordering::SeqCst) { return Err("update_cancelled".into()); }
        let request_started = Instant::now();
        let mut request = request();
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
            if offset > 0 && total.is_none_or(|size| size == offset) { return Ok(offset); }
            discard_part(part);
            return Err("update_range_rejected".into());
        }
        if !(200..300).contains(&status) { return Err("update_download_or_signature_failed".into()); }
        let segment = response.headers().get(reqwest::header::CONTENT_RANGE)
            .and_then(|value| value.to_str().ok())
            .and_then(parse_segment_range);
        let content_range = segment.as_ref().map(|(range, _)| range);
        match segment_action(offset, status, content_range) {
            SegmentAction::Rejected => {
                // 416 is handled above; any other unexpected success status means
                // the body cannot be trusted as a segment for this package.
                discard_part(part);
                return Err("update_download_or_signature_failed".into());
            }
            SegmentAction::Restart if offset > 0 => {
                // The server ignored our offset (200) or answered for another start.
                discard_part(part);
                if restarted { return Err("update_range_rejected".into()); }
                restarted = true;
                offset = 0;
                *total = announced;
                let cache = part.parent().ok_or("update_cache_unavailable")?;
                require_space(&[cache], announced.unwrap_or(MAX_PACKAGE as u64), |path| available_space(path))?;
                continue;
            }
            SegmentAction::Restart if status != 200 => return Err("update_range_rejected".into()),
            SegmentAction::Append | SegmentAction::Restart => {}
        }
        let (declared, segment_end) = if let Some((range, end)) = segment.filter(|_| status == 206) {
            if response.content_length().is_some_and(|length| length != end - offset) {
                return Err("update_range_rejected".into());
            }
            (range.total, Some(end))
        } else {
            (response.content_length(), response.content_length())
        };
        if [declared, segment_end].into_iter().flatten().any(|value| value > MAX_PACKAGE as u64) {
            return Err("update_size_limit".into());
        }
        if total.zip(declared).is_some_and(|(previous, current)| previous != current)
            || total.zip(segment_end).is_some_and(|(total, end)| end > total)
        {
            return Err("update_range_rejected".into());
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
            if segment_end.or(*total).is_some_and(|end| next > end) {
                return Err("update_range_rejected".into());
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
        if count == offset || segment_end.is_some_and(|end| count != end) {
            return Err("update_download_incomplete".into());
        }
        if total.is_some_and(|value| count < value) || (status == 206 && total.is_none()) {
            offset = count;
            continue;
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
    let stored = read_part_identity(&meta)?;
    if !identity_matches(stored.as_deref(), version, url) {
        // Never append a new release to a segment left by another artifact.
        discard_part(&part);
        write_part_identity(&meta, &part_identity(version, url))?;
    }
    if announced > MAX_PACKAGE as u64 { return Err("update_size_limit".into()); }
    let mut total = update.raw_json.get("size").and_then(Value::as_u64).filter(|size| *size > 0);
    let persisted = part_bytes(&part);
    let offset = resume_offset(persisted, total);
    if offset == 0 && persisted > 0 {
        discard_part(&part);
    }
    // Reserve the remaining download on this volume. Installation checks its
    // own volumes against the verified staged file's length.
    require_space(&[cache], announced - offset, |path| available_space(path))?;
    let result = async {
        let count = if total == Some(offset) { offset } else {
            transfer(updater, update, &part, offset, &mut total, version, notes).await?
        };
        updater.set(json!({"state":"verifying","stage":"verifying","version":version,"notes":notes,
            "bytes":count,"total":total,"bytes_per_second":Value::Null}));
        verify_cached_package(update, &part)
    }.await;
    if let Err(error) = result {
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
        if verify_cached_package(&update, &cached).is_ok() {
            *updater.staged.lock().unwrap() = Some((update.clone(), cached));
            updater.set(json!({"state":"ready","version":version,"notes":notes}));
            return Ok(snapshot(&app));
        }
        discard_part(&cached);
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
    // A fresh feed check keeps its veto, but being offline does not: an already
    // staged package installs from the cache and is signature-verified again
    // before any replacement. The staged artifact must still be the exact one
    // this install approved.
    let eligibility = match check_in_operation(&app, false).await {
        Ok(snapshot) => snapshot,
        Err(code) if offline_check_failure(&code) => offline_install_fallback(&app, &approved, &code)?,
        Err(code) => return Err(code),
    };
    if eligibility["state"] != "ready" { return Err("update_not_downloaded".into()); }
    require_same_artifact(&approved, Some(&staged_artifact(&app)?))?;
    updater.require_install_permit(&request)?;
    tauri::async_runtime::spawn_blocking(move || {
        operation.run_install(|| install_in_operation(app, permit, automatic, approved, current, target))
    }).await.map_err(|_| "update_worker_failed".to_string())?
}

fn cancel_update_lease(lease: &Value, mut cancel: impl FnMut(Value) -> bool,
                       mut pause: impl FnMut()) -> Result<(), String> {
    for attempt in 0..3 {
        if cancel(json!({"action":"cancel","lease":lease})) { return Ok(()); }
        if attempt < 2 { pause(); }
    }
    Err("update_barrier_release_failed".into())
}

const CANCEL_REQUEST_TIMEOUT: Duration = Duration::from_secs(2);
const CANCEL_RETRY_DELAY: Duration = Duration::from_millis(200);

fn cancel_owned_barrier(connection: &Connection, body: Value, timeout: Duration) -> bool {
    connection.owned && call_with_session(connection, "desktop-update", Some(body), Some(timeout))
        .is_ok_and(|(reply, _)| reply.status == 200
            && serde_json::from_str::<Value>(&reply.body).is_ok_and(|value| value.get("barrier") == Some(&Value::Null)))
}

fn cancel_local_barrier(runtime: &RuntimeState, body: Value) -> bool {
    let connection = runtime.local.lock().unwrap().clone();
    connection.as_ref().is_some_and(|connection| cancel_owned_barrier(connection, body, CANCEL_REQUEST_TIMEOUT))
}

pub(super) fn reconcile_startup_barrier(connection: &Connection, version: &Value, deadline: Instant) -> Result<(), String> {
    let mut remaining = startup_budget(deadline)?;
    let status = loop {
        let timeout = remaining.min(CANCEL_REQUEST_TIMEOUT);
        let error = match call_with_session(connection, "desktop-update", Some(json!({"action":"status"})), Some(timeout)) {
            Ok((reply, _)) if reply.status == 200 => break reply,
            Ok((reply, _)) if matches!(reply.status, 502 | 503 | 504) => "Cannot inspect update admission".into(),
            Ok(_) => return Err("Cannot inspect update admission".into()),
            Err(error) => error,
        };
        std::thread::sleep(CANCEL_RETRY_DELAY.min(deadline.saturating_duration_since(Instant::now())));
        remaining = startup_budget(deadline).map_err(|_| error)?;
    };
    let status: Value = serde_json::from_str(&status.body).map_err(|_| "Invalid update admission")?;
    let barrier = status.get("barrier").ok_or("Invalid update admission")?;
    if barrier.is_null() { return Ok(()); }
    let lease = barrier.get("lease").filter(|lease| lease.as_str().is_some_and(|lease| !lease.is_empty()))
        .ok_or("Invalid update admission")?;
    let target = barrier.get("version").filter(|target| target.is_string())
        .ok_or("Invalid update admission")?;
    if target == version {
        let (complete, _) = call_with_session(connection, "desktop-update",
            Some(json!({"action":"complete","lease":lease})), Some(startup_budget(deadline)?))?;
        if complete.status != 200 { return Err("Cannot complete desktop update".into()); }
        return Ok(());
    }
    cancel_startup_barrier(connection, lease, deadline)
}

fn cancel_startup_barrier(connection: &Connection, lease: &Value, deadline: Instant) -> Result<(), String> {
    let body = json!({"action":"cancel","lease":lease});
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() { return Err("update_barrier_release_failed".into()); }
        if cancel_owned_barrier(connection, body.clone(), remaining.min(CANCEL_REQUEST_TIMEOUT)) { return Ok(()); }
        std::thread::sleep(CANCEL_RETRY_DELAY.min(deadline.saturating_duration_since(Instant::now())));
    }
}

fn retry_live_update_barrier(runtime: &RuntimeState, lease: &Value, generation: u64, deadline: Instant) -> bool {
    let body = json!({"action":"cancel","lease":lease});
    while Instant::now() < deadline && !QUITTING.load(Ordering::SeqCst) {
        if let Ok(_local_start) = runtime.local_start_gate.try_lock() {
            if QUITTING.load(Ordering::SeqCst) || runtime.recovering.load(Ordering::SeqCst)
                || runtime.generation.load(Ordering::SeqCst) != generation
            {
                return false;
            }
            let timeout = deadline.saturating_duration_since(Instant::now()).min(CANCEL_REQUEST_TIMEOUT);
            let connection = runtime.local.lock().unwrap().clone();
            if !timeout.is_zero() && connection.as_ref().is_some_and(|connection| {
                cancel_owned_barrier(connection, body.clone(), timeout)
            }) {
                return true;
            }
        }
        std::thread::sleep(Duration::from_secs(2).min(deadline.saturating_duration_since(Instant::now())));
    }
    false
}

fn try_release_live_barrier(runtime: &RuntimeState, lease: &Value) -> Result<(), String> {
    cancel_update_lease(lease, |body| cancel_local_barrier(runtime, body),
        || std::thread::sleep(CANCEL_RETRY_DELAY))
}

fn release_update_barrier(app: &tauri::AppHandle, runtime: &RuntimeState, lease: &Value) -> Result<(), String> {
    let result = try_release_live_barrier(runtime, lease);
    if result.is_err() {
        // Keep the live engine available. Its successor reconciles admission at startup
        // if the owned runtime generation changes before cancellation succeeds.
        let handle = app.clone();
        let generation = runtime.generation.load(Ordering::SeqCst);
        let lease = lease.clone();
        std::thread::spawn(move || {
            let runtime = handle.state::<RuntimeState>();
            let deadline = Instant::now() + Duration::from_secs(3600);
            retry_live_update_barrier(&runtime, &lease, generation, deadline);
        });
    }
    result
}

fn check_renewed_lease(lease: &mut Value, ready: &Value, version: &str) -> Result<(), String> {
    let returned = &ready["barrier"]["lease"];
    if returned == lease { return Ok(()); }
    // prepare can acquire a fresh barrier after expiry. Cancel that lease on
    // exit, while still requiring a new install attempt before any replacement.
    if returned.as_str().is_some_and(|value| !value.is_empty())
        && ready["barrier"]["version"] == version
        && ready["barrier"]["phase"] == "draining"
    {
        *lease = returned.clone();
    }
    Err("update_lease_expired".into())
}

#[cfg(test)]
mod lease_tests {
    use super::*;

    #[test]
    fn renewed_update_lease_is_reclaimed_after_old_lease_expires() {
        let mut lease = json!("previous-lease");
        let foreign = json!({"barrier":{"lease":"foreign-lease","version":"2.0.0","phase":"draining"}});
        assert_eq!(check_renewed_lease(&mut lease, &foreign, "1.2.3"), Err("update_lease_expired".into()));
        assert_eq!(lease, "previous-lease");

        let renewed = json!({"barrier":{"lease":"fresh-lease","version":"1.2.3","phase":"draining"}});
        assert_eq!(check_renewed_lease(&mut lease, &renewed, "1.2.3"), Err("update_lease_expired".into()));
        let mut attempts = 0;
        let mut pauses = 0;
        let result = cancel_update_lease(&lease, |body| {
            assert_eq!(body, json!({"action":"cancel","lease":"fresh-lease"}));
            attempts += 1;
            attempts == 2
        }, || pauses += 1);
        assert_eq!((result, attempts, pauses), (Ok(()), 2, 1));
    }

    #[cfg(target_os = "macos")]
    #[test]
    #[ignore = "requires an isolated Python desktop engine on the macOS runner"]
    fn update_cancel_removes_persisted_lease_after_service_recovery() {
        let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let output = Command::new("python3").current_dir(&repo)
            .args(["-B", "-c", "from adaptive_rsi import __version__; print(__version__)"])
            .output().unwrap();
        assert!(output.status.success());
        let version = String::from_utf8(output.stdout).unwrap();
        let launch = |root: &Path, mode: &str| {
            let child = Command::new("python3").current_dir(&repo)
                .args(["-B", "-m", "desktop.scripts.update_cancel_fixture"]).arg(root).arg(mode)
                .env("ADAPTIVE_RSI_DESKTOP_OWNER_PID", std::process::id().to_string())
                .stdin(Stdio::piped()).stdout(Stdio::piped()).spawn().unwrap();
            handshake_local_runtime(PendingChild::new(child), version.trim(),
                Instant::now() + Duration::from_secs(20)).unwrap()
        };
        let action = |connection: &Connection, body: Value| {
            let name = body["action"].as_str().unwrap().to_owned();
            let (reply, _) = call_with_session(connection, "desktop-update", Some(body),
                Some(Duration::from_secs(3))).unwrap();
            assert_eq!(reply.status, 200, "{name}: {}", reply.body);
            serde_json::from_str::<Value>(&reply.body).unwrap()
        };
        let stored_barrier = |root: &Path| {
            let output = Command::new("python3").args(["-B", "-c", r#"
import json, sqlite3, sys
from pathlib import Path
with sqlite3.connect(Path(sys.argv[1]).as_uri() + '?mode=ro', uri=True) as db:
    row = db.execute('SELECT value FROM settings WHERE key=?', ('desktop-update-barrier',)).fetchone()
    assert db.execute('SELECT count(*) FROM calls').fetchone() == (0,)
    print(json.dumps([json.loads(row[0])] if row else []))
"#]).arg(root.join("state.sqlite3")).output().unwrap();
            assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
            serde_json::from_slice::<Value>(&output.stdout).unwrap()
        };

        // The still-running engine must retain admission while every cancel fails.
        let live = tempfile::tempdir().unwrap();
        let (mut live_child, connection, live_pid, _lines) = launch(live.path(), "live");
        let prepared = action(&connection, json!({"action":"prepare","version":"99.99.99"}));
        let lease = &prepared["barrier"]["lease"];
        assert!(lease.is_string());
        assert_eq!(stored_barrier(live.path())[0]["lease"], *lease);
        let state = RuntimeState::default();
        *state.local.lock().unwrap() = Some(connection.clone());
        let generation = state.generation.load(Ordering::SeqCst);
        let outage = live.path().join("fixture-unavailable");
        fs::write(&outage, b"unavailable").unwrap();
        assert_eq!(try_release_live_barrier(&state, lease), Err("update_barrier_release_failed".into()));
        assert!(!retry_live_update_barrier(&state, lease, generation, Instant::now() + Duration::from_secs(1)));
        assert_eq!(stored_barrier(live.path())[0]["lease"], *lease);
        assert_eq!(fs::read_to_string(live.path().join("fixture-outages")).unwrap().lines().count(), 4);
        std::thread::scope(|scope| {
            scope.spawn(|| {
                let deadline = Instant::now() + Duration::from_secs(4);
                while fs::read_to_string(live.path().join("fixture-outages")).unwrap().lines().count() < 5 {
                    assert!(Instant::now() < deadline, "background cancellation did not retry");
                    std::thread::sleep(Duration::from_millis(20));
                }
                fs::remove_file(&outage).unwrap();
            });
            assert!(retry_live_update_barrier(&state, lease, generation, Instant::now() + Duration::from_secs(6)));
        });
        assert!(stored_barrier(live.path()) == json!([]));
        let status = action(&connection, json!({"action":"status"}));
        assert_eq!(status["ready"], true);
        assert!(status["barrier"].is_null());
        let (health, _) = call_with_session(&connection, "health", None, Some(Duration::from_secs(3))).unwrap();
        assert_eq!(serde_json::from_str::<Value>(&health.body).unwrap()["pid"], live_pid);
        stop_child(live_child.get_mut(), Duration::from_secs(5));

        // A failed install after engine shutdown leaves a committed barrier for the successor.
        let restarted = tempfile::tempdir().unwrap();
        let (mut child, old, old_pid, _old_lines) = launch(restarted.path(), "live");
        let prepared = action(&old, json!({"action":"prepare","version":"99.99.99"}));
        let lease = &prepared["barrier"]["lease"];
        assert!(lease.is_string());
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            let status = action(&old, json!({"action":"status"}));
            if status["ready"] == true { break; }
            assert!(Instant::now() < deadline, "fixture did not drain: {}", status["reasons"]);
            std::thread::sleep(Duration::from_millis(100));
        }
        let committed = action(&old, json!({"action":"commit","lease":lease}));
        assert_eq!(committed["barrier"]["phase"], "installing");
        let state = RuntimeState::default();
        publish_local_runtime(&state, child.take(), old, true);
        let mut exiting = shutdown_runtime(&state, Duration::from_secs(5)).unwrap();
        assert!(wait_for_child(&mut exiting, Duration::from_secs(5)));
        assert!(exiting.wait().unwrap().success());
        assert!(state.recovering.load(Ordering::SeqCst));
        assert_eq!(stored_barrier(restarted.path())[0]["lease"], *lease);
        let (mut successor, recovered, new_pid, _new_lines) = launch(restarted.path(), "restart");
        assert_ne!(old_pid, new_pid);
        publish_local_runtime(&state, successor.take(), recovered.clone(), true);
        assert!(!state.recovering.load(Ordering::SeqCst));
        assert_eq!(fs::read_to_string(restarted.path().join("fixture-outages")).unwrap(), "status\ncancel\n");
        let status = action(&recovered, json!({"action":"status"}));
        assert_eq!(status["ready"], true);
        assert!(status["barrier"].is_null());
        assert!(stored_barrier(restarted.path()) == json!([]));
        let mut successor = shutdown_runtime(&state, Duration::from_secs(5)).unwrap();
        assert!(wait_for_child(&mut successor, Duration::from_secs(5)));
    }

    #[test]
    fn persistent_update_cancel_failure_is_bounded() {
        let mut attempts = 0;
        let mut pauses = 0;
        let result = cancel_update_lease(&json!("owned-lease"), |body| {
            assert_eq!(body, json!({"action":"cancel","lease":"owned-lease"}));
            attempts += 1;
            false
        }, || pauses += 1);
        assert_eq!((result, attempts, pauses), (Err("update_barrier_release_failed".into()), 3, 2));
    }
}

fn install_in_operation(app: tauri::AppHandle, permit: InstallPermit, automatic: bool, approved: Value,
                        current_version: String, target_version: String) -> Result<(), String> {
    let updater = app.state::<NativeUpdater>();
    let runtime = app.state::<RuntimeState>();
    // Keep the local engine stable from the drain snapshot through replacement and recovery.
    let _local_start = runtime.local_start_gate.try_lock().map_err(|_| "update_busy")?;
    if runtime.recovering.load(Ordering::SeqCst) {
        return Err("update_runtime_recovery_failed".into());
    }
    let local = runtime.local.lock().unwrap().clone();
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
    let mut _replacement = None;
    let result: Result<(), String> = (|| {
        boundary()?;
        let staged = updater.staged.lock().unwrap().clone().ok_or("update_not_downloaded")?;
        require_same_artifact(&approved, Some(&artifact(&staged.0)))?;
        let version = &staged.0.version;
        let cache = staged.1.parent().ok_or("update_cache_unavailable")?;
        let (file, size) = open_bounded_file(&staged.1, MAX_PACKAGE as u64)?;
        preflight_install_space(&app, cache, size, true)?;
        let snapshot = install_snapshot(file, size, cache)?;
        let bytes = snapshot.as_ref();
        let verified = staged.0.verify(bytes).is_ok();
        let transaction = update_receipt::Transaction {
            operation_id: update_receipt::operation_id(),
            from_version: current_version.clone(),
            to_version: version.clone(),
            artifact_sha256: update_receipt::sha256_hex(bytes),
            signature_sha256: update_receipt::sha256_hex(staged.0.signature.as_bytes()),
            signature_verified: verified,
            artifact_url: staged.0.download_url.as_str().to_string(),
            authorization_revision: revision,
            install_window: window.clone(),
            platform: host_platform().into(),
            arch: host_arch().into(),
        };
        if !verified {
            *updater.staged.lock().unwrap() = None;
            discard_part(&staged.1);
            let receipt = update_receipt::prepare_app(&app)?;
            update_receipt::begin_pending(&receipt, &transaction)?;
            update_receipt::transition(&receipt, "failed", Some("update_download_or_signature_failed"), None)?;
            return Err("update_download_or_signature_failed".into());
        }
        let runtime = app.state::<RuntimeState>();
        let owned = local.as_ref().filter(|connection| connection.owned);
        let enter_replacement = || -> Result<MutexGuard<'static, ()>, String> {
            let gate = REPLACEMENT_GATE.try_lock().map_err(|_| "update_busy")?;
            if QUITTING.load(Ordering::SeqCst) || updater.cancel.load(Ordering::SeqCst) {
                return Err("update_wait_cancelled".into());
            }
            boundary()?;
            Ok(gate)
        };
        if let Some(connection) = owned {
            let reply = call(connection, "desktop-update", Some(json!({"action":"prepare","version":version})))?;
            if reply.status != 200 { return Err("update_drain_failed".into()); }
            let mut ready: Value = serde_json::from_str(&reply.body).map_err(|_| "update_drain_failed")?;
            lease = ready["barrier"]["lease"].clone();
            if !lease.is_string() { return Err("update_drain_failed".into()); }
            let deadline = std::time::Instant::now() + Duration::from_secs(3600);
            while ready["ready"] != true {
                boundary()?;
                updater.set(json!({"state":"waiting_idle","version":version,"reasons":ready["reasons"]}));
                if QUITTING.load(Ordering::SeqCst) || updater.cancel.load(Ordering::SeqCst) || Instant::now() >= deadline {
                    return Err("update_wait_cancelled".into());
                }
                std::thread::sleep(Duration::from_secs(1));
                let reply = call(connection, "desktop-update", Some(json!({"action":"prepare","version":version,"lease":lease})))?;
                if reply.status != 200 { return Err("update_drain_failed".into()); }
                ready = serde_json::from_str(&reply.body).map_err(|_| "update_drain_failed")?;
                check_renewed_lease(&mut lease, &ready, version)?;
            }
            boundary()?;
            revalidate(&app, &approved)?;
            _replacement = Some(enter_replacement()?);
            let reply = call(connection, "desktop-update", Some(json!({"action":"commit","lease":lease})))?;
            if reply.status != 200 { return Err("update_drain_failed".into()); }
        } else {
            revalidate(&app, &approved)?;
            _replacement = Some(enter_replacement()?);
        }
        if updater.cancel.load(Ordering::SeqCst) { return Err("update_wait_cancelled".into()); }
        boundary()?;
        preflight_install_space(&app, cache, size, false)?;
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
        let receipt = update_receipt::prepare_app(&app)?;
        update_receipt::begin_pending(&receipt, &transaction)?;
        let installation = (|| {
            // Keep authorization stable from the final check through replacement.
            let authorization = updater.authorization.lock().unwrap();
            require_window(&window)?;
            install_permit(&authorization, &request)?;
            if updater.cancel.load(Ordering::SeqCst) { return Err("update_wait_cancelled".into()); }
            staged.0.install(bytes).map_err(|_| "update_install_failed".to_string())
        })();
        drop(snapshot);
        if let Err(error) = installation {
            let _ = update_receipt::transition(&receipt, "failed", Some(&error), None);
            return Err(error);
        }
        app.restart();
    })();
    if let Err(error) = result {
        let runtime_state = app.state::<RuntimeState>();
        let error = if engine_stopped {
            match bootstrap(&app) {
                Ok(()) => error,
                Err(reason) => {
                    *runtime_state.error.lock().unwrap() = reason;
                    "update_runtime_recovery_failed".into()
                }
            }
        } else if !runtime_state.recovering.load(Ordering::SeqCst) && lease.is_string() {
            release_update_barrier(&app, &runtime_state, &lease).err().unwrap_or(error)
        } else {
            error
        };
        if runtime_state.recovering.load(Ordering::SeqCst) {
            // The old service may still be exiting after the grace period. Watch for
            // its real exit, then restore a fresh owned runtime instead of exposing
            // a stale connection or reopening the update barrier.
            let handle = app.clone();
            let generation = runtime_state.generation.load(Ordering::SeqCst);
            std::thread::spawn(move || {
                let state = handle.state::<RuntimeState>();
                let deadline = Instant::now() + Duration::from_secs(180);
                while Instant::now() < deadline && !QUITTING.load(Ordering::SeqCst) {
                    if let Ok(_local_start) = state.local_start_gate.try_lock() {
                        if !state.recovering.load(Ordering::SeqCst)
                            || state.generation.load(Ordering::SeqCst) != generation
                        {
                            return;
                        }
                        if let Ok(_replacement) = REPLACEMENT_GATE.try_lock() {
                            if QUITTING.load(Ordering::SeqCst) { return; }
                            if reap_update_runtime(&state) {
                                match bootstrap(&handle) {
                                    Ok(()) => return,
                                    Err(error) => *state.error.lock().unwrap() = error,
                                }
                            }
                        }
                    }
                    std::thread::sleep(Duration::from_millis(500));
                }
            });
        }
        let staged = updater.staged.lock().unwrap();
        if let Some((update, _)) = staged.as_ref() {
            updater.set(json!({"state":"ready","version":update.version,"notes":update.body,"error_code":error}));
        } else {
            updater.set(json!({"state":"error","error_code":error}));
        }
        return Err(error);
    }
    Ok(())
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

#[cfg(all(test, target_os = "macos"))]
mod response_policy_tests {
    use super::*;

    #[test]
    #[ignore = "requires the GitHub macOS HTTPS fixture"]
    fn native_download_rejects_normal_missing_package_before_staging() {
        let fixture = PathBuf::from(std::env::var_os("RSI_DMG_FIXTURE").unwrap());
        let url = std::env::var("RSI_NATIVE_MISSING_PACKAGE_URL").unwrap();
        let part = fixture.join(PART_NAME);
        let certificate = reqwest::Certificate::from_pem(&fs::read(fixture.join("ca.pem")).unwrap()).unwrap();
        let client = native_download_client().add_root_certificate(certificate).build().unwrap();
        let updater = NativeUpdater::default();
        let before = updater.state.lock().unwrap().clone();
        let mut total = None;
        let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        let result = runtime.block_on(async {
            tokio::time::timeout(Duration::from_secs(10), transfer_from(&updater, || client.get(&url),
                &part, 0, &mut total, "99.99.99", "")).await
        }).expect("the native downloader waited for the missing package body");
        assert_eq!(result, Err("update_download_or_signature_failed".into()));
        assert_eq!(total, None);
        assert_eq!(*updater.state.lock().unwrap(), before);
        assert!(!part.exists());
    }
}
