"""Failure classification and the retry policy (§13, §14).

Two halves. The first tests :mod:`nexus.ai.recovery` on its own -- it is pure, so
every class and every refusal can be checked without putting a machine into the
matching broken state. The second drives the real ``agent._run_step`` against a
stubbed runtime and asserts on what actually reached the database and the event
bus, because the interesting claims are not "the policy says no retry" but "the
tool was called exactly once and the row says why".

The stubs here are the two things a test must not do for real: call a model, and
touch this computer. The store, the status vocabulary and the bus are the real
ones, so a test still fails if the schema disagrees with the code -- which is the
point, since a retry that invented a ``retrying`` status would violate the
``task_steps`` CHECK constraint and nothing else would catch it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nexus.ai import agent, recovery
from nexus.db.repos import tasks
from nexus.tools.registry import ToolResult

# --------------------------------------------------------------------- classify


def test_every_kind_has_a_hint() -> None:
    """A class with no hint would print an empty reason on a failed step."""
    for kind in recovery.KINDS:
        assert recovery._HINTS.get(kind), f"{kind} has no hint"


def test_hints_describe_no_kind_outside_the_vocabulary() -> None:
    assert set(recovery._HINTS) == set(recovery.KINDS)


@pytest.mark.parametrize(
    "error,expected",
    [
        ("Permission denied", "permission"),
        ("You declined. Nothing was done.", "permission"),
        ("mkdir: cannot create directory: Operation not permitted", "permission"),
        ("bash: playerctl: command not found", "missing_dependency"),
        ("ffmpeg is not installed", "missing_dependency"),
        ("Screen capture is not supported on this session type", "unavailable"),
        ("HTTP 401 unauthorized", "configuration"),
        ("No model available for job 'reasoning'", "configuration"),
        ("HTTP 429 Too Many Requests", "transient"),
        ("HTTP 503 Service Unavailable", "transient"),
        ("upstream overloaded, please try again later", "transient"),
        ("could not resolve host api.example.com", "network"),
        ("Connection refused", "network"),
        ("read operation timed out", "timeout"),
        ("bind: address already in use", "state"),
        ("the database is locked", "state"),
        ("cat: /tmp/nope: No such file or directory", "wrong_target"),
        ("no such window", "wrong_target"),
        ("params do not match the schema: 'path' is a required property", "tool"),
        ("the disk is full", "ambiguous"),
        ("something went sideways", "ambiguous"),
    ],
)
def test_classify_reads_real_error_text(error: str, expected: str) -> None:
    assert recovery.classify(error).kind == expected


def test_classification_keeps_the_evidence() -> None:
    """The class is a summary; the sentence that produced it stays attached."""
    failure = recovery.classify("bash: playerctl: command not found")
    assert failure.evidence == "bash: playerctl: command not found"
    assert failure.hint


def test_a_busy_server_is_not_a_missing_capability() -> None:
    """"503 Service Unavailable" is the trap in this whole module.

    A pattern matching the bare word "unavailable" reads it as "this capability
    does not exist here" -- which is the one class that is never retried. Every
    rate-limited or overloaded API would then be given up on after one attempt,
    and the log would say the machine cannot do it at all. The two are told apart
    by the status code, and the genuine missing-capability signal arrives as
    ``unavailable_reason`` rather than as prose.
    """
    assert recovery.classify("HTTP 503 Service Unavailable").kind == "transient"
    assert recovery.plan("transient", idempotent=True, attempts=1).retry
    assert recovery.classify("screen capture is not available on this session").kind == "unavailable"


def test_state_wins_over_wrong_target_for_already_in_use() -> None:
    """Pattern order is load-bearing, not incidental.

    "address already in use" also reads as a target problem. It is not one: the
    port exists and something holds it, which needs a re-plan, not a different
    path. If the general pattern were checked first this would silently become
    ``wrong_target`` and the hint would send the planner looking for the wrong
    thing.
    """
    assert recovery.classify("bind: address already in use").kind == "state"


def test_a_denial_beats_the_error_text() -> None:
    """The engine's own verdict is a fact; the tool's wording is a guess at it."""
    failure = recovery.classify("could not resolve host", denied=True)
    assert failure.kind == "permission"


def test_an_unavailable_tool_beats_the_error_text() -> None:
    failure = recovery.classify("connection refused", unavailable_reason="'ffmpeg' is not installed.")
    assert failure.kind == "unavailable"
    assert failure.evidence == "'ffmpeg' is not installed."


def test_our_own_timeout_beats_the_error_text() -> None:
    """A step we killed is a timeout even if the tool's last words were about a file."""
    failure = recovery.classify("no such file or directory", timed_out=True)
    assert failure.kind == "timeout"


