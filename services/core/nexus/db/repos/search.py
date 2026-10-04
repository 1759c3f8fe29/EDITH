"""Unified search across conversations, messages, memories, tasks and activity
(task B7; spec §83, §45 command palette).

One FTS5 table backs all entity types. That keeps ranking comparable across
kinds and means a new searchable entity needs a trigger, not a new query path.

Results always carry ``entity`` and a real ``snippet`` from the indexed text --
never a re-fetched summary, so what the user sees is what actually matched.
"""

from __future__ import annotations

from typing import Any

from .. import conn as db
from .base import fts_escape, to_list

ENTITIES: tuple[str, ...] = ("conversation", "message", "memory", "task", "activity")


def search(
    term: str,
    *,
    entities: tuple[str, ...] | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    match = fts_escape(term)
    if not match:
        return []
    wanted = tuple(e for e in (entities or ENTITIES) if e in ENTITIES)
    if not wanted:
        return []

    params: list[Any] = [match]
    clause = ""
    if set(wanted) != set(ENTITIES):
        clause = f"AND entity IN ({','.join('?' * len(wanted))})"
        params.extend(wanted)
    params.append(min(limit, 100))

    rows = to_list(
        db.query(
            f"""
            SELECT entity,
                   entity_id,
                   title,
                   snippet(search_index, 3, '<<', '>>', ' ... ', 12) AS snippet,
                   bm25(search_index) AS score
            FROM search_index
            WHERE search_index MATCH ?
              {clause}
            ORDER BY score
            LIMIT ?
            """,
            tuple(params),
        )
    )
    for row in rows:
        # bm25 returns a negative score (better = more negative); flip it so a
        # larger number means a better match when it reaches the UI.
        row["score"] = round(-float(row["score"]), 4)
    return rows


def resolve(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach the navigable context each result needs to be clickable.

    A message hit is only useful if the UI knows which conversation to open, so
    that lookup happens here rather than leaving the frontend to guess.
    """
    by_entity: dict[str, list[str]] = {}
    for row in results:
        by_entity.setdefault(row["entity"], []).append(row["entity_id"])

    context: dict[tuple[str, str], dict[str, Any]] = {}

    if by_entity.get("message"):
        ids = by_entity["message"]
        placeholders = ",".join("?" * len(ids))
        for row in db.query(
            f"""
            SELECT m.id, m.conversation_id, m.role, c.title AS conversation_title
            FROM messages m
            LEFT JOIN conversations c ON c.id = m.conversation_id
            WHERE m.id IN ({placeholders})
            """,
            tuple(ids),
        ):
            context[("message", row["id"])] = {
                "conversation_id": row["conversation_id"],
                "conversation_title": row["conversation_title"],
                "role": row["role"],
            }

    if by_entity.get("memory"):
        ids = by_entity["memory"]
        placeholders = ",".join("?" * len(ids))
        for row in db.query(
            f"SELECT id, category, approved, pinned FROM memories WHERE id IN ({placeholders})",
            tuple(ids),
        ):
            context[("memory", row["id"])] = {
                "category": row["category"],
                "approved": bool(row["approved"]),
                "pinned": bool(row["pinned"]),
            }

    if by_entity.get("task"):
        ids = by_entity["task"]
        placeholders = ",".join("?" * len(ids))
        for row in db.query(
            f"SELECT id, status FROM tasks WHERE id IN ({placeholders})", tuple(ids)
        ):
            context[("task", row["id"])] = {"status": row["status"]}

    if by_entity.get("activity"):
        ids = by_entity["activity"]
        placeholders = ",".join("?" * len(ids))
        for row in db.query(
            f"SELECT id, kind, level, created_at FROM activity WHERE id IN ({placeholders})",
            tuple(ids),
        ):
            context[("activity", row["id"])] = {
                "kind": row["kind"],
                "level": row["level"],
                "created_at": row["created_at"],
            }

    if by_entity.get("conversation"):
        ids = by_entity["conversation"]
        placeholders = ",".join("?" * len(ids))
        for row in db.query(
            f"SELECT id, archived, project_id FROM conversations WHERE id IN ({placeholders})",
            tuple(ids),
        ):
            context[("conversation", row["id"])] = {
                "archived": bool(row["archived"]),
                "project_id": row["project_id"],
            }

    enriched: list[dict[str, Any]] = []
    for row in results:
        key = (row["entity"], row["entity_id"])
        if key not in context:
            # The backing row vanished between MATCH and lookup. Dropping it beats
            # rendering a result that navigates nowhere (§80: no dead affordances).
            continue
        merged = dict(row)
        merged["context"] = context[key]
        enriched.append(merged)
    return enriched


def search_resolved(
    term: str,
    *,
    entities: tuple[str, ...] | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    return resolve(search(term, entities=entities, limit=limit))


def counts(term: str) -> dict[str, int]:
    """Per-entity hit counts, for filter chips that show real numbers."""
    match = fts_escape(term)
    if not match:
        return {}
    rows = db.query(
        """
        SELECT entity, COUNT(*) AS n
        FROM search_index
        WHERE search_index MATCH ?
        GROUP BY entity
        """,
        (match,),
    )
    return {row["entity"]: int(row["n"]) for row in rows}


def rebuild() -> None:
    """Rebuild the FTS index from the source tables.

    Needed after an import (task B9), and available in diagnostics when triggers
    were bypassed. Uses the same projections as the triggers in ``schema.py``.
    """
    with db.transaction() as c:
        c.execute("DELETE FROM search_index")
        c.execute(
            """
            INSERT INTO search_index (entity, entity_id, title, body)
            SELECT 'conversation', id, title, '' FROM conversations
            """
        )
        c.execute(
            """
            INSERT INTO search_index (entity, entity_id, title, body)
            SELECT 'message', id, conversation_id, content FROM messages
            """
        )
        c.execute(
            """
            INSERT INTO search_index (entity, entity_id, title, body)
            SELECT 'memory', id, title, body FROM memories
            """
        )
        c.execute(
            """
            INSERT INTO search_index (entity, entity_id, title, body)
            SELECT 'task', id, objective, '' FROM tasks
            """
        )
        c.execute(
            """
            INSERT INTO search_index (entity, entity_id, title, body)
            SELECT 'activity', id, summary, kind FROM activity
            """
        )


def index_size() -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM search_index")
    return int(row["n"]) if row else 0
