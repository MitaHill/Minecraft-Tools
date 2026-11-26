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

from app.database import audit_logs
from app.main import app
from app.moderation import acknowledge_policy, blacklist, node_policy
from app.registry import nodes, tunnels
from app.scanner import scans


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
            "port_start": 30001,
            "port_end": 30002,
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

    def register_policy_node(self, node_id):
        with app.state.database.begin() as connection:
            connection.execute(insert(nodes).values(
                id=node_id, name=node_id, public_host=f"{node_id}.example",
                port_start=30001, port_end=30004, token_hash="fixture",
                status="online", deployed_ports=[[30001, 30008]], blacklist_revision=0,
            ))

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
        payload = {"name": "A", "public_host": "a.example", "port_start": 30003, "port_end": 30007}
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
        self.assertEqual(self.http.get("/api/v1/nodes").json()["nodes"][0]["port_start"], 30001)
        policy = node_policy(app.state.database, "a")["range"]
        acknowledge_policy(app.state.database, "a", "range", policy["revision"], "port occupied")
        row = self.http.get("/api/admin/nodes").json()["nodes"][0]
        self.assertEqual((row["port_start"], row["range_error"]), (30001, "port occupied"))
        self.assertEqual(self.http.patch("/api/admin/nodes/a", json=payload).status_code, 200)
        policy = node_policy(app.state.database, "a")["range"]
        acknowledge_policy(app.state.database, "a", "range", policy["revision"], "")
        row = self.http.get("/api/v1/nodes").json()["nodes"][0]
        self.assertEqual((row["port_start"], row["port_end"]), (30003, 30007))
        self.assertNotIn("token_hash", row)
