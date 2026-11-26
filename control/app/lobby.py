import logging
import struct
from asyncio import sleep, timeout
from dataclasses import dataclass
from hashlib import sha256
from ipaddress import ip_address
from pathlib import Path
from sqlite3 import SQLITE_CONSTRAINT_UNIQUE
from time import time
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import uuid4

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
    case,
    delete,
    func,
    select,
)
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from .database import metadata
from .ip_rules import canonical_ip
from .java_probe import probe
from .moderation import ip_block, live_tunnels, lock_settings, removals, settings
from .registry import blocks, installs, nodes, tunnel_claims, tunnels

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
    Column("listing_id", String),
    Column("publication_id", String),
)
Index("ix_rooms_updated_at", rooms.c.updated_at)
Index("ix_rooms_listing_id", rooms.c.listing_id, unique=True)

online_users = Table(
    "online_users",
    metadata,
    Column("client_ip", String, primary_key=True),
    Column("last_heartbeat", Float, nullable=False),
    Column("first_heartbeat", Float, nullable=False, server_default="0"),
    Column("lobby_heartbeat", Float, nullable=False, server_default="0"),
    Column("channel", String, nullable=True),
    Column("os_version", String, nullable=True),
)
Index("ix_online_users_last_heartbeat", online_users.c.last_heartbeat)

Port = Annotated[int, Field(ge=1, le=65535)]
Identifier = Annotated[int, Field(ge=0, le=2**63 - 1)]


class PresenceHeartbeat(BaseModel):
    channel: Literal["dev", "stable"] | None = None
    os_version: str | None = Field(default=None, max_length=128)
    # 旧客户端不带 body；缺省仍按大厅心跳处理。
    lobby: bool = True


class RoomKey(BaseModel):
    remote_port: Port
    node_id: Identifier
    publication_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")

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
    claim_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")

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
    return (canonical_ip(request.client.host) or request.client.host) if request.client else ""


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


def publication_state(connection, room, owner, token):
    now = time()
    current = connection.execute(
        select(rooms.c.publication_id).where(
            rooms.c.full_room_code == room.code, rooms.c.client_ip == owner,
            rooms.c.updated_at >= now - ROOM_LIFETIME,
        )
    ).one_or_none()
    removed = connection.execute(select(removals.c.code).where(
        removals.c.code == room.code, removals.c.owner == owner,
        removals.c.publication_id == (room.publication_id or ""),
    )).first()
    if removed:
        return "room_unlisted", None
    if ip_block(connection, owner):
        return "ip_blacklisted", None

    source = tunnels.join(nodes)
    filters = [tunnels.c.remote_port == room.remote_port, live_tunnels(now)]
    if room.claim_id:
        if not token:
            return "tunnel_unavailable", None
        source = source.join(tunnel_claims, (
            (tunnel_claims.c.node_id == tunnels.c.node_id)
            & (tunnel_claims.c.install_id == tunnels.c.install_id)
            & (tunnel_claims.c.remote_port == tunnels.c.remote_port)
        )).join(installs, installs.c.id == tunnel_claims.c.install_id)
        filters.extend((
            tunnel_claims.c.id == room.claim_id, tunnel_claims.c.expires_at > now,
            tunnel_claims.c.bound_at.is_not(None),
            installs.c.token_hash == sha256(token.encode()).hexdigest(),
            tunnels.c.proxy_name == f"minecraft-{room.claim_id}",
        ))
    candidates = select(tunnels.c.id, nodes.c.public_host).select_from(source).where(*filters)
    matches = [
        row for row in connection.execute(candidates)
        if canonical_host(row.public_host) == room.server_addr
    ]
    tunnel_id = matches[0].id if len(matches) == 1 else None
    if room.claim_id and not tunnel_id:
        return "tunnel_unavailable", None

    # A blacklisted tunnel is excluded from the live query even before Agent ACK.
    known_ips = connection.execute(
        select(tunnels.c.client_ip, nodes.c.public_host).join(nodes).where(
            tunnels.c.remote_port == room.remote_port, tunnels.c.proxy_present.is_(True),
            tunnels.c.status != "closed",
        )
    )
    if ip_block(connection, *(row.client_ip for row in known_ips
                             if canonical_host(row.public_host) == room.server_addr)):
        return "ip_blacklisted", None

    reserved_hosts = connection.execute(
        select(nodes.c.public_host).select_from(blocks.join(nodes)).where(
            blocks.c.active.is_(True), blocks.c.port == room.remote_port,
        )
    ).scalars()
    if any(canonical_host(host) == room.server_addr for host in reserved_hosts):
        return "tunnel_blocked", None

    enabled = connection.execute(select(settings.c.lobby_enabled)).scalar_one()
    if not enabled:
        if current is None or current.publication_id != room.publication_id:
            return "lobby_closed", None
        return "lobby_paused", tunnel_id
    return "ready", tunnel_id


