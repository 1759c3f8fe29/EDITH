"""Ambient event store (task L9; spec §36, §44, §70, §81).

Where the monitors' observations are kept so "what was I doing yesterday
afternoon?" has a real answer instead of a plausible-sounding invention.

This repository is deliberately unlike the others in three ways, each of which is
a consequence of the data rather than a style choice:

**It forgets on a schedule.** Every row is written with an ``expires_at`` derived
from the ``ambient_retention_days`` setting, and :func:`purge` deletes what is
past it. Nothing references ambient events, so nothing keeps them alive. A
watcher that accumulates forever is a surveillance log the user did not ask for;
one with a horizon is a memory of the last few days (§70, §81).

**It answers by time, not by text.** There is no full-text index. Window titles
and clipboard shapes are the most revealing text in the system, and indexing them
would put a second copy somewhere the retention purge would have to chase --
where any miss leaks content the user believed was gone. Queries here are "this
range, these kinds", which the indexes serve directly.

**It collapses conditions.** A battery that stays below 20% for an hour is one
fact, not twelve samples of a fact. :func:`timeline` folds consecutive rows that
share a ``dedupe_key`` into a single entry with an occurrence count and a span,
so the timeline reads like events rather than like polling.

Writes never raise. A monitor is a background loop with nobody to show an error
to; losing one row is a smaller failure than a watcher that dies because the disk
was full (§78).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from ...logging_setup import get_logger
from .. import conn as db
from .base import dumps, loads, new_id, now, to_list

log = get_logger("db.ambient")

Severity = Literal["info", "notice", "alert"]

#: Fallback when the setting cannot be read (no database yet, corrupt value).
#: A week is long enough to answer "what was I doing on Monday" and short enough
#: that a forgotten toggle does not become a year of window titles.
DEFAULT_RETENTION_DAYS = 7

#: Hard ceiling on a single timeline query, so a UI bug cannot ask for a million
#: rows and stall the event loop.
MAX_LIMIT = 2_000


def _retention_days() -> int:
    try:
        from . import app_settings

        value = int(app_settings.get("ambient_retention_days"))
    except Exception:
        log.debug("ambient retention setting unreadable", exc_info=True)
        return DEFAULT_RETENTION_DAYS
    # 0 is meaningful: "keep nothing beyond this session". Negative is not.
    return max(0, value)


def _expiry(observed_at: str) -> str | None:
    days = _retention_days()
    try:
        base = datetime.fromisoformat(observed_at)
    except ValueError:
        base = datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return (base + timedelta(days=days)).isoformat()


def record(
    monitor: str,
    kind: str,
    summary: str,
    *,
    data: dict[str, Any] | None = None,
    severity: Severity = "info",
    dedupe_key: str = "",
    clears: bool = False,
    observed_at: str | None = None,
    pinned: bool = False,
) -> dict[str, Any] | None:
    """Store one observation. Returns the row, or None if it could not be stored.

    ``pinned`` writes no expiry, which is the only way a row outlives retention.
    It exists for events the user explicitly keeps, and is never set by a monitor.
    """
    seen_at = observed_at or now()
    entry: dict[str, Any] = {
        "id": new_id(),
        "monitor": monitor,
        "kind": kind,
        "summary": summary,
        "data": data or {},
        "severity": severity,
        "dedupe_key": dedupe_key,
        "clears": clears,
        "observed_at": seen_at,
        "created_at": now(),
        "expires_at": None if pinned else _expiry(seen_at),
    }
    try:
        with db.transaction() as c:
            c.execute(
                """
                INSERT INTO ambient_events
                    (id, monitor, kind, summary, data, severity, dedupe_key,
                     clears, observed_at, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry["id"],
                    monitor,
                    kind,
                    summary,
                    dumps(entry["data"]),
                    severity,
                    dedupe_key,
                    1 if clears else 0,
                    seen_at,
                    entry["created_at"],
                    entry["expires_at"],
                ),
            )
    except Exception:
        # See the module docstring: a background watcher has nowhere to report
        # this, and stopping it would cost the user more than the lost row.
        log.warning("could not store ambient event %s/%s", monitor, kind, exc_info=True)
        return None
    return entry


def _hydrate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        row["data"] = loads(row.get("data"), {})
        row["clears"] = bool(row.get("clears"))
    return rows


