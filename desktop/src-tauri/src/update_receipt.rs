use super::*;
use native_update::{same_identity, single_link};
use std::fs::File;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use tauri::Manager;

/// Persisted transaction record for the desktop upgrade that is being installed.
/// It is written before replacement and reconciled by the restarted process, so
/// the exported receipt always describes what actually happened on disk.
pub(super) const RECEIPT_NAME: &str = "update-receipt.json";
pub(super) const RECEIPT_TMP_NAME: &str = "update-receipt.json.tmp";
pub(super) const RECEIPT_VERSION: u64 = 1;
pub(super) const MAX_RECEIPT: u64 = 64 * 1024;

pub(super) fn receipt_path(dir: &Path) -> PathBuf {
    dir.join(RECEIPT_NAME)
}

/// Creates the receipt directory with owner-only access. A planted symlink or a
/// pre-existing world-readable directory is refused or tightened before any
/// receipt is written into it.
pub(super) fn ensure_private_dir(dir: &Path) -> Result<(), String> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt};
        match std::fs::symlink_metadata(dir) {
            Ok(metadata) if metadata.is_dir() => {
                if metadata.mode() & 0o077 != 0 {
                    std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))
                        .map_err(|_| "update_receipt_unavailable")?;
                }
            }
            Ok(_) => return Err("update_receipt_unavailable".into()),
            Err(_) => {
                let mut builder = std::fs::DirBuilder::new();
                builder.recursive(true).mode(0o700);
                builder.create(dir).map_err(|_| "update_receipt_unavailable")?;
            }
        }
        let metadata = std::fs::symlink_metadata(dir).map_err(|_| "update_receipt_unavailable")?;
        if !metadata.is_dir() || metadata.mode() & 0o077 != 0 {
            return Err("update_receipt_unavailable".into());
        }
        return Ok(());
    }
    #[cfg(not(unix))]
    {
        std::fs::create_dir_all(dir).map_err(|_| "update_receipt_unavailable".into())
    }
}

fn hex_digest(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn valid_receipt(value: &Value) -> bool {
    let bounded = |text: Option<&str>, limit: usize| {
        text.is_some_and(|text| !text.is_empty() && text.len() <= limit)
    };
    value.is_object()
        && value["receipt_version"].as_u64() == Some(RECEIPT_VERSION)
        && bounded(value["operation_id"].as_str(), 128)
        && value["operation_id"].as_str().is_some_and(|id| {
            id.bytes().all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
        })
        && matches!(value["state"].as_str(), Some("pending" | "succeeded" | "failed"))
        && value["from_version"].as_str().and_then(super::parse_version).is_some()
        && value["to_version"].as_str().and_then(super::parse_version).is_some()
        && value["artifact_sha256"].as_str().is_some_and(hex_digest)
        && value["signature_sha256"].as_str().is_some_and(hex_digest)
        && value["signature_verified"].as_bool().is_some()
        && value["created_at"].is_u64()
        && value["updated_at"].is_u64()
        && value.get("error_code").is_some()
        && (value["error_code"].is_null() || bounded(value["error_code"].as_str(), 128))
}

pub(super) fn read_receipt(path: &Path) -> Result<Value, String> {
    let metadata = match std::fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(_) => return Err("update_receipt_missing".into()),
    };
    // A planted symlink or hard link is not evidence of a real transaction.
    if !metadata.is_file() || !single_link(&metadata) || metadata.len() == 0 || metadata.len() > MAX_RECEIPT {
        return Err("update_receipt_invalid".into());
    }
    let file = File::open(path).map_err(|_| "update_receipt_invalid")?;
    let opened = file.metadata().map_err(|_| "update_receipt_invalid")?;
    if !same_identity(&opened, &metadata) || opened.len() > MAX_RECEIPT {
        return Err("update_receipt_invalid".into());
    }
    let mut text = String::new();
    file.take(MAX_RECEIPT + 1).read_to_string(&mut text)
        .map_err(|_| "update_receipt_invalid")?;
    if text.len() as u64 > MAX_RECEIPT {
        return Err("update_receipt_invalid".into());
    }
    let value: Value = serde_json::from_str(&text).map_err(|_| "update_receipt_invalid")?;
    if !valid_receipt(&value) {
        return Err("update_receipt_invalid".into());
    }
    Ok(value)
}

