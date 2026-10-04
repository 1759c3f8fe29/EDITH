"""The automation engine (spec §31-§34, §47 Automations screen; §1's own example).

The ``automations`` table, the ``automation_runs`` table, the
``(enabled, next_run_at)`` index and a complete repository -- ``due()``,
``start_run()``, ``finish_run()``, ``reconcile_running_runs()`` -- were written and
then never called by anything outside ``db/``. "Every evening remind me to study" is
the example the product vision leads with, and until this module existed the honest
description of that feature was: a schema.

This is the loop that makes the stored rows do something. It is deliberately small,
and its shape is taken from :mod:`nexus.ai.memory_sweep`, which solves the same
problem -- a periodic task that must be safe to leave running for weeks.

Three properties are the whole design:

**A reminder must survive the UI being closed.** The event bus is in-process with a
bounded replay buffer, so an automation that only published an event would be a
reminder that exists if and only if someone happened to be watching. Every fire
writes an ``activity`` row first -- durable, already a declared activity kind -- and
publishes second. The publish is how an open UI hears about it *now*; the row is how
a closed one hears about it later.

**A failure that will never stop failing is not rescheduled.** ``finish_run``
reschedules by default, which is right for a transient error and wrong for an action
naming a kind nothing can execute. Those are disabled with a reason via
``mark_unavailable`` instead. This distinction is not tidiness: ``due()`` selects on
``next_run_at <= now``, so a permanent failure that is rescheduled to the same past
instant is re-selected on every tick, forever. The choice is between saying why once
and failing in a hot loop.

**A late reminder says it is late, or is not delivered at all.** If the core was off
overnight, an 08:30 reminder is not "now". Up to :data:`MAX_LATENESS_SECONDS` it is
delivered with the delay recorded in the run detail and the activity row; past that
it is recorded as ``skipped``, because a reminder that arrives three days after the
evening it was for is not a reminder, and presenting it as current is exactly the
invented-state problem the spec forbids.

What an automation can *do* is currently one thing: ``notify``. That is a deliberate
floor rather than a placeholder. It is the action the product vision's own example
needs, it needs no consent to run unattended, and it is fully delivered end to end.
Kinds that are not implemented are refused loudly -- the automation is disabled and
the reason is on the row -- rather than accepted and quietly ignored, so the
Automations screen can never show a rule that looks armed and does nothing. Adding
``prompt`` or ``tool`` later is an entry in :data:`_ACTIONS`; both need a decision
about consent for unattended execution that this module does not pretend to have
made.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .core.events import bus
from .db.repos import activity, app_settings, automations as repo
from .permissions import capabilities as capability_catalogue
from .permissions.engine import engine as permission_engine
from .logging_setup import get_logger

log = get_logger("automations")

#: How often the engine looks for due automations. The finest schedule the repo
#: accepts is a minute (``"08:30"``) and the shortest interval is 30 seconds, so a
#: 20-second tick keeps worst-case lateness well inside the resolution of anything
#: that can be stored. It is not a busy loop: the query is one indexed lookup
#: against ``(enabled, next_run_at)`` and returns nothing almost every time.
TICK_SECONDS = 20.0

#: The floor, whatever the setting says. A tick faster than this would be a database
#: query per second to answer a question whose input changes once a minute.
MIN_TICK_SECONDS = 5.0

#: How late a run may be and still be delivered, default six hours. Chosen so that
#: turning the machine on in the morning still delivers that morning's reminder,
#: while one from two days ago is recorded as missed instead of arriving as though
#: it were current.
MAX_LATENESS_SECONDS = 6 * 60 * 60

#: Consecutive failed *ticks* -- not failed runs -- after which the loop gives up
#: and ``status()`` says so. A failing tick means the query itself raised, so
#: retrying forever would be a log line every 20 seconds about a database that is
#: not coming back. An automation whose action fails is a different thing entirely
#: and is recorded per-run without touching this counter.
MAX_CONSECUTIVE_ERRORS = 5


@dataclass
class Outcome:
    """What executing one automation's action did."""

    status: str  # completed | failed | skipped
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)
    #: True when re-running could never succeed, so the automation is disabled with
    #: ``summary`` as its reason instead of being rescheduled into a hot loop.
    permanent: bool = False
    #: Set when the run was refused for want of a permission, naming the capability
    #: that was missing. Stored in its own column so "what is being refused, and to
    #: which rules" is answerable in SQL rather than by reading JSON blobs.
    denied_capability: str | None = None


