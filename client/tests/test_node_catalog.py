import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from minecrafttools.control import TunnelControl
from minecrafttools.node_catalog import NodeCatalog, version_tuple

CATALOG = {
    "blocked": False,
    "updated_at": 1795690000000,
    "min_frpc_version": "0.72.0",
    "nodes": [{"id": "game", "name": "游戏线路"}],
}


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test_catalog", "-platform", "offscreen"])

    def test_cache_replaces_old_and_empty_catalogs_and_separates_channels(self):
        root = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "MINECRAFTTOOLS_DATA_ROOT": root, "MINECRAFTTOOLS_BUILD_CHANNEL": "dev",
        }))
        control = TunnelControl(None, "https://control.example.test")
        original = NodeCatalog.from_payload(CATALOG)
        control.cache_catalog(original)
        self.assertEqual(control.cached_catalog(), original)
        with patch("minecrafttools.control.save_json") as save:
            control.cache_catalog(original)
            save.assert_not_called()

        changed = NodeCatalog.from_payload({**CATALOG, "nodes": [], "min_frpc_version": "0.73.0"})
        control.cache_catalog(changed)
        self.assertEqual(control.cached_catalog(), changed)
        with patch.dict(os.environ, {"MINECRAFTTOOLS_BUILD_CHANNEL": "stable"}):
            stable = TunnelControl(None, "https://control.example.test")
        self.assertIsNone(stable.cached_catalog())
        self.assertEqual(len(list(Path(root).iterdir())), 1)

    def test_reserve_reports_build_channel(self):
        root = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "MINECRAFTTOOLS_DATA_ROOT": root, "MINECRAFTTOOLS_BUILD_CHANNEL": "dev",
        }))
        control = TunnelControl(None, "https://control.example.test")
        control.token = "A" * 43
        with patch.object(control, "request") as request:
            control.reserve("game", lambda *_: None)
        args = request.call_args.args
        self.assertEqual(args[1], "tunnels/register")
        self.assertEqual(args[3], {"node_id": "game", "channel": "dev"})

    def test_rejects_invalid_or_blocked_payloads(self):
        for changes in (
            {"blocked": True}, {"updated_at": True}, {"min_frpc_version": "invalid"},
            {"nodes": [{}, {}]}, {"nodes": CATALOG["nodes"] * 2},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                NodeCatalog.from_payload({**CATALOG, **changes})
        self.assertGreater(version_tuple("0.72.0"), version_tuple("0.9.0"))
