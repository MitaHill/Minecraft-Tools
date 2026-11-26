from time import time

import sqlalchemy as sa
from alembic import op

revision = "0009_cidr_blacklist"
down_revision = "0008_moderation"


def upgrade():
    op.add_column(
        "nodes",
        sa.Column("blacklist_cidr", sa.Boolean, nullable=False, server_default=sa.false()),
    )


def downgrade():
    active_cidr = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM ip_blacklist WHERE instr(ip, '/') > 0 AND removed_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > :now) LIMIT 1"
            ),
            {"now": time()},
        )
        .scalar()
    )
    if active_cidr:
        raise RuntimeError(
            "Remove active CIDR blacklist rules and wait for node sync before rollback"
        )
    op.drop_column("nodes", "blacklist_cidr")
