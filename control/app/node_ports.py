import re
from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    String,
    Table,
    delete,
    insert,
    select,
    update,
)

from .database import metadata
from .registry import nodes

MIN_PORT = 1024
MAX_PORT = 65535
MAX_RANGES = 128
MAX_TEXT = 1024
PORT_TOKEN = re.compile(r"^(\d{1,5})(?:-(\d{1,5}))?$")

Range = tuple[int, int]

node_port_ranges = Table(
    "node_port_ranges",
    metadata,
    Column(
        "node_id",
        String,
        ForeignKey(nodes.c.id, ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("pending", Boolean, primary_key=True, nullable=False, server_default="0"),
    Column("start_port", Integer, primary_key=True),
    Column("end_port", Integer, nullable=False),
    CheckConstraint(
        "start_port >= 1024 AND end_port >= start_port AND end_port <= 65535",
        name="ck_node_port_ranges_bounds",
    ),
)


def merge(ranges: Iterable[Range]) -> tuple[Range, ...]:
    merged: list[Range] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def parse_spec(text: str) -> tuple[Range, ...]:
    if len(text) > MAX_TEXT:
        raise ValueError("端口配置过长")
    tokens = [token.strip() for token in text.split(",")]
    if not text.strip() or any(not token for token in tokens):
        raise ValueError("请输入允许端口")
    ranges = []
    for token in tokens:
        matched = PORT_TOKEN.fullmatch(token)
        if matched is None:
            raise ValueError("端口格式应为 30000-30010,31000")
        start = int(matched.group(1))
        end = int(matched.group(2)) if matched.group(2) else start
        if not MIN_PORT <= start <= MAX_PORT or not MIN_PORT <= end <= MAX_PORT:
            raise ValueError("端口需在 1024–65535 之间")
        if end < start:
            raise ValueError("端口范围起止顺序错误")
        ranges.append((start, end))
    merged = merge(ranges)
    if len(merged) > MAX_RANGES:
        raise ValueError("最多配置 128 段端口")
    return merged


def format_spec(ranges: Iterable[Range]) -> str:
    return ",".join(
        f"{start}-{end}" if start != end else str(start) for start, end in ranges
    )


def contains(ranges: Iterable[Range], port: int) -> bool:
    return any(start <= port <= end for start, end in ranges)


def within(configured: Iterable[Range], deployed: Iterable[Range]) -> bool:
    allowed = merge(deployed)
    return all(
        any(low <= start and end <= high for low, high in allowed)
        for start, end in configured
    )


def load_ranges(connection, node_id: str, *, pending: bool = False) -> tuple[Range, ...]:
    rows = connection.execute(
        select(node_port_ranges.c.start_port, node_port_ranges.c.end_port)
        .where(
            node_port_ranges.c.node_id == node_id,
            node_port_ranges.c.pending.is_(pending),
        )
        .order_by(node_port_ranges.c.start_port)
    )
    return tuple((row.start_port, row.end_port) for row in rows)


def requested_ranges(connection, node_id: str) -> tuple[Range, ...]:
    return load_ranges(connection, node_id, pending=True) or load_ranges(connection, node_id)


def has_pending(connection, node_id: str) -> bool:
    return bool(load_ranges(connection, node_id, pending=True))


def ranges_by_node(connection, *, pending: bool = False) -> dict[str, tuple[Range, ...]]:
    grouped: dict[str, list[Range]] = defaultdict(list)
    rows = connection.execute(
        select(node_port_ranges.c.node_id, node_port_ranges.c.start_port,
               node_port_ranges.c.end_port)
        .where(node_port_ranges.c.pending.is_(pending))
        .order_by(node_port_ranges.c.node_id, node_port_ranges.c.start_port)
    )
    for row in rows:
        grouped[row.node_id].append((row.start_port, row.end_port))
    return {node_id: tuple(items) for node_id, items in grouped.items()}


def replace_ranges(connection, node_id: str, ranges: Iterable[Range], *, pending=False):
    connection.execute(
        delete(node_port_ranges).where(
            node_port_ranges.c.node_id == node_id,
            node_port_ranges.c.pending.is_(pending),
        )
    )
    values = [
        {"node_id": node_id, "pending": pending, "start_port": start, "end_port": end}
        for start, end in ranges
    ]
    if values:
        connection.execute(insert(node_port_ranges), values)


def clear_pending(connection, node_id: str) -> None:
    replace_ranges(connection, node_id, (), pending=True)


def apply_pending(connection, node_id: str) -> bool:
    if not has_pending(connection, node_id):
        return False
    connection.execute(
        delete(node_port_ranges).where(
            node_port_ranges.c.node_id == node_id,
            node_port_ranges.c.pending.is_(False),
        )
    )
    connection.execute(
        update(node_port_ranges)
        .where(
            node_port_ranges.c.node_id == node_id,
            node_port_ranges.c.pending.is_(True),
        )
        .values(pending=False)
    )
    return True
