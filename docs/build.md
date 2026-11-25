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
./dev.sh uv run --project client --no-sync python client/scripts/build.py --frpc /tmp/minecrafttools-build/frp/frpc --version local
```

产物在 `/tmp/minecrafttools-build/dist/artifacts/`，文件名以 `MinecraftTools-` 开头。不经过 `dev.sh` 直接运行 `build.py` 时仍写回 `client/dist/`，可以用 `MINECRAFTTOOLS_DIST_DIR` 和 `MINECRAFTTOOLS_WORK_DIR` 改到项目外。默认本地构建内置样例线路，也可以在命令末尾加 `--nodes-file client/nodes.local.toml` 打包自定义节点。配置大厅服务时，设置 `MINECRAFTTOOLS_LOBBY_URL` 为服务的 `/api/lobby` 地址。

## GitHub Actions 发布

GitHub Actions 在 Windows、macOS 和 Linux 上分别构建 amd64 和 arm64。每轮构建选定同一个上游正式版 `frpc`，下载时校验 SHA-256。Runner 是一次性的，工作流仍直接写 `client/build/frp` 和 `client/dist/artifacts`，不走 `dev.sh`。

`client.yml` 分三段：

1. `build`：四组件测试、PyInstaller 打包 Main App（桌面压缩包与 `MinecraftTools-update-*.tar.gz`），再用 `cargo build --release` 编译 Rust Launcher，并把发布公钥与控制端地址编入 Launcher。
2. `installers`（仅标签）：取本平台产物，调用 `client/scripts/package_desktop.py` 生成 `.msi`、`.dmg` 或 `.run`，然后从安装入口启动并验证 Main App 发出的 `READY`。冒烟使用 `--smoke-test --local-fixture`，不访问网络。
3. `release`：汇总六平台产物，生成 `SHA256SUMS` 和本次通道的 Ed25519 签名清单，创建同名 GitHub Release。Release 共 18 个平台资产：6 个桌面压缩包、6 个更新包、6 个安装包。

正式版本使用 `yyyy-mm-dd-n`。stable 使用同名 Git 标签，dev 加 `dev-` 前缀。dev 采用 GitHub 预发布。安装包和更新包同版本一起发布。 [GitHub 托管 runner](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)

签名清单的有效期是 365 天。空的通道也发布已签名清单；一年内没有新版本时需要手动重签。macOS 安装包暂用 ad-hoc 签名，Windows 安装包未做 Authenticode；正式签名和公证需要另外的凭据素材。

日期标签构建需要预先在 GitHub 的 `dev` 与 `stable` Environment 配置变量 `MINECRAFTTOOLS_NODES_TOML`，用 [节点样例](../client/nodes.example.toml) 的格式填写只含可公开发布字段的目录。一次发布先把选定通道的目录上传为本次运行的产物，再由六个构建作业使用同一份输入。[GitHub Actions 变量上下文](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#vars-context)

控制端和香港 dev Agent 都使用 `https://ctl.mctools.mitahill.com`（Phoenix）。旧 Agent 的原生 TLS 入口为 `us-6.mitahill.com:19443`。Control Server 的部署与入口拓扑见[控制端联调环境](control-server.md#13-分阶段实施)。

### 构建期写入 Launcher

构建发布用 Launcher 时，`MINECRAFTTOOLS_RELEASE_PUBLIC_KEYS` 是编译期 JSON 对象，键名为签名密钥 ID，值为 32 字节 Ed25519 公钥的无填充 Base64URL。`MINECRAFTTOOLS_CONTROL_URL` 内置 HTTPS 地址，安装后的 Launcher 不再需要命令行参数；发布作业也用它拼接更新包 URL。两个变量都是仓库 Variables，六个平台共用；`MINECRAFTTOOLS_RELEASE_KEY_ID` 指定当前 key id。

`release-signing` 环境提供私钥 `RELEASE_SIGNING_KEY_PEM`，发布作业用它签名清单，并核对私钥公钥与 `MINECRAFTTOOLS_RELEASE_PUBLIC_KEYS` 一致。标签构建缺少任一变量时，`prepare` 作业直接失败，避免产出装好即报错的安装包。

标签构建时用于验证签名的公钥只来自仓库 Variables；本机 `release-signing.pem` 是唯一私钥副本，需单独备份。

启用仓库变量 `MINECRAFTTOOLS_PUBLISH_MANIFEST=true` 后，发布工作流在 GitHub Release 创建成功后，通过 `ssh-publish` 把该通道的 18 个发布文件（6 个更新包、6 个桌面压缩包、6 个安装包）上传到 `DEPLOY_SSH_DIRECTORY/<channel>/artifacts/`，在服务器上核对 SHA-256，最后才原子切换 `DEPLOY_SSH_DIRECTORY/<channel>.json`，再删除该通道旧版本的文件。清单里的更新包 URL 指向 `MINECRAFTTOOLS_CONTROL_URL/releases/artifacts/<channel>/<文件名>`，Launcher 因此从 Control Server 下载全部更新内容。GitHub Release 保留同样的 18 个资产，只作归档和手动安装来源。Control Server 通过 [发布卷覆盖配置](../control/compose.releases.yaml) 只读挂载该目录，当前路径是 `/srv/minecrafttools-integration/tls-node-trial/releases`。

