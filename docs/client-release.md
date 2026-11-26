# 客户端更新与节点构建计划

本页记录自动更新和发布的实现及验收目标。当前构建操作见[构建说明](build.md)。真实 MinecraftTools 客户端和 Launcher 已通过本地启动与诊断联调，并在 Phoenix 独立联调实例通过 macOS arm64 的签名更新和回滚测试。 [Launcher 说明](../launcher/README.md)

## 旧版更新器源码复核

重构前的 Windows 安装包将 `src_launcher/launcher.py` 打包为 `launcher.exe`，快捷方式指向它。代码的执行顺序如下。

1. 读取 Inno Setup 写入的 `Documents/MitaHillFRP/install_info.json`，获取安装路径、主程序路径、版本和通道。缺省版本是 `0.0.0`，缺省通道是 `stable`。
2. 优先处理 `pending_update`。有 SHA-256 时校验安装包，校验失败会尝试清理文件和任务。缺少摘要时仍允许继续。通常会弹窗询问是否安装，调用安装程序后立即清除任务，安装结果由安装程序处理。
3. 没有启动安装程序时，按记录或候选路径启动主程序，观察进程是否在五秒内退出。启动失败时记录日志并尝试显示 Windows 错误提示。
4. 主程序启动后，请求旧服务的 `version.json`。`stable` 选择稳定版，`dev` 比较两边版本，可能选中更新的 `stable`。旧格式的单条版本记录也会直接参与判断。版本比较按点分数字处理，无法用于当前仓库的 `yyyy-mm-dd-n`。
5. 发现目标版本更新时，将 Windows 安装包流式下载到用户目录，发送进度通知，并把路径、版本、通道和摘要写进 `pending_update`，留到下一次启动处理。目标版本相等时清理下载目录中的旧安装包。本地版本更高时跳过更新。

备份里还有两条独立路径。`src_updater/main.py` 接收已下载的新 EXE，等待旧进程和文件锁，尝试备份、替换、失败回滚和重启，使用 PySide6 和 `psutil`。`src_updater/launcher.py` 是另一套 PySide6 启动器，会从单条版本记录检查并下载 Windows EXE，用户确认后直接覆盖并启动。这两个入口均未进入当前仓库的六平台构建。

当前仓库的 Launcher 已随桌面安装包一起分发：CI 用 `client/scripts/package_desktop.py` 把 Launcher、Main App 更新包和 bootstrap 元数据打进 MSI、DMG 或 `.run`。现有 CI 中的 `fetch_frpc.py` 用于构建阶段获取上游 `frpc`。

## 主体程序和小型更新器

- **主体程序**负责映射、`frpc` 和大厅，启动时从 Control Server 拉取线路清单。
- **Launcher** 是发布包的正常启动入口，负责网络检查、更新、启动主体程序及启动失败诊断。使用独立的本机可执行文件，优先选标准库和必要的系统 API。用户机器无需额外安装 Python、Qt 等运行环境。

### 启动和强制更新

1. 显示 Launcher 小窗口，检查本机网络。明确断网时弹窗报错，关闭提示后退出，不发送日志。网络状态不明时交给总控制端连通测试判断。
2. 在本地记录设备和系统摘要，包括操作系统名称、系统版本、架构和 Launcher 版本，供启动失败时诊断。
3. 请求总控制端的 HTTPS 健康检查接口。每次请求设超时。首次失败后最多重试 10 次，每次重试前等待 3 秒。成功即继续，全部失败时弹窗报错，关闭提示后退出，不发送日志。
4. 读取本机所选更新通道和实际安装版本，按下文规则检查更新。安装信息损坏时显示修复提示。找到更新就显示进度，在本次启动中完成下载、校验和安装。确认无更新则继续启动当前版本。检查、下载、校验和安装期间窗口保持响应，主体程序等待。替换文件前等待同一安装下运行中的主体程序正常退出。
5. 启动主体程序，等待就绪信号。信号有效时关闭 Launcher。进程无法创建、就绪前退出或启动超时时，进入启动失败诊断。

