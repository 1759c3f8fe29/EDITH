"""Automation repository (task B3; spec §31-§34, §47 Automations screen).

An automation is a stored trigger plus a stored action. Two honesty rules:

* ``next_run_at`` is computed from the schedule, never faked. If the schedule
  cannot be parsed, the automation is disabled and ``unavailable_reason`` says
  why -- a rule that will never fire must not sit in the list looking healthy
  (§34, §78).
* Every attempt writes an ``automation_runs`` row, including skipped and failed
  ones, so "last run" reflects reality rather than the last success.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import conn as db
from .base import dumps, loads, new_id, now, to_dict, to_list, tombstone

ENTITY = "automation"

TRIGGER_KINDS = ("schedule", "interval", "manual")

# "HH:MM" daily, optionally restricted to weekdays.
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class ScheduleError(ValueError):
    """The schedule string cannot be interpreted."""


def parse_next_run(
    trigger_kind: str,
    schedule: str,
    *,
    after: datetime | None = None,
) -> datetime | None:
    """Compute the next fire time.

    Supported forms, kept deliberately small so every accepted schedule really
    runs:

    * ``schedule``: ``"08:30"`` or ``"mon,wed 08:30"``  (local time, daily)
    * ``interval``: ``"15m"``, ``"2h"``, ``"90s"``
    * ``manual``:   no automatic run
    """
    base = after or datetime.now().astimezone()

    if trigger_kind == "manual":
        return None

    text = (schedule or "").strip().lower()
    if not text:
        raise ScheduleError("schedule is empty")

    if trigger_kind == "interval":
        match = re.fullmatch(r"(\d+)\s*([smh])", text)
        if not match:
            raise ScheduleError(f"interval must look like '15m', '2h' or '90s', got {schedule!r}")
        amount = int(match.group(1))
        if amount <= 0:
            raise ScheduleError("interval must be greater than zero")
        unit = {"s": "seconds", "m": "minutes", "h": "hours"}[match.group(2)]
        delta = timedelta(**{unit: amount})
        if delta < timedelta(seconds=30):
            raise ScheduleError("interval must be at least 30 seconds")
        return base + delta

    if trigger_kind == "schedule":
        parts = text.split()
        days: tuple[str, ...] = _WEEKDAYS
        time_part = parts[-1]
        if len(parts) == 2:
            requested = tuple(d.strip() for d in parts[0].split(",") if d.strip())
            unknown = [d for d in requested if d not in _WEEKDAYS]
            if unknown:
                raise ScheduleError(f"unknown day(s): {', '.join(unknown)}")
            days = requested
        elif len(parts) > 2:
            raise ScheduleError(f"could not parse schedule {schedule!r}")

        match = _TIME_RE.match(time_part)
        if not match:
            raise ScheduleError(f"time must look like '08:30', got {time_part!r}")
        hour, minute = int(match.group(1)), int(match.group(2))

        candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        for _ in range(8):
            if candidate > base and _WEEKDAYS[candidate.weekday()] in days:
                return candidate
            candidate += timedelta(days=1)
        raise ScheduleError(f"no upcoming run matches {schedule!r}")

    raise ScheduleError(f"unknown trigger kind {trigger_kind!r}")


def create(
    name: str,
    *,
    trigger_kind: str = "schedule",
    schedule: str = "",
    action: dict[str, Any] | None = None,
    enabled: bool = True,
    capability_scope: list[str] | None = None,
) -> dict[str, Any]:
    if trigger_kind not in TRIGGER_KINDS:
        raise ValueError(f"trigger_kind must be one of {TRIGGER_KINDS}")

    next_run: str | None = None
    unavailable: str | None = None
    try:
        parsed = parse_next_run(trigger_kind, schedule)
        next_run = parsed.isoformat() if parsed else None
    except ScheduleError as exc:
        # Store it, but disabled and labelled. Better than a rule that silently
        # never fires.
        unavailable = str(exc)
        enabled = False

    # Sorted and de-duplicated so the stored consent has one canonical form: the
    # fire-time check is a set comparison, and two rows granting the same powers in
    # a different order must not read as different consent.
    scope = sorted({s for s in (capability_scope or []) if isinstance(s, str) and s})

    aid = new_id()
    stamp = now()
    with db.transaction() as c:
        rev = db.next_rev(c)
        c.execute(
            """
            INSERT INTO automations
                (id, name, trigger_kind, schedule, action, enabled,
                 next_run_at, unavailable_reason, capability_scope,
                 created_at, updated_at, rev)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                aid,
                name,
                trigger_kind,
                schedule,
                dumps(action or {}),
                1 if enabled else 0,
                next_run,
                unavailable,
                dumps(scope),
                stamp,
                stamp,
                rev,
            ),
        )
    return {
        "id": aid,
        "name": name,
        "trigger_kind": trigger_kind,
        "schedule": schedule,
        "action": action or {},
        "enabled": enabled,
        "next_run_at": next_run,
        "unavailable_reason": unavailable,
        "capability_scope": scope,
        "created_at": stamp,
        "updated_at": stamp,
        "rev": rev,
    }


