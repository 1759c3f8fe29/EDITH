"""The agent loop's stopping conditions, proved deterministically.

A live run with a real model and real tools proves the happy path — and only the
happy path. These are the branches a real run reaches by accident, if ever: a
verifier that contradicts the tool it checked, the step budget, re-planning after a
failure, cancellation mid-run, and the three ``agent_*`` settings that are supposed to
change behaviour. Each is reached on purpose here, so a regression shows up as a red
test rather than as an agent that quietly stops honouring a setting.

Everything except the model call and tool execution is real: the same SQLite schema,
the same status vocabulary with its CHECK constraints, the same event bus.
"""
from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable

import pytest
from conftest import BROKEN, CONTRADICTED, STATUS, Planner, Runner, plan_json

import nexus.ai.agent as agent
from nexus.db.repos import app_settings, tasks


def sync(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Run an async test body without depending on a pytest asyncio plugin."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch):
    """Install a planner and a tool runner, undone after the test."""

    def install(planner: Planner | None = None, runner: Runner | None = None):
        if planner is not None:
            monkeypatch.setattr(agent, "_one_shot", planner)
        if runner is not None:
            monkeypatch.setattr(agent.runtime, "execute", runner)
        return planner, runner

    return install


TWO = [
    {"intent": "check the machine", "tool": "system.status", "params": {}},
    {"intent": "sum it up", "tool": None, "params": {}, "depends_on": [0]},
]
BAD_TOOL = plan_json([{"intent": "open it", "tool": "open_the_browser", "params": {}}])


@sync
async def test_a_valid_plan_is_stored_as_steps(stub) -> None:
    stub(Planner([plan_json(TWO)]), Runner({"system.status": STATUS}))
    task = await agent.plan("check the machine and sum it up")
    steps = tasks.list_steps(task["id"])

    assert [s["tool"] for s in steps] == ["system.status", None]
    assert steps[1]["depends_on"] == [0], "the dependency the planner declared must survive"
    assert steps[0]["capability"], "capability is filled from the registry, not the model"


@sync
async def test_it_runs_to_completion_and_a_toolless_step_is_a_real_model_call(stub) -> None:
    planner, _ = stub(Planner([plan_json(TWO)]), Runner({"system.status": STATUS}))
    task = await agent.plan("check the machine and sum it up")
    done = await agent.run(task["id"])
    steps = tasks.list_steps(task["id"])

    assert done["status"] == "completed", done.get("error")
    assert done["progress"]["completed"] == 2
    assert len(planner.thoughts) == 1, "a step with no tool is a reasoning call, not a no-op"
    assert (steps[1]["result"] or "").startswith("Nothing in those")
    # Tri-state, and the None is the point: system.status has no verifier, which is a
    # different fact from "checked and not confirmed".
    assert steps[0]["verified"] is None


@sync
async def test_a_plan_naming_a_missing_tool_is_rejected_then_retried(stub) -> None:
    planner, _ = stub(Planner([BAD_TOOL, plan_json(TWO)]), Runner({"system.status": STATUS}))
    task = await agent.plan("try a made-up tool first")

    assert task["status"] == "pending"
    assert len(tasks.list_steps(task["id"])) == 2
    assert "does not exist" in planner.calls[1], "the rejection has to be handed back to be useful"


@sync
async def test_a_plan_that_never_validates_fails_with_the_real_reason(stub) -> None:
    stub(Planner([BAD_TOOL, BAD_TOOL]), None)
    task = await agent.plan("insist on a made-up tool")

    assert task["status"] == "failed"
    assert "open_the_browser" in (task["error"] or "")
    assert not tasks.list_steps(task["id"]), "a rejected plan must not leave half a plan behind"


@sync
async def test_an_empty_plan_is_an_answer_not_an_empty_task(stub) -> None:
    stub(Planner(['{"steps": []}']), None)
    task = await agent.plan("do something impossible here")

    assert task["status"] == "failed"
    assert "cannot be done" in (task["error"] or "")


@sync
async def test_a_contradicting_verifier_fails_the_step(stub, tmp_dir) -> None:
    app_settings.set("agent_verify_steps", True)
    app_settings.set("agent_pause_on_failure", True)
    three = [
        {"intent": "write it", "tool": "files.write",
         "params": {"path": str(tmp_dir / "x.txt"), "content": "hi"}},
        {"intent": "check the machine", "tool": "system.status", "params": {}},
        {"intent": "sum it up", "tool": None, "params": {}, "depends_on": [1]},
    ]
    stub(Planner([plan_json(three)]),
         Runner({"files.write": CONTRADICTED, "system.status": STATUS}))
    task = await agent.plan("write a file that will not be there")
    result = await agent.run(task["id"])
    steps = tasks.list_steps(task["id"])

    assert result["status"] == "failed"
    assert steps[0]["status"] == "failed", "the tool said ok; the verifier said no, and it loses"
    assert "not there" in (steps[0]["error"] or "")
    assert steps[0]["verified"] == 0
    # Left pending, they read as "about to run" on a task that has stopped for good.
    assert [s["status"] for s in steps[1:]] == ["skipped", "skipped"]


@sync
async def test_with_verification_off_the_same_result_is_allowed_through(stub, tmp_dir) -> None:
    app_settings.set("agent_verify_steps", False)
    three = [
        {"intent": "write it", "tool": "files.write",
         "params": {"path": str(tmp_dir / "x.txt"), "content": "hi"}},
        {"intent": "check the machine", "tool": "system.status", "params": {}},
    ]
    stub(Planner([plan_json(three)]),
         Runner({"files.write": CONTRADICTED, "system.status": STATUS}))
    task = await agent.plan("write a file that will not be there, unverified")
    result = await agent.run(task["id"])
    steps = tasks.list_steps(task["id"])

    assert result["status"] == "completed"
    # The setting changes whether it *stops*, never whether the truth is recorded.
    assert steps[0]["verified"] == 0


def write_then_check(tmp_dir) -> list[dict[str, Any]]:
    """A plan whose first step writes a file — the step the tests make fail."""
    return [
        {"intent": "write it", "tool": "files.write",
         "params": {"path": str(tmp_dir / "x.txt"), "content": "hi"}},
        {"intent": "check the machine", "tool": "system.status", "params": {}},
        {"intent": "sum it up", "tool": None, "params": {}, "depends_on": [1]},
    ]


@sync
async def test_with_pause_off_a_failure_is_replanned_once_then_stops(stub, tmp_dir) -> None:
    app_settings.set("agent_pause_on_failure", False)
    recovery = [
        {"intent": "check the machine instead", "tool": "system.status", "params": {}},
        {"intent": "sum it up", "tool": None, "params": {}, "depends_on": [0]},
    ]
    planner, runner = stub(
        Planner([plan_json(write_then_check(tmp_dir)), plan_json(recovery)]),
        Runner({"files.write": BROKEN, "system.status": STATUS}),
    )
    task = await agent.plan("fail, then recover")
    result = await agent.run(task["id"])
    steps = tasks.list_steps(task["id"])

    assert len(planner.calls) == 2, "one re-plan, and only one"
    assert "disk is full" in planner.calls[1], "re-planning blind is not re-planning"
    # The failure stays on the record; a re-plan is not a way to erase what happened.
    assert steps[0]["status"] == "failed" and steps[0]["tool"] == "files.write"
    assert [s["idx"] for s in steps] == sorted(s["idx"] for s in steps)
    # The re-plan's step 1 depends on its own step 0. Stored, that must become idx 2
    # depending on idx 1 — not on idx 0, the step that just failed. An un-offset
    # dependency would point a re-planned step at the failure and it could never
    # become runnable, so this asserts the offset value, not merely a valid one.
    replanned = [s for s in steps if s["idx"] >= 1]
    assert any(s["depends_on"] for s in replanned)
    assert all(all(int(d) >= 1 for d in s["depends_on"]) for s in replanned), \
        str([(s["idx"], s["depends_on"]) for s in steps])
    assert result["status"] in ("completed", "failed"), "stuck is the one outcome not allowed"
    assert runner.calls.count("system.status") >= 1, "the re-planned step has to actually run"


@sync
async def test_agent_max_steps_caps_the_plan(stub) -> None:
    app_settings.set("agent_max_steps", 2)
    long_plan = [{"intent": f"check {i}", "tool": "system.status", "params": {}} for i in range(4)]
    stub(Planner([plan_json(long_plan)]), Runner({"system.status": STATUS}))
    task = await agent.plan("check four times")

    assert len(tasks.list_steps(task["id"])) == 2


@sync
async def test_agent_max_steps_stops_the_run_and_names_itself(stub) -> None:
    app_settings.set("agent_max_steps", 1)
    long_plan = [{"intent": f"check {i}", "tool": "system.status", "params": {}} for i in range(4)]
    stub(Planner([plan_json(long_plan)]), Runner({"system.status": STATUS}))
    task = await agent.plan("check four times with room for one")
    # Added behind the planner's back: the cap has to hold at run time too, not only
    # where the plan is trimmed. This is the shape a re-plan produces.
    tasks.add_steps(task["id"], [{"intent": "an extra step nobody planned",
                                  "tool": "system.status", "params": {}, "depends_on": []}])
    result = await agent.run(task["id"])

    assert result["status"] == "failed"
    assert "agent_max_steps" in (result.get("error") or ""), \
        "a budget that stops a run without naming itself is indistinguishable from a bug"
    assert result["progress"]["completed"] == 1


class Slow(Runner):
    """A tool runner slow enough that a run can be caught in the middle of it."""

    def __init__(self, results=None, delay: float = 0.4) -> None:
        super().__init__(results)
        self.delay = delay

    async def __call__(self, name: str, params: Any = None, **kw: Any):
        self.calls.append(name)
        await asyncio.sleep(self.delay)
        return STATUS


FOUR_SLOW = [{"intent": f"check {i}", "tool": "system.status", "params": {}} for i in range(4)]


@sync
async def test_cancelling_a_running_task_leaves_nothing_claiming_to_run(stub) -> None:
    planner, runner = stub(Planner([plan_json(FOUR_SLOW)]), Slow())
    task = await agent.plan("run four slow checks")
    agent.start(task["id"])
    await asyncio.sleep(0.5)
    assert agent.is_running(task["id"]), "the test cannot prove cancellation if nothing is running"

    cancelled = agent.cancel(task["id"])
    await asyncio.sleep(0.3)
    steps = tasks.list_steps(task["id"])

    assert cancelled["status"] == "cancelled"
    # Left pending, the remaining steps read as "about to run" on a task nothing is
    # running — the exact lie the Tasks screen would then draw.
    assert not [s for s in steps if s["status"] in ("pending", "running", "waiting_permission")], \
        str([s["status"] for s in steps])
    assert len(runner.calls) < 4, "cancel that finishes the work first is not cancel"
    assert not agent.is_running(task["id"])

    # And a finished task refuses to start, with a sentence saying why.
    with pytest.raises(ValueError, match="cancelled"):
        agent.start(task["id"])


@sync
async def test_starting_twice_does_not_run_the_task_twice(stub) -> None:
    # Counted on the bus rather than by tool calls. Two concurrent runners mostly do
    # not repeat the *same* step — whichever gets there first marks it running and the
    # other moves on — so tool-call counts never see the bug. What two runners always
    # produce is two ``task.started`` events and two racing writers of the final
    # status, which is what this measures. Two guards stop it (start()'s is_running
    # check and run()'s own status check); removing either alone still passes, so this
    # only goes red when both are gone. run()'s is the one holding the line, because
    # create_task defers and a second start() sees a task that is still 'pending'.
    from nexus.core.events import bus

    stub(Planner([plan_json(FOUR_SLOW)]), Slow())
    task = await agent.plan("run slow checks once")
    seen = bus.replay()
    seq_before = seen[-1].seq if seen else 0

    agent.start(task["id"])
    agent.start(task["id"])
    await asyncio.sleep(0.6)
    starts = [e for e in bus.replay(after_seq=seq_before, topics=("task",))
              if e.type == "task.started" and e.payload.get("id") == task["id"]]

    assert len(starts) == 1, f"{len(starts)} task.started events"
    agent.cancel(task["id"])
    await asyncio.sleep(0.2)


@sync
async def test_a_run_notices_a_cancel_it_did_not_hear_about(stub) -> None:
    # ``cancel`` normally also cancels the asyncio task, which stops the loop whether
    # or not the loop checks — so that path proves nothing about the check inside it.
    # This is the case where there is no local handle to cancel: the row is flipped by
    # another process (or by an API worker for a run started as a bare ``run()``), and
    # the only thing that can stop the loop is the loop looking.
    stub(Planner([plan_json(FOUR_SLOW)]), Slow(delay=0.2))
    task = await agent.plan("four checks, cancelled from outside")
    assert task["id"] not in agent._RUNNING, "the point is that this run has no handle"

    runner = asyncio.create_task(agent.run(task["id"]))
    await asyncio.sleep(0.3)
    tasks.set_status(task["id"], "cancelled", error="The task was cancelled.")
    result = await asyncio.wait_for(runner, timeout=5)
    steps = tasks.list_steps(task["id"])

    assert result["status"] == "cancelled"
    assert not [s for s in steps if s["status"] in ("pending", "running", "waiting_permission")], \
        str([s["status"] for s in steps])
    assert [s["status"] for s in steps].count("completed") < 4, "it must not finish the plan"
