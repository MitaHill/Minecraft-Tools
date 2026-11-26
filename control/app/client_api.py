from hashlib import sha256
from secrets import token_urlsafe
from time import time
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, select, union, update

from .ip_rules import canonical_ip
from .moderation import ip_block, lock_settings, settings
from .node_ports import has_pending, load_ranges
from .registry import blocks, installs, nodes, tunnel_claims, tunnels

router = APIRouter(prefix="/api/v1")
bearer = HTTPBearer(auto_error=False)
CLAIM_SECONDS = 120


class TunnelRegistration(BaseModel):
    node_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")


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


@router.get("/node-catalog")
def node_catalog(request: Request, response: Response, channel: Literal["stable", "dev"] = "stable"):
    response.headers["Cache-Control"] = "no-store"
    with request.app.state.database.connect() as connection:
        reject_blacklisted(connection, request)
        updated_at = connection.execute(select(settings.c.catalog_updated_at)).scalar_one()
        entries = connection.execute(
            select(nodes.c.id, nodes.c.name)
            .where(nodes.c.client_channel.in_((channel, "both")))
            .order_by(nodes.c.id)
        ).mappings()
        return {
            "blocked": False,
            "nodes": list(map(dict, entries)),
            "updated_at": updated_at,
            "min_frpc_version": request.app.state.min_frpc_version,
        }


def request_ip(request):
    return canonical_ip(request.client.host) if request.client else None


def reject_blacklisted(connection, request):
    reason = ip_block(connection, request_ip(request))
    if reason is not None:
        raise HTTPException(
            403,
            {"code": "ip_blacklisted", "blocked": True, "reason": reason},
            headers={"Cache-Control": "no-store"},
        )


def available_port(connection, node_id: str, ranges) -> int | None:
    used = set(connection.scalars(union(
        select(tunnel_claims.c.remote_port).where(tunnel_claims.c.node_id == node_id),
        select(blocks.c.port).where(
            blocks.c.node_id == node_id, blocks.c.active.is_(True),
        ),
        select(tunnels.c.remote_port).where(
            tunnels.c.node_id == node_id,
            tunnels.c.proxy_present.is_(True), tunnels.c.status != "closed",
        ),
    )))
    for start, end in ranges:
        for port in range(start, end + 1):
            if port not in used:
                return port
    return None


@router.post("/installs/register", status_code=201)
def register_install(request: Request, response: Response):
    request.app.state.install_rate_limit.check(
        request.client.host if request.client else ""
    )
    install_id, token = uuid4().hex, token_urlsafe(32)
    now = time()
    with request.app.state.database.begin() as connection:
        # Serialize install enrollment with a blacklist update.
        lock_settings(connection)
        reject_blacklisted(connection, request)
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
    node_id = payload.node_id
    with request.app.state.database.begin() as connection:
        # The first write serializes reservation, snapshot, and block transactions.
        connection.execute(
            update(installs).where(installs.c.id == install_id).values(last_seen=now)
        )
        reject_blacklisted(connection, request)
        node = connection.execute(
            select(nodes).where(nodes.c.id == node_id)
        ).mappings().one_or_none()
        if node is None or node["client_channel"] == "hidden":
            raise HTTPException(404, {"code": "node_not_found"})
        if node["server_port"] is None:
            raise HTTPException(503, {"code": "node_unconfigured"})
        if has_pending(connection, node_id):
            raise HTTPException(409, {"code": "node_range_pending"})
        if node["status"] != "online":
            raise HTTPException(503, {"code": "node_offline"})
        connection.execute(
            delete(tunnel_claims).where(tunnel_claims.c.expires_at <= now)
        )
        port = available_port(connection, node_id, load_ranges(connection, node_id))
        if port is None:
            raise HTTPException(409, {"code": "no_available_port"})
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
    return {
        "id": claim_id,
        "proxy_name": f"minecraft-{claim_id}",
        "proof": proof,
        "remote_port": port,
        "server_addr": node["server_addr"] or node["public_host"],
        "server_port": node["server_port"],
        "public_host": node["public_host"],
        "frp_token": node["frp_token"] or "",
    }


def claim_status(connection, claim_id: str, install_id: str, now: float, client_ip=None):
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
                tunnels.c.client_ip,
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
    if reason := ip_block(connection, row["client_ip"], client_ip):
        return {"status": "blocked", "reason": reason, "code": "ip_blacklisted",
                "blocked_at": row["blocked_at"], "last_scan_at": row["last_scan_at"]}
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
        return claim_status(connection, claim_id, install_id, time(), request_ip(request))


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
        result = claim_status(connection, claim_id, install_id, now, request_ip(request))
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
