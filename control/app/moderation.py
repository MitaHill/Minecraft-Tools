from time import time, time_ns

from sqlalchemy import Boolean, Column, Float, Integer, String, Table, func, or_, select, update

from .database import metadata
from .registry import nodes, tunnels

settings = Table(
    "moderation_settings",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("lobby_enabled", Boolean, nullable=False),
    Column("blacklist_revision", Integer, nullable=False),
    Column("catalog_updated_at", Integer, nullable=False),
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


def active_blacklist(now):
    return blacklist.c.removed_at.is_(None) & or_(
        blacklist.c.expires_at.is_(None), blacklist.c.expires_at > now
    )


def active_cidr(now):
    return active_blacklist(now) & blacklist.c.ip.contains("/")


def ip_matches(rule, address):
    return func.ip_rule_prefix(rule, address) >= 0


def ip_block(connection, *ips):
    addresses = set(filter(None, ips))
    if not addresses:
        return None
    scores = [func.ip_rule_prefix(blacklist.c.ip, address) for address in sorted(addresses)]
    specificity = scores[0] if len(scores) == 1 else func.max(*scores)
    return connection.execute(
        select(blacklist.c.reason)
        .where(active_blacklist(time()), specificity >= 0)
        .order_by(specificity.desc(), blacklist.c.created_at.desc(), blacklist.c.ip)
        .limit(1)
    ).scalar_one_or_none()


def live_tunnels(now):
    return (
        (nodes.c.status == "online")
        & tunnels.c.proxy_present.is_(True)
        & tunnels.c.status.in_(("pending", "active"))
        & ~select(blacklist.c.ip)
        .where(ip_matches(blacklist.c.ip, tunnels.c.client_ip), active_blacklist(now))
        .exists()
    )


def lock_settings(connection):
    connection.execute(update(settings).values(lobby_enabled=settings.c.lobby_enabled))


def touch_node_catalog(connection):
    connection.execute(
        update(settings).values(
            catalog_updated_at=func.max(time_ns() // 1_000_000, settings.c.catalog_updated_at + 1)
        )
    )


def node_policy(database, node_id):
    with database.connect() as connection:
        node = connection.execute(select(nodes).where(nodes.c.id == node_id)).mappings().one()
        revision = connection.execute(select(settings.c.blacklist_revision)).scalar_one()
        payload = {}
        if node["blacklist_revision"] != revision:
            rules = active_blacklist(time())
            if not node["blacklist_cidr"]:
                rules &= ~blacklist.c.ip.contains("/")
            entries = connection.execute(
                select(blacklist.c.ip, blacklist.c.reason, blacklist.c.expires_at)
                .where(rules)
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
