"""Consistent SQLite backup and restore commands for a stopped/running PoC."""

from __future__ import annotations

import argparse
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from .config import Settings


def sqlite_backup(source: Path, destination: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(f"Database does not exist: {source}")
    if source.resolve() == destination.resolve():
        raise ValueError("Backup destination must be different from the live database")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # sqlite3.Connection's transaction context manager does not close the
    # connection, so use ``closing`` explicitly around every short-lived
    # backup handle.
    with (
        closing(sqlite3.connect(source)) as source_conn,
        closing(sqlite3.connect(destination)) as destination_conn,
    ):
        source_conn.backup(destination_conn)
    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass


def restore_backup(source: Path, destination: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(f"Backup does not exist: {source}")
    if source.resolve() == destination.resolve():
        raise ValueError("Restore source must be different from the live database")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as check:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise ValueError("Backup integrity check failed")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".restore-tmp")
    try:
        sqlite_backup(source, temporary)
        # The documented restore procedure stops the service first, making the
        # atomic replacement safe with SQLite's WAL sidecar files.
        os.replace(temporary, destination)
        try:
            os.chmod(destination, 0o600)
        except OSError:
            pass
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description="Back up or restore PYKASU's SQLite database")
    parser.add_argument("command", choices=("backup", "restore"))
    parser.add_argument("path", type=Path, help="Absolute or relative SQLite backup path")
    args = parser.parse_args()
    database_path = Settings.from_env().database_path
    target = args.path.expanduser().resolve()
    if args.command == "backup":
        sqlite_backup(database_path, target)
        print(f"Backup written to {target}")
    else:
        restore_backup(target, database_path)
        print(f"Database restored from {target}")


if __name__ == "__main__":
    main()
