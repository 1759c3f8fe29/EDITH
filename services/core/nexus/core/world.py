"""The global world model (doc-multimodal §6, §32; MASTER §15, §78).

Everything a monitor notices has been an event: published on the bus, scored
by the proactive engine, written to the ambient store, and then gone. The one
thing that never existed was the thing the docs kept naming -- *the current
state of the machine*, held in one place, ready to answer "what is going on
right now" without anyone re-reading ``/proc`` through a tool.

This module is that substrate. It subscribes to ``observation.*`` exactly as
the proactive engine does and keeps the latest facts, per domain, each with the
timestamp of the observation that produced it. Three rules, each of which the
easy version breaks:

**A fact is only ever a fact someone observed.** A domain no monitor has
spoken about reads as ``None``, never as a plausible default. ``battery: None``
is an honest "no monitor has reported a battery on this machine"; ``battery:
{"percent": 100}`` would be a guess wearing a fact's clothes (§60, §78). The
monitor that owns a fact does not exist yet for ``process.*`` -- the field is
absent because nothing has been observed, not because the agent declined to
look.

**Old is marked, not dropped.** A fact older than :data:`STALE_AFTER_SECONDS`
is still returned with ``stale: True``, because the honest thing to tell a
model is "battery was 42% twenty minutes ago" -- not silence, from which the
model invents a number. Dropping stale facts is how an aggregator teaches the
agent downstream to hallucinate.

**A crashed world model must not pretend to be current.** The subscriber wraps
every observation in ``try/except`` (the proactive engine's pattern). An
exception sets ``degraded: True`` and the error string on the state, so a model
reading world state knows it may be missing observations rather than trusting
a snapshot that silently stopped updating.

Process facts are handled defensively: ``process.*`` observations arrive only
when the process monitor (C1, another task) publishes them. Subscribing is
harmless -- an event that never comes simply never updates the field -- and
inventing a churn rate for a monitor that does not exist is exactly what the
honesty rules forbid.

Boundedness: every kind keeps a :class:`collections.deque` of at most
:data:`KEEP_PER_KIND` facts (the newest wins), and the number of kinds is
itself capped, because a subscriber that follows a malicious or buggy
producer of unlimited kinds would otherwise grow a dict forever.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque

from ..logging_setup import get_logger
from .events import bus

log = get_logger("core.world")

#: How many facts of one kind are kept. A monitor publishing every few seconds
#: produces thousands per hour; keeping a handful is enough to answer "recently"
#: while capping memory at a few hundred records total.
KEEP_PER_KIND = 8

#: Facts older than this are returned with ``stale: True`` rather than dropped.
#: Battery/network facts from a monitor on a 60 s interval should read fresh;
#: a monitor that has been off for an hour should read as old, not as absent.
STALE_AFTER_SECONDS = 300.0

#: Upper bound on distinct kinds tracked, so a runaway producer of novel event
#: names cannot grow the dict without limit. The known monitors use about a
#: dozen; this leaves room for new ones and stops the pathological case.
MAX_KINDS = 64

#: The domains ``world_state()`` reports, in the order a reader would want
#: them. Each entry is (domain, the observation-kind prefixes that feed it).
#: Anything observed outside these domains is kept in history and visible in
#: ``kinds``/``observed`` counts but has no named field -- the model sees the
#: count, not a fabricated summary.
DOMAINS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("battery", ("system.battery",)),
    ("network", ("system.network",)),
    ("disk", ("system.disk",)),
    ("cpu", ("system.cpu",)),
    ("memory", ("system.memory",)),
    ("usb", ("usb.",)),
    ("bluetooth", ("bluetooth.",)),
    ("window", ("window.",)),
    ("clipboard", ("clipboard.",)),
    ("screen", ("screen.",)),
    # Defensive: the process monitor is another task's deliverable. If it
    # publishes, its facts flow in; if it does not, ``process`` stays None --
    # unknown, never invented.
    ("process", ("process.",)),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime | None:
    """Best-effort parse of an observation timestamp, or None when unusable.

    ``Observation.at`` is an ISO string written by the monitor; a test or a
    foreign publisher may hand us anything, and a fact whose timestamp cannot
    be read is recorded with the time we received it rather than guessed.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _is_stale(observed: datetime, now: datetime) -> bool:
    """Whether a fact is older than :data:`STALE_AFTER_SECONDS`.

    Kept as its own function because it is the one judgement this module
    makes on data, and the mutation tests pin its exact behaviour.

    Exclusive: a fact of exactly :data:`STALE_AFTER_SECONDS` is still fresh,
    matching "older than" above. The boundary is unreachable through the bus --
    ``now`` has moved by the time a published fact is read -- so the test that
    pins it calls this function directly.
    """
    return (now - observed).total_seconds() > STALE_AFTER_SECONDS


