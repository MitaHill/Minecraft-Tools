import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication

from minecrafttools.lobby import LobbyTab


class LobbyPresenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test_lobby", "-platform", "offscreen"])

    def test_open_lobby_registers_presence_before_reading_count(self):
        with patch.dict("os.environ", {"MINECRAFTTOOLS_LOBBY_URL": "http://127.0.0.1/api/v1/lobby"}):
            lobby = LobbyTab()
        self.addCleanup(lobby.close)
        lobby.request = Mock()

        lobby.show()

        rooms, heartbeat = lobby.request.call_args_list
        self.assertEqual(rooms.args[:2], (b"GET", "rooms"))
        self.assertEqual(heartbeat.args[:2], (b"POST", "heartbeat"))
        self.assertEqual(heartbeat.args[3], {})

        rooms.args[2]({"success": True, "rooms": []})
        heartbeat.args[2]({"success": True})
        count = lobby.request.call_args
        self.assertEqual(count.args[:2], (b"GET", "online"))
        count.args[2]({"success": True, "online_count": 1})
        self.assertEqual(lobby.online.text(), "在线人数: 1")
        self.assertEqual(lobby.status.text(), "当前暂无公开房间。")

        self.assertTrue(lobby.presence_timer.isActive())
        lobby.hide()
        self.assertFalse(lobby.presence_timer.isActive())

    def test_publish_failure_shows_the_server_reason(self):
        with patch.dict("os.environ", {"MINECRAFTTOOLS_LOBBY_URL": "http://127.0.0.1/api/v1/lobby"}):
            lobby = LobbyTab()
        self.addCleanup(lobby.close)
        lobby.request = Mock()

        lobby.publish({"node_id": 1, "remote_port": 30001, "full_room_code": "30001_1"})
        post = lobby.request.call_args
        self.assertEqual(post.args[:2], (b"POST", "rooms"))

        post.args[2]({"success": False, "message": "未探测到 Minecraft 服务器"})
        self.assertEqual(lobby.announcement, "房间发布失败：未探测到 Minecraft 服务器")

        post.args[2]({"success": False})
        self.assertEqual(
            lobby.announcement, "房间发布失败，请确认游戏服务器可从公网访问。"
        )

    def test_admin_removal_stops_renewal_and_manual_publish_gets_new_id(self):
        lobby = LobbyTab()
        self.addCleanup(lobby.close)
        lobby.request = Mock()
        room = {"node_id": 1, "remote_port": 30001, "full_room_code": "30001_1"}
        lobby.publish(room)
        first = lobby.request.call_args.args
        first[2]({"success": False, "code": "room_unlisted"})
        self.assertIsNone(lobby.active_room)
        self.assertFalse(lobby.heartbeat.isActive())
        lobby.sync_publication()
        self.assertEqual(lobby.request.call_count, 1)
        lobby.publish(room)
        self.assertNotEqual(first[3]["publication_id"], lobby.request.call_args.args[3]["publication_id"])