def events(
    *,
    since: str | None = None,
    until: str | None = None,
    monitor: str | None = None,
    kinds: list[str] | None = None,
    severity: Severity | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Raw rows, newest first. Every filter is optional."""
    where: list[str] = []
    args: list[Any] = []
    if since:
        where.append("observed_at >= ?")
        args.append(since)
    if until:
        where.append("observed_at <= ?")
        args.append(until)
    if monitor:
        where.append("monitor = ?")
        args.append(monitor)
    if kinds:
        where.append("kind IN (" + ",".join("?" * len(kinds)) + ")")
        args.extend(kinds)
    if severity:
        where.append("severity = ?")
        args.append(severity)

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    args.append(max(1, min(int(limit), MAX_LIMIT)))
    rows = to_list(
        db.conn().execute(
            f"SELECT * FROM ambient_events{clause} ORDER BY observed_at DESC LIMIT ?",
            tuple(args),
        )
    )
    return _hydrate(rows)


def timeline(
    *,
    since: str | None = None,
    until: str | None = None,
    monitor: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Events with consecutive repeats of the same condition folded together.

    Returned newest first. A folded entry gains ``occurrences``, ``first_at`` and
    ``last_at``; ``observed_at`` stays the most recent sighting so ordering is
    unaffected. Rows with an empty ``dedupe_key`` describe a moment rather than a
    condition and are never folded -- two separate window switches are two
    events even when they look identical.
    """
    # Over-fetch, because folding reduces the count and the caller asked for
    # `limit` entries, not `limit` rows.
    raw = events(since=since, until=until, monitor=monitor, limit=min(limit * 4, MAX_LIMIT))

    folded: list[dict[str, Any]] = []
    for row in raw:
        key = row.get("dedupe_key") or ""
        previous = folded[-1] if folded else None
        if (
            key
            and previous is not None
            and previous.get("dedupe_key") == key
            and bool(previous.get("clears")) == bool(row.get("clears"))
        ):
            previous["occurrences"] += 1
            # `raw` is newest-first, so each further row is older than the last.
            previous["first_at"] = row["observed_at"]
            continue
        entry = dict(row)
        entry["occurrences"] = 1
        entry["last_at"] = row["observed_at"]
        entry["first_at"] = row["observed_at"]
        folded.append(entry)
        if len(folded) >= limit:
            break
    return folded


def counts(*, since: str | None = None, until: str | None = None) -> dict[str, Any]:
    """Per-monitor and per-kind totals, for the timeline screen's summary."""
    where: list[str] = []
    args: list[Any] = []
    if since:
        where.append("observed_at >= ?")
        args.append(since)
    if until:
        where.append("observed_at <= ?")
        args.append(until)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    c = db.conn()
    by_monitor = {
        row["monitor"]: row["n"]
        for row in c.execute(
            f"SELECT monitor, COUNT(*) AS n FROM ambient_events{clause} GROUP BY monitor",
            tuple(args),
        )
    }
    by_kind = {
        row["kind"]: row["n"]
        for row in c.execute(
            f"SELECT kind, COUNT(*) AS n FROM ambient_events{clause} "
            "GROUP BY kind ORDER BY n DESC",
            tuple(args),
        )
    }
    alerts = c.execute(
        f"SELECT COUNT(*) AS n FROM ambient_events{clause}"
        + (" AND" if clause else " WHERE")
        + " severity = 'alert'",
        tuple(args),
    ).fetchone()
    span = c.execute(
        f"SELECT MIN(observed_at) AS oldest, MAX(observed_at) AS newest "
        f"FROM ambient_events{clause}",
        tuple(args),
    ).fetchone()

    return {
        "total": sum(by_monitor.values()),
        "by_monitor": by_monitor,
        "by_kind": by_kind,
        "alerts": int(alerts["n"]) if alerts else 0,
        "oldest": span["oldest"] if span else None,
        "newest": span["newest"] if span else None,
        "retention_days": _retention_days(),
    }


def purge(*, before: str | None = None) -> int:
    """Delete expired events. Returns how many rows went.

    Called on startup and after each monitor tick batch. ``before`` overrides the
    cutoff, which is how "forget the last hour" is implemented -- a deliberate
    user action, not retention.
    """
    cutoff = before or now()
    try:
        with db.transaction() as c:
            cursor = c.execute(
                "DELETE FROM ambient_events WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (cutoff,),
            )
            removed = cursor.rowcount or 0
    except Exception:
        log.warning("ambient purge failed", exc_info=True)
        return 0
    if removed:
        log.info("purged %d expired ambient event(s)", removed)
    return removed


def forget(*, since: str | None = None, monitor: str | None = None) -> int:
    """Delete ambient events outright, ignoring retention and pinning.

    This is the user saying "forget what you saw". It is a hard delete with no
    tombstone: ambient events never sync, so there is no replica to inform, and
    recording the identity of a deleted observation would defeat the point of
    deleting it (§70).
    """
    where: list[str] = []
    args: list[Any] = []
    if since:
        where.append("observed_at >= ?")
        args.append(since)
    if monitor:
        where.append("monitor = ?")
        args.append(monitor)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    try:
        with db.transaction() as c:
            cursor = c.execute(f"DELETE FROM ambient_events{clause}", tuple(args))
            removed = cursor.rowcount or 0
    except Exception:
        log.warning("ambient forget failed", exc_info=True)
        return 0
    log.info(
        "forgot %d ambient event(s)",
        removed,
        extra={"since": since, "monitor": monitor},
    )
    return removed
