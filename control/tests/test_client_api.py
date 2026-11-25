import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.client_api import digest
from app.main import app
from app.node_hub import NodeHub, Proxy
from app.registry import installs, nodes, tunnel_claims, tunnels
from app.tunnel_commands import acknowledge_command, enqueue_command, next_command


class ClientTunnelTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.dict("os.environ", {"CONTROL_DATA_DIR": str(root)}))
        self.http = self.enterContext(TestClient(app, base_url="https://control.test"))
        self.db = app.state.database
        self.hub = NodeHub(self.db)
        self.node = {
            "id": "game-node",
            "public_host": "game.example.test",
            "port_start": 30001,
            "port_end": 30002,
        }
        self.other_node = {**self.node, "id": "other-node"}
        with self.db.begin() as connection:
            for node in (self.node, self.other_node):
                connection.execute(
                    nodes.insert().values(
                        **node, name=node["id"], token_hash="fixture", status="online"
                    )
                )
        self.owner = self.enroll()
        self.other = self.enroll()

    def enroll(self):
        response = self.http.post("/api/v1/installs/register")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        data = response.json()
        with self.db.connect() as connection:
            actual = connection.execute(
                select(installs.c.token_hash).where(installs.c.id == data["install_id"])
            ).scalar_one()
        self.assertEqual(actual, digest(data["token"]))
        return {"Authorization": "Bearer " + data["token"]}

    def reserve(self, headers, port=30001):
        return self.http.post(
            "/api/v1/tunnels/register",
            headers=headers,
            json={"node_id": self.node["id"], "remote_port": port},
        )

    def tunnel_owner(self):
        with self.db.connect() as connection:
            return connection.execute(
                select(tunnels.c.install_id).where(tunnels.c.node_id == self.node["id"])
            ).scalar_one()

    def test_owned_status_rejects_forged_claims_and_releases_port_after_unblock(self):
        self.assertEqual(self.reserve({}).status_code, 401)
        self.assertEqual(self.reserve(self.owner, 30003).status_code, 422)
        created = self.reserve(self.owner)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.headers["Cache-Control"], "no-store")
        claim = created.json()
        base = f"/api/v1/tunnels/{claim['id']}"
        status = base + "/status"
        self.assertEqual(self.reserve(self.other).status_code, 409)
        self.assertEqual(self.http.get(status, headers=self.other).status_code, 404)
        self.assertEqual(
            self.http.post(base + "/heartbeat", headers=self.other).status_code, 404
        )
        self.assertEqual(self.http.delete(base, headers=self.other).status_code, 204)
        self.assertEqual(
            self.http.get(status, headers=self.owner).json()["status"], "pending"
        )

        report = Proxy(
            run_id="run-a",
            proxy_name=claim["proxy_name"],
            remote_port=30001,
            claim_id=claim["id"],
            claim_hash=digest(claim["proof"]),
        )
        self.hub.record_snapshot(self.other_node, [report])
        self.hub.record_snapshot(
            self.node, [report.model_copy(update={"claim_hash": "0" * 64})]
        )
        self.assertIsNone(self.tunnel_owner())
        with self.db.connect() as connection:
            self.assertIsNone(
                connection.execute(select(tunnel_claims.c.bound_at)).scalar_one()
            )

        self.hub.record_snapshot(
            self.node, [report.model_copy(update={"remote_port": 30002})]
        )
        self.assertIsNone(self.tunnel_owner())
        self.hub.record_snapshot(self.node, [report])
        self.assertIsNotNone(self.tunnel_owner())
        self.hub.record_snapshot(
            self.node, [report.model_copy(update={"run_id": "run-b"})]
        )
        self.assertIsNotNone(self.tunnel_owner())
        with self.db.connect() as connection:
            tunnel_id = connection.execute(
                select(tunnels.c.id).where(tunnels.c.node_id == self.node["id"])
            ).scalar_one()
        block = enqueue_command(
            self.db, tunnel_id, "block", "Integration rule", "operator"
        )
        self.assertEqual(
            self.http.get(status, headers=self.owner).json()["reason"],
            "Integration rule",
        )
        self.assertEqual(self.http.get(status, headers=self.other).status_code, 404)
        self.assertEqual(
            next_command(self.db, self.node["id"])["command_id"], block["command_id"]
        )
        acknowledge_command(self.db, self.node["id"], block["command_id"], True, "")
        self.hub.record_snapshot(self.node, [])
        self.assertEqual(
            self.http.post(base + "/heartbeat", headers=self.owner).json()["status"],
            "blocked",
        )
        self.assertEqual(self.http.delete(base, headers=self.owner).status_code, 204)
        self.assertEqual(self.reserve(self.owner).status_code, 409)
        unblock = enqueue_command(self.db, tunnel_id, "unblock", "", "operator")
        self.assertEqual(
            next_command(self.db, self.node["id"])["command_id"], unblock["command_id"]
        )
        acknowledge_command(self.db, self.node["id"], unblock["command_id"], True, "")
        self.assertEqual(self.reserve(self.other).status_code, 201)
        self.assertEqual(self.http.get(status, headers=self.owner).status_code, 404)

    def test_expired_claim_can_be_replaced_but_cannot_be_renewed(self):
        old = self.reserve(self.owner).json()
        with self.db.begin() as connection:
            connection.execute(tunnel_claims.update().values(expires_at=time() - 1))
        self.assertEqual(
            self.http.post(
                f"/api/v1/tunnels/{old['id']}/heartbeat", headers=self.owner
            ).status_code,
            404,
        )
        self.assertEqual(self.reserve(self.other).status_code, 201)
        self.hub.record_snapshot(
            self.node,
            [
                Proxy(
                    run_id="stale",
                    proxy_name=old["proxy_name"],
                    remote_port=30001,
                    claim_id=old["id"],
                    claim_hash=digest(old["proof"]),
                )
            ],
        )
        self.assertIsNone(self.tunnel_owner())
