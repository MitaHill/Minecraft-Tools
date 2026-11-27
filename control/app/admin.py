import asyncio
import json
import logging
from asyncio import to_thread
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from secrets import token_urlsafe
from time import time
from typing import Annotated, Literal
from urllib.parse import urlsplit

from argon2 import PasswordHasher, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from sqlalchemy import Column, Float, String, Table, delete, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketDisconnect

from .database import metadata, record_admin_action
from .ip_rules import canonical_ip, canonical_rule
from .lobby import live_online_users, live_rooms, rooms
from .moderation import (
    active_blacklist,
    blacklist,
    ip_matches,
    live_tunnels,
    lock_settings,
    removals,
    settings,
    touch_node_catalog,
)
from .node_ports import (
    MAX_TEXT,
    contains,
    format_spec,
    has_pending,
    load_ranges,
    parse_spec,
    ranges_by_node,
    replace_ranges,
    within,
)
from .rate_limit import RateLimit
from .registry import blocks, nodes, tunnel_claims, tunnels
from .scanner import SCANNABLE, scans
from .tunnel_commands import CommandConflict, commands, enqueue_command

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin")
hasher = PasswordHasher()
SESSION_COOKIE = "minecrafttools_session"
SESSION_AGE = 12 * 60 * 60
LOGIN_WINDOW = 60
LOGIN_ATTEMPTS = 5
Role = Literal["superadmin", "operator", "viewer"]
TunnelStatus = Literal[
    "pending", "active", "block_pending", "blocked", "closed", "offline"
]

sessions = Table(
    "admin_sessions",
    metadata,
    Column("token_hash", String, primary_key=True),
    Column("username", String, nullable=False),
    Column("expires_at", Float, nullable=False),
)


