import asyncio
import json
import logging
import ssl
from contextlib import suppress
from hashlib import sha256
from hmac import compare_digest
from time import time
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from .registry import nodes, tunnel_claims, tunnels
from .tunnel_commands import acknowledge_command, next_command

logger = logging.getLogger(__name__)
MAX_MESSAGE_SIZE = 1024 * 1024
HELLO_TIMEOUT = 10
HEARTBEAT_TIMEOUT = 30
HEARTBEAT_WRITE_INTERVAL = 10


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


class Proxy(BaseModel):
    run_id: str = Field(min_length=1, max_length=200)
    proxy_name: str = Field(min_length=1, max_length=200)
    remote_port: int = Field(ge=1, le=65535)
    claim_id: str | None = Field(default=None, max_length=32)
    claim_hash: str | None = Field(default=None, max_length=64)


class Snapshot(BaseModel):
    tunnels: list[Proxy] = Field(max_length=4096)


class CommandAcknowledgement(BaseModel):
    command_id: int = Field(ge=1, le=2**63 - 1, strict=True)
    ok: bool = Field(strict=True)
    error: str = Field(default="", max_length=300)


def server_tls(certfile: str, keyfile: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile, keyfile)
    return context


class NodeHub:
    def __init__(self, database: Engine):
        self.database = database
        self.sessions: dict[str, asyncio.StreamWriter] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.writers: set[asyncio.StreamWriter] = set()
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

    async def close(self):
        if self.server is not None:
            self.server.close()
        for writer in self.writers:
            writer.close()
        await asyncio.gather(*self.tasks, return_exceptions=True)
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
        reader: asyncio.StreamReader, deadline: float
    ) -> Message | None:
        try:
            async with asyncio.timeout_at(deadline):
                line = await reader.readuntil(b"\n")
        except asyncio.IncompleteReadError as error:
            if not error.partial:
                return None
            raise ProtocolError("truncated message") from None
        except asyncio.LimitOverrunError:
            raise ProtocolError("message too large") from None
        if len(line) > MAX_MESSAGE_SIZE:
            raise ProtocolError("message too large")
        return Message.model_validate_json(line)

    @staticmethod
    async def acknowledge(
        writer: asyncio.StreamWriter, message: Message, payload: dict | None = None
    ):
        reply = {
            "type": f"{message.type}_ack",
            "seq": message.seq,
            "payload": payload or {},
        }
        writer.write(json.dumps(reply, separators=(",", ":")).encode() + b"\n")
        async with asyncio.timeout(5):
            await writer.drain()

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
            connection.execute(
                update(nodes)
                .where(nodes.c.id == hello.node_id)
                .values(
                    status="online",
                    last_seen=time(),
                    agent_version=hello.agent_version,
                    frps_version=hello.frps_version,
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

    def record_snapshot(self, node, items: list[Proxy]):
        names = {item.proxy_name for item in items}
        ports = {item.remote_port for item in items}
        if len(names) != len(items) or len(ports) != len(items):
            raise ProtocolError("duplicate proxy name or port")
        if any(not node["port_start"] <= port <= node["port_end"] for port in ports):
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
                statement = statement.on_conflict_do_update(
                    index_elements=[tunnels.c.node_id, tunnels.c.proxy_name],
                    set_={
                        "run_id": item.run_id,
                        "remote_port": item.remote_port,
                        "remote_addr": remote_addr,
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
                            (same_generation, tunnels.c.created_at), else_=now
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
                )
                connection.execute(statement)
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

    async def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.writers.add(writer)
        node_id = None
        clock = asyncio.get_running_loop().time
        try:
            first = await self.read_message(reader, clock() + HELLO_TIMEOUT)
            if first is None or first.type != "hello":
                raise ProtocolError("hello required")
            hello = Hello.model_validate(first.payload)
            node = await asyncio.to_thread(self.authenticate, hello)
            node_id = hello.node_id
            lock = self.locks.setdefault(node_id, asyncio.Lock())
            async with lock:
                await asyncio.to_thread(self.set_online, hello)
                previous = self.sessions.get(node_id)
                self.sessions[node_id] = writer
                if previous is not None:
                    previous.close()
            await self.acknowledge(writer, first)

            last_seq = first.seq
            last_heartbeat = clock()
            last_write = last_heartbeat
            while self.sessions.get(node_id) is writer:
                message = await self.read_message(
                    reader, last_heartbeat + HEARTBEAT_TIMEOUT
                )
                if message is None:
                    break
                if message.seq <= last_seq:
                    raise ProtocolError("out-of-order sequence")
                last_seq = message.seq
                payload = None
                async with lock:
                    if self.sessions.get(node_id) is not writer:
                        break
                    if message.type == "tunnel_snapshot":
                        snapshot = Snapshot.model_validate(message.payload)
                        await asyncio.to_thread(
                            self.record_snapshot, node, snapshot.tunnels
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
                await self.acknowledge(writer, message, payload)
        except (ProtocolError, ValidationError, TimeoutError, OSError) as error:
            logger.info("Node connection ended (%s)", type(error).__name__)
        except SQLAlchemyError:
            logger.exception("Node registry operation failed")
        finally:
            try:
                if node_id is not None:
                    async with self.locks[node_id]:
                        if self.sessions.get(node_id) is writer:
                            del self.sessions[node_id]
                            await asyncio.to_thread(self.set_offline, node_id)
            except SQLAlchemyError:
                logger.exception("Failed to mark disconnected node offline")
            finally:
                writer.close()
                with suppress(OSError, TimeoutError):
                    await writer.wait_closed()
                self.writers.discard(writer)
                self.tasks.discard(task)
