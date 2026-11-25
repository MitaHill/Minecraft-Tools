import sqlalchemy as sa
from alembic import op

revision = "0005_scanner"
down_revision = "0004_tunnel_commands"


def upgrade() -> None:
    op.add_column(
        "tunnels", sa.Column("created_at", sa.Float, nullable=False, server_default="0")
    )
    op.add_column(
        "tunnels",
        sa.Column("next_scan_at", sa.Float, nullable=False, server_default="0"),
    )
    op.add_column("tunnels", sa.Column("last_reachable_at", sa.Float))
    op.execute(
        "UPDATE tunnels SET created_at = strftime('%s', 'now') WHERE created_at = 0"
    )
    op.create_index("ix_tunnels_next_scan_at", "tunnels", ["next_scan_at"])
    op.create_table(
        "tunnel_scans",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tunnel_id", sa.String, sa.ForeignKey("tunnels.id"), nullable=False),
        sa.Column("checked_at", sa.Float, nullable=False),
        sa.Column("result", sa.String, nullable=False),
        sa.Column("latency_ms", sa.Integer),
        sa.Column("version", sa.String),
        sa.Column("motd", sa.String),
        sa.Column("error", sa.String),
    )
    op.create_index("ix_tunnel_scans_tunnel_id", "tunnel_scans", ["tunnel_id"])


def downgrade() -> None:
    op.drop_table("tunnel_scans")
    op.drop_index("ix_tunnels_next_scan_at", table_name="tunnels")
    op.drop_column("tunnels", "last_reachable_at")
    op.drop_column("tunnels", "next_scan_at")
    op.drop_column("tunnels", "created_at")