class Account(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password_hash: str
    role: Role


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: SecretStr = Field(min_length=1, max_length=512)


class TunnelBlock(BaseModel):
    reason: str = Field(default="管理员封禁", min_length=1, max_length=200)
    run_id: str | None = Field(default=None, min_length=1, max_length=200)
    created_at: float | None = None


class BlacklistRule(BaseModel):
    reason: str = Field(min_length=1, max_length=200)
    duration: Literal["1h", "24h", "7d", "30d", "permanent"]


class TunnelBlacklist(BlacklistRule):
    run_id: str = Field(min_length=1, max_length=200)
    created_at: float


class ManualBlacklist(BlacklistRule):
    target: str = Field(min_length=1, max_length=64)
    confirm_all: bool = Field(default=False, strict=True)


class LobbyToggle(BaseModel):
    enabled: bool


class SecuritySettings(BaseModel):
    auto_ban_enabled: bool
    auto_ban_attempts: int = Field(ge=1, le=20, strict=True)
    auto_ban_window_minutes: int = Field(ge=1, le=1440, strict=True)
    auto_ban_hours: int = Field(ge=1, le=720, strict=True)
    auto_ban_skip_legit: bool


class NodeConfiguration(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    public_host: str = Field(min_length=1, max_length=253)
    allowed_ports: str = Field(min_length=1, max_length=MAX_TEXT)
    server_addr: str | None = Field(default=None, max_length=253)
    server_port: int | None = Field(default=None, ge=1, le=65535, strict=True)
    client_channel: Literal["hidden", "dev", "stable", "both"] = "hidden"

    @field_validator("public_host", "server_addr")
    @classmethod
    def validate_host(cls, value: str | None) -> str | None:
        if value is None:
            return None
        host = value.strip().rstrip(".").lower()
        parsed = urlsplit(f"//[{host}]" if ":" in host else f"//{host}")
        if (
            parsed.hostname != host or parsed.netloc != (f"[{host}]" if ":" in host else host)
            or any(char.isspace() or char in "/\\?#@[]" for char in host)
        ):
            raise ValueError("请输入有效的主机名或 IP")
        return host

    @field_validator("allowed_ports")
    @classmethod
    def validate_allowed_ports(cls, value: str) -> str:
        return format_spec(parse_spec(value))

    @model_validator(mode="after")
    def validate_name(self):
        if not self.name.strip():
            raise ValueError("请输入节点名称")
        return self

    def client_config(self) -> dict:
        return self.model_dump(
            include={"server_addr", "server_port", "client_channel"},
            exclude_unset=True,
        )


class NodeRegistration(NodeConfiguration):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")


@dataclass
class Administration:
    accounts: dict[str, Account]
    secure_cookie: bool
    dummy_hash: str
    rate_limit: RateLimit

    @classmethod
    def load(cls, filename: str, secure_cookie: bool):
        items = (
            json.loads(Path(filename).read_text(encoding="utf-8")) if filename else []
        )
        if not isinstance(items, list):
            raise TypeError("CONTROL_ADMIN_USERS_FILE must contain a JSON array")
        accounts = {}
        for item in items:
            account = Account.model_validate(item)
            extract_parameters(account.password_hash)
            if account.username in accounts:
                raise ValueError("Duplicate admin username")
            accounts[account.username] = account
        dummy_hash = hasher.hash(token_urlsafe(32)) if accounts else ""
        return cls(
            accounts, secure_cookie, dummy_hash, RateLimit(LOGIN_ATTEMPTS, LOGIN_WINDOW)
        )


def token_digest(token: str) -> str:
    return sha256(token.encode()).hexdigest()


def same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403, "Origin not allowed")


def current_account(request: HTTPConnection) -> Account:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        query = select(sessions.c.username).where(
            sessions.c.token_hash == token_digest(token),
            sessions.c.expires_at > time(),
        )
        with request.app.state.database.connect() as connection:
            username = connection.execute(query).scalar_one_or_none()
        account = request.app.state.administration.accounts.get(username)
        if account is not None:
            return account
    raise HTTPException(401, "Sign in required")


SignedIn = Annotated[Account, Depends(current_account)]


def operator_account(account: SignedIn) -> Account:
    if account.role not in ("superadmin", "operator"):
        raise HTTPException(403, "Operator permission required")
    return account


Operator = Annotated[Account, Depends(operator_account)]


@router.post("/session", dependencies=[Depends(same_origin)])
def login(payload: Login, request: Request, response: Response):
    administration = request.app.state.administration
    if not administration.accounts:
        raise HTTPException(401, "Invalid credentials")
    administration.rate_limit.check(request.client.host if request.client else "")
    account = administration.accounts.get(payload.username)
    digest = account.password_hash if account else administration.dummy_hash
    try:
        hasher.verify(digest, payload.password.get_secret_value())
    except (VerificationError, InvalidHashError):
        raise HTTPException(401, "Invalid credentials") from None
    if account is None:
        raise HTTPException(401, "Invalid credentials")

    token = token_urlsafe(32)
    old_token = request.cookies.get(SESSION_COOKIE)
    now = time()
    with request.app.state.database.begin() as connection:
        connection.execute(delete(sessions).where(sessions.c.expires_at <= now))
        if old_token:
            connection.execute(
                delete(sessions).where(sessions.c.token_hash == token_digest(old_token))
            )
        connection.execute(
            insert(sessions).values(
                token_hash=token_digest(token),
                username=account.username,
                expires_at=now + SESSION_AGE,
            )
        )
        record_admin_action(connection, account.username, "login", account.username)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_AGE,
        httponly=True,
        secure=administration.secure_cookie,
        samesite="strict",
        path="/api/admin",
    )
    response.headers["Cache-Control"] = "no-store"
    return {"username": account.username, "role": account.role}


@router.get("/session")
def session(account: SignedIn, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"username": account.username, "role": account.role}


@router.delete("/session", status_code=204, dependencies=[Depends(same_origin)])
def logout(request: Request, response: Response):
    if token := request.cookies.get(SESSION_COOKIE):
        with request.app.state.database.begin() as connection:
            username = connection.execute(
                delete(sessions)
                .where(sessions.c.token_hash == token_digest(token))
                .returning(sessions.c.username)
            ).scalar_one_or_none()
            if username is not None:
                record_admin_action(connection, username, "logout", username)
    response.delete_cookie(SESSION_COOKIE, path="/api/admin")


@router.get("/nodes")
def list_nodes(request: Request, _account: SignedIn):
    query = select(
        *(column for column in nodes.c if column.name not in ("token_hash", "frp_token")),
    ).order_by(nodes.c.id)
    with request.app.state.database.connect() as connection:
        active = ranges_by_node(connection)
        pending = ranges_by_node(connection, pending=True)
        items = []
        for row in connection.execute(query).mappings():
            item = dict(row)
            item["allowed_ports"] = format_spec(active.get(row["id"], ()))
            item["pending_allowed_ports"] = (
                format_spec(pending[row["id"]]) if row["id"] in pending else None
            )
            items.append(item)
    return {"nodes": items}


