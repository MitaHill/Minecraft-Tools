import tempfile
import tomllib
import unittest
from pathlib import Path

from minecraftfrp.config import Node, load_nodes, write_frpc_config


class ConfigTests(unittest.TestCase):
    def test_tcp_config_preserves_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frpc.toml"
            node = Node("节点", "localhost", 7000, 't"ok\\en\n游戏')
            write_frpc_config(path, node, 25565, 25566)
            config = tomllib.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(config["auth"], {"method": "token", "token": node.token})
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
        self.assertEqual(first.address(25565), "127.0.0.1:25565")
        self.assertEqual(second.token, "test")
        self.assertEqual(second.address(25565), "[::1]:25565")

    def test_invalid_server_port_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nodes.toml"
            path.write_text('[[nodes]]\nname = "A"\nserver_addr = "localhost"\nserver_port = true\n')
            with self.assertRaises(ValueError):
                load_nodes(path)
