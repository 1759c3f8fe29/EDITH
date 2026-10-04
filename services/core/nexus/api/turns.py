"""Where a turn goes and whether it may start -- once, for chat and for voice.

Two routes run a turn now: ``POST /api/chat`` and ``POST /api/voice/converse``. Both
have to answer the same three questions before any token exists, and each answer has a
wrong version that looks fine.

* **Which conversation is this?** An id that was deleted between the window opening and
  the user speaking is a 404, not a new conversation invented to hold the answer. A
  request with no id gets a conversation created for it, titled from what was said.
* **Is one already running there?** :class:`nexus.ai.core.AiCore` keys its active-turn
  and cancellation state by conversation id, so a second concurrent turn would overwrite
  the first one's entry and make the stop button stop the wrong turn. That is a 409 with
  a sentence in it, not a race to debug later.
* **What is it called?** The first line of the request, not a model call: naming a
  conversation is not worth a round trip the user waits for, and the UI can rename it.

The reason this is a module rather than three helpers in ``chat_routes`` is that the
voice route needs identical answers. A private second copy of "is this conversation
busy" drifts the moment one of them learns something -- and the symptom is a stop button
that stops someone else's turn, which is not a bug anybody would trace back to a
duplicated four-line guard.
"""

from __future__ import annotations

from fastapi import HTTPException

from ..ai.core import ai_core
from ..db.repos import conversations

#: Fallback title for a turn that arrived with no text at all -- an image, or audio that
#: transcribed to nothing. "New conversation" is what the UI shows until it is renamed.
UNTITLED = "New conversation"


def title_from(message: str) -> str:
    """A first title for a new conversation, from the request itself."""
    first = message.strip().splitlines()[0] if message.strip() else ""
    trimmed = " ".join(first.split())
    if len(trimmed) <= 60:
        return trimmed or UNTITLED
    return trimmed[:57].rstrip() + "..."


def resolve_conversation(
    conversation_id: str | None,
    *,
    first_message: str,
    project_id: str | None = None,
) -> str:
    """The conversation this turn belongs to, creating one if the caller had none.

    Raises 404 for an id that is gone. Silently creating a replacement would put the
    answer somewhere the user is not looking, which reads as the assistant ignoring them.
    """
    if conversation_id:
        if conversations.get(conversation_id) is None:
            raise HTTPException(
                status_code=404,
                detail="That conversation no longer exists, so this message has nowhere to go.",
            )
        return conversation_id
    return str(conversations.create(title_from(first_message), project_id=project_id)["id"])


def refuse_if_busy(conversation_id: str) -> None:
    """409 if a turn is already running on this conversation. See the module docstring."""
    if any(t.get("conversation_id") == conversation_id for t in ai_core().active()):
        raise HTTPException(
            status_code=409,
            detail=(
                "This conversation is already running a turn. Wait for it to finish, "
                "or stop it first."
            ),
        )


__all__ = ["UNTITLED", "refuse_if_busy", "resolve_conversation", "title_from"]