更新源检查失败时显示错误并退出，不发送日志。下载、校验或安装失败时显示错误，提供「重试」和「退出」。已确认的必更状态和可恢复的旧安装会保留，重新打开 Launcher 后继续更新。

### dev 和 stable 的更新规则

本机所选通道和实际安装的构建分别记录。所有候选产物都要匹配当前操作系统与架构，并与**实际安装的版本号**比较。`yyyy-mm-dd-n` 按日期、当天序号的顺序比较。

| 本机所选通道 | 检查结果 | 处理方式 |
| --- | --- | --- |
| `stable` | stable 有更高版本 | 安装 stable |
| `dev` | dev 有更高版本 | 优先安装 dev，即使 stable 版本更高 |
| `dev` | 已确认 dev 无更高版本，stable 有更高版本 | 安装 stable，保留本机的 `dev` 选择 |
| 任一通道 | 按规则检查完毕，没有更高版本 | 启动当前版本 |

dev 查询失败时结束本次检查。只有确认 dev 无更高版本时才检查 stable。切到 stable 后，下次启动依旧先查 dev，等 dev 版本号高于已安装的 stable 才切回。MainApp 用实际安装构建的通道请求线路。[节点清单与端口分配](node-catalog.md)

### 主体程序就绪与 30 秒超时

