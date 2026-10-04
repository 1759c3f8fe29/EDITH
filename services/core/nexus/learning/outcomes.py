"""Recording what happened, safely (spec §38, §78).

Everything here is wrapped so that learning cannot break the thing it learns
from. A missing table, a locked database or a migration that has not run yet must
cost NEXUS a data point, never a user's answer -- an assistant that fails a chat
turn because its statistics table was absent is strictly worse than one that never
learned anything.

That is why every function in this module swallows exceptions and logs at debug.
It is the one place in the codebase where a bare ``except`` is the correct
behaviour rather than a smell, and the reason is recorded here so the next reader
does not "fix" it.

The switch is ``router_learning_enabled``. When it is off, nothing is recorded and
nothing is reordered: the router behaves exactly as its declared chains say. It is
a single setting rather than two because recording without using is a cost with no
benefit, and using without recording is impossible.
"""

from __future__ import annotations

import random
from typing import Any, Sequence

from ..logging_setup import get_logger
from . import bandit

log = get_logger("learning.outcomes")


def enabled() -> bool:
    """Whether routing is allowed to learn. Defaults on, and is explainable.

    Defaulting on is a deliberate departure from the monitors, which default off.
    The reason they default off is that they observe *the user*; this observes
    NEXUS's own calls to its own providers, so there is no privacy question to
    answer -- and a learner that is off by default is a feature nobody switches on.
    What makes it safe is that every reorder is explained in the decision the user
    can read, so it is never a silent substitution.
    """
    try:
        from ..db.repos import app_settings

        return bool(app_settings.get("router_learning_enabled", True))
    except Exception:  # pragma: no cover - pre-migration or DB failure
        log.debug("router_learning_enabled unreadable, assuming off", exc_info=True)
        return False


def record(
    *,
    job: str,
    key: str,
    provider: str,
    model: str,
    ok: bool,
    latency_ms: int | None = None,
    first_token_ms: int | None = None,
    verified: bool | None = None,
    error_kind: str | None = None,
) -> None:
    """Append one outcome. Silent on every failure, by design (see module docstring)."""
    if not enabled():
        return
    try:
        from ..db.repos import model_outcomes

        model_outcomes.record(
            job=job,
            key=key,
            provider=provider,
            model=model,
            ok=ok,
            latency_ms=latency_ms,
            first_token_ms=first_token_ms,
            verified=verified,
            error_kind=error_kind,
        )
    except Exception:  # pragma: no cover - pre-migration or DB failure
        log.debug("could not record outcome for %s on %s", key, job, exc_info=True)


def stats(job: str) -> dict[str, dict[str, Any]]:
    """Aggregates for a job, or an empty dict when nothing can be read."""
    if not enabled():
        return {}
    try:
        from ..db.repos import model_outcomes

        return model_outcomes.stats(job)
    except Exception:  # pragma: no cover - pre-migration or DB failure
        log.debug("could not read outcome stats for %s", job, exc_info=True)
        return {}


def prefer(
    job: str,
    candidates: Sequence[Any],
    *,
    rng: random.Random | None = None,
) -> tuple[list[Any], str]:
    """Reorder eligible candidates by what has been measured. Never raises.

    Returns the candidates unchanged with an empty explanation whenever learning
    is off, the data is unreadable, or the learner agrees with the chain -- so the
    caller's handling of "no change" is the same in all of those cases.
    """
    if not enabled() or len(candidates) < 2:
        return list(candidates), ""
    try:
        measured = stats(job)
        if not measured:
            return list(candidates), ""
        return bandit.order(candidates, measured, rng=rng)
    except Exception:  # pragma: no cover - defensive
        log.debug("could not order candidates for %s", job, exc_info=True)
        return list(candidates), ""


def beliefs(job: str, candidates: Sequence[Any]) -> list[dict[str, Any]]:
    """What the learner believes about each candidate, for the UI."""
    try:
        return bandit.explain(candidates, stats(job))
    except Exception:  # pragma: no cover - defensive
        log.debug("could not explain beliefs for %s", job, exc_info=True)
        return []
