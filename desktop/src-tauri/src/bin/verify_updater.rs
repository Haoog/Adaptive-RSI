use base64::{engine::general_purpose::STANDARD, Engine};
use minisign_verify::{PublicKey, Signature};
use std::{env, fs};

#[path = "../updater_signature.rs"]
mod updater_signature;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = env::args().collect();
    let stream = args.get(4).is_some_and(|arg| arg == "--stream");
    let expected_args = if stream { 5 } else { 4 };
    if args.len() != expected_args {
        return Err("usage: verify_updater BUNDLE SIGNATURE VERSION [--stream]".into());
    }
    let encoded_key = env::var("RSI_UPDATER_PUBLIC_KEY")?;
    let encoded_signature = fs::read_to_string(&args[2])?;
    if stream {
        let file = fs::File::open(&args[1])?;
        let metadata = file.metadata()?;
        if !metadata.is_file() {
            return Err("update_cache_unavailable".into());
        }
        updater_signature::verify_stream(
            file, metadata.len(), encoded_key.trim(), encoded_signature.trim(), &args[3],
        )?;
        println!("Updater signature and version verified");
        return Ok(());
    }
    let key = PublicKey::decode(&String::from_utf8(STANDARD.decode(encoded_key.trim())?)?)?;
    let signature = Signature::decode(&String::from_utf8(STANDARD.decode(encoded_signature.trim())?)?)?;
    let bytes = fs::read(&args[1])?;
    key.verify(&bytes, &signature, true)?;
    if !signature.trusted_comment().split_whitespace().any(|part| part == format!("version:{}", args[3])) {
        return Err("signed version does not match the release".into());
    }
    println!("Updater signature and version verified");
    Ok(())
}
