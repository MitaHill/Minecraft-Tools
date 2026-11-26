import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time
from unittest.mock import patch

from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import insert, select
from starlette.websockets import WebSocketDisconnect
from support import seed_node

from app.database import audit_logs
from app.lobby import rooms
from app.main import app
from app.moderation import (
    acknowledge_policy,
    blacklist,
    ip_block,
    node_policy,
    settings,
)
from app.node_hub import Hello, NodeHub, ProtocolError
from app.node_ports import node_port_ranges
from app.registry import blocks, installs, nodes, tunnel_claims, tunnels
from app.scanner import scans
from app.tunnel_commands import acknowledge_command, commands, next_command


class AdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "admin login test only"
        cls.password_hash = PasswordHasher().hash(cls.password)

    def setUp(self):
        home = Path(self.enterContext(TemporaryDirectory()))
        accounts = home / "accounts.json"
        accounts.write_text(
            json.dumps(
                [
                    {
                        "username": "reader",
                        "role": "viewer",
                        "password_hash": self.password_hash,
                    },
                    {
                        "username": "owner",
                        "role": "superadmin",
                        "password_hash": self.password_hash,
                    },
                ]
            )
        )
        self.enterContext(
            patch.dict(
                "os.environ",
                {
                    "CONTROL_DATA_DIR": str(home),
                    "CONTROL_ADMIN_USERS_FILE": str(accounts),
                },
            )
        )
        self.http = self.enterContext(TestClient(app, base_url="https://control.test"))

    def sign_in(self, username):
        return self.http.post(
            "/api/admin/session", json={"username": username, "password": self.password}
        )

    def test_session_cookie_and_logout(self):
        self.assertEqual(self.http.get("/api/admin/nodes").status_code, 401)
        response = self.sign_in("reader")
        self.assertEqual(response.json(), {"username": "reader", "role": "viewer"})
        cookie = response.headers["Set-Cookie"].casefold()
        for flag in ("httponly", "secure", "samesite=strict", "path=/api/admin"):
            self.assertIn(flag, cookie)
        self.assertEqual(self.http.get("/api/admin/session").json(), response.json())
        self.assertEqual(self.http.delete("/api/admin/session").status_code, 204)
        self.assertEqual(self.http.get("/api/admin/nodes").status_code, 401)
        with app.state.database.connect() as connection:
            query = select(audit_logs.c.actor, audit_logs.c.action).order_by(audit_logs.c.id)
            self.assertEqual(
                connection.execute(query).all(), [("reader", "login"), ("reader", "logout")]
            )

    def test_registry_permissions_and_filter(self):
        node = {
            "id": "test-node",
            "name": "Test Node",
            "public_host": "test.example",
            "allowed_ports": "30001-30002",
        }
        self.assertEqual(self.sign_in("reader").status_code, 200)
        self.assertEqual(self.http.get("/api/admin/nodes").json(), {"nodes": []})
        self.assertEqual(self.http.post("/api/admin/nodes", json=node).status_code, 403)
        self.assertEqual(self.sign_in("owner").status_code, 200)
        response = self.http.post("/api/admin/nodes", json=node)
        self.assertEqual(response.status_code, 201)
        self.assertGreater(len(response.json()["secret"]), 32)
        self.assertEqual(self.http.post("/api/admin/nodes", json=node).status_code, 409)
        (node_data,) = self.http.get("/api/admin/nodes").json()["nodes"]
        self.assertEqual(node_data["id"], "test-node")
        self.assertNotIn("token_hash", node_data)

        with app.state.database.begin() as connection:
            connection.execute(
                insert(tunnels),
                [
                    {
                        "id": "a",
                        "node_id": "test-node",
                        "run_id": "run",
                        "proxy_name": "a",
                        "remote_port": 30001,
                        "status": "active",
                    },
                    {
                        "id": "b",
                        "node_id": "test-node",
                        "run_id": "run",
                        "proxy_name": "b",
                        "remote_port": 30002,
                        "status": "blocked",
                    },
                ],
            )
        self.assertEqual(len(self.http.get("/api/admin/tunnels").json()["tunnels"]), 2)
        (row,) = self.http.get("/api/admin/tunnels?status=blocked").json()["tunnels"]
        self.assertEqual(row["id"], "b")

        with app.state.database.begin() as connection:
            connection.execute(
                scans.insert().values(tunnel_id="a", checked_at=120, result="ok")
            )
            connection.execute(
                tunnels.update().where(tunnels.c.id == "a").values(next_scan_at=400)
            )
        result = self.http.get("/api/admin/scans?tunnel_id=a&limit=1").json()["scans"]
        self.assertEqual(
            [(row["tunnel_id"], row["result"]) for row in result], [("a", "ok")]
        )
        rescan = "/api/admin/tunnels/a/rescan"
        self.assertEqual(self.http.post(rescan).status_code, 409)
        with patch.object(app.state, "scanner_enabled", True):
            self.assertEqual(self.http.post(rescan).status_code, 202)
            self.assertEqual(
                self.http.post("/api/admin/tunnels/missing/rescan").status_code, 404
            )
            self.assertEqual(
                self.http.post("/api/admin/tunnels/b/rescan").status_code, 409
            )
            self.assertEqual(self.sign_in("reader").status_code, 200)
            self.assertEqual(self.http.post(rescan).status_code, 403)
        with app.state.database.connect() as connection:
            query = select(tunnels.c.next_scan_at).where(tunnels.c.id == "a")
            self.assertEqual(connection.execute(query).scalar_one(), 0)
            audit = select(audit_logs.c.actor, audit_logs.c.action, audit_logs.c.target)
            audit = audit.where(audit_logs.c.action.not_in(("login", "logout")))
            self.assertEqual(
                connection.execute(audit.order_by(audit_logs.c.id)).all(),
                [("owner", "node.register", "test-node"), ("owner", "tunnel.rescan", "a")],
            )

    def test_publish_rename_and_hide_node_on_next_catalog_request(self):
        self.assertEqual(self.sign_in("owner").status_code, 200)
        url = "/api/v1/node-catalog?channel=dev"
        initial = self.http.get(url).json()
        self.assertFalse(initial["blocked"])
        self.assertEqual(initial["nodes"], [])
        self.assertEqual(initial["min_frpc_version"], app.state.min_frpc_version)
        payload = {
            "id": "test-node", "name": "Test Node", "public_host": "game.example",
            "allowed_ports": "30001-30002", "client_channel": "dev",
        }
        self.assertEqual(self.http.post("/api/admin/nodes", json=payload).status_code, 422)
        payload.update(server_port=7000, frp_token="fixture-frpc-token")
        self.assertEqual(self.http.post("/api/admin/nodes", json=payload).status_code, 201)
        published = self.http.get(url).json()
        self.assertEqual(published["nodes"], [{"id": "test-node", "name": "Test Node"}])
        self.assertGreater(published["updated_at"], initial["updated_at"])
        self.assertEqual(self.http.get(url).json(), published)
        self.assertEqual(self.http.get("/api/v1/node-catalog").json()["nodes"], [])
        (admin_node,) = self.http.get("/api/admin/nodes").json()["nodes"]
        self.assertTrue(admin_node["frp_token_configured"])
        self.assertNotIn("frp_token", admin_node)
        payload = {key: payload[key] for key in ("public_host", "allowed_ports")}
        edit = "/api/admin/nodes/test-node"
        self.assertEqual(self.http.patch(edit, json={**payload, "name": "Renamed"}).status_code, 200)
        renamed = self.http.get(url).json()
        self.assertEqual(renamed["nodes"], [{"id": "test-node", "name": "Renamed"}])
        self.assertGreater(renamed["updated_at"], published["updated_at"])
        self.assertEqual(self.http.patch(edit, json={
            **payload, "name": "Renamed", "client_channel": "hidden",
        }).status_code, 200)
        hidden = self.http.get(url).json()
        self.assertEqual(hidden["nodes"], [])
        self.assertGreater(hidden["updated_at"], renamed["updated_at"])

    def test_hiding_node_blocks_and_republishing_unblocks_its_tunnels(self):
        self.register_policy_node("a")
        self.sign_in("owner")
        with app.state.database.begin() as connection:
            connection.execute(nodes.update().where(nodes.c.id == "a").values(
                client_channel="both", server_port=7000,
            ))
            connection.execute(insert(tunnels).values(
                id="t", node_id="a", run_id="run", proxy_name="t",
                remote_port=30001, status="active", proxy_present=True,
                client_ip="203.0.113.9",
            ))
        payload = {
            "name": "A", "public_host": "a.example", "allowed_ports": "30001-30004",
            "client_channel": "hidden", "server_port": 7000,
        }
        self.assertEqual(
            self.http.patch("/api/admin/nodes/a", json=payload).status_code, 200
        )
        with app.state.database.connect() as connection:
            self.assertEqual(
                connection.execute(select(tunnels.c.status)).scalar_one(),
                "block_pending",
            )
            self.assertEqual(
                connection.execute(select(commands.c.action)).scalar_one(), "block"
            )
            self.assertEqual(
                connection.execute(select(blocks.c.source)).scalar_one(),
                "node-hidden",
            )
            command_id = connection.execute(select(commands.c.id)).scalar_one()
        self.assertEqual(
            next_command(app.state.database, "a")["command_id"], command_id
        )
        acknowledge_command(app.state.database, "a", command_id, True, "")
        publish = {**payload, "client_channel": "both"}
        self.assertEqual(
            self.http.patch("/api/admin/nodes/a", json=publish).status_code, 200
        )
        with app.state.database.connect() as connection:
            self.assertEqual(
                connection.execute(
                    select(commands.c.action).order_by(commands.c.id)
                ).scalars().all(),
                ["block", "unblock"],
            )

    def test_security_settings_are_configurable(self):
        self.assertEqual(self.http.get("/api/admin/settings").status_code, 401)
        self.assertEqual(self.sign_in("reader").status_code, 200)
        defaults = {
            "auto_ban_enabled": True,
            "auto_ban_attempts": 3,
            "auto_ban_window_minutes": 10,
            "auto_ban_hours": 24,
            "auto_ban_skip_legit": True,
        }
        self.assertEqual(self.http.get("/api/admin/settings").json(), defaults)
        self.assertEqual(
            self.http.patch("/api/admin/settings", json=defaults).status_code, 403
        )
        self.assertEqual(self.sign_in("owner").status_code, 200)
        for changes in (
            {"auto_ban_attempts": 0},
            {"auto_ban_attempts": 21},
            {"auto_ban_window_minutes": 0},
            {"auto_ban_window_minutes": 1441},
            {"auto_ban_hours": 0},
            {"auto_ban_hours": 721},
        ):
            self.assertEqual(
                self.http.patch(
                    "/api/admin/settings", json={**defaults, **changes}
                ).status_code,
                422,
                changes,
            )
        changed = {
            "auto_ban_enabled": False,
            "auto_ban_attempts": 5,
            "auto_ban_window_minutes": 30,
            "auto_ban_hours": 48,
            "auto_ban_skip_legit": False,
        }
        self.assertEqual(
            self.http.patch("/api/admin/settings", json=changed).json(), changed
        )
        self.assertEqual(self.http.get("/api/admin/settings").json(), changed)
        with app.state.database.connect() as connection:
            row = connection.execute(select(settings)).mappings().one()
            self.assertEqual(
                (
                    bool(row["auto_ban_enabled"]),
                    row["auto_ban_attempts"],
                    row["auto_ban_window"],
                    row["auto_ban_seconds"],
                    bool(row["auto_ban_skip_legit"]),
                ),
                (False, 5, 1800, 172800, False),
            )
            self.assertIn(
                "settings.security",
                connection.execute(select(audit_logs.c.action)).scalars().all(),
            )

    def test_blacklisted_requests_get_a_reason_and_no_catalog(self):
        with app.state.database.begin() as connection:
            connection.execute(blacklist.insert().values(
                ip="192.0.2.0/24", reason="Please contact support", actor="fixture", created_at=time(),
            ))
        client = TestClient(app, base_url="https://control.test", client=("192.0.2.14", 50000))
        self.addCleanup(client.close)
        response = client.get("/api/v1/node-catalog")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json(), {"detail": {
            "code": "ip_blacklisted", "blocked": True, "reason": "Please contact support",
        }})
        self.assertEqual(client.get("/api/v1/nodes").status_code, 404)

    def test_invalid_credentials_and_cross_origin_requests(self):
        payload = {"username": "reader", "password": "wrong"}
        response = self.http.post(
            "/api/admin/session", json=payload, headers={"Origin": "https://other.test"}
        )
        self.assertEqual(response.status_code, 403)
        for _ in range(5):
            self.assertEqual(
                self.http.post("/api/admin/session", json=payload).status_code, 401
            )
        self.assertEqual(self.sign_in("reader").status_code, 429)

    def register_policy_node(self, node_id, *, cidr=False):
        with app.state.database.begin() as connection:
            seed_node(
                connection,
                node_id,
                ranges=((30001, 30004),),
                public_host=f"{node_id}.example",
                deployed_ports=[[30001, 30008]],
                blacklist_revision=0,
                blacklist_cidr=cidr,
            )

    def test_live_websocket_origin_reconnect_state_and_ip_blacklist(self):
        self.sign_in("owner")
        for node in ("a", "b"):
            self.register_policy_node(node)
        url = "wss://control.test/api/admin/tunnels/live"
        with (
            self.assertRaises(WebSocketDisconnect),
            self.http.websocket_connect(url, headers={"Origin": "https://other.test"}),
        ):
            pass
        headers = {"Origin": "https://control.test"}
        with self.http.websocket_connect(url, headers=headers) as stream:
            self.assertEqual(stream.receive_json()["tunnels"], [])
            with app.state.database.begin() as connection:
                connection.execute(insert(tunnels), [
                    {"id": node, "node_id": node, "run_id": "current", "proxy_name": node,
                     "client_ip": "192.0.2.8", "remote_port": 30001, "status": "pending"}
                    for node in ("a", "b")
                ])
            self.assertEqual([item["id"] for item in stream.receive_json()["tunnels"]], ["a", "b"])
            path = "/api/admin/tunnels/a/blacklist"
            payload = {"run_id": "current", "created_at": 0, "reason": "abuse", "duration": "1h"}
            self.assertEqual(self.http.post(path, json={**payload, "created_at": 99}).status_code, 409)
            self.assertEqual(self.http.post(path, json=payload).status_code, 202)
            self.assertEqual(stream.receive_json()["tunnels"], [])
            for node in ("a", "b"):
                policy = node_policy(app.state.database, node)["blacklist"]
                self.assertEqual(policy["entries"][0]["ip"], "192.0.2.8")
                acknowledge_policy(app.state.database, node, "blacklist", policy["revision"], "")
            listed = self.http.get("/api/admin/blacklist").json()
            self.assertEqual(listed["entries"][0]["reason"], "abuse")
            self.assertEqual(listed["pending_nodes"], [])
            with app.state.database.begin() as connection:
                connection.execute(blacklist.update().values(expires_at=time() - 1))
            self.assertEqual(len(stream.receive_json()["tunnels"]), 2)
            self.assertEqual(self.http.get("/api/admin/blacklist").json()["entries"], [])

    def test_range_update_conflicts_then_waits_for_agent_ack(self):
        self.register_policy_node("a")
        payload = {
            "name": "A", "public_host": "a.example",
            "allowed_ports": "30003-30005,30007",
        }
        self.sign_in("reader")
        self.assertEqual(self.http.patch("/api/admin/nodes/a", json=payload).status_code, 403)
        self.sign_in("owner")
        with app.state.database.begin() as connection:
            connection.execute(insert(tunnels).values(
                id="a", node_id="a", run_id="current", proxy_name="a",
                remote_port=30001, status="pending",
            ))
        conflict = self.http.patch("/api/admin/nodes/a", json=payload)
        self.assertEqual(conflict.status_code, 409)
        self.assertIn("30001", conflict.json()["detail"])
        with app.state.database.begin() as connection:
            connection.execute(tunnels.update().values(proxy_present=False, status="closed"))
        self.assertEqual(self.http.patch("/api/admin/nodes/a", json=payload).json(), {"pending": True})
        row = self.http.get("/api/admin/nodes").json()["nodes"][0]
        self.assertEqual(row["allowed_ports"], "30001-30004")
        self.assertEqual(row["pending_allowed_ports"], "30003-30005,30007")
        policy = node_policy(app.state.database, "a")["range"]
        self.assertEqual(policy["ranges"], [[30003, 30005], [30007, 30007]])
        self.assertEqual((policy["start"], policy["end"]), (30003, 30007))
        acknowledge_policy(app.state.database, "a", "range", policy["revision"], "port occupied")
        row = self.http.get("/api/admin/nodes").json()["nodes"][0]
        self.assertEqual(
            (row["allowed_ports"], row["pending_allowed_ports"], row["range_error"]),
            ("30001-30004", None, "port occupied"),
        )
        self.assertEqual(self.http.patch("/api/admin/nodes/a", json=payload).status_code, 200)
        policy = node_policy(app.state.database, "a")["range"]
        acknowledge_policy(app.state.database, "a", "range", policy["revision"], "")
        row = self.http.get("/api/admin/nodes").json()["nodes"][0]
        self.assertEqual((row["allowed_ports"], row["pending_allowed_ports"]), ("30003-30005,30007", None))

    def test_ports_are_normalized_and_must_fit_deployed_ranges(self):
        self.register_policy_node("a")
        self.sign_in("owner")
        url = "/api/admin/nodes/a"
        base = {"name": "A", "public_host": "a.example"}
        for spec, message in (
            ("1023", "端口需在 1024–65535 之间"),
            ("30005-30001", "端口范围起止顺序错误"),
            ("30001,,30002", "请输入允许端口"),
            ("30001-30002-30003", "端口格式应为 30000-30010,31000"),
            ("30001,65536", "端口需在 1024–65535 之间"),
            ("30009", "映射范围须在节点已部署的端口范围内"),
        ):
            response = self.http.patch(url, json={**base, "allowed_ports": spec})
            self.assertEqual(response.status_code, 422, spec)
            self.assertIn(message, str(response.json()))
        normalized = self.http.patch(url, json={
            **base, "allowed_ports": "30004,30001-30002,30003,30006",
        })
        self.assertEqual(normalized.status_code, 200)
        self.assertEqual(normalized.json(), {"pending": True})
        row = self.http.get("/api/admin/nodes").json()["nodes"][0]
        self.assertEqual(row["pending_allowed_ports"], "30001-30004,30006")

    def test_delete_node_removes_online_nodes_and_their_state(self):
        self.register_policy_node("a")
        self.sign_in("reader")
        self.assertEqual(self.http.delete("/api/admin/nodes/a").status_code, 403)
        self.sign_in("owner")
        with app.state.database.begin() as connection:
            connection.execute(insert(installs).values(
                id="install", token_hash="token", created_at=0, last_seen=0,
            ))
            connection.execute(insert(tunnels).values(
                id="t", node_id="a", run_id="run", proxy_name="t",
                remote_port=30001, status="active",
            ))
            connection.execute(insert(tunnel_claims).values(
                id="claim", install_id="install", node_id="a", remote_port=30001,
                proof_hash="proof", expires_at=time() + 60,
            ))
            block_id = connection.execute(insert(blocks).values(
                tunnel_id="t", node_id="a", port=30001, run_id="run", proxy_name="t",
                reason="fixture", source="manual", active=True, created_at=0,
            ).returning(blocks.c.id)).scalar_one()
            connection.execute(commands.insert().values(
                block_id=block_id, node_id="a", action="block", status="acked",
                created_at=0, next_attempt_at=0,
            ))
            connection.execute(scans.insert().values(
                tunnel_id="t", checked_at=0, result="ok",
            ))
            connection.execute(rooms.insert().values(
                full_room_code="30001_1", remote_port=30001, node_id=1, room_name="room",
                player_count=1, max_players=8, is_public=True, host_player="Player",
                server_addr="a.example", updated_at=time(), client_ip="203.0.113.10",
                tunnel_id="t", listing_id="room",
            ))
        self.assertEqual(self.http.delete("/api/admin/nodes/a").status_code, 204)
        with app.state.database.connect() as connection:
            for table in (
                nodes, tunnels, scans, blocks, tunnel_claims, commands, rooms,
                node_port_ranges,
            ):
                self.assertEqual(connection.execute(select(table)).all(), [], table.name)
        self.assertEqual(self.http.delete("/api/admin/nodes/a").status_code, 404)

    def test_delete_node_cleans_history_and_refreshes_catalog(self):
        self.register_policy_node("a")
        self.sign_in("owner")
        with app.state.database.begin() as connection:
            connection.execute(nodes.update().where(nodes.c.id == "a").values(
                status="offline", client_channel="dev", server_port=7000,
            ))
            connection.execute(insert(installs).values(
                id="install", token_hash="token", created_at=0, last_seen=0,
            ))
            connection.execute(insert(tunnels).values(
                id="t", node_id="a", run_id="run", proxy_name="t",
                remote_port=30001, status="closed", proxy_present=False,
            ))
            connection.execute(tunnel_claims.insert().values(
                id="claim", install_id="install", node_id="a", remote_port=30002,
                proof_hash="proof", expires_at=0,
            ))
            connection.execute(scans.insert().values(
                tunnel_id="t", checked_at=0, result="ok",
            ))
            block_id = connection.execute(blocks.insert().values(
                tunnel_id="t", node_id="a", port=30001, run_id="run", proxy_name="t",
                reason="fixture", source="manual", active=False, created_at=0,
            ).returning(blocks.c.id)).scalar_one()
            connection.execute(commands.insert().values(
                block_id=block_id, node_id="a", action="block", status="acked",
                created_at=0, next_attempt_at=0,
            ))
            connection.execute(rooms.insert().values(
                full_room_code="30001_1", remote_port=30001, node_id=1, room_name="room",
                player_count=1, max_players=8, is_public=True, host_player="Player",
                server_addr="a.example", updated_at=time(), client_ip="203.0.113.9",
                tunnel_id="t", listing_id="room",
            ))
        before = self.http.get("/api/v1/node-catalog?channel=dev").json()["updated_at"]
        self.assertEqual(self.http.delete("/api/admin/nodes/a").status_code, 204)
        after = self.http.get("/api/v1/node-catalog?channel=dev").json()
        self.assertGreater(after["updated_at"], before)
        with app.state.database.connect() as connection:
            for table in (tunnels, scans, blocks, tunnel_claims, commands, rooms, node_port_ranges):
                self.assertEqual(connection.execute(select(table)).all(), [], table.name)
            self.assertEqual(
                connection.execute(
                    select(audit_logs.c.action).where(audit_logs.c.action == "node.delete")
                ).scalar_one(),
                "node.delete",
            )


    def test_manual_cidr_rules_and_legacy_nodes(self):
        url = "/api/admin/blacklist"
        payload = {"target": "192.0.2.0/24", "reason": "range", "duration": "1h"}
        self.register_policy_node("a", cidr=True)
        self.register_policy_node("b")
        self.sign_in("reader")
        self.assertEqual(self.http.post(url, json=payload).status_code, 403)
        self.assertEqual(self.http.delete(url, params={"target": payload["target"]}).status_code, 403)
        self.sign_in("owner")
        wrong_origin = self.http.post(url, json=payload, headers={"Origin": "https://other.test"})
        self.assertEqual(wrong_origin.status_code, 403)
        self.assertEqual(self.http.post(url, json=payload).status_code, 409)
        with app.state.database.begin() as connection:
            connection.execute(nodes.update().where(nodes.c.id == "b").values(blacklist_cidr=True))
        invalid = self.http.post(url, json={**payload, "target": "192.0.2.8/24"})
        self.assertEqual(invalid.status_code, 422)
        self.assertIn("192.0.2.0/24", invalid.json()["detail"])

        with app.state.database.begin() as connection:
            for name, ip, port in (("matched", "192.0.2.8", 30001), ("other", "198.51.100.8", 30002)):
                connection.execute(insert(tunnels).values(
                    id=name, node_id="a", run_id=name, proxy_name=name, client_ip=ip,
                    remote_port=port, status="active",
                ))
                connection.execute(insert(rooms).values(
                    full_room_code=f"{port}_1", remote_port=port, node_id=1, room_name=name,
                    player_count=1, max_players=8, is_public=True, host_player="Player",
                    server_addr="a.example", updated_at=time(), client_ip=f"203.0.113.{port % 10}",
                    tunnel_id=name, listing_id=name,
                ))

        with self.http.websocket_connect(
            "wss://control.test/api/admin/tunnels/live", headers={"Origin": "https://control.test"},
        ) as stream:
            self.assertEqual(len(stream.receive_json()["tunnels"]), 2)
            self.assertEqual(self.http.post(url, json=payload).status_code, 202)
            self.assertEqual([row["id"] for row in stream.receive_json()["tunnels"]], ["other"])
        self.assertEqual([row["room_name"] for row in self.http.get("/api/lobby/rooms").json()["rooms"]], ["other"])
        self.assertEqual(self.http.post(url, json=payload).status_code, 409)

        with self.assertRaises(ProtocolError):
            NodeHub(app.state.database).set_online(Hello(
                protocol_version=1, node_id="b", secret="fixture", agent_version="old",
                frps_version="fixture", deployed_ports=[(30001, 30008)],
            ))
        for node in ("a", "b"):
            policy = node_policy(app.state.database, node)["blacklist"]
            self.assertEqual(policy["entries"][0]["ip"], payload["target"])

        exact = {**payload, "target": "::ffff:192.0.2.8/128", "reason": "host"}
        self.assertEqual(self.http.post(url, json=exact).json()["ip"], "192.0.2.8")
        self.assertEqual(self.http.post(url, json={**exact, "target": "192.0.2.8"}).status_code, 409)
        with app.state.database.connect() as connection:
            self.assertEqual(ip_block(connection, "::ffff:192.0.2.8"), "host")
            self.assertEqual(ip_block(connection, "192.0.2.9"), "range")
            self.assertIsNone(ip_block(connection, "198.51.100.8"))
        self.assertEqual(self.http.delete(url, params={"target": exact["target"]}).status_code, 204)
        with app.state.database.connect() as connection:
            self.assertEqual(ip_block(connection, "192.0.2.8"), "range")
        self.assertEqual(self.http.delete(url, params={"target": payload["target"]}).status_code, 204)
        self.assertEqual(self.http.get(url).json()["entries"], [])

    def test_entire_address_family_requires_separate_confirmation(self):
        self.sign_in("owner")
        self.register_policy_node("a", cidr=True)
        url = "/api/admin/blacklist"
        for typed, canonical, matched, outside in (
            ("0.0.0.0/0", "0.0.0.0/0", "198.51.100.8", "2001:db8::8"),
            ("0:0:0:0:0:0:0:0/0", "::/0", "2001:db8::8", "198.51.100.8"),
        ):
            payload = {"target": typed, "reason": "temporary", "duration": "1h"}
            denied = self.http.post(url, json=payload)
            self.assertEqual(denied.status_code, 409)
            self.assertEqual(denied.json()["detail"], {"code": "confirm_all", "target": canonical})
            self.assertEqual(self.http.get(url).json()["entries"], [])
            self.assertEqual(self.http.post(url, json={**payload, "confirm_all": "true"}).status_code, 422)
            self.assertEqual(self.http.post(url, json={**payload, "confirm_all": True}).json()["ip"], canonical)
            ip_client = TestClient(app, base_url="https://control.test", client=(matched, 51000))
            try:
                response = ip_client.post("/api/v1/installs/register")
                self.assertEqual(response.json()["detail"]["code"], "ip_blacklisted")
            finally:
                ip_client.close()
            with app.state.database.begin() as connection:
                self.assertEqual(ip_block(connection, matched), "temporary")
                self.assertIsNone(ip_block(connection, outside))
                connection.execute(blacklist.update().values(expires_at=time() - 1))
            with app.state.database.connect() as connection:
                self.assertIsNone(ip_block(connection, matched))
