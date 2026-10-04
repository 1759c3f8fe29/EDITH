"""Migration runner (task B1).

Spec §103 requires that a version change preserves user data, migrates safely,
detects failure, and avoids silent corruption. So:

1. ``PRAGMA integrity_check`` before touching anything.
2. A consistent backup via the sqlite3 online backup API -- not a file copy,
   which can capture a torn WAL.
3. Each migration inside one transaction; a failure rolls back and the original
   database is left untouched, with the backup path reported.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..logging_setup import get_logger
from . import conn as db
from .schema import LATEST_VERSION, MIGRATIONS, Migration

log = get_logger("db.migrate")


class MigrationError(RuntimeError):
    def __init__(self, message: str, backup: Path | None = None) -> None:
        super().__init__(message)
        self.backup = backup


def _ensure_version_table(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def current_version(c: sqlite3.Connection | None = None) -> int:
    c = c or db.conn()
    _ensure_version_table(c)
    row = c.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"]) if row and row["v"] is not None else 0


def _backup(c: sqlite3.Connection, version: int) -> Path:
    """Consistent snapshot using the online backup API."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = db.path().with_suffix(f".backup-v{version}-{stamp}.db")
    dest = sqlite3.connect(target)
    try:
        c.backup(dest)
    finally:
        dest.close()
    log.info("database backed up", extra={"backup": str(target), "from_version": version})
    return target


def pending() -> list[Migration]:
    at = current_version()
    return [m for m in MIGRATIONS if m.version > at]


def migrate() -> dict[str, object]:
    """Apply all pending migrations. Returns a report for /health and logs."""
    c = db.conn()
    _ensure_version_table(c)
    start = current_version(c)

    todo = [m for m in MIGRATIONS if m.version > start]
    if not todo:
        return {"from": start, "to": start, "applied": [], "backup": None}

    ok, detail = db.integrity_check()
    if not ok:
        raise MigrationError(f"refusing to migrate a corrupt database: {detail}")

    # Nothing to preserve on a first-run database, so skip the backup there.
    backup: Path | None = _backup(c, start) if start > 0 else None

    applied: list[int] = []
    for migration in todo:
        log.info(
            "applying migration",
            extra={"version": migration.version, "name": migration.name},
        )
        c.execute("BEGIN IMMEDIATE")
        try:
            for statement in migration.statements:
                c.execute(statement)
            c.execute(
                "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            c.execute("COMMIT")
        except Exception as exc:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            log.error(
                "migration failed and was rolled back",
                extra={"version": migration.version, "backup": str(backup) if backup else None},
                exc_info=True,
            )
            raise MigrationError(
                f"migration v{migration.version} ({migration.name}) failed: {exc}",
                backup=backup,
            ) from exc
        applied.append(migration.version)

    end = current_version(c)
    log.info("migrations complete", extra={"from_version": start, "to_version": end})
    return {
        "from": start,
        "to": end,
        "applied": applied,
        "backup": str(backup) if backup else None,
    }


def status() -> dict[str, object]:
    at = current_version()
    return {
        "current": at,
        "latest": LATEST_VERSION,
        "up_to_date": at >= LATEST_VERSION,
        "pending": [m.version for m in MIGRATIONS if m.version > at],
    }
