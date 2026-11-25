import argparse
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import mkstemp


def backup(source: Path, destination: Path) -> None:
    source = source.resolve(strict=True)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, name = mkstemp(prefix=".control-backup-", dir=destination.parent)
    os.close(handle)
    temporary = Path(name)
    try:
        with (
            closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as reading,
            closing(sqlite3.connect(temporary)) as writing,
        ):
            reading.backup(writing, pages=128)
            if writing.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise RuntimeError("SQLite backup failed integrity_check")
            writing.execute("PRAGMA journal_mode=DELETE")
        with temporary.open("rb") as snapshot:
            os.fsync(snapshot.fileno())
        # Publish the verified file without ever replacing an existing backup.
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Copy a live Control Server SQLite database"
    )
    parser.add_argument("destination", type=Path)
    data_dir = Path(os.environ.get("CONTROL_DATA_DIR", "/data"))
    parser.add_argument("--source", default=data_dir / "control.db", type=Path)
    args = parser.parse_args()
    backup(args.source, args.destination)