def _notify(automation: dict[str, Any], action: dict[str, Any]) -> Outcome:
    """Deliver a reminder: a durable activity row, then a live event.

    The order matters. Writing first means a reminder that reaches the user even if
    nothing is connected when it fires; publishing first and crashing would lose it
    with no trace.
    """
    message = str(action.get("message") or "").strip()
    if not message:
        return Outcome(
            status="failed",
            summary="the notify action has no message to deliver",
            permanent=True,
        )

    level = str(action.get("level") or "info")
    if level not in ("info", "success", "warning", "error"):
        level = "info"

    detail = {
        "automation_id": automation["id"],
        "automation": automation["name"],
        "message": message,
    }
    entry = activity.record(
        "automation",
        message,
        detail=detail,
        level=level,  # type: ignore[arg-type]
    )
    bus.publish(
        "automation.fired",
        {
            "automation_id": automation["id"],
            "name": automation["name"],
            "message": message,
            "level": level,
            "activity_id": entry.get("id"),
        },
    )
    return Outcome(status="completed", summary=message, detail={"activity_id": entry.get("id")})


@dataclass(frozen=True)
class ActionKind:
    """One thing an automation can do, and the powers doing it requires.

    ``capabilities`` lives on the registry entry rather than in a lookup table
    somewhere else, because then adding an action kind *is* declaring its powers.
    A parallel table can be forgotten by whoever adds the next kind, and the
    failure mode of forgetting would be an action that runs unattended having
    declared it needs nothing -- exactly the silent over-reach the two checks in
    :func:`_capability_gate` exist to prevent.
    """

    kind: str
    run: Callable[[dict[str, Any], dict[str, Any]], Outcome]
    #: Names from :mod:`nexus.permissions.capabilities`. Validated against that
    #: catalogue at import, so a typo is an ImportError and not a fire-time
    #: surprise -- and, because ``evaluate`` denies unknown capabilities, not a
    #: silently permanently-broken rule either.
    capabilities: frozenset[str] = frozenset()


#: The action registry. A dict rather than a chain of ``if`` so that "which kinds
#: exist" is one readable list, and so an unknown kind is a lookup miss with a
#: message naming what *is* available rather than a silent fall-through.
_ACTIONS: dict[str, ActionKind] = {
    "notify": ActionKind(
        kind="notify",
        run=_notify,
        # Empty is a claim, not an omission. ``_notify`` writes an ``activity`` row
        # and publishes on the in-process bus; it does not call the desktop
        # notification daemon, so ``notify.send`` is genuinely not among its powers.
        # Two things rest on this being accurate: a notify-only rule may run
        # unattended without consent, and migration v4's ``DEFAULT '[]'`` correctly
        # describes every automation that exists today.
        capabilities=frozenset(),
    ),
}

def action_kinds() -> tuple[str, ...]:
    """Which action kinds exist, read from the registry rather than copied from it.

    This was a module-level ``tuple(_ACTIONS)`` and a test caught what that costs: the
    routes validated an incoming action against a snapshot taken at import while the
    engine dispatched from the dict, so the two could disagree about what NEXUS can
    do. One of them accepting a kind the other cannot run is exactly the shape of a
    rule that looks armed and does nothing.
    """
    return tuple(_ACTIONS)

