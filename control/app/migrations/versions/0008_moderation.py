import sqlalchemy as sa
from alembic import op

revision = "0008_moderation"
down_revision = "0007_admin_audit"


def upgrade():
    for column in (
        sa.Column("deployed_ports", sa.JSON),
        sa.Column("pending_port_start", sa.Integer),
        sa.Column("pending_port_end", sa.Integer),
        sa.Column("range_revision", sa.Integer, nullable=False, server_default="0"),
        sa.Column("range_error", sa.String),
        sa.Column("blacklist_revision", sa.Integer),
        sa.Column("blacklist_error", sa.String),
    ):
        op.add_column("nodes", column)
    op.add_column("tunnels", sa.Column("client_ip", sa.String))
    op.add_column("blocks", sa.Column("client_ip", sa.String))
    op.add_column("rooms", sa.Column("listing_id", sa.String))
    op.add_column("rooms", sa.Column("publication_id", sa.String))
    op.execute("UPDATE rooms SET listing_id = lower(hex(randomblob(16)))")
    op.create_index("ix_rooms_listing_id", "rooms", ["listing_id"], unique=True)
    op.create_table(
        "moderation_settings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("lobby_enabled", sa.Boolean, nullable=False),
        sa.Column("blacklist_revision", sa.Integer, nullable=False),
    )
    op.execute("INSERT INTO moderation_settings VALUES (1, 1, 0)")
    op.create_table(
        "ip_blacklist",
        sa.Column("ip", sa.String, primary_key=True),
        sa.Column("reason", sa.String, nullable=False),
        sa.Column("actor", sa.String, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("expires_at", sa.Float),
        sa.Column("removed_at", sa.Float),
    )
    op.create_table(
        "room_removals",
        sa.Column("code", sa.String, primary_key=True),
        sa.Column("owner", sa.String, primary_key=True),
        sa.Column("publication_id", sa.String, primary_key=True),
        sa.Column("removed_at", sa.Float, nullable=False),
    )


def downgrade():
    op.drop_table("room_removals")
    op.drop_table("ip_blacklist")
    op.drop_table("moderation_settings")
    op.drop_index("ix_rooms_listing_id", table_name="rooms")
    for table, columns in {
        "rooms": ("publication_id", "listing_id"),
        "tunnels": ("client_ip",),
        "blocks": ("client_ip",),
        "nodes": (
            "blacklist_error",
            "blacklist_revision",
            "range_error",
            "range_revision",
            "pending_port_end",
            "pending_port_start",
            "deployed_ports",
        ),
    }.items():
        for column in columns:
            op.drop_column(table, column)