def test_no_error_text_is_ambiguous_not_a_guess() -> None:
    failure = recovery.classify("")
    assert failure.kind == "ambiguous"
    assert "did not say" in failure.evidence


# ------------------------------------------------------------------------- plan


def test_a_transient_failure_on_an_idempotent_tool_is_retried() -> None:
    decision = recovery.plan("transient", idempotent=True, attempts=1)
    assert decision.retry
    assert decision.delay_s > 0
    assert "transient" in decision.why


def test_backoff_grows_and_never_exceeds_the_last_delay() -> None:
    delays = [
        recovery.plan("network", idempotent=True, attempts=n).delay_s
        for n in range(1, recovery.MAX_ATTEMPTS)
    ]
    assert delays == sorted(delays)
    assert all(d <= max(recovery._DELAYS) for d in delays)


@pytest.mark.parametrize(
    "kind",
    ["permission", "missing_dependency", "unavailable", "wrong_target", "state",
     "configuration", "tool", "ambiguous"],
)
def test_classes_a_repeat_cannot_fix_are_never_retried(kind: str) -> None:
    decision = recovery.plan(kind, idempotent=True, attempts=1)
    assert not decision.retry
    assert decision.why


def test_a_non_idempotent_tool_is_never_retried_even_when_the_class_allows_it() -> None:
    """The one rule that overrides classification entirely."""
    for kind in ("transient", "network", "timeout"):
        decision = recovery.plan(kind, idempotent=False, attempts=1)
        assert not decision.retry
        assert "not idempotent" in decision.why


def test_the_attempt_limit_is_the_policy_not_the_caller() -> None:
    """Three attempts, asserted as three.

    Written against the literal on purpose. Every assertion here originally read
    ``recovery.MAX_ATTEMPTS``, which made them all self-consistent with whatever the
    constant happened to say -- raising it to 99 left the suite green, so the tests
    defended a relationship rather than the bound. "Never loop endlessly" (§14) is a
    claim about a number, and a test that reads the number from the code cannot check
    it.
    """
    assert recovery.MAX_ATTEMPTS == 3
    assert recovery.plan("transient", idempotent=True, attempts=3).retry is False
    assert "limit" in recovery.plan("transient", idempotent=True, attempts=3).why
    # And the delays are bounded too: an unbounded backoff is a hang with a reason.
    assert max(recovery._DELAYS) <= 5.0


def test_an_unknown_class_does_not_fall_through_to_a_retry() -> None:
    """A default that retries is how a typo becomes a spin loop."""
    decision = recovery.plan("flaky-ish", idempotent=True, attempts=1)
    assert not decision.retry


def test_the_reason_reads_as_a_sentence() -> None:
    """It is shown to the user on the failed step, so the article has to agree."""
    assert recovery.plan("ambiguous", idempotent=True, attempts=1).why.startswith("an ambiguous")
    assert recovery.plan("unavailable", idempotent=True, attempts=1).why.startswith("an unavailable")
    assert recovery.plan("state", idempotent=True, attempts=1).why.startswith("a state")
    assert recovery.plan("transient", idempotent=True, attempts=1).why.startswith("a transient")


# ------------------------------------------------------------- _run_step wiring


