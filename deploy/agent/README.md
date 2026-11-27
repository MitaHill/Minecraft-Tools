# Agent 部署脚本

`deploy.sh` 在 Debian/Ubuntu 上部署一个 MinecraftTools FRP Agent（Compose 项目包含
`node-net`、`firewall`、`agent`、`frps` 四个服务）。

节点身份由 Agent 自己生成：首次启动在数据卷 `/data/node-id` 写入
`node-<16位hex>`，之后重启复用。控制端收到未知 `node_id` 会自动建档，不再要求
`NODE_ID`、节点 secret 或 frpc 登录令牌。因此本脚本只询问：

1. 节点名称
2. 端口范围
3. Compose 目录

镜像标签自动取 GitHub 最新稳定版；需要固定版本时用 `--tag` 覆盖。

`CONTROL_URL` 固定为 `https://ctl.mctools.mitahill.com`。

## 前置条件

- Debian 或 Ubuntu（含 Ubuntu 衍生版）
- Docker Engine 与 Docker Compose v2（`docker compose` 子命令）
- root 权限
- 已升级到支持自动建档的新版 Control Server；旧控制端仍要求 secret，无法连接新 Agent

脚本只检测、不自动安装 Docker/Compose，也不修改云安全组或 ufw。

## 使用

```sh
sudo ./deploy.sh
```

交互示例：

```text
节点名称（用于管理台显示）: hk-dev
端口范围（例如 10000-10002,10004-10005，最多 16 个端口） [30000-30015]: 10000-10002,10004-10005
Compose 目录 [/opt/minecraft-tools/agent]:
自动使用最新稳定版镜像标签: 2026-11-27-8
```

端口规则：

- 逗号分隔，`起-止` 或单端口；例如 `10000-10002,10004-10005`
- 每个端口 1024–65535
- 所有区段端口总数不超过 16
- 区段不能重叠；相邻区段自动合并
- 不能包含 `7000`（frps 控制）、`7400`（管理）、`9000`（plugin）或所选 `--public-port`
- 与 `ip_local_port_range` 重叠会警告
- 首次部署会检查宿主机已有监听端口

非交互：

```sh
sudo ./deploy.sh --yes \
  --name hk-dev \
  --ports 10000-10002,10004-10005 \
  --dir /opt/minecraft-tools/agent
```

固定镜像版本时再加 `--tag 2026-11-27-8`。

可用参数见 `./deploy.sh --help`。

## 生成的文件

```text
<Compose目录>/
├── compose.yaml
├── deploy.env
├── frps.toml
└── secrets/
    ├── frps-admin-password
    ├── frp-token            # 仅旧部署保留，新部署不生成
    └── control-ca.pem       # 使用 --ca-file 时才有
```

- `compose.yaml`：固定镜像 `kindmitaishere/minecrafttools-frp-agent:<tag>`，按端口区段
  展开 `node-net` 的 Docker 发布
- `deploy.env`：项目名、镜像标签、控制端地址、端口范围、路径等；不含 secret
- `frps.toml`：多段 `allowPorts`、dashboard 密码；新部署不写 `auth.token`
- `secrets/frps-admin-password`：随机生成，Agent 和 frps 共用
- 文件权限：`deploy.env` 0600，`frps.toml` 与 secrets 为 `root:10001 0640`

## 已有目录

- 检测到 `<目录>/deploy.env` 且没有修改参数时，直接复用并启动
- `--start` 只启动已有目录，不写文件
- `--ports`、`--tag`、`--name` 等参数用于更新；更新前会把旧文件备份到
  `backup-<UTC时间戳>/`
- `--force` 允许覆盖非部署目录并强制重建
- 更换 Compose 项目名或目录会创建新的数据卷，旧数据不会自动迁移

旧版本节点 compose 里的 `NODE_ID` 环境变量可以继续保留：Agent 会把它写入
`/data/node-id`，升级后仍是同一个控制端节点。新部署不需要设置它；确认旧的
`NODE_ID` 和 `NODE_SECRET_FILE` 不再需要后，可以再从 compose 中移除。