def save_room(database, room, owner, token, version, motd):
    try:
        with database.begin() as connection:
            lock_settings(connection)
            state, tunnel_id = publication_state(connection, room, owner, token)
            if state not in ("ready", "lobby_paused"):
                return state
            connection.execute(delete(rooms).where(
                rooms.c.updated_at < time() - ROOM_LIFETIME,
                (rooms.c.full_room_code == room.code) | (rooms.c.client_ip == owner),
            ))
            statement = room_upsert(room, owner, version, motd, tunnel_id)
            if connection.execute(statement).rowcount != 1:
                return "owner_conflict"
            return "saved" if state == "ready" else state
    except IntegrityError as error:
        if getattr(error.orig, "sqlite_errorcode", None) != SQLITE_CONSTRAINT_UNIQUE:
            raise
        return "owner_conflict"


def room_upsert(
    room: RoomCreate, owner: str, version: str, motd: str, tunnel_id: str | None
):
    values = room.model_dump(exclude={"claim_id"})
    values.update(
        client_ip=owner, updated_at=time(), tunnel_id=tunnel_id, listing_id=uuid4().hex
    )
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
    changes["listing_id"] = case(
        (rooms.c.publication_id.is_not_distinct_from(room.publication_id)
         & (rooms.c.updated_at >= time() - ROOM_LIFETIME), rooms.c.listing_id),
        else_=statement.excluded.listing_id,
    )
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

    if not room.claim_id and (host, port) not in settings.targets:
        return deny("该端口未开放大厅发布", "target not allowlisted")
    room = room.model_copy(update={"server_addr": host})
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token if scheme.lower() == "bearer" and len(token) == 43 else ""
    database = request.app.state.database

    def preflight():
        with database.connect() as connection:
            return publication_state(connection, room, owner, token)[0]

    messages = {
        "room_unlisted": "房间已被管理员下架，请手动重新发布",
        "lobby_closed": "公开联机大厅暂时关闭",
        "tunnel_blocked": "隧道已封禁",
        "ip_blacklisted": "客户端 IP 已被拉黑",
        "tunnel_unavailable": "隧道未上线或发布凭据已失效",
        "owner_conflict": "禁止多开，此IP已被占用",
    }

    def rejection(code):
        detail = {} if code == "tunnel_blocked" and room.publication_id is None else {"code": code}
        return {"success": False, "message": messages[code], **detail}

    state = await run_in_threadpool(preflight)
    if state not in ("ready", "lobby_paused"):
        return rejection(state)
    for text, label, field in (
        (room.room_name, "房间名", "room name"),
        (room.description or "", "简介", "description"),
    ):
        if any(word in text.casefold() for word in settings.blocked_words):
            return deny(f"{label}包含敏感词", f"blocked word in {field}")

    if room.claim_id and (host, port) not in settings.targets:
        try:
            async with timeout(MAX_PROBE_SECONDS):
                result = await probe(host, port)
        except TimeoutError:
            return deny("未探测到 Minecraft 服务器", "Minecraft status timeout")
        if result.result != "ok":
            return deny("未探测到 Minecraft 服务器", "no Minecraft status")
        version, motd = result.version or "", (result.motd or "")[:200]
    else:
        status = await game_status(host, port)
        if status is None:
            return deny("未探测到 Minecraft 服务器", "no Minecraft status")
        version, motd = status.version.name, status.motd.to_plain()[:200]
    if room.is_public and any(
        word in motd.casefold() for word in settings.blocked_words
    ):
        return deny("服务器简介包含敏感词", "blocked word in MOTD")
    result = await run_in_threadpool(save_room, database, room, owner, token, version, motd)
    if result == "lobby_paused":
        return {"success": True, "enabled": False, "message": "公开联机大厅暂时关闭"}
    if result != "saved":
        return rejection(result)
    return {"success": True, "message": "Room updated"}


