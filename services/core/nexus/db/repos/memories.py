"""Memory repository (tasks B3, B5, B8; spec §16-§18, §39).

Two rules shape this module:

* **Nothing is remembered without approval.** ``propose`` writes with
  ``approved = 0``; only ``approve`` makes a row eligible for retrieval. A
  conversation existing is not the same as NEXUS remembering it (§487).
* **Delete means delete.** ``delete`` removes the row, its vector, and its FTS
  entry, and is verified by re-query in the tests (§70, §513).
"""

from __future__ import annotations

import math
import struct
from typing import Any, Iterable

from .. import conn as db
from .base import fts_escape, new_id, now, to_dict, to_list, tombstone

ENTITY = "memory"

CATEGORIES: tuple[str, ...] = (
    "general",
    "preference",
    "project",
    "person",
    "task",
    "file",
    "device",
    "goal",
    # How a recurring job is done, learned from the user's own completed tasks
    # (task O8). A real category rather than a tag, because it is recalled
    # differently: "how you did this last time" is context for a plan, where the
    # others are facts about the user. Proposed by `nexus.ai.procedures`, approved
    # like every other memory -- never stored straight from inference (§32).
    "procedural",
)


# ----------------------------------------------------------------- vector codec


def encode_vector(values: Iterable[float]) -> bytes:
    vals = list(values)
    return struct.pack(f"<{len(vals)}f", *vals)


def decode_vector(blob: bytes) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob[: count * 4]))


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# ----------------------------------------------------------------------- writes


def propose(
    title: str,
    body: str,
    *,
    category: str = "general",
    project_id: str | None = None,
    source_conversation_id: str | None = None,
    source_message_id: str | None = None,
) -> dict[str, Any]:
    """Record a candidate memory awaiting user approval (§F5)."""
    return _insert(
        title=title,
        body=body,
        category=category,
        project_id=project_id,
        source_conversation_id=source_conversation_id,
        source_message_id=source_message_id,
        approved=0,
    )


def create(
    title: str,
    body: str,
    *,
    category: str = "general",
    project_id: str | None = None,
    source_conversation_id: str | None = None,
    source_message_id: str | None = None,
) -> dict[str, Any]:
    """Create an already-approved memory (explicit user action, e.g. 'remember this')."""
    return _insert(
        title=title,
        body=body,
        category=category,
        project_id=project_id,
        source_conversation_id=source_conversation_id,
        source_message_id=source_message_id,
        approved=1,
    )


