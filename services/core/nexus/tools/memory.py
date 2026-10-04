"""Remembering, recalling and forgetting, as tools the model can actually call
(tasks F5, F7, F8; spec §16-§18, §39, §70).

Without these, "remember that I use Neovim" reaches a model that has no way to
write anything down. It would answer warmly, nothing would be stored, and the user
would discover next week that NEXUS never remembered -- which is precisely the
"fake memory" the specs prohibit (§78). Memory has to be a *tool* for the same
reason opening an application is: the only thing that may change state is a tool
call, so anything the assistant can genuinely do must exist as one.

Routing memory through the tool runtime rather than doing it inside the AI core
also buys the three things every other capability gets: the permission engine
decides (``memory.write`` is a prompt, ``memory.delete`` is high-risk), the call is
recorded in ``tool_invocations`` where the user can audit it, and the effect is
independently verified by reading the row back.

Three shapes here are deliberate:

* **Writing is immediate; a proposal is not.** An explicit "remember this" is the
  user speaking, so it stores approved and is retrievable at once (§16). What the
  assistant merely *infers* is worth remembering goes through
  :mod:`nexus.ai.memory_extract`, which proposes and waits. Both paths exist
  because conflating them either loses explicit instructions or remembers things
  nobody agreed to.
* **Forgetting resolves before it deletes.** ``memory.forget`` returns what it
  *would* remove and removes nothing on its own; the deletion is a second call with
  explicit ids. "Forget about work" could match four memories or four hundred, and
  a natural-language guess is not a mandate to delete (§70).
* **A delete is verified by re-query.** :func:`memories.verify_absent` checks the
  row, its vector, and its search-index entry are all gone. A memory that survives
  in the FTS index after being "deleted" would still surface in recall, so
  reporting success without that check would be a lie the user only finds later.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..core.events import bus
from ..db.repos import memories
from ..logging_setup import get_logger
from .registry import ToolResult, tool

log = get_logger("tools.memory")

#: Recall results per call. Small because these go into a model's context and the
#: point of ranking is that the top few are the relevant ones -- returning thirty
#: is a way of admitting the ranking did not work.
DEFAULT_RECALL = 6
MAX_RECALL = 20


def _index_later(memory_id: str) -> str:
    """Embed a freshly written memory in the background, from this worker thread.

    Returns a sentence about what will happen, because the caller reports it. The
    write already succeeded at this point -- SQLite is the source of truth -- so a
    failure to schedule embedding degrades recall to keyword-only for that memory
    until the next backfill, and is worth saying rather than hiding.

    Delegates rather than scheduling here: the HTTP routes need exactly the same
    thing, and two copies of "put this coroutine on the bus loop" is two places for
    the tracking to be forgotten in.
    """
    from ..ai import embeddings

    return embeddings.schedule_index(memory_id)


# ------------------------------------------------------------------- remember


def _verify_written(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    """Read the memory back. A write that cannot be re-read did not happen."""
    if not result.ok or not isinstance(result.data, dict):
        return False, "No memory id was returned, so nothing could be checked."
    memory_id = str(result.data.get("id") or "")
    if not memory_id:
        return False, "No memory id was returned, so nothing could be checked."
    row = memories.get(memory_id)
    if row is None:
        return False, "The memory was not found when read back immediately after writing."
    if str(row.get("body") or "").strip() != str(params.get("body") or "").strip():
        return False, "The stored text does not match what was submitted."
    state = "approved and retrievable" if row.get("approved") else "awaiting approval"
    return True, f"Read back from the database, {state}."


@tool(
    "memory.remember",
    title="Remember something",
    description=(
        "Store a durable fact about the user so it is available in future "
        "conversations. Use this when the user explicitly asks you to remember "
        "something, states a lasting preference, or corrects a fact you had wrong. "
        "Do not use it for the current state of things, for one-off requests, or "
        "for general knowledge -- only for facts that will still be true in a "
        "month. The fact becomes retrievable immediately."
    ),
    capability="memory.write",
    idempotent=False,
    verifier=_verify_written,
    parameters={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": (
                    "A short noun phrase naming the subject, e.g. 'Code editor' "
                    "or 'Deploy process'."
                ),
            },
            "body": {
                "type": "string",
                "description": (
                    "The fact itself, one sentence in the third person, e.g. "
                    "'Uses Neovim for all editing, with a Lua config.'"
                ),
            },
            "category": {
                "type": "string",
                "enum": list(memories.CATEGORIES),
                "description": "Which kind of fact this is.",
            },
            "project_id": {
                "type": "string",
                "description": (
                    "Scope the memory to one project when it is only true inside "
                    "that project. Leave empty for a fact about the user generally."
                ),
            },
        },
        "required": ["body"],
    },
)
def memory_remember(params: dict[str, Any]) -> ToolResult:
    body = str(params.get("body") or "").strip()
    if not body:
        return ToolResult(
            ok=False,
            error="There is no fact to remember -- 'body' was empty.",
            summary="Nothing was remembered.",
        )
    title = str(params.get("title") or "").strip() or body[:60]
    category = str(params.get("category") or "general").strip().lower()
    project_id = str(params.get("project_id") or "").strip() or None

    row = memories.create(
        title, body, category=category, project_id=project_id
    )
    note = _index_later(row["id"])
    summary = f"Remembered: {title}."
    if note:
        summary = f"{summary} {note}"
    return ToolResult(ok=True, data=row, summary=summary)


# --------------------------------------------------------------------- recall


@tool(
    "memory.recall",
    title="Search what you remember",
    description=(
        "Search stored memories by meaning and by keyword. Relevant memories are "
        "already included in your context automatically, so use this only when the "
        "user asks directly what you remember, or when you need to check something "
        "specific that is not in front of you. Returns nothing when there is no "
        "match -- which means there is no such memory, not that the search failed."
    ),
    capability="memory.write",
    read_only=True,
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to look for, in the user's own words.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_RECALL,
                "description": f"How many to return (default {DEFAULT_RECALL}).",
            },
            "project_id": {
                "type": "string",
                "description": "Restrict to one project's memories.",
            },
        },
        "required": ["query"],
    },
)
def memory_recall(params: dict[str, Any]) -> ToolResult:
    """Search memory. Semantic when reachable, keyword always, and it says which.

    The handler is sync (tool handlers run in a worker thread) while semantic
    search is async, so the coroutine is run on the bus's loop and waited for. When
    there is no loop -- a unit test, a CLI call -- it falls back to keyword search
    and the summary says so, rather than reporting a thin result as a full one.
    """
    query = str(params.get("query") or "").strip()
    if not query:
        return ToolResult(
            ok=False, error="No search text was given.", summary="Nothing was searched."
        )
    limit = int(params.get("limit") or DEFAULT_RECALL)
    limit = max(1, min(limit, MAX_RECALL))
    project_id = str(params.get("project_id") or "").strip() or None

    loop = bus.loop
    found: dict[str, Any]
    if loop is None:
        rows = memories.search_text(query, limit=limit)
        found = {
            "items": rows,
            "semantic": False,
            "semantic_ran": False,
            "notice": "Keyword-only: semantic search needs the running service.",
        }
    else:
        from ..ai import embeddings

        future = asyncio.run_coroutine_threadsafe(
            embeddings.search(query, limit=limit, project_id=project_id), loop
        )
        try:
            found = future.result(timeout=30)
        except Exception as exc:  # noqa: BLE001
            log.info("semantic recall failed, using keyword: %s", exc)
            found = {
                "items": memories.search_text(query, limit=limit),
                "semantic": False,
                "semantic_ran": False,
                "notice": f"Keyword-only: semantic search failed. {exc}",
            }

    items = [
        {
            "id": item.get("id"),
            "title": item.get("title"),
            "body": item.get("body"),
            "category": item.get("category"),
            "found_by": item.get("match", "keyword"),
            "created_at": item.get("created_at"),
        }
        for item in (found.get("items") or [])
    ]
    notice = str(found.get("notice") or "")
    if not items:
        summary = f"Nothing remembered matches {query!r}."
        if notice:
            summary = f"{summary} {notice}"
        return ToolResult(
            ok=True,
            data={
                "items": [],
                "notice": notice,
                # Reported separately so the model can tell "meaning matching ran
                # and nothing was relevant" from "meaning matching never ran".
                "semantic": found.get("semantic"),
                "semantic_hits": found.get("semantic_hits", 0),
            },
            summary=summary,
        )
    summary = f"Found {len(items)} memor{'y' if len(items) == 1 else 'ies'} about {query!r}."
    if notice:
        summary = f"{summary} {notice}"
    return ToolResult(
        ok=True,
        data={
            "items": items,
            "notice": notice,
            "semantic": found.get("semantic"),
            "semantic_hits": found.get("semantic_hits", 0),
        },
        summary=summary,
    )


# --------------------------------------------------------------------- forget


@tool(
    "memory.forget",
    title="Find what forgetting would remove",
    description=(
        "Resolve a 'forget about ...' request into the exact list of memories it "
        "would delete. This deletes NOTHING -- it shows the user what is at stake. "
        "Show them the list, get their confirmation, then call memory.delete with "
        "the ids they agree to."
    ),
    capability="memory.write",
    read_only=True,
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to forget, in words."},
            "category": {
                "type": "string",
                "enum": list(memories.CATEGORIES),
                "description": "Forget a whole category instead of a search.",
            },
            "project_id": {
                "type": "string",
                "description": "Forget everything belonging to one project.",
            },
        },
    },
)
def memory_forget(params: dict[str, Any]) -> ToolResult:
    query = str(params.get("query") or "").strip() or None
    category = str(params.get("category") or "").strip() or None
    project_id = str(params.get("project_id") or "").strip() or None
    if not any((query, category, project_id)):
        return ToolResult(
            ok=False,
            error=(
                "Forgetting needs a target: a search phrase, a category, or a "
                "project. Nothing was matched and nothing was deleted."
            ),
            summary="No target for forgetting was given.",
        )

    matched = memories.resolve_forget(
        query=query, category=category, project_id=project_id
    )
    items = [
        {"id": m.get("id"), "title": m.get("title"), "body": m.get("body"),
         "category": m.get("category")}
        for m in matched
    ]
    if not items:
        return ToolResult(
            ok=True,
            data={"matched": [], "count": 0},
            summary="Nothing remembered matches that, so there is nothing to forget.",
        )
    return ToolResult(
        ok=True,
        data={"matched": items, "count": len(items), "deleted": False},
        summary=(
            f"{len(items)} memor{'y' if len(items) == 1 else 'ies'} would be deleted. "
            "Nothing has been deleted yet -- confirm with the user first."
        ),
    )


def _verify_deleted(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    """Prove the rows, their vectors and their index entries are all gone (§70)."""
    ids = [str(i) for i in (params.get("ids") or [])]
    if not ids:
        return False, "No ids were given, so nothing could be checked."
    survivors = memories.verify_absent(ids)
    if survivors:
        return False, (
            f"{len(survivors)} record(s) still present after deletion: "
            f"{', '.join(survivors[:5])}."
        )
    return True, (
        f"Re-queried all {len(ids)} id(s): the rows, their embeddings and their "
        "search-index entries are gone."
    )


@tool(
    "memory.delete",
    title="Delete memories",
    description=(
        "Permanently delete specific memories by id. This cannot be undone. Only "
        "call it with ids the user has explicitly confirmed, normally the ones "
        "memory.forget listed for them."
    ),
    capability="memory.delete",
    idempotent=True,
    verifier=_verify_deleted,
    parameters={
        "type": "object",
        "properties": {
            "ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The memory ids to delete, from memory.forget.",
            },
        },
        "required": ["ids"],
    },
)
def memory_delete(params: dict[str, Any]) -> ToolResult:
    ids = [str(i).strip() for i in (params.get("ids") or []) if str(i).strip()]
    if not ids:
        return ToolResult(
            ok=False, error="No memory ids were given.", summary="Nothing was deleted."
        )

    # Report what was asked for but did not exist, rather than counting it as
    # deleted. "Deleted 4" when two ids were already gone overstates what happened.
    present = [mid for mid in ids if memories.get(mid) is not None]
    absent = [mid for mid in ids if mid not in present]
    removed = memories.delete_many(present) if present else 0

    summary = f"Deleted {removed} memor{'y' if removed == 1 else 'ies'}."
    if absent:
        summary += f" {len(absent)} of the ids given did not exist."
    return ToolResult(
        ok=removed > 0 or not present,
        data={"deleted": removed, "requested": len(ids), "not_found": absent},
        summary=summary,
    )


__all__ = [
    "DEFAULT_RECALL",
    "MAX_RECALL",
    "memory_delete",
    "memory_forget",
    "memory_recall",
    "memory_remember",
]
