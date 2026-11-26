from ipaddress import ip_address
from time import time, time_ns

from sqlalchemy import Boolean, Column, Float, Integer, String, Table, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .database import metadata, record_admin_action
from .node_ports import apply_pending, clear_pending, has_pending, requested_ranges
from .registry import blocks, nodes, tunnels

settings = Table(
    "moderation_settings",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("lobby_enabled", Boolean, nullable=False),
    Column("blacklist_revision", Integer, nullable=False),
    Column("catalog_updated_at", Integer, nullable=False),
    Column("auto_ban_enabled", Boolean, nullable=False, server_default="1"),
    Column("auto_ban_attempts", Integer, nullable=False, server_default="3"),
    Column("auto_ban_window", Integer, nullable=False, server_default="600"),
    Column("auto_ban_seconds", Integer, nullable=False, server_default="86400"),
    Column("auto_ban_skip_legit", Boolean, nullable=False, server_default="1"),
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


UNCLAIMED_BAN_REASON = "未授权隧道（无有效预约）"


def ban_unclaimed_flood(database, address, now):
    """Ban a public IP that keeps opening tunnels without a valid claim."""
    try:
        ip = ip_address(address)
    except (TypeError, ValueError):
        return False
    if not ip.is_global:
        return False
    with database.begin() as connection:
        lock_settings(connection)
        policy = connection.execute(
            select(
                settings.c.auto_ban_enabled,
                settings.c.auto_ban_attempts,
                settings.c.auto_ban_window,
                settings.c.auto_ban_seconds,
                settings.c.auto_ban_skip_legit,
            )
        ).mappings().one()
        if not policy["auto_ban_enabled"]:
            return False
        window = policy["auto_ban_window"]
        if connection.execute(
            select(blacklist.c.ip).where(
                blacklist.c.ip == address, active_blacklist(now)
            )
        ).first():
            return False
        attempts = connection.execute(
            select(func.count(blocks.c.id)).where(
                blocks.c.client_ip == address,
                blocks.c.source == "unauthorized",
                blocks.c.created_at >= now - window,
            )
        ).scalar_one()
        if attempts < policy["auto_ban_attempts"]:
            return False
        if policy["auto_ban_skip_legit"]:
            legitimate = connection.execute(
                select(tunnels.c.id).where(
                    tunnels.c.client_ip == address,
                    tunnels.c.install_id.is_not(None),
                    tunnels.c.status != "closed",
                    tunnels.c.created_at >= now - window,
                ).limit(1)
            ).first()
            if legitimate is not None:
                return False
        values = {
            "reason": UNCLAIMED_BAN_REASON,
            "actor": "system:unauthorized",
            "created_at": now,
            "expires_at": now + policy["auto_ban_seconds"],
            "removed_at": None,
        }
        connection.execute(
            sqlite_insert(blacklist).values(ip=address, **values)
            .on_conflict_do_update(index_elements=[blacklist.c.ip], set_=values)
        )
        connection.execute(
            update(settings).values(
                blacklist_revision=settings.c.blacklist_revision + 1
            )
        )
        record_admin_action(
            connection, "system:unauthorized", "blacklist.auto", address
        )
    return True


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
        ranges = requested_ranges(connection, node_id)
        if ranges:
            payload["range"] = {
                "revision": node["range_revision"],
                "start": ranges[0][0],
                "end": ranges[-1][1],
                "ranges": [list(item) for item in ranges],
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
        pending = has_pending(connection, node_id)
        match = (nodes.c.id == node_id) & (nodes.c.range_revision == revision)
        if not error:
            applied = connection.execute(
                update(nodes).where(match).values(range_error=None)
            ).rowcount
            if applied and pending:
                apply_pending(connection, node_id)
            return
        if pending:
            # A rejected request keeps its revision. Agents ignore older successful ACKs.
            rejected = connection.execute(
                update(nodes)
                .where(match)
                .values(
                    range_revision=nodes.c.range_revision + 1, range_error=error
                )
            ).rowcount
            if rejected:
                clear_pending(connection, node_id)
                return
        connection.execute(update(nodes).where(match).values(range_error=error))
