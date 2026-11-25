import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from app.database import audit_logs
from app.main import app
from app.registry import tunnels
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