class Queued:
    """Stands in for ``runtime.execute``, returning queued results in order.

    The last result repeats, so a test that wants "this always fails" does not
    have to guess how many attempts the policy will make.
    """

    def __init__(self, results: list[ToolResult]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, name: str, params: dict[str, Any] | None = None, **kw: Any) -> ToolResult:
        self.calls.append({"name": name, "params": params or {}})
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]


class FakeTool:
    """Only the two things ``_run_step`` asks a tool about."""

    def __init__(self, *, idempotent: bool, unavailable: str | None = None) -> None:
        self.idempotent = idempotent
        self._unavailable = unavailable

    def unavailable_reason(self) -> str | None:
        return self._unavailable


@pytest.fixture
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real backoff, zero seconds of it.

    Patched on the policy rather than on ``asyncio.sleep``, because the permission
    watcher sleeps too and a test that removed all sleeping would change what it
    is testing.
    """
    monkeypatch.setattr(recovery, "_DELAYS", (0.0, 0.0))


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Every event the run publishes, in order, still reaching the real bus."""
    seen: list[tuple[str, dict[str, Any]]] = []
    real = agent.bus.publish

    def record(name: str, payload: dict[str, Any] | None = None, **kw: Any) -> Any:
        seen.append((name, payload or {}))
        return real(name, payload, **kw) if payload is not None else real(name, **kw)

    monkeypatch.setattr(agent.bus, "publish", record)
    return seen


def make_step(tool: str = "system_status") -> tuple[dict[str, Any], dict[str, Any]]:
    task = tasks.create("prove the retry policy runs")
    step = tasks.add_steps(task["id"], [{"intent": "do the thing", "tool": tool, "params": {}}])[0]
    return task, tasks.get_step(step["id"])


def run_step(
    monkeypatch: pytest.MonkeyPatch,
    *,
    results: list[ToolResult],
    idempotent: bool = True,
    unavailable: str | None = None,
    verify: bool = True,
) -> tuple[bool, str, Queued, dict[str, Any]]:
    task, step = make_step()
    runner = Queued(results)
    monkeypatch.setattr(agent.runtime, "execute", runner)
    monkeypatch.setattr(
        agent.registry,
        "get",
        lambda name: FakeTool(idempotent=idempotent, unavailable=unavailable),
    )
    ok, message = asyncio.run(agent._run_step(task, step, verify=verify))
    return ok, message, runner, tasks.get_step(step["id"])


def test_a_transient_failure_is_actually_retried_and_can_succeed(
    monkeypatch: pytest.MonkeyPatch, no_waiting: None, events: list[tuple[str, dict[str, Any]]]
) -> None:
    ok, message, runner, row = run_step(
        monkeypatch,
        results=[
            ToolResult(ok=False, error="HTTP 429 Too Many Requests"),
            ToolResult(ok=False, error="HTTP 429 Too Many Requests"),
            ToolResult(ok=True, summary="Up 1h", verified=None),
        ],
    )
    assert ok
    assert len(runner.calls) == 3
    assert row["status"] == "completed"
    # It says it took three goes. A step that silently succeeded on the third
    # attempt reads as one that worked first time.
    assert "attempt 3" in message
    assert "transient" in message
    retries = [payload for name, payload in events if name == "task.step.retry"]
    assert [p["attempt"] for p in retries] == [1, 2]
    assert retries[0]["failure"]["kind"] == "transient"
    assert retries[0]["max_attempts"] == recovery.MAX_ATTEMPTS


def test_an_endlessly_transient_failure_stops_at_the_limit(
    monkeypatch: pytest.MonkeyPatch, no_waiting: None
) -> None:
    ok, message, runner, row = run_step(
        monkeypatch, results=[ToolResult(ok=False, error="503 Service Unavailable")]
    )
    assert not ok
    assert len(runner.calls) == 3
    assert row["status"] == "failed"
    assert "limit" in row["error"]


