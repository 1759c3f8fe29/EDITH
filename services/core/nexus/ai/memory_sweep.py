"""The periodic memory sweep (task F11; spec §16, §17, §71).

:mod:`nexus.ai.memory_extract` reads one exchange, inline, right after a reply is
delivered. That covers the common case and misses in ways the user never sees:

* the process was closed, or crashed, before the background task ran;
* no fast model was reachable at that moment;
* extraction was switched off and later switched back on;
* the turn arrived through a path that does not run it -- a voice command, an
  automation, an agent run.

Each of those loses a durable fact silently. This module is the pass that goes back
over what was said and picks them up, on a timer, using the same small fast model.

Three properties make it safe to leave running:

**It never looks at the same exchange twice.** A watermark in ``app_settings``
records the last exchange examined, ordered by ``(created_at, id)``. Turns that
yielded nothing are behind the watermark forever, which is right: a sentence with no
durable fact in it today will have none tomorrow.

**It only advances the watermark past exchanges a model actually read.** This is
why the sweep calls :func:`~nexus.ai.memory_extract.extract_strict` rather than the
swallowing ``extract``. If the network is down, ``extract`` returns "nothing found"
for every exchange, and a watermark advancing over that would step past a whole
afternoon of conversation and never come back. A failure aborts the run with the
watermark where it was.

**It proposes; it does not remember.** Every write goes through
``memories.propose``, so nothing is retrievable until the user approves it on the
Memory screen (§487). The sweep changes when NEXUS notices something, never who
decides.

It is deliberately *not* a :class:`~nexus.monitors.base.Monitor`. Monitors are
gated behind the ``monitor_enabled`` master switch, which is the switch for NEXUS
watching the screen and the clipboard. Reading back the user's own conversation with
NEXUS is a different act with different consent, and requiring screen awareness
before NEXUS will remember what the user told it would be the wrong coupling.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db.repos import app_settings, conversations, memories
from ..logging_setup import get_logger
from ..providers.base import ProviderError
from ..router import NoModelAvailable
from . import memory_extract

log = get_logger("ai.memory_sweep")

#: Where the watermark lives. An opaque ``"<created_at>|<id>"`` string produced by
#: ``conversations.exchange_cursor``; empty means nothing has been swept yet.
CURSOR_KEY = "memory_sweep_cursor"

#: Exchanges examined per run. The cap exists because a first sweep over months of
#: history would otherwise fire hundreds of model calls in a burst. The watermark
#: walks forward each run, so a backlog drains rather than being dropped -- and
#: ``status()`` reports what is still behind it, because a cap nobody can see reads
#: as "everything has been looked at".
MAX_PER_RUN = 8

#: How long an exchange is left alone before the sweep touches it. The inline
#: extractor runs as a background task the moment a reply completes; without a grace
#: window both passes can read the same exchange at once, each miss the other's
#: proposal, and write it twice. Also keeps the sweep off the tail of a conversation
#: the user is still in the middle of.
GRACE_SECONDS = 180.0

#: Minimum interval between runs, whatever the setting says. A sweep every few
#: seconds would be a model call per turn with extra steps.
MIN_INTERVAL = 60.0


@dataclass
class SweepResult:
    """What one run did. Returned by :func:`sweep_once` and kept for ``status()``."""

    examined: int = 0
    proposed: int = 0
    skipped: int = 0
    #: Set when the run stopped early. The watermark did not advance past whatever
    #: failed, so the same exchanges are retried next time.
    error: str = ""
    #: Exchanges still behind the watermark when the run ended.
    backlog: int = 0
    at: str = ""
    cursor: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "examined": self.examined,
            "proposed": self.proposed,
            "skipped": self.skipped,
            "error": self.error,
            "backlog": self.backlog,
            "at": self.at,
            "cursor": self.cursor,
        }


@dataclass
class _State:
    task: asyncio.Task[None] | None = None
    runs: int = 0
    last: SweepResult | None = None
    consecutive_errors: int = 0
    stopped_reason: str = ""
    history: list[SweepResult] = field(default_factory=list)


_state = _State()

#: After this many consecutive failed runs the loop stops instead of retrying
#: forever, and ``status()`` says why. Matches the monitors' behaviour: a background
#: task that has given up must say so rather than look alive.
MAX_CONSECUTIVE_ERRORS = 5


def _enabled() -> bool:
    """Both switches must be on.

    ``memory_extract_enabled`` is the user's answer to "may NEXUS suggest
    memories at all"; the sweep is one mechanism behind that answer, not a second
    decision, so it can never run while the general switch is off.
    """
    return bool(app_settings.get("memory_extract_enabled", True)) and bool(
        app_settings.get("memory_sweep_enabled", True)
    )


def _interval() -> float:
    try:
        value = float(app_settings.get("memory_sweep_interval_seconds", 900))
    except (TypeError, ValueError):
        value = 900.0
    return max(value, MIN_INTERVAL)


def _cutoff() -> str:
    """The newest timestamp the sweep will look at, as stored ISO-8601 UTC."""
    return (datetime.now(timezone.utc) - timedelta(seconds=GRACE_SECONDS)).isoformat()


async def sweep_once() -> SweepResult:
    """Examine one batch of exchanges. Safe to call directly, e.g. from a route.

    Advances the watermark one exchange at a time rather than once at the end, so a
    run killed halfway keeps the work it finished. Re-doing an exchange would only
    cost a model call -- ``record`` dedupes the proposal -- but the cost is
    avoidable.
    """
    stamp = datetime.now(timezone.utc).isoformat()
    if not _enabled():
        return SweepResult(at=stamp, error="memory extraction is switched off")

    cutoff = _cutoff()
    cursor = str(app_settings.get(CURSOR_KEY, "") or "")
    batch = conversations.exchanges_since(cursor, before=cutoff, limit=MAX_PER_RUN)
    if not batch:
        return SweepResult(at=stamp, cursor=cursor, backlog=0)

    # One query for the whole batch: which of these the inline extractor already
    # read. Exchanges it handled are marked done without a second model call.
    already = memories.extracted_message_ids([str(x["message_id"]) for x in batch])

    result = SweepResult(at=stamp, cursor=cursor)
    for exchange in batch:
        message_id = str(exchange["message_id"])
        if message_id in already or not str(exchange.get("request") or "").strip():
            result.skipped += 1
            cursor = conversations.exchange_cursor(exchange)
            app_settings.set(CURSOR_KEY, cursor)
            result.cursor = cursor
            continue

        try:
            candidates = await memory_extract.extract_strict(
                str(exchange.get("request") or ""),
                str(exchange.get("reply") or ""),
            )
        except NoModelAvailable as exc:
            # No fast model configured or reachable. Stopping with the watermark
            # unmoved is the whole point: these exchanges have not been read.
            result.error = f"no fast model available: {exc}"
            break
        except ProviderError as exc:
            result.error = f"{exc.provider or 'the model'} failed: {exc.message}"
            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- one bad exchange must not end the sweep
            log.warning("sweep failed on message %s", message_id, exc_info=True)
            result.error = f"unexpected failure: {exc}"
            break

        stored = memory_extract.record(
            candidates,
            conversation_id=str(exchange.get("conversation_id") or "") or None,
            message_id=message_id,
            project_id=str(exchange.get("project_id") or "") or None,
        )
        result.examined += 1
        result.proposed += len(stored)

        cursor = conversations.exchange_cursor(exchange)
        app_settings.set(CURSOR_KEY, cursor)
        result.cursor = cursor

    result.backlog = conversations.unswept_count(cursor, before=cutoff)
    if result.examined or result.proposed or result.error:
        log.info(
            "memory sweep: examined %d, proposed %d, skipped %d, backlog %d%s",
            result.examined,
            result.proposed,
            result.skipped,
            result.backlog,
            f", stopped: {result.error}" if result.error else "",
        )
    return result


async def _loop() -> None:
    """Run the sweep on the configured interval until cancelled or exhausted.

    Sleeps first. Startup is already the busiest moment in the process -- migrations,
    provider probes, the UI connecting -- and the sweep is looking at conversations
    that have been sitting there for hours; one interval of patience costs nothing.
    """
    while True:
        try:
            await asyncio.sleep(_interval())
        except asyncio.CancelledError:
            raise

        if not _enabled():
            continue  # the switch can come back on without a restart

        try:
            result = await sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- the loop outlives one bad run
            log.exception("memory sweep run raised")
            _state.consecutive_errors += 1
        else:
            _state.runs += 1
            _state.last = result
            _state.history.append(result)
            del _state.history[:-10]
            if result.error:
                _state.consecutive_errors += 1
            else:
                _state.consecutive_errors = 0

        if _state.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            _state.stopped_reason = (
                f"stopped after {_state.consecutive_errors} consecutive failures"
                + (f": {_state.last.error}" if _state.last and _state.last.error else "")
            )
            log.warning("memory sweep %s", _state.stopped_reason)
            return


def start() -> bool:
    """Start the background loop. Returns whether a new task was created.

    Idempotent, so calling it again after a settings change does not leave two
    loops sweeping the same watermark.
    """
    if _state.task is not None and not _state.task.done():
        return False
    _state.stopped_reason = ""
    _state.consecutive_errors = 0
    _state.task = asyncio.create_task(_loop(), name="memory:sweep")
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
    """What the sweep is doing, truthfully -- including having given up.

    ``backlog`` is read live rather than from the last run: the answer to "is
    anything waiting to be read" changes every time the user says something, and a
    stale number here is what makes a stalled background task look healthy.
    """
    cursor = str(app_settings.get(CURSOR_KEY, "") or "")
    try:
        backlog = conversations.unswept_count(cursor, before=_cutoff())
    except Exception:  # pragma: no cover - pre-migration database
        backlog = 0
    return {
        "running": running(),
        "enabled": _enabled(),
        "interval_seconds": _interval(),
        "batch_size": MAX_PER_RUN,
        "grace_seconds": GRACE_SECONDS,
        "runs": _state.runs,
        "cursor": cursor,
        "backlog": backlog,
        "stopped_reason": _state.stopped_reason,
        "last": _state.last.to_dict() if _state.last else None,
        "recent": [r.to_dict() for r in _state.history],
    }


__all__ = [
    "CURSOR_KEY",
    "GRACE_SECONDS",
    "MAX_PER_RUN",
    "SweepResult",
    "running",
    "start",
    "status",
    "stop",
    "sweep_once",
]
