use base64::{engine::general_purpose::STANDARD, Engine};
use minisign_verify::{PublicKey, Signature};
use std::io::Read;

pub(super) const MAX_PACKAGE: usize = 512 * 1024 * 1024;

pub(super) fn verify_stream(
    reader: impl Read,
    size: u64,
    encoded_key: &str,
    encoded_signature: &str,
    version: &str,
) -> Result<(), &'static str> {
    if size > MAX_PACKAGE as u64 {
        return Err("update_size_limit");
    }
    let invalid = "update_download_or_signature_failed";
    let decode = |value: &str| -> Result<String, &'static str> {
        let bytes = STANDARD.decode(value).map_err(|_| invalid)?;
        String::from_utf8(bytes).map_err(|_| invalid)
    };
    let key = PublicKey::decode(&decode(encoded_key)?).map_err(|_| invalid)?;
    let signature = Signature::decode(&decode(encoded_signature)?).map_err(|_| invalid)?;
    let mut verifier = key.verify_stream(&signature).map_err(|_| invalid)?;
    let mut reader = reader.take(size + 1);
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let count = reader.read(&mut buffer).map_err(|_| "update_cache_unavailable")?;
        if count == 0 {
            break;
        }
        verifier.update(&buffer[..count]);
    }
    if reader.limit() != 1 {
        return Err("update_cache_unavailable");
    }
    verifier.finalize().map_err(|_| invalid)?;
    // Tauri signs the version as a tab-separated field in the trusted comment.
    let signed = signature.trusted_comment().split('\t').find_map(|field| field.strip_prefix("version:"));
    if signed.map(|version| version.trim_start_matches('v')) != Some(version) {
        return Err(invalid);
    }
    Ok(())
}