Launcher 启动主体程序前，先在 `127.0.0.1` 的临时端口监听，通过子进程环境传入端口和一次性随机值。主体程序完成所需初始化且主窗口可用后，连回该端口发送 `READY` 和随机值。Launcher 收到并验证信号后关闭监听和小窗口，然后退出。[Rust TCP 监听](https://doc.rust-lang.org/std/net/struct.TcpListener.html) · [Python 套接字](https://docs.python.org/3/library/socket.html)

从主体进程成功创建时开始计时，**30 秒内未收到有效的 `READY` 就自动弹窗报错**，进入启动失败诊断。30 秒只用于等待主体程序就绪。进程无法创建或在发送 `READY` 前退出时，立即报错。

### 启动失败诊断

- 启动主体程序失败后，先保存本地诊断，再自动通过 HTTPS 上报到拟新增的总控制端诊断接口。报告仅包含所选通道、实际构建版本与通道、系统与架构、失败阶段、可用的退出码和经过脱敏的相关日志片段。
- 窗口显示错误摘要、日志位置和上报状态。发送有独立的超时，失败时保留本地报告。

### 客户端数据目录

配置、安装状态和日志保存在用户数据目录，更新时只替换程序文件。`settings.json` 保存用户偏好。Launcher 管理 `install.json`，记录所选更新通道、实际安装的构建通道与版本。Launcher 和主体程序分别写入 `launcher.log` 和 `client.log`。

| 系统 | 配置和安装状态 | 日志 | 可清理的更新缓存 |
| --- | --- | --- | --- |
| Windows | `%LOCALAPPDATA%\MitaHill\MinecraftTools\` | 同根目录的 `logs\` | 同根目录的 `cache\` |
| macOS | `~/Library/Application Support/com.mitahill.minecrafttools/` | `~/Library/Logs/com.mitahill.minecrafttools/` | `~/Library/Caches/com.mitahill.minecrafttools/` |
| Linux | 配置用 `$XDG_CONFIG_HOME/minecrafttools/`，安装状态用 `$XDG_STATE_HOME/minecrafttools/` | `$XDG_STATE_HOME/minecrafttools/logs/` | `$XDG_CACHE_HOME/minecrafttools/` |

Linux 未设置对应变量时，依次使用 `~/.config`、`~/.local/state` 和 `~/.cache`。更新临时文件进入缓存，恢复安装所需的状态单独保存。[Windows Known Folders](https://learn.microsoft.com/windows/win32/shell/knownfolderid) · [macOS 应用数据目录](https://developer.apple.com/library/archive/documentation/FileManagement/Conceptual/FileSystemProgrammingGuide/AccessingFilesandDirectories/AccessingFilesandDirectories.html) · [XDG 目录规范](https://specifications.freedesktop.org/basedir/)

### 开屏窗口

Launcher 用一个无边框窗口显示现有开屏图，窗口随实际启动进度打开和关闭。主体程序初始化完成后直接显示主窗口并发送 `READY`。[开屏图](../client/src/minecrafttools/MinecraftToolsStartupLogo.png)

开屏图右下角叠一行右对齐的白色小字，随启动阶段更新，例如「检查网络」「尝试连接服务器」「检查更新」「下载更新 42%」「正在启动」。文字与图边留出内距，遇到亮色纹理时加局部深色衬底，保持小字清楚。

首选 **Rust + Slint**，启用 Winit 和软件渲染，仅编译所需特性。检查时可显示不定进度，拿到下载总量后显示实际百分比。发生错误时在同一窗口显示错误摘要及适用的「重试」「退出」按钮。网络和文件处理放到工作线程，界面线程处理绘制和按钮事件。[Slint Winit 后端](https://docs.slint.dev/latest/docs/slint/guide/backends-and-renderers/backend_winit/) · [进度条控件](https://docs.slint.dev/latest/docs/slint/reference/std-widgets/basic-widgets/progressindicator/)

在六种系统与架构组合实测发布体积、冷启动、中文显示和系统依赖。重点验证 Windows ARM64 原生运行及 Linux 的 X11、Wayland。若采用 Slint 桌面免版税许可，将署名放在下载页上。[Slint Windows 支持](https://docs.slint.dev/latest/docs/slint/guide/platforms/desktop/windows/general/) · [Slint 许可条款](https://slint.dev/terms-and-conditions)

### 安装可靠性

- 使用单实例锁、明确的网络超时和有限重试。用更新器内置的发布公钥验证元数据签名，下载到独立的临时位置，核对包大小和 SHA-256 后安装。元数据绑定通道、版本、操作系统及架构。
- 在可写的用户安装位置暂存完整新版，再切换应用目录或应用包。保留可恢复的旧文件，直到新版本通过启动检查。中途退出、掉电或启动失败时，下一次运行能够恢复到完整安装。用户设置和日志独立保存。
- Windows 验证进程退出和文件锁处理。macOS 验证完整 `.app` 的替换、签名和公证。Linux 验证架构、执行权限和完整目录切换。六种系统与架构组合都要验证断网退出且无上报、dev 回退后保留所选通道、包损坏、磁盘空间不足、文件占用、重复启动、中途退出后的恢复，以及主体程序就绪、提前退出或 30 秒超时时的处理。
- CI 应报告六种安装包和更新包的大小，体积上限根据实测结果确定。

### 签名更新的实施与验收

1. 在 `dev` 和 `stable` 签名清单中绑定通道、生成时间、有效期、平台、版本、大小与 SHA-256。外层记录 `schema`、`key_id`、`payload`、`signature`。`payload` 为原始 UTF-8 JSON 的 Base64URL 编码，Ed25519 签名覆盖原始字节。Launcher 用内置公钥按 `key_id` 验证，清单签名或内容校验失败视为更新源不可用。
2. 先构建六平台更新包并校验下载内容，再生成并签署通道清单。托管侧先上传完整版本文件，验证路径与校验值之后才发布清单。空的通道也提供已签名的清单，以便 Launcher 区分暂无更新和读取失败。
3. Launcher 把通过校验的包展开到用户安装目录中的独立版本目录。解包时校验路径范围，macOS 保留 `.app` 内部的有效相对符号链接。安装状态在新主体发出 `READY` 后原子提交。启动失败时用之前的完整版本恢复，清理失败候选并保存诊断。启动器进程与主体程序的版本目录分开。
4. 首先用隔离测试签名密钥验证候选版本安装、签名错误、下载损坏、安装中断和 `READY` 超时。随后接入 `dev` 和 `stable` 正式签名与六平台 CI，用各原生安装入口验证首次运行、更新成功及回滚。

2026-11-25 在 Phoenix 独立联调实例使用测试密钥完成 macOS arm64 验收。Launcher 验证空的 dev 清单后回退到 stable，下载完整 `.app`，保留包内符号链接，收到 `READY` 后才写入安装状态。随后发布一个签名有效、启动后故意退出的 dev 测试候选。Launcher 恢复同一个 stable 版本，显示回滚结果，上报诊断，并清除失败候选。联调结束后恢复空的 dev 清单。Windows、Linux、macOS amd64 和正式签名、公证仍需原生验收。

## GitHub Actions 发布与节点写入

### 发布通道

- 沿用 `yyyy-mm-dd-n` 作为安装版本号。拟保留 `stable` 的现有日期标签，`dev` 使用 `dev-yyyy-mm-dd-n` 标签并发为 GitHub 预发布。按通道筛选发布记录与已验证的元数据。GitHub 的 `/releases/latest` 排除预发布，无法用于查找 `dev`。[GitHub Releases API](https://docs.github.com/en/rest/releases/releases#get-the-latest-release)
- 每个通道的发布都构建 Windows、macOS、Linux 的 amd64 和 arm64 产物。六组产物、配套元数据及基本安装和更新测试通过后，版本才对更新器可见。日常测试构建仅使用测试节点并生成 CI 产物。

### CI/CD 与桌面安装包

`Client build` 在 PR 和 `main` 上构建并测试客户端与 Launcher；Control Server 和 FRP Agent 的测试随 `Server build` 在 `control/`、`node/` 变更时运行。两者按路径各自触发，发布标签会同时运行。桌面端在六个原生目标上编译 Rust Launcher，以 PyInstaller `--onedir` 打包 Main App，再把两者组装为一个安装产物。安装后的应用入口指向 Launcher，冒烟测试从该入口启动并验证 Main App 发出的 `READY`。上述流程已由 `.github/workflows/client.yml` 的 `build`、`installers` 和 `release` 三个作业实现：标签构建用 `client/scripts/package_desktop.py` 生成安装包，用 `--smoke-test --local-fixture` 从安装后的入口冒烟，Release 提供 6 个桌面压缩包、6 个更新包和 6 个安装包。macOS 暂用 ad-hoc 签名，Windows 未做 Authenticode；正式签名与公证仍需 Developer ID 证书和 Apple 公证凭据。

| 平台 | 首版拟用格式 | 安装方式 |
| --- | --- | --- |
| Windows | WiX `.msi` | 按当前用户安装，支持开始菜单和系统卸载。MSI 内部使用与日期版本排序一致的三段数字版本。 |
| macOS | `.dmg` | 拖入包含两个组件的 `MinecraftTools.app`。正式版对应用签名并做 Apple 公证。 |
| Linux | Makeself `.run` | 安装到 `${XDG_DATA_HOME:-~/.local/share}/minecrafttools`，建立 `~/.local/bin/minecrafttools` 和桌面菜单入口，安装目录内提供 `uninstall.sh`。首版以 Ubuntu 24.04 为验证基线。 |

发布标签通过 CI 后，GitHub Release 提供六个安装包、自动更新包和校验文件。首版由 Launcher 自动更新 Main App，Launcher 自身通过完整安装包升级。Control Server 和 FRP Agent 各构建并测试一个覆盖 `linux/amd64` 和 `linux/arm64` 的镜像，再以同一个 `yyyy-mm-dd-n` 标签推送到 [Docker Hub 镜像仓库](build.md#docker-hub-镜像)。该流程已由 `.github/workflows/images.yml` 实现，标签推送时推送双架构镜像，`main` 推送和 PR 只在 `control/`、`node/` 变更时做单平台构建；单元测试与 lint 也在同一工作流里。

Phoenix 托管更新时，上传作业在构建和验证通过后传输发布文件：先把该通道的 18 个发布文件（6 个更新包、6 个桌面压缩包、6 个安装包）上传到 `DEPLOY_SSH_DIRECTORY/<channel>/artifacts/`，在服务器上核对 SHA-256，再原子切换 `DEPLOY_SSH_DIRECTORY/<channel>.json`，最后删除该通道旧版本的文件。清单里的更新包 URL 指向 `MINECRAFTTOOLS_CONTROL_URL/releases/artifacts/<channel>/`，GitHub Release 只作归档和回滚来源。GitHub 的环境名称、变量和目前的配置状态见[发布环境与凭据](build.md#发布环境与凭据)。

### 发布验收矩阵

本轮基线为 `99f9ee8` 的 `dev-2026-11-25-3`。六个安装作业、发布和清单部署均已完成。镜像工作流也已发布 Control Server 与 FRP Agent 双架构镜像。[桌面 CI](https://github.com/MitaHill/Minecraft-Tools/actions/runs/45651994167) · [镜像 CI](https://github.com/MitaHill/Minecraft-Tools/actions/runs/45651994121) · [预发布](https://github.com/MitaHill/Minecraft-Tools/releases/tag/dev-2026-11-25-3)

| 验收项 | 平台 | 通过条件 |
| --- | --- | --- |
| 安装入口 | Windows、macOS、Linux 的 amd64 和 arm64 | 校验 CI 产物，按平台安装，从系统入口启动 Launcher 并观察主体 `READY`。记录系统版本、产物 SHA-256 与启动日志。 |
| 真实在线更新 | 同上 | 在隔离数据目录准备旧版，从已安装的 Launcher 请求 Phoenix 的签名清单并从 Control Server 下载更新包。检查平台匹配、大小和 SHA-256，收到 `READY` 后再提交新版本状态。 |
| 失败回滚 | 同上 | 使用独立测试签名和隔离 Control Server，验证损坏的更新包以及签名有效但启动失败的候选。旧版仍能启动，版本指针保持原值，候选目录被清理，诊断可查询。 |
| 系统安装生命周期 | 同上 | 验证安装入口、再次安装和移除应用。核对 Windows 开始菜单和 MSI 卸载、macOS 应用包、Linux 桌面入口和卸载脚本。 |
| 正式签名 | macOS 两种架构 | 对实际 DMG 验证 Developer ID、公证票据和 Gatekeeper。 |

2026-11-26 的复验使用 Actions 安装产物和 Phoeix 公开 HTTPS 入口。在线 dev 清单与 CI 产物逐字节一致。上一个版本的安装包来自 run `45650152532` 对应平台通过的安装作业。

- **macOS arm64（26.5.1）**。校验新 DMG 与更新包的 SHA-256，通过应用包深度签名检查。在独立状态目录从旧 DMG 启动 `2026-11-25-2`，用同一个 Launcher 在 Phoeix 查询 dev，下载 GitHub 上 40,100,608 字节的更新包，收到 `READY` 后切换到 `2026-11-25-3`。初次下载响应超时，重新运行后成功。通过更新后客户端界面验证大厅刷新及空列表。
- **Linux arm64（Ubuntu 24.04 容器）**。使用普通用户和 Xvfb。旧 `.run` 安装后由用户入口启动，在线更新下载 76,300,235 字节的包，并在 `READY` 后切换到 `2026-11-25-3`。通过新 `.run` 重装并启动。卸载后安装目录、命令入口与桌面入口均已移除，独立运行状态仍可读取。
- **macOS amd64（26.5.1，Rosetta）**。旧 DMG 首启后下载 45,250,900 字节的 GitHub 更新包，收到 `READY` 后切换到目标版本。新 DMG 使用该状态再次启动成功。
- **Linux amd64（Ubuntu 24.04 容器，Rosetta）**。用普通用户和 Xvfb 验证安装、在线下载 80,681,310 字节的更新、重装及卸载，独立运行状态可读取。
- **Windows amd64 和 arm64**。Actions 的两个原生作业通过 MSI 安装、启动及卸载，下载的 MSI 均与 Release 的 SHA-256 一致。本轮本地 Windows 11 ARM 虚拟机在 MSI 验收期间进入挂起，真实在线更新和交互验证仍待补测。
- **macOS arm64 隔离回滚**。在测试签名源分别提供有效签名但损坏的压缩包，以及在 `READY` 前以 37 退出的完整候选。版本状态和旧应用包保留，失败候选和下载缓存被清理。候选退出后实际 CI 主体重新启动并报告 `READY`，测试服务收到诊断。

### 节点发布

在 Control Server 管理台为节点选择发布通道。MainApp 启动时读取清单，点击「启动映射」时由 Control Server 分配端口。[节点清单与端口分配](node-catalog.md)
