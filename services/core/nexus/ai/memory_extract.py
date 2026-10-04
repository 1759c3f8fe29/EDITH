"""What is worth remembering, proposed for approval (task F5; spec §16, §487).

The specs draw a hard line here: NEXUS may notice that something is worth
remembering, and it may not decide to remember it. Approval is the user's.
So every function in this module writes through
:func:`nexus.db.repos.memories.propose`, which stores ``approved = 0`` -- a row
that is invisible to retrieval until the user says yes on the Memory screen.

That constraint makes the interesting question "what is worth proposing", because
a proposal the user has to read and reject costs them more than it saves. The
rules below are therefore about *durability*, not about interest:

* A fact must still be true next month. "I use Neovim" is a memory; "I'm debugging
  the auth service right now" is not, and a store full of stale states is worse
  than an empty one because the model reasons from it confidently.
* It must be about the user, not about the world. "Postgres uses MVCC" belongs to
  the model's training, and remembering it wastes a retrieval slot on something
  that was never going to be forgotten.
* It must have come from the *user's* words. Extracting from NEXUS's own reply
  would let the assistant remember its own inferences as though the user had
  stated them -- a fabricated memory with a plausible provenance, which is the
  worst kind. The assistant's turn is passed in only as context for resolving what
  the user was answering.

Extraction runs on a small fast model and it is allowed to return nothing. That is
the common case and the correct one: most turns contain no durable fact, and a
model pressured to find something in every conversation will invent preferences
the user never expressed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..db.repos import memories
from ..logging_setup import get_logger
from ..providers.base import ChatRequest, Message, ProviderError
from ..router import NoModelAvailable, router
from . import persona

log = get_logger("ai.memory_extract")

#: Most turns yield zero or one durable fact. The cap exists so a long
#: conversation cannot produce a wall of proposals the user will dismiss without
#: reading -- which would train them to dismiss the ones that matter too.
MAX_PER_TURN = 3

#: Below this, there is nothing to extract from. A two-word turn ("yes", "thanks")
#: cannot contain a durable fact, and asking a model about it is a round trip spent
#: to be told no.
MIN_REQUEST_CHARS = 25

_CATEGORIES = ", ".join(memories.CATEGORIES)

_INSTRUCTIONS = f"""\
You extract durable facts about the user from one exchange, for a personal
assistant's long-term memory.

Return JSON only, in this exact shape:

{{"memories": [{{"title": "...", "body": "...", "category": "..."}}]}}

Return {{"memories": []}} when there is nothing worth keeping. That is the normal
answer -- most exchanges contain no durable fact, and an empty list is a correct
and useful response.

Extract a fact only when all of these hold:

* It came from what the USER said, not from the assistant's reply.
* It will still be true in a month. Preferences, tools, habits, relationships,
  constraints, recurring goals -- yes. Current state, one-off requests, what they
  are doing right now, anything phrased in the present continuous -- no.
* It is about the user or their world, not general knowledge.
* The user did not ask you to forget it.

