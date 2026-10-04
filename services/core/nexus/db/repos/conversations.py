"""Conversation and message repository (task B3, screens §38).

Note the deliberate split from memory: a conversation is transient context, not
long-term memory (§16, §487). Nothing here is promoted to a memory without the
user approving it in ``memories.propose`` -> ``memories.approve``.
"""

from __future__ import annotations

from typing import Any

from .. import conn as db
from .base import dumps, loads, new_id, now, to_dict, to_list, tombstone

ENTITY = "conversation"


# ---------------------------------------------------------------- conversations


def create(title: str = "New conversation", project_id: str | None = None) -> dict[str, Any]:
    cid = new_id()
    stamp = now()
    with db.transaction() as c:
        rev = db.next_rev(c)
        c.execute(
            """
            INSERT INTO conversations
                (id, title, project_id, archived, created_at, updated_at, rev)
            VALUES (?, ?, ?, 0, ?, ?, ?)
            """,
            (cid, title, project_id, stamp, stamp, rev),
        )
    return {
        "id": cid,
        "title": title,
        "project_id": project_id,
        "archived": 0,
        "created_at": stamp,
        "updated_at": stamp,
        "rev": rev,
    }


def get(conversation_id: str) -> dict[str, Any] | None:
    return to_dict(
        db.query_one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
    )


