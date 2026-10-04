"""Learning from what actually happened (spec §30, §37, §38).

Three audits named the same shortfall in three ways: the founding doc's §38
("learning from outcomes" -- PARTIAL, no loop), the ultra-intelligent doc's
anticipation gap, and the master-prompt audit's gap 3. The code stored outcomes
and learned nothing from them.

This package is the loop. It is deliberately *small and real* rather than large and
aspirational, and every part of it operates on data NEXUS already collects:

* :mod:`.outcomes` -- the safe recording facade and the switch.
* :mod:`.bandit` -- reinforcement learning proper: a contextual bandit
  (Thompson sampling over Beta posteriors) that learns which model serves which
  job, from measured rewards.

What this package deliberately is **not**: it does not train a model, ship
weights, or claim to improve the providers' own behaviour. It learns *selection*
and *timing* from local measurements, which is the kind of learning a desktop
assistant can actually do honestly. Anything beyond that would be a claim with no
mechanism behind it, which is what §78 exists to forbid.
"""

from __future__ import annotations

from . import bandit, outcomes  # noqa: F401

__all__ = ["bandit", "outcomes"]
