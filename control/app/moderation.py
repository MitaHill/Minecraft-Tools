from ipaddress import IPv6Address, ip_address
from time import time

from sqlalchemy import Boolean, Column, Float, Integer, String, Table, or_, select, update

from .database import metadata
from .registry import nodes, tunnels

settings = Table(
    "moderation_settings",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("lobby_enabled", Boolean, nullable=False),
    Column("blacklist_revision", Integer, nullable=False),
)
blacklist = Table(
    "ip_blacklist",
    metadata,
    Column("ip", String, primary_key=True),
    Column("reason", String, nullable=False),
    Column("actor", String, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float),
    Column("removed_at", Float),
)
removals = Table(
    "room_removals",
    metadata,
    Column("code", String, primary_key=True),
    Column("owner", String, primary_key=True),
    Column("publication_id", String, primary_key=True),
    Column("removed_at", Float, nullable=False),
)


def canonical_ip(value: str | None) -> str | None:
    if not value or "%" in value:
        return None
    try:
        address = ip_address(value)
    except ValueError:
        return None
    if isinstance(address, IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address)


def active_blacklist(now):
    return blacklist.c.removed_at.is_(None) & or_(
        blacklist.c.expires_at.is_(None), blacklist.c.expires_at > now
    )


def ip_block(connection, *ips):
    addresses = {address for value in ips if (address := canonical_ip(value))}
    if not addresses:
        return None
    return connection.execute(
        select(blacklist.c.reason)
        .where(active_blacklist(time()), blacklist.c.ip.in_(addresses))
        .order_by(blacklist.c.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def live_tunnels(now):
    return (
        (nodes.c.status == "online")
        & tunnels.c.proxy_present.is_(True)
        & tunnels.c.status.in_(("pending", "active"))
        & ~select(blacklist.c.ip)
        .where(blacklist.c.ip == tunnels.c.client_ip, active_blacklist(now))
        .exists()
    )


def lock_settings(connection):
    connection.execute(update(settings).values(lobby_enabled=settings.c.lobby_enabled))


def node_policy(database, node_id):
    with database.connect() as connection:
        node = connection.execute(select(nodes).where(nodes.c.id == node_id)).mappings().one()
        revision = connection.execute(select(settings.c.blacklist_revision)).scalar_one()
        payload = {}
        if node["blacklist_revision"] != revision:
            entries = connection.execute(
                select(blacklist.c.ip, blacklist.c.reason, blacklist.c.expires_at)
                .where(active_blacklist(time()))
                .order_by(blacklist.c.ip)
            ).mappings()
            payload["blacklist"] = {"revision": revision, "entries": list(map(dict, entries))}
        payload["range"] = {
            "revision": node["range_revision"],
            "start": node["pending_port_start"] or node["port_start"],
            "end": node["pending_port_end"] or node["port_end"],
        }
        return payload


def acknowledge_policy(database, node_id, kind, revision, error):
    with database.begin() as connection:
        if kind == "blacklist":
            current = select(settings.c.blacklist_revision).scalar_subquery()
            connection.execute(
                update(nodes)
                .where(nodes.c.id == node_id, current == revision)
                .values(
                    blacklist_revision=revision if not error else None,
                    blacklist_error=error or None,
                )
            )
            return
        values = {"range_error": error or None}
        if not error:
            values.update(port_start=nodes.c.pending_port_start, port_end=nodes.c.pending_port_end)
        else:
            # A rejected request keeps its revision. Agents ignore older successful ACKs.
            values["range_revision"] = nodes.c.range_revision + 1
        match = (nodes.c.id == node_id) & (nodes.c.range_revision == revision)
        changed = connection.execute(
            update(nodes)
            .where(match, nodes.c.pending_port_start.is_not(None))
            .values(pending_port_start=None, pending_port_end=None, **values)
        )
        if error and not changed.rowcount:
            connection.execute(update(nodes).where(match).values(range_error=error or None))