def _scope_of(row: dict[str, Any]) -> list[str]:
    """Decode ``capability_scope`` into a list of names, failing closed.

    Anything that is not a JSON array of non-empty strings decodes to ``[]`` -- an
    empty scope, which grants nothing. That is the direction a corrupt or
    hand-edited value has to fail in: a row whose consent cannot be read must not be
    treated as a row that consented (§5).
    """
    raw = loads(row.get("capability_scope"), [])
    if not isinstance(raw, list):
        return []
    return sorted({s for s in raw if isinstance(s, str) and s})


def get(automation_id: str) -> dict[str, Any] | None:
    row = to_dict(
        db.query_one("SELECT * FROM automations WHERE id = ?", (automation_id,))
    )
    if row:
        row["action"] = loads(row.get("action"), {})
        row["enabled"] = bool(row["enabled"])
        row["capability_scope"] = _scope_of(row)
    return row


def list_all() -> list[dict[str, Any]]:
    rows = to_list(db.query("SELECT * FROM automations ORDER BY name"))
    for row in rows:
        row["action"] = loads(row.get("action"), {})
        row["enabled"] = bool(row["enabled"])
        row["last_result"] = loads(row.get("last_result"), None)
        row["capability_scope"] = _scope_of(row)
    return rows


def update(
    automation_id: str,
    *,
    name: str | None = None,
    schedule: str | None = None,
    trigger_kind: str | None = None,
    action: dict[str, Any] | None = None,
    enabled: bool | None = None,
    capability_scope: list[str] | None = None,
) -> dict[str, Any] | None:
    current = get(automation_id)
    if current is None:
        return None

    fields: dict[str, Any] = {}
    if name is not None:
        fields["name"] = name
    if action is not None:
        fields["action"] = dumps(action)
    if trigger_kind is not None:
        if trigger_kind not in TRIGGER_KINDS:
            raise ValueError(f"trigger_kind must be one of {TRIGGER_KINDS}")
        fields["trigger_kind"] = trigger_kind
    if schedule is not None:
        fields["schedule"] = schedule
    if enabled is not None:
        fields["enabled"] = 1 if enabled else 0
    if capability_scope is not None:
        fields["capability_scope"] = dumps(
            sorted({s for s in capability_scope if isinstance(s, str) and s})
        )

    # Any change to timing must recompute next_run_at, or the row lies about when
    # it will fire.
    if schedule is not None or trigger_kind is not None or enabled is not None:
        kind = fields.get("trigger_kind", current["trigger_kind"])
        sched = fields.get("schedule", current["schedule"])
        want_enabled = bool(fields.get("enabled", 1 if current["enabled"] else 0))
        if want_enabled:
            try:
                parsed = parse_next_run(kind, sched)
                fields["next_run_at"] = parsed.isoformat() if parsed else None
                fields["unavailable_reason"] = None
            except ScheduleError as exc:
                fields["next_run_at"] = None
                fields["unavailable_reason"] = str(exc)
                fields["enabled"] = 0
        else:
            fields["next_run_at"] = None

    if not fields:
        return current

    assignments = ", ".join(f"{k} = ?" for k in fields)
    with db.transaction() as c:
        c.execute(
            f"UPDATE automations SET {assignments}, updated_at = ?, rev = ? WHERE id = ?",
            (*fields.values(), now(), db.next_rev(c), automation_id),
        )
    return get(automation_id)


def delete(automation_id: str) -> bool:
    with db.transaction() as c:
        rev = db.next_rev(c)
        cur = c.execute("DELETE FROM automations WHERE id = ?", (automation_id,))
        removed = (cur.rowcount or 0) > 0
        if removed:
            tombstone(c, ENTITY, automation_id, rev)
        return removed


def due(*, at: datetime | None = None) -> list[dict[str, Any]]:
    """Enabled automations whose next_run_at has passed."""
    moment = (at or datetime.now().astimezone()).isoformat()
    rows = to_list(
        db.query(
            """
            SELECT * FROM automations
            WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?
            ORDER BY next_run_at
            """,
            (moment,),
        )
    )
    for row in rows:
        row["action"] = loads(row.get("action"), {})
        row["enabled"] = True
        row["capability_scope"] = _scope_of(row)
    return rows


