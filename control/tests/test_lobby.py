import json
import sqlite3
import threading
import unittest
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from socket import socket
from socketserver import StreamRequestHandler, ThreadingTCPServer
from tempfile import TemporaryDirectory
from time import time
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import URL, create_engine

from app.lobby import cleanup
from app.main import app
from app.registry import nodes, tunnels
from app.tunnel_commands import acknowledge_command, enqueue_command, next_command


def varint(value):
    encoded = bytearray()
    while value >= 128:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


class JavaStatusHandler(StreamRequestHandler):
    def packet(self):
        length = 0
        for shift in range(0, 35, 7):
            byte = self.rfile.read(1)
            if not byte:
                return b""
            length |= (byte[0] & 127) << shift
            if byte[0] < 128:
                return self.rfile.read(length)
        raise ValueError("invalid packet length")

    def handle(self):
        self.request.settimeout(3)
        if not self.packet() or self.packet() != b"\x00":
            return
        payload = json.dumps(
            {
                "version": {"name": "1.21.4", "protocol": 769},
                "players": {"online": 2, "max": 20},
                "description": {"text": "Test MOTD"},
            }
        ).encode()
        response = b"\x00" + varint(len(payload)) + payload
        self.wfile.write(varint(len(response)) + response)
        ping = self.packet()
        if ping:
            self.wfile.write(varint(len(ping)) + ping)


class LobbyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.game = ThreadingTCPServer(("127.0.0.1", 0), JavaStatusHandler)
        cls.game.daemon_threads = True
        cls.addClassCleanup(cls.game.server_close)
        thread = threading.Thread(target=cls.game.serve_forever, daemon=True)
        thread.start()
        cls.addClassCleanup(cls.game.shutdown)

    def setUp(self):
        root = self.enterContext(TemporaryDirectory())
        self.database = Path(root) / "control.db"
        self.port = self.game.server_address[1]
        self.enterContext(
            patch.dict(
                "os.environ",
                {
                    "CONTROL_DATA_DIR": root,
                    "CONTROL_LOBBY_TARGETS": f"127.0.0.1:{self.port}",
                },
            )
        )
        self.http = self.enterContext(TestClient(app, client=("192.0.2.10", 52000)))
        self.node = (
            int.from_bytes(sha256(b"127.0.0.1").digest()[:4], "big") & 0x7FFFFFFF
        )
        self.room = {
            "full_room_code": f"{self.port}_{self.node}",
            "node_id": self.node,
            "remote_port": self.port,
            "room_name": "Lobby test",
            "description": "Test room",
            "max_players": 10,
            "player_count": 1,
            "is_public": True,
            "host_player": "Player",
            "server_addr": "127.0.0.1",
        }

    def list_rooms(self):
        response = self.http.get("/api/lobby/rooms")
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()["success"], True)
        return response.json()["rooms"]

    def test_publish_renew_and_delete(self):
        created = self.http.post("/api/lobby/rooms", json=self.room)
        self.assertEqual(created.json(), {"success": True, "message": "Room updated"})
        (first,) = self.list_rooms()
        self.assertEqual(first["host_ip"], "192.0.2.***")
        self.assertEqual(first["game_version"], "1.21.4")
        self.assertNotIn("client_ip", first)
        self.assertIsNone(first["tunnel_id"])

        self.room["room_name"] = "New name"
        self.assertIs(
            self.http.post("/api/lobby/rooms", json=self.room).json()["success"], True
        )
        (renewed,) = self.list_rooms()
        self.assertEqual(renewed["room_name"], "New name")
        self.assertGreater(renewed["updated_at"], first["updated_at"])

        for _ in range(2):
            response = self.http.request(
                "DELETE",
                "/api/lobby/rooms",
                json={
                    "remote_port": self.port,
                    "node_id": self.node,
                },
            )
            self.assertEqual(
                response.json(), {"success": True, "message": "Room removed"}
            )
        self.assertEqual(self.list_rooms(), [])

    def test_private_room_and_duplicate_owner(self):
        self.room["is_public"] = False
        self.assertIs(
            self.http.post("/api/lobby/rooms", json=self.room).json()["success"], True
        )
        self.assertEqual(self.list_rooms(), [])
        duplicate = {
            **self.room,
            "node_id": self.node + 1,
            "full_room_code": f"{self.port}_{self.node + 1}",
        }
        response = self.http.post("/api/lobby/rooms", json=duplicate)
        self.assertIs(response.json()["success"], False)

    def test_block_pending_unlists_and_refuses_renewal_before_firewall_ack(self):
        database = app.state.database
        with database.begin() as connection:
            connection.execute(
                nodes.insert().values(
                    id="test-node",
                    name="Test Node",
                    public_host="127.0.0.1",
                    port_start=self.port,
                    port_end=self.port,
                    token_hash="test-token-hash",
                )
            )
            connection.execute(
                tunnels.insert().values(
                    id="test-tunnel",
                    node_id="test-node",
                    run_id="run-a",
                    proxy_name="test-proxy",
                    remote_port=self.port,
                )
            )
        self.assertTrue(
            self.http.post("/api/lobby/rooms", json=self.room).json()["success"]
        )
        (published,) = self.list_rooms()
        self.assertEqual(published["tunnel_id"], "test-tunnel")

        block = enqueue_command(database, "test-tunnel", "block", "issue", "operator")
        self.assertEqual(self.list_rooms(), [])
        self.assertEqual(
            self.http.post("/api/lobby/rooms", json=self.room).json(),
            {"success": False, "message": "隧道已封禁"},
        )
        self.assertEqual(
            next_command(database, "test-node")["command_id"], block["command_id"]
        )
        self.assertTrue(
            acknowledge_command(database, "test-node", block["command_id"], True, "")
        )
        unblock = enqueue_command(
            database, "test-tunnel", "unblock", "resolved", "operator"
        )
        self.assertEqual(
            next_command(database, "test-node")["command_id"], unblock["command_id"]
        )
        self.assertTrue(
            acknowledge_command(database, "test-node", unblock["command_id"], True, "")
        )
        self.assertTrue(
            self.http.post("/api/lobby/rooms", json=self.room).json()["success"]
        )

    def test_room_delete_ignores_untrusted_forwarded_ip(self):
        self.assertIs(
            self.http.post("/api/lobby/rooms", json=self.room).json()["success"], True
        )
        # A distinct peer can guess the old protocol's room key but cannot remove the room.
        other = TestClient(app, client=("192.0.2.11", 52001))
        response = other.request(
            "DELETE",
            "/api/lobby/rooms",
            json={
                "remote_port": self.port,
                "node_id": self.node,
            },
            headers={"X-Forwarded-For": "192.0.2.10"},
        )
        self.assertIs(response.json()["success"], True)
        self.assertEqual(len(self.list_rooms()), 1)

    def test_unauthorized_probe_and_text_boundary(self):
        unauthorized = {**self.room, "server_addr": "127.0.0.2"}
        response = self.http.post("/api/lobby/rooms", json=unauthorized)
        self.assertEqual(
            response.json(), {"success": False, "message": "Validation failed"}
        )
        oversized = {**self.room, "room_name": "x" * 51}
        self.assertEqual(
            self.http.post("/api/lobby/rooms", json=oversized).status_code, 422
        )

    def test_allowlisted_non_minecraft_listener_is_rejected(self):
        with socket() as listening:
            listening.bind(("127.0.0.1", 0))
            listening.listen()
            port = listening.getsockname()[1]
            room = {
                **self.room,
                "remote_port": port,
                "full_room_code": f"{port}_{self.node}",
            }
            settings = app.state.lobby_settings
            configured = replace(settings, targets=frozenset({("127.0.0.1", port)}))
            with (
                patch.object(app.state, "lobby_settings", configured),
                patch("app.lobby.MAX_PROBE_SECONDS", 0.15),
            ):
                response = self.http.post("/api/lobby/rooms", json=room)
            self.assertEqual(
                response.json(), {"success": False, "message": "Validation failed"}
            )

    def test_housekeeping_expires_rooms_without_recent_renewal(self):
        self.assertIs(
            self.http.post("/api/lobby/rooms", json=self.room).json()["success"], True
        )
        cleanup(app.state.database, time() + 9)
        self.assertEqual(len(self.list_rooms()), 1)
        cleanup(app.state.database, time() + 11)
        self.assertEqual(self.list_rooms(), [])

    def test_online_heartbeat_counts_unique_recent_peers(self):
        route = "/api/lobby/online"
        self.assertEqual(
            self.http.get(route).json(), {"success": True, "online_count": 0}
        )
        for _ in range(2):
            self.assertEqual(
                self.http.post("/api/lobby/heartbeat", json={}).json(),
                {"success": True},
            )
        self.assertEqual(
            self.http.get(route).json(), {"success": True, "online_count": 1}
        )
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE online_users SET last_heartbeat = ?", (time() - 16,))
        self.assertEqual(
            self.http.get(route).json(), {"success": True, "online_count": 0}
        )


