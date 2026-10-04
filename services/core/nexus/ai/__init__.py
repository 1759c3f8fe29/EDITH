"""The AI core: the layer that turns a sentence into verified action (Phase F).

Everything below this package is a capability -- a router that picks models, a
registry of tools, a permission engine, a memory store. None of them decide
anything. This package is where deciding happens, and it is the only place that
holds the loop the behaviour specs describe:

    PERCEIVE -> INTERPRET -> ASSESS -> PLAN -> AUTHORIZE -> ACT
             -> OBSERVE -> VERIFY -> ADAPT -> COMPLETE -> REPORT

The modules split along the seams of that loop:

* :mod:`persona`     -- who NEXUS is, assembled from what it can *actually* do.
* :mod:`intent`      -- what kind of request this is, which decides the model.
* :mod:`context`     -- what NEXUS knows that is relevant, under a token budget.
* :mod:`embeddings`  -- semantic recall, so memory is meaning and not keywords.
* :mod:`memory_extract` -- what is worth remembering, proposed for approval.
* :mod:`memory_sweep`  -- the same, on a timer, for the exchanges that pass missed.
* :mod:`core`        -- the loop itself, including the tool-calling round trip.

One rule shapes all of them, and it is the rule the specs state most often:
**never claim something happened when it did not.** Concretely, in this package
that means generated prose never becomes an effect (only a schema-validated tool
call reaches :mod:`nexus.tools.runtime`), a tool result is never reported as
success until its verifier agrees, and the capability list handed to the model is
built from the live registry so the model cannot offer what this computer lacks.
"""

from __future__ import annotations

from .core import AiCore, Turn, TurnCancelled, ai_core
from .intent import Intent, classify
from .persona import system_prompt

__all__ = [
    "AiCore",
    "Intent",
    "Turn",
    "TurnCancelled",
    "ai_core",
    "classify",
    "system_prompt",
]
