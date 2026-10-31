"""HTTP + WebSocket surface for the NEXUS core (task A6; spec §60, §65, §74).

This is the *only* way the desktop UI reaches anything. That is a deliberate
architectural constraint from §65: the permission engine sits between intent and
execution, so a UI that could open a file directly would be a hole straight
through it. Every route here either reads something the user already owns or goes
through :mod:`nexus.tools.runtime`, which cannot execute without a decision from
:mod:`nexus.permissions.engine`.

Three rules shape every endpoint:

* **No invented data.** A route with nothing real to return says so with a status
  and a sentence, never with a plausible-looking empty shape. §60, §78.
* **Local only.** The server binds to loopback and CORS is limited to the dev
  origins the UI actually runs on, because this process can read files and run
  commands -- exposing it on a LAN would hand that to anything on the network.
* **The bus is the truth for anything live.** Permission prompts, monitor
  observations and tool progress arrive over the WebSocket rather than by
  polling, so the UI cannot show a stale "waiting for approval" after the request
  already timed out. §97.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..config import settings
from ..core.events import bus
from ..db import conn as db, migrate
from ..logging_setup import get_logger

log = get_logger("api")

#: The dev-server origins the desktop UI is served from. Deliberately explicit:
#: this process can read files and run shell commands, so a permissive CORS
#: policy would let any page the user visits drive it.
DEV_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "tauri://localhost",
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Bring the core up in dependency order, and refuse to serve if storage is broken.

    The database is migrated *before* anything else starts, because a monitor or a
    tool writing into a half-migrated schema is how silent corruption happens
    (§103). If the migration fails the app still starts -- so the UI can render a
    real "core is broken, here is why" screen instead of failing to connect and
    leaving the user guessing (§60).
    """
    bus.bind_loop(asyncio.get_running_loop())
    app.state.storage_error = None
    try:
        db.configure()
        result = migrate.migrate()
        log.info("database ready", extra={"migrations": result})
        ok, detail = db.integrity_check()
        if not ok:
            app.state.storage_error = f"Database integrity check failed: {detail}"
    except Exception as exc:  # pragma: no cover - surfaced to the UI, not swallowed
        log.error("database unavailable", exc_info=True)
        app.state.storage_error = str(exc)

    if app.state.storage_error is None:
        # No run can be in flight in a process that has just started, so any task
        # still marked running is a task whose core died under it. Reconciled before
        # anything else can read the table: a UI connecting in the next second would
        # otherwise draw a spinner over a run that no longer exists, and the agent's
        # own "is it already running" guard would refuse to resume it forever.
        from ..db.repos import tasks as tasks_repo

        interrupted = tasks_repo.reconcile_interrupted()
        for task_id in interrupted:
            snapshot = tasks_repo.get_with_steps(task_id) or {"id": task_id}
            snapshot["progress"] = tasks_repo.progress(task_id)
            bus.publish("task.interrupted", snapshot)
        if interrupted:
            log.warning("reconciled %d interrupted task(s)", len(interrupted))

    from ..monitors import supervisor

    sup = supervisor()
    app.state.supervisor = sup
    if app.state.storage_error is None:
        # Honours the master switch internally: with monitor_enabled off (the
        # default) this starts nothing at all. Calling it unconditionally is what
        # makes "turn watching on" work without a restart.
        await sup.start()

        # The periodic memory sweep. Started separately from the monitors and not
        # gated by their master switch: reading back the user's own conversation is
        # a different act from watching their screen, and it must not require the
        # second to do the first. Checks its own settings on every tick, so a
        # toggle takes effect without a restart.
        from ..ai import memory_sweep

        memory_sweep.start()

        # Embed any approved memory that has no vector yet, in the background.
        # Scheduled rather than awaited: the UI's first request must not queue
        # behind an embedding round trip, which has been measured at 50-110 s
        # against a cold endpoint. Until it finishes, recall still works -- by
        # keyword -- and /api/memories/embeddings reports how many are left, so
        # "partially searchable by meaning" is a state the UI can show instead of
        # a silent difference in behaviour between two memories.
        from ..ai import embeddings

        embeddings.schedule_backfill()

        # The automation engine. Like the sweep, it is not a monitor and is not
        # behind the watching master switch: firing a reminder the user themselves
        # scheduled is not an act of observation.
        #
        # Reconciled before it starts, for the same reason tasks are above -- a run
        # still marked 'running' in a process that has just booted is a run whose
        # core died under it, and leaving it would show a reminder as permanently
        # in-flight on the Automations screen.
        from .. import automations as automation_engine
        from ..db.repos import automations as automations_repo

        stranded = automations_repo.reconcile_running_runs()
        if stranded:
            log.warning("reconciled %d interrupted automation run(s)", stranded)
        automation_engine.start()

        # The proactive engine (L10): scores the observation stream and turns
        # what clears the bar into suggestions -- proposals that cite their
        # observation, never actions. Like the sweep and the automation engine,
        # it is not a monitor and is not behind the watching master switch:
        # suggesting from what was already seen is not a new act of watching.
        # Its own master switch (proactive_suggestions_enabled, default off) is
        # read on every tick, so the timer half stays quiet until switched on.
        from ..core import proactive

        proactive.start()

        # The world model (C2): the fused current-state substrate the
        # doc-multimodal audit named as the largest structural gap. Purely a
        # consumer of what the monitors already publish -- subscribing is not
        # watching -- and every fact it holds carries the observation that
        # produced it. Started unconditionally so its honesty is not gated
        # behind a monitor being enabled: an empty world state that says
        # "nothing observed" is the truthful answer when every watcher is off.
        from ..core import world

        world.start()

        # The continuous learning processor (task C9): observes interactions and
        # extracts actionable memories, preferences, and corrections. Not behind
        # the monitoring master switch: remembering what the user explicitly said
        # is not an act of watching, and the processor checks its own settings
        # (learning_enabled, default on) on every tick.
        from ..tools.learning import start_learning

        await start_learning()

    bus.publish("core.ready", {"storage_error": app.state.storage_error})
    try:
        yield
    finally:
        await sup.stop()
        from ..ai import memory_sweep

        await memory_sweep.stop()
        from .. import automations as automation_engine

        await automation_engine.stop()
        from ..core import proactive

        await proactive.stop()
        from ..core import world

        await world.stop()
        from ..tools.learning import stop_learning

        await stop_learning()
        from ..ai import embeddings

        left = await embeddings.drain()
        if left:
            # Named rather than swallowed: those memories are stored and findable by
            # keyword, and the next startup backfill will embed them.
            log.info("shutdown cancelled %d in-flight memory embedding(s)", left)
        from ..providers import close_all

        await close_all()
        db.close_thread_connection()


