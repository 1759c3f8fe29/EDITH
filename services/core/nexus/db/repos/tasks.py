"""Task and step repository for agent mode (task B3; spec §20-§23, §40).

The status values match the spec's state machine exactly:

    pending -> running -> completed | failed | cancelled
                       -> waiting_permission -> running
    (any live state) -> interrupted   [process died; set at startup]

``interrupted`` exists so a task whose process was killed mid-run is never left
displaying "running" forever, and is never silently promoted to "completed"
(§79, §105).
"""

from __future__ import annotations

from typing import Any

from .. import conn as db
from .base import dumps, loads, new_id, now, to_dict, to_list, tombstone

ENTITY = "task"

LIVE_STATUSES = ("pending", "running", "waiting_permission")
TERMINAL_STATUSES = ("completed", "failed", "cancelled", "interrupted")


# -------------------------------------------------------------------- tasks


def create(
    objective: str,
    *,
    project_id: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    tid = new_id()
    stamp = now()
    with db.transaction() as c:
        rev = db.next_rev(c)
        c.execute(
            """
            INSERT INTO tasks
                (id, objective, status, project_id, conversation_id,
                 created_at, updated_at, rev)
            VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)
            """,
            (tid, objective, project_id, conversation_id, stamp, stamp, rev),
        )
    return {
        "id": tid,
        "objective": objective,
        "status": "pending",
        "project_id": project_id,
        "conversation_id": conversation_id,
        "result": None,
        "error": None,
        "started_at": None,
        "finished_at": None,
        "created_at": stamp,
        "updated_at": stamp,
        "rev": rev,
    }


def get(task_id: str) -> dict[str, Any] | None:
    return to_dict(db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,)))


def get_with_steps(task_id: str) -> dict[str, Any] | None:
    task = get(task_id)
    if task is None:
        return None
    task["steps"] = list_steps(task_id)
    return task


