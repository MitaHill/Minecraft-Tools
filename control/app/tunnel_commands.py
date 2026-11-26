from time import time

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    case,
    delete,
    select,
    update,
)
from sqlalchemy.engine import Engine

from .database import metadata, record_admin_action
from .lobby import canonical_host, rooms
from .registry import blocks, nodes, tunnels

commands = Table(
    "node_commands",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("block_id", Integer, ForeignKey("blocks.id"), nullable=False),
    Column("node_id", String, ForeignKey("nodes.id"), nullable=False),
    Column("action", String, nullable=False),
    Column("status", String, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("acked_at", Float),
    Column("next_attempt_at", Float, nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", String),
    sqlite_autoincrement=True,
)


class CommandConflict(ValueError):
    pass


def enqueue_command(
    database: Engine,
    tunnel_id: str,
    action: str,
    reason: str,
    source: str,
    expected_run_id: str | None = None,
    expected_port: int | None = None,
    expected_scan_at: float | None = None,
    expected_created_at: float | None = None,
) -> dict | None:
    now = time()
    with database.begin() as connection:
        # Acquire the write lock before evaluating the current tunnel generation.
        connection.execute(
            update(tunnels)
            .where(tunnels.c.id == tunnel_id)
            .values(status=tunnels.c.status)
        )
        tunnel = (
            connection.execute(select(tunnels).where(tunnels.c.id == tunnel_id))
            .mappings()
            .one_or_none()
        )
        if tunnel is None:
            return None
        if expected_run_id is not None and tunnel["run_id"] != expected_run_id:
            raise CommandConflict("Tunnel generation changed")
        if expected_created_at is not None and (
            tunnel["created_at"] != expected_created_at or not tunnel["proxy_present"]
        ):
            raise CommandConflict("隧道已变化，请刷新后重试")
        if expected_port is not None and tunnel["remote_port"] != expected_port:
            raise CommandConflict("Tunnel port changed")
        if expected_scan_at is not None and tunnel["last_scan_at"] != expected_scan_at:
            raise CommandConflict("A more recent scan superseded this result")
        active = (
            connection.execute(
                select(blocks).where(
                    blocks.c.tunnel_id == tunnel_id,
                    blocks.c.active.is_(True),
                )
            )
            .mappings()
            .one_or_none()
        )
        if active is not None:
            newest = (
                connection.execute(
                    select(commands)
                    .where(
                        commands.c.block_id == active["id"],
                    )
                    .order_by(commands.c.id.desc())
                    .limit(1)
                )
                .mappings()
                .one()
            )
            if newest["action"] == action:
                return {"command_id": newest["id"], "status": newest["status"]}
            if newest["status"] == "pending":
                raise CommandConflict("Wait for the previous firewall command")

        if action == "block":
            if tunnel["status"] not in ("active", "pending"):
                raise CommandConflict("Tunnel has no current active generation")
            port_reserved = connection.execute(
                select(blocks.c.id).where(
                    blocks.c.node_id == tunnel["node_id"],
                    blocks.c.port == tunnel["remote_port"],
                    blocks.c.active.is_(True),
                )
            ).first()
            if port_reserved is not None:
                raise CommandConflict("Port already reserved by another tunnel")
            block_id = connection.execute(
                blocks.insert().values(
                    node_id=tunnel["node_id"],
                    tunnel_id=tunnel_id,
                    port=tunnel["remote_port"],
                    run_id=tunnel["run_id"],
                    proxy_name=tunnel["proxy_name"],
                    reason=reason,
                    source=source,
                    client_ip=tunnel["client_ip"],
                    active=True,
                    created_at=now,
                )
            ).inserted_primary_key[0]
            connection.execute(
                update(tunnels)
                .where(tunnels.c.id == tunnel_id)
                .values(
                    status="block_pending",
                    block_reason=reason,
                )
            )
            public_host = connection.execute(
                select(nodes.c.public_host).where(
                    nodes.c.id == tunnel["node_id"],
                )
            ).scalar_one()
            connection.execute(
                delete(rooms).where(
                    (rooms.c.tunnel_id == tunnel_id)
                    | (
                        (rooms.c.server_addr == canonical_host(public_host))
                        & (rooms.c.remote_port == tunnel["remote_port"])
                    ),
                )
            )
        elif action == "unblock" and active is not None:
            block_id = active["id"]
        else:
            raise CommandConflict("Tunnel is not blocked")

        command_id = connection.execute(
            commands.insert().values(
                block_id=block_id,
                node_id=tunnel["node_id"],
                action=action,
                status="pending",
                created_at=now,
                next_attempt_at=now,
            )
        ).inserted_primary_key[0]
        if source.startswith("admin:"):
            record_admin_action(
                connection, source.removeprefix("admin:"), f"tunnel.{action}", tunnel_id
            )
        return {"command_id": command_id, "status": "pending"}


def next_command(database: Engine, node_id: str) -> dict | None:
    now = time()
    with database.begin() as connection:
        pending = (
            select(commands.c.id)
            .where(
                commands.c.node_id == node_id,
                commands.c.status == "pending",
                commands.c.next_attempt_at <= now,
            )
            .order_by(commands.c.id)
            .limit(1)
            .scalar_subquery()
        )
        attempted = (
            connection.execute(
                update(commands)
                .where(commands.c.id == pending)
                .values(
                    next_attempt_at=now + 15,
                    attempts=commands.c.attempts + 1,
                )
                .returning(commands.c.id, commands.c.action, commands.c.block_id)
            )
            .mappings()
            .one_or_none()
        )
        if attempted is None:
            return None
        target = (
            connection.execute(
                select(blocks).where(blocks.c.id == attempted["block_id"])
            )
            .mappings()
            .one()
        )
        return {
            "command_id": attempted["id"],
            "action": attempted["action"],
            "port": target["port"],
            "proxy_name": target["proxy_name"],
            "run_id": target["run_id"],
        }


def acknowledge_command(
    database: Engine, node_id: str, command_id: int, ok: bool, error: str
) -> bool:
    now = time()
    with database.begin() as connection:
        match = (
            (commands.c.id == command_id)
            & (commands.c.node_id == node_id)
            & (commands.c.attempts > 0)
        )
        command = (
            connection.execute(
                update(commands)
                .where(match, commands.c.status == "pending")
                .values(
                    status="acked" if ok else "pending",
                    acked_at=now if ok else None,
                    last_error=None if ok else (error or "node returned a failure"),
                    next_attempt_at=now + 15,
                )
                .returning(commands.c.block_id, commands.c.action)
            )
            .mappings()
            .one_or_none()
        )
        if command is None:
            return (
                connection.execute(
                    select(commands.c.id).where(match, commands.c.status == "acked")
                ).first()
                is not None
            )
        if not ok:
            return True
        target = (
            connection.execute(select(blocks).where(blocks.c.id == command["block_id"]))
            .mappings()
            .one()
        )
        if command["action"] == "block":
            changes = {"status": "blocked", "blocked_at": now}
        else:
            connection.execute(
                update(blocks)
                .where(blocks.c.id == target["id"])
                .values(active=False, removed_at=now)
            )
            changes = {
                "status": case(
                    (tunnels.c.proxy_present.is_(True), "pending"), else_="closed"
                ),
                "blocked_at": None,
                "block_reason": None,
                "scan_failures": 0,
                "last_scan_at": None,
                "next_scan_at": now,
                "created_at": now,
            }
        connection.execute(
            update(tunnels).where(tunnels.c.id == target["tunnel_id"]).values(**changes)
        )
        return True
