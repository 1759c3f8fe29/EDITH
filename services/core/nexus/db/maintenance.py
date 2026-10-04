"""Export, import, retention and vacuum (tasks B9, B10; spec §71, §72).

Export is a real, reimportable JSON document -- not a formatted summary. What
comes out of ``export_all`` can be fed straight into ``import_all`` on a fresh
database and produce the same content.

Deliberate omissions from the export, both documented in the payload's
``excluded`` field so the user is never misled about what they are holding:

* **Credentials.** They live in the OS keyring, not the database (§65).
* **Embeddings.** They are derived data, regenerable from the text, and would
  multiply the file size for no recoverable information.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from . import conn as db
from .migrate import current_version
from .repos import app_settings, search

log = get_logger("db.maintenance")

EXPORT_FORMAT = 1

# Order matters on import: parents before children, because foreign keys are ON.
EXPORT_TABLES: tuple[str, ...] = (
    "projects",
    "conversations",
    "messages",
    "memories",
    "tasks",
    "task_steps",
    "tool_invocations",
    "activity",
    "permissions",
    "devices",
    "device_capabilities",
    "automations",
    "automation_runs",
    "settings",
    "tombstones",
)

EXCLUDED = (
    "api credentials (stored in the OS keyring, never in the database)",
    "memory embeddings (derived data, regenerated on demand)",
)


class ImportError_(RuntimeError):
    """Raised when an import payload cannot be trusted."""


def export_all() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "format": EXPORT_FORMAT,
        "schema_version": current_version(),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "excluded": list(EXCLUDED),
        "tables": {},
        "counts": {},
    }
    for table in EXPORT_TABLES:
        rows = [dict(r) for r in db.query(f"SELECT * FROM {table}")]
        payload["tables"][table] = rows
        payload["counts"][table] = len(rows)
    return payload


def export_to_file(target: Path) -> dict[str, Any]:
    payload = export_all()
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file then rename, so an interrupted export cannot leave a
    # truncated file that looks like a valid backup.
    tmp = target.with_suffix(target.suffix + ".partial")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(target)
    total = sum(payload["counts"].values())
    log.info("export complete", extra={"path": str(target), "rows": total})
    return {
        "path": str(target),
        "rows": total,
        "counts": payload["counts"],
        "bytes": target.stat().st_size,
        "excluded": list(EXCLUDED),
    }


def inspect_import(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a payload and describe what importing it would do.

    Returned before anything is written so the confirmation dialog can state the
    real effect (§62: no destructive action without an accurate preview).
    """
    if not isinstance(payload, dict):
        raise ImportError_("payload is not a JSON object")
    fmt = payload.get("format")
    if fmt != EXPORT_FORMAT:
        raise ImportError_(
            f"unsupported export format {fmt!r}; this build reads format {EXPORT_FORMAT}"
        )
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        raise ImportError_("payload has no 'tables' object")

    unknown = sorted(set(tables) - set(EXPORT_TABLES))
    incoming = {t: len(tables.get(t) or []) for t in EXPORT_TABLES}
    existing = {t: _count(t) for t in EXPORT_TABLES}

    return {
        "schema_version": payload.get("schema_version"),
        "exported_at": payload.get("exported_at"),
        "incoming": incoming,
        "existing": existing,
        "unknown_tables": unknown,
        "incoming_total": sum(incoming.values()),
        "existing_total": sum(existing.values()),
    }


def import_all(payload: dict[str, Any], *, mode: str = "merge") -> dict[str, Any]:
    """Import an export payload.

    ``mode``:
      * ``merge``   -- keep existing rows; skip any incoming row whose primary
                       key already exists. Nothing is overwritten.
      * ``replace`` -- delete existing rows in the exported tables first.

    The whole import is one transaction: on any failure the database is exactly
    as it was, and the error names the table and row that broke (§103).
    """
    if mode not in ("merge", "replace"):
        raise ImportError_(f"mode must be 'merge' or 'replace', got {mode!r}")

    report = inspect_import(payload)
    tables: dict[str, list[dict[str, Any]]] = payload["tables"]
    inserted: dict[str, int] = {}
    skipped: dict[str, int] = {}

    with db.transaction() as c:
        if mode == "replace":
            for table in reversed(EXPORT_TABLES):
                c.execute(f"DELETE FROM {table}")

        for table in EXPORT_TABLES:
            rows = tables.get(table) or []
            if not rows:
                inserted[table] = 0
                skipped[table] = 0
                continue

            columns = _columns(c, table)
            added = 0
            passed = 0
            for row in rows:
                usable = {k: v for k, v in row.items() if k in columns}
                if not usable:
                    passed += 1
                    continue
                placeholders = ",".join("?" * len(usable))
                names = ",".join(usable)
                try:
                    cur = c.execute(
                        f"INSERT OR IGNORE INTO {table} ({names}) VALUES ({placeholders})",
                        tuple(usable.values()),
                    )
                except Exception as exc:  # noqa: BLE001 - re-raised with context
                    raise ImportError_(
                        f"row in table {table!r} could not be imported: {exc}"
                    ) from exc
                if cur.rowcount:
                    added += 1
                else:
                    passed += 1
            inserted[table] = added
            skipped[table] = passed

        # sync_seq must end up above every imported rev, or the next local write
        # would reuse a revision number that already exists.
        highest = 0
        for table in ("projects", "conversations", "messages", "memories", "tasks",
                      "permissions", "devices", "automations", "settings"):
            row = c.execute(f"SELECT MAX(rev) AS m FROM {table}").fetchone()
            if row and row["m"] is not None:
                highest = max(highest, int(row["m"]))
        c.execute("UPDATE sync_seq SET value = MAX(value, ?) WHERE id = 1", (highest,))

    # Triggers do not fire for INSERT OR IGNORE rows that were ignored, and an
    # imported row's index entry may be stale, so rebuild rather than assume.
    search.rebuild()

    total_inserted = sum(inserted.values())
    log.info(
        "import complete",
        extra={"mode": mode, "inserted": total_inserted, "skipped": sum(skipped.values())},
    )
    return {
        "mode": mode,
        "inserted": inserted,
        "skipped": skipped,
        "inserted_total": total_inserted,
        "skipped_total": sum(skipped.values()),
        "source": {"schema_version": report["schema_version"], "exported_at": report["exported_at"]},
        "search_index_rows": search.index_size(),
    }


