import sqlalchemy as sa
from alembic import op

revision = "0001_diagnostics"
down_revision = None


def upgrade() -> None:
    op.create_table(
        "diagnostics",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "received_at",
            sa.DateTime,
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column("os", sa.String, nullable=False),
        sa.Column("arch", sa.String, nullable=False),
        sa.Column("report", sa.JSON, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("diagnostics")
