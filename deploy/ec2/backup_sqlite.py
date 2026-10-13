"""Create a consistent, private SQLite backup using SQLite's backup API."""

import argparse
import os
import sqlite3
from contextlib import closing
from pathlib import Path


def backup_database(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"Database does not exist: {source}")
    # Never overwrite an existing backup, and create it privately from the start.
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with (
            closing(
                sqlite3.connect(
                    f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=20
                )
            ) as source_db,
            closing(sqlite3.connect(destination)) as backup_db,
        ):
            source_db.backup(backup_db)
    except sqlite3.Error:
        destination.unlink()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    backup_database(args.source, args.destination)
    print(f"Database backup saved: {args.destination}")


if __name__ == "__main__":
    main()