@router.post("/nodes", status_code=201, dependencies=[Depends(same_origin)])
def register_node(
    payload: NodeRegistration, request: Request, response: Response, account: SignedIn
):
    if account.role != "superadmin":
        raise HTTPException(403, "Superadmin permission required")
    if payload.client_channel != "hidden" and payload.server_port is None:
        raise HTTPException(422, "发布线路前需要填写 frpc 连接端口")
    ranges = parse_spec(payload.allowed_ports)
    statement = sqlite_insert(nodes).values(
        **payload.model_dump(exclude={
            "name", "allowed_ports", "server_addr", "server_port", "client_channel",
        }),
        **payload.client_config(),
        name=payload.name.strip(),
        token_hash="",
    )
    statement = statement.on_conflict_do_nothing(index_elements=[nodes.c.id])
    with request.app.state.database.begin() as connection:
        if connection.execute(statement).rowcount != 1:
            raise HTTPException(409, "Node already exists")
        replace_ranges(connection, payload.id, ranges)
        if payload.client_channel != "hidden":
            touch_node_catalog(connection)
        record_admin_action(connection, account.username, "node.register", payload.id)
    response.headers["Cache-Control"] = "no-store"
    return {"id": payload.id}


@router.get("/tunnels")
def list_tunnels(
    request: Request, _account: SignedIn, status: TunnelStatus | None = None
):
    query = select(tunnels).order_by(tunnels.c.node_id, tunnels.c.id)
    if status is not None:
        query = query.where(tunnels.c.status == status)
    with request.app.state.database.connect() as connection:
        items = [dict(row) for row in connection.execute(query).mappings()]
    return {"tunnels": items, "scanner_enabled": request.app.state.scanner_enabled}


def change_tunnel_block(
    tunnel_id: str, action: str, reason: str, request: Request, account: Account,
    run_id: str | None = None, created_at: float | None = None,
):
    try:
        command = enqueue_command(
            request.app.state.database,
            tunnel_id,
            action,
            reason,
            f"admin:{account.username}",
            expected_run_id=run_id,
            expected_created_at=created_at,
        )
    except CommandConflict as error:
        raise HTTPException(409, str(error)) from None
    if command is None:
        raise HTTPException(404, "Tunnel not found")
    return command


@router.post(
    "/tunnels/{tunnel_id}/block", status_code=202, dependencies=[Depends(same_origin)]
)
def block_tunnel(
    tunnel_id: str, payload: TunnelBlock, request: Request, account: Operator
):
    reason = payload.reason.strip()
    if not reason:
        raise HTTPException(422, "请输入封禁理由")
    return change_tunnel_block(tunnel_id, "block", reason, request, account, payload.run_id, payload.created_at)


@router.post(
    "/tunnels/{tunnel_id}/unblock", status_code=202, dependencies=[Depends(same_origin)]
)
def unblock_tunnel(tunnel_id: str, request: Request, account: Operator):
    return change_tunnel_block(tunnel_id, "unblock", "", request, account)


@router.get("/scans")
def list_scans(
    request: Request,
    _account: SignedIn,
    tunnel_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 32,
):
    query = (
        select(scans)
        .order_by(scans.c.checked_at.desc(), scans.c.id.desc())
        .limit(limit)
    )
    if tunnel_id is not None:
        query = query.where(scans.c.tunnel_id == tunnel_id)
    with request.app.state.database.connect() as connection:
        items = [dict(row) for row in connection.execute(query).mappings()]
    return {"scans": items}


@router.post(
    "/tunnels/{tunnel_id}/rescan", status_code=202, dependencies=[Depends(same_origin)]
)
def rescan_tunnel(tunnel_id: str, request: Request, account: Operator):
    if not request.app.state.scanner_enabled:
        raise HTTPException(409, "Scanner is disabled")
    with request.app.state.database.begin() as connection:
        changed = connection.execute(
            update(tunnels)
            .where(tunnels.c.id == tunnel_id, tunnels.c.status.in_(SCANNABLE))
            .values(next_scan_at=0)
            .returning(tunnels.c.id)
        ).scalar_one_or_none()
        if changed is None:
            exists = connection.execute(
                select(tunnels.c.id).where(tunnels.c.id == tunnel_id)
            ).scalar_one_or_none()
            if exists is None:
                raise HTTPException(404, "Tunnel not found")
            raise HTTPException(409, "Tunnel is not eligible for scanning")
        record_admin_action(connection, account.username, "tunnel.rescan", tunnel_id)
    return {"status": "scheduled"}