def list_all(
    *,
    statuses: tuple[str, ...] | None = None,
    project_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if statuses:
        clauses.append(f"status IN ({','.join('?' * len(statuses))})")
        params.extend(statuses)
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(min(limit, 200))
    rows = to_list(
        db.query(
            f"""
            SELECT t.*,
                   (SELECT COUNT(*) FROM task_steps s WHERE s.task_id = t.id) AS step_count,
                   (SELECT COUNT(*) FROM task_steps s
                     WHERE s.task_id = t.id AND s.status = 'completed') AS steps_done
            FROM tasks t
            {where}
            ORDER BY t.created_at DESC
            LIMIT ?
            """,
            tuple(params),
        )
    )
    return rows


def set_status(
    task_id: str,
    status: str,
    *,
    result: str | None = None,
    error: str | None = None,
) -> bool:
    stamp = now()
    fields: dict[str, Any] = {"status": status}
    if status == "running":
        fields["started_at"] = stamp
    if status in TERMINAL_STATUSES:
        fields["finished_at"] = stamp
    if status == "running":
        # Cleared, not left behind: an interrupted task carries the moment its last
        # attempt stopped, and resuming it with that still set makes "took" the
        # difference between a new start and an older finish -- a negative duration.
        fields["finished_at"] = None
    if status == "pending":
        # Back to not-yet-run, which is what a retry writes before starting. The
        # previous attempt's verdict is cleared for the same reason the step-level
        # write clears it: a task with no outcome yet must not display the last
        # outcome as though it were this one. The trail keeps what happened.
        fields.setdefault("result", None)
        fields.setdefault("error", None)
        fields["finished_at"] = None
        fields["started_at"] = None
    if result is not None:
        fields["result"] = result
    if error is not None:
        fields["error"] = error
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with db.transaction() as c:
        rev = db.next_rev(c)
        cur = c.execute(
            f"UPDATE tasks SET {assignments}, updated_at = ?, rev = ? WHERE id = ?",
            (*fields.values(), stamp, rev, task_id),
        )
        return (cur.rowcount or 0) > 0


def delete(task_id: str) -> bool:
    with db.transaction() as c:
        rev = db.next_rev(c)
        cur = c.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        removed = (cur.rowcount or 0) > 0
        if removed:
            tombstone(c, ENTITY, task_id, rev)
        return removed


# Statuses that can only exist while a run is in flight. Deliberately not
# ``LIVE_STATUSES``: that includes ``pending``, and a pending task is one that was
# planned and never started -- it survives a restart perfectly well and must not be
# stamped with an interruption that did not happen to it.
IN_FLIGHT_STATUSES = ("running", "waiting_permission")

STEP_INTERRUPTED = (
    "Interrupted by a restart before it finished. It is not known whether it "
    "completed, so resuming the task runs it again."
)


def reconcile_interrupted() -> list[str]:
    """Startup pass: no run can still be in flight in a fresh process (§105).

    Returns the ids it changed, so the caller can announce them on the bus -- a
    window open across the restart is showing those tasks as running.

    Steps that were mid-flight go back to **pending**, not failed. The distinction
    is the whole point of the pass: ``interrupted`` is a resumable status and
    ``next_runnable_step`` only ever picks a pending step, so failing them would
    make Resume produce a task that dies on its first move. What is not known --
    whether the tool actually finished before the process died -- is written onto
    the step in words instead of being decided by the status.
    """
    placeholders = ",".join("?" * len(IN_FLIGHT_STATUSES))
    with db.transaction() as c:
        rev = db.next_rev(c)
        stamp = now()
        ids = [
            row[0]
            for row in c.execute(
                f"SELECT id FROM tasks WHERE status IN ({placeholders})", IN_FLIGHT_STATUSES
            ).fetchall()
        ]
        if not ids:
            return []
        c.execute(
            f"""
            UPDATE task_steps
            SET status = 'pending',
                error = ?,
                updated_at = ?
            WHERE status IN ({placeholders})
              AND task_id IN (SELECT id FROM tasks WHERE status IN ({placeholders}))
            """,
            (STEP_INTERRUPTED, stamp, *IN_FLIGHT_STATUSES, *IN_FLIGHT_STATUSES),
        )
        c.execute(
            f"""
            UPDATE tasks
            SET status = 'interrupted',
                error = COALESCE(error, ?),
                finished_at = ?, updated_at = ?, rev = ?
            WHERE status IN ({placeholders})
            """,
            (
                "The core stopped while this task was running, so it did not finish.",
                stamp,
                stamp,
                rev,
                *IN_FLIGHT_STATUSES,
            ),
        )
        return ids


# -------------------------------------------------------------------- steps


def add_steps(task_id: str, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Write a plan. Steps are ordered by ``idx``, unique per task.

    The plan comes from the model (§20) but is stored as data, not prose, so
    execution and the UI read the same structure.
    """
    stamp = now()
    created: list[dict[str, Any]] = []
    with db.transaction() as c:
        row = c.execute(
            "SELECT COALESCE(MAX(idx), -1) AS m FROM task_steps WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        next_idx = int(row["m"]) + 1
        for offset, step in enumerate(steps):
            sid = new_id()
            idx = next_idx + offset
            c.execute(
                """
                INSERT INTO task_steps
                    (id, task_id, idx, intent, capability, tool, params,
                     depends_on, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    sid,
                    task_id,
                    idx,
                    step.get("intent", ""),
                    step.get("capability"),
                    step.get("tool"),
                    dumps(step.get("params") or {}),
                    dumps(step.get("depends_on") or []),
                    stamp,
                    stamp,
                ),
            )
            created.append(
                {
                    "id": sid,
                    "task_id": task_id,
                    "idx": idx,
                    "intent": step.get("intent", ""),
                    "capability": step.get("capability"),
                    "tool": step.get("tool"),
                    "params": step.get("params") or {},
                    "depends_on": step.get("depends_on") or [],
                    "status": "pending",
                    "verified": None,
                    "created_at": stamp,
                    "updated_at": stamp,
                }
            )
    return created


def list_steps(task_id: str) -> list[dict[str, Any]]:
    rows = to_list(
        db.query("SELECT * FROM task_steps WHERE task_id = ? ORDER BY idx", (task_id,))
    )
    for row in rows:
        row["params"] = loads(row.get("params"), {})
        row["depends_on"] = loads(row.get("depends_on"), [])
    return rows


def get_step(step_id: str) -> dict[str, Any] | None:
    row = to_dict(db.query_one("SELECT * FROM task_steps WHERE id = ?", (step_id,)))
    if row:
        row["params"] = loads(row.get("params"), {})
        row["depends_on"] = loads(row.get("depends_on"), [])
    return row


def set_step_status(
    step_id: str,
    status: str,
    *,
    result: str | None = None,
    error: str | None = None,
    verified: bool | None = None,
) -> bool:
    fields: dict[str, Any] = {"status": status}
    if result is not None:
        fields["result"] = result
    if error is not None:
        fields["error"] = error
    if verified is not None:
        fields["verified"] = 1 if verified else 0
    if status == "running":
        # A step that is starting has no result and no failure yet. Cleared rather
        # than left: a resumed or re-planned step keeps the previous attempt's error
        # otherwise, and the panel would print last time's failure in red underneath
        # this time's result. ``setdefault`` so an explicit argument still wins.
        fields.setdefault("result", None)
        fields.setdefault("error", None)
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with db.transaction() as c:
        cur = c.execute(
            f"UPDATE task_steps SET {assignments}, updated_at = ? WHERE id = ?",
            (*fields.values(), now(), step_id),
        )
        return (cur.rowcount or 0) > 0


def replace_pending_steps(task_id: str, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-plan mid-task (§22 adaptation) without rewriting history.

    Completed and failed steps stay exactly as they were; only not-yet-started
    steps are replaced, so the record of what actually happened is preserved.
    """
    with db.transaction() as c:
        c.execute(
            "DELETE FROM task_steps WHERE task_id = ? AND status = 'pending'",
            (task_id,),
        )
    return add_steps(task_id, steps)


#: Step statuses a retry resets. ``completed`` is deliberately absent: retrying a
#: task must not redo work that already succeeded, which is the whole meaning of
#: "from the last good step" (§64). ``running`` and ``waiting_permission`` are
#: absent too -- those only exist while a run is in flight, and a retry is refused
#: while one is.
RETRYABLE_STEP_STATUSES = ("failed", "skipped", "cancelled")

STEP_RETRIED = "Queued again by a retry. The previous attempt's failure is above, in the trail."


def reset_steps_for_retry(task_id: str) -> list[str]:
    """Put the steps a retry would re-run back to ``pending``. Returns their ids.

    Only the steps that did not succeed, and no new status: ``pending`` is what
    ``next_runnable_step`` looks for, so this is the one write that makes a finished
    task runnable again. The previous attempt's ``error`` is cleared from the row
    rather than left on it -- the trail keeps what happened, and a step about to run
    again should not display last time's failure as if it were current.

    ``verified`` is cleared for the same reason. A step that failed verification and
    is being run again has not been verified *this* time, and leaving the old 0 in
    place would make the tri-state a record of a different attempt.
    """
    placeholders = ",".join("?" * len(RETRYABLE_STEP_STATUSES))
    with db.transaction() as c:
        ids = [
            row[0]
            for row in c.execute(
                f"SELECT id FROM task_steps WHERE task_id = ? "
                f"AND status IN ({placeholders}) ORDER BY idx",
                (task_id, *RETRYABLE_STEP_STATUSES),
            ).fetchall()
        ]
        if not ids:
            return []
        c.execute(
            f"""
            UPDATE task_steps
            SET status = 'pending', error = ?, result = NULL, verified = NULL,
                updated_at = ?
            WHERE id IN ({','.join('?' * len(ids))})
            """,
            (STEP_RETRIED, now(), *ids),
        )
        return ids


def next_runnable_step(task_id: str) -> dict[str, Any] | None:
    """The first pending step whose dependencies are all completed."""
    steps = list_steps(task_id)
    done = {s["id"] for s in steps if s["status"] == "completed"}
    done_idx = {s["idx"] for s in steps if s["status"] == "completed"}
    for step in steps:
        if step["status"] != "pending":
            continue
        deps = step["depends_on"] or []
        satisfied = all(
            (d in done) or (isinstance(d, int) and d in done_idx) for d in deps
        )
        if satisfied:
            return step
    return None


def progress(task_id: str) -> dict[str, int]:
    """Real step counts -- the only thing the UI may show as progress (§80)."""
    rows = db.query(
        "SELECT status, COUNT(*) AS n FROM task_steps WHERE task_id = ? GROUP BY status",
        (task_id,),
    )
    counts = {r["status"]: int(r["n"]) for r in rows}
    total = sum(counts.values())
    return {
        "total": total,
        "completed": counts.get("completed", 0),
        "failed": counts.get("failed", 0),
        "pending": counts.get("pending", 0),
        "running": counts.get("running", 0),
        "skipped": counts.get("skipped", 0),
        "waiting_permission": counts.get("waiting_permission", 0),
    }
