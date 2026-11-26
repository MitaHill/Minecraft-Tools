import sqlalchemy as sa
from alembic import op

revision = "0011_node_port_ranges"
down_revision = "0010_client_node_catalog"


def upgrade():
    op.create_table(
        "node_port_ranges",
        sa.Column(
            "node_id",
            sa.String,
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("pending", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("start_port", sa.Integer, nullable=False),
        sa.Column("end_port", sa.Integer, nullable=False),
        sa.CheckConstraint(
            "start_port >= 1024 AND end_port >= start_port AND end_port <= 65535",
            name="ck_node_port_ranges_bounds",
        ),
        sa.PrimaryKeyConstraint("node_id", "pending", "start_port"),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, port_start, port_end, pending_port_start, pending_port_end "
            "FROM nodes"
        )
    ).mappings().all()
    insert_range = sa.text(
        "INSERT INTO node_port_ranges (node_id, pending, start_port, end_port) "
        "VALUES (:node_id, :pending, :start_port, :end_port)"
    )
    for row in rows:
        connection.execute(
            insert_range,
            {
                "node_id": row["id"],
                "pending": False,
                "start_port": row["port_start"],
                "end_port": row["port_end"],
            },
        )
        if row["pending_port_start"] is not None:
            connection.execute(
                insert_range,
                {
                    "node_id": row["id"],
                    "pending": True,
                    "start_port": row["pending_port_start"],
                    "end_port": row["pending_port_end"],
                },
            )
    for column in ("pending_port_end", "pending_port_start", "port_end", "port_start"):
        op.drop_column("nodes", column)


def downgrade():
    for column in (
        sa.Column("port_start", sa.Integer),
        sa.Column("port_end", sa.Integer),
        sa.Column("pending_port_start", sa.Integer),
        sa.Column("pending_port_end", sa.Integer),
    ):
        op.add_column("nodes", column)
    connection = op.get_bind()
    spans = connection.execute(
        sa.text(
            "SELECT node_id, MIN(start_port) AS start_port, MAX(end_port) AS end_port "
            "FROM node_port_ranges WHERE pending = :pending GROUP BY node_id"
        ),
        {"pending": False},
    ).mappings().all()
    for row in spans:
        connection.execute(
            sa.text(
                "UPDATE nodes SET port_start = :start_port, port_end = :end_port "
                "WHERE id = :node_id"
            ),
            row,
        )
    pending_spans = connection.execute(
        sa.text(
            "SELECT node_id, MIN(start_port) AS start_port, MAX(end_port) AS end_port "
            "FROM node_port_ranges WHERE pending = :pending GROUP BY node_id"
        ),
        {"pending": True},
    ).mappings().all()
    for row in pending_spans:
        connection.execute(
            sa.text(
                "UPDATE nodes SET pending_port_start = :start_port, "
                "pending_port_end = :end_port WHERE id = :node_id"
            ),
            row,
        )
    op.drop_table("node_port_ranges")
