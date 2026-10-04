"""The automation engine: does a stored rule actually do anything.

Every table, index and repository function this engine needs existed before it did,
and nothing called them. That is the failure these tests are shaped around -- not a
wrong answer, but a feature that is present in the schema, listed in the product
vision ("Every evening remind me to study"), visible on a Settings screen, and
inert. A test that only checked ``create()`` writes a row would have passed against
that entire state.

So these are about the fire path: a rule whose time has come produces a durable
record, moves to its next occurrence, and -- the case with no symptom -- a rule that
can never succeed stops asking.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from nexus import automations as engine
from nexus.core.events import bus
from nexus.db.repos import activity, automations as repo


@pytest.fixture(autouse=True)
def only_this_tests_rules() -> None:
    """Start every test with an empty table.

    ``run_due`` fires *every* due automation, which is correct and makes count
    assertions meaningless against the session-scoped database: a test asserting
    ``fired == 1`` would instead see one per rule left behind by earlier tests. The
    first run of this file failed exactly that way -- ``fired=5`` -- which is the
    fixture earning its place rather than papering over anything.
    """
    for row in repo.list_all():
        repo.delete(row["id"])


def make(action: dict | None, *, schedule: str = "15m", name: str = "test rule") -> dict:
    """A rule due 15 minutes from now, so ``run_due(at=...)`` decides when it fires."""
    return repo.create(
        name,
        trigger_kind="interval",
        schedule=schedule,
        action=action if action is not None else {"kind": "notify", "message": "study"},
    )


def after(automation: dict, seconds: float) -> datetime:
    """A moment ``seconds`` past this automation's scheduled time."""
    return datetime.fromisoformat(str(automation["next_run_at"])) + timedelta(seconds=seconds)


# ------------------------------------------------------------------- it fires


def test_a_due_reminder_fires_and_is_recorded() -> None:
    """The whole point: a stored rule produces a real delivery.

    Asserted through the repo and the activity log rather than the return value,
    because a ``TickResult`` saying ``fired=1`` is exactly what a stub would also
    say. What makes the reminder real is the row.
    """
    automation = make({"kind": "notify", "message": "time to study"})

    result = engine.run_due(at=after(automation, 1))

    assert result.fired == 1, result
    entries = activity.list_recent(kinds=("automation",), limit=20)
    assert any(e["summary"] == "time to study" for e in entries), (
        "the reminder fired but wrote no activity row, so it exists only for a UI "
        "that happened to be connected at that instant"
    )
    runs = repo.runs(automation["id"])
    assert [r["status"] for r in runs] == ["completed"], runs


def test_a_fired_reminder_is_durable_not_just_an_event() -> None:
    """The activity row is written even though nothing is subscribed to the bus.

    This is the property that makes a reminder survive a closed UI. The bus is
    in-process with a bounded replay buffer, so an engine that only published would
    deliver reminders if and only if someone was watching -- which is the one time a
    reminder is least needed.
    """
    automation = make({"kind": "notify", "message": "durable please"})
    assert bus.subscriber_count == 0, "this test is meaningless with a live subscriber"

    engine.run_due(at=after(automation, 1))

    entries = activity.list_recent(kinds=("automation",), limit=20)
    assert any(e["summary"] == "durable please" for e in entries)


def test_a_fired_reminder_also_reaches_a_connected_ui() -> None:
    """...and the live event is still published, for a UI that *is* open."""
    automation = make({"kind": "notify", "message": "live please"})
    marker = bus.publish("test.marker", {})

    engine.run_due(at=after(automation, 1))

    fired = [e for e in bus.replay(after_seq=marker.seq) if e.type == "automation.fired"]
    assert len(fired) == 1, fired
    assert fired[0].payload["message"] == "live please"


def test_firing_moves_the_rule_to_its_next_occurrence() -> None:
    """A rule that fires without rescheduling fires again on the very next tick."""
    automation = make({"kind": "notify", "message": "again"}, schedule="15m")

    engine.run_due(at=after(automation, 1))

    row = repo.get(automation["id"])
    assert row is not None and row["next_run_at"] is not None
    assert row["next_run_at"] > automation["next_run_at"], (
        "next_run_at did not advance, so this rule is due again immediately"
    )
    assert row["enabled"] is True


