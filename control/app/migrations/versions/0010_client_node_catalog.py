from time import time_ns

import sqlalchemy as sa
from alembic import op

revision = "0010_client_node_catalog"
down_revision = "0009_cidr_blacklist"


def upgrade():
    for column in (
        sa.Column("server_addr", sa.String),
        sa.Column("server_port", sa.Integer),
        sa.Column("frp_token", sa.String),
        sa.Column("client_channel", sa.String, nullable=False, server_default="hidden"),
    ):
        op.add_column("nodes", column)
    op.add_column(
        "moderation_settings",
        sa.Column("catalog_updated_at", sa.Integer, nullable=False, server_default="0"),
    )
    op.execute(
        sa.text("UPDATE moderation_settings SET catalog_updated_at = :timestamp").bindparams(
            timestamp=time_ns() // 1_000_000
        )
    )


def downgrade():
    op.drop_column("moderation_settings", "catalog_updated_at")
    for column in ("client_channel", "frp_token", "server_port", "server_addr"):
        op.drop_column("nodes", column)
