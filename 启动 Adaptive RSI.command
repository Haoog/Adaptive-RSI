#!/bin/zsh
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "\n启动失败：这台 Mac 没有安装 python3。请先安装（任选其一）：\n  1. 访问 https://www.python.org/downloads/ 下载安装\n  2. 已安装 Homebrew 时在终端运行 brew install python3\n安装完成后重新双击本文件。也可以使用桌面版：见 README 的「桌面版（推荐）」。"
  read -r "?按回车键关闭…"
  exit 1
fi
if ! python3 scripts/start.py; then
  echo "\n启动失败：请把上面的错误信息截图或复制，按 README「从源码启动」一节排查，或在 GitHub 提交 Issue。"
  read -r "?按回车键关闭…"
fi
