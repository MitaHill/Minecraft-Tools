import sqlalchemy as sa
from alembic import op

revision = "0004_tunnel_commands"
down_revision = "0003_registry"


def upgrade() -> None:
    op.create_table(
        "blocks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tunnel_id", sa.String, sa.ForeignKey("tunnels.id"), nullable=False),
        sa.Column("node_id", sa.String, sa.ForeignKey("nodes.id"), nullable=False),
        sa.Column("port", sa.Integer, nullable=False),
        sa.Column("run_id", sa.String, nullable=False),
        sa.Column("proxy_name", sa.String, nullable=False),
        sa.Column("reason", sa.String, nullable=False),
        sa.Column("source", sa.String, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("removed_at", sa.Float),
    )
    op.create_index(
        "ix_blocks_active_port",
        "blocks",
        ["node_id", "port"],
        unique=True,
        sqlite_where=sa.text("active = 1"),
    )
    op.create_table(
        "node_commands",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("block_id", sa.Integer, sa.ForeignKey("blocks.id"), nullable=False),
        sa.Column("node_id", sa.String, sa.ForeignKey("nodes.id"), nullable=False),
        sa.Column("action", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("acked_at", sa.Float),
        sa.Column("next_attempt_at", sa.Float, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.String),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_node_commands_queue",
        "node_commands",
        ["node_id", "status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_table("node_commands")
    op.drop_table("blocks")