for _spec in _ACTIONS.values():
    for _cap in sorted(_spec.capabilities):
        if not capability_catalogue.exists(_cap):
            raise RuntimeError(
                f"action {_spec.kind!r} declares capability {_cap!r}, which is not in "
                "nexus.permissions.capabilities"
            )


class UnknownActionKind(ValueError):
    """The action kind is not in the registry, so its powers cannot be known."""


def required_capabilities(action: dict[str, Any] | None) -> frozenset[str]:
    """The capabilities running ``action`` would need.

    Raises rather than returning an empty set for an unrecognised kind. Empty means
    "needs nothing", and an action NEXUS cannot identify has not earned that
    description -- returning it would let a future kind reach the machine simply by
    not being registered yet (§5).
    """
    kind = str((action or {}).get("kind") or "").strip()
    spec = _ACTIONS.get(kind)
    if spec is None:
        raise UnknownActionKind(kind)
    return spec.capabilities


def normalise_scope(scope: object) -> list[str]:
    """One canonical form for a scope: sorted, de-duplicated, strings only."""
    if not isinstance(scope, list):
        return []
    return sorted({s for s in scope if isinstance(s, str) and s})


def unknown_capabilities(scope: object) -> list[str]:
    """Names in ``scope`` that no capability in the catalogue answers to.

    Worth checking at the door because the alternative is worse than a typo: the
    permission engine denies an unknown capability as a hard limit, so a scope
    naming one would arm a rule that can never run and whose refusal reads as
    NEXUS being locked down rather than as a misspelling.
    """
    return [n for n in normalise_scope(scope) if not capability_catalogue.exists(n)]


def scope_surplus(action: dict[str, Any] | None, scope: object) -> list[str]:
    """Capabilities ``scope`` carries that ``action`` has no use for, sorted.

    Surplus is not dangerous -- :func:`_capability_gate` only ever checks coverage --
    but it is a disagreement between whoever built the consent and what the action
    actually does, and a rule recorded as authorised for something it cannot do is a
    false statement about the system (§78). Reported, not trimmed: trimming would
    hide the disagreement, and rewriting a record of consent is not this layer's
    call to make.
    """
    return sorted(set(normalise_scope(scope)) - required_capabilities(action))


def _gap(needed: frozenset[str], scope: object) -> list[str]:
    """The one subtraction. ``scope`` is validated rather than trusted, so a value
    that is not a list of strings carries nothing -- which fails closed."""
    return sorted(needed - set(normalise_scope(scope)))


def scope_gap(action: dict[str, Any] | None, scope: object) -> list[str]:
    """Capabilities ``action`` needs that ``scope`` does not carry, sorted.

    Empty result means the stored consent covers the action. Raises for an action kind
    that is not registered, because a scope cannot be checked against powers nobody
    has declared -- see :func:`required_capabilities`.
    """
    return _gap(required_capabilities(action), scope)


def _capability_title(name: str) -> str:
    """The catalogue's own words for a capability, for a message a person reads."""
    try:
        return capability_catalogue.get(name).title.lower()
    except KeyError:  # pragma: no cover -- the registry is validated at import
        return name


