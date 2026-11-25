import sqlalchemy as sa
from alembic import op

revision = "0002_lobby"
down_revision = "0001_diagnostics"


def upgrade() -> None:
    op.create_table(
        "rooms",
        sa.Column("full_room_code", sa.String, primary_key=True),
        sa.Column("remote_port", sa.Integer, nullable=False),
        sa.Column("node_id", sa.Integer, nullable=False),
        sa.Column("room_name", sa.String, nullable=False),
        sa.Column("game_version", sa.String, nullable=True),
        sa.Column("player_count", sa.Integer, nullable=False),
        sa.Column("max_players", sa.Integer, nullable=False),
        sa.Column("description", sa.String, nullable=True),
        sa.Column("is_public", sa.Boolean, nullable=False),
        sa.Column("host_player", sa.String, nullable=False),
        sa.Column("server_addr", sa.String, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.Column("client_ip", sa.String, nullable=False, unique=True),
        sa.Column("tunnel_id", sa.String, nullable=True),
    )
    op.create_index("ix_rooms_updated_at", "rooms", ["updated_at"])
    op.create_table(
        "online_users",
        sa.Column("client_ip", sa.String, primary_key=True),
        sa.Column("last_heartbeat", sa.Float, nullable=False),
    )
    op.create_index(
        "ix_online_users_last_heartbeat", "online_users", ["last_heartbeat"]
    )


def downgrade() -> None:
    op.drop_table("online_users")
    op.drop_table("rooms")
