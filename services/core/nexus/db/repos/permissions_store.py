"""Persistence for permission grants and tool invocations (task B3, C-phase store).

Grants are stored, not inferred. The engine in ``nexus.permissions.engine`` asks
this module what the user has already decided; if there is no row, the answer is
"ask", never "allow" (§5, §7).

``once`` grants are never written here -- they are consumed in-process, so a
restart cannot resurrect them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .. import conn as db
from .base import dumps, loads, new_id, now, to_dict, to_list, tombstone

ENTITY = "permission"

GRANT_KINDS = ("deny", "session", "always", "constrained")


# ------------------------------------------------------------------- grants


def upsert_grant(
    capability: str,
    *,
    scope: str = "*",
    grant_kind: str = "always",
    constraints: dict[str, Any] | None = None,
    ttl_minutes: int | None = None,
) -> dict[str, Any]:
    if grant_kind not in GRANT_KINDS:
        raise ValueError(f"grant_kind must be one of {GRANT_KINDS}, got {grant_kind!r}")
    stamp = now()
    expires_at: str | None = None
    if grant_kind == "session" and ttl_minutes:
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=int(ttl_minutes))
        ).isoformat()
    with db.transaction() as c:
        rev = db.next_rev(c)
        c.execute(
            """
            INSERT INTO permissions
                (id, capability, scope, grant_kind, constraints,
                 granted_at, expires_at, updated_at, rev)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(capability, scope) DO UPDATE SET
                grant_kind  = excluded.grant_kind,
                constraints = excluded.constraints,
                granted_at  = excluded.granted_at,
                expires_at  = excluded.expires_at,
                updated_at  = excluded.updated_at,
                rev         = excluded.rev
            """,
            (
                new_id(),
                capability,
                scope,
                grant_kind,
                dumps(constraints or {}),
                stamp,
                expires_at,
                stamp,
                rev,
            ),
        )
    return {
        "capability": capability,
        "scope": scope,
        "grant_kind": grant_kind,
        "constraints": constraints or {},
        "granted_at": stamp,
        "expires_at": expires_at,
        "rev": rev,
    }


def find_grant(capability: str, scope: str = "*") -> dict[str, Any] | None:
    """Exact-scope lookup, then the wildcard fallback.

    Scope matching is intentionally narrow: a grant for ``~/Documents`` must not
    imply a grant for ``/etc``. Broader path logic lives in the policy layer,
    which understands what a path *is*; this module only stores decisions.
    """
    for candidate in (scope, "*"):
        row = to_dict(
            db.query_one(
                "SELECT * FROM permissions WHERE capability = ? AND scope = ?",
                (capability, candidate),
            )
        )
        if row is None:
            continue
        row["constraints"] = loads(row.get("constraints"), {})
        if _expired(row):
            revoke(capability, candidate)
            continue
        return row
    return None


def _expired(row: dict[str, Any]) -> bool:
    raw = row.get("expires_at")
    if not raw:
        return False
    try:
        return datetime.fromisoformat(raw) <= datetime.now(timezone.utc)
    except ValueError:
        # An unparseable expiry is treated as expired: fail closed (§5).
        return True


def list_grants() -> list[dict[str, Any]]:
    rows = to_list(
        db.query("SELECT * FROM permissions ORDER BY capability, scope")
    )
    for row in rows:
        row["constraints"] = loads(row.get("constraints"), {})
        row["expired"] = _expired(row)
    return rows


def revoke(capability: str, scope: str = "*") -> bool:
    with db.transaction() as c:
        rev = db.next_rev(c)
        row = c.execute(
            "SELECT id FROM permissions WHERE capability = ? AND scope = ?",
            (capability, scope),
        ).fetchone()
        cur = c.execute(
            "DELETE FROM permissions WHERE capability = ? AND scope = ?",
            (capability, scope),
        )
        removed = (cur.rowcount or 0) > 0
        if removed and row is not None:
            tombstone(c, ENTITY, row["id"], rev)
        return removed


def revoke_all() -> int:
    """'Revoke everything' from the Permissions screen (§43)."""
    with db.transaction() as c:
        rows = c.execute("SELECT id FROM permissions").fetchall()
        cur = c.execute("DELETE FROM permissions")
        for row in rows:
            tombstone(c, ENTITY, row["id"], db.next_rev(c))
        return cur.rowcount or 0


def clear_session_grants() -> int:
    """Called at startup: session grants must not survive a restart (§7)."""
    with db.transaction() as c:
        cur = c.execute("DELETE FROM permissions WHERE grant_kind = 'session'")
        return cur.rowcount or 0


# ---------------------------------------------------------- tool invocations


def record_invocation(
    tool: str,
    params: dict[str, Any],
    *,
    ok: bool,
    # None means "this tool has no verifier", which is not the same as False.
    verified: bool | None = None,
    result: Any = None,
    error: str | None = None,
    duration_ms: int | None = None,
    task_id: str | None = None,
    step_id: str | None = None,
) -> str:
    """Audit row for every tool call, successful or not (§36, §60)."""
    iid = new_id()
    with db.transaction() as c:
        c.execute(
            """
            INSERT INTO tool_invocations
                (id, tool, params, task_id, step_id, ok, verified,
                 result, error, duration_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                iid,
                tool,
                dumps(params),
                task_id,
                step_id,
                1 if ok else 0,
                None if verified is None else (1 if verified else 0),
                dumps(result) if result is not None else None,
                error,
                duration_ms,
                now(),
            ),
        )
    return iid


def list_invocations(*, limit: int = 100, tool: str | None = None) -> list[dict[str, Any]]:
    params: list[Any] = []
    where = ""
    if tool:
        where = "WHERE tool = ?"
        params.append(tool)
    params.append(min(limit, 500))
    rows = to_list(
        db.query(
            f"SELECT * FROM tool_invocations {where} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        )
    )
    for row in rows:
        row["params"] = loads(row.get("params"), {})
        row["result"] = loads(row.get("result"), None)
    return rows


def invocation_stats(*, tool: str | None = None) -> dict[str, Any]:
    """Real success/verification rates for the diagnostics screen (§66)."""
    params: tuple[Any, ...] = (tool,) if tool else ()
    where = "WHERE tool = ?" if tool else ""
    row = db.query_one(
        f"""
        SELECT COUNT(*) AS total,
               SUM(ok) AS succeeded,
               SUM(CASE WHEN verified = 1 THEN 1 ELSE 0 END) AS verified,
               AVG(duration_ms) AS avg_ms
        FROM tool_invocations {where}
        """,
        params,
    )
    if row is None:
        return {"total": 0, "succeeded": 0, "verified": 0, "avg_ms": None}
    return {
        "total": int(row["total"] or 0),
        "succeeded": int(row["succeeded"] or 0),
        "verified": int(row["verified"] or 0),
        "avg_ms": round(row["avg_ms"], 1) if row["avg_ms"] is not None else None,
    }


def prune_invocations(keep_days: int) -> int:
    if keep_days <= 0:
        return 0
    with db.transaction() as c:
        cur = c.execute(
            "DELETE FROM tool_invocations WHERE created_at < datetime('now', ?)",
            (f"-{int(keep_days)} days",),
        )
        return cur.rowcount or 0
