from hashlib import sha256
from secrets import token_urlsafe
from time import time
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, select, update

from .registry import blocks, installs, nodes, tunnel_claims, tunnels

router = APIRouter(prefix="/api/v1")
bearer = HTTPBearer(auto_error=False)
CLAIM_SECONDS = 120


class TunnelRegistration(BaseModel):
    node_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    remote_port: int = Field(ge=1024, le=65535, strict=True)


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def current_install(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    if credentials is not None and len(credentials.credentials) == 43:
        with request.app.state.database.connect() as connection:
            install_id = connection.execute(
                select(installs.c.id).where(
                    installs.c.token_hash == digest(credentials.credentials)
                )
            ).scalar_one_or_none()
        if install_id is not None:
            return install_id
    raise HTTPException(
        401,
        "Installation authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


Install = Annotated[str, Depends(current_install)]


@router.post("/installs/register", status_code=201)
def register_install(request: Request, response: Response):
    request.app.state.install_rate_limit.check(
        request.client.host if request.client else ""
    )
    install_id, token = uuid4().hex, token_urlsafe(32)
    now = time()
    with request.app.state.database.begin() as connection:
        connection.execute(
            installs.insert().values(
                id=install_id,
                token_hash=digest(token),
                created_at=now,
                last_seen=now,
            )
        )
    response.headers["Cache-Control"] = "no-store"
    return {"install_id": install_id, "token": token}


@router.post("/tunnels/register", status_code=201)
def register_tunnel(
    payload: TunnelRegistration,
    request: Request,
    response: Response,
    install_id: Install,
):
    request.app.state.claim_rate_limit.check(install_id)
    now = time()
    claim_id, proof = uuid4().hex, token_urlsafe(32)
    node_id, port = payload.node_id, payload.remote_port
    with request.app.state.database.begin() as connection:
        # The first write serializes reservation, snapshot, and block transactions.
        connection.execute(
            update(installs).where(installs.c.id == install_id).values(last_seen=now)
        )
        node = connection.execute(
            select(nodes.c.port_start, nodes.c.port_end, nodes.c.status).where(
                nodes.c.id == node_id
            )
        ).one_or_none()
        if node is None:
            raise HTTPException(404, "Node not found")
        if not node.port_start <= port <= node.port_end:
            raise HTTPException(422, "Port outside the node range")
        if node.status != "online":
            raise HTTPException(503, "Node is offline")
        connection.execute(
            delete(tunnel_claims).where(tunnel_claims.c.expires_at <= now)
        )
        reserved = (
            select(tunnel_claims.c.id)
            .where(
                tunnel_claims.c.node_id == node_id,
                tunnel_claims.c.remote_port == port,
            )
            .exists()
        )
        blocked = (
            select(blocks.c.id)
            .where(
                blocks.c.node_id == node_id,
                blocks.c.port == port,
                blocks.c.active.is_(True),
            )
            .exists()
        )
        live = (
            select(tunnels.c.id)
            .where(
                tunnels.c.node_id == node_id,
                tunnels.c.remote_port == port,
                tunnels.c.proxy_present.is_(True),
                tunnels.c.status != "closed",
            )
            .exists()
        )
        if connection.execute(select(reserved | blocked | live)).scalar_one():
            raise HTTPException(409, "Port unavailable")
        connection.execute(
            tunnel_claims.insert().values(
                id=claim_id,
                install_id=install_id,
                node_id=node_id,
                remote_port=port,
                proof_hash=digest(proof),
                expires_at=now + CLAIM_SECONDS,
            )
        )
    response.headers["Cache-Control"] = "no-store"
    return {"id": claim_id, "proxy_name": f"minecraft-{claim_id}", "proof": proof}


def claim_status(connection, claim_id: str, install_id: str, now: float):
    association = and_(
        tunnels.c.node_id == tunnel_claims.c.node_id,
        tunnels.c.proxy_name == f"minecraft-{claim_id}",
        tunnels.c.remote_port == tunnel_claims.c.remote_port,
        tunnels.c.install_id == tunnel_claims.c.install_id,
    )
    row = (
        connection.execute(
            select(
                tunnel_claims.c.bound_at,
                tunnels.c.status,
                tunnels.c.block_reason,
                tunnels.c.blocked_at,
                tunnels.c.last_scan_at,
            )
            .select_from(tunnel_claims.outerjoin(tunnels, association))
            .where(
                tunnel_claims.c.id == claim_id,
                tunnel_claims.c.install_id == install_id,
                tunnel_claims.c.expires_at > now,
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise HTTPException(404, "Tunnel registration not found")
    return {
        "status": row["status"]
        or ("closed" if row["bound_at"] is not None else "pending"),
        "reason": row["block_reason"],
        "blocked_at": row["blocked_at"],
        "last_scan_at": row["last_scan_at"],
    }


@router.get("/tunnels/{claim_id}/status")
def read_status(
    claim_id: str, request: Request, response: Response, install_id: Install
):
    response.headers["Cache-Control"] = "no-store"
    with request.app.state.database.connect() as connection:
        return claim_status(connection, claim_id, install_id, time())


@router.post("/tunnels/{claim_id}/heartbeat")
def renew_tunnel(
    claim_id: str, request: Request, response: Response, install_id: Install
):
    now = time()
    with request.app.state.database.begin() as connection:
        connection.execute(
            update(tunnel_claims)
            .where(
                tunnel_claims.c.id == claim_id,
                tunnel_claims.c.install_id == install_id,
                tunnel_claims.c.expires_at > now,
            )
            .values(expires_at=now + CLAIM_SECONDS)
        )
        result = claim_status(connection, claim_id, install_id, now)
        connection.execute(
            update(installs).where(installs.c.id == install_id).values(last_seen=now)
        )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.delete("/tunnels/{claim_id}", status_code=204)
def release_tunnel(claim_id: str, request: Request, install_id: Install):
    with request.app.state.database.begin() as connection:
        connection.execute(
            delete(tunnel_claims).where(
                tunnel_claims.c.id == claim_id,
                tunnel_claims.c.install_id == install_id,
            )
        )
