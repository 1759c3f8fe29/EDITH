"""Project repository (task B3; spec §41 Projects screen).

A project groups conversations, memories, tasks and files under one objective.
Deleting a project must not delete the user's conversations -- the schema uses
ON DELETE SET NULL for exactly that reason, and ``delete`` reports how many rows
were detached so the UI can say so plainly instead of implying data loss.
"""

from __future__ import annotations

from typing import Any

from .. import conn as db
from .base import new_id, now, to_dict, to_list, tombstone

ENTITY = "project"


def create(title: str, description: str = "") -> dict[str, Any]:
    pid = new_id()
    stamp = now()
    with db.transaction() as c:
        rev = db.next_rev(c)
        c.execute(
            """
            INSERT INTO projects
                (id, title, description, status, created_at, updated_at, rev)
            VALUES (?, ?, ?, 'active', ?, ?, ?)
            """,
            (pid, title, description, stamp, stamp, rev),
        )
    return {
        "id": pid,
        "title": title,
        "description": description,
        "status": "active",
        "created_at": stamp,
        "updated_at": stamp,
        "rev": rev,
    }


def get(project_id: str) -> dict[str, Any] | None:
    return to_dict(db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,)))


def list_all(*, status: str | None = "active", limit: int = 100) -> list[dict[str, Any]]:
    params: list[Any] = []
    where = ""
    if status:
        where = "WHERE status = ?"
        params.append(status)
    params.append(min(limit, 300))
    return to_list(
        db.query(
            f"""
            SELECT p.*,
                   (SELECT COUNT(*) FROM conversations c WHERE c.project_id = p.id) AS conversation_count,
                   (SELECT COUNT(*) FROM memories m WHERE m.project_id = p.id) AS memory_count,
                   (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id) AS task_count
            FROM projects p
            {where}
            ORDER BY p.updated_at DESC
            LIMIT ?
            """,
            tuple(params),
        )
    )


def update(
    project_id: str,
    *,
    title: str | None = None,
    description: str | None = None,
    status: str | None = None,
) -> bool:
    fields: dict[str, Any] = {}
    if title is not None:
        fields["title"] = title
    if description is not None:
        fields["description"] = description
    if status in ("active", "archived"):
        fields["status"] = status
    if not fields:
        return False
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with db.transaction() as c:
        rev = db.next_rev(c)
        cur = c.execute(
            f"UPDATE projects SET {assignments}, updated_at = ?, rev = ? WHERE id = ?",
            (*fields.values(), now(), rev, project_id),
        )
        return (cur.rowcount or 0) > 0


def delete(project_id: str) -> dict[str, Any]:
    """Delete a project, detaching (not deleting) its contents.

    Returns counts so the confirmation dialog can state the real consequence.
    """
    detached = {
        "conversations": _count("conversations", project_id),
        "memories": _count("memories", project_id),
        "tasks": _count("tasks", project_id),
    }
    with db.transaction() as c:
        rev = db.next_rev(c)
        cur = c.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        removed = (cur.rowcount or 0) > 0
        if removed:
            tombstone(c, ENTITY, project_id, rev)
    return {"deleted": removed, "detached": detached}


def _count(table: str, project_id: str) -> int:
    row = db.query_one(
        f"SELECT COUNT(*) AS n FROM {table} WHERE project_id = ?", (project_id,)
    )
    return int(row["n"]) if row else 0
