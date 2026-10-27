use base64::{engine::general_purpose::STANDARD, Engine};
use minisign_verify::{PublicKey, Signature};
use std::{env, fs};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = env::args().collect();
    if args.len() != 4 {
        return Err("usage: verify_updater BUNDLE SIGNATURE VERSION".into());
    }
    let encoded_key = env::var("RSI_UPDATER_PUBLIC_KEY")?;
    let key = PublicKey::decode(&String::from_utf8(STANDARD.decode(encoded_key.trim())?)?)?;
    let encoded_signature = fs::read_to_string(&args[2])?;
    let signature = Signature::decode(&String::from_utf8(STANDARD.decode(encoded_signature.trim())?)?)?;
    let bytes = fs::read(&args[1])?;
    key.verify(&bytes, &signature, true)?;
    if !signature.trusted_comment().split_whitespace().any(|part| part == format!("version:{}", args[3])) {
        return Err("signed version does not match the release".into());
    }
    println!("Updater signature and version verified");
    Ok(())
}
