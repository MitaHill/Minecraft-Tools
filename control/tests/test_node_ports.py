import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from support import seed_node

from app.database import open_database
from app.node_ports import (
    apply_pending,
    clear_pending,
    contains,
    format_spec,
    has_pending,
    load_ranges,
    parse_spec,
    ranges_by_node,
    replace_ranges,
    requested_ranges,
    within,
)


class PortSpecTests(unittest.TestCase):
    def test_parse_sorts_merges_and_rejects_bad_input(self):
        self.assertEqual(parse_spec("30004, 30001-30002,30003"), ((30001, 30004),))
        self.assertEqual(parse_spec("31000"), ((31000, 31000),))
        self.assertEqual(parse_spec("30005-30005,30001"), ((30001, 30001), (30005, 30005)))
        for text, message in (
            ("", "请输入允许端口"),
            ("30001,", "请输入允许端口"),
            ("abc", "端口格式应为 30000-30010,31000"),
            ("30001-30002-30003", "端口格式应为 30000-30010,31000"),
            ("1023", "端口需在 1024–65535 之间"),
            ("65536", "端口需在 1024–65535 之间"),
            ("30002-30001", "端口范围起止顺序错误"),
            (",".join(str(1024 + 2 * index) for index in range(129)), "最多配置 128 段端口"),
        ):
            with self.assertRaises(ValueError) as raised:
                parse_spec(text)
            self.assertEqual(str(raised.exception), message)

    def test_format_contains_and_within(self):
        ranges = parse_spec("30001-30002,30010")
        self.assertEqual(format_spec(ranges), "30001-30002,30010")
        self.assertTrue(contains(ranges, 30002))
        self.assertFalse(contains(ranges, 30003))
        self.assertTrue(within(ranges, [[30000, 30003], [30009, 30011]]))
        self.assertTrue(within(((30002, 30003),), [[30001, 30002], [30003, 30004]]))
        self.assertFalse(within(((30001, 30005),), [[30001, 30002], [30004, 30005]]))
        self.assertFalse(within(ranges, []))


class PortStorageTests(unittest.TestCase):
    def setUp(self):
        home = Path(self.enterContext(TemporaryDirectory()))
        self.database = open_database(home)
        self.addCleanup(self.database.dispose)
        with self.database.begin() as connection:
            seed_node(connection, "a", ranges=((30001, 30002),))

    def test_pending_replaces_only_pending_rows_then_applies(self):
        with self.database.begin() as connection:
            self.assertEqual(load_ranges(connection, "a"), ((30001, 30002),))
            replace_ranges(connection, "a", ((30001, 30002), (30005, 30005)), pending=True)
            self.assertTrue(has_pending(connection, "a"))
            self.assertEqual(
                requested_ranges(connection, "a"), ((30001, 30002), (30005, 30005))
            )
            self.assertEqual(load_ranges(connection, "a"), ((30001, 30002),))
            self.assertEqual(
                ranges_by_node(connection, pending=True)["a"],
                ((30001, 30002), (30005, 30005)),
            )
            self.assertTrue(apply_pending(connection, "a"))
            self.assertFalse(has_pending(connection, "a"))
            self.assertEqual(
                load_ranges(connection, "a"), ((30001, 30002), (30005, 30005))
            )

    def test_clear_pending_keeps_active_ranges(self):
        with self.database.begin() as connection:
            replace_ranges(connection, "a", ((30005, 30005),), pending=True)
            clear_pending(connection, "a")
            self.assertFalse(has_pending(connection, "a"))
            self.assertEqual(load_ranges(connection, "a"), ((30001, 30002),))
