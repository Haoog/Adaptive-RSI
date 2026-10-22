#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != Linux ]]; then
  echo '此安装器用于 Linux 服务器。macOS 请使用 scripts/start.py。' >&2
  exit 1
fi
for tool in python3 git gh docker systemctl; do
  command -v "$tool" >/dev/null || { echo "缺少工具：$tool" >&2; exit 1; }
done
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
runtime_dir="${XDG_DATA_HOME:-$HOME/.local/share}/adaptive-rsi-runtime"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/adaptive-rsi"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
docker info >/dev/null
docker build -f "$repo_dir/deploy/Dockerfile.worker" -t adaptive-rsi-worker:local "$repo_dir"
python3 -m venv "$runtime_dir"
"$runtime_dir/bin/python" -m pip install "$repo_dir"
mkdir -p "$config_dir" "$unit_dir"
chmod 700 "$config_dir"
if [[ ! -f "$config_dir/server.env" ]]; then
  printf '%s\n' 'ADAPTIVE_RSI_PORT=6090' '# ADAPTIVE_RSI_PUBLIC_URL=https://rsi.example.com' > "$config_dir/server.env"
  chmod 600 "$config_dir/server.env"
fi
# systemd 的路径和变量有自己的转义规则，拒绝难以可靠表示的安装位置。
if [[ "$repo_dir$runtime_dir$config_dir" == *'"'* || "$repo_dir$runtime_dir$config_dir" == *'%'* || "$repo_dir$runtime_dir$config_dir" == *$'\n'* ]]; then
  echo '安装路径不能包含引号、百分号或换行。' >&2
  exit 1
fi
cat > "$unit_dir/adaptive-rsi.service" <<EOF
[Unit]
Description=Adaptive RSI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory="$repo_dir"
ExecStart="$runtime_dir/bin/python" "$repo_dir/scripts/service.py"
Environment="PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
EnvironmentFile="$config_dir/server.env"
Restart=on-failure
RestartSec=5
TimeoutStopSec=45
UMask=0077

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now adaptive-rsi.service
echo '服务已启动。通过 journalctl --user -u adaptive-rsi 查看启动地址。'
echo "若需退出 SSH 后及重启后继续运行，请执行：sudo loginctl enable-linger $(id -un)"
echo '默认访问：ssh -N -L 6090:127.0.0.1:6090 用户@服务器，然后打开日志中的认证地址。'
