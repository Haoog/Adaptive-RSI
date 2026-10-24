#!/bin/zsh
cd "$(dirname "$0")"
if ! python3 scripts/start.py; then
  echo "\n启动失败：请确认已安装 python3。可在终端手动运行 python3 scripts/start.py 查看具体错误。"
  read -r "?按回车键关闭…"
fi