def _insert(**kw: Any) -> dict[str, Any]:
    mid = new_id()
    stamp = now()
    category = kw["category"] if kw["category"] in CATEGORIES else "general"
    with db.transaction() as c:
        rev = db.next_rev(c)
        c.execute(
            """
            INSERT INTO memories
                (id, title, body, category, project_id, source_conversation_id,
                 source_message_id, approved, pinned, created_at, updated_at, rev)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                mid,
                kw["title"],
                kw["body"],
                category,
                kw["project_id"],
                kw["source_conversation_id"],
                kw["source_message_id"],
                kw["approved"],
                stamp,
                stamp,
                rev,
            ),
        )
    return {
        "id": mid,
        "title": kw["title"],
        "body": kw["body"],
        "category": category,
        "project_id": kw["project_id"],
        "source_conversation_id": kw["source_conversation_id"],
        "source_message_id": kw["source_message_id"],
        "approved": kw["approved"],
        "pinned": 0,
        "created_at": stamp,
        "updated_at": stamp,
        "rev": rev,
    }


def approve(memory_id: str) -> bool:
    with db.transaction() as c:
        rev = db.next_rev(c)
        cur = c.execute(
            "UPDATE memories SET approved = 1, updated_at = ?, rev = ? WHERE id = ?",
            (now(), rev, memory_id),
        )
        return (cur.rowcount or 0) > 0


def update(
    memory_id: str,
    *,
    title: str | None = None,
    body: str | None = None,
    category: str | None = None,
    pinned: bool | None = None,
    project_id: str | None = None,
) -> bool:
    fields: dict[str, Any] = {}
    if title is not None:
        fields["title"] = title
    if body is not None:
        fields["body"] = body
    if category is not None:
        fields["category"] = category if category in CATEGORIES else "general"
    if pinned is not None:
        fields["pinned"] = 1 if pinned else 0
    if project_id is not None:
        fields["project_id"] = project_id
    if not fields:
        return False
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with db.transaction() as c:
        rev = db.next_rev(c)
        cur = c.execute(
            f"UPDATE memories SET {assignments}, updated_at = ?, rev = ? WHERE id = ?",
            (*fields.values(), now(), rev, memory_id),
        )
        changed = (cur.rowcount or 0) > 0
        if changed and ("body" in fields or "title" in fields):
            # The stored embedding no longer describes the text.
            c.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (memory_id,))
        return changed


def set_vector(memory_id: str, vector: list[float], model: str) -> None:
    with db.transaction() as c:
        c.execute(
            """
            INSERT INTO memory_vectors (memory_id, dim, model, vec, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                dim = excluded.dim, model = excluded.model,
                vec = excluded.vec, created_at = excluded.created_at
            """,
            (memory_id, len(vector), model, encode_vector(vector), now()),
        )


# ------------------------------------------------------------------------ reads


def get(memory_id: str) -> dict[str, Any] | None:
    return to_dict(db.query_one("SELECT * FROM memories WHERE id = ?", (memory_id,)))


def list_all(
    *,
    limit: int = 100,
    approved: bool | None = True,
    category: str | None = None,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if approved is not None:
        clauses.append("approved = ?")
        params.append(1 if approved else 0)
    if category:
        clauses.append("category = ?")
        params.append(category)
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(min(limit, 500))
    return to_list(
        db.query(
            f"SELECT * FROM memories {where} ORDER BY pinned DESC, updated_at DESC LIMIT ?",
            tuple(params),
        )
    )


def pending_count() -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM memories WHERE approved = 0")
    return int(row["n"]) if row else 0


def search_text(term: str, *, limit: int = 20, approved_only: bool = True) -> list[dict[str, Any]]:
    """Keyword search via FTS5 (task B7)."""
    match = fts_escape(term)
    if not match:
        return []
    clause = "AND m.approved = 1" if approved_only else ""
    return to_list(
        db.query(
            f"""
            SELECT m.*, bm25(search_index) AS score
            FROM search_index
            JOIN memories m ON m.id = search_index.entity_id
            WHERE search_index MATCH ?
              AND search_index.entity = 'memory'
              {clause}
            ORDER BY score
            LIMIT ?
            """,
            (match, min(limit, 100)),
        )
    )


def search_semantic(
    query_vector: list[float],
    *,
    limit: int = 10,
    threshold: float = 0.25,
    approved_only: bool = True,
) -> list[dict[str, Any]]:
    """Vector similarity over stored embeddings (task B5).

    Brute-force cosine. At personal-assistant scale (thousands of memories, not
    millions) this stays well inside a frame budget, and it keeps the vector
    store swappable without a native extension (§76).
    """
    clause = "WHERE m.approved = 1" if approved_only else ""
    rows = db.query(
        f"""
        SELECT m.*, v.vec AS vec_blob, v.dim AS vec_dim
        FROM memory_vectors v
        JOIN memories m ON m.id = v.memory_id
        {clause}
        """
    )
    scored: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        blob = item.pop("vec_blob", None)
        item.pop("vec_dim", None)
        if not blob:
            continue
        score = cosine(query_vector, decode_vector(blob))
        if score >= threshold:
            item["score"] = score
            scored.append(item)
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:limit]


def extracted_message_ids(message_ids: list[str]) -> set[str]:
    """Which of these messages already produced a memory, approved or not.

    The sweep uses this to skip exchanges the inline extractor already read. It has
    to include unapproved rows: a proposal the user rejected still means the
    exchange was examined, and re-proposing what they just dismissed is the fastest
    way to teach them to ignore the Memory screen.

    Note this cannot distinguish "examined and found nothing" -- that turn leaves no
    row at all. The watermark in nexus.ai.memory_sweep covers that case; this covers
    the overlap between the two mechanisms.
    """
    if not message_ids:
        return set()
    found: set[str] = set()
    # Chunked: SQLite's parameter limit is 999 by default, and a sweep backlog can
    # be longer than that.
    for start in range(0, len(message_ids), 500):
        batch = message_ids[start : start + 500]
        placeholders = ",".join("?" * len(batch))
        rows = db.query(
            f"""
            SELECT DISTINCT source_message_id FROM memories
            WHERE source_message_id IN ({placeholders})
            """,
            tuple(batch),
        )
        found.update(str(r["source_message_id"]) for r in rows)
    return found


def coverage() -> dict[str, int]:
    """How much of memory is embedded -- drives the honest B6 fallback notice."""
    total = db.query_one("SELECT COUNT(*) AS n FROM memories WHERE approved = 1")
    vectored = db.query_one(
        """
        SELECT COUNT(*) AS n FROM memory_vectors v
        JOIN memories m ON m.id = v.memory_id WHERE m.approved = 1
        """
    )
    return {
        "approved": int(total["n"]) if total else 0,
        "embedded": int(vectored["n"]) if vectored else 0,
    }


def unembedded(*, limit: int = 500) -> list[dict[str, Any]]:
    """Approved memories with no stored vector, oldest first.

    Oldest first so a backfill interrupted halfway leaves a coherent state: the
    memories that have been around longest -- and are therefore most likely to be
    referred to obliquely -- become searchable by meaning first.
    """
    return to_list(
        db.query(
            """
            SELECT m.* FROM memories m
            LEFT JOIN memory_vectors v ON v.memory_id = m.id
            WHERE m.approved = 1 AND v.memory_id IS NULL
            ORDER BY m.created_at ASC
            LIMIT ?
            """,
            (min(limit, 2_000),),
        )
    )


def vector_models() -> set[str]:
    """Which embedding models the stored vectors came from.

    Almost always one. More than one, or one that is not the current model, means
    part of the store cannot be compared against a fresh query -- ``cosine``
    returns 0.0 across mismatched dimensions, so those rows go quiet rather than
    wrong. The caller reports that instead of showing an unexplained empty result.
    """
    rows = db.query("SELECT DISTINCT model FROM memory_vectors WHERE model <> ''")
    return {str(row["model"]) for row in rows}


# --------------------------------------------------------------------- deletion


def delete(memory_id: str) -> bool:
    with db.transaction() as c:
        rev = db.next_rev(c)
        cur = c.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        removed = (cur.rowcount or 0) > 0
        if removed:
            tombstone(c, ENTITY, memory_id, rev)
        return removed


def resolve_forget(
    *,
    query: str | None = None,
    project_id: str | None = None,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """Find exactly what a 'forget ...' request would remove (task F8).

    Returned for preview and confirmation *before* anything is deleted, so the
    user sees the blast radius rather than trusting a natural-language guess.
    """
    if project_id:
        return to_list(
            db.query("SELECT * FROM memories WHERE project_id = ?", (project_id,))
        )
    if category:
        return to_list(
            db.query("SELECT * FROM memories WHERE category = ?", (category,))
        )
    if query:
        match = fts_escape(query)
        if not match:
            return []
        return to_list(
            db.query(
                """
                SELECT m.* FROM search_index
                JOIN memories m ON m.id = search_index.entity_id
                WHERE search_index MATCH ? AND search_index.entity = 'memory'
                """,
                (match,),
            )
        )
    return []


def delete_many(memory_ids: list[str]) -> int:
    """Delete a resolved set atomically. Returns the number actually removed."""
    if not memory_ids:
        return 0
    placeholders = ",".join("?" * len(memory_ids))
    with db.transaction() as c:
        cur = c.execute(
            f"DELETE FROM memories WHERE id IN ({placeholders})", tuple(memory_ids)
        )
        removed = cur.rowcount or 0
        for mid in memory_ids:
            tombstone(c, ENTITY, mid, db.next_rev(c))
        return removed


def verify_absent(memory_ids: list[str]) -> list[str]:
    """Re-query after a delete. Any id returned here is a §70 violation."""
    if not memory_ids:
        return []
    placeholders = ",".join("?" * len(memory_ids))
    rows = db.query(
        f"SELECT id FROM memories WHERE id IN ({placeholders})", tuple(memory_ids)
    )
    still_there = [r["id"] for r in rows]
    vec_rows = db.query(
        f"SELECT memory_id FROM memory_vectors WHERE memory_id IN ({placeholders})",
        tuple(memory_ids),
    )
    still_there.extend(r["memory_id"] for r in vec_rows)
    fts_rows = db.query(
        f"""
        SELECT entity_id FROM search_index
        WHERE entity = 'memory' AND entity_id IN ({placeholders})
        """,
        tuple(memory_ids),
    )
    still_there.extend(r["entity_id"] for r in fts_rows)
    return sorted(set(still_there))
