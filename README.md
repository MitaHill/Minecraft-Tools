# MinecraftFRP

在 Windows、macOS 和 Linux 上把本地 Minecraft TCP 端口映射到 FRP 节点。

## 当前版本

1. 从 GitHub Release 下载对应系统和架构的压缩包，解压后打开 MinecraftFRP。
2. 复制 [节点样例](client/nodes.example.toml)，填写节点地址、FRP 控制端口和所需 token，在「设置」中载入这份 TOML。
3. 在「映射」页选择线路和端口。映射成功后复制联机地址，也可选择发布到联机大厅。远程端口须在服务端允许的范围内。

`client/` 是桌面客户端，`server/` 是原 FastAPI 服务，`tools/admin/` 是独立管理工具。开发和发版见 [构建说明](docs/build.md)。

## 下一阶段

客户端将由主体程序和独立的 Launcher 组成。Launcher 先检查网络和总控制端，再检查更新。`dev` 优先更新 dev，也可在没有更新的 dev 版本时安装更高版本的 stable。有更新就当次下载安装，随后启动主体程序。主体程序须在 30 秒内报告就绪，启动失败时保存诊断并自动上报。断网或总控制端连通测试失败时，Launcher 报错退出，不上传日志。

主体程序使用 GitHub Actions 构建时写入的节点清单，设置页将移除节点导入入口。设计和验收目标见 [客户端更新与节点构建计划](docs/client-release.md)。