def test_a_non_idempotent_tool_is_called_once_and_the_row_says_why(
    monkeypatch: pytest.MonkeyPatch, no_waiting: None
) -> None:
    ok, message, runner, row = run_step(
        monkeypatch,
        results=[ToolResult(ok=False, error="HTTP 429 Too Many Requests")],
        idempotent=False,
    )
    assert not ok
    assert len(runner.calls) == 1
    assert "not idempotent" in row["error"]


def test_a_denial_is_an_answer_not_something_to_retry(
    monkeypatch: pytest.MonkeyPatch, no_waiting: None
) -> None:
    ok, message, runner, row = run_step(
        monkeypatch,
        results=[ToolResult(ok=False, error="You declined. Nothing was done.")],
    )
    assert not ok
    assert len(runner.calls) == 1
    assert "permission" in row["error"]


def test_a_missing_program_is_not_retried_and_names_the_fix(
    monkeypatch: pytest.MonkeyPatch, no_waiting: None
) -> None:
    ok, message, runner, row = run_step(
        monkeypatch,
        results=[ToolResult(ok=False, error="bash: playerctl: command not found")],
    )
    assert len(runner.calls) == 1
    assert "not installed" in row["error"]


def test_a_tool_whose_program_is_missing_is_never_retried_whatever_it_said(
    monkeypatch: pytest.MonkeyPatch, no_waiting: None
) -> None:
    """The unavailability is a fact about the machine, not about this attempt."""
    ok, message, runner, row = run_step(
        monkeypatch,
        results=[ToolResult(ok=False, error="connection refused")],
        unavailable="'ffmpeg' is not installed.",
    )
    assert len(runner.calls) == 1
    assert "not available here" in row["error"]


def test_a_verifier_contradiction_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, no_waiting: None
) -> None:
    """The tool says it happened, the check says it did not.

    Repeating an action that may already have taken effect cannot settle that
    disagreement, so it goes to the planner instead of round the loop.

    The verification text says the check **timed out** deliberately. With "the file
    is not there" this test passed even with the guard removed, because that sentence
    classifies as ``ambiguous`` and ambiguous is not retryable either -- so it proved
    nothing about the guard. A timeout is retryable, which makes the guard the only
    thing standing between a contradicted write and a second write.
    """
    ok, message, runner, row = run_step(
        monkeypatch,
        results=[
            ToolResult(
                ok=True,
                summary="wrote it",
                verified=False,
                verification="reading the file back timed out",
            )
        ],
    )
    assert not ok
    assert len(runner.calls) == 1
    assert row["status"] == "failed"
    assert row["verified"] == 0
    assert "could not be confirmed" in row["error"]
    # And no retry reason is bolted onto it -- the retry policy never saw it.
    assert "not idempotent" not in row["error"]
    assert "retrying after" not in row["error"]


def test_a_step_that_succeeds_first_time_says_nothing_about_attempts(
    monkeypatch: pytest.MonkeyPatch, no_waiting: None, events: list[tuple[str, dict[str, Any]]]
) -> None:
    ok, message, runner, row = run_step(
        monkeypatch, results=[ToolResult(ok=True, summary="Up 1h", verified=None)]
    )
    assert ok and message == "Up 1h"
    assert len(runner.calls) == 1
    assert row["verified"] is None
    assert not [name for name, _ in events if name == "task.step.retry"]


def test_retrying_never_invents_a_status(
    monkeypatch: pytest.MonkeyPatch, no_waiting: None, events: list[tuple[str, dict[str, Any]]]
) -> None:
    """``task_steps.status`` has a CHECK constraint and no ``retrying`` value.

    Every status this path writes must already be in that vocabulary, and the
    write is what proves it: an invented value would raise IntegrityError here
    rather than in front of a user.
    """
    run_step(
        monkeypatch,
        results=[
            ToolResult(ok=False, error="HTTP 429 Too Many Requests"),
            ToolResult(ok=True, summary="Up 1h"),
        ],
    )
    seen = {
        payload["step"]["status"]
        for name, payload in events
        if name.startswith("task.step.") and payload.get("step")
    }
    assert seen <= {"pending", "running", "waiting_permission", "completed", "failed"}
    assert "running" in seen


