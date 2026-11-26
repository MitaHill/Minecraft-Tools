import asyncio
import json
import logging
import ssl
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest
from time import time
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import case, false, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from .ip_rules import canonical_ip
from .moderation import (
    acknowledge_policy,
    active_blacklist,
    active_cidr,
    ban_unclaimed_flood,
    blacklist,
    lock_settings,
    node_policy,
)
from .node_ports import load_ranges
from .registry import blocks, nodes, tunnel_claims, tunnels
from .tunnel_commands import (
    CommandConflict,
    acknowledge_command,
    enqueue_command,
    next_command,
)

logger = logging.getLogger(__name__)
MAX_MESSAGE_SIZE = 1024 * 1024
HELLO_TIMEOUT = 10
HEARTBEAT_TIMEOUT = 30
HEARTBEAT_WRITE_INTERVAL = 10
UNCLAIMED_GRACE = 30.0
UNCLAIMED_BLOCK_REASON = "未授权隧道：没有有效的预约凭证"


class ProtocolError(ValueError):
    pass


class Message(BaseModel):
    type: str
    seq: int = Field(ge=1, le=2**63 - 1, strict=True)
    payload: dict[str, Any]


class Hello(BaseModel):
    protocol_version: Literal[1]
    node_id: str = Field(min_length=1, max_length=63)
    secret: str = Field(min_length=1, max_length=128)
    agent_version: str = Field(min_length=1, max_length=80)
    frps_version: str = Field(min_length=1, max_length=80)
    public_host: str | None = Field(default=None, max_length=253)
    deployed_ports: list[tuple[int, int]] | None = Field(default=None, max_length=128)
    blacklist_cidr: bool = Field(default=False, strict=True)


class Proxy(BaseModel):
    run_id: str = Field(min_length=1, max_length=200)
    proxy_name: str = Field(min_length=1, max_length=200)
    remote_port: int = Field(ge=1, le=65535)
    claim_id: str | None = Field(default=None, max_length=32)
    claim_hash: str | None = Field(default=None, max_length=64)
    client_ip: str | None = Field(default=None, max_length=64)


class Snapshot(BaseModel):
    tunnels: list[Proxy] = Field(max_length=4096)


class CommandAcknowledgement(BaseModel):
    command_id: int = Field(ge=1, le=2**63 - 1, strict=True)
    ok: bool = Field(strict=True)
    error: str = Field(default="", max_length=300)


class PolicyAcknowledgement(BaseModel):
    kind: Literal["range", "blacklist"]
    revision: int = Field(ge=0, le=2**63 - 1, strict=True)
    error: str = Field(default="", max_length=300)


def server_tls(
    certfile: str, keyfile: str, client_ca: str = "", client_crl: str = ""
) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile, keyfile)
    if client_crl and not client_ca:
        raise ValueError("The node client CRL requires a client CA")
    if client_ca:
        context.load_verify_locations(cafile=client_ca)
        context.verify_mode = ssl.CERT_REQUIRED
    if client_crl:
        context.load_verify_locations(cafile=client_crl)
        context.verify_flags |= ssl.VERIFY_CRL_CHECK_LEAF
    return context


@dataclass
class TLSNodeConnection:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    async def receive(self) -> bytes | None:
        try:
            return await self.reader.readuntil(b"\n")
        except asyncio.IncompleteReadError as error:
            if not error.partial:
                return None
            raise ProtocolError("truncated message") from None
        except asyncio.LimitOverrunError:
            raise ProtocolError("message too large") from None

    async def send(self, body: bytes):
        self.writer.write(body)
        await self.writer.drain()

    async def close(self):
        self.writer.close()
        await self.writer.wait_closed()

    def verify_identity(self, node_id: str):
        tls = self.writer.get_extra_info("ssl_object")
        if tls is not None and tls.context.verify_mode == ssl.CERT_REQUIRED:
            certificate = tls.getpeercert()
            expected = ("URI", f"urn:minecrafttools:node:{node_id}")
            identities = [
                san
                for san in (certificate or {}).get("subjectAltName", ())
                if san[0] == "URI"
            ]
            if identities != [expected]:
                raise ProtocolError("client certificate identity does not match node")


@dataclass
class WebSocketNodeConnection:
    socket: WebSocket

    async def receive(self) -> bytes | None:
        event = await self.socket.receive()
        if event["type"] == "websocket.disconnect":
            return None
        text = event.get("text")
        if text is None:
            raise ProtocolError("text message required")
        return text.encode()

    async def send(self, body: bytes):
        await self.socket.send_text(body.decode())

    async def close(self):
        with suppress(RuntimeError, WebSocketDisconnect):
            await self.socket.close()


