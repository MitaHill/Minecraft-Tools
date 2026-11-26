# 控制端与节点端方案（Docker Bridge）

Control Server 已实现 Launcher 接口、兼容大厅接口和管理台。启动及构建命令见 [Control Server README](../control/README.md)。[分阶段实施](#13-分阶段实施)记录本地与 Phoenix 联调结果，以及节点端接入计划。

## 1. 范围与决定

### 1.1 目标

- 从零搭建 Control Server，兼容现有 Launcher 的 API 和未来 Minecraft Tools 主程序的网络 API。
- 保留联机大厅功能。
- 节点端以 Docker 方式提供 FRP 服务，并通过 TCP 长连接向控制端上报隧道。
- 控制端定时扫描隧道，判断是否具备 Minecraft Java 特征；不合格的隧道从公网封禁。
- 固定 Linux Docker Bridge 网络，在 frps 的容器网络命名空间内按 TCP 端口封禁。

### 1.2 已确定的决策

| 项目 | 决定 |
| --- | --- |
| 控制端后端 | Python 3.12+ / FastAPI |
| 控制端数据库 | SQLite（WAL，单实例；保留迁 PostgreSQL 的接口） |
| 控制端前端 | Vue 3 + Element Plus、Vue Router、TypeScript、Vite |
| 节点 Agent | Go，单二进制，职责固定 |
| 节点网络 | 固定 Docker Bridge 网络 |
| FRP 服务 | 固定版本 frps，和 agent 同一 bridge 网络 |
| 防火墙 | frps 与 firewall-helper 共享 `node-net` 的网络命名空间，helper 用 `nftables` 封端口，再用 `ss -K` 关闭该端口的连接 |
| 封禁层次 | frps plugin 拒绝代理注册和新用户连接，helper 阻断 TCP 流量并清除目标端口的已有连接 |
| 扫描位置 | 控制端统一扫描，节点端只上报和本地自检 |
| 隧道粒度 | 每个可封禁的 TCP 隧道独占一个 frps 监听端口。当前客户端默认一个 frpc 进程跑一个隧道 |
| 最小实现 | 控制端模块化单体，单 uvicorn worker；不引入 Celery/Redis |

### 1.3 非目标

- 不在本期实现多控制端 HA；SQLite 只支持单实例。
- 首版验证范围为 rootful Linux Docker Engine。宿主机 Docker 防火墙后端与容器内规则分别验证。

## 2. 总体架构

```text
[Vue 管理台] ──HTTPS──┐
[Launcher]   ──HTTPS──┤
[主程序]     ──HTTPS──┤  Control Server（FastAPI + SQLite）
                       │   ├─ Launcher 兼容 API
                       │   ├─ 主程序 / 大厅 API
                       │   ├─ 节点 Hub（TLS TCP 长连接）
                       │   ├─ Scanner（Minecraft Java 探测）
                       │   └─ 管理 API
                       │
                 Node（Docker Bridge）
                 ├─ node-net（发布 FRP 控制端口和游戏端口）
                 │  ├─ frps（共享 node-net 的网络命名空间）
                 │  └─ firewall-helper（共享同一命名空间，NET_ADMIN）
                 └─ agent（Go，通过 Unix socket 指示 helper）
```

数据流：

1. agent 启动后通过 TLS 长连接连控制端，发送节点信息和隧道快照。
2. frps 通过 server plugin 把 `Login`、`NewProxy`、`CloseProxy` 和 `NewUserConn` 事件交给 agent。
3. agent 把隧道上报控制端；控制端落库并安排扫描。
4. 控制端 Scanner 连接节点公网地址和远程端口，执行 Minecraft Java Status 握手。
5. 判定不合格后，控制端下发封禁命令。agent 拒绝对应的 `NewProxy` 和 `NewUserConn`，再通知 firewall-helper 挡住目标端口并关闭该端口的已有连接。
6. 控制端标记隧道 `blocked`，删除大厅房间；客户端轮询状态后停止 frpc 并显示原因。

## 3. 组件职责

### 3.1 Control Server

- 提供 Launcher 三个根路径接口，保持与现有 Launcher 完全兼容。
- 提供主程序 `/api/v1` 接口和旧大厅 `/api/lobby/*` 兼容接口。
- 维护节点、隧道、扫描、封禁、房间、发布、诊断数据。
- 接收节点 TCP 长连接，维护节点会话和命令队列。
- 运行 Scanner，按隧道调度扫描。
- 提供 Vue 管理台 API。

### 3.2 Node 端

- `frps`：只负责 FRP 转发，远程端口按配置范围监听。
- `agent`：
  - 连控制端；
  - 提供 frps server plugin；
  - 上报隧道；
  - 维护本地封禁列表；
  - 向 firewall-helper 发送封禁/解封命令；
  - 处理控制端命令并 ack。
- `firewall-helper`：
  - 以 `network_mode: service:node-net` 加入 frps 的网络命名空间；
  - 只授予 `NET_ADMIN`，管理该命名空间中的专用 nftables 表和相关 TCP socket；
  - 不连接控制端，不暴露端口，只通过共享 Unix socket 接收 agent 命令。

### 3.3 客户端与 Launcher

- Launcher 只使用 `/health`、`/releases/{channel}.json`、`/releases/artifacts/{channel}/<文件名>`、`/diagnostics`。
- 主程序使用 `/api/v1` 注册隧道、轮询封禁状态、维护大厅房间。
- frpc 使用稳定代理名和 metadata，便于节点和控制端关联隧道。
- 封禁后客户端停止 frpc、取消大厅发布、显示原因。

### 3.4 前端文案规范

管理台所有页面只保留用户完成操作所需的文字，包括导航与操作名称、重要状态，以及必要的错误和空状态提示。用户需要采取下一步时，用一句话说清楚。

页面标题、按钮或上下文已表达的信息，省略重复的副标题和说明。API 路径、请求方法等实现细节记录在开发文档中。

## 4. 网络与端口

### 4.1 控制端

| 用途 | 端口 | 暴露 |
| --- | --- | --- |
| HTTPS API / 管理台 | 443 | 公网 |
| 节点 TCP Hub | 9443（TLS） | 公网，仅节点出站连接 |
| 开发 HTTP | 8000 | 本机 |

控制端建议放在反向代理（Caddy/Traefik/Nginx）后面，TLS 在反向代理终止；节点 Hub 单独监听 9443。

### 4.2 节点端 Bridge

节点端固定一个 bridge 网络，例如 `minecrafttools-node`。

| 服务 | 网络 | 端口 |
| --- | --- | --- |
| node-net | `minecrafttools-node` | 持有网络命名空间，发布 `7000` 和远程端口段，例如 `30000-30100` |
| frps | `service:node-net` | 在共享命名空间监听控制端口、游戏端口和内部管理端口 |
| agent | `minecrafttools-node` | 不发布端口；监听内部 `9000` 给 frps plugin |
| firewall-helper | `service:node-net` | 通过共享卷里的 Unix socket 接收命令 |

约定：

- frps 的 `webServer` 只监听容器网络内部，例如 `0.0.0.0:7400`，不发布到宿主机公网。
- agent 的 plugin 监听 `0.0.0.0:9000`，frps 通过 `agent:9000` 访问。
- 首版沿用 1:1 端口映射，例如 `30000-30100:30000-30100`。helper 始终匹配 frps 的容器侧监听端口，采用非 1:1 映射时，控制端需分别记录公网端口和容器端口。
- frps 的 `allowPorts` 和 agent 的 plugin 都校验端口范围；客户端 UI 只能选择节点允许的范围。
- 游戏端口范围与控制端口、管理端口及该命名空间的临时源端口范围分离。范围有重叠时，在命名空间配置 `ip_local_reserved_ports` 并验证自动分配结果。 [Linux 端口分配文档](https://docs.kernel.org/networking/ip-sysctl.html)
- 当前客户端随机端口是 `10000-65534`，必须改为按节点返回的端口范围选择，否则大部分启动会失败。

### 4.3 共享网络命名空间

Docker 将发布端口的流量送到 `node-net` 的容器端口。`frps` 与 helper 通过 `network_mode: service:node-net` 共享其中的网卡、端口和防火墙表，容器内的 `input` 和 `output` 直接处理 frps 的游戏连接。只有 helper 获得 `NET_ADMIN`，权限作用在这个节点的网络命名空间。部署使用普通 bridge 和发布端口，无需 host 网络、`privileged`、Docker socket 或宿主机防火墙规则。 [Docker Compose 网络模式](https://docs.docker.com/reference/compose-file/services/#network_mode) · [Docker 发布端口](https://docs.docker.com/engine/network/port-publishing/) · [Linux 网络命名空间](https://man7.org/linux/man-pages/man7/network_namespaces.7.html)

### 4.4 容器内封锁规则

在共享命名空间创建独立的 `inet minecrafttools` 表，以一个端口集合同时处理 IPv4 和 IPv6。入站匹配目标端口并发送 TCP RST，出站拦截该源端口的普通数据，保留 RST 让对端收到断开通知。此处的端口都是 TCP 隧道端口。 [nftables 规则手册](https://netfilter.org/projects/nftables/manpage.html)

```nftables
table inet minecrafttools {
    set blocked_ports {
        type inet_service
    }
    chain ingress {
        type filter hook input priority filter; policy accept;
        tcp dport @blocked_ports counter reject with tcp reset
    }
    chain egress {
        type filter hook output priority filter; policy accept;
        tcp sport @blocked_ports tcp flags & rst == 0 counter drop
    }
}
```

下面两条命令展示封禁 `30001` 的顺序，应在 firewall-helper 容器内执行。正常实现由 helper 校验目标并序列化操作，规则集只改自有的表和集合。

```sh
nft add element inet minecrafttools blocked_ports '{ 30001 }'
ss -HKnt state connected 'sport = :30001'
```

先拦住新流量，再主动关闭已有连接，包括空闲的 TCP 连接。`ss` 只匹配该端口上的连接，frps 的监听器继续存在。执行后用 `ss -Hnt state connected 'sport = :30001'` 核验。`ss -K` 可能跳过内核不支持关闭的 socket，须读回结果后才确认封禁成功。解封从集合移除端口，客户端后续可发起新连接。 [iproute2 `ss` 手册](https://github.com/iproute2/iproute2/blob/main/man/man8/ss.8) · [Linux socket destroy 权限实现](https://github.com/torvalds/linux/blob/master/net/core/sock_diag.c)

### 4.5 方案比较

- FRP 插件能够拒绝新的代理或玩家连接。拒绝 `Ping` 会关闭整个官方 frpc 会话，粒度是会话。按端口关闭已有连接由 helper 完成，同一 frpc 的其他代理可继续传输。 [FRP Server Plugin](https://gofrp.org/en/docs/features/common/server-plugin/) · [frpc v0.72.0 心跳处理](https://github.com/fatedier/frp/blob/v0.72.0/client/control.go)
- 如目标环境无法授予 `NET_ADMIN`，可另行验证 HAProxy 的 TCP 代理方案。每个游戏端口对应独立后端，先设为 `maint`，再执行 `shutdown sessions server`。这种方案会增加一跳，并需核实 frps 看到的源地址及桥接网络内的访问路径。当前选用已验证的命名空间方案。 [HAProxy 后端状态](https://www.haproxy.com/documentation/haproxy-runtime-api/reference/set-server/) · [HAProxy 连接终止](https://www.haproxy.com/documentation/haproxy-runtime-api/reference/shutdown-sessions-server/)

## 5. 数据模型

SQLite 使用 WAL，单进程写入。表设计：

| 表 | 关键字段 |
| --- | --- |
| `nodes` | `id`, `name`, `public_host`, `port_start`, `port_end`, `agent_version`, `frps_version`, `status`, `last_seen`, `token_hash` |
| `tunnels` | `id`, `node_id`, `run_id`, `proxy_name`, `remote_port`, `remote_addr`, `local_ip`, `local_port`, `install_id`, `status`, `scan_failures`, `last_scan_at`, `blocked_at`, `block_reason` |
| `tunnel_scans` | `id`, `tunnel_id`, `checked_at`, `result`, `latency_ms`, `version`, `motd`, `error` |
| `blocks` | `id`, `tunnel_id`, `node_id`, `port`, `reason`, `source`, `active`, `created_at`, `removed_at` |
| `clients` | `install_id`, `version`, `channel`, `os`, `arch`, `last_seen` |
| `rooms` | 沿用现有大厅字段和 `full_room_code` 主键，新增可空的 `tunnel_id` |
| `online_users` | 大厅在线心跳，按客户端 IP 保存 `last_heartbeat` |
| `releases` | `channel`, `version`, `os`, `arch`, `url`, `size`, `sha256`, `signature` |
| `diagnostics`（已实现） | `id`、接收时间 `received_at`、归一化的 `os` 和 `arch`、保留 Launcher 原字段的 `report` JSON |
| `admins` / `audit_logs` | 管理台认证和操作审计 |
| `node_commands` | `id`, `node_id`, `tunnel_id`, `type`, `payload`, `status`, `created_at`, `acked_at` |

SQLite 连接设置：

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
```

约束：

- 只跑一个 uvicorn worker、一个控制端实例。
- 不在网络请求事务里做扫描或长耗时操作。
- 心跳不要每条写库；内存维护 `last_seen`，定期批量落库。
- 迁移用 Alembic；SQLite 的 `ALTER TABLE` 能力有限，字段变更提前规划。
- 备份用 `VACUUM INTO` 或 `sqlite3 .backup`，不要直接复制正在写的文件。
- 出现多副本/HA、明显锁等待或复杂事务需求时迁 PostgreSQL。

## 6. API

### 6.1 Launcher 兼容 API（根路径，必须与 Launcher 完全一致）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 当前返回 `200` 和 `{"status":"ok"}`，无需查库或鉴权，独立于大厅限流和黑名单 |
| GET | `/releases/{channel}.json` | `channel` 为 `dev` 或 `stable`。从发布目录读取签名清单，清单缺失时返回 `503` |
| GET | `/releases/artifacts/{channel}/{filename}` | `channel` 为 `dev` 或 `stable`。按校验后的完整包文件名读取本地 `<channel>/artifacts/`，支持 Range 请求 |
| POST | `/diagnostics` | 校验 Launcher 的启动失败报告，在线程池中写入 SQLite，事务提交后返回空响应 `201`，无效请求返回 `422` |

发布目录布局是 `<release_dir>/<channel>.json` 和 `<release_dir>/<channel>/artifacts/<文件名>`。每个通道只保留最新版本的六个更新包、六个桌面压缩包和六个安装包；需要回退时按旧标签重新构建并发布。

清单外层传输 `schema`、`key_id`、`payload`、`signature`。`payload` 是以下 JSON 原始字节的 Base64URL 编码。签名覆盖原始字节。

```json
{
  "channel": "dev",
  "issued_at": 1760000000,
  "expires_at": 1762592000,
  "releases": [
    {
      "channel": "dev",
      "version": "2026-01-02-1",
      "os": "darwin",
      "arch": "arm64",
      "url": "https://ctl.mctools.mitahill.com/releases/artifacts/dev/MinecraftTools-update-2026-01-02-1-darwin-arm64.tar.gz",
      "size": 1048576,
      "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  ]
}
```

要求：

- `version` 必须是 `yyyy-mm-dd-n`，不能把 `dev-yyyy-mm-dd-n` 标签直接写入。
- `os` 用 `darwin`/`windows`/`linux`，`arch` 用 `amd64`/`arm64`。
- 发布作业把清单有效期签成 365 天；空的通道也提供同样有效期的已签名清单，一年内没有新版本时需要手动重签。
- 诊断记录的 `os` 和 `arch` 列使用发布平台命名。现有实现把 `macos` 转为 `darwin`，`aarch64` 转为 `arm64`，`x86_64` 转为 `amd64`。`report` JSON 保留 Launcher 发送的原值。
- `/diagnostics` 不要复用 `/api/report_violation` 的自动封 IP 语义。
- `/health` 不要套大厅的 60 次/分钟自动封禁中间件。

### 6.2 主程序 API

```text
POST /api/v1/installs/register
POST /api/v1/tunnels/register
POST /api/v1/tunnels/{id}/heartbeat
GET  /api/v1/tunnels/{id}/status
DELETE /api/v1/tunnels/{id}

GET  /api/v1/lobby/rooms
POST /api/v1/lobby/rooms
DELETE /api/v1/lobby/rooms
POST /api/v1/lobby/heartbeat
GET  /api/v1/lobby/online
```

状态响应：

```json
{
  "status": "blocked",
  "reason": "测试",
  "blocked_at": 1760000000,
  "last_scan_at": 1760000000
}
```

`status` 取值：`pending`、`active`、`block_pending`、`blocked`、`closed`、`offline`。客户端把 `block_pending` 和 `blocked` 都视为需要停止映射的状态。

`reason` 是控制端下发给客户端直接展示的封禁文案。管理员封禁使用操作员填写的原因，扫描器等自动触发条件提供各自的文案；客户端原样展示（仅保留对历史 `no_minecraft_status` 记录的兼容）。当前扫描器自动封禁的文案为「测试」，后续按触发条件补充。

客户端状态联动按以下顺序实现并验收。

1. 第一次使用受控线路时，由 `POST /api/v1/installs/register` 发放安装 ID 和随机令牌。令牌按 Control Server 地址保存在用户数据目录，数据库只保留摘要。后续用 `Authorization: Bearer` 认证，监听器和大厅记录均不公开令牌。
2. 受控线路在写 frpc 配置前，使用节点 ID 和远程端口申请隧道注册。控制端核对该节点的允许范围，返回 `id`、`proxy_name` 和独立生成的 `proof`。注册租期为 120 秒，已映射的客户端每 25 秒续约。
3. 客户端把隧道 ID 和证明放入该代理的 FRP metadata。Agent 随快照上报证明摘要，控制端核对节点、代理名、端口及摘要后才确认归属。客户端状态查询只对所属安装开放。测试覆盖伪造 metadata、旧代次、异地节点和端口复用。
4. 主体每 5 秒非阻塞查询状态。收到 `block_pending` 或 `blocked` 时显示原因，停止 frpc，清除映射地址并结束大厅续期。退出映射时注销注册。为晚到的 HTTP 回包、FRP 重连和退出期间的封禁分别验证状态一致性。
5. 先在本地用两个安装身份验证权限和映射流程，再通过 Phoenix 与香港节点用实际 Launcher 和客户端验收封禁界面。用同一 FRP 会话的第二条测试代理确认目标端口隔离。

### 6.3 旧大厅兼容 API

以[旧服务路由](../server/src/main.py)、[旧请求模型](../server/src/models.py)和 [Qt 客户端](../client/src/minecrafttools/lobby.py)为接口基准。

| 方法与路径 | 兼容行为 |
| --- | --- |
| `POST /api/lobby/rooms` | 接收原 `RoomCreate` JSON，以 `full_room_code` 创建房间或续期。成功响应含 `success: true` 和 `message`，业务拒绝继续返回客户端可识别的 `success: false` 和 `message`。 |
| `DELETE /api/lobby/rooms` | 从 JSON 请求体读取整数 `node_id` 与 `remote_port`，按两者组合删除房间。重复删除保持幂等，成功响应保留 `success` 和 `message`。 |
| `GET /api/lobby/rooms` | 返回 `{"success": true, "rooms": [...]}`。最多返回 100 条公开房间，按 `updated_at` 倒序，保留现有房间字段和脱敏后的 `host_ip`。 |
| `POST /api/lobby/heartbeat` | 记录调用者的在线心跳，响应保留 `success`。 |
| `GET /api/lobby/online` | 返回 `{"success": true, "online_count": n}`。沿用 15 秒在线窗口，`online_count` 保持整数。 |

旧客户端的房间键为 `<remote_port>_<node_id>`，其中 `node_id` 是整数。Qt 客户端每 5 秒用同一份 `POST /rooms` 请求续期房间。打开大厅时，客户端立即发送 `POST /heartbeat`，随后每 5 秒续报并刷新在线人数。离开大厅后停止续报，在线记录按来源 IP 去重并在 15 秒后过期。已登记的地址和端口唯一匹配正在运行的隧道时，发布记录关联该隧道。当前兼容接口通过 `CONTROL_LOBBY_TARGETS` 限制可探测的目标，删除操作同时核对房间键和来源 IP。上线前需要核对实际反向代理的可信地址与旧客户端换网行为。

### 6.4 节点协议

节点使用 TLS TCP 长连接，协议为按行 JSON。每行一个消息，带 `type`、`seq`、`payload`。

```text
hello           {protocol_version, node_id, secret, agent_version, frps_version, public_host}
heartbeat       {seq, tunnel_count, load}
tunnel_snapshot {seq, tunnels: [...]}
tunnel_opened   {seq, tunnel: {...}}
tunnel_closed   {seq, proxy_name, remote_port}
block_tunnel    {command_id, tunnel_id, proxy_name, remote_port, run_id, reason}
unblock_tunnel  {command_id, tunnel_id, proxy_name, remote_port}
ack             {command_id, ok, error}
```

约束：

- 节点只主动出站，不监听公网控制端口。
- 心跳 10 秒；30 秒无心跳判离线。
- 重连退避 1s → 60s，带 jitter。
- 重连后发送全量 `tunnel_snapshot`。
- 命令带 `command_id`，至少一次、幂等；agent 持久化最近命令。
- 单行最大 1 MB；超过断开并记录。

### 6.5 管理端 API

```text
GET/POST/DELETE /api/admin/session
GET/POST /api/admin/nodes
PATCH    /api/admin/nodes/{id}
GET      /api/admin/tunnels?status=
WS       /api/admin/tunnels/live
POST     /api/admin/tunnels/{id}/block
POST     /api/admin/tunnels/{id}/blacklist
GET      /api/admin/blacklist
DELETE   /api/admin/blacklist/{ip}
POST     /api/admin/tunnels/{id}/unblock
POST     /api/admin/tunnels/{id}/rescan
GET      /api/admin/scans
GET/POST /api/admin/releases
GET      /api/admin/diagnostics
GET      /api/admin/lobby/rooms
PATCH    /api/admin/lobby
DELETE   /api/admin/lobby/rooms/{listing_id}
GET      /api/admin/audit
```

管理端已实现基于 Cookie 的服务端会话。超级管理员、运维和只读角色可读取管理页面。创建和编辑节点限超级管理员。创建响应只返回一次节点密钥，节点列表省略密钥及其摘要。[账号配置](../control/README.md#管理台前端)

## 7. 隧道发现与身份

### 7.1 受控线路的 FRP metadata

客户端 frpc 配置：

```toml
transport.tls.enable = false

[[proxies]]
name = "minecraft-<tunnel_id>"
type = "tcp"
localIP = "127.0.0.1"
localPort = 25565
remotePort = 30001
metadatas.tunnel_id = "<tunnel_id>"
metadatas.tunnel_proof = "<proof>"
```

frp server plugin 的 `NewProxy` 请求包含 `User.RunID`、`ProxyName`、`RemotePort` 和代理级 `Metas`。Agent 只保存并上报 `claim_id`、`claim_hash`、`run_id`、`proxy_name` 与 `remote_port`。Control Server 根据已验证的节点与注册证明确认安装归属。当前原型使用普通 TCP 连接 frps。 [FRP v0.72.0 配置样例](https://github.com/fatedier/frp/blob/v0.72.0/conf/frpc_full_example.toml)

### 7.2 兜底方式

如果客户端暂时不加 metadata：

- agent 在 `NewProxy` 时记录 `run_id → proxy_name/remote_port`；
- 控制端按 `node_id + remote_port` 关联；
- 客户端调用 `POST /api/v1/tunnels/register` 补充归属；
- 仍要处理“节点先上报、客户端后注册”的竞态，接口幂等。

### 7.3 每条 TCP 隧道独占监听端口

同一个节点上，一个 frps TCP 监听端口只对应一条隧道。agent 在 `NewProxy` 时校验明确的 `remote_port`，首版拒绝随机端口 `0`、非 TCP 类型和 TCP 分组共享端口。按 `node_id`、`remote_port` 与实际注册的代理核对封禁目标。

当前客户端一次只启动一个 frpc 进程和一条隧道。端口级封禁也适用于同一 frpc 内的多代理。`Ping` 拒绝影响整个 frpc 会话，只在明确需要封锁会话时使用。

## 8. 扫描与判定

### 8.1 扫描方式

控制端 Scanner 连接 `remote_addr:remote_port`，执行 Minecraft Java Status 协议：

1. TCP 连接；
2. 发送 Handshake（`next_state=1`）；
3. 发送 Status Request；
4. 读取 Status Response JSON；
5. 可选 Ping/Pong 校验延迟；
6. 老版本可回退 Legacy Ping。

不依赖 `mcstatus` 作为运行时依赖；可以用它做测试对照。Scanner 用 `asyncio`，并发加信号量，避免把节点打满。

### 8.2 判定结果

| 结果 | 含义 | 是否计入封禁 |
| --- | --- | --- |
| `ok` | 完成合法 Java Status 握手，JSON 可解析 | 否，清零失败计数 |
| `not_minecraft` | TCP 连上，但握手/JSON 不合法 | 是 |
| `unreachable` | TCP 连不上、超时、DNS 失败 | 否，标记 `offline` 并继续重试 |
| `unknown` | 其他异常 | 否，记录日志 |

“MOTD”判定：

- 基本标准是 **能完成 Java Status 握手并解析出 status JSON**，至少包含 `version` 和 `players`。
- `description` 为空不单独判死；有些服务器 MOTD 为空但仍是 Minecraft Java。
- 如果产品要求必须有非空 MOTD，开启 `strict_motd=true`，并接受 `enable-status=false` 的服务器会被判不合格。

节点 TLS Hub 启用后可设置 `CONTROL_SCANNER_ENABLED=1` 启动调度器。手动封禁联调保持此项关闭，以独立验证命令和状态回传。

### 8.3 默认策略

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `scan_interval` | 30 秒 | 每隧道扫描间隔，带 jitter |
| `grace_period` | 60 秒 | 隧道创建后等待 Minecraft 启动 |
| `failure_threshold` | 3 次 | 连续 `not_minecraft` 后封禁 |
| `unreachable_grace` | 10 分钟 | 长时间不可达标记 offline，不自动封禁 |
| `circuit_breaker` | 20% | 一轮扫描失败率超过 20% 时暂停自动封禁并告警 |
| `auto_unblock` | 关闭 | 默认人工解封；可配置连续成功后自动解封 |

严格模式可以设 `failure_threshold=1`、`grace_period=0`，但误封会明显增加。达到连续失败阈值后，扫描器自动封禁当前隧道，并返回「未检测到 Minecraft Java 响应」。

### 8.4 安全约束

- 只扫描该节点已注册的 `public_host` 和允许端口，禁止扫描任意地址，防止控制端变成 SSRF/端口扫描器。
- 解析出的私网、回环、链路本地地址必须拒绝。
- 扫描目标必须属于当前隧道；隧道关闭或端口复用时取消旧扫描。
- 扫描任务按节点和全局限流；扫描结果采样保留，避免 SQLite 无限增长。

## 9. 封禁执行

### 9.1 控制端

封禁事务：

1. 写 `blocks`，把 `tunnels.status` 置为 `block_pending`；
2. 删除该隧道关联的大厅房间；
3. 创建 `node_commands`，下发 `block_tunnel`；
4. 收到 agent 的 ack 后把 `tunnels.status` 置为 `blocked`；未 ack 则重试并告警。

解封：

1. 下发 `unblock_tunnel`；
2. helper 移除容器内端口规则并确认，随后 agent 解除插件拒绝；
3. 控制端按节点报告的实际连接状态恢复 `active` 或 `offline`，允许重新扫描；
4. 可选：连续多次合法扫描后自动解封。

### 9.2 frps plugin

- `NewProxy` 先核对类型、端口范围及端口是否保留给已封禁的隧道。封禁期间拒绝目标端口的重新注册，重连、更名或旧代理下线均不释放该端口。
- `NewUserConn` 按 frps 提供的 `run_id`、`proxy_name` 关联实际注册的端口，拒绝目标代理的新玩家连接。helper 仍负责已经进入转发的连接。
- `CloseProxy` 清理在线代理记录并上报。封禁记录等显式解封再删除。
- 若客户端在 `block_pending` 就关闭 frpc，Agent 保留该端口最后关闭的代理代次，以便核验并完成晚到的封禁。仍在线的代理占用该端口时拒绝旧代次命令。控制端在解封时根据最近的快照恢复端口状态，已关闭的旧代理不再占用端口。
- `Login` 可用于明确的安装级封锁。普通隧道封禁维持 FRP 心跳行为，继续允许同一会话的其他代理工作。

插件元数据用于关联记录。封禁指令以控制端下发的节点、隧道、代理与端口为准，匹配当前注册或已经保留的封禁记录。旧指令的隧道代次与当前端口占用不符时拒绝执行。

### 9.3 firewall-helper 与状态恢复

agent 通过共享卷里的 Unix socket 调用 helper。第一版的 socket 用专用目录和组权限限制访问，helper 验证动作、指令 ID、端口类型与配置范围，串行应用变更。helper 仅持有 `NET_ADMIN`，其持久化卷只由 helper 写入。

```json
{"command_id":"block-42","action":"block","port":30001}
{"command_id":"block-42","ok":true,"port":30001}
```

- 每次操作先核对目标和当前状态。封禁意图先写入持久卷，再按[容器内封锁规则](#44-容器内封锁规则)更新集合、清除目标 TCP 连接并读回。若规则成功、socket 清除失败，保留规则，返回失败以触发重试。
- 重复命令按 `command_id` 去重。ACK 丢失或 helper 重启后，重新核对内核状态再返回结果。解除封禁时，agent 在 helper 确认规则移除和状态更新前继续拒绝注册与新连接。
- 启动时读取保存的封禁集合，用 `nft -f` 原子更新自有表。helper 检查入站和出站链、匹配规则、端口集合与内核能力，并每 3 秒核对和修复。agent 在规则与本地状态完成同步前拒绝新代理注册。
- helper 或 frps 单独重启时，`node-net` 保留命名空间。`node-net` 重建时，同步重建共享它的服务。新命名空间先恢复规则，再启动 frps。上线测试要覆盖 Docker daemon 重启，不能只依赖 Compose 的显式重启顺序。
- frps 镜像使用固定版本的上游二进制。入口先通过 bridge 向 agent 查询 helper 的实时健康状态，确认返回的网络命名空间与 frps 一致，再启动 frps。Docker 直接拉起容器也执行这个检查。 [Compose 启动顺序](https://docs.docker.com/compose/how-tos/startup-order/) · [Docker 重启策略](https://docs.docker.com/engine/containers/start-containers-automatically/)
- `nftables` 或 `SOCK_DESTROY` 能力不足时报告具体错误，保持 `block_pending` 并告警。目标端口已被流量规则封住时，保留这层保护。 [nftables 原子更新](https://wiki.netfilter.org/wiki-nftables/index.php/Atomic_rule_replacement) · [Linux INET_DIAG_DESTROY](https://github.com/torvalds/linux/blob/master/net/ipv4/Kconfig)

### 9.4 客户端通知与大厅

- 客户端映射期间每 5 秒轮询 `GET /api/v1/tunnels/{id}/status`。
- 收到 `block_pending` 或 `blocked` 后：停止 frpc、原样显示服务端返回的封禁文案、调用 `DELETE /api/lobby/rooms`、禁用重新发布。
- 控制端在封禁事务中删除大厅房间，并从 `block_pending` 开始拒绝目标端口发布和续期，直至解封确认。客户端删除是幂等兜底。
- 客户端想重新尝试时，先走“重新检测”或等待管理员解封；直接重启 frpc 会被 plugin 拒绝。

### 9.5 封禁顺序

1. agent 进入待封禁状态，先拒绝目标代理的新注册和新用户连接。
2. helper 持久化端口状态，应用入站和出站规则，再关闭目标端口上的 TCP 连接。
3. helper 读回封禁集合和目标端口的活动连接。核验通过后，由 agent ACK，控制端从 `block_pending` 转为 `blocked`。
4. 大厅下线和客户端状态轮询沿用既定流程。解封后，有效的原监听器可以接收新连接，已关闭的连接需要客户端重新建立。

## 10. Docker 部署

### 10.1 控制端 compose

```yaml
services:
  control:
    build: ./control
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
    volumes:
      - control-data:/data
    environment:
      CONTROL_DATA_DIR: /data
      CONTROL_PUBLIC_URL: https://control.example.com
      CONTROL_NODE_HUB_PORT: "9443"
    restart: unless-stopped

  web:
    build: ./web
    ports:
      - "80:80"
      - "443:443"
    depends_on:
      - control
    volumes:
      - caddy-data:/data
      - caddy-config:/config
    restart: unless-stopped

volumes:
  control-data:
  caddy-data:
  caddy-config:
```

说明：

- SQLite 文件放在 `control-data` 卷。
- 只跑一个 `control` 实例；不要水平扩容。
- 反向代理负责 TLS 和静态 Vue 资源。
- 反向代理应发送 `Host`、`X-Forwarded-Proto` 和客户端地址。设定 `CONTROL_TRUSTED_PROXIES` 为实际代理地址。宿主机代理经 Docker 端口映射访问时，以 Control Server 实际看到的 bridge 网关为准。
- HTTPS Agent 通过反向代理访问节点 Hub。仍使用原生 TLS 的 Agent 走 Hub 的独立监听端口。

#### Phoenix 入口和发布目录

2026-11-25 起，Control Server 的 HTTPS 入口是 `https://ctl.mctools.mitahill.com`（A/AAAA 都指向 Phoenix）。宿主机系统 nginx 在 443 上终止 TLS，把 `/` 代理到 `127.0.0.1:18765`（Compose 中 control 容器发布的 8000 端口），并发送 `Host`、`X-Forwarded-For` 与 `X-Forwarded-Proto`。证书由 certbot 管理。旧 trial nginx 的 `mitahill.com:18443` 入口已经停用。

香港 dev Agent 使用 `CONTROL_URL=https://ctl.mctools.mitahill.com:443`，并将 `CONTROL_CA_FILE` 设为空，使用系统信任库验证 nginx 的服务端证书。它在 `/api/v1/node/connect` 通过 HTTPS 升级为 WebSocket，沿用节点 secret 和原有消息格式。Control Server 设置 `CONTROL_NODE_HUB_HTTPS=1`，并在 nginx 的该路径转发 WebSocket 升级头。Phoenix 当前 Compose 部署通过 `control/node-https.yaml` 覆盖镜像和该环境变量，重建控制端时需把此文件加入现有的 `-f` 参数。

```nginx
location = /api/v1/node/connect {
    proxy_pass http://127.0.0.1:18765;
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

旧 Agent 仍可连接 `tls://us-6.mitahill.com:19443`，并使用其原有 CA 配置。HTTPS 节点入口使用服务端证书和节点 secret。启用节点客户端 CA 的 Hub 继续使用独立的 mTLS 监听。

#### 节点 mTLS 与证书轮换

在 Hub 的 TLS 挂载目录添加节点客户端 CA，设置 `CONTROL_NODE_HUB_CLIENT_CA=/run/control-tls/node-clients-ca.pem`。启用后 Hub 要求客户端证书链通过校验，证书须使用 `clientAuth`，并包含唯一的 URI SAN `urn:minecrafttools:node:<node_id>`。Hub 仍核对原有的节点 secret。滚动接入时先为各 Agent 配置凭据，再启用 Hub 的客户端证书校验。

Agent 使用 `CONTROL_CLIENT_CERT_FILE` 与 `CONTROL_CLIENT_KEY_FILE` 读取只读挂载中的证书和私钥，两个变量成对配置。轮换时并行签发同一节点身份的新证书，先部署新证书和私钥，再让 Agent 重连，确认快照、心跳和命令 ACK。Agent 在新 TLS 握手时会重读证书和私钥。轮换成功后撤销旧证书，用 `CONTROL_NODE_HUB_CLIENT_CRL=/run/control-tls/node-clients.crl.pem` 配置签名 CRL，并重启隔离 Control Server 以加载新的信任配置。轮换 CA 时先部署可验证新旧链的 CA 及对应的 CRL，再逐个迁移节点。

发布目录在宿主机 `/srv/minecrafttools-integration/tls-node-trial/releases`，通过 [发布卷覆盖配置](../control/compose.releases.yaml) 只读挂载为 `/run/releases`，容器内 `CONTROL_RELEASE_DIR=/run/releases`。CI 的 `ssh-publish` 作业把该通道的发布文件传到 `<channel>/artifacts/`，在服务器上核对 SHA-256 后，用 SFTP 先写临时文件再改名，原子替换对应通道的 `dev.json` 或 `stable.json`，最后删除该通道旧版本的文件。

### 10.2 节点端 Compose

实际部署使用 [节点 Compose](../node/compose.yaml)。`firewall-helper`、`firewall-health` 和 `agent` 由 `node/` 镜像提供。以下示例展示网络拓扑。本地网络验证使用了等价的 Docker CLI 网络参数，见[封锁验证](#122-真实-frp-测试)。

```yaml
services:
  node-net:
    image: alpine:3.22
    command: ["tail", "-f", "/dev/null"]
    networks: [node]
    ports:
      - "7000:7000"
      - "30000-30100:30000-30100"
    cap_drop: [ALL]
    restart: unless-stopped

  firewall:
    build: ./node
    command: ["firewall-helper"]
    network_mode: "service:node-net"
    depends_on:
      node-net:
        condition: service_started
        restart: true
    cap_drop: [ALL]
    cap_add: [NET_ADMIN]
    security_opt: ["no-new-privileges:true"]
    environment:
      FIREWALL_STATE: /var/lib/minecrafttools-firewall/state.json
      FIREWALL_SOCKET: /run/minecrafttools/firewall.sock
    volumes:
      - firewall-data:/var/lib/minecrafttools-firewall
      - firewall-run:/run/minecrafttools
    healthcheck:
      test: ["CMD", "/usr/local/bin/frp-agent", "firewall-health"]
      interval: 5s
      timeout: 2s
      retries: 6
    restart: unless-stopped

  agent:
    build: ./node
    command: ["agent"]
    networks: [node]
    depends_on:
      firewall:
        condition: service_healthy
        restart: true
    cap_drop: [ALL]
    environment:
      CONTROL_URL: https://ctl.mctools.mitahill.com:443
      CONTROL_CA_FILE: ""
      NODE_ID: node-xxx
      FRPS_ADMIN_URL: http://node-net:7400
      PLUGIN_LISTEN: 0.0.0.0:9000
      FIREWALL_SOCKET: /run/minecrafttools/firewall.sock
    volumes:
      - node-data:/data
      - firewall-run:/run/minecrafttools
    restart: unless-stopped

  frps:
    build: ./node
    command: ["frps", "-c", "/etc/frp/frps.toml"]
    user: "10001:10001"
    network_mode: "service:node-net"
    depends_on:
      node-net:
        condition: service_started
        restart: true
      firewall:
        condition: service_healthy
        restart: true
      agent:
        condition: service_started
    cap_drop: [ALL]
    volumes:
      - ./frps.toml:/etc/frp/frps.toml:ro
    restart: unless-stopped

networks:
  node:
    driver: bridge

volumes:
  node-data:
  firewall-data:
  firewall-run:
```

frps 配置示例适用 FRP v0.72.0。 [上游 frps 配置](https://github.com/fatedier/frp/blob/v0.72.0/conf/frps_full_example.toml)

```toml
bindPort = 7000
proxyBindAddr = "0.0.0.0"
auth.token = "<node-token>"

allowPorts = [
  { start = 30000, end = 30100 }
]

webServer.addr = "0.0.0.0"
webServer.port = 7400
webServer.user = "admin"
webServer.password = "<internal-password>"

[[httpPlugins]]
name = "node-agent"
addr = "agent:9000"
path = "/handler"
ops = ["Login", "NewProxy", "CloseProxy", "NewUserConn"]
```

- `node-net` 是共享命名空间在 bridge 上的 DNS 名。agent 通过 `node-net:7400` 读管理状态，frps 通过 `agent:9000` 调插件，双方端口都留在 bridge 内。
- `frps.toml` 含 FRP 凭据。在宿主机将文件设置为 `root:10001`、权限 `0640`，以只读方式挂载给使用 UID/GID `10001` 的 frps。
- `allowPorts`、agent 的白名单、helper 的操作范围和客户端节点配置使用同一游戏端口范围。IPv6 节点配置 `proxyBindAddr = "::"`，并独立验证 Docker bridge 与公网 IPv6 发布。
- 原型的 frps 接受普通 TCP。Agent 到控制端 Hub 使用独立的 TLS 连接。
- `transport.tcpMux` 保持默认。端口封禁只涉及玩家连接的 socket，FRP 控制连接继续运行。

### 10.3 前置条件

- Linux Docker Engine、普通 bridge，以及能在共享网络命名空间使用的 `nftables` 和 `SOCK_DESTROY`。helper 镜像提供 `nft` 与 `iproute2` 的 `ss`，在目标内核实际验证，而后再启用节点。
- Compose 启动时用 helper 的健康检查确认规则已恢复。制定 `node-net` 替换时同时重建依赖服务的流程，另外验证容器被 Docker 自动拉起时的行为。 [Compose 启动顺序](https://docs.docker.com/compose/how-tos/startup-order/)
- 控制端 SQLite 卷需要持久化和备份，节点的封禁状态也需要持久卷。
- 节点 agent 需要能出站访问控制端 9443。

## 11. 可靠性与运维

- agent 断线重连，重连后全量快照；控制端按 `node_id + proxy_name` 幂等 upsert。
- 命令带 `command_id`，agent 持久化最近命令，重启后去重。
- 心跳不逐条写 SQLite；内存维护 `last_seen`，每 10 秒批量落库。
- firewall-helper 定期读回自有规则并补齐差异。命名空间重建后先恢复规则，再允许 frps 接入。
- 控制端启动时恢复 `block_pending` 命令，重发未 ack 的封禁。
- Scanner 队列存在 SQLite 的 `next_scan_at`，重启后继续。
- 管理台展示节点在线、隧道状态、扫描结果、封禁原因、helper 健康状态。
- 审计所有管理操作。
- SQLite 每日备份；保留最近 N 份。
- 指标：节点连接数、隧道数、扫描延迟/失败率、封禁数、helper 失败数、SQLite 锁等待。

## 12. 测试计划

### 12.1 单元/集成

- Launcher 三个根路径的响应格式。
- 旧大厅 API 的响应结构。
- 扫描器：合法 Java、非 Java、超时、空 MOTD、Legacy Ping。
- 判定状态机：grace、连续失败、unreachable、熔断、解封。
- 节点协议：重连、快照、命令 ack、幂等。

### 12.2 真实 FRP 测试

**已完成的容器网络验证（2026-11-25）**使用 Colima 的独立 Linux arm64 VM，Docker Engine 29.5.2、Linux 6.8、官方 frps/frpc v0.72.0、nftables 1.0.6 和 iproute2 6.1.0。一个 frpc 在同一会话注册 `30001` 和 `30002` 两条 TCP 代理，后端用本地 echo 服务。测试使用 `--network container:<node-net>`，只有 helper 容器获得 `NET_ADMIN`。

| 场景 | 实测结果 |
| --- | --- |
| `30001` 封禁后再从 bridge 用 IPv4、IPv6 连接 | 两种连接都被拒绝。通过 Docker 映射到本机的 IPv4 端口访问也无法传输业务数据。 |
| `30001` 已有两个空闲连接 | `ss -K` 关闭 IPv4 与 IPv6 socket，对端收到断开。监听器仍在。 |
| `30002` 已有连接与新连接 | 同一 frpc 会话里的既有 IPv4、IPv6 连接仍可传输，本机发布的 IPv4 端口也可正常建立新连接。 |
| helper 重启与解封 | 重启 helper 后原规则仍有效，移除集合元素后原监听器可接受新连接。 |
| 容器网络命名空间重建 | 新旧 Docker sandbox ID 不同。先向新命名空间加载保存的封禁端口，再启动 frps。`30001` 依旧被拒绝，`30002` 可连通。 |

**已完成的本地 Compose 联调（2026-11-25）**同一 frpc 在 `30001` 和 `30002` 上建立真实 TCP 隧道，两条隧道均由 Agent 上报至 Control Server。管理端重复发送同一封禁请求时返回同一命令 ID。封禁确认后，`30001` 的已有连接被切断，新连接无法传输数据。`30002` 的现有连接和新连接均可继续传输。重启 helper 后封禁保持，解封后原监听器可接入。Agent 重启后两条隧道仍可在控制端查询，`30002` 可接入。

本地故障演练额外验证了 ACK 丢失后同一指令重发、重复 ACK、旧指令与旧代次拒绝。手工移除入站规则后，helper 恢复规则。停掉 helper 后直接通过 Docker 重启 frps，确认进程等到规则核验通过才启动。重建 `node-net` 后 Docker sandbox ID 发生变化，原封禁仍生效，frps 拒绝目标端口重新注册，另一端口在 frpc 重连后恢复。在关闭 `live-restore` 的独立 VM 中用 systemd 重启 Docker，确认 daemon PID 已变化，自动拉起的容器按同样顺序恢复。持续探测在上述恢复期间均未从封禁端口收到业务数据。

**实现后的集成测试**

1. 按[节点端 Compose](#102-节点端-compose)搭建实际 agent、helper 和 frps，验证正常代理注册、快照上报以及 Dashboard 读数。用真实 Minecraft Java 和非 Minecraft TCP 服务分别验证扫描结果。
2. 下发有效、重复和过期的封禁指令，检查只切断目标端口。保持同一 frpc 上的另一条代理传输，同时确认 `NewProxy` 拒绝封禁端口的重连与复用，`NewUserConn` 拒绝新玩家。
3. 分别重启 helper、agent、frps 和 Docker daemon，再重建 `node-net`。在重启过程中持续从外部连接两个端口，确认封禁端口没有短暂恢复访问。覆盖旧 ACK 丢失、持久卷读写失败与内核缺少 `SOCK_DESTROY` 的情况。
4. 在目标 Linux 节点验证实际公网 IPv4 和启用时的 IPv6、目标 Docker 防火墙后端及发布的端口映射。本次已覆盖 Docker bridge 内的 IPv6 和本机发布的 IPv4。

**Phoenix 和香港节点公网联调（2026-11-25）**

- Phoenix 的独立 Control Server 迁移前生成了 SQLite 在线备份，原卷继续用于测试。独立 HTTPS 入口以受信任证书提供 `/health`、两个 Launcher 通道、大厅与管理台。IPv4 和 IPv6 健康检查通过，管理台登录会话与同源限制通过。真实 macOS Launcher 使用此入口启动已打包主体并收到 `READY`。通道清单为空，本次覆盖现有安装的启动流程。
- 香港节点使用独立 Docker Compose 项目。Agent 校验节点 Hub 证书后注册并上报两个公网 IPv4 隧道。一个真实 frpc 同时转发 Java Status 测试服务和另一个 TCP 服务。两条隧道建立后，管理 API 封禁使目标连接断开、房间下线并拒绝续期，第二条连接继续传输。解封恢复原监听器，并重新通过大厅发布与移除。
- 香港节点分别重启 helper、Agent 和 frps。前两次重启保留封禁和第二条隧道的已有连接，frps 重启后另一个代理重新注册，封禁端口被拒绝。Docker daemon 重启及网络命名空间重建在本地独立 VM 完成。香港节点这次使用公网 IPv4。
- 在同一联调实例启用扫描器。Phoenix 识别正常 Java Status 并完成管理端重扫。另一条隧道在默认 60 秒宽限期后产生三次 `not_minecraft`，随后香港节点执行自动封禁，目标已有连接关闭，正常 Java 隧道继续传输。
- 在两台联调服务器部署客户端归属验证。真实 Qt 客户端校验香港 frps 证书，完成安装注册、映射与大厅发布。收到待封禁状态即关闭 frpc 并停止房间续期，Agent 随后仍能确认封禁；另一条隧道继续传输。解封后，客户端在原端口重新注册成功。macOS 打包主体由实际 Launcher 启动并报告 `READY`，窗口验证封禁原因和已清除的映射地址。

### 12.3 故障演练

- 控制端不可达时，agent 保持已有隧道并继续提供 FRP；新隧道标记 unverified。
- firewall-helper 失败时，控制端保持 `block_pending` 并告警，不假装 `blocked`。
- SQLite 锁等待/磁盘满时的错误处理。
- Scanner 全局失败时触发熔断，不批量封禁。

## 13. 分阶段实施

### 联调环境与验收顺序

| 环境 | 用途 | 数据与入口 |
| --- | --- | --- |
| 本地工作区 | 开发与第一轮验收 | 用临时 SQLite、Launcher 安装状态和 FRP 测试配置，先验证新库和已有库迁移，再运行接口、桌面和容器测试。 |
| Phoenix | Control Server 联调 | Compose 项目 `minecrafttools-integration`，应用目录 `/opt/minecraft-tools/control-server`，SQLite 卷 `minecrafttools-integration_control-data`。HTTPS 入口为 `https://ctl.mctools.mitahill.com`，发布目录为 `/srv/minecrafttools-integration/tls-node-trial/releases`。部署按 [Phoenix 部署 SOP](../deploy/control-server/README.md) 手工执行。 |
| 香港节点（伤心云 Lite-Lv1） | FRP Agent 与公网隧道联调 | 使用独立的 Compose 项目、bridge、封禁状态卷和一段经盘点确认空闲的测试端口。agent 通过 `tls://us-6.mitahill.com:19443` 主动连到 Phoenix。 |
| 香港 dev 线路 | 开发通道的内置线路 | Compose 项目 `minecrafttools-hk-dev`，节点 ID 为 `hk-dev`。Agent 连接 `https://ctl.mctools.mitahill.com:443`。frps 接收 `hk-1.mitahill.com:27002` 的普通 TCP 连接，Phoenix 大厅允许 `33001–33002` 两个游戏端口。 |

- 部署前记录两台机器的系统、监听端口、Docker 版本与已有服务。本地通过后才部署相同构建。在测试实例之间配置节点凭据与 TLS；连接参数和凭据在仓库外提供。
- 按顺序验收旧大厅和 Launcher 回归、Control Server 容器和数据库恢复、管理台权限、Agent 注册和快照、手动封禁与解封、自动扫描与大厅下线。公网节点验证两个独立隧道，并在持有连接时测试目标端口断开、另一端口继续传输和重启后的恢复。
- 客户端发布独立跑完签名清单、真实替换、`READY`、失败回滚与对应平台的安装测试。记录实际执行的测试、平台、产物和结果。代码验收覆盖本项目适用的格式、静态检查、单元、集成与运行时测试。

### 接下来的工作

既有的本地及公网联调覆盖双隧道封禁、解封、命令重试、客户端归属、大厅下线、Java Status 和节点容器重启。本轮以成功的 CI 标签构建为基线，按以下顺序补齐验收。[真实 FRP 测试](#122-真实-frp-测试)

1. **本地验证 CI 产物。** 记录 run ID、提交、标签与校验值。用独立 Docker 项目启动 Control Server，覆盖空库迁移、健康检查、Launcher 接口和诊断写入。先在 macOS arm64 安装并启动 CI 的 DMG，再用同一 Launcher 验证线上签名清单、下载、`READY` 后切换和隔离环境中的启动失败回滚。随后补齐其余五个平台的真实更新和卸载验证。[客户端验收矩阵](client-release.md#发布验收矩阵)
2. **补齐客户端发布配置。** 按通道注入并校验节点目录，核对受控节点的端口区间与 frps `allowPorts`。在具备正式签名凭据时，验证 macOS Developer ID、Apple 公证和 Gatekeeper。
3. **在本地 Docker 验证加固。** 节点 Hub 要求受信任且可绑定节点身份的客户端证书，验证无证书、错误身份和旧证书被拒绝。验证轮换期间仍能回报快照和命令 ACK，审计记录可追溯，限流后仍能完成节点重连。用 SQLite 备份还原到隔离实例，校验完整性、迁移版本、封禁状态和 API。
4. **在 Phoenix 和香港节点复验。** 保留联调配置和数据卷备份，部署本地已通过的同一构建。香港独立 Compose 项目使用两条公网测试隧道，覆盖手动和自动封禁、已有连接、节点容器重启和 `node-net` 重建。持续探测封禁端口无业务数据通过，并验证另一路在 frpc 重连后恢复。Docker daemon 重启前盘点同机服务，如会影响同机业务，使用独立 Docker daemon 验证该场景。
5. **发布 stable。** 汇总格式、静态检查、单元与集成测试、桌面验收及公网联调。验收通过后，先上传并核验版本文件，再切换 `stable.json`，最后从已安装的客户端验证可见和可更新。

2026-11-26 在独立 Colima Docker 环境运行了镜像 CI 发布的 Control Server `2026-11-25-3`。空库迁移到 `0006_install_claims`，SQLite 完整性检查通过。健康检查、两个签名清单和大厅查询返回 200，合法诊断返回 201 并落库。同期使用 macOS arm64 CI 的 DMG 连接该本地服务，Launcher 从安装包启动主体，收到 `READY` 后写入版本状态。

同日从本地测试构建向 Phoeix 和香港分别加载 amd64 镜像，核对镜像层与本地一致，在独立 Compose 项目验证 mTLS 和公网双隧道。手动封禁关闭目标端口原有连接，第二条隧道仍可传输。重建香港 `node-net` 后持续探测未收到封禁端口的数据，另一路在 frpc 重连后恢复，解封后目标重新接入。随后替换节点证书、加载 CRL 并重启联调 Control Server，旧证书被拒绝，Agent 进程未重启并用新证书恢复在线。两台宿主机上的现有项目保留运行。

联调 SQLite 通过在线备份导出。在本地独立容器还原后，完整性检查通过，已有隧道和封禁命令保留，迁移到 `0007_admin_audit` 并确认审计写入。审计记录包含时间、操作者、操作与目标，随登录、退出、登记节点、重扫及封禁命令事务写入 `audit_logs`。已有命令的幂等重试沿用原审计记录。

### Phase 1：控制端骨架

#### 已完成的本地联调

2026-11-25 在 macOS arm64 上验证了 `control/` 的 FastAPI、SQLAlchemy、SQLite WAL 和单 Uvicorn worker。应用启动时在 `CONTROL_DATA_DIR/control.db` 上运行 Alembic，首个迁移是 `0001_diagnostics`。联调用的是正常模式的 Launcher，服务地址为 `http://127.0.0.1:8765`。

- 正常路径读到 `/health`、`dev` 和 `stable` 的 `200` 响应，启动真实 MinecraftTools 主体，收到 `READY` 后退出。
- 模拟主体在就绪前以退出码 `1` 退出。Launcher 保存本地报告并收到 `201`，窗口显示「诊断报告已上报」。数据库内的原报告与本地 JSON 一致。非法报告返回 `422`，重启后原有记录仍可读取。
- 两个通道使用空清单，实际验证的是现有版本启动和诊断流程。复现步骤见 [Control Server README](../control/README.md#验证-launcher)。
- 管理台前端位于 `control/web/`，由 FastAPI 在 `/admin/` 托管。已用浏览器验证未登录跳转、登录、查看空列表、筛选与退出。接口测试另覆盖非空记录、越权和错误密码限流。 [前端启动说明](../control/README.md#管理台前端)
- 旧大厅五个接口已实现。真实桌面客户端经本地 frps 连接 Java Status 测试服务，发布后可见列表，续期后保持单条记录，停止映射后下线。使用新数据库和升级自 `0001_diagnostics` 的数据库回归，原诊断记录可读取。
- Linux arm64 容器通过在线备份、独立卷恢复和容器重建后的数据检查。Phoenix 的独立 HTTPS 入口及与香港节点的 TLS 联调已通过。生产大厅切换仍待验收。

#### 旧大厅实现与验收细节

1. **先固定兼容样例。** 从现有客户端请求生成发布、续期、删除的样例，再从旧服务提取列表和人数的响应结构。把第 6.3 节五个路由写成可重复的接口测试。保留现有默认值、字段类型、文本长度边界和客户端依赖的错误结构。
2. **新增数据库迁移。** 在 `0001_diagnostics` 后追加 `rooms` 与 `online_users` 的 Alembic 迁移。用 `full_room_code` 做房间主键，为 `updated_at` 和 `last_heartbeat` 加索引。保留服务端探测得到的有效版本和简介，续期时更新房间时间。验证新库启动、已有库升级和重复启动，确认原诊断记录仍可读取。
3. **在 `control/` 中接上五个路由。** 复用旧大厅的请求结构、公开房间筛选和 IP 脱敏规则。在短事务内完成防多开检查和房间写入，按键删除并处理重复请求。按旧服务的 10 秒房间过期阈值和 60 秒清理周期验证续期与回收，同时单独验证 15 秒的在线窗口。数据库事务只涵盖本地操作，Minecraft 状态探测使用独立超时。
4. **接入可控的 Minecraft Java Status 探测。** 本地验证使用预配置的测试主机和端口。先校验目标是否允许，再执行探测。用固定测试服务覆盖合法状态、超时和非 Minecraft 响应。公网切换前再确定正式节点允许范围，以及旧客户端只有 `node_id`、`remote_port` 时如何验证删除操作归属。
5. **用现有客户端回归。** 设置 `MINECRAFTTOOLS_LOBBY_URL=http://127.0.0.1:8765/api/lobby`，使用测试节点发布房间。在大厅刷新后确认只出现一条记录，续期后仍可见，停止映射后房间消失。接口测试另外覆盖私有房间、重复删除、心跳超时和拒绝未经配置的探测目标。最后重跑 Launcher 的健康检查和诊断测试，验证大厅逻辑与 Launcher 根路径可以同时运行。

本地验收以干净测试库和已执行 `0001_diagnostics` 的测试库各通过一次为准。旧 `server/` 继续作为兼容对照，生产大厅的迁移和切换在本地验收之后单独验证。

#### 大厅验证后的 Phase 1 工作

1. **容器化和恢复演练。** 按[部署示例](#101-控制端-compose)新增 Control Server 镜像和 Compose 服务，用 `control/uv.lock` 安装依赖，保持单 worker，将可写的 `/data` 挂载为持久卷。以 `/health` 做容器健康检查。代理在接入时将 `control` 标记为 `service_healthy` 依赖。用 SQLite 在线备份生成快照，在独立卷恢复后执行 `PRAGMA integrity_check`，并用原有诊断行及大厅接口验证恢复结果。验收包括容器重建后迁移版本和数据都保留。 [Compose 健康检查](https://docs.docker.com/compose/how-tos/startup-order/) · [SQLite 在线备份](https://sqlite.org/backup.html)
2. **管理台接入。** 为现有前端接入同源登录和只读节点、隧道列表，沿用[管理端权限约定](#65-管理端-api)。用空库和测试记录验证空状态、筛选、未登录和权限不足的响应。Phase 2 再用 Agent 上报的实时数据验证列表。
3. **公网试运行前检查兼容。** 核对在用客户端是否还依赖 `/api/tunnel/validate` 或 `/api/check_access`，明确兼容范围。区分需迁移的持久规则和可以由客户端重建的房间与心跳，用旧数据库副本演练备份、导入和回退。将 Launcher 根路径和大厅路径置于同一 HTTPS 入口，健康检查独立于大厅限流和封禁逻辑，诊断请求采用独立的大小、频率和保留策略。切换前后各验证一次 Launcher 启动、发布房间和删除房间。

正式更新源沿用[客户端更新与节点构建计划](client-release.md)。先在隔离环境生成 `dev` 和 `stable` 清单，校验版本、平台、大小和 SHA-256。Launcher 的签名校验、真实程序替换和回滚通过后，再向正式 Launcher 返回可下载安装的版本。

### Phase 2：节点接入

- Go agent + TLS TCP 长连接 + 快照 + 心跳。
- 部署 node-net、frps、agent 和 firewall-helper 的 bridge Compose 拓扑。
- 接入 frps server plugin 的 `Login`、`NewProxy`、`CloseProxy` 和 `NewUserConn`。
- 实现 firewall-helper 的 Unix socket、容器内 nftables 端口集、socket 清理与启动恢复。
- 管理台节点详情、隧道列表。

验收标准是 frpc 建隧道后控制端可见，agent 重启后能恢复快照，手工封禁和解封可通过 Unix socket 触发且只影响目标端口。

### Phase 3：扫描与封禁

- Scanner + 判定状态机 + 熔断。
- 接入扫描器触发的封禁命令、plugin 拒绝、容器内端口与连接清理。
- 客户端状态轮询、封禁 UI、大厅自动下线。
- 管理台封禁/解封/重扫。

验收：非 Minecraft 隧道在宽限期后被封；已有连接被切断；客户端看到原因；大厅房间消失。

### Phase 4：加固

- 节点 mTLS、密钥轮换、审计、限流。
- 发布元数据签名、CI 生成 dev/stable 元数据。
- 指标、压测、误封调优。
- SQLite → PostgreSQL 的迁移预案。

## 14. 风险与待确认

1. **命名空间生命周期**。在目标节点验证 Docker daemon 重启和 `node-net` 重建时的恢复顺序。容器规则随命名空间销毁，封禁状态从独立卷恢复。
2. **端口归属**。同一 frpc 可以带多条代理，每个目标端口须独占。处理重新注册、端口复用和过期命令时，核对隧道身份与端口当前占用。
3. **目标内核能力**。在目标节点验证共享命名空间可用 `nftables` 和 `SOCK_DESTROY`。当前实测范围为 rootful Linux Docker；其他运行时和公网 IPv6 需要单独验收。
4. **SQLite 单实例**：不能多副本；节点/扫描量上来后要迁 PostgreSQL。
5. **误封**：单次失败就封会误杀启动中的服务器；默认连续 3 次 + 60 秒宽限，严格模式需接受误封。
6. **恶意客户端**。Minecraft Status 是启发式判定。端口封禁后若还需要限制同一客户端注册其他端口，必须有可验证的安装身份或节点分配记录。
7. **端口范围**。客户端配置指定 `remote_port_start` 和 `remote_port_end` 时，映射端口限制在该区间。受控节点发布时应将这两个字段与 `allowPorts` 一起维护。
8. **Launcher 联调范围**。六平台 CI 已完成原生安装入口冒烟。macOS arm64 已使用测试密钥验证真实更新和回滚，仍需用正式签名的 CI 产物复验并补齐其他平台。
9. **发布数据链**。`dev-2026-11-25-3` 已发布，签名清单部署作业通过。首个 stable 版本以及已安装客户端的线上更新路径仍需验收。

## 参考

- [Docker Compose 网络模式](https://docs.docker.com/reference/compose-file/services/#network_mode)
- [Docker 发布端口](https://docs.docker.com/engine/network/port-publishing/)
- [nftables 规则手册](https://netfilter.org/projects/nftables/manpage.html)
- [frp Server Plugin](https://gofrp.org/en/docs/features/common/server-plugin/)
- [frps v0.72.0 新用户连接处理](https://github.com/fatedier/frp/blob/v0.72.0/server/proxy/proxy.go)
- [frps v0.72.0 代理注册和心跳](https://github.com/fatedier/frp/blob/v0.72.0/server/control.go)
