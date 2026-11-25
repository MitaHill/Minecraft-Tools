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