class MigrationTests(unittest.TestCase):
    def test_upgrade_preserves_an_existing_diagnostics_database(self):
        home = Path(self.enterContext(TemporaryDirectory()))
        engine = create_engine(URL.create("sqlite", database=str(home / "control.db")))
        self.addCleanup(engine.dispose)
        migration = Config()
        migration.set_main_option("script_location", "app:migrations")
        with engine.begin() as connection:
            migration.attributes["connection"] = connection
            command.upgrade(migration, "0001_diagnostics")
            connection.exec_driver_sql(
                "INSERT INTO diagnostics (os, arch, report) VALUES (?, ?, ?)",
                ("darwin", "arm64", '{"stage":"ready"}'),
            )
        self.enterContext(patch.dict("os.environ", {"CONTROL_DATA_DIR": str(home)}))
        for _ in range(2):
            with TestClient(app) as http:
                self.assertEqual(http.get("/health").json(), {"status": "ok"})
                self.assertEqual(
                    http.get("/api/lobby/rooms").json(), {"success": True, "rooms": []}
                )
                with engine.connect() as connection:
                    revision = connection.exec_driver_sql(
                        "SELECT version_num FROM alembic_version"
                    ).scalar_one()
                    report = connection.exec_driver_sql(
                        "SELECT report FROM diagnostics"
                    ).scalar_one()
                self.assertEqual(revision, "0006_install_claims")
                self.assertEqual(json.loads(report), {"stage": "ready"})