def start_run(automation_id: str) -> str:
    rid = new_id()
    with db.transaction() as c:
        c.execute(
            """
            INSERT INTO automation_runs (id, automation_id, started_at, status, detail)
            VALUES (?, ?, ?, 'running', '{}')
            """,
            (rid, automation_id, now()),
        )
    return rid


def finish_run(
    run_id: str,
    automation_id: str,
    *,
    status: str,
    detail: dict[str, Any] | None = None,
    reschedule: bool = True,
    denied_capability: str | None = None,
) -> None:
    """Close out a run and move next_run_at forward.

    ``status`` is one of running / completed / failed / skipped. A failure still
    reschedules -- a transient error should not silently retire an automation --
    but the failure is recorded and visible.

    ``denied_capability`` names the permission that stopped the run, when one did.
    It goes in its own column rather than only into ``detail`` so that "which rules
    are being refused, and for what" is one query instead of a JSON scan.
    """
    stamp = now()
    with db.transaction() as c:
        c.execute(
            """
            UPDATE automation_runs
            SET finished_at = ?, status = ?, detail = ?, denied_capability = ?
            WHERE id = ?
            """,
            (stamp, status, dumps(detail or {}), denied_capability, run_id),
        )
        c.execute(
            "UPDATE automations SET last_run_at = ?, last_result = ?, updated_at = ?, rev = ? WHERE id = ?",
            (stamp, dumps({"status": status, **(detail or {})}), stamp, db.next_rev(c), automation_id),
        )

    if not reschedule:
        return
    row = get(automation_id)
    if row is None or not row["enabled"]:
        return
    try:
        parsed = parse_next_run(row["trigger_kind"], row["schedule"])
        with db.transaction() as c:
            c.execute(
                "UPDATE automations SET next_run_at = ?, updated_at = ?, rev = ? WHERE id = ?",
                (
                    parsed.isoformat() if parsed else None,
                    now(),
                    db.next_rev(c),
                    automation_id,
                ),
            )
    except ScheduleError as exc:
        with db.transaction() as c:
            c.execute(
                """
                UPDATE automations
                SET enabled = 0, next_run_at = NULL, unavailable_reason = ?,
                    updated_at = ?, rev = ?
                WHERE id = ?
                """,
                (str(exc), now(), db.next_rev(c), automation_id),
            )


def mark_unavailable(automation_id: str, reason: str) -> dict[str, Any] | None:
    """Disable an automation that can never succeed, and say why.

    The third place this shape appears -- ``create`` and ``finish_run`` already do
    it inline for an unparseable schedule -- and the first that is reachable from
    outside the repo, because the other permanent failure is not a bad schedule but
    a bad *action*: a kind nothing can execute, or a required field that is missing.

    Clearing ``next_run_at`` is what makes this safe rather than cosmetic, and it is
    the reason this is not simply ``update(enabled=False)`` plus a note. ``due()``
    selects on ``next_run_at <= now``, so a permanently broken automation left with
    a past timestamp is re-selected on *every* tick: it fails, is rescheduled to the
    same past instant, and fails again, forever. Disabling without clearing the
    timestamp would swap a silent no-op for a hot loop.
    """
    with db.transaction() as c:
        cur = c.execute(
            """
            UPDATE automations
            SET enabled = 0, next_run_at = NULL, unavailable_reason = ?,
                updated_at = ?, rev = ?
            WHERE id = ?
            """,
            (reason, now(), db.next_rev(c), automation_id),
        )
        if not (cur.rowcount or 0):
            return None
    return get(automation_id)


def runs(automation_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    rows = to_list(
        db.query(
            """
            SELECT * FROM automation_runs
            WHERE automation_id = ?
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (automation_id, min(limit, 100)),
        )
    )
    for row in rows:
        row["detail"] = loads(row.get("detail"), {})
    return rows


def reconcile_running_runs() -> int:
    """A run marked 'running' in a fresh process was killed (§105)."""
    with db.transaction() as c:
        cur = c.execute(
            """
            UPDATE automation_runs
            SET status = 'failed', finished_at = ?,
                detail = json_set(COALESCE(NULLIF(detail, ''), '{}'), '$.error',
                                  'Interrupted by application restart')
            WHERE status = 'running'
            """,
            (now(),),
        )
        return cur.rowcount or 0
