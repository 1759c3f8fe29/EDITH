"""The proactive engine (L10): rules over the ambient stream, ending at proposals.

This module is the missing consumer of :mod:`nexus.core.scoring` -- the half
that was written, tested in isolation, and never wired to a producer. Monitors
publish ``observation.<kind>`` on the event bus; ``score()`` turns one into a
judgement; this engine owns everything after that:

    observation -> score() -> disposition -> proposal (never an action)

Three rules from the spec shape the module (TASKS.md L10, doc2 §31, §82):

* **Observation is not authorization.** Whatever ``score()`` says -- even
  ``act`` -- the engine's only output is a *suggestion* that cites the
  observation that produced it. The user accepts or ignores; nothing runs a
  tool because a monitor saw something.
* **Quiet hours and per-rule rate limits.** A suggestion that arrives during
  the quiet window is held (counted, not dropped) and released when the window
  opens. The ``RateGate`` keys on the observation's condition, so two rules
  about different things never share a cooldown and one condition cannot
  speak twice inside its window.
* **Every suggestion cites its observation.** The payload carries the whole
  judgement -- kind, the five numbers, the reasons, the dedupe key -- so the

  user can argue with the rule, not with the assistant.

What it deliberately does not do: call a model, run a tool, or change state.
``act`` and ``investigate`` dispositions appear verbatim in the payload (the
scoring layer's opinion is information), but the engine acts on neither. That
is the whole of L10: *NEXUS proposes; it never acts unasked.*

Attention state (doc2 §9) starts neutral and is updated only by facts this
engine can read. There is no ``window.activity`` or ``voice.speaking`` event on
the bus today, so activity stays ``"idle"`` and ``speaking`` stays False; the
scoring layer's demotions work correctly against a neutral attention, and
nothing here guesses. When those events exist, the subscriber below is the one
place that learns about them.

Honesty about failure follows the codebase's pattern (§60): a background loop
that has given up must say so. After ``MAX_CONSECUTIVE_ERRORS`` consecutive
failures the loop stops and ``status()`` reports ``stopped_reason`` rather than
looking alive.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from typing import Any

from ..logging_setup import get_logger
from .events import bus
from .scoring import CRITICAL_URGENCY, Attention, Judgement, RateGate, score
from ..db.repos import activity, app_settings
from ..monitors.base import Observation

log = get_logger("proactive")

#: A loop that has failed this many times in a row stops itself and says why,
#: matching memory_sweep and the monitor supervisor. A proactive engine that
#: crashed every second but kept "running" would be a liar with a good accent.
MAX_CONSECUTIVE_ERRORS = 5

#: How often the engine drains releases whose cooldown expired. The gate's
#: ``due()`` is the path for conditions that stopped repeating while still
#: true; without a timer, "disk 95% full" would be spoken once and never again
#: -- exactly the silence the gate was built to end.
GATE_POLL_SECONDS = 30.0

#: Held suggestions are bounded. The quiet-hours hold keeps at most this many
#: payloads; older ones are dropped and counted, so a leak cannot hide inside a
#: feature that is supposed to be quiet.
HELD_LIMIT = 50

#: Activity kind used for every row this engine writes. ``proactive`` is not
#: in the activity repo's KINDS tuple -- that tuple drives list filters, not
#: validation, and a suggestion IS activity the user can see and search.
ACTIVITY_KIND = "proactive"


# ------------------------------------------------------------------- settings


def _enabled() -> bool:
    """The master switch. Default off -- a proactive engine nobody asked for
    is the failure mode L10 exists to prevent, not the feature itself.

    Read on every tick, not at start: flipping the setting must take effect
    without finding and killing this task.
    """
    return bool(app_settings.get("proactive_suggestions_enabled", False))


def _quiet_window() -> tuple[dt_time, dt_time] | None:
    """Parse ``proactive_quiet_hours`` (``"22:00-08:00"``) into local times.

    A window may cross midnight -- that is the normal shape of a sleeping
    schedule. Unparseable or empty means *no quiet hours*: silence enforced by
    a typo would be the one failure mode here that hides real alerts, so the
    engine errs toward speaking.
    """
    raw = str(app_settings.get("proactive_quiet_hours", "") or "")
    if not raw.strip():
        return None
    try:
        start_s, end_s = raw.split("-", 1)
        start = dt_time.fromisoformat(start_s.strip())
        end = dt_time.fromisoformat(end_s.strip())
    except ValueError:
        return None
    if start == end:
        return None
    return start, end


def _in_quiet_hours(now: datetime | None = None) -> bool:
    """Whether ``now`` local falls inside the quiet-hours window."""
    window = _quiet_window()
    if window is None:
        return False
    start, end = window
    moment = (now or datetime.now()).time()
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end  # crosses midnight


# ---------------------------------------------------------------------- state


@dataclass
class _EngineState:
    task: asyncio.Task[None] | None = None
    subscriber: asyncio.Task[None] | None = None
    attention: Attention = field(default_factory=Attention)
    #: The one gate for all rules; the per-rule key is the judgement's dedupe
    #: key or kind, exactly as RateGate keys internally.
    gate: RateGate = field(default_factory=lambda: RateGate(cooldown_seconds=600.0))
    consecutive_errors: int = 0
    stopped_reason: str = ""
    #: Suggestions held because they arrived during quiet hours.
    held: list[dict[str, Any]] = field(default_factory=list)
    # Counters for status() -- the UI can show "watching, quiet, 3 held".
    offered: int = 0
    proposed: int = 0
    suppressed: int = 0
    released_from_hold: int = 0
    last: dict[str, Any] | None = None


_state = _EngineState()


# ------------------------------------------------------------------- delivery


def _summary_for(judgement: Judgement) -> str:
    """One plain line for the log. Reports the condition and the disposition,
    without pretending to be the assistant or paraphrasing the observation away."""
    return f"Suggestion ({judgement.disposition}): {judgement.kind}"


def _deliver(payload: dict[str, Any]) -> None:
    """Publish one suggestion: activity row first, then the bus event.

    The automations' notify action established this order. Writing the row
    first means a suggestion that crashes the process still left a trace the
    user can find in Activity; publishing first would leave the row unwritten
    when the process dies between the two calls.
    """
    judgement = payload["judgement"]
    entry = activity.record(
        ACTIVITY_KIND,
        payload["summary"],
        detail={"judgement": judgement, "held": payload.get("held", False)},
        level=(  # type: ignore[arg-type]
            "warning" if judgement.get("urgency", 0) >= CRITICAL_URGENCY else "info"
        ),
    )
    _state.proposed += 1
    _state.last = {"kind": judgement.get("kind"), "activity_id": entry.get("id")}
    bus.publish("proactive.suggestion", {**payload, "activity_id": entry.get("id")})


def _propose(judgement: Judgement, summary: str, *, suppressed: int = 0) -> None:
    """Take one released judgement to the user, or hold it for quiet hours."""
    payload = {
        "judgement": judgement.to_dict(),
        "summary": summary,
        "suppressed": suppressed,
    }
    if _in_quiet_hours():
        _state.held.append(payload)
        del _state.held[:-HELD_LIMIT]  # bounded: a hold that grows forever is a leak
        return
    _deliver(payload)


# ------------------------------------------------------------------ the engine


async def _handle_observation(payload: dict[str, Any]) -> None:
    """One observation through scoring, the gate, and quiet hours.

    Everything needed arrives in the payload: the supervisor publishes
    ``observation.<kind>`` with ``{"monitor": <id>, **observation.to_dict()}``.
    ``Observation`` has no ``from_dict`` (it is a producer-facing record), so
    the engine reconstructs it field by field, and a malformed event is a log
    line, not a crash.
    """
    attention = _state.attention
    try:
        observation = Observation(
            kind=str(payload.get("kind") or ""),
            summary=str(payload.get("summary") or ""),
            data=dict(payload.get("data") or {}),
            severity=str(payload.get("severity") or "info"),
            dedupe_key=str(payload.get("dedupe_key") or ""),
            clears=bool(payload.get("clears") or False),
            at=str(payload.get("at") or ""),
        )
    except Exception:  # noqa: BLE001
        log.warning("proactive engine dropped a malformed observation")
        return
    if not observation.kind:
        return

    judgement = score(observation, attention)
    _state.offered += 1
    if judgement.disposition == "ignore":
        return
    if judgement.disposition == "log":
        # The log disposition stores without speaking. It never passes through
        # the gate: rate-limiting a history row would lose history for no
        # benefit, which is why RateGate itself only sees notify and above.
        # Logged even while the master switch is off, for the same reason
        # ambient history keeps writing: a record of what was seen is not an
        # interruption, and the counters stay honest for status().
        if _enabled():
            activity.record(
                ACTIVITY_KIND,
                _summary_for(judgement),
                detail={"judgement": judgement.to_dict(), "logged": True},
            )
        return
    summary = _summary_for(judgement)
    aggregate = _state.gate.offer(judgement, summary)
    if aggregate is None:
        _state.suppressed += 1
        return
    # Master switch gates delivery, not scoring: the counters in status()
    # stay truthful (the engine sees everything), while a disabled engine
    # never puts a suggestion in front of the user.
    if not _enabled():
        _state.suppressed += 1
        return
    _propose(aggregate.judgement, aggregate.summary, suppressed=aggregate.suppressed)


async def _drain_held() -> None:
    """Release held suggestions once quiet hours end, keeping the newest.

    Held suggestions were true when held; by release time they may be stale.
    The newest carries the freshest reading, and older ones are counted into
    its ``suppressed`` so the release can say "over the last few hours, N
    events" rather than replaying every stale step.
    """
    if not _state.held or _in_quiet_hours():
        return
    held = _state.held
    _state.held = []
    newest = held[-1]
    older = len(held) - 1
    if older:
        # Older held payloads are counted into the newest release rather than
        # replayed: their readings are stale, but how many there were is not.
        suppressed = int(newest.get("suppressed") or 0) + older
        newest = {**newest, "suppressed": suppressed}
    _deliver(newest)
    _state.released_from_hold += 1
    _state.suppressed += older


async def _drain_gate() -> None:
    """Release what the gate held while a cooldown ran (``RateGate.due``)."""
    for aggregate in _state.gate.due():
        j = aggregate.judgement
        if j.disposition in ("ignore", "log"):
            continue
        _propose(j, aggregate.summary, suppressed=aggregate.suppressed)


async def _subscriber() -> None:
    """Read the bus forever, scoring every observation.

    Topics are plain prefixes: ``"observation"`` matches every
    ``observation.<kind>``. The engine also listens to ``monitor`` events so a
    stopped monitor can be noticed, but attention state is never guessed from
    them -- no window/voice event exists yet, so nothing is inferred.
    """
    agen = bus.subscribe(topics=("observation", "monitor"))
    try:
        async for event in agen:
            if event.type.startswith("observation."):
                await _handle_observation(event.payload)
            # monitor.* events are not observations and do not flow into
            # scoring; they are kept out of the engine deliberately. The
            # scoring rules for monitor.stopped exist for the ambient rows the
            # supervisor writes, which already reach the Activity screen.
    finally:
        with contextlib.suppress(Exception):
            await agen.aclose()


async def _loop() -> None:
    """Run the timer half: drain held suggestions, then gate releases.

    Sleeps first (startup is the busiest moment in the process). The master
    switch is read on every tick, so flipping the setting takes effect
    without restarting the task. Errors are counted, never fatal, until
    MAX_CONSECUTIVE_ERRORS in a row -- then the loop stops itself and says why.
    """
    while True:
        try:
            await asyncio.sleep(GATE_POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        if not _enabled():
            continue
        try:
            await _drain_held()
            await _drain_gate()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- the loop outlives one bad second
            _state.consecutive_errors += 1
            log.exception("proactive engine tick failed")
            if _state.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                _state.stopped_reason = (
                    f"stopped after {_state.consecutive_errors} consecutive failures"
                )
                log.warning("proactive engine %s", _state.stopped_reason)
                return
        else:
            _state.consecutive_errors = 0


# -------------------------------------------------------------- lifecycle API


def start() -> bool:
    """Start the engine. Returns whether a new task was created.

    Idempotent, so a settings change can call it again without a second
    subscriber racing the first over the same observations.
    """
    if _state.task is not None and not _state.task.done():
        return False
    _state.stopped_reason = ""
    _state.consecutive_errors = 0
    _state.task = asyncio.create_task(_loop(), name="proactive:engine")
    _state.subscriber = asyncio.create_task(_subscriber(), name="proactive:subscriber")
    return True


async def stop() -> None:
    """Cancel the loop and the subscriber."""
    tasks = (_state.task, _state.subscriber)
    _state.task = None
    _state.subscriber = None
    for task in tasks:
        if task is None or task.done():
            continue
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def running() -> bool:
    loop_alive = _state.task is not None and not _state.task.done()
    sub_alive = _state.subscriber is not None and not _state.subscriber.done()
    return loop_alive and sub_alive


def status() -> dict[str, Any]:
    """What the engine is doing, truthfully -- including having given up.

    ``offered`` counts every observation scored (including ignored ones);
    ``suppressed`` is what the gate held during cooldowns; ``held`` is the
    current quiet-hours backlog. ``stopped_reason`` is set only when the loop
    self-stopped, and is shown instead of pretending to run.
    """
    return {
        "running": running(),
        "enabled": _enabled(),
        "quiet_hours": str(app_settings.get("proactive_quiet_hours", "22:00-08:00")),
        "in_quiet_hours": _in_quiet_hours(),
        "offered": _state.offered,
        "proposed": _state.proposed,
        "suppressed": _state.suppressed,
        "held": len(_state.held),
        "released_from_hold": _state.released_from_hold,
        "last": _state.last,
        "stopped_reason": _state.stopped_reason,
    }