def _fact(kind: str, payload: dict[str, Any], received_at: str) -> dict[str, Any]:
    """One observation into one record the model can read.

    Carries only what the observation actually said: the domain data verbatim,
    the summary, and both timestamps (``observed_at`` is what the monitor
    claims; ``received_at`` is when this process saw it -- they differ when a
    bus replays or a publisher backdates).
    """
    data = payload.get("data")
    return {
        "kind": kind,
        "summary": str(payload.get("summary") or ""),
        "data": dict(data) if isinstance(data, dict) else {},
        "observed_at": str(payload.get("at") or "") or received_at,
        "received_at": received_at,
        "severity": str(payload.get("severity") or "info"),
        "clears": bool(payload.get("clears") or False),
    }


# ----------------------------------------------------------------------- state


@dataclass
class _WorldState:
    """Mutable internal state; every read path goes through world_state()."""

    #: kind -> newest-first facts. A deque of dicts, bounded per kind.
    facts: dict[str, Deque[dict[str, Any]]] = field(default_factory=dict)
    #: The bus subscriber task, or None when not started.
    subscriber: asyncio.Task[None] | None = None
    #: Set the first time the subscriber fails; never silently cleared, because
    #: a state that went broken and then "fixed itself" without a new
    #: observation did not actually fix itself.
    degraded: bool = False
    error: str = ""
    #: Total observations accepted, for status reporting.
    observed: int = 0
    #: ISO timestamp of the most recent fact, or None when nothing was seen.
    last_observed_at: str | None = None
    #: Unknown-kind events are counted here, never turned into named domains.
    unknown_kinds: int = 0


_state = _WorldState()


def _remember_failure(exc: BaseException) -> None:
    """Record that the world model broke, loudly and honestly.

    The state is *not* cleared: a broken subscriber means every "latest fact"
    may be the last one ever seen, so the model must be told the snapshot is
    unreliable rather than trusted as current.
    """
    _state.degraded = True
    _state.error = f"{type(exc).__name__}: {exc}"
    log.exception("world model subscriber failed; state is now degraded")


def _accept(kind: str, payload: dict[str, Any]) -> None:
    """Record one fact. Bounded by construction; never raises on its own.

    The dict of deques is capped two ways: each deque is a bounded ``maxlen``
    so a chatty kind cannot grow it, and a novel kind is only added while
    ``MAX_KINDS`` has room -- after which it is counted as unknown rather
    than silently extending memory.
    """
    received_at = _now_iso()
    fact = _fact(kind, payload, received_at)

    history = _state.facts.get(kind)
    if history is None:
        if len(_state.facts) >= MAX_KINDS:
            _state.unknown_kinds += 1
            return
        history = deque(maxlen=KEEP_PER_KIND)
        _state.facts[kind] = history
    history.appendleft(fact)

    _state.observed += 1
    _state.last_observed_at = received_at


async def _subscriber() -> None:
    """Read ``observation.*`` off the bus forever, remembering the latest.

    A prefix subscription matches every ``observation.<kind>`` the supervisor
    publishes. Only events whose *type* starts with ``observation.`` are
    handled: the subscription's first yield is the ``stream.resumed``
    handshake and heartbeats arrive on idle -- their payloads carry no
    observation, and counting a handshake as an unknown kind would be
    exactly the plausible-looking wrong number this module exists to avoid.
    Each event is handled inside its own ``try/except``: one malformed payload
    is a warning, not the end of the world model, and a handler that raises
    marks the state degraded rather than killing the task (a dead subscriber
    that still looked "running" would be the §78 fake).
    """
    agen = bus.subscribe(topics=("observation",))
    try:
        async for event in agen:
            if not event.type.startswith("observation."):
                continue
            kind = str(event.payload.get("kind") or "")
            try:
                if kind:
                    _accept(kind, event.payload)
                # An event with no kind is counted, not stored: there is no
                # domain to file it under, and inventing one ("misc") is a
                # summary of nothing.
                else:
                    _state.unknown_kinds += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- one bad event, one log line
                # Deliberately not re-raised. Re-raising ends the task, and a
                # dead subscriber leaves `running()` false while `world_state()`
                # keeps answering with facts frozen at the moment of the crash --
                # the §78 fake this module exists to avoid. The state is marked
                # degraded instead, so every later read carries the warning, and
                # the next event still gets a chance to be recorded.
                _remember_failure(exc)
    finally:
        with contextlib.suppress(Exception):
            await agen.aclose()