#[cfg(unix)]
fn write_private_file(path: &Path, bytes: &[u8]) -> Result<(), String> {
    use std::os::unix::fs::OpenOptionsExt;
    let _ = std::fs::remove_file(path);
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .map_err(|_| "update_receipt_unavailable")?;
    let opened = file.metadata().map_err(|_| "update_receipt_unavailable")?;
    let linked = std::fs::symlink_metadata(path).map_err(|_| "update_receipt_unavailable")?;
    // Never write through a link planted between the removal and the open.
    if !opened.is_file() || !single_link(&opened) || !single_link(&linked)
        || !same_identity(&opened, &linked)
    {
        drop(file);
        let _ = std::fs::remove_file(path);
        return Err("update_receipt_unavailable".into());
    }
    file.write_all(bytes).map_err(|_| "update_receipt_unavailable")?;
    file.sync_all().map_err(|_| "update_receipt_unavailable")?;
    Ok(())
}

#[cfg(not(unix))]
fn write_private_file(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let _ = std::fs::remove_file(path);
    std::fs::write(path, bytes).map_err(|_| "update_receipt_unavailable".into())
}

pub(super) fn write_receipt(path: &Path, value: &Value) -> Result<(), String> {
    let bytes = serde_json::to_vec_pretty(value).map_err(|_| "update_receipt_unavailable")?;
    if bytes.len() as u64 > MAX_RECEIPT {
        return Err("update_receipt_invalid".into());
    }
    let temp = path.with_file_name(RECEIPT_TMP_NAME);
    if let Err(error) = write_private_file(&temp, &bytes) {
        let _ = std::fs::remove_file(&temp);
        return Err(error);
    }
    // rename replaces the directory entry, so a planted symlink or hard link at
    // the final name is replaced without its target ever being written.
    if std::fs::rename(&temp, path).is_err() {
        let _ = std::fs::remove_file(&temp);
        return Err("update_receipt_unavailable".into());
    }
    if let Some(parent) = path.parent() {
        if let Ok(directory) = File::open(parent) {
            let _ = directory.sync_all();
        }
    }
    Ok(())
}

pub(super) struct Transaction {
    pub operation_id: String,
    pub from_version: String,
    pub to_version: String,
    pub artifact_sha256: String,
    pub signature_sha256: String,
    /// Result of verifying the exact bytes that were about to be installed.
    pub signature_verified: bool,
    pub artifact_url: String,
    pub authorization_revision: u64,
    pub install_window: String,
    pub platform: String,
    pub arch: String,
}

fn now_secs() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs())
        .unwrap_or(0)
}

pub(super) fn begin_pending(path: &Path, transaction: &Transaction) -> Result<Value, String> {
    let now = now_secs();
    let record = json!({
        "receipt_version": RECEIPT_VERSION,
        "operation_id": transaction.operation_id,
        "from_version": transaction.from_version,
        "to_version": transaction.to_version,
        "artifact_sha256": transaction.artifact_sha256,
        "signature_sha256": transaction.signature_sha256,
        "signature_verified": transaction.signature_verified,
        "artifact_url": transaction.artifact_url,
        "authorization_revision": transaction.authorization_revision,
        "install_window": transaction.install_window,
        "platform": transaction.platform,
        "arch": transaction.arch,
        "state": "pending",
        "error_code": Value::Null,
        "created_at": now,
        "updated_at": now,
    });
    write_receipt(path, &record)?;
    Ok(record)
}

/// A pending record can only move forward once; a finished transaction stays as
/// evidence and is never rewritten into another outcome.
pub(super) fn transition(path: &Path, state: &str, error_code: Option<&str>,
                         current_version: Option<&str>) -> Result<Value, String> {
    match state {
        "succeeded" | "failed" => {}
        _ => return Err("update_receipt_invalid".into()),
    }
    // Validate the requested outcome before reading, so a caller error is not
    // reported as a state conflict with an existing document.
    let error_code = match (state, error_code) {
        ("succeeded", _) => {
            if current_version.is_none() { return Err("update_receipt_invalid".into()); }
            None
        }
        (_, Some(code)) if !code.is_empty() && code.len() <= 128 => Some(code),
        _ => return Err("update_receipt_invalid".into()),
    };
    let mut record = read_receipt(path)?;
    if record["state"] != "pending" {
        return Err("update_receipt_state".into());
    }
    if state == "succeeded" {
        let current = current_version.unwrap_or_default();
        if record["to_version"].as_str() != Some(current) {
            return Err("update_receipt_state".into());
        }
        record["error_code"] = Value::Null;
    } else {
        record["error_code"] = json!(error_code.unwrap_or_default());
    }
    record["state"] = json!(state);
    record["updated_at"] = json!(now_secs());
    write_receipt(path, &record)?;
    Ok(record)
}