class NodeHub:
    def __init__(self, database: Engine):
        self.database = database
        self.sessions: dict[str, asyncio.Task] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.tasks: set[asyncio.Task] = set()
        self.server: asyncio.Server | None = None

    async def start(self, host: str, port: int, context: ssl.SSLContext):
        await asyncio.to_thread(self.reset_online_state)
        self.server = await asyncio.start_server(
            self.accept,
            host,
            port,
            ssl=context,
            limit=MAX_MESSAGE_SIZE,
            ssl_handshake_timeout=HELLO_TIMEOUT,
            ssl_shutdown_timeout=2,
        )

    async def disconnect(self, node_id: str) -> None:
        task = self.sessions.get(node_id)
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self.sessions.pop(node_id, None)

    async def close(self):
        if self.server is not None:
            self.server.close()
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.server is not None:
            await self.server.wait_closed()

    def reset_online_state(self):
        with self.database.begin() as connection:
            connection.execute(
                update(nodes).where(nodes.c.status == "online").values(status="offline")
            )
            connection.execute(
                update(tunnels)
                .where(
                    tunnels.c.status.in_(["active", "pending"]),
                )
                .values(status="offline")
            )

    @staticmethod
    async def read_message(
        connection: TLSNodeConnection | WebSocketNodeConnection, deadline: float
    ) -> Message | None:
        async with asyncio.timeout_at(deadline):
            line = await connection.receive()
        if line is None:
            return None
        if len(line) > MAX_MESSAGE_SIZE:
            raise ProtocolError("message too large")
        return Message.model_validate_json(line)

    @staticmethod
    async def acknowledge(
        connection: TLSNodeConnection | WebSocketNodeConnection,
        message: Message,
        payload: dict | None = None,
    ):
        reply = {
            "type": f"{message.type}_ack",
            "seq": message.seq,
            "payload": payload or {},
        }
        async with asyncio.timeout(5):
            await connection.send(json.dumps(reply, separators=(",", ":")).encode() + b"\n")

    def authenticate(self, hello: Hello):
        query = select(nodes).where(nodes.c.id == hello.node_id)
        with self.database.connect() as connection:
            node = connection.execute(query).mappings().one_or_none()
        if node is None or not compare_digest(
            node["token_hash"], sha256(hello.secret.encode()).hexdigest()
        ):
            raise ProtocolError("authentication failed")
        return node

    def set_online(self, hello: Hello):
        with self.database.begin() as connection:
            lock_settings(connection)
            now = time()
            if hello.deployed_ports is None:
                unsupported = active_blacklist(now)
            elif not hello.blacklist_cidr:
                unsupported = active_cidr(now)
            else:
                unsupported = None
            if unsupported is not None and connection.execute(
                select(select(blacklist.c.ip).where(unsupported).exists())
            ).scalar_one():
                raise ProtocolError("node does not support the active IP blacklist")
            connection.execute(
                update(nodes)
                .where(nodes.c.id == hello.node_id)
                .values(
                    status="online",
                    last_seen=time(),
                    agent_version=hello.agent_version,
                    frps_version=hello.frps_version,
                    deployed_ports=hello.deployed_ports,
                    blacklist_revision=None,
                    blacklist_cidr=hello.blacklist_cidr and hello.deployed_ports is not None,
                )
            )

    def heartbeat(self, node_id: str):
        with self.database.begin() as connection:
            connection.execute(
                update(nodes).where(nodes.c.id == node_id).values(last_seen=time())
            )

    def set_offline(self, node_id: str):
        with self.database.begin() as connection:
            connection.execute(
                update(nodes).where(nodes.c.id == node_id).values(status="offline")
            )
            connection.execute(
                update(tunnels)
                .where(
                    tunnels.c.node_id == node_id,
                    tunnels.c.status.in_(["active", "pending"]),
                )
                .values(status="offline")
            )

    def record_current_snapshot(self, node_id, items):
        with self.database.connect() as connection:
            node = connection.execute(select(nodes).where(nodes.c.id == node_id)).mappings().one()
        self.record_snapshot(node, items)

    def record_snapshot(self, node, items: list[Proxy]):
        names = {item.proxy_name for item in items}
        ports = {item.remote_port for item in items}
        if len(names) != len(items) or len(ports) != len(items):
            raise ProtocolError("duplicate proxy name or port")
        deployed = node.get("deployed_ports")
        if deployed is None:
            with self.database.connect() as connection:
                allowed = load_ranges(connection, node["id"])
        else:
            allowed = deployed
        if any(not any(start <= port <= end for start, end in allowed) for port in ports):
            raise ProtocolError("proxy outside configured port range")
        node_id = node["id"]
        host = node["public_host"]
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        can_close = tunnels.c.status.in_(("pending", "active", "offline"))
        blocked = tunnels.c.status.in_(("block_pending", "blocked"))
        missing = (
            update(tunnels)
            .where(
                tunnels.c.node_id == node_id,
                can_close | tunnels.c.proxy_present.is_(True),
                tunnels.c.proxy_name.not_in(names),
            )
            .values(
                proxy_present=False,
                status=case((can_close, "closed"), else_=tunnels.c.status),
            )
        )
        now = time()
        hidden = node.get("client_channel") == "hidden"
        closing = []
        with self.database.begin() as connection:
            connection.execute(missing)
            for item in items:
                install_id = None
                if (
                    item.claim_hash
                    and item.claim_id
                    and item.proxy_name == f"minecraft-{item.claim_id}"
                ):
                    install_id = connection.execute(
                        update(tunnel_claims)
                        .where(
                            tunnel_claims.c.id == item.claim_id,
                            tunnel_claims.c.node_id == node_id,
                            tunnel_claims.c.remote_port == item.remote_port,
                            tunnel_claims.c.proof_hash == item.claim_hash,
                            tunnel_claims.c.expires_at > now,
                        )
                        .values(bound_at=func.coalesce(tunnel_claims.c.bound_at, now))
                        .returning(tunnel_claims.c.install_id)
                    ).scalar_one_or_none()
                remote_addr = f"{host}:{item.remote_port}"
                client_ip = canonical_ip(item.client_ip)
                statement = insert(tunnels).values(
                    id=str(
                        uuid5(
                            NAMESPACE_URL, f"minecrafttools:{node_id}/{item.proxy_name}"
                        )
                    ),
                    node_id=node_id,
                    run_id=item.run_id,
                    proxy_name=item.proxy_name,
                    remote_port=item.remote_port,
                    install_id=install_id,
                    remote_addr=remote_addr,
                    client_ip=client_ip,
                    status="pending",
                    created_at=now,
                    next_scan_at=now,
                )
                same_generation = (
                    (tunnels.c.run_id == item.run_id)
                    & (tunnels.c.remote_port == item.remote_port)
                    & (tunnels.c.status != "closed")
                    & tunnels.c.install_id.is_not_distinct_from(install_id)
                )
                same_unclaimed = (
                    tunnels.c.install_id.is_(None) if install_id is None else false()
                )
                statement = statement.on_conflict_do_update(
                    index_elements=[tunnels.c.node_id, tunnels.c.proxy_name],
                    set_={
                        "run_id": item.run_id,
                        "remote_port": item.remote_port,
                        "remote_addr": remote_addr,
                        "client_ip": case(
                            (same_generation, func.coalesce(client_ip, tunnels.c.client_ip)),
                            else_=client_ip,
                        ),
                        "install_id": install_id,
                        "proxy_present": True,
                        "status": case(
                            (
                                same_generation
                                & tunnels.c.status.in_(["pending", "active"]),
                                tunnels.c.status,
                            ),
                            else_="pending",
                        ),
                        "scan_failures": case(
                            (same_generation, tunnels.c.scan_failures), else_=0
                        ),
                        "created_at": case(
                            (same_generation | same_unclaimed, tunnels.c.created_at),
                            else_=now,
                        ),
                        "next_scan_at": case(
                            (same_generation, tunnels.c.next_scan_at), else_=now
                        ),
                        "last_scan_at": case(
                            (same_generation, tunnels.c.last_scan_at), else_=None
                        ),
                        "last_reachable_at": case(
                            (same_generation, tunnels.c.last_reachable_at), else_=None
                        ),
                    },
                    where=tunnels.c.status.not_in(["block_pending", "blocked"]),
                ).returning(tunnels.c.id, tunnels.c.status, tunnels.c.created_at)
                row = connection.execute(statement).mappings().one_or_none()
                if row is not None:
                    claimed = install_id is not None
                    if hidden or not claimed:
                        closing.append({
                            "id": row["id"],
                            "created_at": row["created_at"],
                            "run_id": item.run_id,
                            "port": item.remote_port,
                            "client_ip": client_ip,
                            "claimed": claimed,
                        })
                connection.execute(
                    update(tunnels)
                    .where(
                        blocked,
                        tunnels.c.proxy_present.is_(False),
                        tunnels.c.node_id == node_id,
                        tunnels.c.proxy_name == item.proxy_name,
                        tunnels.c.run_id == item.run_id,
                        tunnels.c.remote_port == item.remote_port,
                    )
                    .values(proxy_present=True)
                )
                if client_ip:
                    connection.execute(update(blocks).where(
                        blocks.c.tunnel_id == select(tunnels.c.id).where(
                            tunnels.c.node_id == node_id,
                            tunnels.c.proxy_name == item.proxy_name,
                        ).scalar_subquery(),
                        blocks.c.run_id == item.run_id,
                        blocks.c.proxy_name == item.proxy_name,
                        blocks.c.client_ip.is_(None),
                    ).values(client_ip=client_ip))

        for target in closing:
            if target["claimed"]:
                reason, source = "节点已下架，关闭隧道", "node-hidden"
            else:
                if not hidden and time() - target["created_at"] < UNCLAIMED_GRACE:
                    continue
                reason, source = UNCLAIMED_BLOCK_REASON, "unauthorized"
            try:
                command = enqueue_command(
                    self.database,
                    target["id"],
                    "block",
                    reason,
                    source,
                    expected_run_id=target["run_id"],
                    expected_port=target["port"],
                )
            except CommandConflict:
                continue
            if command is not None and not target["claimed"]:
                ban_unclaimed_flood(self.database, target["client_ip"], time())

    async def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        await self.serve(TLSNodeConnection(reader, writer))

    async def serve(self, connection: TLSNodeConnection | WebSocketNodeConnection):
        task = asyncio.current_task()
        self.tasks.add(task)
        node_id = None
        clock = asyncio.get_running_loop().time
        try:
            first = await self.read_message(connection, clock() + HELLO_TIMEOUT)
            if first is None or first.type != "hello":
                raise ProtocolError("hello required")
            hello = Hello.model_validate(first.payload)
            if isinstance(connection, TLSNodeConnection):
                connection.verify_identity(hello.node_id)
            if hello.deployed_ports is not None and any(
                not 1024 <= start <= end <= 65535 for start, end in hello.deployed_ports
            ):
                raise ProtocolError("invalid deployed port ranges")
            await asyncio.to_thread(self.authenticate, hello)
            node_id = hello.node_id
            lock = self.locks.setdefault(node_id, asyncio.Lock())
            async with lock:
                await asyncio.to_thread(self.set_online, hello)
                previous = self.sessions.get(node_id)
                self.sessions[node_id] = task
                if previous is not None:
                    previous.cancel()
            policy = (await asyncio.to_thread(node_policy, self.database, node_id)
                      if hello.deployed_ports is not None else None)
            await self.acknowledge(connection, first, {"policy": policy} if policy else None)
            logger.info("Node authenticated node_id=%s", node_id)

            last_seq = first.seq
            last_heartbeat = clock()
            last_write = last_heartbeat
            while self.sessions.get(node_id) is task:
                message = await self.read_message(
                    connection, last_heartbeat + HEARTBEAT_TIMEOUT
                )
                if message is None:
                    break
                if message.seq <= last_seq:
                    raise ProtocolError("out-of-order sequence")
                last_seq = message.seq
                payload = None
                async with lock:
                    if self.sessions.get(node_id) is not task:
                        break
                    if message.type == "tunnel_snapshot":
                        snapshot = Snapshot.model_validate(message.payload)
                        await asyncio.to_thread(
                            self.record_current_snapshot, node_id, snapshot.tunnels
                        )
                    elif message.type == "heartbeat":
                        last_heartbeat = clock()
                        if last_heartbeat - last_write >= HEARTBEAT_WRITE_INTERVAL:
                            await asyncio.to_thread(self.heartbeat, node_id)
                            last_write = last_heartbeat
                        command = await asyncio.to_thread(
                            next_command, self.database, node_id
                        )
                        payload = {"commands": [command] if command is not None else []}
                        if hello.deployed_ports is not None:
                            payload["policy"] = await asyncio.to_thread(node_policy, self.database, node_id)
                    elif message.type == "policy_ack" and hello.deployed_ports is not None:
                        result = PolicyAcknowledgement.model_validate(message.payload)
                        await asyncio.to_thread(
                            acknowledge_policy, self.database, node_id, result.kind,
                            result.revision, result.error,
                        )
                    elif message.type == "ack":
                        result = CommandAcknowledgement.model_validate(message.payload)
                        accepted = await asyncio.to_thread(
                            acknowledge_command,
                            self.database,
                            node_id,
                            result.command_id,
                            result.ok,
                            result.error,
                        )
                        if not accepted:
                            raise ProtocolError("unknown command acknowledgement")
                    else:
                        raise ProtocolError("unsupported message")
                await self.acknowledge(connection, message, payload)
        except (ProtocolError, ValidationError, TimeoutError, OSError, WebSocketDisconnect) as error:
            logger.info("Node connection ended (%s)", type(error).__name__)
        except asyncio.CancelledError:
            pass
        except SQLAlchemyError:
            logger.exception("Node registry operation failed")
        finally:
            try:
                if node_id is not None:
                    async with self.locks[node_id]:
                        if self.sessions.get(node_id) is task:
                            del self.sessions[node_id]
                            await asyncio.to_thread(self.set_offline, node_id)
            except SQLAlchemyError:
                logger.exception("Failed to mark disconnected node offline")
            finally:
                try:
                    with suppress(OSError, TimeoutError):
                        await connection.close()
                finally:
                    self.tasks.discard(task)
