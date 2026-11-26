# 节点端口容量与允许端口

## 两个概念

- **节点容量**：节点 `.env` 的 `NODE_PORT_START`/`NODE_PORT_END` 与 `frps.toml` 的 `allowPorts` 的交集。Agent 上线时上报，管理台显示为「部署范围」。默认 `30000-30015`，16 个端口。
- **允许端口**：管理台在容量内划定的生效子集。Agent 按它拒绝越界映射，控制端只在这个集合里分配端口。改它只动管理台，Agent 确认后生效。
- 改容量必须动节点，控制端无法远程扩大。保存时校验「允许端口 ⊆ 节点容量」，Agent 应用策略时还会再校验一次。

## 容量怎么选

- 端口须在 1024–65535 之间；避开 `7000`（frps 控制）、`7400`（管理）、`9000`（plugin）、宿主机已有服务，以及 `ip_local_port_range`（默认 `32768-60999`）。
- Docker 是 1:1 逐端口发布：每个端口一条 iptables 规则，默认还会起一个 `docker-proxy` 进程，实测约 3 MB PSS。16 个端口约 45 MB，128 个约 360 MB，1001 个约 2.8 GB。默认 16 个；要开大段，先确认节点内存，或者改 Docker 的 `userland-proxy` 设置。
- **不要用 `1000-60000`**：5.9 万个发布项会拖垮节点；`27002`、`7000`、`7400`、`9000` 已经在用，Docker 会直接报端口冲突；`1000-1023` 是特权端口，`32768-60999` 是临时端口。要这种量级只能改成 host 网络或动态发布，不在当前方案内。

## 迁移影响

- 新容量**包含**旧端口时，最省事：`33001-33002` 变成 `33001-33016`，现有分享地址继续有效，管理台的允许端口也仍然合法；只需重建节点容器，隧道短断后自动重连。
- 新容量**不包含**旧端口时，旧地址会失效，大厅房间需要重新发布；先把节点设为「未发布」，再改容量，等节点上线后把允许端口改到新范围，最后重新发布。
- 重建 `node-net` 会重启 `firewall`、`frps`、`agent`，所有隧道断开；这是容量变更不可避免的短断。

## 扩容步骤：保留旧端口（以 hk-dev 33001-33016 为例）

以下命令在节点目录执行。其他节点替换目录、项目名和覆盖文件；节点需要 `python3`。

```sh
cd /srv/minecrafttools-hk-dev
stamp=$(date +%Y%m%d-%H%M%S)
compose() {
    docker compose --env-file deploy.env -p minecrafttools-hk-dev -f compose.yaml -f images.yaml "$@"
}

# 1. 备份
cp -a deploy.env "deploy.env.before-ports-$stamp"
FRPS_CONFIG=$(sed -n 's/^FRPS_CONFIG=//p' deploy.env)
[ -n "$FRPS_CONFIG" ] || { echo "找不到 FRPS_CONFIG"; exit 1; }
cp -a "$FRPS_CONFIG" "$FRPS_CONFIG.before-ports-$stamp"
compose config > "compose.before-ports-$stamp.yaml"

# 2. 改容量
sed -i -E 's/^NODE_PORT_START=.*/NODE_PORT_START=33001/; s/^NODE_PORT_END=.*/NODE_PORT_END=33016/' deploy.env
grep -q '^NODE_PORT_START=' deploy.env || echo 'NODE_PORT_START=33001' >> deploy.env
grep -q '^NODE_PORT_END=' deploy.env || echo 'NODE_PORT_END=33016' >> deploy.env

# 3. 改 frps allowPorts
python3 - "$FRPS_CONFIG" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
updated, count = re.subn(
    r"(?ms)^allowPorts\s*=\s*\[.*?\]",
    "allowPorts = [{ start = 33001, end = 33016 }]",
    text,
)
if count != 1:
    raise SystemExit(f"allowPorts block: expected 1, found {count}")
path.write_text(updated, encoding="utf-8")
PY
grep -n '^allowPorts' "$FRPS_CONFIG"

# 4. 宿主机和云安全组放行新端口。例如 ufw：
# ufw allow 33003:33016/tcp

# 5. 重建
compose up -d --force-recreate --no-build --wait node-net firewall frps agent
compose ps
docker inspect minecrafttools-hk-dev-node-net-1 \
  --format '{{range $p, $b := .HostConfig.PortBindings}}{{$p}} -> {{range $b}}{{.HostPort}} {{end}}{{"\n"}}{{end}}'
ss -H -ltn 'sport >= :33001 and sport <= :33016'
docker logs --tail 30 minecrafttools-hk-dev-agent-1 2>&1 | tail -5
```

然后回管理台：

1. 等节点 `online`，「部署范围」显示 `33001-33016`。
2. 编辑节点，允许端口改成 `33001-33016`（或容量内的子集），保存并等「生效范围」更新。现有隧道仍在 `33001-33002`，扩展不会和它们冲突。
3. 用客户端注册一次，确认分到新端口且能连。

## 换到全新范围

如果目标范围不包含旧端口（例如从 `33001-33002` 改成 `2000-2015`）：

1. 管理台先把节点设为「未发布」，确认存活隧道和大厅房间下线。
2. 按上面的步骤改容量，把 `NODE_PORT_*` 和 `allowPorts` 换成新范围。
3. 等节点上线后，把允许端口改成新范围，等 Agent 确认。
4. 发布通道改回 `dev`，重新验证注册和大厅。
5. 旧客户端大厅需要时，在 Phoenix 的 `deploy.env` 更新 `CONTROL_LOBBY_TARGETS` 并重启 control。

## 回滚

```sh
cp -a "deploy.env.before-ports-$stamp" deploy.env
cp -a "$FRPS_CONFIG.before-ports-$stamp" "$FRPS_CONFIG"
compose up -d --force-recreate --no-build --wait node-net firewall frps agent
```

回滚同样会断开隧道。回滚后在管理台把允许端口改回旧范围。

## 默认值

`node/compose.yaml` 的默认容量是 `30000-30015`（16 个）。已有节点的 `.env` 优先，改默认值不会自动迁移；按上面的步骤单独扩容。

新节点可直接使用 [Agent 部署脚本](../deploy/agent/README.md) 输入多段端口，脚本会同时生成 Docker 端口映射、`frps.toml` 的 `allowPorts` 和 `NODE_PORT_START/END`（取区段最小/最大）。区段之间的空隙不会出现在 Agent 上报的部署范围里。扩容或改端口后仍需按本文顺序在管理台调整允许端口和发布状态。
