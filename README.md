# MinecraftTools

在 Windows、macOS 和 Linux 上把本地 Minecraft TCP 端口映射到 FRP 节点。

## 当前客户端

1. 解压对应系统和架构的压缩包，打开 MinecraftTools。客户端默认没有节点或大厅服务。
2. 配好自己的 FRP 节点后，复制 [节点样例](client/nodes.example.toml)，填写节点地址、FRP 控制端口和所需 token，在「设置」中载入 TOML。节点配置了 `allowPorts` 时，可以用 `remote_port_start` 和 `remote_port_end` 同步限制客户端可选的远程端口。
3. 在「映射」页选择线路和端口。映射成功后复制联机地址。设置 `MINECRAFTTOOLS_LOBBY_URL` 后可使用联机大厅。

连接受控节点时，在节点 TOML 中填写 Control Server 登记的 `node_id` 和允许端口范围。由 Launcher 启动客户端时会传入 Control Server 地址。frpc 默认使用系统 CA 校验该节点，使用内部 CA 时填写 `tls_ca_file`。

`client/` 是桌面客户端，`server/` 是原 FastAPI 服务，`tools/admin/` 是独立管理工具。开发和发版见 [构建说明](docs/build.md)。

仓库根目录的 `./dev.sh` 把编译缓存、虚拟环境和打包产物放到 `/tmp/minecrafttools-build`，项目树里不留 `target/`、`.venv/`、`build/`、`dist/`，直接套在命令前面即可，例如 `./dev.sh uv sync --project client --extra build`。

在 `launcher/` 运行 `cargo run --bin launcher -- --demo` 可体验模拟更新流程（也可以 `./dev.sh cargo run --manifest-path launcher/Cargo.toml --bin launcher -- --demo`）。Launcher 也可以连接本地测试服务并启动真实的 MinecraftTools，见 [Launcher 原型](launcher/README.md)。

`control/` 是新的 Control Server。Launcher 启动、旧大厅、管理台和节点封禁已通过本地与公网联调。见[本地联调说明](control/README.md)和[分阶段实施](docs/control-server.md#13-分阶段实施)。

## 下一阶段

客户端将由主体程序和独立的 Launcher 组成。Launcher 先检查网络和总控制端，再检查更新。`dev` 优先更新 dev，也可在没有更新的 dev 版本时安装更高版本的 stable。有更新就当次下载安装，随后启动主体程序。主体程序须在 30 秒内报告就绪，启动失败时保存诊断并自动上报。断网或总控制端连通测试失败时，Launcher 报错退出，不上传日志。

主体程序使用 GitHub Actions 构建时写入的节点清单，设置页将移除节点导入入口。设计和验收目标见 [客户端更新与节点构建计划](docs/client-release.md)。

## 许可证

本项目使用 [GNU General Public License v3.0](LICENSE)。