def test_an_unclassifiable_failure_is_reported_exactly_as_the_tool_said_it(
    monkeypatch: pytest.MonkeyPatch, no_waiting: None
) -> None:
    """No retry reason is bolted onto a failure that explains nothing.

    "the failure does not say what went wrong" would tell the user only that the
    tool was terse, which the message they are reading already showed them. Every
    other refusal names something they could act on, so those are appended.
    """
    ok, message, runner, row = run_step(
        monkeypatch,
        results=[ToolResult(ok=False, error="the disk is full", summary="could not write it")],
    )
    assert not ok
    assert len(runner.calls) == 1
    assert row["error"] == "the disk is full (could not write it)"


def test_the_error_from_the_last_attempt_is_what_the_row_keeps(
    monkeypatch: pytest.MonkeyPatch, no_waiting: None
) -> None:
    """Not the first attempt's, and not a summary of all of them.

    A step being retried goes back through ``running``, which clears the previous
    attempt's error -- so the row can never show a stale failure from an attempt
    that has already been superseded.
    """
    ok, message, runner, row = run_step(
        monkeypatch,
        results=[
            ToolResult(ok=False, error="HTTP 429 Too Many Requests"),
            ToolResult(ok=False, error="HTTP 503 Service Unavailable"),
            ToolResult(ok=False, error="HTTP 429 Too Many Requests"),
        ],
    )
    assert not ok
    assert row["error"].startswith("HTTP 429")
    assert "503" not in row["error"]


# --------------------------------------------------------- task-level retry


def failed_task(monkeypatch: pytest.MonkeyPatch, *, tools: list[str | None]) -> dict[str, Any]:
    """A task with one completed step and the rest failed/skipped, as a real run leaves it."""
    task = tasks.create("a task that stopped part-way")
    steps = tasks.add_steps(
        task["id"],
        [{"intent": f"step {i}", "tool": tool, "params": {}} for i, tool in enumerate(tools)],
    )
    tasks.set_step_status(steps[0]["id"], "completed", result="did it", verified=True)
    for step in steps[1:2]:
        tasks.set_step_status(step["id"], "failed", error="the disk is full")
    for step in steps[2:]:
        tasks.set_step_status(step["id"], "skipped", error="Not run: the task stopped.")
    tasks.set_status(task["id"], "failed", error="the disk is full")
    return task


def test_a_failed_task_cannot_be_started_but_can_be_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The distinction the two calls exist to keep.

    ``start`` on a failed task would find no pending step, run nothing, and report a
    fresh failure -- so it refuses. ``retry`` is the call that first puts the steps
    that did not succeed back to pending, which is what makes running it again mean
    anything.
    """
    task = failed_task(monkeypatch, tools=["system_status", "file_write", None])
    assert "failed" not in agent.STARTABLE
    with pytest.raises(ValueError, match="failed"):
        agent.start(task["id"])
    assert agent.retry_preview(task["id"])["can_retry"]


def test_the_preview_names_what_would_run_again_and_what_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = failed_task(monkeypatch, tools=["system_status", "file_write", None])
    preview = agent.retry_preview(task["id"])
    assert preview["keeping"] == 1
    assert [s["idx"] for s in preview["steps"]] == [1, 2]
    # A thinking step calls no tool, so idempotence is not a fact about it either way.
    assert preview["steps"][1]["tool"] is None
    assert preview["steps"][1]["idempotent"] is None


def test_the_preview_flags_a_tool_that_is_not_safe_to_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fact that changes the decision, called out rather than buried in a list.

    The step-level policy refuses to repeat a non-idempotent tool automatically. A
    person pressing Retry is the human it defers to -- which only works if they were
    told which step that applies to before pressing it.
    """
    task = failed_task(monkeypatch, tools=["system_status", "file_write"])
    monkeypatch.setattr(
        agent.registry, "get", lambda name: FakeTool(idempotent=name != "file_write")
    )
    preview = agent.retry_preview(task["id"])
    assert preview["not_idempotent"] == ["file_write"]


