import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select
from support import seed_node

from app.client_api import digest
from app.java_probe import Status
from app.main import app
from app.moderation import blacklist
from app.node_hub import NodeHub, Proxy
from app.node_ports import node_port_ranges
from app.registry import blocks, installs, nodes, tunnel_claims, tunnels
from app.scanner import ScanPolicy, record_scan
from app.tunnel_commands import (
    acknowledge_command,
    commands,
    enqueue_command,
    next_command,
)


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
            "deployed_ports": [[30001, 30002]],
        }
        self.other_node = {**self.node, "id": "other-node"}
        with self.db.begin() as connection:
            for node in (self.node, self.other_node):
                seed_node(
                    connection,
                    node["id"],
                    public_host=node["public_host"],
                    deployed_ports=node["deployed_ports"],
                    client_channel="both",
                    server_port=7000,
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

    def reserve(self, headers, channel=None, node_id=None):
        payload = {"node_id": node_id or self.node["id"]}
        if channel is not None:
            payload["channel"] = channel
        return self.http.post(
            "/api/v1/tunnels/register",
            headers=headers,
            json=payload,
        )

    def tunnel_owner(self):
        with self.db.connect() as connection:
            return connection.execute(
                select(tunnels.c.install_id).where(tunnels.c.node_id == self.node["id"])
            ).scalar_one()

    def publish_node(self, **changes):
        values = {
            "client_channel": "both", "server_addr": "frp.example.test", "server_port": 7000,
            **changes,
        }
        with self.db.begin() as connection:
            connection.execute(nodes.update().where(nodes.c.id == self.node["id"]).values(**values))

    def node_row(self, **changes):
        if changes:
            with self.db.begin() as connection:
                connection.execute(nodes.update().where(nodes.c.id == self.node["id"]).values(**changes))
        with self.db.connect() as connection:
            return connection.execute(
                select(nodes).where(nodes.c.id == self.node["id"])
            ).mappings().one()

    def add_port(self, port):
        with self.db.begin() as connection:
            connection.execute(node_port_ranges.insert().values(
                node_id=self.node["id"], pending=False, start_port=port, end_port=port,
            ))

    def test_registration_enforces_node_publish_channel(self):
        def claim(channel, headers):
            response = self.reserve(headers, channel)
            if response.status_code == 201:
                self.assertEqual(self.http.delete(
                    f"/api/v1/tunnels/{response.json()['id']}", headers=headers,
                ).status_code, 204)
            return response

        self.publish_node(client_channel="dev")
        self.assertEqual(claim(None, self.owner).status_code, 404)
        self.assertEqual(claim("stable", self.owner).status_code, 404)
        self.assertEqual(claim("dev", self.owner).status_code, 201)

        self.publish_node(client_channel="both")
        self.assertEqual(claim(None, self.owner).status_code, 201)
        self.assertEqual(claim("dev", self.other).status_code, 201)

        self.publish_node(client_channel="stable")
        self.assertEqual(claim("dev", self.owner).status_code, 404)
        self.assertEqual(claim("stable", self.owner).status_code, 201)

        self.publish_node(client_channel="hidden")
        self.assertEqual(claim(None, self.owner).status_code, 404)
        self.assertEqual(claim("dev", self.owner).status_code, 404)

    def test_claimed_proxy_is_not_blocked(self):
        self.publish_node(client_channel="both")
        created = self.reserve(self.owner)
        self.assertEqual(created.status_code, 201)
        claim = created.json()
        self.hub.record_snapshot(self.node_row(), [Proxy(
            run_id="run-claimed", proxy_name=claim["proxy_name"],
            remote_port=claim["remote_port"], claim_id=claim["id"],
            claim_hash=digest(claim["proof"]), client_ip="8.8.8.8",
        )])
        with self.db.connect() as connection:
            row = connection.execute(select(tunnels)).mappings().one()
            self.assertIsNotNone(row["install_id"])
            self.assertEqual(row["status"], "pending")
            self.assertEqual(connection.execute(select(commands)).all(), [])

    def test_claimed_proxy_on_hidden_node_is_closed_without_ban(self):
        self.publish_node(client_channel="both")
        created = self.reserve(self.owner)
        self.assertEqual(created.status_code, 201)
        claim = created.json()
        self.hub.record_snapshot(self.node_row(client_channel="hidden"), [Proxy(
            run_id="run-claimed", proxy_name=claim["proxy_name"],
            remote_port=claim["remote_port"], claim_id=claim["id"],
            claim_hash=digest(claim["proof"]), client_ip="8.8.8.8",
        )])
        with self.db.connect() as connection:
            row = connection.execute(select(tunnels)).mappings().one()
            self.assertIsNotNone(row["install_id"])
            self.assertEqual(row["status"], "block_pending")
            self.assertEqual(
                connection.execute(select(blocks.c.source)).scalar_one(),
                "node-hidden",
            )
            self.assertEqual(connection.execute(select(blacklist)).all(), [])

    def test_unclaimed_proxy_is_blocked_after_grace(self):
        node = self.node_row(client_channel="both")
        proxy = Proxy(
            run_id="rogue", proxy_name="rogue", remote_port=30001,
            client_ip="8.8.8.8",
        )
        self.hub.record_snapshot(node, [proxy])
        with self.db.connect() as connection:
            row = connection.execute(select(tunnels)).mappings().one()
            self.assertEqual((row["status"], row["install_id"]), ("pending", None))
            self.assertEqual(connection.execute(select(commands)).all(), [])
        with self.db.begin() as connection:
            connection.execute(
                tunnels.update().where(tunnels.c.id == row["id"])
                .values(created_at=time() - 31)
            )
        self.hub.record_snapshot(node, [proxy])
        with self.db.connect() as connection:
            self.assertEqual(
                connection.execute(select(tunnels.c.status)).scalar_one(),
                "block_pending",
            )
            self.assertEqual(
                connection.execute(select(commands.c.action)).scalar_one(), "block"
            )
            self.assertEqual(
                connection.execute(select(blocks.c.source)).scalar_one(),
                "unauthorized",
            )

    def test_unclaimed_proxy_on_hidden_node_is_blocked_immediately(self):
        node = self.node_row(client_channel="hidden")
        self.hub.record_snapshot(node, [Proxy(
            run_id="rogue", proxy_name="rogue", remote_port=30001,
            client_ip="8.8.8.8",
        )])
        with self.db.connect() as connection:
            self.assertEqual(
                connection.execute(select(tunnels.c.status)).scalar_one(),
                "block_pending",
            )

    def test_repeated_unclaimed_proxies_ban_public_ip(self):
        node = self.node_row(client_channel="hidden", deployed_ports=[[30001, 30003]])
        self.hub.record_snapshot(node, [
            Proxy(
                run_id="rogue", proxy_name=f"rogue-{port}", remote_port=port,
                client_ip="8.8.8.8",
            )
            for port in (30001, 30002, 30003)
        ])
        with self.db.connect() as connection:
            self.assertEqual(
                set(connection.execute(select(tunnels.c.status)).scalars()),
                {"block_pending"},
            )
            entry = connection.execute(select(blacklist)).mappings().one()
            self.assertEqual(
                (entry["ip"], entry["actor"]), ("8.8.8.8", "system:unauthorized")
            )

    def test_concurrent_automatic_requests_reserve_distinct_ports_then_exhaust(self):
        self.publish_node(frp_token="fixture-frpc-token")
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(self.reserve, (self.owner, self.other))
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertEqual({first.json()["remote_port"], second.json()["remote_port"]}, {30001, 30002})
        self.assertEqual(first.json()["server_addr"], "frp.example.test")
        self.assertEqual(first.json()["server_port"], 7000)
        self.assertEqual(first.json()["public_host"], "game.example.test")
        self.assertEqual(first.json()["frp_token"], "fixture-frpc-token")
        exhausted = self.reserve(self.owner)
        self.assertEqual(exhausted.status_code, 409)
        self.assertEqual(exhausted.json()["detail"]["code"], "no_available_port")
        self.assertEqual(self.http.delete(
            f"/api/v1/tunnels/{first.json()['id']}", headers=self.owner,
        ).status_code, 204)
        self.assertEqual(self.reserve(self.owner).json()["remote_port"],
                         first.json()["remote_port"])

    def test_automatic_selection_excludes_live_and_blocked_then_uses_current_endpoint(self):
        self.add_port(30003)
        self.publish_node(server_addr=None, server_port=17000)
        with self.db.begin() as connection:
            connection.execute(tunnels.insert().values(
                id="live", node_id=self.node["id"], run_id="run", proxy_name="live",
                remote_port=30001, status="active",
            ))
            connection.execute(tunnels.insert().values(
                id="blocked", node_id=self.node["id"], run_id="run", proxy_name="blocked",
                remote_port=30002, status="blocked", proxy_present=False,
            ))
            connection.execute(blocks.insert().values(
                tunnel_id="blocked", node_id=self.node["id"], run_id="run", proxy_name="blocked",
                port=30002, reason="fixture", source="manual", active=True, created_at=time(),
            ))
        response = self.reserve(self.owner)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["remote_port"], 30003)
        self.assertEqual(response.json()["server_addr"], "game.example.test")
        self.assertEqual(response.json()["server_port"], 17000)

    def test_owned_status_rejects_forged_claims_and_releases_port_after_unblock(self):
        self.assertEqual(self.reserve({}).status_code, 401)
        created = self.reserve(self.owner)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.headers["Cache-Control"], "no-store")
        claim = created.json()
        base = f"/api/v1/tunnels/{claim['id']}"
        status = base + "/status"
        other_claim = self.reserve(self.other)
        self.assertEqual(other_claim.status_code, 201)
        self.assertEqual(self.http.delete(
            f"/api/v1/tunnels/{other_claim.json()['id']}", headers=self.other,
        ).status_code, 204)
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
        reused = self.reserve(self.owner)
        self.assertEqual(reused.status_code, 201)
        self.assertEqual(reused.json()["remote_port"], 30002)
        unblock = enqueue_command(self.db, tunnel_id, "unblock", "", "operator")
        self.assertEqual(
            next_command(self.db, self.node["id"])["command_id"], unblock["command_id"]
        )
        acknowledge_command(self.db, self.node["id"], unblock["command_id"], True, "")
        self.assertEqual(self.reserve(self.other).status_code, 201)
        self.assertEqual(self.http.get(status, headers=self.owner).status_code, 404)

    def test_scanner_block_returns_custom_message_to_client(self):
        claim = self.reserve(self.owner).json()
        self.hub.record_snapshot(
            self.node,
            [
                Proxy(
                    run_id="run-scan",
                    proxy_name=claim["proxy_name"],
                    remote_port=30001,
                    claim_id=claim["id"],
                    claim_hash=digest(claim["proof"]),
                )
            ],
        )
        with self.db.connect() as connection:
            tunnel = (
                connection.execute(
                    select(tunnels).where(tunnels.c.node_id == self.node["id"])
                )
                .mappings()
                .one()
            )
        now = time()
        record_scan(
            self.db,
            {
                "id": tunnel["id"],
                "run_id": tunnel["run_id"],
                "remote_port": tunnel["remote_port"],
            },
            Status("not_minecraft"),
            ScanPolicy(grace=0, failures=1),
            now,
            True,
        )
        response = self.http.get(
            f"/api/v1/tunnels/{claim['id']}/status", headers=self.owner
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            (response.json()["status"], response.json()["reason"]),
            ("block_pending", "未检测到 Minecraft Java 响应"),
        )

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
