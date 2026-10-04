"""What happens to a task whose core died under it.

A run only exists inside the process running it. Kill that process and the database
is left asserting something that is no longer true: a task marked ``running`` with a
step marked ``waiting_permission``, and nothing anywhere that could ever finish
either. The UI then draws a spinner over a run that does not exist, and the agent's
own "is it already running" guard refuses to resume it — forever.

So startup reconciles the table before anything can read it. These tests seed exactly
that wreckage, run the real lifespan, and check the three things that have to be true
afterwards: the task says interrupted, the step is runnable again rather than failed,
and a live subscriber is told.
"""
from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable

import pytest

from nexus.core.events import bus
from nexus.db.repos import tasks


def sync(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def in_flight_task(objective: str, step_status: str = "waiting_permission") -> dict[str, Any]:
    """Seed the state a killed core leaves behind: a live task, a live step."""
    task = tasks.create(objective=objective)
    tasks.add_steps(task["id"], [
        {"idx": 0, "intent": "a step that was in flight", "tool": "system.status",
         "params": {}, "depends_on": []},
    ])
    step = tasks.list_steps(task["id"])[0]
    tasks.set_status(task["id"], "running")
    tasks.set_step_status(step["id"], step_status)
    return task


@sync
async def test_startup_reconciles_a_stranded_task_and_tells_a_subscriber() -> None:
    task = in_flight_task("reconciliation proof")
    seen: list[dict[str, Any]] = []

    async def listen() -> None:
        # Subscribed *before* startup on purpose. The event is published during
        # lifespan, before uvicorn is listening, so no WebSocket client can be
        # connected in time and ``after_seq=0`` deliberately replays nothing — this is
        # the only honest way to observe it.
        async for event in bus.subscribe(("task",), after_seq=0):
            seen.append(event.to_dict())

    listener = asyncio.create_task(listen())
    await asyncio.sleep(0.2)
    try:
        from nexus.api import create_app

        app = create_app()
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.5)
    finally:
        listener.cancel()

    row = tasks.get_with_steps(task["id"])
    step = row["steps"][0]
    events = [e for e in seen if e["type"] == "task.interrupted" and e.get("id") == task["id"]]

    assert row["status"] == "interrupted"
    assert row["error"], "a task that stopped for a reason must carry the reason"
    # Back to pending, not failed. ``next_runnable_step`` only picks pending, so a
    # failed step would make Resume produce a task that dies on its first move.
    assert step["status"] == "pending"
    assert "not known whether it completed" in (step["error"] or ""), \
        "the honest note is the point: nobody knows if the tool ran"
    assert len(events) == 1, [e["type"] for e in seen]
    assert events[0].get("progress", {}).get("pending") == 1
    assert len(events[0].get("steps", [])) == 1, "the event carries the plan, not just an id"

    tasks.delete(task["id"])


@sync
async def test_reconciliation_leaves_a_pending_task_alone() -> None:
    # The trap this guards: LIVE_STATUSES includes 'pending', so sweeping it would
    # stamp never-started tasks with an interruption that never happened.
    task = tasks.create(objective="planned, never started")
    tasks.add_steps(task["id"], [
        {"idx": 0, "intent": "x", "tool": "system.status", "params": {}, "depends_on": []},
    ])

    changed = tasks.reconcile_interrupted()

    assert task["id"] not in changed
    assert tasks.get(task["id"])["status"] == "pending"
    assert tasks.get(task["id"])["error"] is None
    tasks.delete(task["id"])


def test_reconciliation_reports_what_it_changed() -> None:
    task = in_flight_task("reported by id", step_status="running")

    changed = tasks.reconcile_interrupted()

    assert task["id"] in changed, "the caller has to know what to announce"
    assert tasks.reconcile_interrupted() == [], "a second pass has nothing left to do"
    tasks.delete(task["id"])


def test_a_step_that_starts_again_drops_the_last_attempt_s_failure() -> None:
    task = tasks.create(objective="step-clear proof")
    tasks.add_steps(task["id"], [
        {"idx": 0, "intent": "x", "tool": "system.status", "params": {}, "depends_on": []},
    ])
    step_id = tasks.list_steps(task["id"])[0]["id"]

    tasks.set_step_status(step_id, "failed", error="the disk was full",
                          result="wrote 4 of 900 bytes")
    assert tasks.get_step(step_id)["error"] == "the disk was full"

    tasks.set_step_status(step_id, "running")
    restarted = tasks.get_step(step_id)
    # Otherwise the panel prints last time's failure in red underneath this time's
    # result, and both look current.
    assert restarted["error"] is None
    assert restarted["result"] is None

    tasks.set_step_status(step_id, "completed", result="392.4 GB free on /")
    done = tasks.get_step(step_id)
    assert (done["result"], done["error"]) == ("392.4 GB free on /", None)
    tasks.delete(task["id"])


def test_a_resumed_task_does_not_keep_a_stale_finish_time() -> None:
    # A finished_at older than started_at makes the UI's "took" a negative duration.
    task = tasks.create(objective="resumed after finishing")
    tasks.set_status(task["id"], "failed", error="stopped")
    assert tasks.get(task["id"])["finished_at"], "the failure has to have been stamped"

    tasks.set_status(task["id"], "running")

    assert tasks.get(task["id"])["finished_at"] is None
    tasks.delete(task["id"])
