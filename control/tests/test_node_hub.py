import asyncio
import json
import ssl
import subprocess
import unittest
from contextlib import suppress
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import select
from support import seed_node

from app.database import audit_logs, open_database
from app.node_hub import NodeHub, server_tls
from app.registry import nodes, tunnels
from app.tunnel_commands import acknowledge_command, commands, enqueue_command


class NodeHubTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        directory = Path(cls.enterClassContext(TemporaryDirectory()))
        cls.cert = str(directory / "cert.pem")
        cls.key = str(directory / "key.pem")
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-keyout",
                cls.key,
                "-out",
                cls.cert,
            ],
            check=True,
            capture_output=True,
        )
        cls.clients = {}
        for label, node_id in (
            ("current", "test-node"),
            ("renewed", "test-node"),
            ("other", "other-node"),
        ):
            cert, key, request, extensions = (
                directory / f"{label}.{suffix}"
                for suffix in ("pem", "key", "csr", "ext")
            )
            extensions.write_text(
                "basicConstraints=critical,CA:FALSE\n"
                "keyUsage=critical,digitalSignature\n"
                "extendedKeyUsage=clientAuth\n"
                f"subjectAltName=URI:urn:minecrafttools:node:{node_id}\n"
            )
            subprocess.run(
                [
                    "openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes",
                    "-subj", f"/CN={label}", "-keyout", str(key), "-out", str(request),
                ],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "openssl", "x509", "-req", "-days", "1", "-in", str(request),
                    "-CA", cls.cert, "-CAkey", cls.key, "-CAcreateserial",
                    "-extfile", str(extensions), "-out", str(cert),
                ],
                check=True,
                capture_output=True,
            )
            cls.clients[label] = (cert, key)
        (directory / "index.txt").touch()
        config = directory / "ca.cnf"
        config.write_text(
            "[ca]\ndefault_ca = local\n"
            "[local]\n"
            f"database = {directory / 'index.txt'}\n"
            f"new_certs_dir = {directory}\n"
            f"certificate = {cls.cert}\n"
            f"private_key = {cls.key}\n"
            "default_md = sha256\n"
            "default_crl_days = 1\n"
        )
        cls.crl = str(directory / "revoked.pem")
        for args in (
            ["-revoke", str(cls.clients["current"][0])],
            ["-gencrl", "-out", cls.crl],
        ):
            subprocess.run(
                ["openssl", "ca", "-batch", "-config", str(config), *args],
                check=True,
                capture_output=True,
            )

    async def asyncSetUp(self):
        home = Path(self.enterContext(TemporaryDirectory()))
        self.db = open_database(home)
        self.addCleanup(self.db.dispose)
        self.secret = "random node test secret"
        with self.db.begin() as connection:
            seed_node(
                connection,
                "test-node",
                name="Test Node",
                public_host="node.test",
                token_hash=sha256(self.secret.encode()).hexdigest(),
                client_channel="both",
            )
        self.hub = NodeHub(self.db)
        await self.hub.start("127.0.0.1", 0, server_tls(self.cert, self.key))
        self.addAsyncCleanup(self.hub.close)

    async def connect(self, client=None):
        context = ssl.create_default_context(cafile=self.cert)
        if client is not None:
            context.load_cert_chain(*self.clients[client])
        port = self.hub.server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection(
            "127.0.0.1",
            port,
            ssl=context,
            server_hostname="localhost",
        )

        async def close():
            writer.close()
            with suppress(ConnectionResetError, ssl.SSLError):
                await writer.wait_closed()

        self.addAsyncCleanup(close)
        return reader, writer

    async def test_mutual_tls_requires_certificate_bound_to_node_id(self):
        await self.hub.close()
        await self.hub.start(
            "127.0.0.1", 0, server_tls(self.cert, self.key, client_ca=self.cert)
        )
        with suppress(ConnectionResetError, ssl.SSLError):
            reader, writer = await self.connect()
            await self.send(writer, "hello", 1, self.hello(self.secret))
            self.assertEqual(await reader.readline(), b"")
        self.assertEqual(self.statuses(), ("offline", {}))

        reader, writer = await self.connect("other")
        await self.send(writer, "hello", 1, self.hello(self.secret))
        self.assertEqual(await reader.readline(), b"")
        self.assertEqual(self.statuses(), ("offline", {}))

        for client in ("current", "renewed"):
            reader, writer = await self.connect(client)
            await self.send(writer, "hello", 1, self.hello(self.secret))
            self.assertEqual(json.loads(await reader.readline())["type"], "hello_ack")
            await self.send(writer, "heartbeat", 2, {})
            self.assertEqual(
                json.loads(await reader.readline())["type"], "heartbeat_ack"
            )
        self.assertEqual(self.statuses(), ("online", {}))

    async def test_mutual_tls_revokes_old_certificate_after_rotation(self):
        await self.hub.close()
        await self.hub.start(
            "127.0.0.1",
            0,
            server_tls(self.cert, self.key, client_ca=self.cert, client_crl=self.crl),
        )
        with suppress(ConnectionResetError, ssl.SSLError):
            reader, writer = await self.connect("current")
            await self.send(writer, "hello", 1, self.hello(self.secret))
            self.assertEqual(await reader.readline(), b"")
        self.assertEqual(self.statuses(), ("offline", {}))

        reader, writer = await self.connect("renewed")
        await self.send(writer, "hello", 1, self.hello(self.secret))
        self.assertEqual(json.loads(await reader.readline())["type"], "hello_ack")
        self.assertEqual(self.statuses(), ("online", {}))

    async def send(self, writer, message_type, seq, payload):
        writer.write(
            json.dumps({"type": message_type, "seq": seq, "payload": payload}).encode()
            + b"\n"
        )
        await writer.drain()

    def hello(self, secret):
        return {
            "protocol_version": 1,
            "node_id": "test-node",
            "secret": secret,
            "agent_version": "test",
            "frps_version": "0.72.0",
        }

    async def test_disconnect_cancels_a_connected_session(self):
        async def idle():
            await asyncio.sleep(30)

        task = asyncio.create_task(idle())
        self.hub.sessions["test-node"] = task
        await self.hub.disconnect("test-node")
        self.assertTrue(task.cancelled())
        self.assertNotIn("test-node", self.hub.sessions)

    def statuses(self):
        with self.db.connect() as connection:
            node_state = connection.execute(select(nodes.c.status)).scalar_one()
            proxy_states = dict(
                connection.execute(select(tunnels.c.proxy_name, tunnels.c.status)).all()
            )
        return node_state, proxy_states

    async def test_authenticated_snapshot_and_disconnect(self):
        reader, writer = await self.connect()
        await self.send(writer, "hello", 1, self.hello(self.secret))
        self.assertEqual(json.loads(await reader.readline())["type"], "hello_ack")
        first = {"run_id": "run-a", "proxy_name": "alpha", "remote_port": 30001}
        second = {"run_id": "run-a", "proxy_name": "beta", "remote_port": 30002}
        await self.send(writer, "tunnel_snapshot", 2, {"tunnels": [first, second]})
        self.assertEqual(json.loads(await reader.readline())["seq"], 2)
        self.assertEqual(
            self.statuses(), ("online", {"alpha": "pending", "beta": "pending"})
        )
        await self.send(writer, "heartbeat", 3, {})
        self.assertEqual(json.loads(await reader.readline())["type"], "heartbeat_ack")
        await self.send(writer, "tunnel_snapshot", 4, {"tunnels": [second]})
        self.assertEqual(
            json.loads(await reader.readline())["type"], "tunnel_snapshot_ack"
        )
        self.assertEqual(
            self.statuses(), ("online", {"alpha": "closed", "beta": "pending"})
        )

        writer.close()
        await writer.wait_closed()
        async with asyncio.timeout(3):
            while self.statuses()[0] != "offline":
                await asyncio.sleep(0.01)
        self.assertEqual(
            self.statuses(), ("offline", {"alpha": "closed", "beta": "offline"})
        )

    async def test_refuses_wrong_secret_and_port_outside_registry(self):
        reader, writer = await self.connect()
        await self.send(writer, "hello", 1, self.hello("incorrect"))
        self.assertEqual(await reader.readline(), b"")
        self.assertEqual(self.statuses(), ("offline", {}))

        reader, writer = await self.connect()
        await self.send(writer, "hello", 1, self.hello(self.secret))
        self.assertEqual(json.loads(await reader.readline())["type"], "hello_ack")
        await self.send(
            writer,
            "tunnel_snapshot",
            2,
            {
                "tunnels": [
                    {
                        "run_id": "run-a",
                        "proxy_name": "outside",
                        "remote_port": 30099,
                    }
                ]
            },
        )
        self.assertEqual(await reader.readline(), b"")
        self.assertEqual(self.statuses(), ("offline", {}))

    async def test_retries_lost_command_ack_and_accepts_duplicate_ack(self):
        reader, writer = await self.connect()
        await self.send(writer, "hello", 1, self.hello(self.secret))
        self.assertEqual(json.loads(await reader.readline())["type"], "hello_ack")
        proxy = {"run_id": "run-a", "proxy_name": "alpha", "remote_port": 30001}
        await self.send(writer, "tunnel_snapshot", 2, {"tunnels": [proxy]})
        self.assertEqual(
            json.loads(await reader.readline())["type"], "tunnel_snapshot_ack"
        )
        with self.db.connect() as connection:
            tunnel_id = connection.execute(select(tunnels.c.id)).scalar_one()

        async def exchange(kind, seq, payload):
            await self.send(writer, kind, seq, payload)
            return json.loads(await reader.readline())["payload"]

        with patch("app.tunnel_commands.time", return_value=100) as clock:
            queued = enqueue_command(self.db, tunnel_id, "block", "test", "admin:operator")
            cmd_id = queued["command_id"]
            self.assertEqual(
                enqueue_command(self.db, tunnel_id, "block", "retry", "admin:operator"),
                queued,
            )
            self.assertFalse(
                acknowledge_command(self.db, "test-node", cmd_id, True, "")
            )
            first = await exchange("heartbeat", 3, {})
            self.assertEqual(
                first,
                {
                    "commands": [
                        {
                            "run_id": "run-a",
                            "proxy_name": "alpha",
                            "port": 30001,
                            "command_id": cmd_id,
                            "action": "block",
                        }
                    ]
                },
            )
            clock.return_value = 114
            self.assertFalse((await exchange("heartbeat", 4, {})).get("commands"))
            self.assertEqual(self.statuses(), ("online", {"alpha": "block_pending"}))
            clock.return_value = 115
            self.assertEqual(await exchange("heartbeat", 5, {}), first)
            self.assertFalse(
                acknowledge_command(self.db, "other-node", cmd_id, True, "")
            )
            rejected = {
                "command_id": cmd_id,
                "ok": False,
                "error": "helper temporarily unavailable",
            }
            await exchange("ack", 6, rejected)
            self.assertEqual(self.statuses(), ("online", {"alpha": "block_pending"}))
            clock.return_value = 130
            self.assertEqual(await exchange("heartbeat", 7, {}), first)
            ack = {"command_id": cmd_id, "ok": True}
            await exchange("ack", 8, ack)
            await exchange("ack", 9, ack)
            self.assertEqual(self.statuses(), ("online", {"alpha": "blocked"}))
            self.assertFalse((await exchange("heartbeat", 10, {})).get("commands"))
        with self.db.connect() as connection:
            status, attempts, acked_at = connection.execute(
                select(commands.c.status, commands.c.attempts, commands.c.acked_at)
            ).one()
            audit = select(audit_logs.c.actor, audit_logs.c.action, audit_logs.c.target)
            self.assertEqual(
                connection.execute(audit).all(),
                [("operator", "tunnel.block", tunnel_id)],
            )
        self.assertEqual((status, attempts, acked_at), ("acked", 3, 130))