def test_missed_runs_collapse_into_one() -> None:
    """Down for a while is not a queue of reminders.

    A daily reminder missed three times should deliver tonight's, not three copies.
    ``due()`` reports the row once and ``finish_run`` moves it to the next *future*
    occurrence, so this is a property of the pair -- worth pinning because the
    obvious alternative implementation (loop until caught up) is a burst of
    identical notifications.
    """
    automation = make({"kind": "notify", "message": "collapse"}, schedule="60s")

    result = engine.run_due(at=after(automation, 3600))

    assert result.fired + result.skipped == 1, result
    assert len([r for r in repo.runs(automation["id"])]) == 1


# ------------------------------------------------- the failure with no symptom


def test_an_unrunnable_action_is_disabled_rather_than_retried_forever() -> None:
    """The hot loop, pinned.

    ``finish_run`` reschedules by default, which is right for a transient error. An
    action naming a kind nothing implements is not transient: rescheduled, it lands
    back at a past ``next_run_at``, is re-selected by ``due()`` on the next tick,
    fails again, and does so every 20 seconds forever -- writing a run row each
    time. The fix is to clear ``next_run_at``, and the assertion that matters is the
    second one: the rule is genuinely gone from ``due()``, not merely flagged.
    """
    automation = make({"kind": "explode", "message": "nope"})

    engine.run_due(at=after(automation, 1))

    row = repo.get(automation["id"])
    assert row is not None
    assert row["enabled"] is False, "an action that can never run was left armed"
    assert row["next_run_at"] is None, (
        "next_run_at was left in the past, so due() re-selects this rule on every "
        "tick: a permanent failure became a hot loop"
    )
    assert row["unavailable_reason"], "disabled with no reason shown to the user"
    assert "explode" in row["unavailable_reason"]

    later = engine.run_due(at=after(automation, 3600))
    assert later.failed == 0 and later.fired == 0, (
        f"the disabled rule ran again: {later}"
    )


def test_the_reason_names_the_kinds_that_do_exist() -> None:
    """An error saying only "unknown" leaves the user guessing at the fix."""
    automation = make({"kind": "wat"})

    engine.run_due(at=after(automation, 1))

    reason = (repo.get(automation["id"]) or {})["unavailable_reason"]
    assert "notify" in reason, reason


def test_a_notify_with_no_message_is_a_permanent_failure() -> None:
    """A reminder with nothing to say cannot be delivered by retrying."""
    automation = make({"kind": "notify", "message": "   "})

    engine.run_due(at=after(automation, 1))

    row = repo.get(automation["id"]) or {}
    assert row["enabled"] is False
    assert row["next_run_at"] is None
    assert "message" in (row["unavailable_reason"] or "")


def test_an_empty_action_is_refused() -> None:
    """A rule stored with no action at all is inert; it must not look armed."""
    automation = make({})

    engine.run_due(at=after(automation, 1))

    row = repo.get(automation["id"]) or {}
    assert row["enabled"] is False
    assert row["unavailable_reason"]


# ----------------------------------------------------------------- lateness


def test_a_reminder_missed_by_days_is_skipped_not_delivered() -> None:
    """Delivering it would state something false: that this is due now.

    Recorded as a skipped run rather than dropped, so "why did I not get my
    reminder" has an answer on the screen.
    """
    automation = make({"kind": "notify", "message": "stale"})

    result = engine.run_due(at=after(automation, engine.MAX_LATENESS_SECONDS + 60))

    assert result.skipped == 1 and result.fired == 0, result
    runs = repo.runs(automation["id"])
    assert runs[0]["status"] == "skipped"
    assert "missed" in runs[0]["detail"]["reason"]
    assert runs[0]["detail"]["late_seconds"] > engine.MAX_LATENESS_SECONDS
    assert not any(
        e["summary"] == "stale" for e in activity.list_recent(kinds=("automation",), limit=20)
    ), "a reminder from days ago was delivered as though it were current"


def test_a_skipped_reminder_still_moves_on() -> None:
    """Skipping must reschedule, or the rule is stuck being skipped forever."""
    automation = make({"kind": "notify", "message": "moves on"})

    engine.run_due(at=after(automation, engine.MAX_LATENESS_SECONDS + 60))

    row = repo.get(automation["id"]) or {}
    assert row["enabled"] is True
    assert row["next_run_at"] is not None


def test_a_reminder_delivered_late_says_how_late() -> None:
    """Inside the window it is delivered -- and the delay is on the run row.

    A reminder nine minutes late is still late, and the run detail is the only place
    that survives to say so.
    """
    automation = make({"kind": "notify", "message": "slightly late"})

    engine.run_due(at=after(automation, 540))

    detail = repo.runs(automation["id"])[0]["detail"]
    assert 500 < detail["late_seconds"] < 600, detail


