#!/usr/bin/env bash
# 免 clone 的 Linux 服务器安装：从官方 Release 拉取校验过的安装包，
# 布局与内置自更新兼容（installations/<版本> + current 软链）。
set -euo pipefail

# Also used by the clone installer. Keep this in the bootstrap so the pipe
# installation can check a release that predates this reporting code.
start_and_report() {
  python3 - "$1" <<'PYTHON'
import http.client
import json
import os
import pwd
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

unit = "adaptive-rsi.service"
user = pwd.getpwuid(os.getuid()).pw_name
english = (os.getenv("LC_ALL") or os.getenv("LC_MESSAGES") or os.getenv("LANG") or "").startswith("en")


def say(zh, en):
    print(en if english else zh, flush=True)


def command(*args, timeout=3):
    try:
        return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, timeout=timeout, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def deadline_reached(*unused):
    raise TimeoutError()


def health(state, remaining):
    # Bound the entire probe, including a peer that slowly trickles response bytes.
    signal.setitimer(signal.ITIMER_REAL, min(2, remaining))
    try:
        session = json.loads((state / "server.json").read_text())
        endpoint = urlsplit(session.get("local_origin") or session["origin"])
        if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or not endpoint.port:
            return False
        connection = http.client.HTTPConnection("127.0.0.1", endpoint.port, timeout=min(2, remaining))
        try:
            connection.request("GET", "/api/health", headers={"Cookie": session["cookie"] + "=" + session["token"]})
            response = connection.getresponse()
            return response.status == 200 and json.loads(response.read(65536)).get("pid") == session["pid"]
        finally:
            connection.close()
    except (OSError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException):
        return False
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def state_directory(main_pid):
    # The clone unit permits an explicit state directory in server.env.
    if main_pid.isdecimal() and int(main_pid) > 0:
        try:
            prefix = b"ADAPTIVE_RSI_HOME="
            for value in Path(f"/proc/{main_pid}/environ").read_bytes().split(b"\0"):
                if value.startswith(prefix):
                    return Path(os.fsdecode(value[len(prefix):])).expanduser()
        except OSError:
            pass
    return Path(sys.argv[1]).expanduser()


started = command("systemctl", "--user", "daemon-reload", timeout=10) is not None and command(
    "systemctl", "--user", "enable", "--now", "--no-block", unit, timeout=10) is not None
if started:
    say("systemd：已提交用户服务启动请求。", "systemd: user service start requested.")
else:
    say("systemd：用户服务启动请求失败。", "systemd: user service start request failed.")

linger = command("loginctl", "show-user", user, "--property=Linger", "--value")
if linger == "yes":
    say("linger：已启用。", "linger: enabled.")
else:
    if linger == "no":
        say("linger：未启用。", "linger: disabled.")
    else:
        say("linger：无法查询。", "linger: status unavailable.")
    say(f"如需退出 SSH 后和重启后继续运行，请执行 sudo loginctl enable-linger {user}",
        f"To keep the service running after SSH logout and reboot, run sudo loginctl enable-linger {user}")

signal.signal(signal.SIGALRM, deadline_reached)
deadline = time.monotonic() + 20
status = "unknown"
ready = False
while started and time.monotonic() < deadline:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        break
    detail = command("systemctl", "--user", "show", unit, "--property=ActiveState", "--property=MainPID",
                     timeout=min(2, remaining)) or ""
    fields = dict(line.split("=", 1) for line in detail.splitlines() if "=" in line)
    current = fields.get("ActiveState")
    status = current if current in {"active", "activating", "deactivating", "inactive", "failed", "reloading"} else "unknown"
    remaining = deadline - time.monotonic()
    if remaining > 0 and status == "active" and health(state_directory(fields.get("MainPID", "")), remaining):
        ready = True
        break
    if status == "failed":
        break
    time.sleep(min(0.5, max(0, deadline - time.monotonic())))
say(f"systemd 用户服务状态：{status}", f"systemd user service state: {status}")
if ready:
    say("健康检查已就绪。请在服务日志中查看认证地址。", "Health check ready. See the service journal for the authenticated URL.")
else:
    say("健康检查未就绪。请检查 systemctl --user status adaptive-rsi 和 journalctl --user -u adaptive-rsi。",
        "Health check not ready. Inspect systemctl --user status adaptive-rsi and journalctl --user -u adaptive-rsi.")
    sys.exit(1)
PYTHON
}

if [[ "${1:-}" == --start-user-service ]]; then
  start_and_report "$2"
  exit
fi

repo="Haoog/Adaptive-RSI"
base="${XDG_DATA_HOME:-$HOME/.local/share}/adaptive-rsi-installations"
state="${XDG_DATA_HOME:-$HOME/.local/share}/adaptive-rsi"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

for tool in curl python3 unzip systemctl shasum; do
  command -v "$tool" >/dev/null || { echo "缺少工具：$tool" >&2; exit 1; }
done
if [[ "$(uname -s)" != Linux ]]; then
  echo '此安装器用于 Linux 服务器。macOS 请使用 scripts/start.py。' >&2
  exit 1
fi

release=$(curl -fsSL --retry 3 -H 'Accept: application/vnd.github+json' \
  "https://api.github.com/repos/${repo}/releases/latest")
version=$(printf '%s' "$release" | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"].lstrip("v"))')
if ! [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Release 版本号无法识别：$version" >&2
  exit 1
fi
if [[ "$base$state$unit_dir" == *'"'* || "$base$state$unit_dir" == *'%'* || "$base$state$unit_dir" == *$'\n'* ]]; then
  echo '安装路径不能包含引号、百分号或换行。' >&2
  exit 1
fi

target="$base/$version"
if [[ -e "$target" ]]; then
  echo "版本 $version 已安装于 ${target}；重复执行仅刷新软链。" >&2
else
  work=$(mktemp -d)
  trap 'rm -rf "$work"' EXIT
  asset="adaptive-rsi-${version}-plugin.zip"
  echo "下载 $asset …"
  # 与校验文件中记录的文件名保持一致，shasum -c 才能找到归档。
  curl -fsSL --retry 3 -o "$work/$asset" \
    "https://github.com/${repo}/releases/download/v${version}/${asset}"
  curl -fsSL --retry 3 -o "$work/${asset}.sha256" \
    "https://github.com/${repo}/releases/download/v${version}/${asset}.sha256"
  (cd "$work" && shasum -a 256 -c "${asset}.sha256")
  unzip -q "$work/$asset" -d "$work/unpacked"
  mkdir -p "$base"
  mv "$work/unpacked/adaptive-rsi" "$target"
fi

ln -sfn "$target" "$base/current"
mkdir -p "$state" "$unit_dir"
chmod 700 "$state"

cat > "$unit_dir/adaptive-rsi.service" <<EOF
[Unit]
Description=Adaptive RSI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory="$base/current"
ExecStart=python3 "$base/current/scripts/rsi" serve
Environment="PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
Restart=on-failure
RestartSec=5
TimeoutStopSec=45
UMask=0077

[Install]
WantedBy=default.target
EOF
start_and_report "$HOME/.local/share/adaptive-rsi"
echo "后续自更新在 $base 内进行，systemd 单元无需改动。"