def create_app() -> FastAPI:
    app = FastAPI(
        title="NEXUS core",
        version="0.1.0",
        # The UI is the only client and it has typed models generated from these
        # routes; the interactive docs stay on because they are genuinely useful
        # for verifying a route by hand during development.
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(DEV_ORIGINS),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["content-type"],
    )

    from .routes import (
        agent_routes, automations_routes, chat_routes, data, models_routes,
        monitors_routes, permissions_routes, system, tools_routes, voice_routes,
    )
    from .ws import router as ws_router

    for module in (system, permissions_routes, tools_routes, monitors_routes, data,
                   models_routes, voice_routes, chat_routes, agent_routes,
                   automations_routes):
        app.include_router(module.router)
    app.include_router(ws_router)

    @app.exception_handler(Exception)
    async def unhandled(request: Any, exc: Exception) -> JSONResponse:
        """Turn a crash into a sentence rather than an empty 500.

        §58 requires an error to say what happened and what to do about it. A bare
        500 with no body gives the UI nothing to render but "something went wrong",
        which is the least useful thing it could say.
        """
        log.error("unhandled error", exc_info=True, extra={"path": str(request.url)})
        return JSONResponse(
            status_code=500,
            content={
                "error": type(exc).__name__,
                "message": str(exc) or "The core hit an unexpected error.",
                "detail": "This is a bug in NEXUS, not something you did. The full trace is in the core log.",
            },
        )

    return app


def run() -> None:
    """Entry point for ``python -m nexus.api``."""
    import uvicorn

    cfg = settings()
    uvicorn.run(
        "nexus.api:create_app",
        factory=True,
        host=cfg.host,
        port=cfg.port,
        log_level=cfg.log_level.lower(),
        reload=cfg.dev,
    )


__all__ = ["create_app", "run", "lifespan", "DEV_ORIGINS"]