/// Only a restarted process that reports the expected version and a healthy
/// local runtime may turn the pending record into a success.
pub(super) fn reconcile(path: &Path, current_version: &str, healthy: bool) -> Result<Value, String> {
    let record = read_receipt(path)?;
    if record["state"] != "pending" {
        return Ok(record);
    }
    if record["signature_verified"] != true {
        return transition(path, "failed", Some("update_download_or_signature_failed"), None);
    }
    if record["to_version"].as_str() != Some(current_version) {
        return transition(path, "failed", Some("update_version_mismatch"), None);
    }
    if !healthy {
        return transition(path, "failed", Some("update_health_failed"), None);
    }
    transition(path, "succeeded", None, Some(current_version))
}

pub(super) fn app_receipt_path(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let directory = app.path().app_data_dir().map_err(|_| "update_receipt_unavailable")?.join("update-receipts");
    Ok(receipt_path(&directory))
}

pub(super) fn prepare_app(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let path = app_receipt_path(app)?;
    if let Some(directory) = path.parent() {
        ensure_private_dir(directory)?;
    }
    Ok(path)
}

pub(super) fn reconcile_app(app: &tauri::AppHandle, current_version: &str, healthy: bool) -> Result<Value, String> {
    let path = app_receipt_path(app)?;
    if std::fs::symlink_metadata(&path).is_err() {
        return Ok(Value::Null);
    }
    reconcile(&path, current_version, healthy)
}

/// Reads the persisted transaction for export. The UI never assembles a receipt
/// from polled state, so the exported file cannot claim an install that was not
/// recorded on disk.
pub(super) fn export(app: &tauri::AppHandle) -> Value {
    match app_receipt_path(app).and_then(|path| read_receipt(&path)) {
        Ok(receipt) => json!({"available": true, "receipt": receipt}),
        Err(error) => json!({"available": false, "error": error}),
    }
}

pub(super) fn operation_id() -> String {
    let mut bytes = [0u8; 16];
    #[cfg(unix)]
    {
        if let Ok(mut source) = File::open("/dev/urandom") {
            if source.read_exact(&mut bytes).is_ok() {
                return bytes.iter().map(|byte| format!("{byte:02x}")).collect();
            }
        }
    }
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_nanos())
        .unwrap_or(0);
    format!("{now:032x}{:08x}", std::process::id())
}

pub(super) fn sha256_hex(bytes: &[u8]) -> String {
    fn compress(hash: &mut [u32; 8], block: &[u8], constants: &[u32; 64]) {
        let mut schedule = [0u32; 64];
        for (index, word) in block.chunks_exact(4).enumerate() {
            schedule[index] = u32::from_be_bytes([word[0], word[1], word[2], word[3]]);
        }
        for index in 16..64 {
            let previous = schedule[index - 15];
            let recent = schedule[index - 2];
            let s0 = previous.rotate_right(7) ^ previous.rotate_right(18) ^ (previous >> 3);
            let s1 = recent.rotate_right(17) ^ recent.rotate_right(19) ^ (recent >> 10);
            schedule[index] = schedule[index - 16]
                .wrapping_add(s0)
                .wrapping_add(schedule[index - 7])
                .wrapping_add(s1);
        }
        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut h] = *hash;
        for index in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let choose = (e & f) ^ ((!e) & g);
            let temp1 = h
                .wrapping_add(s1)
                .wrapping_add(choose)
                .wrapping_add(constants[index])
                .wrapping_add(schedule[index]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let majority = (a & b) ^ (a & c) ^ (b & c);
            let temp2 = s0.wrapping_add(majority);
            h = g;
            g = f;
            f = e;
            e = d.wrapping_add(temp1);
            d = c;
            c = b;
            b = a;
            a = temp1.wrapping_add(temp2);
        }
        for (slot, value) in hash.iter_mut().zip([a, b, c, d, e, f, g, h]) {
            *slot = slot.wrapping_add(value);
        }
    }
    const K: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
        0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
        0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
        0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
        0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
    ];
    let mut hash: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
    ];
    // Stream the complete blocks, then pad only the tail. A 512 MiB package must
    // not be copied just to hash it.
    let mut blocks = bytes.chunks_exact(64);
    for block in &mut blocks {
        compress(&mut hash, block, &K);
    }
    let remainder = blocks.remainder();
    let mut tail = [0u8; 128];
    tail[..remainder.len()].copy_from_slice(remainder);
    tail[remainder.len()] = 0x80;
    let tail_len = if remainder.len() + 9 <= 64 { 64 } else { 128 };
    tail[tail_len - 8..tail_len].copy_from_slice(&(bytes.len() as u64).wrapping_mul(8).to_be_bytes());
    for block in tail[..tail_len].chunks_exact(64) {
        compress(&mut hash, block, &K);
    }
    hash.iter().map(|word| format!("{word:08x}")).collect()
}