@router.patch("/nodes/{node_id}", dependencies=[Depends(same_origin)])
def edit_node(node_id: str, payload: NodeConfiguration, request: Request, account: SignedIn):
    if account.role != "superadmin":
        raise HTTPException(403, "Superadmin permission required")
    name, host = payload.name.strip(), payload.public_host
    ranges = parse_spec(payload.allowed_ports)
    client_config = payload.client_config()
    closing = []
    reopening = []
    with request.app.state.database.begin() as connection:
        lock_settings(connection)
        node = connection.execute(select(nodes).where(nodes.c.id == node_id)).mappings().one_or_none()
        if node is None:
            raise HTTPException(404, "节点不存在")
        channel = client_config.get("client_channel", node["client_channel"])
        server_port = client_config.get("server_port", node["server_port"])
        if channel != "hidden" and server_port is None:
            raise HTTPException(422, "发布线路前需要填写 frpc 连接端口")
        changing_range = ranges != load_ranges(connection, node_id)
        changing_host = host != node["public_host"]
        if has_pending(connection, node_id):
            raise HTTPException(409, "节点正在应用端口范围")
        if changing_range or changing_host:
            if node["status"] != "online":
                raise HTTPException(409, "节点上线后可以修改公网地址和映射端口范围")
            live_ports = select(tunnels.c.remote_port).where(
                tunnels.c.node_id == node_id,
                tunnels.c.proxy_present.is_(True),
                tunnels.c.status != "closed",
            )
            claims = select(tunnel_claims.c.remote_port).where(
                tunnel_claims.c.node_id == node_id, tunnel_claims.c.expires_at > time()
            )
            reserved = select(blocks.c.port).where(blocks.c.node_id == node_id, blocks.c.active)
            used = set(connection.execute(live_ports.union(claims, reserved)).scalars())
            conflicts = sorted(port for port in used if changing_host or not contains(ranges, port))
            if conflicts:
                ports = "、".join(map(str, conflicts))
                raise HTTPException(409, f"以下端口仍在使用或预留：{ports}")
        values = {"name": name, "public_host": host, **client_config}
        if changing_range:
            deployed = node["deployed_ports"]
            if deployed is None:
                raise HTTPException(409, "节点 Agent 尚未同步部署端口范围")
            if not within(ranges, deployed):
                raise HTTPException(
                    422,
                    f"映射范围须在节点已部署的端口范围内（当前 {format_spec(deployed)}）",
                )
            replace_ranges(connection, node_id, ranges, pending=True)
            values.update(range_revision=node["range_revision"] + 1, range_error=None)
        connection.execute(update(nodes).where(nodes.c.id == node_id).values(**values))
        if channel == "hidden" and node["client_channel"] != "hidden":
            closing = list(connection.execute(
                select(tunnels.c.id, tunnels.c.run_id, tunnels.c.remote_port).where(
                    tunnels.c.node_id == node_id,
                    tunnels.c.proxy_present.is_(True),
                    tunnels.c.status.in_(("pending", "active")),
                )
            ).mappings())
        elif channel != "hidden" and node["client_channel"] == "hidden":
            reopening = list(connection.execute(
                select(blocks.c.tunnel_id).where(
                    blocks.c.node_id == node_id,
                    blocks.c.active.is_(True),
                    blocks.c.source == "node-hidden",
                )
            ).scalars())
        if channel != node["client_channel"] or (channel != "hidden" and name != node["name"]):
            touch_node_catalog(connection)
        record_admin_action(connection, account.username, "node.edit", node_id)
    for tunnel in closing:
        with suppress(CommandConflict):
            enqueue_command(
                request.app.state.database,
                tunnel["id"],
                "block",
                "节点已下架，关闭隧道",
                "node-hidden",
                expected_run_id=tunnel["run_id"],
                expected_port=tunnel["remote_port"],
            )
    for tunnel_id in reopening:
        with suppress(CommandConflict):
            enqueue_command(
                request.app.state.database, tunnel_id, "unblock", "", "node-hidden"
            )
    return {"pending": changing_range}