@router.delete("/rooms")
def remove_room(room: RoomKey, request: Request):
    owner = client_ip(request)
    with request.app.state.database.begin() as connection:
        connection.execute(delete(rooms).where(
            rooms.c.full_room_code == room.code, rooms.c.client_ip == owner,
            rooms.c.publication_id.is_not_distinct_from(room.publication_id),
        ))
        if room.publication_id is None:
            connection.execute(delete(removals).where(
                removals.c.code == room.code, removals.c.owner == owner,
                removals.c.publication_id == "",
            ))
    return {"success": True, "message": "Room removed"}


def live_rooms(connection, limit=None):
    now = time()
    active = select(tunnels.c.id).join(nodes).where(live_tunnels(now))
    query = (
        select(rooms).where(
            rooms.c.is_public.is_(True), rooms.c.updated_at >= now - ROOM_LIFETIME,
            rooms.c.tunnel_id.is_(None) | rooms.c.tunnel_id.in_(active),
        ).order_by(rooms.c.updated_at.desc(), rooms.c.full_room_code)
    )
    return list(map(dict, connection.execute(query.limit(limit)).mappings()))


@router.get("/rooms")
def list_rooms(request: Request):
    with request.app.state.database.connect() as connection:
        enabled = connection.execute(select(settings.c.lobby_enabled)).scalar_one()
        listed = live_rooms(connection, limit=100) if enabled else []
    for row in listed:
        row["host_ip"] = mask_ip(row.pop("client_ip"))
        row.pop("listing_id")
        row.pop("publication_id")
    return {"success": True, "rooms": listed, **({} if enabled else {"enabled": False})}


@router.post("/heartbeat")
def heartbeat(request: Request, payload: PresenceHeartbeat | None = None):
    now = time()
    profile = payload or PresenceHeartbeat()
    os_version = profile.os_version.strip() if profile.os_version else None
    statement = insert(online_users).values(
        client_ip=client_ip(request),
        last_heartbeat=now,
        first_heartbeat=now,
        lobby_heartbeat=now if profile.lobby else 0.0,
        channel=profile.channel,
        os_version=os_version or None,
    )
    changes = {
        "last_heartbeat": now,
        "first_heartbeat": case(
            (online_users.c.last_heartbeat < now - ONLINE_LIFETIME, now),
            else_=online_users.c.first_heartbeat,
        ),
        "channel": func.coalesce(statement.excluded.channel, online_users.c.channel),
        "os_version": func.coalesce(
            statement.excluded.os_version, online_users.c.os_version
        ),
    }
    if profile.lobby:
        changes["lobby_heartbeat"] = now
    statement = statement.on_conflict_do_update(
        index_elements=[online_users.c.client_ip], set_=changes
    )
    with request.app.state.database.begin() as connection:
        connection.execute(statement)
    return {"success": True}


def live_online_users(connection, now=None):
    now = time() if now is None else now
    first_heartbeat = case(
        (online_users.c.first_heartbeat > 0, online_users.c.first_heartbeat),
        else_=online_users.c.last_heartbeat,
    ).label("first_heartbeat")
    query = (
        select(
            online_users.c.client_ip,
            first_heartbeat,
            online_users.c.last_heartbeat,
            online_users.c.channel,
            online_users.c.os_version,
        )
        .where(online_users.c.last_heartbeat >= now - ONLINE_LIFETIME)
        .order_by(first_heartbeat, online_users.c.client_ip)
    )
    return list(map(dict, connection.execute(query).mappings()))


@router.get("/online")
def online(request: Request):
    query = (
        select(func.count())
        .select_from(online_users)
        .where(
            online_users.c.lobby_heartbeat >= time() - ONLINE_LIFETIME,
        )
    )
    with request.app.state.database.connect() as connection:
        enabled = connection.execute(select(settings.c.lobby_enabled)).scalar_one()
        count = connection.execute(query).scalar_one() if enabled else 0
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
