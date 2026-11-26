# Phoenix Control Server 部署

GitHub Actions 只构建并推送镜像，不连接服务器。部署按本文件手工执行。

服务器上的 compose 目录固定在 `/opt/minecraft-tools/control-server/`，项目名固定为
`minecrafttools-integration`。换目录或换项目名会连到新的数据卷，旧数据就看不见了。

## 服务器目录

| 文件 | 来源 |
| --- | --- |
| `compose.yaml`、`compose.node-hub.yaml`、`compose.releases.yaml` | 仓库 `control/` 下的同名文件 |
| `node-https.yaml`、`public-control.yaml`、`deploy.sh` | 仓库 `deploy/control-server/` |
| `deploy.env` | 从 `deploy.env.example` 复制，`chmod 600`，只存在服务器上 |

数据不在这个目录：

- SQLite：Docker 卷 `minecrafttools-integration_control-data`
- 发布文件：`/srv/minecrafttools-integration/tls-node-trial/releases`（CI 的 `ssh-publish` 继续写这里）
- 管理员账号：`/srv/minecrafttools-integration/tls-node-trial/admin`
- 节点 TLS：`/srv/minecrafttools-integration/tls-node-trial/hub-tls`

端口：`18765` 只监听 `127.0.0.1`，由宿主机 nginx 终止 TLS 后代理；`19443` 是节点 Hub。

## 管理台 WebSocket

在 Phoenix 的同一 HTTPS 虚拟主机里，将 `/api/admin/tunnels/live` 加到现有节点入口使用的 WebSocket 代理配置。保留请求的 `Host` 和 `Origin`，由可信 nginx 转发 `X-Forwarded-Proto: https`。代理到 Control Server 时使用 HTTP/1.1，并转发 `Upgrade` 和 `Connection`。同时确认 Control Server 的 `CONTROL_TRUSTED_PROXIES` 覆盖 nginx 实际使用的代理地址。

在隔离环境确认管理台握手后，按 Control Server、节点 Agent 与 firewall-helper、Qt 客户端的顺序滚动更新。新版 Agent 上线后在节点页会显示部署范围。黑名单页会显示尚未确认规则的节点。

## 发布

前提：镜像已经在 Docker Hub 上，标签是 `yyyy-mm-dd-n`（`.github/workflows/images.yml` 的 publish 任务）。
不备份旧目录，不 `down -v`。

```sh
# 1. 仓库里的 compose 文件有改动时，先同步过去
scp control/compose.yaml control/compose.node-hub.yaml control/compose.releases.yaml \
    deploy/control-server/node-https.yaml deploy/control-server/public-control.yaml \
    deploy/control-server/deploy.sh phoenix:/opt/minecraft-tools/control-server/

# 2. 部署
ssh phoenix 'cd /opt/minecraft-tools/control-server && ./deploy.sh 2026-11-26-2'
```

`deploy.sh` 做四件事：拉新标签、把 `CONTROL_IMAGE_TAG` 写进 `deploy.env`、`up -d --no-build`
重建、等容器变成 `healthy`（最多 60 秒）。成功打印 `control is healthy on <tag>` 并清理悬空镜像；
失败打印最近 50 行日志并把标签退回上一版。

## 每次必做的检查

```sh
ssh phoenix 'docker ps --filter name=minecrafttools-integration --format "{{.Status}}"'
curl -fsS https://ctl.mctools.mitahill.com/health
curl -fsS -o /dev/null -w "%{http_code}\n" https://ctl.mctools.mitahill.com/admin/
curl -fsS https://ctl.mctools.mitahill.com/releases/dev.json | head -c 40
ssh phoenix 'docker logs minecrafttools-integration-control-1 2>&1 | grep "WebSocket /api/v1/node/connect" | tail -3'
```

预期：容器 `healthy`；`/health` 返回 `{"status":"ok"}`；`/admin/` 返回 200；
`dev.json` 是签名清单；日志里有节点的 WebSocket 连接和 `connection open`。
大厅再抽一条：`curl -fsS https://ctl.mctools.mitahill.com/api/v1/lobby/rooms | head -c 80`。

## 回滚

```sh
ssh phoenix 'cd /opt/minecraft-tools/control-server && ./deploy.sh <上一版标签>'
```

镜像在 Docker Hub 上就能重新拉回来。数据库迁移不会自动回退，跨迁移版本回滚前先确认
迁移是向后兼容的。

## 注意

- 不要 `docker compose down -v`：会删除控制端数据库卷。
- 不要在服务器上 `docker compose build`：目录里没有源码，镜像只在 CI 构建。
- 命令必须带 `-p minecrafttools-integration`，`deploy.sh` 已经固定。
- 旧目录 `/srv/minecrafttools-integration/tls-node-trial/control` 已于 2026-11-26 删除，不再保留副本。

## 首次搭建

```sh
install -d /opt/minecraft-tools/control-server
# 放入上表里的文件，写好 deploy.env
cd /opt/minecraft-tools/control-server
docker compose -p minecrafttools-integration --env-file deploy.env \
  -f compose.yaml -f compose.node-hub.yaml -f node-https.yaml \
  -f compose.releases.yaml -f public-control.yaml up -d --no-build control
```
