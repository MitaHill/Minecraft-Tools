from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    UniqueConstraint,
)

from .database import metadata

nodes = Table(
    "nodes",
    metadata,
    Column("id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("public_host", String, nullable=False),
    Column("port_start", Integer, nullable=False),
    Column("port_end", Integer, nullable=False),
    Column("agent_version", String),
    Column("frps_version", String),
    Column("status", String, nullable=False, server_default="offline"),
    Column("last_seen", Float),
    Column("token_hash", String, nullable=False),
    Column("deployed_ports", JSON(none_as_null=True)),
    Column("pending_port_start", Integer),
    Column("pending_port_end", Integer),
    Column("range_revision", Integer, nullable=False, server_default="0"),
    Column("range_error", String),
    Column("blacklist_revision", Integer),
    Column("blacklist_error", String),
)

tunnels = Table(
    "tunnels",
    metadata,
    Column("id", String, primary_key=True),
    Column("node_id", String, ForeignKey("nodes.id"), nullable=False),
    Column("run_id", String, nullable=False),
    Column("proxy_name", String, nullable=False),
    Column("remote_port", Integer, nullable=False),
    Column("remote_addr", String),
    Column("client_ip", String),
    Column("local_ip", String),
    Column("local_port", Integer),
    Column("install_id", String),
    Column("proxy_present", Boolean, nullable=False, server_default="1"),
    Column("status", String, nullable=False, server_default="pending"),
    Column("scan_failures", Integer, nullable=False, server_default="0"),
    Column("last_scan_at", Float),
    Column("created_at", Float, nullable=False, server_default="0"),
    Column("next_scan_at", Float, nullable=False, server_default="0"),
    Column("last_reachable_at", Float),
    Column("blocked_at", Float),
    Column("block_reason", String),
    UniqueConstraint("node_id", "proxy_name"),
)

blocks = Table(
    "blocks",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("tunnel_id", String, ForeignKey("tunnels.id"), nullable=False),
    Column("node_id", String, ForeignKey("nodes.id"), nullable=False),
    Column("port", Integer, nullable=False),
    Column("run_id", String, nullable=False),
    Column("proxy_name", String, nullable=False),
    Column("reason", String, nullable=False),
    Column("source", String, nullable=False),
    Column("client_ip", String),
    Column("active", Boolean, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("removed_at", Float),
)

installs = Table(
    "installs",
    metadata,
    Column("id", String, primary_key=True),
    Column("token_hash", String, nullable=False, unique=True),
    Column("created_at", Float, nullable=False),
    Column("last_seen", Float, nullable=False),
)

tunnel_claims = Table(
    "tunnel_claims",
    metadata,
    Column("id", String, primary_key=True),
    Column("install_id", String, ForeignKey("installs.id"), nullable=False),
    Column("node_id", String, ForeignKey("nodes.id"), nullable=False),
    Column("remote_port", Integer, nullable=False),
    Column("proof_hash", String, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("bound_at", Float),
    UniqueConstraint("node_id", "remote_port"),
)