def test_an_on_time_reminder_is_not_labelled_late() -> None:
    """The other half of the same behaviour: no spurious lateness on a normal fire."""
    automation = make({"kind": "notify", "message": "on time"})

    engine.run_due(at=after(automation, 0))

    assert "late_seconds" not in repo.runs(automation["id"])[0]["detail"]


# ------------------------------------------------------------------ execute()


def test_execute_never_raises_when_a_handler_does() -> None:
    """One bad action must not take the loop down with it.

    Patched rather than provoked, because every real handler is written not to raise
    -- which is exactly why the guard needs a test that does not depend on one of
    them being buggy today.
    """
    def boom(automation: dict, action: dict) -> engine.Outcome:
        raise RuntimeError("handler exploded")

    engine._ACTIONS["boom"] = engine.ActionKind(kind="boom", run=boom)
    try:
        outcome = engine.execute({"id": "x", "name": "x", "action": {"kind": "boom"}})
    finally:
        del engine._ACTIONS["boom"]

    assert outcome.status == "failed"
    assert "handler exploded" in outcome.summary
    assert outcome.permanent is False, (
        "an unexpected exception is not proof the action can never work, so it must "
        "not permanently disable the rule"
    )


def test_a_transient_failure_keeps_the_rule_armed() -> None:
    """The counterpart to the disable path: a failure that might pass next time.

    Without this, "disable on permanent failure" is one refactor away from becoming
    "disable on any failure", which would retire a working reminder because the disk
    was busy once.
    """
    def flaky(automation: dict, action: dict) -> engine.Outcome:
        return engine.Outcome(status="failed", summary="network was down")

    engine._ACTIONS["flaky"] = engine.ActionKind(kind="flaky", run=flaky)
    automation = make({"kind": "flaky"})
    try:
        result = engine.run_due(at=after(automation, 1))
    finally:
        del engine._ACTIONS["flaky"]

    assert result.failed == 1
    row = repo.get(automation["id"]) or {}
    assert row["enabled"] is True, "a transient failure retired the rule"
    assert row["next_run_at"] is not None


# -------------------------------------------------------------------- status


def test_status_reports_due_count_live() -> None:
    """A stalled engine reporting a cached zero looks exactly like a healthy idle one."""
    automation = make({"kind": "notify", "message": "pending"})
    repo.update(automation["id"], schedule="30s")

    state = engine.status()
    assert state["running"] is False  # no loop started in tests
    assert "notify" in state["action_kinds"]
    assert isinstance(state["due_now"], int)


def test_the_engine_is_not_started_twice() -> None:
    """Two loops would fire every reminder twice."""
    assert engine.running() is False
    assert engine._state.task is None


# --------------------------------------------------------------- the loop itself


def test_the_started_loop_actually_fires_a_due_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``start()`` must reach ``run_due``, not merely create a task.

    Every other test in this file calls ``run_due`` directly, which proves the fire
    path and says nothing about whether anything ever calls it. That distinction is
    not hypothetical here: this entire feature spent its life as a correct repository
    that nothing invoked. A test suite that only exercised ``run_due`` would have been
    just as green against an engine that was never started.

    The tick is patched rather than configured because ``MIN_TICK_SECONDS`` is a
    deliberate floor of five seconds and a test must not wait for it.
    """
    monkeypatch.setattr(engine, "_tick_seconds", lambda: 0.01)
    automation = make({"kind": "notify", "message": "loop delivered this"})
    # Due already, so the first tick after the initial sleep picks it up.
    repo.update(automation["id"], schedule="30s")
    monkeypatch.setattr(
        engine.repo, "due", lambda **kw: repo.list_all()  # every rule counts as due
    )

    async def drive() -> None:
        assert engine.start() is True
        try:
            for _ in range(200):
                await asyncio.sleep(0.01)
                if repo.runs(automation["id"]):
                    break
        finally:
            await engine.stop()

    asyncio.run(drive())

    runs = repo.runs(automation["id"])
    assert runs, "the engine was started but never ran a due automation"
    assert runs[0]["status"] == "completed"
    assert engine.running() is False, "stop() left the loop alive"


def test_a_second_start_does_not_create_a_second_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two loops would deliver every reminder twice."""
    monkeypatch.setattr(engine, "_tick_seconds", lambda: 0.01)

    async def drive() -> None:
        assert engine.start() is True
        try:
            assert engine.start() is False, "a second loop was started"
        finally:
            await engine.stop()

    asyncio.run(drive())
