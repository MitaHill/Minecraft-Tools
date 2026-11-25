import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from secrets import token_urlsafe
from time import time
from typing import Annotated, Literal

from argon2 import PasswordHasher, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, SecretStr, model_validator
from sqlalchemy import Column, Float, String, Table, delete, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .database import metadata
from .rate_limit import RateLimit
from .registry import nodes, tunnels
from .scanner import SCANNABLE, scans
from .tunnel_commands import CommandConflict, enqueue_command

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


class NodeRegistration(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    name: str = Field(min_length=1, max_length=80)
    public_host: str = Field(min_length=1, max_length=253)
    port_start: int = Field(ge=1024, le=65535)
    port_end: int = Field(ge=1024, le=65535)

    @model_validator(mode="after")
    def validate_port_range(self):
        if self.port_end < self.port_start:
            raise ValueError("port_end must be greater than or equal to port_start")
        return self


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


def current_account(request: Request) -> Account:
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
            connection.execute(
                delete(sessions).where(sessions.c.token_hash == token_digest(token))
            )
    response.delete_cookie(SESSION_COOKIE, path="/api/admin")


@router.get("/nodes")
def list_nodes(request: Request, _account: SignedIn):
    query = select(
        *(column for column in nodes.c if column.name != "token_hash")
    ).order_by(nodes.c.id)
    with request.app.state.database.connect() as connection:
        items = [dict(row) for row in connection.execute(query).mappings()]
    return {"nodes": items}


@router.post("/nodes", status_code=201, dependencies=[Depends(same_origin)])
def register_node(
    payload: NodeRegistration, request: Request, response: Response, account: SignedIn
):
    if account.role != "superadmin":
        raise HTTPException(403, "Superadmin permission required")
    token = token_urlsafe(32)
    statement = sqlite_insert(nodes).values(
        **payload.model_dump(), token_hash=token_digest(token)
    )
    statement = statement.on_conflict_do_nothing(index_elements=[nodes.c.id])
    with request.app.state.database.begin() as connection:
        if connection.execute(statement).rowcount != 1:
            raise HTTPException(409, "Node already exists")
    response.headers["Cache-Control"] = "no-store"
    return {"id": payload.id, "secret": token}


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
    tunnel_id: str, action: str, reason: str, request: Request, account: Account
):
    try:
        command = enqueue_command(
            request.app.state.database,
            tunnel_id,
            action,
            reason,
            f"admin:{account.username}",
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
    return change_tunnel_block(tunnel_id, "block", payload.reason, request, account)


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
def rescan_tunnel(tunnel_id: str, request: Request, _account: Operator):
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
    return {"status": "scheduled"}