def _capability_gate(
    automation: dict[str, Any],
    action: dict[str, Any],
    spec: ActionKind,
    *,
    attended: bool = False,
) -> Outcome | None:
    """Two checks between a stored rule and the machine. ``None`` means clear to run.

    Nobody is watching when an automation fires, and that changes what a permission
    prompt would mean: there is no one to answer it. So the fire-time test is
    :meth:`~nexus.permissions.engine.PermissionEngine.evaluate`, not ``require`` --
    which would park a background loop for three minutes and then fail closed
    anyway -- and its ``ask`` outcome is read here as a refusal. "NEXUS would have
    asked you" is not consent.

    Both checks are load-bearing, and neither substitutes for the other:

    * The **stored scope** is what the user agreed this rule may do at the moment
      they armed it. Without it, an action later edited to need more power would
      inherit the trust its narrower version earned -- and in trusted mode, or with
      an ``always`` grant made for some unrelated task, that widening would never
      surface a prompt at all. This is the check that makes editing a rule honest.
    * The **live engine** is whether those powers still hold *now*. Without it, a
      permission revoked on the Permissions screen would go on working for every
      rule armed before the revocation, which would make that screen a lie.

    A refusal is ``failed``, never ``skipped``. ``skipped`` in this module means "not
    attempted, and that was the correct thing to do" -- a reminder too late to be a
    reminder -- and it is counted without a warning. A rule that cannot do its job
    because permission is missing is a rule not doing its job, and the user needs to
    be able to see that.
    """
    # The resolved registry entry is passed in rather than looked up again from the
    # action. A mutation test is why: an earlier version re-derived it here and caught
    # ``UnknownActionKind`` in a branch ``execute`` had already made unreachable, so a
    # mutant that changed what that branch returned survived. Dead defensive code is
    # worse than none -- it reads as a handled case.
    needed = spec.capabilities
    if not needed:
        return None

    gap = _gap(needed, automation.get("capability_scope"))
    if gap:
        return Outcome(
            status="failed",
            summary=(
                "this rule needs permission it was not given when you saved it: "
                + ", ".join(_capability_title(c) for c in gap)
                + " -- open it, check what it does, and save it again"
            ),
            detail={"missing_scope": gap, "granted_scope": automation.get("capability_scope")},
            # Permanent: re-running changes nothing, because the shortfall is in what
            # was consented to, not in what the machine will allow. ``mark_unavailable``
            # clears next_run_at, so the rule stops firing and says why on the row --
            # which is the UNAVAILABLE state the Automations screen already renders.
            permanent=True,
            denied_capability=gap[0],
        )

    # Params come from the action itself, because that is where a future kind puts
    # the path, URL or command the policy layer scopes on. Copied so the engine
    # cannot reach back into the row's stored dict.
    params = {k: v for k, v in action.items() if k != "kind"}

    for name in sorted(needed):
        decision = permission_engine.evaluate(name, params)
        if decision.allowed:
            continue
        asked = decision.outcome == "ask"
        return Outcome(
            status="failed",
            summary=(
                (
                    f"{_capability_title(name)} has not been granted -- allow it on the "
                    "Permissions screen and try again"
                    if attended
                    else f"{_capability_title(name)} needs your decision, and this rule "
                    "fired with nobody there to give it"
                )
                if asked
                else f"{_capability_title(name)} is not allowed: {decision.reason}"
            ),
            detail={
                "capability": name,
                "outcome": decision.outcome,
                "source": decision.source,
                "reason": decision.reason,
            },
            # Only a hard limit is permanent: it is refused in code, so no amount of
            # granting on the Permissions screen will change the answer, and retrying
            # daily forever would just be a warning a day. A denied or unasked
            # capability can be granted, so that rule keeps its schedule and starts
            # working the moment it is.
            permanent=decision.source == "hard_limit",
            denied_capability=name,
        )

    return None


def execute(automation: dict[str, Any], *, attended: bool = False) -> Outcome:
    """Run one automation's action. Never raises: every path returns an Outcome.

    ``attended`` is true only for RUN NOW, where the user is looking at the screen.
    It changes exactly one thing -- how a missing permission is explained, because
    "nobody is there to ask" is false when somebody just clicked a button -- and
    nothing about what is allowed. It is also the hook the first action kind that
    needs consent will use: an attended run can afford to ``await require(...)`` and
    actually put the question on screen, which an unattended one cannot.
    """
    action = automation.get("action")
    if not isinstance(action, dict) or not action:
        return Outcome(
            status="failed",
            summary="this automation has no action to run",
            permanent=True,
        )

    kind = str(action.get("kind") or "").strip()
    spec = _ACTIONS.get(kind)
    if spec is None:
        known = ", ".join(action_kinds())
        return Outcome(
            status="failed",
            summary=(
                f"{kind!r} is not an action NEXUS can run"
                if kind
                else "this automation's action does not say what kind it is"
            )
            + f" (available: {known})",
            permanent=True,
        )

    refusal = _capability_gate(automation, action, spec, attended=attended)
    if refusal is not None:
        return refusal

    try:
        return spec.run(automation, action)
    except Exception as exc:  # noqa: BLE001 -- one bad action must not stop the engine
        log.exception("automation %s raised", automation.get("id"))
        return Outcome(status="failed", summary=f"unexpected failure: {exc}")


