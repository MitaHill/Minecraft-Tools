import sqlalchemy as sa
from alembic import op

revision = "0007_admin_audit"
down_revision = "0006_install_claims"


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("actor", sa.String, nullable=False),
        sa.Column("action", sa.String, nullable=False),
        sa.Column("target", sa.String, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("audit_logs")
