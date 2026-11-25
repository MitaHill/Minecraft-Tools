import json
import os
from hashlib import sha256

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


def room_payload(host: str, port: int, name: str, description: str, max_players: int) -> dict:
    # The API keys rooms by port and node ID, even when two hosts use the same port.
    node_id = int.from_bytes(sha256(host.lower().encode()).digest()[:4], "big") & 0x7fffffff
    return {
        "full_room_code": f"{port}_{node_id}",
        "node_id": node_id,
        "remote_port": port,
        "room_name": name or "我的 Minecraft 服务器",
        "description": description,
        "max_players": max_players,
        "player_count": 1,
        "is_public": True,
        "host_player": "玩家",
        "server_addr": host,
    }


class RoomCard(QFrame):
    def __init__(self, room: dict):
        super().__init__()
        self.setObjectName("RoomCard")
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(self.text(room.get("room_name") or "未知房间", "cardTitle"), 1)
        header.addWidget(self.text(f"{room.get('player_count', 0)}/{room.get('max_players', 20)} 人", "cardPlayerCount"))
        layout.addLayout(header)
        layout.addWidget(self.text(f"房主: {room.get('host_player') or '玩家'}", "cardInfo"))
        layout.addWidget(self.text(f"Ver: {room.get('game_version') or '探测中…'}", "cardInfo"))
        layout.addWidget(self.text(f"来自: {room.get('host_ip') or '—'}", "cardInfo"))
        if room.get("description"):
            layout.addWidget(self.text(room["description"], "cardDescription"))

        host, port = room.get("server_addr"), room.get("remote_port")
        copy = QPushButton("复制连接地址")
        valid = isinstance(host, str) and bool(host) and type(port) is int and 1 <= port <= 65535
        copy.setEnabled(valid)
        if valid:
            address = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
            copy.clicked.connect(lambda: QApplication.clipboard().setText(address))
        layout.addWidget(copy, alignment=Qt.AlignmentFlag.AlignRight)

    @staticmethod
    def text(value: str, name: str) -> QLabel:
        label = QLabel()
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setText(str(value))
        label.setWordWrap(True)
        label.setObjectName(name)
        return label


class LobbyTab(QWidget):
    publication_status = Signal(str)

    def __init__(self, control: str = ""):
        super().__init__()
        self.api = os.environ.get(
            "MINECRAFTTOOLS_LOBBY_URL", os.environ.get("MINECRAFTFRP_LOBBY_URL", f"{control}/api/v1/lobby" if control else "")
        ).strip().rstrip("/")
        self.network = QNetworkAccessManager(self)
        self.active_room = None
        self.last_sent = None
        self.publication_pending = False
        self.announcement = ""
        self.heartbeat = QTimer(self)
        self.heartbeat.setInterval(5000)
        self.heartbeat.timeout.connect(self.sync_publication)
        self.presence_timer = QTimer(self)
        self.presence_timer.setInterval(5000)
        self.presence_timer.timeout.connect(self.sync_presence)
        self.presence_pending = False
        self.visited = False

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(QLabel("正在联机的房间"), 1)
        self.refresh_button = QPushButton("刷新列表")
        self.refresh_button.setEnabled(bool(self.api))
        self.refresh_button.clicked.connect(self.refresh_rooms)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        rooms = QWidget()
        self.rooms = QVBoxLayout(rooms)
        self.rooms.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(rooms)
        layout.addWidget(scroll, 1)

        self.status = QLabel("准备就绪" if self.api else "尚未配置联机大厅服务。")
        self.online = QLabel("在线人数: —")
        footer = QHBoxLayout()
        footer.addWidget(self.status, 1)
        footer.addWidget(self.online)
        layout.addLayout(footer)

    def request(self, method, route, callback, payload=None):
        request = QNetworkRequest(QUrl(f"{self.api}/{route}"))
        request.setTransferTimeout(8000)
        request.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, "LMFP/1.3.1")
        if method == b"GET":
            reply = self.network.get(request)
        else:
            request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
            reply = self.network.sendCustomRequest(request, method, json.dumps(payload).encode())

        def finished():
            data = {}
            if reply.error() == QNetworkReply.NetworkError.NoError:
                try:
                    candidate = json.loads(reply.readAll().data())
                    if isinstance(candidate, dict):
                        data = candidate
                except (ValueError, UnicodeError):
                    pass
            reply.deleteLater()
            callback(data)

        reply.finished.connect(finished)

    def showEvent(self, event):
        super().showEvent(event)
        if not self.api:
            return
        self.presence_timer.start()
        if not self.visited:
            self.refresh_rooms()
        else:
            self.sync_presence()

    def hideEvent(self, event):
        self.presence_timer.stop()
        super().hideEvent(event)

    def refresh_rooms(self):
        if not self.api:
            return
        self.visited = True
        self.refresh_button.setEnabled(False)
        self.status.setText("正在加载房间列表…")
        self.request(b"GET", "rooms", self.show_rooms)
        self.sync_presence()

    def sync_presence(self):
        if self.presence_pending:
            return
        self.presence_pending = True

        def counted(data):
            self.presence_pending = False
            self.show_online(data)

        def registered(data):
            if data.get("success") is True and self.presence_timer.isActive():
                self.request(b"GET", "online", counted)
            else:
                counted({})

        self.request(b"POST", "heartbeat", registered, {})

    def show_online(self, data):
        count = data.get("online_count")
        self.online.setText(f"在线人数: {count if type(count) is int else '—'}")

    def show_rooms(self, data):
        self.refresh_button.setEnabled(True)
        rooms = data.get("rooms")
        if not isinstance(rooms, list):
            self.status.setText("无法获取房间列表。")
            return
        while self.rooms.count():
            self.rooms.takeAt(0).widget().deleteLater()
        valid = [room for room in rooms if isinstance(room, dict)]
        for room in valid:
            self.rooms.addWidget(RoomCard(room))
        self.status.setText(f"已加载 {len(valid)} 个房间" if valid else "当前暂无公开房间。")

    def publish(self, room):
        self.active_room = room
        self.announcement = ""
        self.heartbeat.start()
        self.sync_publication()

    def unpublish(self):
        self.active_room = None
        self.heartbeat.stop()
        self.sync_publication()

    def sync_publication(self):
        if self.publication_pending:
            return
        room = self.active_room
        if self.last_sent is not None and self.last_sent is not room:
            previous, self.last_sent = self.last_sent, None
            method = b"DELETE"
            payload = {key: previous[key] for key in ("node_id", "remote_port")}
        elif room is not None:
            self.last_sent = room
            method, payload = b"POST", room
        else:
            return
        self.publication_pending = True

        def finished(data):
            self.publication_pending = False
            if method == b"POST" and self.active_room is room:
                if data.get("success") is True:
                    status = "房间已发布到联机大厅。"
                else:
                    reason = data.get("message")
                    status = (
                        f"房间发布失败：{reason}"
                        if isinstance(reason, str) and reason
                        else "房间发布失败，请确认游戏服务器可从公网访问。"
                    )
                if status != self.announcement:
                    self.announcement = status
                    self.publication_status.emit(status)
            if method == b"DELETE" or self.last_sent is not self.active_room:
                self.sync_publication()

        self.request(method, "rooms", finished, payload)
