"""Tests for the proactive engine (L10): observation -> score -> proposal.

The engine under test is the real one -- real bus, real scoring, real settings
store against the temporary database -- because the failure modes this file
exists to catch are wiring failures: a suggestion published while the master
switch is off, a quiet-hours hold that never releases, a malformed observation
killing the subscriber. None of those are visible through a mocked engine.

What stays out is the machine: no monitor samples anything. Observations are
published by hand onto the real bus, which is exactly how the supervisor
delivers them (``observation.<kind>`` with the monitor's id in the payload).

Async tests follow the suite's ``@sync`` convention (``asyncio.run`` per test)
because the engine's tasks must live and die inside the test: an engine leaked
across tests would publish suggestions into the next one.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
from datetime import datetime
from typing import Any, Callable, Iterator

import pytest

from nexus.core.events import bus
from nexus.core import proactive
from nexus.db.repos import activity, app_settings
from nexus.monitors.base import Observation


def sync(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Run one coroutine test to completion, then assert the engine stopped."""
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = asyncio.run(_run_with_engine(fn, *args, **kwargs))
        assert proactive.running() is False  # nothing leaked into the next test
        return result

    return wrapper


async def _run_with_engine(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """One fresh engine per test, stopped and cleared afterwards.

    Counters, gate, held queue and stop-reason all reset, so a test reads the
    engine's own history, not the previous test's.
    """
    proactive._state.__init__()
    proactive._state.attention = proactive.Attention()
    # The timer loop sleeps first, so its first sleep is committed with
    # whatever GATE_POLL_SECONDS holds at start(): a test that shortens it
    # later only shortens the *second* tick. Shorten it before start.
    original_poll = proactive.GATE_POLL_SECONDS
    proactive.GATE_POLL_SECONDS = 0.05
    assert proactive.start() is True
    # The engine's subscriber registers only on its first loop turn; a test
    # that publishes before that turn would publish into the void. Wait for
    # the subscription to be live before the test body runs.
    while bus.subscriber_count == 0:
        await asyncio.sleep(0)
    try:
        return await fn(*args, **kwargs)
    finally:
        await proactive.stop()
        proactive.GATE_POLL_SECONDS = original_poll
        proactive._state.__init__()


def obs(kind: str, **kwargs: Any) -> Observation:
    return Observation(kind=kind, summary=f"{kind} observed", **kwargs)


def publish_observation(o: Observation) -> None:
    """The supervisor's publish shape, verbatim (supervisor.py ``_emit``)."""
    bus.publish(f"observation.{o.kind}", {"monitor": "test", **o.to_dict()})


async def _drain_bus() -> None:
    """Give the engine's subscriber loop turns to consume what was published.

    ``bus.publish`` offers onto subscriber queues synchronously, but the
    subscriber's ``async for`` needs loop turns to score and deliver. A few
    zero-second sleeps yield those turns.
    """
    for _ in range(20):
        await asyncio.sleep(0)


class collecting_suggestions:
    """Async context manager collecting ``proactive.suggestion`` payloads.

    Must wrap the publish, not follow it: ``bus.subscribe`` registers only on
    its first ``__anext__`` and never replays for ``after_seq=0``, so an
    event published before the subscription exists is missed. Starting the
    consumer task first guarantees the subscription is live.
    """

    def __init__(self) -> None:
        self.collected: list[dict[str, Any]] = []

    async def __aenter__(self) -> list[dict[str, Any]]:
        self._task = asyncio.create_task(self._consume())
        while bus.subscriber_count == 0:
            await asyncio.sleep(0)
        return self.collected

    async def __aexit__(self, *exc: object) -> None:
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def _consume(self) -> None:
        agen = bus.subscribe(topics=("proactive",))
        try:
            while True:
                event = await agen.__anext__()
                if event.type == "proactive.suggestion":
                    self.collected.append(event.payload)
        except asyncio.CancelledError:
            raise
        finally:
            await agen.aclose()


def delivery_settings() -> None:
    """Master switch on, quiet hours off: every suggestion test needs both.

    Quiet hours default to 22:00-08:00, and the suite has run inside that
    window in real life; a test that leaves the default would silently test
    the hold path instead of the delivery path.
    """
    app_settings.set("proactive_suggestions_enabled", True)
    app_settings.set("proactive_quiet_hours", "")


# --------------------------------------------------------------------- wiring


@sync
async def test_an_observation_worth_acting_on_becomes_a_cited_proposal():
    delivery_settings()

    # A disk at 99% is the one rule that clears the "act" bar on its numbers
    # alone: actionability 0.75, weight 0.71, urgency 0.90. Battery at 15%
    # scores only "investigate" (actionability 0.30) and is the wrong subject
    # for this test.
    async with collecting_suggestions() as collected:
        publish_observation(obs("system.disk", data={"percent_used": 99}))
        await _drain_bus()
    assert len(collected) == 1
    payload = collected[0]

    judgement = payload["judgement"]
    assert judgement["kind"] == "system.disk"
    # The citation is the whole judgement, not a summary of it: the five
    # scored numbers, the reasons, and the dedupe key, so the user can argue
    # with the rule.
    assert {"importance", "urgency", "confidence", "relevance", "actionability",
            "reasons", "dedupe_key"} <= set(judgement)
    assert payload["summary"]
    assert payload["activity_id"]  # the Activity row exists and is cited
    assert proactive._state.proposed == 1
    # The act disposition is opinion, not authority: even scored "act", the
    # payload is a suggestion; nothing ran.
    assert judgement["disposition"] == "act"
    assert "disk essentially full" in judgement["reasons"]


@sync
async def test_the_master_switch_off_means_no_suggestions_leave_the_engine():
    app_settings.set("proactive_suggestions_enabled", True)
    app_settings.set("proactive_quiet_hours", "")
    app_settings.set("proactive_suggestions_enabled", False)

    publish_observation(obs("system.battery", data={"percent": 15, "charging": False}))
    await _drain_bus()

    assert proactive._state.offered == 1  # scored, not ignored
    assert proactive._state.proposed == 0  # but never delivered
    # And nothing is waiting to escape later: the switch-off path is not a hold.
    assert not proactive._state.held


@sync
async def test_a_stopped_monitor_is_not_an_observation_waiting_to_be_suggested():
    # monitor.stopped is the supervisor's own record, not an observation this
    # engine scores: the subscriber filters observation.* only.
    app_settings.set("proactive_quiet_hours", "")
    bus.publish("monitor.stopped", {"monitor": "test", "reason": "test"})
    await _drain_bus()
    assert proactive._state.offered == 0


@sync
async def test_quiet_hours_hold_and_release():
    app_settings.set("proactive_suggestions_enabled", True)
    # 00:00-23:59 is quiet for every local time except exactly 23:59, so the
    # hold always engages without a test having to fake the clock.
    app_settings.set("proactive_quiet_hours", "00:00-23:59")

    publish_observation(obs("system.battery", data={"percent": 15, "charging": False}))
    await _drain_bus()
    assert proactive._state.proposed == 0
    assert len(proactive._state.held) == 1

    # Window opens: the timer drains held suggestions on its next tick. The
    # fixture already set GATE_POLL_SECONDS short before start(), so the tick
    # that releases the hold lands within the wait below.
    app_settings.set("proactive_quiet_hours", "")
    for _ in range(50):
        if proactive._state.released_from_hold == 1:
            break
        await asyncio.sleep(0.02)
    assert proactive._state.proposed == 1
    assert proactive._state.released_from_hold == 1


@sync
async def test_rate_limit_one_condition_one_suggestion():
    delivery_settings()

    # Battery at 15% scores "investigate" (actionability 0.30, weight 0.56):
    # notify-class and above, so the gate sees it and delivers once.
    async with collecting_suggestions() as collected:
        publish_observation(obs("system.battery", data={"percent": 15, "charging": False}))
        await _drain_bus()
    assert len(collected) == 1
    assert collected[0]["suppressed"] == 0
    assert proactive._state.proposed == 1

    # Same condition, same key, inside the cooldown: held by the gate, not
    # delivered, and counted so the next release says how many it stands for.
    publish_observation(obs("system.battery", data={"percent": 14, "charging": False}))
    await _drain_bus()
    assert proactive._state.suppressed >= 1
    assert proactive._state.proposed == 1  # still exactly one


@sync
async def test_malformed_observation_does_not_kill_the_subscriber():
    delivery_settings()

    bus.publish("observation.", {"monitor": "test", "kind": ""})  # empty kind
    await _drain_bus()
    # The subscriber is still alive: a second, well-formed observation flows.
    async with collecting_suggestions() as collected:
        publish_observation(obs("system.battery", data={"percent": 15, "charging": False}))
        await _drain_bus()
    assert collected[0]["judgement"]["kind"] == "system.battery"


@sync
async def test_the_log_disposition_writes_an_activity_row_without_speaking():
    delivery_settings()

    # An unknown kind with no rule scores low: default rule, weight 0.1, which
    # lands at 'log' -- recorded, never spoken (the disposition exists for
    # exactly this, see scoring.RateGate's docstring).
    publish_observation(obs("mystery.noise"))
    await _drain_bus()

    assert proactive._state.offered == 1
    assert proactive._state.proposed == 0  # never a suggestion event
    rows = activity.list_recent(kinds=("proactive",), limit=5)
    assert rows and any("mystery.noise" in (r["summary"] or "") for r in rows)


# ------------------------------------------------------------- honest failure


@sync
async def test_status_says_why_the_loop_stopped():
    delivery_settings()

    async def explode() -> None:
        raise RuntimeError("boom")

    original = proactive._drain_gate
    proactive._drain_gate = explode
    proactive.GATE_POLL_SECONDS = 0.05
    try:
        await asyncio.sleep(0.5)
        status = proactive.status()
        assert status["running"] is False
        assert status["stopped_reason"].startswith("stopped after")
        assert proactive.running() is False
    finally:
        proactive._drain_gate = original
        proactive.GATE_POLL_SECONDS = 30.0


# ------------------------------------------------------------- quiet hours


def test_quiet_hours_window_parses_and_crosses_midnight():
    app_settings.set("proactive_quiet_hours", "")
    assert proactive._in_quiet_hours(datetime(2026, 9, 8, 23, 30)) is False  # no window

    app_settings.set("proactive_quiet_hours", "22:00-08:00")
    assert proactive._in_quiet_hours(datetime(2026, 9, 8, 23, 30)) is True
    assert proactive._in_quiet_hours(datetime(2026, 9, 8, 12, 0)) is False
    assert proactive._in_quiet_hours(datetime(2026, 9, 8, 3, 0)) is True  # after midnight

    app_settings.set("proactive_quiet_hours", "nonsense")
    # Unparseable is NOT quiet: a typo must not silence real alerts.
    assert proactive._in_quiet_hours(datetime(2026, 9, 8, 23, 30)) is False


def test_status_shape():
    status = proactive.status()
    assert {
        "running",
        "enabled",
        "quiet_hours",
        "in_quiet_hours",
        "offered",
        "proposed",
    } <= set(status)
    assert status["running"] in (True, False)  # present even before start()
