import sqlalchemy as sa
from alembic import op

revision = "0012_security_settings"
down_revision = "0011_node_port_ranges"


def upgrade():
    for column in (
        sa.Column(
            "auto_ban_enabled",
            sa.Boolean,
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "auto_ban_attempts", sa.Integer, nullable=False, server_default="3"
        ),
        sa.Column(
            "auto_ban_window", sa.Integer, nullable=False, server_default="600"
        ),
        sa.Column(
            "auto_ban_seconds", sa.Integer, nullable=False, server_default="86400"
        ),
        sa.Column(
            "auto_ban_skip_legit",
            sa.Boolean,
            nullable=False,
            server_default=sa.true(),
        ),
    ):
        op.add_column("moderation_settings", column)


def downgrade():
    for name in (
        "auto_ban_skip_legit",
        "auto_ban_seconds",
        "auto_ban_window",
        "auto_ban_attempts",
        "auto_ban_enabled",
    ):
        op.drop_column("moderation_settings", name)