The title is a short noun phrase naming the subject ("Code editor", "Deploy
process", "Sister's birthday"). The body is one sentence stating the fact in the
third person ("Uses Neovim for all editing, with a Lua config.").

The category must be one of: {_CATEGORIES}.

Never invent a fact that is merely consistent with what was said. If the user
mentioned Python once, that is not a preference for Python.\
"""

#: Phrases that make extraction pointless or actively wrong. "Forget X" must not
#: produce a proposal to remember X -- which is exactly what a naive extractor
#: does, because the sentence contains the fact.
_SKIP_MARKERS = (
    "forget ",
    "don't remember",
    "do not remember",
    "stop remembering",
    "delete my",
    "clear my memor",
)


@dataclass(slots=True)
class Candidate:
    title: str
    body: str
    category: str

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "body": self.body, "category": self.category}


async def extract(
    request: str,
    reply: str = "",
) -> list[Candidate]:
    """Read one exchange and return durable facts worth proposing.

    Returns an empty list on any failure. Extraction is an enhancement running
    behind a completed reply, and an unreachable model means the user gets no
    proposal -- not an error about a feature they did not invoke.

    That swallowing makes "found nothing" and "could not look" indistinguishable,
    which is correct here and wrong for a sweep that advances a watermark past
    whatever it examined. :func:`extract_strict` is the same work with the failure
    left visible.
    """
    try:
        return await extract_strict(request, reply)
    except NoModelAvailable as exc:
        log.info("memory extraction skipped: %s", exc)
        return []
    except ProviderError as exc:
        log.info("memory extraction failed: %s", exc.message)
        return []


async def extract_strict(
    request: str,
    reply: str = "",
) -> list[Candidate]:
    """Extraction that raises instead of reporting an empty result.

    Raises :class:`~nexus.router.NoModelAvailable` when no fast model is reachable
    and :class:`~nexus.providers.base.ProviderError` when the request fails. An
    empty list from this function therefore means the model read the exchange and
    found nothing durable, which is a real answer and the most common one.
    """
    cleaned = (request or "").strip()
    if len(cleaned) < MIN_REQUEST_CHARS:
        return []
    lowered = cleaned.lower()
    if any(marker in lowered for marker in _SKIP_MARKERS):
        return []

    exchange = f"USER: {cleaned}"
    if reply.strip():
        # Truncated: the assistant's reply is context for what the user meant, not
        # material to extract from, and a long one would dominate the prompt.
        exchange += f"\n\nASSISTANT (context only, do not extract from this): {reply.strip()[:1_500]}"

    decision = router().resolve("fast")

    # ``with_tools=False``: this pass cannot act, and telling a model it can
    # operate the computer while handing it no tools invites it to describe
    # actions instead of returning JSON.
    system = persona.system_prompt(job="fast", with_tools=False) + "\n\n---\n\n" + _INSTRUCTIONS

    request_obj = ChatRequest(
        messages=[Message(role="user", content=exchange)],
        model=decision.model_id,
        system=system,
        temperature=0.0,
        max_output_tokens=600,
    )

    collected: list[str] = []
    async for event in router().stream(request_obj, job="fast", decision=decision):
        if event.type == "text" and event.text:
            collected.append(event.text)

    return _parse("".join(collected))


def _parse(text: str) -> list[Candidate]:
    """Read the model's JSON, tolerating the wrappers models add.

    Fenced code blocks and a sentence of preamble are common even under an
    instruction not to add them. Recovering from that is worth a few lines here;
    the alternative is silently proposing nothing whenever the model is chatty.
    """
    raw = (text or "").strip()
    if not raw:
        return []
    if "```" in raw:
        chunks = raw.split("```")
        for chunk in chunks:
            stripped = chunk.strip()
            if stripped.startswith("json"):
                stripped = stripped[4:].strip()
            if stripped.startswith("{"):
                raw = stripped
                break
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return []
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return []

    items = parsed.get("memories") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        return []

    out: list[Candidate] = []
    for item in items[:MAX_PER_TURN]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()[:120]
        body = str(item.get("body") or "").strip()[:1_000]
        if not body:
            continue
        category = str(item.get("category") or "general").strip().lower()
        if category not in memories.CATEGORIES:
            category = "general"
        out.append(Candidate(title=title or body[:60], body=body, category=category))
    return out


def record(
    candidates: list[Candidate],
    *,
    conversation_id: str | None = None,
    message_id: str | None = None,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Store candidates as proposals, skipping ones already known.

    Each proposal keeps the conversation and message it came from, so the Memory
    screen can show the user the exact sentence that produced it. A memory whose
    provenance cannot be shown is one the user has to trust blindly, and the whole
    point of the approval step is that they do not have to.

    Deduplication is by exact body against the most recent 200 rows, approved and
    pending alike. It catches the ordinary case -- a fast model at temperature 0
    re-reading the same exchange writes the same sentence -- and not a paraphrase.
    Catching paraphrases would need an embedding per candidate, which is a real cost
    for a duplicate the user can dismiss in one click.
    """
    if not candidates:
        return []

    existing = {
        str(m.get("body") or "").strip().lower()
        for m in memories.list_all(limit=200, approved=None)
    }

    stored: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate.body.strip().lower() in existing:
            continue  # already known or already proposed; a duplicate is noise
        row = memories.propose(
            candidate.title,
            candidate.body,
            category=candidate.category,
            project_id=project_id,
            source_conversation_id=conversation_id,
            source_message_id=message_id,
        )
        stored.append(row)
        # Added as we go: two candidates in one batch can say the same thing, and
        # re-reading the table per candidate to notice that would be a query per
        # proposal.
        existing.add(candidate.body.strip().lower())

    if stored:
        log.info("proposed %d memories for approval", len(stored))
    return stored


async def propose_from_turn(
    request: str,
    reply: str,
    *,
    conversation_id: str | None = None,
    message_id: str | None = None,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Extract from one exchange and record whatever it found, awaiting approval."""
    return record(
        await extract(request, reply),
        conversation_id=conversation_id,
        message_id=message_id,
        project_id=project_id,
    )


__all__ = [
    "MAX_PER_TURN",
    "Candidate",
    "extract",
    "extract_strict",
    "propose_from_turn",
    "record",
]