def import_from_file(source: Path, *, mode: str = "merge") -> dict[str, Any]:
    source = Path(source)
    if not source.exists():
        raise ImportError_(f"file not found: {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ImportError_(f"could not read {source}: {exc}") from exc
    return import_all(payload, mode=mode)


# ------------------------------------------------------------------ retention


def retention_sweep(*, dry_run: bool = False) -> dict[str, Any]:
    """Apply the retention settings (task B10).

    A value of 0 means "keep forever" and is skipped, so the default
    configuration never deletes anything the user did not ask to be deleted.
    """
    cfg = app_settings.get_all()
    plan = {
        "activity": int(cfg.get("retain_activity_days") or 0),
        "tool_invocations": int(cfg.get("retain_tool_invocations_days") or 0),
        "conversations": int(cfg.get("retain_conversations_days") or 0),
    }

    result: dict[str, Any] = {"plan": plan, "removed": {}, "dry_run": dry_run}

    for table, days in plan.items():
        if days <= 0:
            result["removed"][table] = 0
            continue
        cutoff = f"-{days} days"
        if table == "conversations":
            # Only conversations with no pinned memory pointing at them, and never
            # one the user archived deliberately.
            sql_count = """
                SELECT COUNT(*) AS n FROM conversations
                WHERE updated_at < datetime('now', ?)
                  AND archived = 0
                  AND id NOT IN (
                      SELECT source_conversation_id FROM memories
                      WHERE source_conversation_id IS NOT NULL
                  )
            """
            sql_delete = sql_count.replace("SELECT COUNT(*) AS n", "DELETE")
        else:
            sql_count = f"SELECT COUNT(*) AS n FROM {table} WHERE created_at < datetime('now', ?)"
            sql_delete = f"DELETE FROM {table} WHERE created_at < datetime('now', ?)"

        row = db.query_one(sql_count, (cutoff,))
        count = int(row["n"]) if row else 0
        if dry_run or count == 0:
            result["removed"][table] = count
            continue
        with db.transaction() as c:
            cur = c.execute(sql_delete, (cutoff,))
            result["removed"][table] = cur.rowcount or 0

    total = sum(result["removed"].values())
    if total and not dry_run:
        log.info("retention sweep removed rows", extra={"removed": result["removed"]})
    return result


def vacuum() -> dict[str, Any]:
    """Reclaim space after deletions. Cannot run inside a transaction."""
    before = db.path().stat().st_size if db.path().exists() else 0
    c = db.conn()
    c.execute("VACUUM")
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    after = db.path().stat().st_size if db.path().exists() else 0
    return {"bytes_before": before, "bytes_after": after, "reclaimed": max(0, before - after)}


def stats() -> dict[str, Any]:
    """Row counts and file size for the diagnostics screen (§66)."""
    counts = {table: _count(table) for table in EXPORT_TABLES}
    counts["memory_vectors"] = _count("memory_vectors")
    counts["search_index"] = search.index_size()
    ok, detail = db.integrity_check()
    file_path = db.path()
    return {
        "path": str(file_path),
        "bytes": file_path.stat().st_size if file_path.exists() else 0,
        "schema_version": current_version(),
        "integrity_ok": ok,
        "integrity_detail": detail,
        "counts": counts,
    }


def _count(table: str) -> int:
    row = db.query_one(f"SELECT COUNT(*) AS n FROM {table}")
    return int(row["n"]) if row else 0


def _columns(c: Any, table: str) -> set[str]:
    return {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
