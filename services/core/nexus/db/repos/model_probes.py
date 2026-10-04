"""What this API key can actually call (spec §13, §78).

A provider listing a model and your key being able to call it are two different
facts. NVIDIA's hosted listing names ``nvidia/cosmos-reason2-8b``; calling it with
this project's key returns ``404 Function not found for account``. The model is
real and deployed -- it just is not enabled here. Nothing in the listing hints at
that, and there is no way to find out except to ask.

This module stores the answer so the question is asked once. The alternative is a
router that re-learns the same 404 on every request, making the user wait through
a round-trip to a model that was never going to work.

The important distinction is ``durable``:

* **Durable** -- 404 not-found, authentication rejected. Nothing about waiting
  will change it; the user has to enable the model, change keys, or pick something
  else. Safe to route around.
* **Transient** -- timeout, rate limit, overload, network. Recorded so the
  Settings screen can show it, but it must *never* remove a model from routing. A
  cold NIM model has been measured taking over 100 seconds to first token, and a
  model that was slow once is not a model that is broken. Permanently benching it
  would quietly shrink the bench with nothing on screen to explain why.

That asymmetry is the whole point of the table. Recording "it failed" would be
easy and would make the router worse.
"""

from __future__ import annotations

from typing import Any

from .. import conn as db
from .base import now, to_list

#: Error kinds that will not fix themselves. These are the ``kind`` strings the
#: provider layer actually raises (``nexus.providers.base``), not invented names:
#: a 404 becomes ``model_unavailable`` and a 401/403 becomes
#: ``authentication_failed``.
#:
#: Anything not in here is treated as transient, because the cost of being wrong
#: runs one way: wrongly calling a failure durable removes a working model from
#: the bench, while wrongly calling it transient only costs one retry.
#:
#: ``request_rejected`` (HTTP 400) is deliberately absent. A 400 nearly always
#: means the *request* was shaped wrong -- sending a text segment to an image-only
#: OCR endpoint earns one -- and benching a good model because a probe payload was
#: malformed is precisely the mistake this asymmetry exists to prevent.
#: ``quota_exceeded`` is absent too: quotas reset.
DURABLE_KINDS: frozenset[str] = frozenset(
    {"model_unavailable", "authentication_failed", "missing_credential"}
)


def is_durable(error_kind: str | None) -> bool:
    return (error_kind or "") in DURABLE_KINDS


def record(
    key: str,
    *,
    provider: str,
    model: str,
    transport: str = "chat",
    ok: bool,
    error_kind: str | None = None,
    detail: str = "",
    latency_ms: int | None = None,
) -> dict[str, Any]:
    """Store the outcome of one real call to one model.

    Upsert rather than append: only the latest answer matters, and a growing log
    of every probe would need pruning to stay useful. ``durable`` is derived here
    rather than passed in so a caller cannot accidentally bench a model by
    mislabelling a timeout.
    """
    durable = 1 if (not ok and is_durable(error_kind)) else 0
    stamp = now()
    with db.transaction() as c:
        c.execute(
            """
            INSERT INTO model_probes
                (key, provider, model, transport, ok, error_kind, durable, detail,
                 latency_ms, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                provider   = excluded.provider,
                model      = excluded.model,
                transport  = excluded.transport,
                ok         = excluded.ok,
                error_kind = excluded.error_kind,
                durable    = excluded.durable,
                detail     = excluded.detail,
                latency_ms = excluded.latency_ms,
                checked_at = excluded.checked_at
            """,
            (
                key, provider, model, transport, 1 if ok else 0,
                error_kind, durable, detail[:500], latency_ms, stamp,
            ),
        )
    return {
        "key": key, "ok": ok, "error_kind": error_kind, "durable": bool(durable),
        "detail": detail, "latency_ms": latency_ms, "checked_at": stamp,
    }


def get(key: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM model_probes WHERE key = ?", (key,))
    return dict(row) if row else None


def all_probes() -> list[dict[str, Any]]:
    return to_list(
        db.query("SELECT * FROM model_probes ORDER BY ok DESC, provider, model")
    )


def unusable() -> dict[str, str]:
    """Models known to be uncallable for a reason that will not change.

    Returns ``key -> a sentence the user can act on``. The router consults this to
    skip a candidate, and the sentence it stores is the one shown as the skip
    reason -- so the explanation on screen is the provider's actual answer, not a
    guess reconstructed later.
    """
    rows = db.query(
        "SELECT key, error_kind, detail, checked_at FROM model_probes "
        "WHERE ok = 0 AND durable = 1"
    )
    out: dict[str, str] = {}
    for row in rows:
        kind = row["error_kind"] or "provider_error"
        when = str(row["checked_at"] or "")[:10]
        if kind == "model_unavailable":
            reason = (
                "Your account cannot call this model -- the provider lists it but "
                "answers 404 for this key. Enable it on the provider's site, or "
                "leave it and NEXUS will use the next model in the chain."
            )
        elif kind == "authentication_failed":
            reason = "The key for this provider was rejected. Check it in Settings."
        elif kind == "missing_credential":
            reason = "There is no key configured for this provider yet."
        else:
            reason = str(row["detail"] or "The provider refused this model.")
        out[str(row["key"])] = f"{reason} (checked {when})"
    return out


def stats() -> dict[str, Any]:
    row = db.query_one(
        """
        SELECT COUNT(*) AS total,
               SUM(ok) AS callable,
               SUM(CASE WHEN ok = 0 AND durable = 1 THEN 1 ELSE 0 END) AS blocked,
               SUM(CASE WHEN ok = 0 AND durable = 0 THEN 1 ELSE 0 END) AS flaky,
               MAX(checked_at) AS last_checked
        FROM model_probes
        """
    )
    if row is None:
        return {"total": 0, "callable": 0, "blocked": 0, "flaky": 0, "last_checked": None}
    return {
        "total": int(row["total"] or 0),
        "callable": int(row["callable"] or 0),
        "blocked": int(row["blocked"] or 0),
        "flaky": int(row["flaky"] or 0),
        "last_checked": row["last_checked"],
    }


def clear(key: str | None = None) -> int:
    """Forget probe results, so the next request measures again.

    Used when the user says they have enabled a model: the stored 404 is now
    stale, and refusing to re-check it would make the app permanently wrong about
    something the user just fixed.
    """
    with db.transaction() as c:
        if key:
            cur = c.execute("DELETE FROM model_probes WHERE key = ?", (key,))
        else:
            cur = c.execute("DELETE FROM model_probes")
        return int(cur.rowcount or 0)
