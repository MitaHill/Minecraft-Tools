import asyncio
import logging
from collections import Counter
from contextlib import suppress
from dataclasses import asdict, dataclass
from random import uniform
from time import time

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    delete,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from .database import metadata
from .java_probe import Status, probe
from .registry import nodes, tunnels
from .tunnel_commands import CommandConflict, enqueue_command

logger = logging.getLogger(__name__)
SCANNABLE = ("pending", "active", "offline")
MAX_PER_NODE = 4
MAX_PER_ROUND = 16
KEEP_SCANS_PER_TUNNEL = 32
# 返回给客户端展示的封禁文案；后续按触发条件补充各自的文案。
AUTO_BLOCK_MESSAGE = "未检测到 Minecraft Java 响应"

scans = Table(
    "tunnel_scans",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("tunnel_id", String, ForeignKey("tunnels.id"), nullable=False),
    Column("checked_at", Float, nullable=False),
    Column("result", String, nullable=False),
    Column("latency_ms", Integer),
    Column("version", String),
    Column("motd", String),
    Column("error", String),
)


@dataclass(frozen=True)
class ScanPolicy:
    interval: float = 30
    grace: float = 60
    failures: int = 3
    unreachable_grace: float = 600
    strict_motd: bool = False
    private_test_nodes: frozenset[str] = frozenset()

    def __post_init__(self):
        if (
            not 1 <= self.interval <= 3600
            or not 0 <= self.grace <= 3600
            or not 1 <= self.failures <= 20
        ):
            raise ValueError(
                "Invalid scanner interval, grace period, or failure threshold"
            )


def select_due(database: Engine, now: float) -> list[dict]:
    columns = (
        tunnels.c.id,
        tunnels.c.run_id,
        tunnels.c.node_id,
        tunnels.c.remote_port,
        nodes.c.public_host,
    )
    with database.connect() as connection:
        rows = connection.execute(
            select(*columns)
            .join(nodes)
            .where(
                nodes.c.status == "online",
                tunnels.c.status.in_(SCANNABLE),
                tunnels.c.next_scan_at <= now,
                tunnels.c.remote_port.between(nodes.c.port_start, nodes.c.port_end),
            )
            .order_by(tunnels.c.next_scan_at, tunnels.c.id)
        ).mappings()
        selected = []
        per_node = Counter()
        for row in rows:
            if per_node[row["node_id"]] < MAX_PER_NODE:
                selected.append(dict(row))
                per_node[row["node_id"]] += 1
                if len(selected) >= MAX_PER_ROUND:
                    break
    return selected


def record_scan(
    database: Engine,
    target: dict,
    result: Status,
    policy: ScanPolicy,
    now: float,
    automatic_bans: bool,
) -> None:
    if result.result == "ok" and policy.strict_motd and not (result.motd or "").strip():
        result = Status(
            "not_minecraft",
            result.latency_ms,
            result.version,
            result.motd,
            "Empty MOTD",
        )
    same_tunnel = (
        (tunnels.c.id == target["id"])
        & (tunnels.c.run_id == target["run_id"])
        & (tunnels.c.remote_port == target["remote_port"])
        & tunnels.c.status.in_(SCANNABLE)
        & tunnels.c.node_id.in_(select(nodes.c.id).where(nodes.c.status == "online"))
    )
    with database.begin() as connection:
        current = (
            connection.execute(
                update(tunnels)
                .where(same_tunnel)
                .values(
                    last_scan_at=now,
                    next_scan_at=now + policy.interval * uniform(0.9, 1.1),
                )
                .returning(
                    tunnels.c.status,
                    tunnels.c.scan_failures,
                    tunnels.c.created_at,
                    tunnels.c.last_reachable_at,
                )
            )
            .mappings()
            .one_or_none()
        )
        if current is None:
            return
        failures = 0
        changes = {}
        if result.result in ("ok", "not_minecraft"):
            changes["last_reachable_at"] = now
        if result.result == "ok":
            changes["status"] = "active"
        elif result.result == "not_minecraft":
            if current["status"] == "offline":
                changes["status"] = "pending"
            if now - current["created_at"] >= policy.grace:
                failures = min(policy.failures, current["scan_failures"] + 1)
        elif (
            result.result == "unreachable"
            and now - (current["last_reachable_at"] or current["created_at"])
            >= policy.unreachable_grace
        ):
            changes["status"] = "offline"
        changes["scan_failures"] = failures
        connection.execute(update(tunnels).where(same_tunnel).values(**changes))
        connection.execute(
            scans.insert().values(
                tunnel_id=target["id"], checked_at=now, **asdict(result)
            )
        )
        older = (
            select(scans.c.id)
            .where(scans.c.tunnel_id == target["id"])
            .order_by(scans.c.id.desc())
            .offset(KEEP_SCANS_PER_TUNNEL)
        )
        connection.execute(delete(scans).where(scans.c.id.in_(older)))

    if automatic_bans and failures >= policy.failures:
        with suppress(CommandConflict):
            enqueue_command(
                database,
                target["id"],
                "block",
                AUTO_BLOCK_MESSAGE,
                "scanner",
                expected_run_id=target["run_id"],
                expected_port=target["remote_port"],
                expected_scan_at=now,
            )


async def run_scanner(
    database: Engine, policy: ScanPolicy, stop: asyncio.Event
) -> None:
    async def check(target):
        return await probe(
            target["public_host"],
            target["remote_port"],
            allow_private=target["node_id"] in policy.private_test_nodes,
        )

    while not stop.is_set():
        try:
            batch = await asyncio.to_thread(select_due, database, time())
            if batch:
                results = await asyncio.gather(
                    *(check(target) for target in batch), return_exceptions=True
                )
                results = [
                    result
                    if isinstance(result, Status)
                    else Status("unknown", error="Probe failed")
                    for result in results
                ]
                infrastructure_failures = sum(
                    result.result in ("unknown", "unreachable") for result in results
                )
                healthy_round = (
                    len(batch) < 5 or infrastructure_failures / len(batch) <= 0.20
                )
                if not healthy_round:
                    logger.warning(
                        "Automatic blocking suspended for round: %d of %d probes unavailable",
                        infrastructure_failures,
                        len(batch),
                    )
                for target, result in zip(batch, results, strict=True):
                    await asyncio.to_thread(
                        record_scan,
                        database,
                        target,
                        result,
                        policy,
                        time(),
                        healthy_round,
                    )
        except SQLAlchemyError:
            logger.exception("Tunnel scanner database operation failed")
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=1)