def _lateness(automation: dict[str, Any], at: datetime) -> float:
    """Seconds between when this was due and now. Negative is impossible from
    ``due()`` and is clamped, so a clock adjustment cannot produce a nonsense delay."""
    stamp = automation.get("next_run_at")
    if not stamp:
        return 0.0
    try:
        due_at = datetime.fromisoformat(str(stamp))
    except ValueError:
        return 0.0
    if due_at.tzinfo is None:
        due_at = due_at.astimezone()
    return max(0.0, (at - due_at).total_seconds())


@dataclass
class TickResult:
    """What one pass over the due automations did."""

    fired: int = 0
    failed: int = 0
    skipped: int = 0
    at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "fired": self.fired,
            "failed": self.failed,
            "skipped": self.skipped,
            "at": self.at,
        }


def run_due(*, at: datetime | None = None) -> TickResult:
    """Fire every automation whose time has come. Safe to call directly.

    Missed runs collapse: an automation that was due three times while the core was
    down fires once, not three times, because ``due()`` reports the row and
    ``finish_run`` moves it to the *next* future occurrence. That is the right
    reading of a daily reminder -- the user wants tonight's, not a queue of the ones
    they slept through.
    """
    moment = at or datetime.now().astimezone()
    result = TickResult(at=moment.isoformat())

    for automation in repo.due(at=moment):
        late = _lateness(automation, moment)
        run_id = repo.start_run(automation["id"])

        if late > _max_lateness():
            hours = late / 3600
            repo.finish_run(
                run_id,
                automation["id"],
                status="skipped",
                detail={
                    "reason": "missed while the core was not running",
                    "late_seconds": round(late),
                    "due_at": automation.get("next_run_at"),
                },
            )
            result.skipped += 1
            log.info(
                "automation %r skipped: due %.1f hours ago", automation["name"], hours
            )
            continue

        outcome = execute(automation)
        detail = dict(outcome.detail)
        detail["summary"] = outcome.summary
        if late >= 1.0:
            # Recorded whenever it is real, not only when it is large: a reminder
            # delivered nine minutes late is still delivered late, and the run row
            # is the only place that fact survives.
            detail["late_seconds"] = round(late)
            detail["due_at"] = automation.get("next_run_at")

        if outcome.permanent:
            # The safety here is entirely ``mark_unavailable``: it clears
            # ``next_run_at``, which is what stops ``due()`` re-selecting the rule.
            #
            # ``reschedule=False`` is deliberately *not* load-bearing, and a mutation
            # test proved it -- flipping it to True changes no observable state,
            # because ``mark_unavailable`` runs next and nulls the timestamp
            # ``finish_run`` had just computed. It is kept for two smaller reasons:
            # it skips a parse and an UPDATE that would immediately be undone, and
            # that UPDATE would bump ``rev``, publishing a sync revision describing a
            # schedule the row never actually had. Keeping it also means correctness
            # does not depend on these two calls staying in this order.
            repo.finish_run(
                run_id,
                automation["id"],
                status=outcome.status,
                detail=detail,
                reschedule=False,
                denied_capability=outcome.denied_capability,
            )
            repo.mark_unavailable(automation["id"], outcome.summary)
            result.failed += 1
            log.warning(
                "automation %r disabled: %s", automation["name"], outcome.summary
            )
            bus.publish(
                "automation.unavailable",
                {
                    "automation_id": automation["id"],
                    "name": automation["name"],
                    "reason": outcome.summary,
                },
            )
            continue

        repo.finish_run(
            run_id,
            automation["id"],
            status=outcome.status,
            detail=detail,
            denied_capability=outcome.denied_capability,
        )
        if outcome.status == "completed":
            result.fired += 1
        elif outcome.status == "skipped":
            result.skipped += 1
        else:
            result.failed += 1
            log.warning(
                "automation %r failed: %s", automation["name"], outcome.summary
            )

    return result


