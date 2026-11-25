from pathlib import Path
from time import time

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    JSON,
    URL,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    event,
    func,
)
from sqlalchemy.engine import Connection, Engine

metadata = MetaData()
diagnostics = Table(
    "diagnostics",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "received_at", DateTime, server_default=func.current_timestamp(), nullable=False
    ),
    Column("os", String, nullable=False),
    Column("arch", String, nullable=False),
    Column("report", JSON, nullable=False),
)
audit_logs = Table(
    "audit_logs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("created_at", Float, nullable=False),
    Column("actor", String, nullable=False),
    Column("action", String, nullable=False),
    Column("target", String, nullable=False),
)


def record_admin_action(
    connection: Connection, actor: str, action: str, target: str
) -> None:
    connection.execute(
        audit_logs.insert().values(
            created_at=time(),
            actor=actor,
            action=action,
            target=target,
        )
    )


def open_database(data_dir: Path) -> Engine:
    data_dir.mkdir(parents=True, exist_ok=True)
    url = URL.create("sqlite", database=str(data_dir / "control.db"))
    engine = create_engine(url, connect_args={"autocommit": False})

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _record):
        # SQLite requires these PRAGMAs outside a transaction.
        connection.autocommit = True
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()
            connection.autocommit = False

    migration = Config()
    migration.set_main_option("script_location", "app:migrations")
    try:
        with engine.begin() as connection:
            migration.attributes["connection"] = connection
            command.upgrade(migration, "head")
    except Exception:
        engine.dispose()
        raise
    return engine