#[cfg(test)]
mod receipt_tests {
    use super::*;

    fn transaction(operation_id: &str, to: &str) -> Transaction {
        Transaction {
            operation_id: operation_id.into(),
            from_version: "1.2.3".into(),
            to_version: to.into(),
            artifact_sha256: sha256_hex(b"signed artifact"),
            signature_sha256: sha256_hex(b"signature"),
            signature_verified: true,
            artifact_url: "https://example.test/adaptive-rsi-1.2.4.tar.gz".into(),
            authorization_revision: 7,
            install_window: "night".into(),
            platform: "macos".into(),
            arch: "aarch64".into(),
        }
    }

    #[cfg(unix)]
    fn mode_of(path: &Path) -> u32 {
        use std::os::unix::fs::PermissionsExt;
        std::fs::symlink_metadata(path).unwrap().permissions().mode() & 0o777
    }

    #[test]
    fn sha256_matches_published_vectors() {
        assert_eq!(sha256_hex(b""), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
        assert_eq!(sha256_hex(b"abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
        assert_eq!(
            sha256_hex(b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"),
            "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"
        );
        // The digest is over the exact installed bytes, so a one-byte change must differ.
        assert_ne!(sha256_hex(b"signed artifact"), sha256_hex(b"signed artifacT"));
        // Padding boundaries (one or two tail blocks) are the easy place to break.
        for (length, digest) in [
            (55, "9f4390f8d30c2dd92ec9f095b65e2b9ae9b0a925a5258e241c9f1e910f734318"),
            (56, "b35439a4ac6f0948b6d6f9e3c6af0f5f590ce20f1bde7090ef7970686ec6738a"),
            (63, "7d3e74a05d7db15bce4ad9ec0658ea98e3f06eeecf16b4c6fff2da457ddc2f34"),
            (64, "ffe054fe7ae0cb6dc65c3af9b61d5209f439851db43d0ba5997337df154668eb"),
            (119, "31eba51c313a5c08226adf18d4a359cfdfd8d2e816b13f4af952f7ea6584dcfb"),
            (120, "2f3d335432c70b580af0e8e1b3674a7c020d683aa5f73aaaedfdc55af904c21c"),
            (127, "c57e9278af78fa3cab38667bef4ce29d783787a2f731d4e12200270f0c32320a"),
            (128, "6836cf13bac400e9105071cd6af47084dfacad4e5e302c94bfed24e013afb73e"),
            (1000, "41edece42d63e8d9bf515a9ba6932e1c20cbc9f5a5d134645adb5db1b9737ea3"),
        ] {
            assert_eq!(sha256_hex(&vec![b'a'; length]), digest, "length {length}");
        }
    }

    #[test]
    fn operation_ids_are_distinct_and_bounded() {
        let first = operation_id();
        let second = operation_id();
        assert!(first != second, "{first} was reused");
        assert!(8 <= first.len() && first.len() <= 128, "{first}");
        assert!(first.bytes().all(|byte| byte.is_ascii_hexdigit()));
    }

    #[test]
    fn pending_receipt_persists_operation_identity_and_digests() {
        let folder = tempfile::tempdir().unwrap();
        ensure_private_dir(folder.path()).unwrap();
        let path = receipt_path(folder.path());
        assert_eq!(path.file_name().unwrap(), RECEIPT_NAME);
        assert!(!path.exists(), "a receipt must not exist before the install starts");
        let written = begin_pending(&path, &transaction("a1b2c3d4", "1.2.4")).unwrap();
        assert_eq!(written["state"], json!("pending"));
        assert_eq!(written["operation_id"], json!("a1b2c3d4"));
        assert_eq!(written["from_version"], json!("1.2.3"));
        assert_eq!(written["to_version"], json!("1.2.4"));
        assert_eq!(written["artifact_sha256"], json!(sha256_hex(b"signed artifact")));
        assert_eq!(written["signature_sha256"], json!(sha256_hex(b"signature")));
        assert_eq!(written["signature_verified"], json!(true));
        assert_eq!(written["authorization_revision"], json!(7));
        assert_eq!(written["install_window"], json!("night"));
        assert_eq!(written["receipt_version"], json!(RECEIPT_VERSION));
        assert!(written["created_at"].is_u64());
        assert_eq!(read_receipt(&path).unwrap(), written);
        // 导出必须来自磁盘上的真实记录，而不是某个进程内快照。
        assert_eq!(read_receipt(&path).unwrap()["operation_id"], json!("a1b2c3d4"));
        #[cfg(unix)]
        {
            assert_eq!(mode_of(&path), 0o600, "receipt must be private");
            assert_eq!(mode_of(folder.path()), 0o700, "receipt directory must be private");
        }
        assert!(!folder.path().join(RECEIPT_TMP_NAME).exists(), "atomic write must not leave a temp file");
    }

    #[cfg(unix)]
    #[test]
    fn receipt_never_writes_or_reads_through_links() {
        let folder = tempfile::tempdir().unwrap();
        ensure_private_dir(folder.path()).unwrap();
        let outside = folder.path().join("outside");
        std::fs::write(&outside, b"outside").unwrap();
        let path = receipt_path(folder.path());
        std::os::unix::fs::symlink(&outside, &path).unwrap();
        // Reading a planted link must not follow it.
        assert_eq!(read_receipt(&path).unwrap_err(), "update_receipt_invalid");
        begin_pending(&path, &transaction("a1b2c3d4", "1.2.4")).unwrap();
        assert_eq!(std::fs::read(&outside).unwrap(), b"outside");
        assert!(!std::fs::symlink_metadata(&path).unwrap().file_type().is_symlink());
        assert_eq!(read_receipt(&path).unwrap()["operation_id"], json!("a1b2c3d4"));

        std::fs::remove_file(&path).unwrap();
        std::fs::hard_link(&outside, &path).unwrap();
        begin_pending(&path, &transaction("deadbeef", "1.2.4")).unwrap();
        assert_eq!(std::fs::read(&outside).unwrap(), b"outside");
        assert_eq!(read_receipt(&path).unwrap()["operation_id"], json!("deadbeef"));
    }

    #[test]
    fn crash_before_rename_keeps_the_previous_receipt_and_stale_temp_is_replaced() {
        let folder = tempfile::tempdir().unwrap();
        ensure_private_dir(folder.path()).unwrap();
        let path = receipt_path(folder.path());
        let first = begin_pending(&path, &transaction("11111111", "1.2.4")).unwrap();
        // A crash can leave a partially written temp file behind; it must never
        // be mistaken for the committed receipt.
        std::fs::write(folder.path().join(RECEIPT_TMP_NAME), b"{\"state\":\"succ").unwrap();
        assert_eq!(read_receipt(&path).unwrap(), first);
        let second = begin_pending(&path, &transaction("22222222", "1.2.5")).unwrap();
        assert_eq!(second["operation_id"], json!("22222222"));
        assert!(!folder.path().join(RECEIPT_TMP_NAME).exists());
        // An unwritable directory fails loudly and leaves the old document intact.
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(folder.path(), std::fs::Permissions::from_mode(0o500)).unwrap();
            let blocked = begin_pending(&path, &transaction("33333333", "1.2.6"));
            std::fs::set_permissions(folder.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
            assert_eq!(blocked.unwrap_err(), "update_receipt_unavailable");
            assert_eq!(read_receipt(&path).unwrap()["operation_id"], json!("22222222"));
        }
    }

    #[test]
    fn malformed_oversized_and_unknown_receipts_are_rejected() {
        let folder = tempfile::tempdir().unwrap();
        ensure_private_dir(folder.path()).unwrap();
        let path = receipt_path(folder.path());
        assert_eq!(read_receipt(&path).unwrap_err(), "update_receipt_missing");
        std::fs::write(&path, b"not json").unwrap();
        assert_eq!(read_receipt(&path).unwrap_err(), "update_receipt_invalid");
        std::fs::write(&path, vec![b'x'; MAX_RECEIPT as usize + 1]).unwrap();
        assert_eq!(read_receipt(&path).unwrap_err(), "update_receipt_invalid");
        let mut valid = begin_pending(&path, &transaction("a1b2c3d4", "1.2.4")).unwrap();
        for (field, value) in [("state", json!("applied")), ("receipt_version", json!(2)),
                               ("operation_id", json!("")), ("artifact_sha256", json!("zz")),
                               ("to_version", json!("1.2.4-beta"))] {
            let mut broken = valid.clone();
            broken[field] = value;
            write_receipt(&path, &broken).unwrap();
            assert_eq!(read_receipt(&path).unwrap_err(), "update_receipt_invalid", "{field}");
        }
        valid["operation_id"] = json!("a1b2c3d4");
        valid["state"] = json!("pending");
        valid["receipt_version"] = json!(1);
        valid["artifact_sha256"] = json!(sha256_hex(b"signed artifact"));
        valid["to_version"] = json!("1.2.4");
        write_receipt(&path, &valid).unwrap();
        assert_eq!(read_receipt(&path).unwrap()["state"], json!("pending"));
    }

    #[test]
    fn reconcile_marks_succeeded_only_for_the_matching_version_and_health() {
        let folder = tempfile::tempdir().unwrap();
        ensure_private_dir(folder.path()).unwrap();
        let path = receipt_path(folder.path());

        begin_pending(&path, &transaction("aaaaaaaa", "1.2.4")).unwrap();
        let mut unverified = transaction("cccccccc", "1.2.4");
        unverified.signature_verified = false;
        begin_pending(&path, &unverified).unwrap();
        assert_eq!(reconcile(&path, "1.2.4", true).unwrap()["state"], json!("failed"));
        begin_pending(&path, &transaction("aaaaaaaa", "1.2.4")).unwrap();
        // 旧版本重新启动：替换没有发生，不能记成成功。
        let mismatch = reconcile(&path, "1.2.3", true).unwrap();
        assert_eq!(mismatch["state"], json!("failed"));
        assert_eq!(mismatch["error_code"], json!("update_version_mismatch"));

        begin_pending(&path, &transaction("bbbbbbbb", "1.2.4")).unwrap();
        let unhealthy = reconcile(&path, "1.2.4", false).unwrap();
        assert_eq!(unhealthy["state"], json!("failed"));
        assert_eq!(unhealthy["error_code"], json!("update_health_failed"));

        begin_pending(&path, &transaction("cccccccc", "1.2.4")).unwrap();
        let succeeded = reconcile(&path, "1.2.4", true).unwrap();
        assert_eq!(succeeded["state"], json!("succeeded"));
        assert_eq!(succeeded["error_code"], Value::Null);
        assert!(succeeded["updated_at"].as_u64().unwrap() >= succeeded["created_at"].as_u64().unwrap());
        // A finished transaction is immutable evidence and is not rewritten on later starts.
        assert_eq!(reconcile(&path, "1.2.4", true).unwrap(), succeeded);
        assert_eq!(reconcile(&path, "9.9.9", false).unwrap(), succeeded);
    }

    #[test]
    fn failed_signature_check_is_recorded_and_never_becomes_success() {
        let folder = tempfile::tempdir().unwrap();
        ensure_private_dir(folder.path()).unwrap();
        let path = receipt_path(folder.path());
        let mut rejected = transaction("eeeeeeee", "1.2.4");
        rejected.signature_verified = false;
        let pending = begin_pending(&path, &rejected).unwrap();
        assert_eq!(pending["signature_verified"], json!(false));
        let failed = transition(&path, "failed", Some("update_download_or_signature_failed"), None).unwrap();
        assert_eq!(failed["state"], json!("failed"));
        assert_eq!(failed["error_code"], json!("update_download_or_signature_failed"));
        // A later healthy start of the same version must not launder the failure.
        assert_eq!(reconcile(&path, "1.2.4", true).unwrap()["state"], json!("failed"));
    }

    #[test]
    fn transitions_cannot_downgrade_a_finished_receipt() {
        let folder = tempfile::tempdir().unwrap();
        ensure_private_dir(folder.path()).unwrap();
        let path = receipt_path(folder.path());
        begin_pending(&path, &transaction("dddddddd", "1.2.4")).unwrap();
        let failed = transition(&path, "failed", Some("update_install_failed"), None).unwrap();
        assert_eq!(failed["state"], json!("failed"));
        assert_eq!(failed["error_code"], json!("update_install_failed"));
        assert_eq!(transition(&path, "succeeded", None, Some("1.2.4")).unwrap_err(), "update_receipt_state");
        assert_eq!(transition(&path, "failed", None, None).unwrap_err(), "update_receipt_invalid");
        assert_eq!(read_receipt(&path).unwrap()["state"], json!("failed"));
    }
}
