#!/usr/bin/env bash
# 免 clone 的 Linux 服务器安装：从官方 Release 拉取校验过的安装包，
# 布局与内置自更新兼容（installations/<版本> + current 软链）。
set -euo pipefail

# Both installers share the offline-install guard and readiness probe.
check_install_idle() {
  local services jobs
  if ! services=$(systemctl --user list-units --all --plain --no-legend --no-pager adaptive-rsi.service) ||
     ! jobs=$(systemctl --user list-jobs --no-legend --no-pager adaptive-rsi.service); then
    echo '无法确认 systemd 用户服务状态，已停止安装。 Cannot verify systemd user service state; installation stopped.' >&2
    return 1
  fi
  # Any loaded state other than inactive/failed, or a queued job, blocks installation.
  if ! awk '$1 == "adaptive-rsi.service" && $3 != "inactive" && $3 != "failed" { busy = 1 } END { exit busy }' <<< "$services" ||
     ! awk '$2 == "adaptive-rsi.service" { busy = 1 } END { exit busy }' <<< "$jobs"; then
    echo '服务仍在运行或切换状态。请暂停调度、等待任务结束并停止服务后再安装；也可使用内置更新。 Drain work and stop the service before installing, or use the built-in updater.' >&2
    return 1
  fi
  python3 - "$1" <<'PYTHON'
import fcntl
import sys
from pathlib import Path

state = Path(sys.argv[1]).expanduser()
try:
    with (state / "runtime.lock").open("rb") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except FileNotFoundError:
    pass
except OSError:
    sys.exit("无法独占运行目录，已停止安装。 Runtime state is in use or unavailable; installation stopped.")
if any((state / "installations" / name).exists() for name in ("active.json", "restart.json")):
    sys.exit("状态目录已有自更新接管或恢复计划，请使用内置更新或先完成恢复。 The built-in updater owns this installation or has a pending recovery plan.")
PYTHON
}

installed_build() {
  python3 -I -c 'import runpy, sys; print(runpy.run_path(sys.argv[1])["BUILD_ID"])' "$1/adaptive_rsi/build.py"
}

# Also used by the clone installer. Keep this in the bootstrap so the pipe
# installation can check a release that predates this reporting code.
start_and_report() {
  python3 - "$1" "$2" <<'PYTHON'
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
expected_build = sys.argv[2]
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


def health(state, group, remaining):
    # Bound the entire probe, including a peer that slowly trickles response bytes.
    signal.setitimer(signal.ITIMER_REAL, min(2, remaining))
    try:
        session = json.loads((state / "server.json").read_text())
        pid = session["pid"]
        if type(pid) is not int or pid <= 0 or not group.startswith("/") or group == "/":
            return False
        # The clone unit supervises a child; the release unit normally serves directly.
        groups = [line.split(":", 2)[-1] for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()]
        if not any(path == group or path.startswith(group + "/") for path in groups):
            return False
        endpoint = urlsplit(session.get("local_origin") or session["origin"])
        if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or not endpoint.port:
            return False
        connection = http.client.HTTPConnection("127.0.0.1", endpoint.port, timeout=min(2, remaining))
        try:
            connection.request("GET", "/api/health", headers={"Cookie": session["cookie"] + "=" + session["token"]})
            response = connection.getresponse()
            if response.status != 200:
                return False
            payload = json.loads(response.read(65536))
            return payload.get("pid") == pid and payload.get("build") == expected_build
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
    detail = command("systemctl", "--user", "show", unit, "--property=ActiveState", "--property=MainPID", "--property=ControlGroup",
                     timeout=min(2, remaining)) or ""
    fields = dict(line.split("=", 1) for line in detail.splitlines() if "=" in line)
    current = fields.get("ActiveState")
    status = current if current in {"active", "activating", "deactivating", "inactive", "failed", "reloading"} else "unknown"
    remaining = deadline - time.monotonic()
    if remaining > 0 and status == "active" and health(
        state_directory(fields.get("MainPID", "")), fields.get("ControlGroup", ""), remaining
    ):
        ready = True
        break
    if status == "failed":
        break
    time.sleep(min(0.5, max(0, deadline - time.monotonic())))
say(f"systemd 用户服务状态：{status}", f"systemd user service state: {status}")
if ready:
    say("健康检查已就绪。请在服务日志中查看认证地址。", "Health check ready. See the service journal for the authenticated URL.")
else:
    say("健康检查未就绪：未确认本次安装的构建正在运行。请检查 systemctl --user status adaptive-rsi 和 journalctl --user -u adaptive-rsi。",
        "Health check not ready: the installed build was not confirmed running. Inspect systemctl --user status adaptive-rsi and journalctl --user -u adaptive-rsi.")
    sys.exit(1)
PYTHON
}

case "${1:-}" in
  --check-user-service) check_install_idle "$2"; exit ;;
  --installed-build) installed_build "$2"; exit ;;
  --start-user-service) start_and_report "$2" "$3"; exit ;;
esac

repo="Haoog/Adaptive-RSI"
base="${XDG_DATA_HOME:-$HOME/.local/share}/adaptive-rsi-installations"
state="$HOME/.local/share/adaptive-rsi"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

for tool in curl python3 unzip systemctl shasum; do
  command -v "$tool" >/dev/null || { echo "缺少工具：$tool" >&2; exit 1; }
done
if [[ "$(uname -s)" != Linux ]]; then
  echo '此安装器用于 Linux 服务器。macOS 请使用 scripts/start.py。' >&2
  exit 1
fi

check_install_idle "$state"

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
  build=$(installed_build "$target")
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
  build=$(installed_build "$work/unpacked/adaptive-rsi")
  check_install_idle "$state"
  mkdir -p "$base"
  mv "$work/unpacked/adaptive-rsi" "$target"
fi

check_install_idle "$state"
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
start_and_report "$state" "$build"
echo "后续自更新在 $base 内进行，systemd 单元无需改动。"
