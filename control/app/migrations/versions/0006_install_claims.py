import sqlalchemy as sa
from alembic import op

revision = "0006_install_claims"
down_revision = "0005_scanner"


def upgrade() -> None:
    op.add_column(
        "tunnels",
        sa.Column("proxy_present", sa.Boolean, nullable=False, server_default="1"),
    )
    op.execute("UPDATE tunnels SET proxy_present = 0 WHERE status = 'closed'")
    op.create_table(
        "installs",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("token_hash", sa.String, nullable=False, unique=True),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("last_seen", sa.Float, nullable=False),
    )
    op.create_table(
        "tunnel_claims",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column(
            "install_id", sa.String, sa.ForeignKey("installs.id"), nullable=False
        ),
        sa.Column("node_id", sa.String, sa.ForeignKey("nodes.id"), nullable=False),
        sa.Column("remote_port", sa.Integer, nullable=False),
        sa.Column("proof_hash", sa.String, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
        sa.Column("bound_at", sa.Float),
        sa.UniqueConstraint("node_id", "remote_port"),
    )
    op.create_index("ix_tunnel_claims_expires_at", "tunnel_claims", ["expires_at"])
    op.execute("UPDATE tunnels SET install_id = NULL WHERE install_id IS NOT NULL")


def downgrade() -> None:
    op.execute("UPDATE tunnels SET install_id = NULL WHERE install_id IS NOT NULL")
    op.drop_table("tunnel_claims")
    op.drop_table("installs")
    op.drop_column("tunnels", "proxy_present")
