# 构建

以下是当前仓库可用的构建和发布流程。双通道自动更新和构建时写入节点的目标见 [客户端更新与节点构建计划](client-release.md)。

## 本地构建

桌面客户端使用 Python 3.15.2，版本记录在 `client/.python-version`。安装 uv 0.13.10 或更新版本后，在仓库根目录同步客户端和打包工具。uv 会获取所需的 Python 版本。

```sh
uv sync --project client --locked --extra build
uv run --project client --no-sync python -m unittest discover -s client/tests -v
```

下面以 Apple Silicon 为例。Windows 用 `windows` 和 `frpc.exe`，Linux 用 `linux`。架构填 `amd64` 或 `arm64`。

```sh
uv run --project client --no-sync python client/scripts/fetch_frpc.py --os darwin --arch arm64 --output client/build/frp
uv run --project client --no-sync python -m minecraftfrp --frpc client/build/frp/frpc --nodes client/nodes.example.toml
uv run --project client --no-sync python client/scripts/build.py --frpc client/build/frp/frpc --version local
```

产物在 `client/dist/artifacts/`。本机节点配置可存为 `client/nodes.local.toml`。含 token 的文件只留本地。

## GitHub Actions 发布

GitHub Actions 在 Windows、macOS 和 Linux 上分别构建 amd64 和 arm64。每轮构建选定同一个上游正式版 `frpc`，下载时校验 SHA-256。macOS 产物目前没有 Developer ID 签名和公证。

正式版本和 Git 标签统一用 `yyyy-mm-dd-n`。例如，`2026-11-25-1` 是当天第一次发布，第二次用 `2026-11-25-2`，次日从 `1` 开始。推送标签后，六个构建全部通过才创建同名 Release，并附上六个压缩包和 `SHA256SUMS`。客户端当前通过手动下载新压缩包更新。

## 大厅服务和管理工具

原 API 可在 `server/` 安装其 `requirements.txt` 后运行 `python app.py`。管理工具在 `tools/admin/` 安装其依赖后运行 `python admin_gui.py`。

API 的管理接口读取环境变量 `MINECRAFTFRP_ADMIN_KEY`。管理工具的节点管理功能读取 `MINECRAFTFRP_ADMIN_PASSWORD`、`MINECRAFTFRP_SSH_HOST`、`MINECRAFTFRP_SSH_USER`、`MINECRAFTFRP_SSH_PASSWORD` 和 `MINECRAFTFRP_SERVER_LIST_KEY`。

也可在各自目录的本地 `.env` 文件中配置这些变量，并在已安装依赖的虚拟环境中用 `uv run --no-project --env-file .env python app.py` 或 `uv run --no-project --env-file .env python admin_gui.py` 启动。
