import logging
import struct
from asyncio import sleep, timeout
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from sqlite3 import SQLITE_CONSTRAINT_UNIQUE
from time import time
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from mcstatus import JavaServer
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import (
    Boolean,
    Column,
    Float,
    Index,
    Integer,
    String,
    Table,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from .database import metadata
from .registry import blocks, nodes, tunnels

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/lobby")

ROOM_LIFETIME = 10
ONLINE_LIFETIME = 15
CLEANUP_INTERVAL = 60
MAX_PROBE_SECONDS = 3

rooms = Table(
    "rooms",
    metadata,
    Column("full_room_code", String, primary_key=True),
    Column("remote_port", Integer, nullable=False),
    Column("node_id", Integer, nullable=False),
    Column("room_name", String, nullable=False),
    Column("game_version", String, nullable=True),
    Column("player_count", Integer, nullable=False),
    Column("max_players", Integer, nullable=False),
    Column("description", String, nullable=True),
    Column("is_public", Boolean, nullable=False),
    Column("host_player", String, nullable=False),
    Column("server_addr", String, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("client_ip", String, nullable=False, unique=True),
    Column("tunnel_id", String, nullable=True),
)
Index("ix_rooms_updated_at", rooms.c.updated_at)

online_users = Table(
    "online_users",
    metadata,
    Column("client_ip", String, primary_key=True),
    Column("last_heartbeat", Float, nullable=False),
)
Index("ix_online_users_last_heartbeat", online_users.c.last_heartbeat)

Port = Annotated[int, Field(ge=1, le=65535)]
Identifier = Annotated[int, Field(ge=0, le=2**63 - 1)]


class RoomKey(BaseModel):
    remote_port: Port
    node_id: Identifier

    @property
    def code(self) -> str:
        return f"{self.remote_port}_{self.node_id}"


class RoomCreate(RoomKey):
    full_room_code: str
    room_name: str = Field(max_length=50)
    game_version: str | None = "未知版本"
    player_count: Identifier = 0
    max_players: Identifier = 20
    description: str | None = Field(default="欢迎来玩！", max_length=200)
    is_public: bool = True
    host_player: str
    server_addr: str = Field(max_length=253)

    @model_validator(mode="after")
    def validate_room_code(self):
        if self.full_room_code != self.code:
            raise ValueError("full_room_code must match remote_port and node_id")
        return self


@dataclass(frozen=True)
class LobbySettings:
    targets: frozenset[tuple[str, int]]
    blocked_words: tuple[str, ...]

    @classmethod
    def load(cls, targets: str, rules_file: str):
        allowed = set()
        for entry in filter(None, map(str.strip, targets.split(","))):
            address = urlsplit(f"//{entry}")
            if (
                not address.hostname
                or not address.port
                or address.username is not None
                or address.path
                or address.query
                or address.fragment
            ):
                raise ValueError(f"Invalid CONTROL_LOBBY_TARGETS entry: {entry!r}")
            allowed.add((canonical_host(address.hostname), address.port))
        rules = (
            Path(rules_file).read_text(encoding="utf-8").splitlines()
            if rules_file
            else ()
        )
        words = tuple(rule.strip().casefold() for rule in rules if rule.strip())
        return cls(frozenset(allowed), words)


def canonical_host(host: str) -> str:
    normalized = host.strip().strip("[]").rstrip(".")
    try:
        return ip_address(normalized).compressed
    except ValueError:
        return normalized.casefold()


def mask_ip(address: str) -> str:
    if "." in address and ":" not in address:
        return f"{address.rsplit('.', 1)[0]}.***"
    return f"{address[: len(address) // 2]}***" if address else "***"


def client_ip(request: Request) -> str:
    # Uvicorn handles proxy addresses only for configured trusted peers.
    return request.client.host if request.client else ""


async def game_status(host: str, port: int):
    try:
        async with timeout(MAX_PROBE_SECONDS):
            return await JavaServer(host, port, timeout=MAX_PROBE_SECONDS).async_status(
                tries=1
            )
    except (
        OSError,
        ValueError,
        EOFError,
        IndexError,
        KeyError,
        TypeError,
        struct.error,
    ):
        return None


def save_room(
    database: Engine, room: RoomCreate, owner: str, version: str, motd: str
) -> Literal["saved", "blocked", "owner_conflict"]:
    try:
        with database.begin() as connection:
            # Serialize a renewal with a block's unlisting transaction.
            connection.execute(
                update(rooms)
                .where(rooms.c.full_room_code == room.code)
                .values(updated_at=rooms.c.updated_at)
            )
            reserved_hosts = connection.execute(
                select(nodes.c.public_host)
                .select_from(blocks.join(nodes))
                .where(
                    blocks.c.active.is_(True),
                    blocks.c.port == room.remote_port,
                )
            ).scalars()
            if any(canonical_host(host) == room.server_addr for host in reserved_hosts):
                return "blocked"
            candidates = connection.execute(
                select(tunnels.c.id, nodes.c.public_host)
                .join(nodes)
                .where(
                    tunnels.c.remote_port == room.remote_port,
                    tunnels.c.status.in_(("pending", "active")),
                )
            )
            matching = [
                tunnel_id
                for tunnel_id, host in candidates
                if canonical_host(host) == room.server_addr
            ]
            tunnel_id = matching[0] if len(matching) == 1 else None
            statement = room_upsert(room, owner, version, motd, tunnel_id)
            return (
                "saved"
                if connection.execute(statement).rowcount == 1
                else "owner_conflict"
            )
    except IntegrityError as error:
        if getattr(error.orig, "sqlite_errorcode", None) != SQLITE_CONSTRAINT_UNIQUE:
            raise
        return "owner_conflict"


def room_upsert(
    room: RoomCreate, owner: str, version: str, motd: str, tunnel_id: str | None
):
    values = room.model_dump()
    values.update(client_ip=owner, updated_at=time(), tunnel_id=tunnel_id)
    if version:
        values["game_version"] = version
    if motd:
        values["description"] = motd

    statement = insert(rooms).values(**values)
    changes = {
        name: statement.excluded[name]
        for name in values
        if name not in {"full_room_code", "client_ip"}
    }
    if not version:
        changes["game_version"] = func.coalesce(
            rooms.c.game_version, statement.excluded.game_version
        )
    if not motd:
        changes["description"] = func.coalesce(
            rooms.c.description, statement.excluded.description
        )
    return statement.on_conflict_do_update(
        index_elements=[rooms.c.full_room_code],
        set_=changes,
        where=rooms.c.client_ip == owner,
    )


@router.post("/rooms")
async def publish_room(room: RoomCreate, request: Request):
    settings = request.app.state.lobby_settings
    host = canonical_host(room.server_addr)
    port = room.remote_port
    owner = client_ip(request)

    def deny(message: str, reason: str) -> dict:
        # Clients renew every 5 seconds, so a broken tunnel logs at that rate.
        logger.warning(
            "Lobby publish rejected: %s (%s:%d from %s)", reason, host, port, owner
        )
        return {"success": False, "message": message}

    if (host, port) not in settings.targets:
        return deny("该端口未开放大厅发布", "target not allowlisted")
    for text, label, field in (
        (room.room_name, "房间名", "room name"),
        (room.description or "", "简介", "description"),
    ):
        if any(word in text.casefold() for word in settings.blocked_words):
            return deny(f"{label}包含敏感词", f"blocked word in {field}")

    status = await game_status(host, port)
    if status is None:
        return deny("未探测到 Minecraft 服务器", "no Minecraft status")
    motd = status.motd.to_plain()[:200]
    if room.is_public and any(
        word in motd.casefold() for word in settings.blocked_words
    ):
        return deny("服务器简介包含敏感词", "blocked word in MOTD")
    room = room.model_copy(update={"server_addr": host})
    result = await run_in_threadpool(
        save_room,
        request.app.state.database,
        room,
        owner,
        status.version.name,
        motd,
    )
    if result == "blocked":
        return deny("隧道已封禁", "tunnel blocked")
    if result == "owner_conflict":
        return deny("禁止多开，此IP已被占用", "room already owned by this IP")
    return {"success": True, "message": "Room updated"}


@router.delete("/rooms")
def remove_room(room: RoomKey, request: Request):
    with request.app.state.database.begin() as connection:
        connection.execute(
            delete(rooms).where(
                rooms.c.full_room_code == room.code,
                rooms.c.client_ip == client_ip(request),
            )
        )
    return {"success": True, "message": "Room removed"}


@router.get("/rooms")
def list_rooms(request: Request):
    query = (
        select(rooms)
        .where(rooms.c.is_public.is_(True))
        .order_by(
            rooms.c.updated_at.desc(),
            rooms.c.full_room_code,
        )
        .limit(100)
    )
    with request.app.state.database.connect() as connection:
        listed = [dict(row) for row in connection.execute(query).mappings()]
    for row in listed:
        row["host_ip"] = mask_ip(row.pop("client_ip"))
    return {"success": True, "rooms": listed}


@router.post("/heartbeat")
def heartbeat(request: Request):
    now = time()
    statement = insert(online_users).values(
        client_ip=client_ip(request), last_heartbeat=now
    )
    statement = statement.on_conflict_do_update(
        index_elements=[online_users.c.client_ip],
        set_={"last_heartbeat": now},
    )
    with request.app.state.database.begin() as connection:
        connection.execute(statement)
    return {"success": True}


@router.get("/online")
def online(request: Request):
    query = (
        select(func.count())
        .select_from(online_users)
        .where(
            online_users.c.last_heartbeat >= time() - ONLINE_LIFETIME,
        )
    )
    with request.app.state.database.connect() as connection:
        count = connection.execute(query).scalar_one()
    return {"success": True, "online_count": count}


def cleanup(database: Engine, now: float) -> None:
    with database.begin() as connection:
        connection.execute(
            delete(rooms).where(rooms.c.updated_at < now - ROOM_LIFETIME)
        )
        connection.execute(
            delete(online_users).where(
                online_users.c.last_heartbeat < now - ONLINE_LIFETIME,
            )
        )


async def cleanup_lobby(database: Engine) -> None:
    while True:
        try:
            await run_in_threadpool(cleanup, database, time())
        except SQLAlchemyError:
            logger.exception("Failed to expire lobby data")
        await sleep(CLEANUP_INTERVAL)
