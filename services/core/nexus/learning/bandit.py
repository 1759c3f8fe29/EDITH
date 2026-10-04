"""Routing that learns which model actually serves a job (spec §15, §30, §38).

The router declares a chain per job: an ordered list of models, best first, by the
judgement of whoever wrote it. That judgement is real knowledge and it is also
fixed -- it cannot notice that the model at the head of the chain has been timing
out all week, or that the second one answers this job twice as fast.

This module is the part that notices. It is a **contextual bandit** over the
candidates the router has *already* decided are usable: Thompson sampling from a
Beta posterior per model, fitted to the rewards recorded in
:mod:`nexus.db.repos.model_outcomes`.

Three constraints make this safe to put in front of a user, and they are the whole
design:

**It can only reorder, never admit.** ``order()`` receives the candidates that
survived ``Router._why_not`` -- availability, durable failures, tool and vision
support, context size. A model that is unreachable, unentitled or wrong for the
job is not in the list and cannot be learned into it. The learner's power is
limited to preferring one legitimate option over another legitimate option.

**With no data it reproduces the declared order.** A fresh install has no
outcomes, and a uniform prior would make the learner shuffle a carefully-ordered
chain at random on day one -- strictly worse than the engineering judgement it
replaced. So chain position is itself a prior: earlier candidates start with more
pseudo-success, decaying by position. The first call therefore goes where the
chain says, and the order only moves once measurements disagree with it.

**It explains itself.** Silent substitution is forbidden (``router/jobs.py``), and
"the model you expected was quietly swapped for another" is exactly that. Every
reorder returns a sentence naming what was measured, which the ``Decision``'s
reason carries, so a user who asks why NEXUS used the second model in the list
gets the counts rather than a shrug.

Exploration is the reason this is sampling rather than a ranking. Always picking
the current best-known model means never finding out that a model which failed
twice in its first week has since been fixed -- the bench shrinks to one, and
nothing on screen explains why. Drawing from the posterior keeps every eligible
candidate reachable, with a probability that falls as the evidence against it
accumulates.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from ..logging_setup import get_logger

log = get_logger("learning.bandit")

#: Beta prior for a model with no history. Beta(1, 1) is uniform -- maximum
#: ignorance -- which is the honest starting point before position is applied.
PRIOR_ALPHA = 1.0
PRIOR_BETA = 1.0

#: Pseudo-successes granted to the head of the declared chain, decaying by
#: ``POSITION_DECAY`` per place. Tuned so the declared order survives the first
#: handful of calls and then yields: at 3.0, a second-place model needs roughly
#: three more successes than the head to overtake it, which is evidence rather
#: than noise.
POSITION_PRIOR = 3.0
POSITION_DECAY = 0.5

#: Below this many calls a model's own record is too thin to outweigh its chain
#: position, so the position prior still dominates. This is not a cutoff -- the
#: data is used from the first call -- it is the scale on which it starts to count.
CONFIDENCE_CALLS = 3


@dataclass(frozen=True)
class Arm:
    """One candidate and the evidence about it."""

    key: str
    position: int
    calls: int
    successes: int
    failures: int
    mean_reward: float | None
    mean_latency_ms: int | None

    @property
    def reward_total(self) -> float:
        """Total reward, reconstructed from the mean. 0.0 where nothing is known."""
        if self.mean_reward is None:
            return 0.0
        return self.mean_reward * self.calls

    def posterior(self) -> tuple[float, float]:
        """The Beta parameters this arm is sampled from.

        Reward is in 0..1, so it acts as fractional success: an arm with four
        calls averaging 0.8 has 3.2 pseudo-successes and 0.8 pseudo-failures. This
        is the standard extension of a Bernoulli bandit to bounded rewards, and it
        means a *slow* success (lower reward) counts as partial evidence against
        the model without being recorded as a failure it did not have.
        """
        bonus = POSITION_PRIOR * (POSITION_DECAY ** self.position)
        alpha = PRIOR_ALPHA + bonus + self.reward_total
        beta = PRIOR_BETA + max(0.0, self.calls - self.reward_total)
        return alpha, beta


def arms(candidates: Sequence[Any], stats: dict[str, dict[str, Any]]) -> list[Arm]:
    """Pair each candidate with what has been measured about it."""
    built: list[Arm] = []
    for position, candidate in enumerate(candidates):
        key = getattr(candidate, "key", None) or str(candidate)
        record = stats.get(key) or {}
        built.append(
            Arm(
                key=key,
                position=position,
                calls=int(record.get("calls") or 0),
                successes=int(record.get("successes") or 0),
                failures=int(record.get("failures") or 0),
                mean_reward=record.get("mean_reward"),
                mean_latency_ms=record.get("mean_latency_ms"),
            )
        )
    return built


def order(
    candidates: Sequence[Any],
    stats: dict[str, dict[str, Any]],
    *,
    rng: random.Random | None = None,
) -> tuple[list[Any], str]:
    """Reorder eligible candidates by sampled posterior. Returns (ordered, why).

    ``why`` is empty when the order is unchanged, so a caller can tell "the
    learner agreed with the chain" from "the learner moved something" and only
    explain the second.
    """
    if len(candidates) < 2:
        return list(candidates), ""

    draw = rng or random.Random()
    scored = arms(candidates, stats)

    # Nothing measured for any candidate: the chain's own order is the only
    # information available, and it is better than a coin toss.
    if not any(arm.calls for arm in scored):
        return list(candidates), ""

    samples: dict[str, float] = {}
    for arm in scored:
        alpha, beta = arm.posterior()
        samples[arm.key] = draw.betavariate(alpha, beta)

    paired = list(zip(scored, candidates))
    # Position breaks ties, so an equal draw never reshuffles arbitrarily.
    paired.sort(key=lambda pair: (-samples[pair[0].key], pair[0].position))
    ordered = [candidate for _, candidate in paired]

    if ordered[0] is candidates[0]:
        return ordered, ""

    winner = paired[0][0]
    displaced = scored[0]
    return ordered, _explain(winner, displaced)


def _explain(winner: Arm, displaced: Arm) -> str:
    """One sentence naming the measurements that moved a model to the front."""
    def describe(arm: Arm) -> str:
        if not arm.calls:
            return f"{arm.key} (not used yet)"
        rate = (arm.successes / arm.calls) * 100.0
        speed = (
            f", {arm.mean_latency_ms} ms average" if arm.mean_latency_ms is not None else ""
        )
        return f"{arm.key} ({arm.successes}/{arm.calls} succeeded, {rate:.0f}%{speed})"

    return (
        f"Chosen from measured outcomes rather than chain order: "
        f"{describe(winner)} ahead of {describe(displaced)}."
    )


def explain(candidates: Sequence[Any], stats: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """What the learner currently believes, for the Models screen to show.

    Reports the posterior *mean* rather than a sample, because a user reading a
    table wants the belief, not one draw from it.
    """
    out: list[dict[str, Any]] = []
    for arm in arms(candidates, stats):
        alpha, beta = arm.posterior()
        out.append(
            {
                "key": arm.key,
                "chain_position": arm.position,
                "calls": arm.calls,
                "successes": arm.successes,
                "failures": arm.failures,
                "success_rate": (arm.successes / arm.calls) if arm.calls else None,
                "mean_reward": arm.mean_reward,
                "mean_latency_ms": arm.mean_latency_ms,
                "expected": alpha / (alpha + beta),
                # Stated so the table never implies a belief is evidence-backed
                # when it is mostly the prior talking.
                "confident": arm.calls >= CONFIDENCE_CALLS,
            }
        )
    return out
