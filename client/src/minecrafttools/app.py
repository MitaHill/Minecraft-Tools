import argparse
import logging
import os
import random
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
    QFileDialog,
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

from minecrafttools.config import Node, TunnelClaim, load_nodes, write_frpc_config
from minecrafttools.control import TunnelControl, control_base
from minecrafttools.lobby import LobbyTab, room_payload
from minecrafttools.styles import STYLE

ANNOUNCEMENTS = QUrl("https://b.clash.ink/archives/mi-ta-shan-lian-ji-gong-ju")


def client_settings() -> QSettings:
    if data_root := os.environ.get("MINECRAFTTOOLS_DATA_ROOT"):
        return QSettings(str(Path(data_root) / "settings.ini"), QSettings.Format.IniFormat)

    settings = QSettings()
    previous = QSettings("MitaHill", "MinecraftFRP")
    for key in ("theme", "nodes_file"):
        if not settings.contains(key) and previous.contains(key):
            settings.setValue(key, previous.value(key))
    return settings


class MappingWindow(QWidget):
    def __init__(self, frpc: Path, nodes_file: Path | None):
        super().__init__()
        self.setWindowTitle("MinecraftTools")
        self.resize(400, 550)
        self.frpc = frpc
        self.settings = client_settings()
        self.config_dir: TemporaryDirectory | None = None
        self.output = bytearray()
        self.failure = ""
        self.address = ""
        self.stopping = False
        self.registering = False
        self.closing = False
        self.claim: TunnelClaim | None = None
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
        self.load_button = QPushButton("载入节点")
        self.load_button.clicked.connect(self.choose_nodes)
        self.local_port = QSpinBox()
        self.local_port.setRange(1, 65535)
        self.local_port.setValue(25565)
        self.remote_port = QSpinBox()
        self.remote_port.setRange(1, 65535)
        self.remote_port.setValue(random.randint(10000, 65534))
        self.node.currentIndexChanged.connect(self.update_remote_ports)
        self.start_button = QPushButton("启动映射")
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self.toggle_mapping)
        self.status = QLabel("载入节点配置后即可启动映射。")
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
        self.lobby = LobbyTab(self.control.base)
        self.lobby.publication_status.connect(self.append_log)
        self.tabs.addTab(self.mapping_page(), "映射")
        self.lobby_index = self.tabs.addTab(self.lobby, "联机大厅")
        self.tabs.addTab(self.settings_page(), "设置")
        self.announcements_index = self.tabs.addTab(self.announcements_page(), "线上公告")
        self.tabs.currentChanged.connect(self.tab_changed)
        QVBoxLayout(self).addWidget(self.tabs)

        if nodes_file is not None:
            self.open_nodes(nodes_file)
        elif previous := self.settings.value("nodes_file", "", type=str):
            self.open_nodes(Path(previous))

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
        ports.addRow("输入本地端口:", self.local_port)
        ports.addRow("远程端口:", self.remote_port)
        layout.addLayout(ports)
        self.public_room = QGroupBox("发布到联机大厅")
        self.public_room.setCheckable(True)
        self.public_room.setChecked(False)
        self.public_room.setEnabled(bool(self.lobby.api))
        room = QFormLayout(self.public_room)
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
        layout.addLayout(buttons)
        layout.addWidget(QLabel("运行日志:"))
        layout.addWidget(self.mapping_log, 1)
        address = QFormLayout()
        address.addRow("映射地址:", self.link)
        layout.addLayout(address)
        layout.addWidget(self.status)
        return page

    def settings_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.addWidget(self.load_button)
        self.node_file_label = QLabel("尚未载入 TOML 节点配置。")
        self.node_file_label.setTextFormat(Qt.TextFormat.PlainText)
        self.node_file_label.setWordWrap(True)
        layout.addWidget(self.node_file_label)
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

    def tab_changed(self, index: int):
        if index == self.lobby_index:
            self.lobby.load_once()

    def append_log(self, message: str):
        self.mapping_log.appendPlainText(f"{datetime.now():%H:%M:%S}  {message}")

    def set_status(self, message: str):
        if self.status.text() != message:
            self.status.setText(message)
            self.append_log(message)

    def choose_nodes(self):
        selected, _ = QFileDialog.getOpenFileName(self, "载入节点", "", "TOML (*.toml)")
        if selected and self.open_nodes(Path(selected)):
            self.settings.setValue("nodes_file", selected)

    def open_nodes(self, path: Path):
        try:
            nodes = load_nodes(path)
        except (OSError, UnicodeError, ValueError):
            self.node_file_label.setText("无法载入这份 TOML，现有节点列表仍可使用。")
            self.set_status("无法载入节点配置，请检查 TOML 格式和节点字段。")
            return False
        self.node.clear()
        for node in nodes:
            self.node.addItem(node.name, node)
        self.start_button.setEnabled(True)
        self.node_file_label.setText(str(path))
        self.set_status(f"已载入 {len(nodes)} 个节点。")
        return True

    def update_remote_ports(self):
        node = self.node.currentData()
        if node is None:
            return
        start, end = node.port_range
        previous = self.remote_port.value()
        self.remote_port.setRange(start, end)
        if not start <= previous <= end:
            self.remote_port.setValue(random.randint(start, end))

    def toggle_mapping(self):
        if self.registering or self.stopping or self.closing:
            return
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.stop_mapping()
            return
        node = self.node.currentData()
        if node is None:
            return
        if not self.frpc.is_file():
            self.set_status("找不到 frpc，请使用包含当前平台 frpc 的构建。")
            return

        self.failure = ""
        if node.node_id:
            if not self.control.base:
                self.set_status("受控节点需要可用的 Control Server，请通过 Launcher 启动。")
                return
            self.registering = True
            self.set_mapping_controls(True)
            self.start_button.setEnabled(False)
            self.set_status("正在注册隧道…")
            self.control.reserve(node.node_id, self.remote_port.value(),
                                 lambda claim, error: self.start_registered(node, claim, error))
            return
        self.start_mapping(node)

    def start_registered(self, node: Node, claim: TunnelClaim | None, error: str):
        self.registering = False
        if self.closing:
            if claim:
                self.control.release(claim)
            return
        if claim is None:
            self.failure = error
            self.mapping_finished()
            return
        self.claim = claim
        self.start_mapping(node)

    def start_mapping(self, node: Node):
        try:
            self.config_dir = TemporaryDirectory(prefix="minecrafttools-")
            config = Path(self.config_dir.name) / "frpc.toml"
            write_frpc_config(config, node, self.local_port.value(), self.remote_port.value(), self.claim)
        except (OSError, UnicodeError):
            self.failure = "无法写入临时配置。"
            self.mapping_finished()
            return
        except ValueError as error:
            self.failure = str(error)
            self.mapping_finished()
            return

        self.output.clear()
        self.address = node.address(self.remote_port.value())
        self.set_mapping_controls(True)
        self.set_status("正在连接节点…")
        self.start_timeout.start(20_000)
        self.process.start(str(self.frpc), ["-c", str(config)])
        if self.claim:
            self.renew_at = monotonic() + 25
            self.status_timer.start()

    def poll_tunnel(self):
        claim = self.claim
        if not claim or self.status_pending or self.stopping or self.closing:
            return
        self.status_pending = True
        renew = monotonic() >= self.renew_at

        def finished(code, payload):
            if self.claim is not claim:
                return
            self.status_pending = False
            if self.stopping or self.closing:
                return
            if code == 200 and renew:
                self.renew_at = monotonic() + 25
            if code == 200 and payload.get("status") in ("block_pending", "blocked"):
                reason = payload.get("reason")
                if reason == "no_minecraft_status":
                    reason = "未检测到 Minecraft Java 响应。"
                self.failure = "隧道已封禁。"
                if isinstance(reason, str) and reason.strip():
                    self.failure += reason.strip()[:200]
            elif code in (401, 404):
                self.failure = "隧道注册已失效，请重新启动映射。"
            else:
                return
            self.stop_mapping()

        self.control.status(claim, renew, finished)

    def set_mapping_controls(self, running: bool):
        for widget in (self.node, self.load_button, self.local_port, self.remote_port):
            widget.setEnabled(not running)
        self.public_room.setEnabled(not running and bool(self.lobby.api))
        self.start_button.setText("停止映射" if running else "启动映射")
        self.start_button.setEnabled(self.node.count() > 0)
        if not running:
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
                if self.public_room.isChecked() and self.lobby.active_room is None:
                    node = self.node.currentData()
                    self.lobby.publish(room_payload(
                        node.public_host or node.server_addr,
                        self.remote_port.value(),
                        self.room_name.text().strip(),
                        self.room_description.text().strip(),
                        self.room_players.value(),
                    ))
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
        claim, self.claim = self.claim, None
        if claim:
            self.control.release(claim)
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
        event.accept()


def send_launcher_ready(port: str, token: str) -> None:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=1) as connection:
            connection.sendall(f"READY {token}\n".encode("ascii"))
    except (OSError, ValueError, OverflowError):
        logging.getLogger(__name__).warning("无法向 Launcher 发送就绪信号。")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nodes", type=Path)
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
    app.setWindowIcon(QIcon(str(resources / "logo.ico")))
    artwork = QPixmap(str(resources / "MinecraftToolsStartupLogo.png"))
    if options.smoke_test:
        if artwork.isNull() or not frpc.is_file():
            return 1
        result = subprocess.run([str(frpc), "--version"], timeout=10, check=False)
        if result.returncode:
            return result.returncode
    window = MappingWindow(frpc, options.nodes)
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
    # Destroy the window while Qt can still process deferred deletion.
    app.aboutToQuit.connect(window.deleteLater)
    return app.exec()