# -------------------------------------------------------------------- assembly


def _latest_for(kind_history: Deque[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    """The newest fact of one kind, with its staleness marked.

    The deque is newest-first (``appendleft``), so index 0 is the latest
    observation. A fact whose ``observed_at`` cannot be parsed at all is judged
    against ``received_at`` instead -- the honest fallback is the time this
    process actually saw it, not a guess, and not ``None`` either, because
    the observation genuinely happened.
    """
    if not kind_history:
        return None
    newest = kind_history[0]
    observed = _parse_iso(str(newest.get("observed_at") or ""))
    if observed is None:
        observed = _parse_iso(str(newest.get("received_at") or ""))
    return {
        "kind": newest.get("kind"),
        "observed_at": newest.get("observed_at"),
        "received_at": newest.get("received_at"),
        "summary": newest.get("summary"),
        "severity": newest.get("severity"),
        "clears": newest.get("clears"),
        "data": newest.get("data"),
        # Staleness is computed, never stored: a fact is fresh or stale
        # *relative to the moment it is read*, and baking it in at accept
        # time would freeze every fact as fresh forever.
        "stale": bool(observed is not None and _is_stale(observed, now)),
    }


def _domain_kinds(domain: str) -> tuple[str, ...]:
    for name, prefixes in DOMAINS:
        if name == domain:
            return prefixes
    return ()


def world_state() -> dict[str, Any]:
    """The whole world model as JSON-safe data, honestly annotated.

    Domains with no observation read as ``None``. Domains with observations
    carry the latest fact's ``data`` verbatim plus its timestamps and a
    ``stale`` flag. ``degraded`` is True when the subscriber has failed at
    least once; ``kinds`` lists what is actually held so a caller can see
    which monitors have spoken.
    """
    now = datetime.now(timezone.utc)
    domains: dict[str, Any] = {}
    for domain, _prefixes in DOMAINS:
        # Every kind that feeds this domain, newest across all of them.
        candidates = [
            _latest_for(_state.facts[kind], now)
            for kind in _state.facts
            if any(kind == p or kind.startswith(p) for p in _domain_kinds(domain))
        ]
        latest = None
        best_key = ""
        for candidate in candidates:
            if candidate is None:
                continue
            # Newer facts win by observed_at string compare; ISO strings sort
            # chronologically, which is why both fields are ISO everywhere.
            key = str(candidate.get("observed_at") or candidate.get("received_at") or "")
            if key >= best_key:
                latest, best_key = candidate, key
        domains[domain] = latest

    return {
        "domains": domains,
        "degraded": _state.degraded,
        "error": _state.error or None,
        "observed": _state.observed,
        "unknown_kinds": _state.unknown_kinds,
        "kinds": sorted(_state.facts.keys()),
        "last_observed_at": _state.last_observed_at,
    }


# ---------------------------------------------------------------- redacted view


#: Fields in observation data that are user content rather than machine facts.
#: A window title is a document name, a URL, a message subject; a clipboard
#: entry's content is whatever the user copied. These never enter the agent's
#: system prompt unredacted, and clipboard *content* is not passed at all --
#: presence and shape only, per the privacy rule the clipboard monitor set.
_SENSITIVE_FIELDS = ("title", "previous_title", "content", "preview", "name", "product", "vendor")

#: Fields that are dropped rather than redacted: the clipboard monitor already
#: made the decision that copied content should never travel with a summary,
#: and re-deciding it here would open a second path around that rule.
_DROPPED_FIELDS = ("content", "preview")

#: Domains whose observation *summaries* embed user content, and a safe
#: replacement built from the kind alone. The monitors write titles into
#: window.title summaries ("firefox: Bank statement.pdf"), copied text into
#: clipboard.copy ones ("Copied text: ..."), and device names into usb and
#: bluetooth ones -- so redacting ``data`` without replacing the summary
#: would leave the user content one field away, in the exact line the model
#: reads. The replacement names the domain and the fact that details were
#: withheld, which is what the model needs; the details are one tool call
#: away for the user who asks.
#:
#: Matching is on the observation kind's *suffix*, and deliberately narrow,
#: because a domain can mix two shapes: ``window.title`` embeds a document
#: name while ``window.focus`` summaries carry only app names and durations
#: ("Switched to firefox after 2m in vim"); ``clipboard.copy`` embeds the
#: copied text's preview while ``clipboard.credential`` says only what
#: category was copied and that it was not kept; ``bluetooth.<role>.connected``
#: embeds the device's name while ``bluetooth.radio`` ("Bluetooth is back
#: on") is a machine fact. Redirecting the safe kinds too would replace real
#: facts with vaguer ones for no privacy gain.
_KIND_REDIRECTION: tuple[tuple[str, str, str], ...] = (
    ("window", ".title", "the focused window's title changed (details withheld)"),
    ("clipboard", ".copy", "something was copied (content withheld)"),
    # Every usb.* observation is a device appearing or going, and every one
    # of its summaries is "{device name} connected" -- there is no usb kind
    # whose summary is safe to pass through, so the empty suffix matches all.
    ("usb", "", "a USB device was attached or removed"),
    ("bluetooth", ".connected", "a Bluetooth device connected or disconnected"),
    ("bluetooth", ".disconnected", "a Bluetooth device connected or disconnected"),
)


def _safe_summary(domain: str, fact: dict[str, Any]) -> Any:
    """The summary a domain's fact is allowed to show the agent.

    Returns the original summary when the domain's summaries are machine
    facts (percentages, mountpoints, routes), and a redirected one when the
    monitor's summary format embeds user content. ``None`` from a missing
    summary is preserved as None -- ``_world_block`` falls back to the domain
    name there, and turning None into a string would change what it renders.
    """
    if fact.get("summary") is None:
        return None
    kind = str(fact.get("kind") or "")
    for dom, kind_suffix, replacement in _KIND_REDIRECTION:
        if dom == domain and (not kind_suffix or kind.endswith(kind_suffix)):
            return replacement
    return fact.get("summary")


def _redact(value: Any) -> Any:
    """One level of observation data with sensitive fields masked.

    Sensitive fields can nest -- a window.focus observation keeps the old and
    new session under ``from``/``to``, each carrying a ``title`` -- so this
    walks dicts one level deeper rather than scanning only the top. It does
    not recurse arbitrarily: no observation nests further, and a general
    walker that redacted inside strings would be the "safe-looking version
    that eats normal output" the scrubber decision log warns about.
    """
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        if key in _DROPPED_FIELDS:
            continue
        if isinstance(item, dict):
            out[key] = _redact(item)
        elif key in _SENSITIVE_FIELDS and item not in (None, ""):
            out[key] = "[redacted]"
        else:
            out[key] = item
    return out


def redacted_state() -> dict[str, Any]:
    """``world_state()`` shaped for the agent's system prompt.

    The world model the *model* sees is smaller and blinder than the one the
    process holds, on purpose: device names and window titles are user data
    (the window monitor's own docstring says so), and a summary block exists
    to stop tool calls, not to become a surveillance surface in the prompt.
    Redaction covers the summary line as well as the data: the monitors write
    titles and device names into their summaries, so scrubbing only the
    payload would leave the user content sitting in the one line the model
    definitely reads.
    """
    state = world_state()
    for domain, fact in state["domains"].items():
        if not fact:
            continue
        fact["data"] = _redact(fact.get("data"))
        fact["summary"] = _safe_summary(domain, fact)
    return state


# -------------------------------------------------------------- lifecycle API


def start() -> bool:
    """Start the world model. Returns whether a new subscriber was created.

    Idempotent, matching proactive.start(): a lifespan that runs twice must
    not leave two subscribers racing over the same observations.
    """
    if _state.subscriber is not None and not _state.subscriber.done():
        return False
    _state.subscriber = asyncio.create_task(_subscriber(), name="world:subscriber")
    return True


async def stop() -> None:
    """Cancel the subscriber. Facts are kept -- history is still true history."""
    task = _state.subscriber
    _state.subscriber = None
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def running() -> bool:
    return _state.subscriber is not None and not _state.subscriber.done()


def reset() -> None:
    """Drop all state. For tests: a leaked world model from one test must not
    colour the next one's facts."""
    _state.__init__()


def status() -> dict[str, Any]:
    """What the world model is doing, truthfully."""
    return {
        "running": running(),
        "degraded": _state.degraded,
        "error": _state.error or None,
        "observed": _state.observed,
        "kinds": sorted(_state.facts.keys()),
        "unknown_kinds": _state.unknown_kinds,
        "keep_per_kind": KEEP_PER_KIND,
        "stale_after_seconds": STALE_AFTER_SECONDS,
    }
