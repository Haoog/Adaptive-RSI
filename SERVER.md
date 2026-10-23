# 服务器部署

**简体中文** | [English](SERVER.en.md)

Linux 服务器负责代码、模型执行、检查和交付。个人电脑只打开浏览器，关机不影响服务器任务。macOS 仍可用 `python3 scripts/start.py` 本机运行。

手机使用：在已配置的 HTTPS 控制台地址后加 `/install`，或点击侧栏「手机安装」。详见 [手机安装与流程指南](EXPERIENCE.md)。

## 安装与访问

准备 Python 3.9+（含 venv）、Git、已登录的 GitHub CLI、可使用的 Docker Engine 和 systemd 用户服务。运行账号需要访问 Docker。请使用单独的服务账号；这是单用户维护工具。

```sh
git clone https://github.com/Haoog/Adaptive-RSI.git
cd Adaptive-RSI
gh auth login
bash scripts/install-server.sh
sudo loginctl enable-linger "$(id -un)"
```

安装器构建 `adaptive-rsi-worker:local`，建立独立 Python 环境，启动用户服务；已有配置不覆盖。`enable-linger` 使用户服务在退出 SSH 和服务器重启后继续运行。

默认仅监听服务器的 `127.0.0.1:6090`。在个人电脑执行：

```sh
ssh -N -L 6090:127.0.0.1:6090 用户@服务器
```

在服务器执行 `journalctl --user -u adaptive-rsi` 查看启动认证地址，再在个人电脑浏览器打开该地址。认证地址包含会话凭据，不要公开；普通不带凭据的地址会显示登录说明。

接入项目时填**服务器上的路径**，或使用 GitHub 链接。模型服务配置同样使用服务器的网络和认证环境；个人电脑上的登录不会自动上传。新项目保持暂停，选择模型、确认检查可执行和交付设置后启动。

## 模型与项目环境

- API：页面添加官方或兼容服务；Linux 默认以 0600 私有文件保存密钥，重启后继续使用。也可使用环境变量或仅会话密钥。
- OpenCode：镜像已包含 1.19.3。在页面选择 OpenCode 及实际供应商，使用其支持的官方登录或密钥连接；无需宿主 CLI，认证和模型目录均可使用执行镜像。也可使用 `opencode auth login` 或全局 `provider` 配置。
- Codex：镜像包含 0.163.0。在页面发起设备授权，在个人电脑浏览器打开官方地址并输入一次性验证码；凭据写入服务器服务账号的 `~/.codex/auth.json`。也可使用 Responses API 或兼容服务。操作及故障恢复见 [账号连接](docs/accounts.md)。
- 依赖：默认检查镜像是 `python:3.12-slim`，需事先 `docker pull python:3.12-slim`；本项目可把项目高级 JSON 设为 `{"sandbox":{"image":"adaptive-rsi-worker:local"}}`。其他语言请构建包含对应工具链和缓存依赖的可信镜像。需要下载依赖时在受信任的准备环境完成，检查容器不联网。

CLI 容器只挂载该次工作区与独立 HOME；不挂载控制器、其他项目、评估集、SSH/GitHub 凭据或 Docker socket。只读角色不能修改源代码，所有角色不能写 Git 元数据。CLI 为调用模型需要联网，独立检查和原生命令工具不联网。CLI 能访问执行所需的所选模型连接凭据；这不构成对任意恶意程序的机密计算隔离。

## HTTPS 域名访问

如需直接通过域名访问，在服务器配置反向代理（示例见 [Caddyfile](deploy/Caddyfile.example)），并在 `~/.config/adaptive-rsi/server.env` 添加：

```sh
ADAPTIVE_RSI_PUBLIC_URL=https://rsi.example.com
```

然后执行 `systemctl --user restart adaptive-rsi`，从新的启动认证地址登录。公网入口使用 HTTPS；应用仍只监听回环地址。Host、Origin、会话 Cookie 和 CSRF 检查继续有效，域名访问的 Cookie 带 `Secure`。不要将模型执行服务或 Docker API 暴露到公网。

## 管理与数据

```sh
systemctl --user status adaptive-rsi
systemctl --user restart adaptive-rsi
systemctl --user stop adaptive-rsi
journalctl --user -u adaptive-rsi -f
```

运行状态默认存于 `~/.local/share/adaptive-rsi`，项目克隆在旁边的 `adaptive-rsi-projects`。备份状态、私有凭据目录和项目；不要将它们提交到 GitHub。运行器更新由原有监督流程处理，用户服务跟踪更新后的进程；失败会尝试恢复旧版本。执行镜像更新需重新构建，已有任务保留自己的配置。

项目检查和 Issues 同步默认均每小时；只保留两种计划。发现、调用、任务时长和改动文件数不设额度，暂停与取消随时有效。日志大小、网络请求超时及容器执行边界属于实现保护，不作为发现或任务数量额度。
