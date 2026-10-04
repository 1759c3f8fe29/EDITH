"""Tool surface: what exists, what it would do, and running it (§45, §65, §66).

Execution goes through :func:`nexus.tools.runtime.execute` with
``require_permission=True`` and no way for a caller to turn that off. The flag
exists on the runtime for the agent's own internal verification steps, not for
HTTP, and exposing it here would make the permission engine optional from the
outside -- which is the one thing §65 says it must never be.

The pairing that matters is ``preview`` and ``execute``: §44 requires the user to
be able to see what an action would do before it happens, and a preview that ran
a different code path from the real thing would be a lie with extra steps. Both
resolve the same tool, validate against the same schema, and report the same
capability, so what the preview shows is what execution will attempt.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from ...logging_setup import get_logger
from ...tools import registry, runtime

router = APIRouter(prefix="/api/tools", tags=["tools"])
log = get_logger("api.tools")


@router.get("")
def list_tools(include_unavailable: bool = True) -> dict[str, Any]:
    """The catalogue, with unavailability named rather than hidden.

    Unavailable tools are included by default and each carries the reason it
    cannot run. Silently omitting them would leave the user unable to tell
    "NEXUS cannot do this" from "NEXUS needs xdotool installed" -- and only one
    of those has a fix (§78, §110).
    """
    tools = registry.all_tools() if include_unavailable else registry.available_tools()
    return {
        "tools": [
            {
                **tool.to_dict(),
                "unavailable_reason": tool.unavailable_reason(),
            }
            for tool in tools
        ],
        "total": len(registry.all_tools()),
        "available": len(registry.available_tools()),
    }


@router.get("/schemas")
def schemas(only_available: bool = True) -> dict[str, Any]:
    """The function-calling schemas, exactly as the model receives them.

    Defaults to available-only because handing the model a tool that cannot run
    invites it to plan around one and then fail mid-task.
    """
    return {"schemas": registry.schemas(only_available=only_available)}


@router.post("/preview")
def preview(
    name: str = Body(..., embed=True),
    params: dict[str, Any] | None = Body(None, embed=True),
) -> dict[str, Any]:
    """What this call would do, without doing it (§44)."""
    result = runtime.preview(name, params)
    if not result.get("ok", True):
        raise HTTPException(status_code=400, detail=result.get("error", "That call is not valid."))
    return result


@router.post("/execute")
async def execute(
    name: str = Body(..., embed=True),
    params: dict[str, Any] | None = Body(None, embed=True),
    conversation_id: str | None = Body(None, embed=True),
    task_id: str | None = Body(None, embed=True),
) -> dict[str, Any]:
    """Run a tool. Blocks while the permission engine waits for the user.

    Returns 200 with ``ok: false`` for a tool that ran and failed, rather than an
    HTTP error: the request itself succeeded, and the failure is a *result* the UI
    needs to show with its summary and verification state intact. A 500 here would
    throw away the sentence explaining what went wrong (§58).
    """
    result = await runtime.execute(
        name,
        params,
        conversation_id=conversation_id,
        task_id=task_id,
        # Not a parameter of this route, and deliberately so: see module docstring.
        require_permission=True,
    )
    return result.to_dict()


@router.get("/by-capability")
def by_capability() -> dict[str, Any]:
    """Which tools each capability unlocks.

    The Permissions screen needs this to answer the question a user actually asks
    when a prompt appears: "what does allowing this let it do?" (§45).
    """
    return {"map": registry.by_capability()}


# ------------------------------------------------------------------- terminal
#
# ``terminal.run`` starts a subprocess the caller has no way to stop: until now
# the only cancellation was the command's own timeout (§17 makes interrupting a
# running action a hard requirement, not a nicety). These routes expose the
# cancel/cancel_all functions that already existed in ``tools.terminal`` -- they
# send a real SIGTERM (escalating to SIGKILL) and publish ``terminal.cancelled``
# on the bus, so the UI hears about a stop no matter who asked for it.
#
# No permission check: cancelling something the *user* started is the one action
# that must never prompt. The permission engine gates starting commands; stopping
# one is the emergency exit (§17), and an interrupt that first asks "may I
# interrupt?" is not an interrupt.


@router.get("/terminal/running")
def terminal_running() -> dict[str, Any]:
    """Commands the agent's tool surface is currently executing."""
    from ...tools import terminal

    return {"running": terminal.running()}


@router.post("/terminal/cancel")
def terminal_cancel(run_id: str | None = Body(None, embed=True)) -> dict[str, Any]:
    """Stop one running command, or all of them when no run_id is given.

    ``run_id`` is read from the JSON body (``{"run_id": ...}``), matching the
    other POST routes in this file rather than making the UI tack it onto the
    query string. Cancelling a specific run that is no longer running is not an
    error -- a double-click on "Stop" in the UI must not produce one -- and
    cancelling everything when nothing runs returns a plain zero. Both come back
    200 with the honest count of what was actually stopped.
    """
    from ...tools import terminal

    if run_id is None:
        stopped = terminal.cancel_all()
        return {
            "stopped": stopped,
            "detail": f"Stopped {stopped} running command(s).",
        }
    outcome = terminal.cancel(run_id)
    if outcome["stopped"]:
        return outcome
    return {
        "stopped": False,
        "run_id": run_id,
        "detail": outcome["reason"],
    }