def list_all(
    *,
    limit: int = 50,
    archived: bool = False,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["archived = ?"]
    params: list[Any] = [1 if archived else 0]
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    params.append(min(limit, 200))
    rows = db.query(
        f"""
        SELECT c.*,
               (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count,
               (SELECT m.content FROM messages m
                 WHERE m.conversation_id = c.id
                 ORDER BY m.created_at DESC LIMIT 1) AS last_message
        FROM conversations c
        WHERE {' AND '.join(clauses)}
        ORDER BY c.updated_at DESC
        LIMIT ?
        """,
        tuple(params),
    )
    return to_list(rows)


def rename(conversation_id: str, title: str) -> bool:
    with db.transaction() as c:
        rev = db.next_rev(c)
        cur = c.execute(
            "UPDATE conversations SET title = ?, updated_at = ?, rev = ? WHERE id = ?",
            (title, now(), rev, conversation_id),
        )
        return (cur.rowcount or 0) > 0


def set_archived(conversation_id: str, archived: bool) -> bool:
    with db.transaction() as c:
        rev = db.next_rev(c)
        cur = c.execute(
            "UPDATE conversations SET archived = ?, updated_at = ?, rev = ? WHERE id = ?",
            (1 if archived else 0, now(), rev, conversation_id),
        )
        return (cur.rowcount or 0) > 0


def delete(conversation_id: str) -> bool:
    """Hard delete. Messages cascade; FTS rows go via triggers (§70)."""
    with db.transaction() as c:
        rev = db.next_rev(c)
        cur = c.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        removed = (cur.rowcount or 0) > 0
        if removed:
            tombstone(c, ENTITY, conversation_id, rev)
        return removed


def touch(conversation_id: str) -> None:
    """Bump ordering without changing content."""
    with db.transaction() as c:
        rev = db.next_rev(c)
        c.execute(
            "UPDATE conversations SET updated_at = ?, rev = ? WHERE id = ?",
            (now(), rev, conversation_id),
        )


# --------------------------------------------------------------------- messages


def add_message(
    conversation_id: str,
    role: str,
    content: str = "",
    *,
    status: str = "complete",
    attachments: list[dict[str, Any]] | None = None,
    capability: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    mid = new_id()
    stamp = now()
    with db.transaction() as c:
        rev = db.next_rev(c)
        c.execute(
            """
            INSERT INTO messages
                (id, conversation_id, role, content, status, attachments,
                 capability, provider, model, created_at, updated_at, rev)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mid,
                conversation_id,
                role,
                content,
                status,
                dumps(attachments or []),
                capability,
                provider,
                model,
                stamp,
                stamp,
                rev,
            ),
        )
        c.execute(
            "UPDATE conversations SET updated_at = ?, rev = ? WHERE id = ?",
            (stamp, db.next_rev(c), conversation_id),
        )
    return {
        "id": mid,
        "conversation_id": conversation_id,
        "role": role,
        "content": content,
        "status": status,
        "attachments": attachments or [],
        "capability": capability,
        "provider": provider,
        "model": model,
        "created_at": stamp,
        "updated_at": stamp,
        "rev": rev,
    }


def finalize_message(
    message_id: str,
    *,
    content: str | None = None,
    status: str | None = None,
    capability: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    latency_ms: int | None = None,
    error: str | None = None,
) -> bool:
    """Write the outcome of a streamed reply.

    ``status`` must end up one of complete / failed / interrupted -- a message
    left at 'streaming' after a crash is what §105 "application restart during
    task" is about, and startup reconciliation depends on that being accurate.
    """
    fields: dict[str, Any] = {
        "content": content,
        "status": status,
        "capability": capability,
        "provider": provider,
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "latency_ms": latency_ms,
        "error": error,
    }
    usable = {k: v for k, v in fields.items() if v is not None}
    if not usable:
        return False
    assignments = ", ".join(f"{k} = ?" for k in usable)
    with db.transaction() as c:
        rev = db.next_rev(c)
        cur = c.execute(
            f"UPDATE messages SET {assignments}, updated_at = ?, rev = ? WHERE id = ?",
            (*usable.values(), now(), rev, message_id),
        )
        return (cur.rowcount or 0) > 0


def list_messages(conversation_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
    rows = db.query(
        """
        SELECT * FROM messages
        WHERE conversation_id = ?
        ORDER BY created_at
        LIMIT ?
        """,
        (conversation_id, min(limit, 2000)),
    )
    out = to_list(rows)
    for item in out:
        item["attachments"] = loads(item.get("attachments"), [])
    return out


def recent_turns(conversation_id: str, *, limit: int = 12) -> list[dict[str, Any]]:
    """The tail of a conversation, oldest-first, for prompt assembly (§19)."""
    rows = db.query(
        """
        SELECT role, content, created_at FROM messages
        WHERE conversation_id = ?
          AND status IN ('complete', 'interrupted')
          AND role IN ('user', 'assistant')
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (conversation_id, min(limit, 60)),
    )
    return list(reversed(to_list(rows)))


def exchanges_since(
    cursor: str = "",
    *,
    before: str = "",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Completed exchanges after ``cursor``, oldest first, for the memory sweep.

    An "exchange" is an assistant reply paired with the user message it answered.
    Extraction needs both -- the user's words are the material, the reply is context
    for what those words meant -- and pairing them here rather than in Python keeps
    the sweep to one query per batch instead of one per message.

    ``cursor`` is an opaque ``"<created_at>|<id>"`` watermark produced by
    :func:`exchange_cursor`. Ordering is ``(created_at, id)`` rather than
    ``created_at`` alone: two messages written in the same microsecond would
    otherwise have no defined order, and a watermark over an undefined order silently
    skips rows.

    ``before`` excludes anything newer than that timestamp. The sweep uses it to
    leave the most recent turns alone, because the inline extractor is still running
    on them (see nexus.ai.memory_sweep.GRACE_SECONDS).
    """
    at, _, mid = cursor.partition("|")
    clauses = ["a.role = 'assistant'", "a.status = 'complete'", "a.content <> ''"]
    params: list[Any] = []
    if at:
        clauses.append("(a.created_at > ? OR (a.created_at = ? AND a.id > ?))")
        params.extend((at, at, mid))
    if before:
        clauses.append("a.created_at < ?")
        params.append(before)
    params.append(min(limit, 200))
    rows = to_list(
        db.query(
            f"""
            SELECT
                a.id              AS message_id,
                a.conversation_id AS conversation_id,
                a.created_at      AS created_at,
                a.content         AS reply,
                c.project_id      AS project_id,
                (
                    SELECT u.content FROM messages u
                    WHERE u.conversation_id = a.conversation_id
                      AND u.role = 'user'
                      AND (u.created_at < a.created_at
                           OR (u.created_at = a.created_at AND u.id < a.id))
                    ORDER BY u.created_at DESC, u.id DESC
                    LIMIT 1
                )                 AS request
            FROM messages a
            JOIN conversations c ON c.id = a.conversation_id
            WHERE {' AND '.join(clauses)}
            ORDER BY a.created_at, a.id
            LIMIT ?
            """,
            tuple(params),
        )
    )
    for row in rows:
        row["request"] = row.get("request") or ""
    return rows


def exchange_cursor(exchange: dict[str, Any]) -> str:
    """The watermark that means "everything up to and including this one"."""
    return f"{exchange.get('created_at') or ''}|{exchange.get('message_id') or ''}"


def unswept_count(cursor: str = "", *, before: str = "") -> int:
    """How many exchanges are still behind the watermark.

    Reported in the sweep's status so a capped batch shows the backlog it did not
    reach. A cap the user cannot see reads as "everything has been looked at".
    """
    at, _, mid = cursor.partition("|")
    clauses = ["role = 'assistant'", "status = 'complete'", "content <> ''"]
    params: list[Any] = []
    if at:
        clauses.append("(created_at > ? OR (created_at = ? AND id > ?))")
        params.extend((at, at, mid))
    if before:
        clauses.append("created_at < ?")
        params.append(before)
    row = db.query_one(
        f"SELECT COUNT(*) AS n FROM messages WHERE {' AND '.join(clauses)}",
        tuple(params),
    )
    return int(row["n"]) if row else 0


def get_message(message_id: str) -> dict[str, Any] | None:
    row = to_dict(db.query_one("SELECT * FROM messages WHERE id = ?", (message_id,)))
    if row:
        row["attachments"] = loads(row.get("attachments"), [])
    return row


def delete_message(message_id: str) -> bool:
    with db.transaction() as c:
        rev = db.next_rev(c)
        cur = c.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        removed = (cur.rowcount or 0) > 0
        if removed:
            tombstone(c, "message", message_id, rev)
        return removed


def reconcile_streaming() -> int:
    """Mark orphaned 'streaming' messages as interrupted.

    Called at startup (§88): the process died mid-reply, so the honest state is
    interrupted -- not complete, and not still streaming.
    """
    with db.transaction() as c:
        rev = db.next_rev(c)
        cur = c.execute(
            """
            UPDATE messages
            SET status = 'interrupted',
                error = COALESCE(error, 'Interrupted by application restart'),
                updated_at = ?, rev = ?
            WHERE status = 'streaming'
            """,
            (now(), rev),
        )
        return cur.rowcount or 0
