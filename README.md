# MinecraftTools

在 Windows、macOS 和 Linux 上把本地 Minecraft TCP 端口映射到 FRP 节点。

## 当前客户端

以下步骤适用于从当前源码构建的安装包。已发布的 `dev-2026-11-25-3` 仍在设置页载入节点 TOML。

1. 使用对应系统和架构的安装包，从系统应用入口打开 MinecraftTools。
2. 在「映射」页选择安装包内置的线路和端口。映射成功后复制联机地址。
3. 从 Launcher 打开的客户端会自动访问总控制端的联机大厅。

自建节点可以基于 [节点样例](client/nodes.example.toml) 生成安装包。连接受控节点时，在构建用 TOML 中填写 Control Server 登记的 `node_id` 和与 frps `allowPorts` 一致的远程端口范围。当前原型的 frpc 使用普通 TCP 连接 frps。

`client/` 是桌面客户端，`server/` 是原 FastAPI 服务，`tools/admin/` 是独立管理工具。开发和发版见 [构建说明](docs/build.md)。

仓库根目录的 `./dev.sh` 把编译缓存、虚拟环境和打包产物放到 `/tmp/minecrafttools-build`，项目树里不留 `target/`、`.venv/`、`build/`、`dist/`，直接套在命令前面即可，例如 `./dev.sh uv sync --project client --extra build`。

在 `launcher/` 运行 `cargo run --bin launcher -- --demo` 可体验模拟更新流程（也可以 `./dev.sh cargo run --manifest-path launcher/Cargo.toml --bin launcher -- --demo`）。Launcher 也可以连接本地测试服务并启动真实的 MinecraftTools，见 [Launcher 原型](launcher/README.md)。

`control/` 是新的 Control Server。Launcher 启动、旧大厅、管理台和节点封禁已通过本地与公网联调。见[本地联调说明](control/README.md)和[分阶段实施](docs/control-server.md#13-分阶段实施)。

## 下一阶段

客户端由主体程序和独立的 Launcher 组成，两者打进同一个安装包。Launcher 先检查网络和总控制端，再检查更新。`dev` 优先更新 dev，也可在没有更新的 dev 版本时安装更高版本的 stable。有更新就当次下载安装，随后启动主体程序。主体程序须在 30 秒内报告就绪，启动失败时保存诊断并自动上报。断网或总控制端连通测试失败时，Launcher 报错退出，不上传日志。这部分已经实现并纳入 CI：标签构建产出 MSI、DMG、`.run` 和更新包，并从安装入口冒烟。

控制端 HTTPS 入口是 `https://ctl.mctools.mitahill.com`。发布版本使用对应通道的内置节点目录。各平台的复验范围和 macOS 正式签名与公证进度见 [客户端验收矩阵](docs/client-release.md#发布验收矩阵)。

## 许可证

本项目使用 [GNU General Public License v3.0](LICENSE)。