@router.delete("/nodes/{node_id}", status_code=204, dependencies=[Depends(same_origin)])
async def delete_node(node_id: str, request: Request, account: SignedIn):
    if account.role != "superadmin":
        raise HTTPException(403, "Superadmin permission required")
    with request.app.state.database.begin() as connection:
        node = connection.execute(select(nodes).where(nodes.c.id == node_id)).mappings().one_or_none()
        if node is None:
            raise HTTPException(404, "节点不存在")
        tunnel_ids = select(tunnels.c.id).where(tunnels.c.node_id == node_id)
        connection.execute(delete(rooms).where(rooms.c.tunnel_id.in_(tunnel_ids)))
        connection.execute(delete(commands).where(commands.c.node_id == node_id))
        connection.execute(delete(scans).where(scans.c.tunnel_id.in_(tunnel_ids)))
        connection.execute(delete(blocks).where(blocks.c.node_id == node_id))
        connection.execute(delete(tunnel_claims).where(tunnel_claims.c.node_id == node_id))
        connection.execute(delete(tunnels).where(tunnels.c.node_id == node_id))
        connection.execute(delete(nodes).where(nodes.c.id == node_id))
        if node["client_channel"] != "hidden":
            touch_node_catalog(connection)
        record_admin_action(connection, account.username, "node.delete", node_id)
    hub = request.app.state.node_hub
    if hub is not None:
        await hub.disconnect(node_id)


def tunnel_snapshot(database):
    query = (
        select(
            tunnels.c.id, tunnels.c.run_id, tunnels.c.created_at, tunnels.c.proxy_name, tunnels.c.status,
            tunnels.c.node_id, tunnels.c.remote_port, tunnels.c.client_ip, tunnels.c.install_id,
        ).join(nodes).where(live_tunnels(time())).order_by(tunnels.c.node_id, tunnels.c.id)
    )
    with database.connect() as connection:
        return list(map(dict, connection.execute(query).mappings()))


async def stream_tunnels(app):
    current = None
    sent = set()
    while True:
        subscribers = app.state.tunnel_subscribers
        if subscribers:
            try:
                rows = await to_thread(tunnel_snapshot, app.state.database)
            except SQLAlchemyError:
                logger.exception("Could not read live tunnels")
            else:
                sent.intersection_update(subscribers)
                recipients = subscribers if rows != current else subscribers - sent
                for queue in recipients:
                    if queue.full():
                        queue.get_nowait()
                    queue.put_nowait(rows)
                sent.update(recipients)
                current = rows
        else:
            sent.clear()
            current = None
        await asyncio.sleep(1)


@router.websocket("/tunnels/live")
async def watch_tunnels(socket: WebSocket):
    scheme = "https" if socket.url.scheme == "wss" else "http"
    if socket.headers.get("origin") != f"{scheme}://{socket.url.netloc}":
        await socket.close(code=1008)
        return
    try:
        await to_thread(current_account, socket)
    except HTTPException:
        await socket.close(code=1008)
        return
    await socket.accept()
    app = socket.app
    queue = asyncio.Queue(maxsize=1)
    app.state.tunnel_subscribers.add(queue)
    receiver = asyncio.create_task(socket.receive())
    try:
        last_auth = time()
        while not receiver.done():
            now = time()
            if now - last_auth >= 5:
                try:
                    await to_thread(current_account, socket)
                except HTTPException:
                    await socket.close(code=1008)
                    return
                last_auth = now
            update_rows = asyncio.create_task(queue.get())
            try:
                ready, _ = await asyncio.wait(
                    (receiver, update_rows), timeout=5, return_when=asyncio.FIRST_COMPLETED
                )
                if receiver in ready:
                    return
                if update_rows in ready:
                    await socket.send_json({
                        "tunnels": update_rows.result(), "scanner_enabled": app.state.scanner_enabled,
                    })
                else:
                    await socket.send_json({"type": "heartbeat"})
            finally:
                update_rows.cancel()
    except (OSError, WebSocketDisconnect):
        pass
    finally:
        receiver.cancel()
        app.state.tunnel_subscribers.discard(queue)


