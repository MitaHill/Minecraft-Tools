import argparse
import logging
import os
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from PySide6.QtCore import QProcess, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplashScreen,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from minecrafttools.config import TunnelReservation, write_frpc_config
from minecrafttools.control import TunnelControl, blacklist_message, control_base
from minecrafttools.lobby import LobbyTab, room_payload
from minecrafttools.node_catalog import NodeCatalog, version_tuple
from minecrafttools.styles import STYLE

ANNOUNCEMENTS = QUrl("https://b.clash.ink/archives/mi-ta-shan-lian-ji-gong-ju")
LEGACY_BLOCK_REASONS = {"no_minecraft_status": "未检测到 Minecraft Java 响应。"}


def blocked_message(reason: object) -> str:
    if not isinstance(reason, str):
        return "隧道已封禁。"
    message = LEGACY_BLOCK_REASONS.get(reason, reason).strip()[:200]
    return f"隧道已封禁。{message}"


def client_settings() -> QSettings:
    if data_root := os.environ.get("MINECRAFTTOOLS_DATA_ROOT"):
        return QSettings(str(Path(data_root) / "settings.ini"), QSettings.Format.IniFormat)

    settings = QSettings()
    previous = QSettings("MitaHill", "MinecraftFRP")
    if not settings.contains("theme") and previous.contains("theme"):
        settings.setValue("theme", previous.value("theme"))
    return settings


