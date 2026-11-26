# 节点清单与端口分配

## MainApp 启动

MainApp 启动时请求 `GET /api/v1/node-catalog?channel=stable`。开发构建请求 `channel=dev`，以实际安装的构建通道为准。Launcher 继续负责程序更新和启动。

- `200` 返回 `nodes`（节点 ID 和名称）、`updated_at`（节点目录的 Unix 毫秒更新时间）、`min_frpc_version` 和 `blocked: false`。MainApp 校验响应后才允许选择线路和启动映射。低于最低版本时提示更新 MinecraftTools。
- `403` 返回 `detail.code = ip_blacklisted`、`detail.blocked = true` 和 `detail.reason`。MainApp 显示封禁理由，禁用映射。响应不含节点或连接信息。
- 清单按控制端和通道在用户数据目录中持久化。配置变化时原子替换，空清单也会替换旧缓存。请求失败时可显示缓存中的线路名称，重试成功后才能启动映射。封禁时界面清空线路。

## 启动映射

1. 用户选节点并填写本地端口。已有安装身份时，MainApp 只发一次 `POST /api/v1/tunnels/register`，请求体只有 `node_id`。
2. Control Server 校验客户端 IP 和节点状态，在生效范围内排除占用、预留和封禁的端口。选端口和写预留发生在同一个事务。没有空闲端口时返回 `409`，错误代码为 `no_available_port`。
3. 成功时返回 `remote_port`、隧道凭证、frpc 连接主机及端口和玩家访问主机。需要 frps 登录令牌的线路也在此时返回可下发给客户端的令牌。MainApp 用分配结果启动 frpc，确认映射成功后显示可分享地址。失败或停止时释放预留，请求中断留下的预留会到期。

旧客户端仍可请求 `GET /api/v1/nodes`，并在注册隧道时指定 `remote_port`。两个清单接口都会检查 IP 黑名单。

## 管理与上线

- 在 Control Server 管理台的「节点」页面选择发布通道，并填写节点对外开放的 frps 连接端口。连接主机留空时使用节点的公网主机。已有节点升级后先显示为未发布，可逐个配置并发布。选择「未发布」会把节点从新客户端清单移除，并停止接受该节点的新自动分配请求。
- 玩家访问主机与 frpc 连接主机可分别配置。存在实时映射、预留或封禁记录时，公网主机变更仍遵守后台原有约束。已部署的 frps `allowPorts` 和 Agent 策略继续决定服务端可分配的范围。
- 需要 frps 登录令牌时，填写可下发给客户端的 frps 登录令牌。Agent 注册密钥仅供节点与控制端通信。
- `CONTROL_MIN_FRPC_VERSION` 在 Control Server 上配置，默认 `0.72.0`。发布前确认软件所带的 frpc 版本满足要求。
- 先升级 Control Server 并在各通道配置线路，确认清单和端口预留后再发布新版 MainApp。
