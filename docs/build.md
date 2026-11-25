# 构建

以下是当前仓库可用的构建和发布流程。双通道自动更新和构建时写入节点的目标见 [客户端更新与节点构建计划](client-release.md)。

## 本地构建

桌面客户端使用 Python 3.15.2，版本记录在 `client/.python-version`。安装 uv 0.13.10 或更新版本后，在仓库根目录同步客户端和打包工具。uv 会获取所需的 Python 版本。

仓库根目录的 `dev.sh` 不下载也不打包，只设定环境变量：虚拟环境、编译缓存和打包产物都落在 `/tmp/minecrafttools-build`，项目树里不出现 `target/`、`.venv/`、`build/`、`dist/` 和 `__pycache__`。临时目录被系统清理后重新执行 `uv sync` 即可。要换位置就设置 `MINECRAFTTOOLS_BUILD_ROOT`。Windows 上不方便用 sh 时按下面的命令直接执行，自行把 `CARGO_TARGET_DIR`、`UV_PROJECT_ENVIRONMENT`、`MINECRAFTTOOLS_DIST_DIR` 和 `MINECRAFTTOOLS_WORK_DIR` 指到项目外。

```sh
./dev.sh uv sync --project client --locked --extra build
./dev.sh uv run --project client --no-sync python -m unittest discover -s client/tests -v
```

下面以 Apple Silicon 为例。Windows 用 `windows` 和 `frpc.exe`，Linux 用 `linux`。架构填 `amd64` 或 `arm64`。

```sh
./dev.sh uv run --project client --no-sync python client/scripts/fetch_frpc.py --os darwin --arch arm64 --output /tmp/minecrafttools-build/frp
./dev.sh uv run --project client --no-sync python -m minecrafttools --frpc /tmp/minecrafttools-build/frp/frpc
./dev.sh uv run --project client --no-sync python client/scripts/build.py --frpc /tmp/minecrafttools-build/frp/frpc --version local
```

产物在 `/tmp/minecrafttools-build/dist/artifacts/`，文件名以 `MinecraftTools-` 开头。不经过 `dev.sh` 直接运行 `build.py` 时仍写回 `client/dist/`，可以用 `MINECRAFTTOOLS_DIST_DIR` 和 `MINECRAFTTOOLS_WORK_DIR` 改到项目外。本机节点配置可存为 `client/nodes.local.toml`，用「设置」或 `--nodes` 载入。含 token 的文件只留本地。配置大厅服务时，设置 `MINECRAFTTOOLS_LOBBY_URL` 为服务的 `/api/lobby` 地址。

## GitHub Actions 发布

GitHub Actions 在 Windows、macOS 和 Linux 上分别构建 amd64 和 arm64。每轮构建选定同一个上游正式版 `frpc`，下载时校验 SHA-256。macOS 产物目前没有 Developer ID 签名和公证。Runner 是一次性的，工作流仍直接写 `client/build/frp` 和 `client/dist/artifacts`，不走 `dev.sh`。

正式版本使用 `yyyy-mm-dd-n`。stable 使用同名 Git 标签，dev 加 `dev-` 前缀。六个原生 runner 完成 Launcher 测试和桌面构建后，工作流创建同名 GitHub Release，提供各平台的桌面压缩包、`MinecraftTools-update-*.tar.gz`、`SHA256SUMS` 与本次通道的 Ed25519 签名清单。dev 采用 GitHub 预发布。 [GitHub 托管 runner](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)

构建发布用 Launcher 时，`MINECRAFTTOOLS_RELEASE_PUBLIC_KEYS` 是编译期 JSON 对象，键名为签名密钥 ID，值为 32 字节 Ed25519 公钥的无填充 Base64URL。正式构建也可用 `MINECRAFTTOOLS_CONTROL_URL` 内置 HTTPS 地址。`release-signing` 环境提供 `RELEASE_SIGNING_KEY_PEM`，指定 `MINECRAFTTOOLS_RELEASE_KEY_ID`，并提供同一份公开密钥映射用于签名核对。Phoenix 的联调密钥按同样格式准备。

启用仓库变量 `MINECRAFTTOOLS_PUBLISH_MANIFEST=true` 后，发布工作流在 GitHub Release 创建成功后，通过 `ssh-publish` 把已签名清单写入 `DEPLOY_SSH_DIRECTORY` 并原子切换对应通道。Control Server 可用 [发布卷覆盖配置](../control/compose.releases.yaml) 只读挂载该目录。验证 Release 六个包的 URL 可下载后，再启用正式通道。