class MappingWindow(QWidget):
    def __init__(self, frpc: Path, frpc_version: tuple[int, int, int] | None):
        super().__init__()
        self.setWindowTitle("MinecraftTools")
        self.resize(378, 550)
        self.frpc = frpc
        self.frpc_version = frpc_version
        self.catalog_ready = False
        self.settings = client_settings()
        self.config_dir: TemporaryDirectory | None = None
        self.output = bytearray()
        self.failure = ""
        self.address = ""
        self.stopping = False
        self.registering = False
        self.closing = False
        self.reservation: TunnelReservation | None = None
        self.publication_removed = False
        self.status_pending = False
        self.renew_at = 0.0
        self.control = TunnelControl(self, control_base())

        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.finished.connect(self.mapping_finished)
        self.process.errorOccurred.connect(self.process_error)

        self.start_timeout = QTimer(self)
        self.start_timeout.setSingleShot(True)
        self.start_timeout.timeout.connect(self.mapping_timed_out)
        self.stop_timeout = QTimer(self)
        self.stop_timeout.setSingleShot(True)
        self.stop_timeout.timeout.connect(self.process.kill)
        self.status_timer = QTimer(self)
        self.status_timer.setInterval(5000)
        self.status_timer.timeout.connect(self.poll_tunnel)

        self.node = QComboBox()
        self.node.setEnabled(False)
        self.local_port = QSpinBox()
        self.local_port.setRange(1, 65535)
        self.local_port.setValue(25565)
        self.start_button = QPushButton("启动映射")
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self.toggle_mapping)
        self.retry_catalog = QPushButton("重试读取线路")
        self.retry_catalog.clicked.connect(self.refresh_catalog)
        self.retry_catalog.hide()
        self.status = QLabel("正在读取线路清单…")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.link = QLineEdit()
        self.link.setReadOnly(True)
        self.copy_button = QPushButton("复制链接")
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(lambda: QApplication.clipboard().setText(self.link.text()))

        self.mapping_log = QPlainTextEdit()
        self.mapping_log.setReadOnly(True)
        self.mapping_log.setMaximumBlockCount(80)
        self.tabs = QTabWidget()
        self.lobby = LobbyTab(
            self.control.base, lambda: self.control.token, self.control.channel
        )
        self.lobby.publication_status.connect(self.append_log)
        self.lobby.publication_removed.connect(self.room_was_removed)
        self.tabs.addTab(self.mapping_page(), "映射")
        self.tabs.addTab(self.lobby, "联机大厅")
        self.tabs.addTab(self.settings_page(), "设置")
        self.announcements_index = self.tabs.addTab(self.announcements_page(), "线上公告")
        QVBoxLayout(self).addWidget(self.tabs)

    def mapping_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)
        servers = QHBoxLayout()
        help_button = QPushButton("帮助")
        help_button.clicked.connect(lambda: self.tabs.setCurrentIndex(self.announcements_index))
        servers.addWidget(help_button)
        servers.addWidget(QLabel("选择线路:"))
        servers.addWidget(self.node, 1)
        layout.addLayout(servers)

        ports = QFormLayout()
        ports.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        ports.addRow("输入本地端口:", self.local_port)
        layout.addLayout(ports)
        self.public_room = QGroupBox("发布到联机大厅")
        self.public_room.setCheckable(True)
        self.public_room.setChecked(False)
        self.public_room.setEnabled(bool(self.lobby.api))
        room = QFormLayout(self.public_room)
        room.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.room_name = QLineEdit()
        self.room_name.setMaxLength(50)
        self.room_name.setPlaceholderText("我的 Minecraft 服务器")
        self.room_description = QLineEdit()
        self.room_description.setMaxLength(200)
        self.room_description.setPlaceholderText("简短的介绍（可选）")
        self.room_players = QSpinBox()
        self.room_players.setRange(1, 100)
        self.room_players.setValue(10)
        room.addRow("房间名称:", self.room_name)
        room.addRow("房间介绍:", self.room_description)
        room.addRow("最大人数:", self.room_players)
        layout.addWidget(self.public_room)

        buttons = QHBoxLayout()
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.copy_button)
        self.republish = QPushButton("重新发布房间")
        self.republish.clicked.connect(self.publish_room)
        self.republish.hide()
        buttons.addWidget(self.republish)
        layout.addLayout(buttons)
        layout.addWidget(QLabel("运行日志:"))
        layout.addWidget(self.mapping_log, 1)
        address = QFormLayout()
        address.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        address.addRow("映射地址:", self.link)
        layout.addLayout(address)
        layout.addWidget(self.status)
        layout.addWidget(self.retry_catalog)
        return page

    def settings_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(15, 15, 15, 15)
        theme = QComboBox()
        theme.addItems(["自动", "昼间", "夜间"])
        selected = self.settings.value("theme", 0, type=int)
        theme.setCurrentIndex(max(0, min(2, selected)))
        self.apply_theme(theme.currentIndex())
        theme.currentIndexChanged.connect(self.apply_theme)
        row = QFormLayout()
        row.addRow("主题:", theme)
        layout.addLayout(row)
        layout.addStretch()
        return page

    def apply_theme(self, index: int):
        self.settings.setValue("theme", index)
        dark = index == 2 or (index == 0 and not 7 <= datetime.now().hour < 18)
        self.setStyleSheet(STYLE["dark" if dark else "light"])

    def announcements_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(15, 15, 15, 15)
        button = QPushButton("打开线上公告")
        button.clicked.connect(lambda: QDesktopServices.openUrl(ANNOUNCEMENTS))
        layout.addWidget(button)
        layout.addStretch()
        return page

    def append_log(self, message: str):
        self.mapping_log.appendPlainText(f"{datetime.now():%H:%M:%S}  {message}")

    def set_status(self, message: str):
        if self.status.text() != message:
            self.status.setText(message)
            if message:
                self.append_log(message)

    def show_catalog(self, catalog: NodeCatalog | None):
        self.node.clear()
        if catalog:
            for node_id, name in catalog.nodes:
                self.node.addItem(name, node_id)
        self.node.setEnabled(self.catalog_ready and self.node.count() > 0)
        self.start_button.setEnabled(self.node.isEnabled())

    def refresh_catalog(self):
        self.catalog_ready = False
        self.node.setEnabled(False)
        self.start_button.setEnabled(False)
        self.retry_catalog.hide()
        if not self.control.base:
            self.set_status("无法连接控制端，请通过 Launcher 启动。")
            return
        self.set_status("正在读取线路清单…")
        self.control.fetch_catalog(self.catalog_received)

    def catalog_received(self, code, data):
        if self.closing:
            return
        if code == 403:
            self.show_catalog(None)
            self.set_status(blacklist_message(data) or "控制端拒绝访问线路清单。")
            return
        try:
            catalog = NodeCatalog.from_payload(data) if code == 200 else None
        except ValueError:
            catalog = None
        if catalog is None:
            cached = self.control.cached_catalog()
            self.show_catalog(cached)
            self.set_status("线路未刷新，请重试。" if cached else "无法读取线路清单，请重试。")
            self.retry_catalog.show()
            return
        self.control.cache_catalog(catalog)
        if not catalog.nodes:
            self.set_status("当前没有可用线路。")
        elif self.frpc_version is None:
            self.set_status("无法读取 frpc 版本，请检查安装是否完整。")
        elif self.frpc_version < version_tuple(catalog.min_frpc_version):
            self.set_status(f"线路需要 frpc {catalog.min_frpc_version} 或更高版本，请更新 MinecraftTools。")
        else:
            self.catalog_ready = True
            self.set_status("")
        self.show_catalog(catalog)

    def toggle_mapping(self):
        if self.registering or self.stopping or self.closing:
            return
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.stop_mapping()
            return
        node_id = self.node.currentData()
        if not self.catalog_ready or node_id is None:
            return
        if not self.frpc.is_file():
            self.set_status("找不到 frpc，请使用包含当前平台 frpc 的构建。")
            return

        self.failure = ""
        self.publication_removed = False
        self.registering = True
        self.set_mapping_controls(True)
        self.start_button.setEnabled(False)
        self.set_status("正在申请映射端口…")
        self.control.reserve(node_id, self.start_registered)

    def start_registered(self, reservation: TunnelReservation | None, error: str):
        self.registering = False
        if self.closing:
            if reservation:
                self.control.release(reservation.claim)
            return
        if reservation is None:
            self.failure = error
            self.mapping_finished()
            return
        self.reservation = reservation

        node, port, claim = reservation.node, reservation.port, reservation.claim
        try:
            self.config_dir = TemporaryDirectory(prefix="minecrafttools-")
            config = Path(self.config_dir.name) / "frpc.toml"
            write_frpc_config(config, node, self.local_port.value(), port, claim)
        except (OSError, UnicodeError):
            self.failure = "无法写入临时配置。"
            self.mapping_finished()
            return
        except ValueError as error:
            self.failure = str(error)
            self.mapping_finished()
            return

        self.output.clear()
        self.address = node.address(port)
        self.set_mapping_controls(True)
        self.set_status("正在连接节点…")
        self.start_timeout.start(20_000)
        self.process.start(str(self.frpc), ["-c", str(config)])
        self.renew_at = monotonic() + 25
        self.status_timer.start()

    def poll_tunnel(self):
        reservation = self.reservation
        if not reservation or self.status_pending or self.stopping or self.closing:
            return
        self.status_pending = True
        renew = monotonic() >= self.renew_at

        def finished(code, payload):
            if self.reservation is not reservation:
                return
            self.status_pending = False
            if self.stopping or self.closing:
                return
            if code == 200 and renew:
                self.renew_at = monotonic() + 25
            if code == 200 and payload.get("status") in ("block_pending", "blocked"):
                reason = payload.get("reason")
                if payload.get("code") == "ip_blacklisted":
                    self.failure = f"客户端 IP 已被拉黑。{reason or ''}"
                else:
                    self.failure = blocked_message(reason)
            elif code in (401, 404):
                self.failure = "隧道注册已失效，请重新启动映射。"
            else:
                return
            self.stop_mapping()

        self.control.status(reservation.claim, renew, finished)

    def set_mapping_controls(self, running: bool):
        self.node.setEnabled(not running and self.catalog_ready)
        self.local_port.setEnabled(not running)
        self.public_room.setEnabled(not running and bool(self.lobby.api))
        self.start_button.setText("停止映射" if running else "启动映射")
        self.start_button.setEnabled(self.catalog_ready and self.node.count() > 0)
        if not running:
            self.republish.hide()
            self.link.clear()
            self.copy_button.setEnabled(False)

    def read_output(self):
        self.output.extend(self.process.readAllStandardOutput().data())
        while (end := self.output.find(b"\n")) != -1:
            line = self.output[:end].lower()
            del self.output[:end + 1]
            if self.stopping:
                continue
            if b"start proxy success" in line:
                self.start_timeout.stop()
                self.link.setText(self.address)
                self.copy_button.setEnabled(True)
                self.set_status("映射运行中。")
                if not self.publication_removed:
                    self.publish_room()
            elif b"start error" in line or b"login to server failed" in line:
                self.failure = "映射失败，请检查节点、认证信息和远程端口。"
                self.stop_mapping()
            elif b"try to reconnect to server" in line:
                self.link.clear()
                self.copy_button.setEnabled(False)
                self.lobby.unpublish()
                self.set_status("连接中断，正在重连…")
                self.start_timeout.start(20_000)
        if len(self.output) > 16384:
            del self.output[:-4096]

    def room_was_removed(self):
        self.publication_removed = True
        self.republish.show()

    def publish_room(self):
        reservation = self.reservation
        if not reservation or not self.public_room.isChecked() or self.lobby.active_room is not None:
            return
        node = reservation.node
        self.publication_removed = False
        self.republish.hide()
        room = room_payload(
            node.public_host or node.server_addr,
            reservation.port,
            self.room_name.text().strip(),
            self.room_description.text().strip(),
            self.room_players.value(),
        )
        if self.lobby.control_lobby:
            room["claim_id"] = reservation.claim.id
        self.lobby.publish(room)

    def mapping_timed_out(self):
        self.failure = "连接超时，请检查节点地址和网络。"
        self.stop_mapping()

    def stop_mapping(self):
        self.stopping = True
        self.start_timeout.stop()
        self.status_timer.stop()
        self.lobby.unpublish()
        self.set_status(self.failure or "正在停止映射…")
        self.link.clear()
        self.start_button.setEnabled(False)
        self.copy_button.setEnabled(False)
        self.stop_timeout.start(2000)
        self.process.terminate()

    def process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.failure = "无法启动 frpc，请检查可执行文件和系统架构。"
            self.mapping_finished()

    def mapping_finished(self, exit_code=0):
        self.lobby.unpublish()
        self.start_timeout.stop()
        self.stop_timeout.stop()
        self.status_timer.stop()
        self.status_pending = False
        reservation, self.reservation = self.reservation, None
        if reservation:
            self.control.release(reservation.claim)
        if self.config_dir is not None:
            self.config_dir.cleanup()
            self.config_dir = None
        self.set_mapping_controls(False)
        if self.failure:
            self.set_status(self.failure)
        elif exit_code and not self.stopping:
            self.set_status("frpc 已退出，请检查节点和远程端口。")
        else:
            self.set_status("映射已停止。")
        self.stopping = False

    def closeEvent(self, event: QCloseEvent):
        self.closing = True
        self.lobby.unpublish()
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()
            if not self.process.waitForFinished(2000):
                self.closing = False
                event.ignore()
                return
        self.lobby.stop_presence()
        event.accept()