# ------------------------------------------------------------------ the loop


@dataclass
class _State:
    task: asyncio.Task[None] | None = None
    ticks: int = 0
    last: TickResult | None = None
    consecutive_errors: int = 0
    stopped_reason: str = ""


_state = _State()


def _enabled() -> bool:
    return bool(app_settings.get("automations_enabled", True))


def _tick_seconds() -> float:
    try:
        value = float(app_settings.get("automations_tick_seconds", TICK_SECONDS))
    except (TypeError, ValueError):
        value = TICK_SECONDS
    return max(value, MIN_TICK_SECONDS)


def _max_lateness() -> float:
    try:
        value = float(
            app_settings.get("automations_max_lateness_seconds", MAX_LATENESS_SECONDS)
        )
    except (TypeError, ValueError):
        value = float(MAX_LATENESS_SECONDS)
    return max(value, 0.0)


async def _loop() -> None:
    """Tick until cancelled or exhausted.

    Sleeps first, like the memory sweep and for the same reason: startup is the
    busiest moment in the process, and an automation due right now is at most one
    tick from being noticed.
    """
    while True:
        await asyncio.sleep(_tick_seconds())

        if not _enabled():
            continue  # the switch can come back on without a restart

        try:
            # The repo is synchronous SQLite. Off-thread so a slow disk cannot stall
            # the loop that is also streaming tokens to the UI.
            result = await asyncio.to_thread(run_due)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- the loop outlives one bad tick
            log.exception("automation tick raised")
            _state.consecutive_errors += 1
        else:
            _state.ticks += 1
            _state.last = result
            _state.consecutive_errors = 0

        if _state.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            _state.stopped_reason = (
                f"stopped after {_state.consecutive_errors} consecutive failed ticks"
            )
            log.warning("automation engine %s", _state.stopped_reason)
            return


def start() -> bool:
    """Start the engine. Idempotent, so a settings change cannot leave two loops
    firing the same automations twice."""
    if _state.task is not None and not _state.task.done():
        return False
    _state.stopped_reason = ""
    _state.consecutive_errors = 0
    _state.task = asyncio.create_task(_loop(), name="automations:engine")
    return True


async def stop() -> None:
    task = _state.task
    _state.task = None
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def running() -> bool:
    return _state.task is not None and not _state.task.done()


def status() -> dict[str, Any]:
    """What the engine is doing, including having given up.

    ``due_now`` is read live rather than from the last tick: a stalled background
    task whose status reports a cached zero is indistinguishable from a healthy one
    with nothing to do.
    """
    try:
        due_now = len(repo.due())
    except Exception:  # pragma: no cover - pre-migration database
        due_now = 0
    return {
        "running": running(),
        "enabled": _enabled(),
        "tick_seconds": _tick_seconds(),
        "max_lateness_seconds": _max_lateness(),
        "action_kinds": list(action_kinds()),
        "ticks": _state.ticks,
        "due_now": due_now,
        "stopped_reason": _state.stopped_reason,
        "last": _state.last.to_dict() if _state.last else None,
    }


__all__ = [
    "ActionKind",
    "MAX_LATENESS_SECONDS",
    "Outcome",
    "TICK_SECONDS",
    "TickResult",
    "UnknownActionKind",
    "action_kinds",
    "execute",
    "normalise_scope",
    "required_capabilities",
    "run_due",
    "running",
    "scope_gap",
    "scope_surplus",
    "start",
    "status",
    "stop",
    "unknown_capabilities",
]
