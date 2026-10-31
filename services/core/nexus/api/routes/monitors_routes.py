"""Awareness surface: what is watching, and turning it on or off (§39, L5).

Two properties this module preserves, because getting either wrong would make
the standing "you are being watched" indicator untrustworthy:

* **The master switch is separate from the per-monitor flags.** ``monitor_enabled``
  is off by default, and while it is off nothing observes regardless of the
  individual toggles. So ``monitor_screen_enabled: true`` does *not* mean the
  screen is being read, and this module never implies it does -- ``watching`` is
  computed from tasks that are actually running, not from settings.
* **A monitor that cannot run says so.** ``apply_settings`` reports
  ``"unavailable"`` for a monitor whose binary is missing, and that value reaches
  the UI unchanged. A toggle that flips on and then quietly observes nothing is
  the fake-feature failure §80 forbids.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request

from ...db.repos import ambient, app_settings
from ...logging_setup import get_logger

router = APIRouter(prefix="/api/monitors", tags=["awareness"])
log = get_logger("api.monitors")

#: The settings this screen owns. Kept explicit so a PATCH here cannot reach a
#: setting that belongs to another screen (a typo'd key would otherwise write a
#: value nothing reads, and look like it worked).
_OWNED_PREFIXES = ("monitor_", "ambient_")


def _sup(request: Request):
    sup = getattr(request.app.state, "supervisor", None)
    if sup is None:
        raise HTTPException(status_code=503, detail="The awareness supervisor is not running.")
    return sup


def _type_ok(default: Any, value: Any) -> bool:
    """Does ``value`` match the shape of the setting's default?

    ``bool`` is checked before ``int`` because in Python ``bool`` *is* an ``int``,
    so a plain isinstance check would let ``monitor_screen_interval_seconds: true``
    through -- and it would then be stored, read back as 1, and turn a five-minute
    interval into a one-second screenshot loop.
    """
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    return True


@router.get("")
def overview(request: Request) -> dict[str, Any]:
    """Monitor states, plus the settings that govern them, in one payload."""
    sup = _sup(request)
    settings = {
        key: value
        for key, value in app_settings.get_all().items()
        if key.startswith(_OWNED_PREFIXES)
    }
    return {
        **sup.status(),
        "settings": settings,
        # Counts, not events: the timeline is its own paged route, and loading
        # every observation to render a summary card would be slow for no reason.
        "ambient_counts": ambient.counts(),
    }


@router.get("/watching")
def watching(request: Request) -> dict[str, Any]:
    """Only what is observing *right now*.

    Small and cheap on purpose: the indicator that tells the user they are being
    watched must be able to refresh often, and must never be a cached guess (L5).
    """
    sup = _sup(request)
    return {"watching": sup.watching(), "any": sup.is_watching_user()}


@router.patch("/settings")
async def update_settings(
    request: Request,
    values: dict[str, Any] = Body(..., embed=True),
) -> dict[str, Any]:
    """Write awareness settings and reconcile the running monitors immediately.

    The reconcile is not optional and not deferred to a restart: a toggle whose
    effect arrives later is indistinguishable from one that did nothing.
    """
    rejected = [key for key in values if not key.startswith(_OWNED_PREFIXES)]
    if rejected:
        raise HTTPException(
            status_code=400,
            detail=f"Not awareness settings: {', '.join(sorted(rejected))}. Use /api/settings for those.",
        )
    # Checked against DEFAULTS rather than just written: ``set_many`` will happily
    # store a misspelled key, and nothing would ever read it. The toggle would
    # appear to save and change nothing -- §80's fake feature, arrived at by typo.
    unknown = [key for key in values if key not in app_settings.DEFAULTS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown setting(s): {', '.join(sorted(unknown))}.",
        )
    wrong_type = sorted(k for k, v in values.items() if not _type_ok(app_settings.DEFAULTS[k], v))
    if wrong_type:
        expected = ", ".join(
            f"{key} expects {type(app_settings.DEFAULTS[key]).__name__}" for key in wrong_type
        )
        raise HTTPException(status_code=400, detail=f"Wrong type: {expected}.")

    app_settings.set_many(values)
    changes = await _sup(request).apply_settings()
    unavailable = [mid for mid, outcome in changes.items() if outcome == "unavailable"]
    return {
        "ok": True,
        "changes": changes,
        # Surfaced separately because it is the one outcome that needs a sentence
        # in the UI rather than a state change: the user asked for something the
        # machine cannot currently do.
        "unavailable": unavailable,
        "status": _sup(request).status(),
    }


@router.get("/timeline")
def timeline(
    limit: int = 100,
    monitor: str | None = None,
    kind: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """What was observed, most recent first (§39: the user can review and delete)."""
    return {
        "events": ambient.events(
            limit=limit,
            monitor=monitor,
            # The repo filters on a list; a single ``kind`` query param is what a
            # URL naturally carries, so it is widened here rather than making
            # every caller build an array.
            kinds=[kind] if kind else None,
            since=since,
            until=until,
        ),
        "counts": ambient.counts(since=since, until=until),
    }


@router.get("/grouped")
def grouped(limit: int = 200, monitor: str | None = None, since: str | None = None) -> dict[str, Any]:
    """The same observations collapsed into sessions, which is how a day reads."""
    return {"timeline": ambient.timeline(limit=limit, monitor=monitor, since=since)}


@router.post("/forget")
def forget(
    since: str | None = Body(None, embed=True),
    monitor: str | None = Body(None, embed=True),
    everything: bool = Body(False, embed=True),
) -> dict[str, Any]:
    """Delete observations. §39 requires this to be a real delete, not a hide.

    ``everything`` has to be asked for explicitly. An unqualified POST wiping the
    whole timeline would make an accidental click unrecoverable, and ambient
    events are hard-deleted with no tombstone -- there is nothing to undo from.
    """
    if not everything and since is None and monitor is None:
        raise HTTPException(
            status_code=400,
            detail="Say what to forget: a cutoff time, a monitor, or everything: true.",
        )
    removed = ambient.forget(since=since, monitor=monitor)
    return {
        "ok": True,
        "removed": removed,
        "message": f"{removed} observation(s) deleted." if removed else "There was nothing matching to delete.",
    }


@router.get("/screen/frames")
def screen_frames(request: Request) -> dict[str, Any]:
    """Frames captured by the screen monitor, newest first.

    Returns the frames currently on disk for the timeline view.
    """
    sup = _sup(request)
    screen_monitor = sup.get("screen")
    if screen_monitor is None:
        return {"frames": [], "available": False, "reason": "Screen monitor not registered"}
    if not screen_monitor.state.running:
        return {"frames": [], "available": False, "reason": "Screen monitor not running"}
    return {"frames": screen_monitor.frames(), "available": True}
