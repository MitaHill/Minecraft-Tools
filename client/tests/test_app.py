import unittest

from minecrafttools.app import blocked_message


class BlockedMessageTests(unittest.TestCase):
    def test_custom_server_message_is_shown_verbatim(self):
        self.assertEqual(blocked_message("测试"), "隧道已封禁。测试")

    def test_legacy_scan_message_is_translated(self):
        self.assertEqual(
            blocked_message("no_minecraft_status"),
            "隧道已封禁。未检测到 Minecraft Java 响应。",
        )

    def test_missing_reason_shows_only_prefix(self):
        for reason in (None, "", "   ", 1, ["测试"]):
            with self.subTest(reason=reason):
                self.assertEqual(blocked_message(reason), "隧道已封禁。")

    def test_long_message_is_truncated(self):
        self.assertEqual(blocked_message("x" * 250), "隧道已封禁。" + "x" * 200)
