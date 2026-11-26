import asyncio
import json
import socket
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import select
from support import seed_node

from app.database import open_database
from app.java_probe import Status, encode_length, probe, read_length
from app.registry import tunnels
from app.scanner import ScanPolicy, record_scan, scans, select_due
from app.tunnel_commands import next_command


class JavaProbeTests(unittest.IsolatedAsyncioTestCase):
    async def listen(self, handler):
        async def serve(reader, writer):
            try:
                await handler(reader, writer)
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        await self.enterAsyncContext(server)
        return server.sockets[0].getsockname()[1]

    async def test_modern_accepts_empty_motd_and_requires_approved_address(self):
        async def handler(reader, writer):
            hello = await reader.readexactly(await read_length(reader))
            request = await reader.readexactly(await read_length(reader))
            self.assertEqual(hello[-1:], b"\x01")
            self.assertEqual(request, b"\x00")
            status = json.dumps(
                {
                    "version": {"name": "1.21.4", "protocol": 769},
                    "players": {"online": 0, "max": 20},
                    "description": {"text": ""},
                }
            ).encode()
            packet = b"\x00" + encode_length(len(status)) + status
            writer.write(encode_length(len(packet)) + packet)
            await writer.drain()

        port = await self.listen(handler)
        rejected = await probe("127.0.0.1", port)
        self.assertEqual(rejected.result, "unknown")
        permitted = await probe("127.0.0.1", port, allow_private=True)
        self.assertEqual(
            (permitted.result, permitted.version, permitted.motd), ("ok", "1.21.4", "")
        )

    async def test_legacy_fallback(self):
        async def handler(reader, writer):
            header = await reader.readexactly(2)
            if header == b"\xfe\x01":
                status = "§1\x0078\x001.6.4\x00Legacy test\x000\x0020".encode(
                    "utf-16-be"
                )
                writer.write(b"\xff" + (len(status) // 2).to_bytes(2, "big") + status)
            else:
                await reader.readexactly(header[0] + 1)
                writer.write(b"\x01\x00")
            await writer.drain()

        port = await self.listen(handler)
        status = await probe("127.0.0.1", port, allow_private=True)
        self.assertEqual(
            (status.result, status.version, status.motd), ("ok", "1.6.4", "Legacy test")
        )

    async def test_non_minecraft_is_distinct_from_unreachable(self):
        async def handler(reader, writer):
            header = await reader.readexactly(2)
            if header != b"\xfe\x01":
                await reader.readexactly(header[0] + 1)
            writer.write(b"HTTP/1.0 404\r\n\r\n")
            await writer.drain()

        port = await self.listen(handler)
        invalid = await probe("127.0.0.1", port, allow_private=True)
        self.assertEqual(invalid.result, "not_minecraft")
        with socket.socket() as offline:
            offline.bind(("127.0.0.1", 0))
            failure = await probe(
                "127.0.0.1", offline.getsockname()[1], allow_private=True
            )
        self.assertEqual(failure.result, "unreachable")


class ScanPolicyTests(unittest.TestCase):
    def setUp(self):
        home = Path(self.enterContext(TemporaryDirectory()))
        self.database = open_database(home)
        self.addCleanup(self.database.dispose)
        self.policy = ScanPolicy()
        with self.database.begin() as connection:
            seed_node(
                connection,
                "test-node",
                ranges=((30001, 30002), (30005, 30005)),
                name="Test",
                public_host="node.test",
                token_hash="test-token-hash",
            )
            connection.execute(
                tunnels.insert().values(
                    id="tested",
                    node_id="test-node",
                    run_id="run-a",
                    proxy_name="test-proxy",
                    remote_port=30001,
                    status="pending",
                    created_at=100,
                    next_scan_at=100,
                )
            )
        (self.target,) = select_due(self.database, 100)

    def state(self):
        with self.database.connect() as connection:
            return connection.execute(
                select(tunnels.c.status, tunnels.c.scan_failures)
            ).one()

    def test_requires_consecutive_failures_after_grace_then_queues_block(self):
        invalid = Status("not_minecraft")
        record_scan(self.database, self.target, invalid, self.policy, 140, True)
        self.assertEqual(self.state(), ("pending", 0))
        record_scan(self.database, self.target, invalid, self.policy, 161, True)
        record_scan(self.database, self.target, Status("ok"), self.policy, 191, True)
        self.assertEqual(self.state(), ("active", 0))
        for now in (222, 252, 282):
            record_scan(self.database, self.target, invalid, self.policy, now, False)
        self.assertEqual(self.state(), ("active", 3))
        record_scan(self.database, self.target, invalid, self.policy, 312, True)
        self.assertEqual(self.state(), ("block_pending", 3))
        command = next_command(self.database, "test-node")
        self.assertEqual(
            (command["action"], command["port"], command["run_id"]),
            ("block", 30001, "run-a"),
        )

    def test_unreachable_ages_to_offline_and_old_generation_results_are_discarded(self):
        record_scan(
            self.database, self.target, Status("unreachable"), self.policy, 701, True
        )
        self.assertEqual(self.state(), ("offline", 0))
        with self.database.begin() as connection:
            connection.execute(
                tunnels.update().values(run_id="run-b", remote_port=30002)
            )
        record_scan(self.database, self.target, Status("ok"), self.policy, 732, True)
        self.assertEqual(self.state(), ("offline", 0))
        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute(select(scans.c.result)).scalars().all(),
                ["unreachable"],
            )
        self.assertIsNone(next_command(self.database, "test-node"))

    def test_tunnels_outside_configured_ranges_are_skipped(self):
        with self.database.begin() as connection:
            connection.execute(
                tunnels.insert().values(
                    id="outside",
                    node_id="test-node",
                    run_id="run-b",
                    proxy_name="outside",
                    remote_port=30003,
                    status="pending",
                    created_at=100,
                    next_scan_at=100,
                )
            )
        self.assertEqual([item["id"] for item in select_due(self.database, 100)], ["tested"])
