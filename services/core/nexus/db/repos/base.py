"""Shared repository helpers (task B3).

Route handlers never see SQL (§111); they call repositories. These helpers keep
the ``updated_at`` / ``rev`` / tombstone conventions in one place so no
repository can forget them and quietly break Phase K sync.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .. import conn as db


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> str:
    """ISO-8601 UTC. Stored as TEXT so SQLite ordering matches chronology."""
    return datetime.now(timezone.utc).isoformat()


def dumps(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def loads(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        # A malformed JSON column must not take down a whole list view (§105
        # "malformed memory"). Callers get the default and the row stays usable.
        return default


def to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def to_list(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def tombstone(c: sqlite3.Connection, entity: str, entity_id: str, rev: int) -> None:
    """Record that a row was hard-deleted.

    §70 requires the data itself to be gone; this keeps only the identity so a
    remote replica can be told to delete it too.
    """
    c.execute(
        """
        INSERT INTO tombstones (entity, entity_id, deleted_at, rev)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(entity, entity_id) DO UPDATE SET
            deleted_at = excluded.deleted_at,
            rev        = excluded.rev
        """,
        (entity, entity_id, now(), rev),
    )


def build_update(
    table: str,
    entity_id: str,
    fields: Mapping[str, Any],
    *,
    id_column: str = "id",
) -> tuple[str, tuple[Any, ...]] | None:
    """Compose a partial UPDATE that also bumps updated_at.

    Returns None when there is nothing to change, so callers can skip a pointless
    write (and a pointless rev increment).
    """
    usable = {k: v for k, v in fields.items() if v is not None}
    if not usable:
        return None
    assignments = ", ".join(f"{key} = ?" for key in usable)
    sql = (
        f"UPDATE {table} SET {assignments}, updated_at = ?, rev = ? "
        f"WHERE {id_column} = ?"
    )
    return sql, (*usable.values(), entity_id)


def fts_escape(term: str) -> str:
    """Quote a user query for FTS5 MATCH.

    FTS5 treats bare punctuation as syntax, so an unescaped apostrophe or hyphen
    from the search box raises instead of returning results. Each token is
    quoted and given a prefix wildcard for incremental search.
    """
    tokens = [t for t in "".join(ch if ch.isalnum() else " " for ch in term).split() if t]
    if not tokens:
        return ""
    return " ".join(f'"{t}"*' for t in tokens)
