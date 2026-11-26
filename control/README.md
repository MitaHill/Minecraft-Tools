# Control Server

Control Server 实现 [Launcher API](../docs/control-server.md#61-launcher-兼容-api根路径必须与-launcher-完全一致)。`dev` 和 `stable` 从发布目录读取签名清单。服务启动时运行 Alembic 迁移，诊断写入 SQLite 后返回 `201`。原报告保存在 `report`，`os` 和 `arch` 两列用于按平台查询。

联调入口是 `https://ctl.mctools.mitahill.com`（Phoenix）。宿主机 nginx 终止 TLS 后代理到 control 容器，并在 `/api/v1/node/connect` 为 dev Agent 升级 WebSocket。原生 TLS 节点入口仍为 `us-6.mitahill.com:19443`。发布清单位于宿主机 `/srv/minecrafttools-integration/tls-node-trial/releases`，通过 `compose.releases.yaml` 只读挂载。

## 启动服务

在仓库根目录执行。数据和虚拟环境使用独立的临时目录，服务只监听本机。

```sh
control_root=$(mktemp -d "${TMPDIR:-/tmp}/minecrafttools-control.XXXXXX")
printf 'Control Server 数据目录 %s\n' "$control_root/data"
CONTROL_DATA_DIR="$control_root/data" \
UV_PROJECT_ENVIRONMENT="$control_root/venv" \
PYTHONDONTWRITEBYTECODE=1 \
uv run --project control --locked --python 3.12 \
  uvicorn app.main:app --app-dir control --host 127.0.0.1 --port 8765 --workers 1
```

## 管理台前端

前端位于 `control/web/`，使用 Vue 3、Vue Router、TypeScript、Vite 和 Element Plus。登录后可查看实时隧道、节点、在线统计、黑名单和公开联机大厅。节点创建、编辑和删除限超级管理员，封禁、拉黑、解除及大厅管理限超级管理员和运维。超级管理员可通过管理 API 创建节点，返回的新节点密钥仅出现一次。

先按上文启动控制端，再在仓库根目录执行以下命令。需要 Node.js 22.12 或更高版本。

```sh
npm --prefix control/web ci
npm --prefix control/web run dev
```

打开 [http://127.0.0.1:5173/admin/](http://127.0.0.1:5173/admin/)。开发服务器把 `/health` 和 `/api/admin` 代理到 `http://127.0.0.1:8765`，包括隧道页面使用的 WebSocket。使用其他后端地址时，在启动 Vite 前设定 `CONTROL_API_TARGET`。

构建时运行 `npm --prefix control/web run build`，产物位于 `control/web/dist/`。重启控制端后可通过 [http://127.0.0.1:8765/admin/](http://127.0.0.1:8765/admin/) 同源访问。构建产物存在时，FastAPI 自动托管页面和静态资源。

通过 `CONTROL_ADMIN_USERS_FILE` 指定应用可读的 JSON 文件。每条记录包含 `username`、`role` 和 `password_hash`，角色为 `superadmin`、`operator` 或 `viewer`。密码以 Argon2id 保存，配置文件留在仓库外。生成哈希时从终端交互读取密码。

```sh
(cd control && uv run --locked python -c 'from argon2 import PasswordHasher; from getpass import getpass; print(PasswordHasher().hash(getpass()))')
```

管理会话默认使用 HTTPS Secure Cookie。本机以回环地址直接测试 HTTP 时可设 `CONTROL_ADMIN_COOKIE_SECURE=0`。Compose 部署时通过覆盖文件只读挂载账号文件，并让 `CONTROL_ADMIN_USERS_FILE` 指向容器内路径。

## 验证 Launcher

在另一个终端，把 `control_root` 指向启动服务时打印的数据目录的上一级。用 OpenSSL 3 生成隔离测试密钥，为两个通道签署空清单，再用相应公钥构建 Launcher。

```sh
release_trial=$(mktemp -d "${TMPDIR:-/tmp}/minecrafttools-release.XXXXXX")
openssl genpkey -algorithm ED25519 -out "$release_trial/key.pem"
for channel in dev stable; do
  python3 control/scripts/sign_releases.py --channel "$channel" \
    --base-url "http://127.0.0.1:8765/releases/artifacts/$channel" --key-id local-test \
    --private-key "$release_trial/key.pem" --output "$control_root/data/releases"
done
export MINECRAFTTOOLS_RELEASE_PUBLIC_KEYS=$(openssl pkey -in "$release_trial/key.pem" -pubout -outform DER | \
  python3 -c 'import base64,json,sys; print(json.dumps({"local-test":base64.urlsafe_b64encode(sys.stdin.buffer.read()[-32:]).decode().rstrip("=")}))')
```

查询健康状态和两份清单，再给 Launcher 指定独立的安装状态目录和已打包的主体程序。更新包放在 `$control_root/data/releases/<channel>/artifacts/`，清单仍是 `releases/<channel>.json`。回环 HTTP 适用于本段空清单启动测试，签名清单中的正式更新包 URL 使用 HTTPS。

```sh
curl -fsS http://127.0.0.1:8765/health
curl -fsS http://127.0.0.1:8765/releases/dev.json
curl -fsS http://127.0.0.1:8765/releases/stable.json

launcher_root=$(mktemp -d "${TMPDIR:-/tmp}/minecrafttools-launcher.XXXXXX")
echo '{"channel":"dev","version":"2026-01-01-1","build_channel":"stable"}' \
  > "$launcher_root/install.json"
MINECRAFTTOOLS_DATA_ROOT="$launcher_root" ./dev.sh cargo run --locked \
  --manifest-path launcher/Cargo.toml --bin launcher -- \
  --control-url http://127.0.0.1:8765 --subject /path/to/MinecraftTools
```

主体主窗口显示后，Launcher 应收到 `READY` 并关闭。要验证诊断上报，先运行 `./dev.sh cargo build --locked --manifest-path launcher/Cargo.toml --bin demo-subject`，然后将 `--subject` 指向生成的 `demo-subject`（Windows 为 `demo-subject.exe`），并加上 `DEMO_SUBJECT_MODE=exit`。Launcher 应显示「诊断报告已上报」，报告在 `$launcher_root/logs/`。回到启动服务的终端，停止测试服务后可查询迁移版本和诊断记录。

```sh
sqlite3 "$control_root/data/control.db" \
  'SELECT version_num FROM alembic_version; SELECT id, os, arch, report FROM diagnostics;'
```

## 旧大厅联调

在启动服务前，通过 `CONTROL_LOBBY_TARGETS` 列出允许探测的 Minecraft Java 隧道。例如 `127.0.0.1:30001,example.test:30002`。IPv6 地址使用 `[地址]:端口`。未配置时仍可读取大厅和记录在线心跳，发布房间需要允许的端口和有效的 Java Status 响应。`CONTROL_LOBBY_BLOCKWORDS_FILE` 可选填旧服务同格式的 UTF-8 词表路径。

将客户端的 `MINECRAFTTOOLS_LOBBY_URL` 指向服务的 `/api/lobby`。关闭映射时，服务按房间键和发布时记录的来源 IP 删除。经反向代理发布时，由部署配置决定哪些代理可以传递客户端地址。

```sh
PYTHONPATH=control ./dev.sh uv run --project control --locked python -m unittest discover -s control/tests -v
```

## Docker 和 SQLite 快照

Compose 默认将控制端发布到宿主机回环地址。运行中的 SQLite 数据目录在 `control-data` 卷内。

```sh
docker compose -f control/compose.yaml -p minecrafttools-test up -d --build
curl -fsS http://127.0.0.1:8765/health
docker compose -f control/compose.yaml -p minecrafttools-test exec -T control \
  python scripts/backup.py /data/control-snapshot.db
docker compose -f control/compose.yaml -p minecrafttools-test cp \
  control:/data/control-snapshot.db ./control-snapshot.db
```

备份脚本在生成副本后运行 `PRAGMA integrity_check`，目标文件名需为空闲路径。恢复演练使用独立数据目录，将快照当作 `control.db` 启动另一套服务，检查迁移版本和已有诊断、房间记录，再从 HTTP 验证读写。

## 节点与扫描器

启用 `compose.node-hub.yaml` 后配置节点 Hub 的证书目录、监听地址和端口。先在管理台登记节点，把首次返回的节点密钥和信任证书交给 Agent。节点端使用 [Compose](../node/compose.yaml)，封禁通过 helper 在共享网络命名空间执行。

`CONTROL_SCANNER_ENABLED=1` 启用扫描器。默认在宽限期后连续三次检测为非 Minecraft 才下发封禁。操作员可在隧道页面查看检测记录、发起重扫和封禁隧道，也可在黑名单页面解除封禁。

Agent 上线后报告已发布的节点端口与 frps `allowPorts` 的交集。管理台用一个输入框配置节点的允许端口（逗号分隔、段内用横杠，例如 `30000-30010,31000`），必须在部署范围内，Agent 确认后才生效。多段端口需要新 Agent；旧 Agent 只按首尾范围校验。扩展部署范围时同步更新节点 Docker 配置、frps `allowPorts` 和 Agent 的环境变量，再让 Agent 重新注册连接。节点离线且没有实时映射、预留和生效封禁时可以在管理台删除，历史映射、封禁和扫描记录会一并清理。IPv4 和 IPv6 都以 frps 看到的连接来源为准。黑名单页面支持手动添加单个 IP 或 CIDR，并逐条解除。填写 `0.0.0.0/0` 或 `::/0` 时需要额外确认。所有受控节点都要先升级 Agent 和 firewall-helper，局部升级期间仍可执行单隧道封禁。

先升级 Control Server，再更新所有已登记的 Agent。节点上报 CIDR 能力后才能添加网段规则。回退到旧版本前，先解除生效中的 CIDR 并确认各节点已经同步。

公开大厅关闭时，现有发布可续期，重新开放后仍有效的房间继续展示。单独下架的房间需要房主手动重新发布。新版受控客户端可用当前隧道凭据发布，旧客户端继续使用配置中的大厅探测目标。Phoenix 和香港节点的实际测试、客户端状态联动和发布计划见[控制端实施计划](../docs/control-server.md#13-分阶段实施)。
