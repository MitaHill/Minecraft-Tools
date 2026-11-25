import sqlalchemy as sa
from alembic import op

revision = "0003_registry"
down_revision = "0002_lobby"


def upgrade() -> None:
    op.create_table(
        "nodes",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("public_host", sa.String, nullable=False),
        sa.Column("port_start", sa.Integer, nullable=False),
        sa.Column("port_end", sa.Integer, nullable=False),
        sa.Column("agent_version", sa.String),
        sa.Column("frps_version", sa.String),
        sa.Column("status", sa.String, nullable=False, server_default="offline"),
        sa.Column("last_seen", sa.Float),
        sa.Column("token_hash", sa.String, nullable=False),
    )
    op.create_table(
        "tunnels",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("node_id", sa.String, sa.ForeignKey("nodes.id"), nullable=False),
        sa.Column("run_id", sa.String, nullable=False),
        sa.Column("proxy_name", sa.String, nullable=False),
        sa.Column("remote_port", sa.Integer, nullable=False),
        sa.Column("remote_addr", sa.String),
        sa.Column("local_ip", sa.String),
        sa.Column("local_port", sa.Integer),
        sa.Column("install_id", sa.String),
        sa.Column("status", sa.String, nullable=False, server_default="pending"),
        sa.Column("scan_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_scan_at", sa.Float),
        sa.Column("blocked_at", sa.Float),
        sa.Column("block_reason", sa.String),
        sa.UniqueConstraint("node_id", "proxy_name"),
    )
    op.create_table(
        "admin_sessions",
        sa.Column("token_hash", sa.String, primary_key=True),
        sa.Column("username", sa.String, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("admin_sessions")
    op.drop_table("tunnels")
    op.drop_table("nodes")
