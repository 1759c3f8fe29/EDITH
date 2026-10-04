"""Permission surface: the catalogue, the grants, and answering a prompt (§45, §97).

The interesting property of this module is what it *cannot* do. There is no route
that grants a capability directly, because a grant is only ever the answer to a
request the engine itself raised -- an endpoint that could mint a grant with no
pending request would let a compromised UI (or a model that talked the UI into a
fetch) authorise itself. ``respond`` therefore takes a request id the engine
issued, and the engine rejects an id it does not recognise.

Revocation *is* exposed, because taking permission away must never require a
prompt to hang off (§45).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from ...db.repos import permissions_store
from ...logging_setup import get_logger
from ...permissions import capabilities as caps
from ...permissions.engine import engine

router = APIRouter(prefix="/api/permissions", tags=["permissions"])
log = get_logger("api.permissions")


@router.get("")
def overview() -> dict[str, Any]:
    """Everything the Permissions screen renders, grouped as the user reads it."""
    return {
        "groups": [
            {
                "name": group,
                "capabilities": [caps.to_dict(c) for c in members],
            }
            for group, members in caps.by_group().items()
        ],
        "grants": permissions_store.list_grants(),
        "session_grants": engine.session_grants(),
        "pending": engine.pending(),
        "overview": engine.overview(),
    }


@router.get("/pending")
def pending() -> dict[str, Any]:
    """Requests waiting for an answer right now.

    The WebSocket is the primary channel for these; this route exists so a UI that
    reconnects can recover the current set instead of showing an empty queue while
    a tool is genuinely blocked waiting (§97).
    """
    return {"pending": engine.pending()}


@router.post("/{request_id}/respond")
def respond(
    request_id: str,
    choice: str = Body(..., embed=True),
    constraints: dict[str, Any] | None = Body(None, embed=True),
) -> dict[str, Any]:
    """Deliver the user's decision.

    Returns the engine's own verdict rather than a bare 200. A request that already
    timed out returns ``ok: false`` with a sentence, because a UI that reported
    "allowed" for a click that arrived too late would be claiming an action
    happened when it did not (§79).
    """
    result = engine.respond(request_id, choice, constraints=constraints)
    if result.get("ok"):
        return result

    # Two very different failures, and the UI should react differently to each,
    # so they must not share a status code. The engine only drops a request from
    # the pending set when it accepts an answer, so "still pending after a
    # failure" means the *choice* was wrong (a fixable 400: offer the right
    # buttons), and "gone" means the window closed while the user was deciding
    # (a 409: stop showing the dialog).
    still_waiting = any(p["id"] == request_id for p in engine.pending())
    raise HTTPException(
        status_code=400 if still_waiting else 409,
        detail=result.get("reason", "Could not record that answer."),
    )


@router.post("/revoke")
def revoke(
    capability: str = Body(..., embed=True),
    scope: str = Body("*", embed=True),
) -> dict[str, Any]:
    removed = engine.revoke(capability, scope)
    return {
        "ok": removed,
        "message": (
            f"{caps.get(capability).title} is no longer allowed."
            if removed
            else "There was no grant to remove."
        ),
    }


@router.post("/revoke-all")
def revoke_all() -> dict[str, Any]:
    counts = engine.revoke_all()
    return {
        "ok": True,
        "counts": counts,
        "message": "Every permission has been withdrawn. NEXUS will ask again the next time it needs one.",
    }


@router.post("/clear-session")
def clear_session() -> dict[str, Any]:
    cleared = engine.clear_session()
    return {
        "ok": True,
        "cleared": cleared,
        "message": f"{cleared} session permission(s) dropped." if cleared else "No session permissions were active.",
    }


@router.post("/{request_id}/cancel")
def cancel(request_id: str) -> dict[str, Any]:
    """Withdraw a request without answering it.

    Dismissing the dialog is not the same as denying: the caller is told the
    request was withdrawn, and no "deny" grant is written that would silently
    block the same action later (§45).
    """
    if not engine.cancel_pending(request_id):
        raise HTTPException(status_code=409, detail="That request is no longer waiting for an answer.")
    return {"ok": True, "message": "Request withdrawn."}


@router.get("/invocations")
def invocations(limit: int = 100, tool: str | None = None) -> dict[str, Any]:
    """The audit trail: what actually ran, with what, and whether it was verified.

    §46 makes the log the other half of the leash -- permission decides whether
    something may happen, this records that it did. Both halves have to be
    readable or the promise is only half kept.
    """
    return {
        "invocations": permissions_store.list_invocations(limit=limit, tool=tool),
        "stats": permissions_store.invocation_stats(tool=tool),
    }
