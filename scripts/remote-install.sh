#!/usr/bin/env bash
# 免 clone 的 Linux 服务器安装：从官方 Release 拉取校验过的安装包，
# 布局与内置自更新兼容（installations/<版本> + current 软链）。
set -euo pipefail

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
  echo "版本 $version 已安装于 $target；重复执行仅刷新软链。" >&2
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
systemctl --user daemon-reload
systemctl --user enable --now adaptive-rsi.service

echo '服务已启动。通过 journalctl --user -u adaptive-rsi 查看启动地址。'
echo "后续自更新在 $base 内进行，systemd 单元无需改动。"
echo "若需退出 SSH 后及重启后继续运行，请执行：sudo loginctl enable-linger $(id -un)"
