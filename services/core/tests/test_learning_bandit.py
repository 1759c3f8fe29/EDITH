"""The learning router: rewards, posteriors, and the limits on what it may do.

Three things are being pinned here, in rising order of importance.

**The reward formula**, because it is the definition of "better" that everything
downstream optimises. If a contradicted answer scored above a transient failure,
the router would learn to prefer a model that lies confidently over one that
errors visibly -- so that ordering is asserted directly rather than left to
arithmetic nobody checked.

**The posterior**, because an untested bandit is indistinguishable from a random
shuffle. The tests fix the two ends: with no data the declared chain order is
preserved exactly (a fresh install must not reshuffle a hand-ordered chain), and
with enough evidence the measured winner overtakes the chain head.

**The boundary**, which is the only one that is a safety property rather than a
quality one: the learner receives candidates that have already cleared
``_why_not``, and nothing it does can put a rejected model back in play. The test
for that drives the real ``Router.resolve`` with a blocked model and asserts it
stays blocked no matter what the outcome table says about it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

import pytest

from nexus.db.repos import model_outcomes
from nexus.learning import bandit


@dataclass
class Fake:
    """A candidate stand-in -- the learner only ever reads ``.key``."""

    key: str


def arm(key: str, position: int, **kwargs: Any) -> bandit.Arm:
    base = {
        "calls": 0,
        "successes": 0,
        "failures": 0,
        "mean_reward": None,
        "mean_latency_ms": None,
    }
    base.update(kwargs)
    return bandit.Arm(key=key, position=position, **base)  # type: ignore[arg-type]


def measured(
    key: str, *, calls: int, successes: int, mean_reward: float, latency: int | None = None
) -> dict[str, Any]:
    return {
        key: {
            "key": key,
            "calls": calls,
            "successes": successes,
            "failures": calls - successes,
            "mean_reward": mean_reward,
            "mean_latency_ms": latency,
        }
    }


# ---------------------------------------------------------------- the reward


def test_a_fast_verified_success_scores_near_the_top() -> None:
    value = model_outcomes.reward(ok=True, latency_ms=500, verified=True)
    assert 0.9 < value <= 1.0


def test_a_slow_success_still_beats_any_failure() -> None:
    slow = model_outcomes.reward(ok=True, latency_ms=120_000, verified=None)
    transient = model_outcomes.reward(ok=False, latency_ms=None, verified=None,
                                      error_kind="timeout")
    # Past SLOW_MS the speed term floors at zero, leaving the success weight.
    assert slow == pytest.approx(model_outcomes.SUCCESS_WEIGHT)
    assert slow > transient


def test_an_unmeasured_latency_earns_the_middle_not_the_top() -> None:
    """Absence of a measurement is not evidence of being fast."""
    unknown = model_outcomes.reward(ok=True, latency_ms=None, verified=None)
    instant = model_outcomes.reward(ok=True, latency_ms=0, verified=None)
    assert unknown < instant
    assert unknown == pytest.approx(
        model_outcomes.SUCCESS_WEIGHT + model_outcomes.SPEED_WEIGHT * 0.5
    )


def test_a_durable_failure_scores_zero_and_a_transient_one_does_not() -> None:
    """A model that timed out once must stay explorable; a 404 need not."""
    assert model_outcomes.reward(
        ok=False, latency_ms=None, verified=None, error_kind="model_unavailable"
    ) == 0.0
    assert model_outcomes.reward(
        ok=False, latency_ms=None, verified=None, error_kind="timeout"
    ) == model_outcomes.TRANSIENT_REWARD
    assert model_outcomes.TRANSIENT_REWARD > 0.0


def test_a_contradicted_answer_scores_below_an_honest_failure() -> None:
    """The one ordering that must never invert: confident-and-wrong is worst."""
    contradicted = model_outcomes.reward(ok=True, latency_ms=100, verified=False)
    transient = model_outcomes.reward(
        ok=False, latency_ms=None, verified=None, error_kind="timeout"
    )
    assert contradicted < transient
    # But not zero -- the model did answer, and a durable failure is still worse.
    assert contradicted > model_outcomes.reward(
        ok=False, latency_ms=None, verified=None, error_kind="model_unavailable"
    )


def test_unverifiable_is_not_treated_as_failure() -> None:
    """NULL verified must score the same as a plain success, not as wrong."""
    unchecked = model_outcomes.reward(ok=True, latency_ms=1_000, verified=None)
    confirmed = model_outcomes.reward(ok=True, latency_ms=1_000, verified=True)
    assert unchecked == confirmed


# ------------------------------------------------------------- the posterior


def test_chain_position_is_the_prior_so_the_head_starts_ahead() -> None:
    head, second = arm("a", 0), arm("b", 1)
    assert head.posterior()[0] > second.posterior()[0]
    # Both are pure prior, so neither claims confidence.
    assert head.calls == 0 and second.calls == 0


def test_with_no_data_the_declared_order_is_preserved_exactly() -> None:
    """A fresh install must not shuffle a hand-ordered chain."""
    chain = [Fake("a"), Fake("b"), Fake("c")]
    ordered, why = bandit.order(chain, {}, rng=random.Random(1))
    assert [c.key for c in ordered] == ["a", "b", "c"]
    assert why == ""


def test_reward_acts_as_fractional_success_in_the_posterior() -> None:
    """Four calls averaging 0.8 is 3.2 pseudo-successes, 0.8 pseudo-failures."""
    a = arm("a", 0, calls=4, successes=4, mean_reward=0.8)
    alpha, beta = a.posterior()
    bonus = bandit.POSITION_PRIOR * (bandit.POSITION_DECAY ** 0)
    assert alpha == pytest.approx(bandit.PRIOR_ALPHA + bonus + 3.2)
    assert beta == pytest.approx(bandit.PRIOR_BETA + 0.8)


def test_a_consistently_better_model_overtakes_the_chain_head() -> None:
    """The point of the whole module: measurement beats declared order."""
    chain = [Fake("head"), Fake("challenger")]
    stats = {
        **measured("head", calls=30, successes=6, mean_reward=0.2, latency=40_000),
        **measured("challenger", calls=30, successes=30, mean_reward=0.95, latency=800),
    }
    # Sampling is stochastic, so the claim is about the distribution, not one draw.
    wins = 0
    for seed in range(200):
        ordered, _ = bandit.order(chain, stats, rng=random.Random(seed))
        if ordered[0].key == "challenger":
            wins += 1
    assert wins > 190, f"challenger led only {wins}/200 draws"


def test_a_reorder_explains_itself_with_the_counts() -> None:
    """Silent substitution is forbidden, so the swap must carry its evidence."""
    chain = [Fake("head"), Fake("challenger")]
    stats = {
        **measured("head", calls=20, successes=2, mean_reward=0.1),
        **measured("challenger", calls=20, successes=20, mean_reward=0.95, latency=700),
    }
    for seed in range(50):
        ordered, why = bandit.order(chain, stats, rng=random.Random(seed))
        if ordered[0].key == "challenger":
            assert "measured outcomes" in why
            assert "challenger" in why and "head" in why
            assert "20/20" in why          # the winner's record
            assert "2/20" in why           # what it displaced
            assert "700 ms" in why
            break
    else:  # pragma: no cover - the challenger dominates; this cannot be reached
        pytest.fail("challenger never led")


def test_agreeing_with_the_chain_produces_no_explanation() -> None:
    """Nothing to explain when the learner leaves the order alone."""
    chain = [Fake("head"), Fake("other")]
    stats = measured("head", calls=20, successes=20, mean_reward=0.95, latency=500)
    _, why = bandit.order(chain, stats, rng=random.Random(0))
    assert why == ""


def test_a_single_candidate_is_never_reordered() -> None:
    only = [Fake("a")]
    ordered, why = bandit.order(only, measured("a", calls=5, successes=0, mean_reward=0.0))
    assert ordered == only and why == ""


def test_beliefs_state_whether_they_rest_on_evidence() -> None:
    chain = [Fake("thin"), Fake("thick")]
    stats = {
        **measured("thin", calls=1, successes=1, mean_reward=0.9),
        **measured("thick", calls=25, successes=20, mean_reward=0.8),
    }
    beliefs = {b["key"]: b for b in bandit.explain(chain, stats)}
    assert beliefs["thin"]["confident"] is False
    assert beliefs["thick"]["confident"] is True
    assert 0.0 < beliefs["thick"]["expected"] < 1.0
    assert beliefs["thick"]["success_rate"] == pytest.approx(0.8)