def send_launcher_ready(port: str, token: str) -> None:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=1) as connection:
            connection.sendall(f"READY {token}\n".encode("ascii"))
    except (OSError, ValueError, OverflowError):
        logging.getLogger(__name__).warning("无法向 Launcher 发送就绪信号。")


def read_frpc_version(frpc: Path) -> tuple[int, int, int] | None:
    try:
        result = subprocess.run(
            [str(frpc), "--version"], check=True, capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return version_tuple(result.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frpc", type=Path)
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args()
    launcher_port = os.environ.get("MINECRAFTTOOLS_LAUNCHER_PORT")
    launcher_token = os.environ.get("MINECRAFTTOOLS_LAUNCHER_TOKEN")
    resources = Path(__file__).resolve().parent
    binary = "frpc.exe" if sys.platform == "win32" else "frpc"
    frpc = (options.frpc or resources / "bin" / binary).resolve()

    app = QApplication([sys.argv[0]])
    app.setOrganizationName("MitaHill")
    app.setApplicationName("MinecraftTools")
    app.setWindowIcon(QIcon(str(resources / "logo-macos.png")))
    artwork = QPixmap(str(resources / "MinecraftToolsStartupLogo.png"))
    frpc_version = read_frpc_version(frpc)
    if options.smoke_test and (artwork.isNull() or frpc_version is None):
        return 1
    window = MappingWindow(frpc, frpc_version)
    if options.smoke_test:
        window.show()

        def finish_smoke_test():
            if launcher_port and launcher_token:
                send_launcher_ready(launcher_port, launcher_token)
            app.quit()

        QTimer.singleShot(0, finish_smoke_test)
    elif launcher_port and launcher_token:
        window.show()
        QTimer.singleShot(0, lambda: send_launcher_ready(launcher_port, launcher_token))
    elif artwork.isNull():
        window.show()
    else:
        width = min(700, app.primaryScreen().availableGeometry().width())
        splash = QSplashScreen(artwork.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation))
        splash.show()

        def show_window():
            window.show()
            splash.finish(window)

        QTimer.singleShot(650, show_window)
    if not options.smoke_test:
        QTimer.singleShot(0, window.refresh_catalog)
    # Destroy the window while Qt can still process deferred deletion.
    app.aboutToQuit.connect(window.deleteLater)
    return app.exec()