### 发布环境与凭据

在仓库的 **Settings → Environments** 管理发布凭据。`release-signing`、`dockerhub-publish` 和 `ssh-publish` 都只接受 `yyyy-mm-dd-n` 和 `dev-yyyy-mm-dd-n` 形状的标签。桌面工作流校验日期和序号，签名和 Phoenix 上传使用各自的环境。镜像发布见[发布通道](client-release.md#发布通道)。

| Environment | 类型 | 名称 | 用途与状态 |
| --- | --- | --- | --- |
| `release-signing` | Secret | `RELEASE_SIGNING_KEY_PEM` | 已配置，Ed25519 发布私钥，标签构建时用于签名清单。 |
| 仓库 | Variable | `MINECRAFTTOOLS_RELEASE_KEY_ID` | 已配置为 `release-2026-11`。 |
| 仓库 | Variable | `MINECRAFTTOOLS_RELEASE_PUBLIC_KEYS` | 已配置，构建期写入 Launcher 的公钥映射。 |
| 仓库 | Variable | `MINECRAFTTOOLS_CONTROL_URL` | 已配置为 `https://ctl.mctools.mitahill.com`。 |
| 仓库 | Variable | `MINECRAFTTOOLS_PUBLISH_MANIFEST` | 已启用，Release 创建后上传发布文件并切换签名清单。 |
| `dockerhub-publish` | Secret | `DOCKERHUB_TOKEN` | 已配置，允许登录 Docker Hub 并推送镜像。当前令牌于 2027-02-23 到期。 |
| `dockerhub-publish` | Variable | `DOCKERHUB_USERNAME` | 已配置，Docker Hub 登录名。 |
| `dockerhub-publish` | Variable | `DOCKERHUB_NAMESPACE` | 已配置为 `kindmitaishere`。 |
| `ssh-publish` | Secret | `DEPLOY_SSH_KEY` | 已配置，Phoenix 的 CI 专用 Ed25519 root 私钥，无解锁口令。 |
| `ssh-publish` | Variables | `DEPLOY_SSH_HOST`、`DEPLOY_SSH_PORT`、`DEPLOY_SSH_USER` | 已配置，Phoenix 的 SSH 连接参数。 |
| `ssh-publish` | Variable | `DEPLOY_SSH_KNOWN_HOSTS` | 已配置，固定的 Phoenix Ed25519 主机公钥，用于严格验证服务器身份。 |
| `ssh-publish` | Variable | `DEPLOY_SSH_DIRECTORY` | 已配置为 Phoenix 上的发布目录。 |

登录名和部署参数用环境 Variables。凭据值通过 GitHub Environment Secrets 更新。Docker Hub Token 和 CI 专用私钥的本机副本存于 macOS 钥匙圈。SSH 发布作业在 runner 的临时目录中以 `0600` 权限写入 CI 私钥，用 `StrictHostKeyChecking=yes` 和固定的主机公钥连接 Phoenix，作业结束后删除临时私钥。

Phoenix 的 CI 公钥在 root 的 `authorized_keys` 中标记为 `minecrafttools-ci-ssh-publish`，设置了 OpenSSH `restrict`，关闭端口转发和终端分配等会话能力。需要撤销 CI 的服务器访问时，移除该公钥行并删除 `ssh-publish` 的 `DEPLOY_SSH_KEY`。

### Docker Hub 镜像

`.github/workflows/images.yml` 在标签推送时构建并推送 Control Server 与 FRP Agent 镜像，两者共用 `yyyy-mm-dd-n` 版本标签。以 `2026-11-25-1` 为例，两个镜像的完整引用如下。

```text
kindmitaishere/minecrafttools-control:2026-11-25-1
kindmitaishere/minecrafttools-frp-agent:2026-11-25-1
```

每个镜像都包含 `linux/amd64` 与 `linux/arm64`，推送后用 `docker buildx imagetools inspect` 校验两个平台。`main` 分支和 `control/**`、`node/**` 的 PR 只做单平台构建，不推送，用来提前发现 Dockerfile 破损。正式版 Git 标签 `yyyy-mm-dd-n` 直接用作镜像标签。dev 的 Git 标签 `dev-yyyy-mm-dd-n` 去掉 `dev-` 后用作镜像标签。两个通道共享当天的发布序号，避免不同构建占用同一个镜像标签。

### dev-2026-11-26-1 发布记录

2026-11-26 把新版工作流及配套变更推到 `origin/main`，分支构建和 `dev-2026-11-26-1` 标签构建全部通过。本次推送的提交：

| 提交 | 内容 |
| --- | --- |
| `8566340` | 桌面端内置节点目录，`build.py --nodes-file` |
| `e96a414` | 管理操作审计、封禁文案原样下发 |
| `da75547` | HTTPS 节点接入、按通道提供发布文件 |
| `1aeffc9` | FRP Agent 用 HTTPS WebSocket 连接节点 Hub |
| `dd0d60d` | 新版工作流与文档 |
| `832fc5e` | 修复 macOS runner 上 bash 3.2 的空数组展开 |

`dd0d60d` 的第一次分支构建在 darwin 上失败：`build` 作业用空数组承载可选参数，而 macOS 仍是 bash 3.2，`"${arr[@]}"` 在 `set -u` 下直接退出。`832fc5e` 改为把命令本身放进数组后重跑，六个平台通过。运行记录：

- 分支构建：[Client build 45678710138](https://github.com/MitaHill/Minecraft-Tools/actions/runs/45678710138)（`832fc5e`，四组件测试、lint、六平台打包、Launcher 测试全通过）、[Container images 45678710080](https://github.com/MitaHill/Minecraft-Tools/actions/runs/45678710080)。
- 标签构建：[Client build 45680547451](https://github.com/MitaHill/Minecraft-Tools/actions/runs/45680547451)、[Container images 45680547426](https://github.com/MitaHill/Minecraft-Tools/actions/runs/45680547426)，都使用 `832fc5e`。六个安装作业通过安装、从 Launcher 启动和 Main App `READY` 检查，`release` 与 `deploy-release` 成功。`release-nodes` 取 `dev` 环境的 `MINECRAFTTOOLS_NODES_TOML`，与运行产物的 SHA-256 一致。
- 预发布：[dev-2026-11-26-1](https://github.com/MitaHill/Minecraft-Tools/releases/tag/dev-2026-11-26-1)，20 个资产：6 个桌面压缩包、6 个更新包、6 个安装包、`dev.json` 和 `SHA256SUMS`。镜像 `kindmitaishere/minecrafttools-control:2026-11-26-1` 与 `kindmitaishere/minecrafttools-frp-agent:2026-11-26-1` 的 `linux/amd64`、`linux/arm64` 校验通过。

验收结果：

- Phoenix 原来运行的是本地构建的 `agent-https-20261126`，有 WebSocket 入口但没有按通道的发布文件路由。已改为拉取发布镜像 `2026-11-26-1` 并重建 `control`：`docker compose -p minecrafttools-integration --env-file deploy.env -f compose.yaml -f compose.node-hub.yaml -f node-https.yaml -f releases.yaml -f ../public-control.yaml up -d --no-build control`。`node-https.yaml` 现在固定引用发布镜像，旧内容备份为 `node-https.yaml.bak-<时间戳>`。重建后 `/api/v1/node/connect` 收到香港 dev Agent 的 WebSocket 连接，`hk-dev` 与 `hk-integration` 均为 `online`。
- `/releases/dev.json` 与预发布里的 `dev.json` 逐字节一致，六个更新包的地址都指向 `https://ctl.mctools.mitahill.com/releases/artifacts/dev/`，版本为 `2026-11-26-1`。
- Phoenix 的 `dev/artifacts/` 有 18 个文件，与 GitHub `SHA256SUMS` 全部一致，没有旧版本残留。从清单实际下载 Windows AMD64（55,020,716 字节）和 macOS ARM64（40,101,176 字节）更新包，大小与 SHA-256 均匹配。
- 预发布的 Windows AMD64 MSI 和 macOS ARM64 DMG 与 `SHA256SUMS` 一致；DMG 挂载后应用包深度签名校验通过。解压 macOS ARM64 更新包运行 `--smoke-test` 返回 0，说明发布包的冻结目录里已有可用节点。

当前 Control Server 的文件名校验会拒绝两个 Linux 桌面 `.tar.gz`（本轮实测这两个地址返回 404，安装包本身已上传，可从 GitHub 预发布下载）。若要让这两个压缩包也能从 Phoenix 下载，需先补齐发布文件路由的文件名规则和测试。

## 大厅服务和管理工具

原 API 可在 `server/` 安装其 `requirements.txt` 后运行 `python app.py`。管理工具在 `tools/admin/` 安装其依赖后运行 `python admin_gui.py`。虚拟环境同样放在项目外，例如 `./dev.sh uv venv /tmp/minecrafttools-build/server-venv`；套上 `./dev.sh` 时这两个目录也不会产生 `__pycache__`。

API 的管理接口读取环境变量 `MINECRAFTTOOLS_ADMIN_KEY`。管理工具的节点管理功能读取 `MINECRAFTTOOLS_ADMIN_PASSWORD`、`MINECRAFTTOOLS_SSH_HOST`、`MINECRAFTTOOLS_SSH_USER`、`MINECRAFTTOOLS_SSH_PASSWORD` 和 `MINECRAFTTOOLS_SERVER_LIST_KEY`。

大厅地址和上述管理变量也接受旧的 `MINECRAFTFRP_` 前缀，优先读取新变量。

也可在各自目录的本地 `.env` 文件中配置这些变量，并在已安装依赖的虚拟环境中用 `uv run --no-project --env-file .env python app.py` 或 `uv run --no-project --env-file .env python admin_gui.py` 启动。
