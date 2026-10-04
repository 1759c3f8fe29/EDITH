"""Health, system status, and the honest-unavailability surface (§60, §110).

``/health`` exists so the desktop shell can tell three states apart that all look
like "nothing works" from the UI: the core is not running, the core is running but
its storage is broken, and the core is fine but has no credentials. §60 requires
each to produce a different screen, so each gets a different payload here.
"""

from __future__ import annotations

import os
import platform
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ...db import conn as db, maintenance, migrate
from ...logging_setup import diagnostics as diagnostics_buffer, get_logger, redact_data
from ...permissions import engine as permission_engine
from ...tools import registry

router = APIRouter(tags=["system"])
log = get_logger("api.system")

_STARTED = time.monotonic()


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    """Cheap enough for the shell to poll while waiting for the core to come up.

    Deliberately does not touch a provider: a health check that made a network
    call would report "unhealthy" for an offline machine whose local half is
    working perfectly, which is exactly the conflation §60 and §61 warn about.
    """
    # The storage verdict is taken now, not read back from startup. Reporting the
    # startup sample forever meant one bad moment -- and until the FTS5 fix in
    # db.conn, one *falsely* bad moment -- pinned this endpoint to "degraded" for
    # the life of the process while /api/storage, asking the same function, said
    # the database was fine. A health check that cannot change its mind is not
    # reporting health.
    ok, detail = db.integrity_check()
    # What happened at startup is still worth reporting, because it has a
    # consequence a live re-check cannot undo: the monitors, the memory sweep and
    # the embedding backfill are only started when startup found the database
    # sound, and nothing retries them afterwards. So a core whose storage reads
    # fine *now* but failed then is still degraded, and says which it is.
    startup_error = getattr(request.app.state, "storage_error", None)
    return {
        "status": "ok" if (ok and not startup_error) else "degraded",
        "uptime_seconds": round(time.monotonic() - _STARTED, 1),
        "storage": {
            "ok": ok,
            "detail": detail,
            "path": str(db.path()),
            "schema_version": migrate.current_version() if ok else None,
        },
        "startup_error": startup_error,
        "background_features_started": startup_error is None,
        "pid": os.getpid(),
        "platform": f"{platform.system()} {platform.release()}",
        "session_type": os.environ.get("XDG_SESSION_TYPE", "unknown"),
    }


@router.get("/api/startup")
def startup(request: Request) -> dict[str, Any]:
    """Everything the UI needs on first paint, in one round trip.

    One call rather than six, because a shell that renders six loading skeletons
    and fills them in over a second looks broken even when it is not (§56).
    """
    sup = getattr(request.app.state, "supervisor", None)
    tools = registry.all_tools()
    unavailable = [
        {"name": t.name, "title": t.title, "reason": t.unavailable_reason()}
        for t in tools
        if t.unavailable_reason()
    ]
    return {
        "health": health(request),
        "permissions": permission_engine.engine.startup(),
        "tools": {
            "total": len(tools),
            "available": len(tools) - len(unavailable),
            # Named, not counted: "8 tools unavailable" is not actionable, while
            # "xdotool is not installed" tells the user what to do (§78, §110).
            "unavailable": unavailable,
        },
        "watching": sup.watching() if sup else [],
        "session_type": os.environ.get("XDG_SESSION_TYPE", "unknown"),
    }


@router.get("/api/system/status")
async def system_status() -> dict[str, Any]:
    """Real machine state, through the tool layer so the read is still audited.

    ``system.info`` is an implicit capability, so this does not prompt -- but it
    still goes through the runtime rather than reading ``/proc`` here, so the call
    lands in the audit trail like every other one.
    """
    from ...tools import runtime

    result = await runtime.execute("system.status")
    if not result.ok:
        raise HTTPException(status_code=503, detail=result.error)
    # Scrubbed on the way out (§56, "tool output shown to the user"). Today this is
    # defence in depth and not a fix for a live leak: ``system.status`` reports host,
    # OS, uptime, CPU, memory, disks and interfaces, and none of those carry a
    # credential -- it does not list process command lines, which is where a
    # ``--password=`` would show up. It is scrubbed anyway because the guarantee that
    # matters is about the *boundary*, not about today's payload: the day this data
    # grows a field that quotes a command, the redaction is already here.
    return {
        "data": redact_data(result.data),
        "summary": result.summary,
        "verified": result.verified,
    }


@router.get("/api/diagnostics")
def diagnostics(limit: int = 200) -> dict[str, Any]:
    """The in-memory log ring, already secret-redacted at the filter (A9, §112)."""
    return {
        "records": diagnostics_buffer.dump(limit=limit),
        "database": maintenance.stats(),
    }


@router.get("/api/world")
def world() -> dict[str, Any]:
    """The fused current state the monitors observed (C2; §60, §78).

    Every domain here traces to a real ``observation.<kind>`` published on the
    bus, with the timestamp of the observation that produced it. A domain no
    monitor has spoken about is ``None`` -- not a plausible default -- and a
    fact older than the world model's staleness window carries ``stale: true``
    rather than being dropped, so a reader learns the fact is old instead of
    being left to guess.
    """
    from ...core import world as world_model

    return world_model.world_state()