DURATIONS = {"1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000}


def add_blacklist_rule(connection, target, payload, actor, now):
    reason = payload.reason.strip()
    if not reason:
        raise HTTPException(422, "请输入封禁理由")
    if connection.execute(
        select(blacklist.c.ip).where(blacklist.c.ip == target, active_blacklist(now))
    ).first():
        raise HTTPException(409, "此 IP 或网段已有生效中的规则")
    unsupported = nodes.c.deployed_ports.is_(None)
    if "/" in target:
        unsupported |= nodes.c.blacklist_cidr.is_(False)
    pending = connection.execute(select(nodes.c.id).where(unsupported).order_by(nodes.c.id)).scalars().all()
    if pending:
        raise HTTPException(409, "以下节点尚未支持此规则：" + "、".join(pending))
    duration = DURATIONS.get(payload.duration)
    values = {
        "reason": reason, "actor": actor, "created_at": now,
        "expires_at": now + duration if duration else None, "removed_at": None,
    }
    connection.execute(
        sqlite_insert(blacklist).values(ip=target, **values)
        .on_conflict_do_update(index_elements=[blacklist.c.ip], set_=values)
    )
    affected = select(tunnels.c.id).where(ip_matches(target, tunnels.c.client_ip))
    connection.execute(delete(rooms).where(
        ip_matches(target, rooms.c.client_ip) | rooms.c.tunnel_id.in_(affected)
    ))
    connection.execute(update(settings).values(blacklist_revision=settings.c.blacklist_revision + 1))
    record_admin_action(connection, actor, "blacklist.add", target)
    return {"ip": target, "status": "pending"}


@router.post("/blacklist", status_code=202, dependencies=[Depends(same_origin)])
def create_blacklist(payload: ManualBlacklist, request: Request, account: Operator):
    target = parse_blacklist_target(payload.target)
    if target in ("0.0.0.0/0", "::/0") and not payload.confirm_all:
        raise HTTPException(409, {"code": "confirm_all", "target": target})
    with request.app.state.database.begin() as connection:
        lock_settings(connection)
        return add_blacklist_rule(connection, target, payload, account.username, time())


def parse_blacklist_target(target: str):
    try:
        return canonical_rule(target)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@router.post("/tunnels/{tunnel_id}/blacklist", status_code=202, dependencies=[Depends(same_origin)])
def blacklist_tunnel(tunnel_id: str, payload: TunnelBlacklist, request: Request, account: Operator):
    now = time()
    with request.app.state.database.begin() as connection:
        lock_settings(connection)
        tunnel = connection.execute(
            select(tunnels).join(nodes).where(
                tunnels.c.id == tunnel_id, tunnels.c.run_id == payload.run_id,
                tunnels.c.created_at == payload.created_at, live_tunnels(now)
            )
        ).mappings().one_or_none()
        if tunnel is None:
            raise HTTPException(409, "隧道已变化，请刷新后重试")
        ip = canonical_ip(tunnel["client_ip"])
        if ip is None:
            raise HTTPException(409, "此隧道尚无可验证的客户端 IP")
        return add_blacklist_rule(connection, ip, payload, account.username, now)


@router.get("/blacklist")
def list_blacklist(request: Request, _account: SignedIn):
    now = time()
    with request.app.state.database.connect() as connection:
        ips = connection.execute(
            select(blacklist).where(active_blacklist(now)).order_by(blacklist.c.created_at.desc())
        ).mappings()
        entries = list(map(dict, ips))
        tunnel_blocks = connection.execute(
            select(blocks, tunnels.c.status).join(tunnels).where(blocks.c.active)
            .order_by(blocks.c.created_at.desc())
        ).mappings()
        blocked = list(map(dict, tunnel_blocks))
        revision = connection.execute(select(settings.c.blacklist_revision)).scalar_one()
        pending_nodes = list(map(dict, connection.execute(
            select(nodes.c.id, nodes.c.status, nodes.c.blacklist_error)
            .where(nodes.c.blacklist_revision.is_distinct_from(revision)).order_by(nodes.c.id)
        ).mappings())) if revision else []
    return {"entries": entries, "blocks": blocked, "pending_nodes": pending_nodes}


@router.delete("/blacklist", status_code=204, dependencies=[Depends(same_origin)])
def remove_blacklist_rule(
    request: Request, account: Operator, target: Annotated[str, Query(min_length=1, max_length=64)],
):
    expire_blacklist_rule(parse_blacklist_target(target), request, account)


@router.delete("/blacklist/{ip}", status_code=204, dependencies=[Depends(same_origin)])
def remove_blacklist(ip: str, request: Request, account: Operator):
    expire_blacklist_rule(canonical_ip(ip), request, account)


def expire_blacklist_rule(address, request, account):
    now = time()
    with request.app.state.database.begin() as connection:
        removed = connection.execute(
            update(blacklist).where(blacklist.c.ip == address, active_blacklist(now))
            .values(removed_at=now)
        )
        if removed.rowcount:
            connection.execute(update(settings).values(blacklist_revision=settings.c.blacklist_revision + 1))
            record_admin_action(connection, account.username, "blacklist.remove", address)


@router.get("/online")
def list_online(request: Request, _account: SignedIn):
    with request.app.state.database.connect() as connection:
        return {"online": live_online_users(connection)}


@router.get("/lobby/rooms")
def admin_rooms(request: Request, _account: SignedIn):
    with request.app.state.database.connect() as connection:
        enabled = connection.execute(select(settings.c.lobby_enabled)).scalar_one()
        return {"enabled": enabled, "rooms": live_rooms(connection)}


@router.patch("/lobby", dependencies=[Depends(same_origin)])
def edit_lobby(payload: LobbyToggle, request: Request, account: Operator):
    with request.app.state.database.begin() as connection:
        changed = connection.execute(update(settings).where(
            settings.c.lobby_enabled != payload.enabled
        ).values(lobby_enabled=payload.enabled))
        if changed.rowcount:
            action = "lobby.open" if payload.enabled else "lobby.close"
            record_admin_action(connection, account.username, action, "public")
    return {"enabled": payload.enabled}


@router.delete("/lobby/rooms/{listing_id}", status_code=204, dependencies=[Depends(same_origin)])
def unlist_room(listing_id: str, request: Request, account: Operator):
    with request.app.state.database.begin() as connection:
        entry = connection.execute(
            delete(rooms).where(rooms.c.listing_id == listing_id).returning(
                rooms.c.full_room_code, rooms.c.client_ip, rooms.c.publication_id
            )
        ).one_or_none()
        if entry is not None:
            connection.execute(sqlite_insert(removals).values(
                code=entry.full_room_code, owner=entry.client_ip,
                publication_id=entry.publication_id or "", removed_at=time(),
            ).on_conflict_do_nothing())
            record_admin_action(connection, account.username, "lobby.unlist", listing_id)


SECURITY_COLUMNS = (
    settings.c.auto_ban_enabled,
    settings.c.auto_ban_attempts,
    settings.c.auto_ban_window,
    settings.c.auto_ban_seconds,
    settings.c.auto_ban_skip_legit,
)


def security_payload(row):
    return {
        "auto_ban_enabled": bool(row["auto_ban_enabled"]),
        "auto_ban_attempts": row["auto_ban_attempts"],
        "auto_ban_window_minutes": row["auto_ban_window"] // 60,
        "auto_ban_hours": row["auto_ban_seconds"] // 3600,
        "auto_ban_skip_legit": bool(row["auto_ban_skip_legit"]),
    }


@router.get("/settings")
def read_security_settings(request: Request, _account: SignedIn):
    with request.app.state.database.connect() as connection:
        row = connection.execute(select(*SECURITY_COLUMNS)).mappings().one()
    return security_payload(row)


@router.patch("/settings", dependencies=[Depends(same_origin)])
def edit_security_settings(
    payload: SecuritySettings, request: Request, account: Operator
):
    values = {
        "auto_ban_enabled": payload.auto_ban_enabled,
        "auto_ban_attempts": payload.auto_ban_attempts,
        "auto_ban_window": payload.auto_ban_window_minutes * 60,
        "auto_ban_seconds": payload.auto_ban_hours * 3600,
        "auto_ban_skip_legit": payload.auto_ban_skip_legit,
    }
    with request.app.state.database.begin() as connection:
        lock_settings(connection)
        connection.execute(update(settings).values(**values))
        record_admin_action(
            connection, account.username, "settings.security", "security"
        )
        row = connection.execute(select(*SECURITY_COLUMNS)).mappings().one()
    return security_payload(row)
