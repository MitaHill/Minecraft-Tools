import sqlalchemy as sa
from alembic import op

revision = "0013_online_presence"
down_revision = "0012_security_settings"


def upgrade():
    op.add_column(
        "online_users",
        sa.Column("first_heartbeat", sa.Float, nullable=False, server_default="0"),
    )
    op.add_column(
        "online_users",
        sa.Column("lobby_heartbeat", sa.Float, nullable=False, server_default="0"),
    )
    op.add_column("online_users", sa.Column("channel", sa.String, nullable=True))
    op.add_column("online_users", sa.Column("os_version", sa.String, nullable=True))
    op.execute(
        "UPDATE online_users "
        "SET first_heartbeat = last_heartbeat, lobby_heartbeat = last_heartbeat"
    )


def downgrade():
    for name in ("os_version", "channel", "lobby_heartbeat", "first_heartbeat"):
        op.drop_column("online_users", name)
