import tempfile
import tomllib
import unittest
from pathlib import Path

from minecrafttools.config import Node, TunnelClaim, valid_host, write_frpc_config


class ConfigTests(unittest.TestCase):
    def test_managed_config_uses_allocated_port_and_proxy_metadata(self):
        claim = TunnelClaim("abc", "minecraft-abc", "test-proof")
        node = Node("localhost", 7000, 't"ok\\en\n游戏', "::1")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frpc.toml"
            write_frpc_config(path, node, 25565, 30002, claim)
            config = tomllib.loads(path.read_text(encoding="utf-8"))
            with self.assertRaises(ValueError):
                write_frpc_config(path, node, 25565, 65536, claim)

        self.assertEqual(config["auth"], {"method": "token", "token": node.token})
        self.assertEqual(config["transport"]["tls"], {"enable": False})
        (proxy,) = config["proxies"]
        self.assertEqual(proxy["name"], "minecraft-abc")
        self.assertEqual(proxy["type"], "tcp")
        self.assertEqual(proxy["localPort"], 25565)
        self.assertEqual(proxy["remotePort"], 30002)
        self.assertEqual(proxy["metadatas"], {"tunnel_id": "abc", "tunnel_proof": "test-proof"})
        self.assertEqual(node.address(30002), "[::1]:30002")

    def test_validates_connection_hosts(self):
        for host in ("localhost", "game.example.test", "127.0.0.1", "::1"):
            self.assertTrue(valid_host(host))
        for host in ("", "https://example.com", "example.test:7000", "example.test@host", "[::1]"):
            self.assertFalse(valid_host(host))