### 发布环境与凭据

在仓库的 **Settings → Environments** 管理发布凭据。`dockerhub-publish` 和 `ssh-publish` 已配置为只接受 `yyyy-mm-dd-n` 和 `dev-yyyy-mm-dd-n` 形状的标签。桌面工作流校验日期和序号，签名和可选的 Phoenix 上传使用各自的环境。镜像发布及原生安装包计划见[发布通道](client-release.md#发布通道)。

| Environment | 类型 | 名称 | 用途与状态 |
| --- | --- | --- | --- |
| `dockerhub-publish` | Secret | `DOCKERHUB_TOKEN` | 已配置，允许登录 Docker Hub 并推送镜像。当前令牌于 2027-02-23 到期。 |
| `dockerhub-publish` | Variable | `DOCKERHUB_USERNAME` | 已配置，Docker Hub 登录名。 |
| `dockerhub-publish` | Variable | `DOCKERHUB_NAMESPACE` | 目标值为 `kindmitaishere`，待写入 GitHub。 |
| `ssh-publish` | Secret | `DEPLOY_SSH_KEY` | 已配置，Phoenix 的 CI 专用 Ed25519 root 私钥，无解锁口令。 |
| `ssh-publish` | Variables | `DEPLOY_SSH_HOST`、`DEPLOY_SSH_PORT`、`DEPLOY_SSH_USER` | Phoenix 的 SSH 连接参数。 |
| `ssh-publish` | Variable | `DEPLOY_SSH_KNOWN_HOSTS` | 固定的 Phoenix Ed25519 主机公钥，用于严格验证服务器身份。 |
| `ssh-publish` | Variable | `DEPLOY_SSH_DIRECTORY` | 待指定，SFTP 上传目标目录。 |

登录名和部署参数用环境 Variables。凭据值通过 GitHub Environment Secrets 更新。Docker Hub Token 和 CI 专用私钥的本机副本存于 macOS 钥匙圈。SSH 发布作业在 runner 的临时目录中以 `0600` 权限写入 CI 私钥，用 `StrictHostKeyChecking=yes` 和固定的主机公钥连接 Phoenix，作业结束后删除临时私钥。

Phoenix 的 CI 公钥在 root 的 `authorized_keys` 中标记为 `minecrafttools-ci-ssh-publish`，设置了 OpenSSH `restrict`，关闭端口转发和终端分配等会话能力。需要撤销 CI 的服务器访问时，移除该公钥行并删除 `ssh-publish` 的 `DEPLOY_SSH_KEY`。

### Docker Hub 镜像

Control Server 和 FRP Agent 的镜像共用 `yyyy-mm-dd-n` 版本标签。以 `2026-11-25-1` 为例，两个镜像的完整引用如下。

```text
kindmitaishere/minecrafttools-control:2026-11-25-1
kindmitaishere/minecrafttools-frp-agent:2026-11-25-1
```

正式版 Git 标签 `yyyy-mm-dd-n` 直接用作镜像标签。dev 的 Git 标签 `dev-yyyy-mm-dd-n` 去掉 `dev-` 后用作镜像标签。两个通道共享当天的发布序号，避免不同构建占用同一个镜像标签。

## 大厅服务和管理工具

原 API 可在 `server/` 安装其 `requirements.txt` 后运行 `python app.py`。管理工具在 `tools/admin/` 安装其依赖后运行 `python admin_gui.py`。虚拟环境同样放在项目外，例如 `./dev.sh uv venv /tmp/minecrafttools-build/server-venv`；套上 `./dev.sh` 时这两个目录也不会产生 `__pycache__`。

API 的管理接口读取环境变量 `MINECRAFTTOOLS_ADMIN_KEY`。管理工具的节点管理功能读取 `MINECRAFTTOOLS_ADMIN_PASSWORD`、`MINECRAFTTOOLS_SSH_HOST`、`MINECRAFTTOOLS_SSH_USER`、`MINECRAFTTOOLS_SSH_PASSWORD` 和 `MINECRAFTTOOLS_SERVER_LIST_KEY`。

大厅地址和上述管理变量也接受旧的 `MINECRAFTFRP_` 前缀，优先读取新变量。

也可在各自目录的本地 `.env` 文件中配置这些变量，并在已安装依赖的虚拟环境中用 `uv run --no-project --env-file .env python app.py` 或 `uv run --no-project --env-file .env python admin_gui.py` 启动。
