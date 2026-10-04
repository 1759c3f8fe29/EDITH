"""Agent mode over HTTP: plan a task, run it, stop it (Phase G; §20-§22, §80).

Three deliberate shapes here.

**Planning and running are separate calls.** ``POST /api/tasks`` writes a plan and
touches nothing else, so the UI can show the steps and the user can decide. §20
wants a plan the user can see before it happens, and that is only true if planning
is a request that does not also execute. ``POST /api/tasks/{id}/run`` is the
decision to go ahead; ``?run=true`` on the create call is the shorthand for "do it
now" and is the only way to get both in one request.

**Running returns immediately.** A task takes minutes and can stop on a permission
prompt. The response is the task as it stands; progress arrives as ``task.*`` events
on the websocket, each carrying real step counts from the store (§80). Nothing here
invents a percentage.

**No permission checks in this module.** Deliberate, and the opposite of a gap: every
step goes through :func:`nexus.tools.runtime.execute`, which asks the engine per
capability with the task id attached. Guarding the route as well would prompt once
for "run a task" and then again per step, which teaches people to click through
prompts -- and a blanket "yes" at the door is exactly the approval §45 refuses to let
anything grant.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from ...ai import agent
from ...db.repos import activity, tasks
from ...logging_setup import get_logger

router = APIRouter(prefix="/api/tasks", tags=["agent"])
log = get_logger("api.agent")


def _payload(task_id: str) -> dict[str, Any]:
    """One response shape for every route here, so the UI has one thing to read."""
    task = tasks.get_with_steps(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="No task with that id.")
    return {
        "task": task,
        "progress": tasks.progress(task_id),
        "running": agent.is_running(task_id),
        # The steps are the plan; the activity is what actually happened, including
        # every permission answer. Progress the user can trust comes from these two
        # together, never from a timer (§81).
        "activity": activity.for_task(task_id),
    }


@router.post("")
async def create_task(
    objective: str = Body(..., embed=True),
    project_id: str | None = Body(None, embed=True),
    conversation_id: str | None = Body(None, embed=True),
    run: bool = Body(False, embed=True),
) -> dict[str, Any]:
    """Plan a task. With ``run: true``, start it as well.

    A task whose planning failed is returned with status ``failed`` and the real
    reason -- no model reachable, no tools available, or a planner that said the
    objective cannot be done with what is installed. That is a 200 with bad news in
    it, not an error: the task exists, it is stored, and the reason is worth reading.
    """
    objective = (objective or "").strip()
    if not objective:
        raise HTTPException(status_code=422, detail="Say what the task should accomplish.")
    try:
        task = await agent.plan(objective, project_id=project_id, conversation_id=conversation_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if run and task.get("status") != "failed":
        agent.start(task["id"])
    return _payload(task["id"])


# ``async def`` on the two routes below is load-bearing, not style. A sync route
# runs in FastAPI's threadpool, where there is no running event loop, and
# :func:`agent.start` schedules the run on one -- so the sync version failed with
# "no running event loop" instead of starting the task. Same for ``cancel``, which
# cancels that scheduled run.
@router.post("/{task_id}/run")
async def run_task(task_id: str) -> dict[str, Any]:
    """Start (or resume) a planned task. Returns at once; watch the bus for progress."""
    try:
        agent.start(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No task with that id.") from exc
    except ValueError as exc:
        # Already finished, or has no steps. 409: the request was well-formed and the
        # task's state is the reason it cannot be honoured.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _payload(task_id)


@router.get("/{task_id}/retry")
def retry_preview(task_id: str) -> dict[str, Any]:
    """What a retry would re-run, before anything is re-run.

    A GET because it changes nothing, and a separate call because the UI needs the
    answer *while the button is still unpressed*: which steps would run again, and
    whether any of their tools is one where running twice is not the same as running
    once. "Retry?" on its own is not something a person can meaningfully agree to.
    """
    try:
        return agent.retry_preview(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No task with that id.") from exc


@router.post("/{task_id}/retry")
async def retry_task(task_id: str) -> dict[str, Any]:
    """Re-run the steps of a finished task that did not succeed.

    ``async def`` for the same reason as ``run``: this schedules work on the running
    event loop. Not folded into ``/run`` because it is a different operation -- it
    rewrites stored step statuses first, and a route that silently did that when
    asked to "run" would make a resume and a retry indistinguishable in the log.
    """
    try:
        agent.retry(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No task with that id.") from exc
    except ValueError as exc:
        # The task's state is the reason, and the message is the same one the preview
        # would have shown -- so a UI that skipped the preview still gets it.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _payload(task_id)


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str) -> dict[str, Any]:
    """Stop a task and withdraw any permission prompt it was waiting on.

    Idempotent, and honest about what it cannot undo: a step already executed stays
    executed. Cancelling marks everything that had not started as cancelled rather
    than leaving steps that will never run sitting at ``pending`` forever.
    """
    try:
        agent.cancel(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No task with that id.") from exc
    return _payload(task_id)


@router.delete("/{task_id}")
def delete_task(task_id: str) -> dict[str, Any]:
    """Remove a task and its steps. Refused while it is running.

    Deleting a running task would leave the loop writing steps to rows that no
    longer exist and the user with no record of what it did to their machine.
    """
    if agent.is_running(task_id):
        raise HTTPException(
            status_code=409,
            detail="This task is running. Cancel it first, then delete it.",
        )
    if not tasks.delete(task_id):
        raise HTTPException(status_code=404, detail="No task with that id.")
    return {"deleted": True, "id": task_id}