def test_retrying_keeps_completed_work_and_requeues_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = failed_task(monkeypatch, tools=["system_status", "system_status", "system_status"])
    monkeypatch.setattr(agent, "start", lambda task_id: {"stopped_here": task_id})

    agent.retry(task["id"])

    rows = tasks.list_steps(task["id"])
    assert [r["status"] for r in rows] == ["completed", "pending", "pending"]
    # The completed step keeps its result and its verification; the requeued ones
    # carry neither, because neither is a fact about the attempt about to happen.
    assert rows[0]["result"] == "did it" and rows[0]["verified"] == 1
    assert rows[1]["result"] is None and rows[1]["verified"] is None
    assert "retry" in rows[1]["error"].lower()
    # And the task itself no longer displays the outcome of the previous attempt.
    refreshed = tasks.get(task["id"])
    assert refreshed["status"] == "pending"
    assert refreshed["error"] is None
    assert refreshed["finished_at"] is None


def test_retrying_publishes_what_it_did(monkeypatch: pytest.MonkeyPatch) -> None:
    task = failed_task(monkeypatch, tools=["system_status", "system_status"])
    monkeypatch.setattr(agent, "start", lambda task_id: {})
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(agent.bus, "publish", lambda name, payload=None, **kw: seen.append((name, payload or {})))

    agent.retry(task["id"])

    retried = [p for name, p in seen if name == "task.retried"]
    assert retried and retried[0]["retried_steps"] == 1
    assert retried[0]["kept_steps"] == 1


def test_a_completed_task_is_not_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """There is nothing to re-run, and "retry" over a success would redo real work."""
    task = tasks.create("a task that worked")
    step = tasks.add_steps(task["id"], [{"intent": "do it", "tool": "system_status", "params": {}}])[0]
    tasks.set_step_status(step["id"], "completed", result="done")
    tasks.set_status(task["id"], "completed", result="done")
    preview = agent.retry_preview(task["id"])
    assert not preview["can_retry"]
    assert "completed" in preview["reason"]
    with pytest.raises(ValueError):
        agent.retry(task["id"])


def test_a_cancelled_task_can_be_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelling stops the work; it does not mean the work is unwanted forever."""
    task = tasks.create("a task the user stopped")
    steps = tasks.add_steps(
        task["id"],
        [{"intent": "one", "tool": "system_status", "params": {}},
         {"intent": "two", "tool": "system_status", "params": {}}],
    )
    tasks.set_step_status(steps[0]["id"], "completed", result="did it")
    tasks.set_step_status(steps[1]["id"], "cancelled", error="The task was cancelled.")
    tasks.set_status(task["id"], "cancelled", error="The task was cancelled.")
    assert agent.retry_preview(task["id"])["can_retry"]


def test_a_retry_is_refused_while_a_run_is_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two runs on one task would execute the same steps twice."""
    task = failed_task(monkeypatch, tools=["system_status", "system_status"])
    monkeypatch.setattr(agent, "is_running", lambda task_id: True)
    preview = agent.retry_preview(task["id"])
    assert not preview["can_retry"]
    assert "in flight" in preview["reason"]


def test_the_reset_invents_no_step_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """``task_steps.status`` has a CHECK constraint; the write is what proves this."""
    task = failed_task(monkeypatch, tools=["system_status", "system_status", "system_status"])
    ids = tasks.reset_steps_for_retry(task["id"])
    assert len(ids) == 2
    assert {r["status"] for r in tasks.list_steps(task["id"])} == {"completed", "pending"}
    # Idempotent: a second reset has nothing left to move.
    assert tasks.reset_steps_for_retry(task["id"]) == []
