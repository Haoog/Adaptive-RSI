#!/usr/bin/env bash
set -euo pipefail

fixture=$(mktemp -d "${RUNNER_TEMP:?}/rsi-legacy-dmg.XXXXXX")
volume_name="RSI DMG ${fixture##*.}"
export RSI_DMG_VOLUME="/Volumes/$volume_name"
cleanup() {
  if [ -d "$RSI_DMG_VOLUME" ]; then
    hdiutil detach "$RSI_DMG_VOLUME" || hdiutil detach -force "$RSI_DMG_VOLUME" || return 1
  fi
  rm -rf "$fixture"
}
trap 'cleanup || exit 1' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir "$fixture/payload"
printf 'Adaptive RSI DMG fixture\n' > "$fixture/payload/marker.txt"
hdiutil create -size 16m -fs HFS+ -format UDRW -srcfolder "$fixture/payload" \
  -volname "$volume_name" "$fixture/writable.dmg"
hdiutil convert "$fixture/writable.dmg" -format UDRO -o "$fixture/source.dmg"

cat > "$fixture/ca.cnf" <<'CONFIG'
[req]
distinguished_name = dn
x509_extensions = ca
[dn]
[ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
CONFIG
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -sha256 -subj '/CN=RSI DMG Fixture CA' \
  -config "$fixture/ca.cnf" -keyout "$fixture/ca.key" -out "$fixture/ca.pem"
openssl req -new -newkey rsa:2048 -nodes -subj '/CN=RSI DMG Fixture' \
  -keyout "$fixture/server.key" -out "$fixture/server.csr"
printf 'subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n' > "$fixture/server.ext"
openssl x509 -req -days 1 -sha256 -in "$fixture/server.csr" \
  -CA "$fixture/ca.pem" -CAkey "$fixture/ca.key" -CAcreateserial \
  -extfile "$fixture/server.ext" -out "$fixture/server.pem"

python3 -B desktop/scripts/legacy_dmg_https_fixture.py "$fixture" \
  cargo test --locked --manifest-path desktop/src-tauri/Cargo.toml --bin adaptive-rsi-desktop
