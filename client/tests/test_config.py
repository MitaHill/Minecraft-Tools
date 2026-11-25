import tempfile
import tomllib
import unittest
from pathlib import Path

from minecrafttools.config import Node, TunnelClaim, load_nodes, write_frpc_config


class ConfigTests(unittest.TestCase):
    def test_managed_config_uses_proxy_metadata_over_plain_tcp(self):
        claim = TunnelClaim("abc", "minecraft-abc", "test-proof")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frpc.toml"
            node = Node("Managed", "localhost", 7000)
            write_frpc_config(path, node, 25565, 25566, claim)
            config = tomllib.loads(path.read_text())
            self.assertEqual(config["transport"]["tls"], {"enable": False})
            (proxy,) = config["proxies"]
            self.assertEqual(proxy["name"], "minecraft-abc")
            self.assertEqual(
                proxy["metadatas"], {"tunnel_id": "abc", "tunnel_proof": "test-proof"}
            )

    def test_tcp_config_preserves_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frpc.toml"
            node = Node("节点", "localhost", 7000, 't"ok\\en\n游戏')
            write_frpc_config(path, node, 25565, 25566)
            config = tomllib.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(config["auth"], {"method": "token", "token": node.token})
        self.assertEqual(config["transport"]["tls"], {"enable": False})
        self.assertEqual(config["proxies"][0]["type"], "tcp")
        self.assertEqual(config["proxies"][0]["localPort"], 25565)
        self.assertEqual(config["proxies"][0]["remotePort"], 25566)

    def test_load_nodes_with_and_without_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nodes.toml"
            path.write_text(
                '[[nodes]]\nname = "A"\nserver_addr = "127.0.0.1"\nserver_port = 7000\n'
                '[[nodes]]\nname = "B"\nserver_addr = "::1"\nserver_port = 7000\ntoken = "test"\n',
                encoding="utf-8",
            )
            first, second = load_nodes(path)

        self.assertEqual(first.token, "")
        self.assertEqual(first.port_range, (1, 65535))
        self.assertEqual(first.address(25565), "127.0.0.1:25565")
        self.assertEqual(second.token, "test")
        self.assertEqual(second.address(25565), "[::1]:25565")

    def test_invalid_server_port_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nodes.toml"
            path.write_text(
                '[[nodes]]\nname = "A"\nserver_addr = "localhost"\nserver_port = true\n'
            )
            with self.assertRaises(ValueError):
                load_nodes(path)

    def test_configured_game_port_range(self):
        base = '[[nodes]]\nname = "A"\nserver_addr = "localhost"\nserver_port = 7000\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nodes.toml"
            path.write_text(
                base + "remote_port_start = 30001\nremote_port_end = 30002\n"
            )
            (node,) = load_nodes(path)
            self.assertEqual(node.port_range, (30001, 30002))
            output = Path(directory) / "frpc.toml"
            write_frpc_config(output, node, 25565, 30002)
            with self.assertRaises(ValueError):
                write_frpc_config(output, node, 25565, 30003)
            self.assertEqual(
                tomllib.loads(output.read_text())["proxies"][0]["remotePort"], 30002
            )
            for invalid in (
                "remote_port_start = 30001\n",
                "remote_port_start = 30002\nremote_port_end = 30001\n",
            ):
                with self.subTest(config=invalid):
                    path.write_text(base + invalid)
                    with self.assertRaises(ValueError):
                        load_nodes(path)
