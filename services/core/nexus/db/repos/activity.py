"""Activity log repository (tasks B3, and the store behind §36 / §44).

Every meaningful thing NEXUS does lands here: model selection, memory access,
tool invocation, permission decisions, task transitions, automation runs,
failures. This is the trust surface -- if it is not recorded, the user has no
way to know what happened.
"""

from __future__ import annotations

from typing import Any, Literal

from .. import conn as db
from .base import dumps, loads, new_id, now, to_list

Level = Literal["info", "success", "warning", "error"]

# Kinds are a closed set so the Activity screen filters (§44) can be built from
# it rather than from whatever strings happened to be written.
KINDS: tuple[str, ...] = (
    "ai",
    "memory",
    "tool",
    "application",
    "file",
    "browser",
    "terminal",
    "screen",
    "permission",
    "task",
    "device",
    "automation",
    "system",
    "error",
)


def record(
    kind: str,
    summary: str,
    *,
    detail: dict[str, Any] | None = None,
    level: Level = "info",
    task_id: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Append an activity entry.

    Deliberately tolerant: a logging failure must never abort the operation it
    was describing. The failure itself is logged through the app logger.
    """
    entry = {
        "id": new_id(),
        "kind": kind,
        "summary": summary,
        "detail": detail or {},
        "level": level,
        "task_id": task_id,
        "conversation_id": conversation_id,
        "created_at": now(),
    }
    with db.transaction() as c:
        c.execute(
            """
            INSERT INTO activity
                (id, kind, summary, detail, level, task_id, conversation_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry["id"],
                kind,
                summary,
                dumps(entry["detail"]),
                level,
                task_id,
                conversation_id,
                entry["created_at"],
            ),
        )
    return entry


def list_recent(
    *,
    limit: int = 100,
    before: str | None = None,
    kinds: tuple[str, ...] | None = None,
    levels: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if before:
        clauses.append("created_at < ?")
        params.append(before)
    if kinds:
        clauses.append(f"kind IN ({','.join('?' * len(kinds))})")
        params.extend(kinds)
    if levels:
        clauses.append(f"level IN ({','.join('?' * len(levels))})")
        params.extend(levels)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(min(limit, 500))
    rows = db.query(
        f"SELECT * FROM activity {where} ORDER BY created_at DESC LIMIT ?",
        tuple(params),
    )
    out = to_list(rows)
    for item in out:
        item["detail"] = loads(item.get("detail"), {})
    return out


def for_task(task_id: str) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM activity WHERE task_id = ? ORDER BY created_at",
        (task_id,),
    )
    out = to_list(rows)
    for item in out:
        item["detail"] = loads(item.get("detail"), {})
    return out


def counts_by_kind(since: str | None = None) -> dict[str, int]:
    if since:
        rows = db.query(
            "SELECT kind, COUNT(*) AS n FROM activity WHERE created_at >= ? GROUP BY kind",
            (since,),
        )
    else:
        rows = db.query("SELECT kind, COUNT(*) AS n FROM activity GROUP BY kind")
    return {r["kind"]: int(r["n"]) for r in rows}


def prune(keep_days: int) -> int:
    """Retention enforcement (task B10). Returns rows removed."""
    with db.transaction() as c:
        cur = c.execute(
            "DELETE FROM activity WHERE created_at < datetime('now', ?)",
            (f"-{int(keep_days)} days",),
        )
        return cur.rowcount or 0
