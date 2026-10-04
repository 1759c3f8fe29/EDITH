"""What each model actually did, so routing can learn instead of guessing.

This is the memory the learner reads (spec §15, §30, §38, §78). Its relationship
to :mod:`nexus.db.repos.model_probes` is worth stating plainly, because the two
look similar and answer different questions:

* **A probe** asks *can this key call this model at all*. One row per model,
  overwritten each time, a capability fact.
* **An outcome** asks *when NEXUS used this model for this job, how did it go*.
  One row per call, kept, because the useful quantity is a rate over time and
  overwriting would destroy the only thing outcomes are for.

**The reward is computed once, at the moment of the call, and stored.** It folds
in facts that were true then -- how long the call took against the latency budget
that applied, whether the work could be verified -- and recomputing it later from
a changed formula would silently rewrite history. A learner trained on numbers
nobody measured is worse than one with no data, because it is confident.

**A failure is not automatically a zero.** The split that `model_probes` makes
between durable and transient failures matters here too: a model that timed out
once under load did not reveal itself to be a bad choice, and scoring it zero
would bench it on the strength of one bad afternoon. Transient failures earn a
low-but-nonzero reward, so the model stays explorable; only failures that will
not fix themselves score zero.

**Verification is three-valued, and stays that way.** ``verified`` mirrors
``ToolResult.verified``: NULL means the result could not be checked, which is not
the same as checked-and-wrong. Collapsing them would teach the learner that
honest, unverifiable work was failure -- and the thing it would learn to prefer is
a model that claims more than it can show.
"""

from __future__ import annotations

from typing import Any

from .. import conn as db
from .base import now
from .model_probes import is_durable

#: Reward for a call that failed in a way that may not recur (timeout, rate
#: limit, overload). Low enough to lose to any working model, high enough that the
#: candidate keeps being tried -- a transient failure is evidence about a moment,
#: not about a model.
TRANSIENT_REWARD = 0.15

#: Latency past which a successful answer stops earning speed credit. Not a
#: timeout: a slow success is still a success, it just earns the floor of the
#: speed term rather than none of the reward.
SLOW_MS = 60_000.0

#: Weight split between "it worked" and "it was quick". Success dominates on
#: purpose: the router's job is to answer, and a fast wrong answer is worthless.
SUCCESS_WEIGHT = 0.75
SPEED_WEIGHT = 0.25

#: Penalty applied to a success the verifier actively contradicted. Not zero --
#: the model did answer -- but below a transient failure, because a confident
#: wrong answer costs the user more than a visible error does.
CONTRADICTED_REWARD = 0.05


def reward(
    *,
    ok: bool,
    latency_ms: int | None,
    verified: bool | None,
    error_kind: str | None = None,
) -> float:
    """Score one call in 0.0..1.0. Pure, so the formula can be tested directly.

    The shape, and why each branch exists:

    * verification said *wrong* -> ``CONTRADICTED_REWARD``. Worse than an error,
      because the user was told something untrue.
    * failed durably -> 0.0. Nothing about retrying changes it.
    * failed transiently -> ``TRANSIENT_REWARD``. Stays explorable.
    * succeeded -> success term plus a speed term that decays linearly to zero at
      ``SLOW_MS``. An unmeasured latency earns the *middle* of the speed term, not
      the top: absence of a measurement is not evidence of being fast.
    """
    if ok and verified is False:
        return CONTRADICTED_REWARD
    if not ok:
        return 0.0 if is_durable(error_kind) else TRANSIENT_REWARD

    if latency_ms is None:
        speed = 0.5
    else:
        speed = max(0.0, 1.0 - (max(0, latency_ms) / SLOW_MS))
    return SUCCESS_WEIGHT + SPEED_WEIGHT * speed


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
) -> dict[str, Any]:
    """Append the outcome of one real call. Never raises into the caller.

    Recording must not be able to break the thing it is observing: a learner that
    takes down a chat turn because its table is missing would be a strictly worse
    assistant than one that never learned. Failures here are swallowed by the
    caller (``nexus.learning.outcomes``), which is the only place this is called
    from.
    """
    value = reward(ok=ok, latency_ms=latency_ms, verified=verified, error_kind=error_kind)
    stamp = now()
    with db.transaction() as c:
        c.execute(
            """
            INSERT INTO model_outcomes
                (job, key, provider, model, ok, latency_ms, first_token_ms,
                 verified, error_kind, reward, at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job,
                key,
                provider,
                model,
                1 if ok else 0,
                latency_ms,
                first_token_ms,
                None if verified is None else (1 if verified else 0),
                error_kind,
                value,
                stamp,
            ),
        )
    return {
        "job": job,
        "key": key,
        "ok": ok,
        "latency_ms": latency_ms,
        "verified": verified,
        "error_kind": error_kind,
        "reward": value,
        "at": stamp,
    }


def stats(job: str, *, limit_per_key: int = 200) -> dict[str, dict[str, Any]]:
    """Per-model aggregates for one job, keyed by model key.

    ``limit_per_key`` bounds how far back the aggregate looks so the learner
    tracks a provider that got better (or worse) instead of being anchored to its
    first week forever. A model is its recent self, not its lifetime average.
    """
    rows = db.query(
        """
        SELECT key, provider, model, ok, latency_ms, verified, reward, at
        FROM model_outcomes
        WHERE job = ?
        ORDER BY key, at DESC
        """,
        (job,),
    )

    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        bucket = out.setdefault(
            row["key"],
            {
                "key": row["key"],
                "provider": row["provider"],
                "model": row["model"],
                "calls": 0,
                "successes": 0,
                "failures": 0,
                "reward_total": 0.0,
                "latency_total": 0,
                "latency_samples": 0,
                "last_at": row["at"],
            },
        )
        if bucket["calls"] >= limit_per_key:
            continue
        bucket["calls"] += 1
        if row["ok"]:
            bucket["successes"] += 1
        else:
            bucket["failures"] += 1
        bucket["reward_total"] += float(row["reward"])
        if row["latency_ms"] is not None:
            bucket["latency_total"] += int(row["latency_ms"])
            bucket["latency_samples"] += 1

    for bucket in out.values():
        calls = bucket["calls"]
        bucket["mean_reward"] = bucket["reward_total"] / calls if calls else None
        bucket["success_rate"] = bucket["successes"] / calls if calls else None
        bucket["mean_latency_ms"] = (
            round(bucket["latency_total"] / bucket["latency_samples"])
            if bucket["latency_samples"]
            else None
        )
    return out


def recent(job: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """The newest outcomes, for the Models screen to show what was learned from."""
    if job:
        rows = db.query(
            "SELECT * FROM model_outcomes WHERE job = ? ORDER BY at DESC, id DESC LIMIT ?",
            (job, limit),
        )
    else:
        rows = db.query(
            "SELECT * FROM model_outcomes ORDER BY at DESC, id DESC LIMIT ?", (limit,)
        )
    return [dict(row) for row in rows]


def purge(keep_per_job: int = 2_000) -> int:
    """Bound the table. Returns rows deleted.

    Outcomes are a rolling window, not an archive: the learner reads the recent
    past, and an unbounded table would grow for the life of the install to answer
    a question nothing asks.
    """
    with db.transaction() as c:
        cursor = c.execute(
            """
            DELETE FROM model_outcomes
            WHERE id NOT IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY job ORDER BY at DESC, id DESC
                    ) AS rank
                    FROM model_outcomes
                ) ranked
                WHERE rank <= ?
            )
            """,
            (keep_per_job,),
        )
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