```sh
sudo ./deploy.sh --start
sudo ./deploy.sh --dir /opt/minecraft-tools/agent --ports 10000-10005
sudo ./deploy.sh --dir /opt/minecraft-tools/agent --tag 2026-11-27-1
```

端口变更后，按[节点端口容量](../../docs/node-ports.md)的顺序在管理台调整允许端口和发布通道。

## 管理台步骤

脚本启动成功后会打印管理台需要完成的操作：

1. 新节点会自动出现在 `/admin/nodes`，初始状态为“未发布”
2. 确认“公网地址”：
   - 直连节点 Hub 时自动取连接来源 IP，通常就是节点公网 IP
   - 经 nginx 转发时来源可能是代理地址，需要改成节点公网 IP 或域名
3. “允许端口”默认等于部署范围，可以按需缩小
4. 选择发布通道（dev/stable/both）后客户端才能看到和使用

`frpc 连接端口` 由 Agent 上报（默认 7000）；如果使用 `--public-port` 改了端口，
管理台会同步该值。

## 验证

```sh
cd /opt/minecraft-tools/agent
docker compose --env-file deploy.env -f compose.yaml ps
docker logs --tail 30 mitahill-mctools-agent
```

预期：四个容器运行，`firewall` 为 `healthy`，Agent 日志出现
`connected to control server`，管理台节点 online 且部署范围与输入一致。

失败时脚本会打印 `compose ps` 和最近日志。常见原因：

- 镜像标签不存在：`docker compose pull` 失败
- 控制端地址不可达或证书不受信任：检查 `CONTROL_URL`/CA
- 端口被宿主机占用：换端口或释放占用
- 防火墙 helper 无法启动：宿主机内核需要 nftables、NET_ADMIN 和 `SOCK_DESTROY`

## Agent 版本显示 0.0.0-local

`0.0.0-local` 是二进制里的默认值，表示构建镜像时没有注入版本。控制端只是显示
Agent 在 hello 里上报的 `agent_version`，升级 Control Server 不会改变已有节点的
镜像，必须重建该节点的 `firewall`、`agent`、`frps` 容器。

先确认容器实际使用的镜像和镜像里的版本：

```sh
docker inspect mitahill-mctools-agent --format '{{.Config.Image}}'
docker run --rm --entrypoint /usr/local/bin/frp-agent <上面的镜像> version
```

如果 `version` 输出 `0.0.0-local`，用带 `VERSION` 的构建参数重新构建（或发布新
镜像标签）：

```sh
cd node
docker build --build-arg VERSION=2026-11-27-1 \
  -t kindmitaishere/minecrafttools-frp-agent:2026-11-27-1 .
```

`node/compose.yaml` 的本地构建也支持 `AGENT_VERSION`：

```sh
cd /srv/minecrafttools-hk-dev
AGENT_VERSION=2026-11-27-1 docker compose \
  --env-file deploy.env -p minecrafttools-hk-dev \
  -f compose.yaml -f images.yaml build firewall agent frps
```

已有节点更新镜像标签后，按 `firewall` → `agent` → `frps` 重建；隧道会短暂断开。

如果使用本脚本，重新执行并带上新标签即可，脚本会在启动前检查镜像的 `version` 并
在不匹配时警告：

```sh
sudo ./deploy.sh --tag 2026-11-27-1
```

CI 发布的镜像由 `images.yml` 的 publish 任务传入 `VERSION=$TAG`，因此优先使用
CI 发布的标签，而不是本地 `docker build` 的镜像。

## 安全说明

控制端不再校验节点 secret。任何能连到 Hub 的主机都可以注册节点，知道已有
`node_id` 的主机也可以冒充该节点。默认新建节点为“未发布”，只能降低影响，不能
阻止注册。建议在 nginx 或云防火墙限制 `/api/v1/node/connect` 的来源 IP。

节点删除后，对应 Agent 重连会重新建档；需要永久阻止时应在网络层封禁。

## 回滚

```sh
cd /opt/minecraft-tools/agent
docker compose --env-file deploy.env -f compose.yaml down
```

不要加 `-v`，否则会删除节点数据卷。需要回退配置时，从
`backup-<UTC时间戳>/` 恢复文件后重新启动。
