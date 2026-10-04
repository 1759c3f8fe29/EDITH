"""Reads and writes for everything the user owns: conversations, memories,
activity, projects, tasks, search, settings.

No permission checks here, deliberately. §45's engine guards what NEXUS does to
the *machine*; these routes only touch NEXUS's own database, which the user
already owns, and prompting to read your own conversation list would train people
to click through prompts without reading them -- the exact habit that makes the
engine worthless when it matters.

Memory is the one place with a rule that is easy to get wrong: a proposed memory
is not a memory. §32 requires the user to approve before NEXUS treats something
as remembered, so ``approved`` is never defaulted to true on this surface, and the
pending list is separate from the real one.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from ...ai import agent
from ...db import maintenance
from ...db.repos import (
    activity,
    app_settings,
    conversations,
    memories,
    projects,
    search as search_repo,
    tasks,
)
from ...logging_setup import get_logger

router = APIRouter(prefix="/api", tags=["data"])
log = get_logger("api.data")


def _index(memory_id: str) -> str:
    """Schedule semantic indexing for a memory, returning why not if it could not be.

    Kept as one helper rather than inlined three times so every door into an
    approved memory -- the user writing one, approving a proposal, editing the
    text -- goes through the same path. When a memory reached the store without
    passing here, the symptom was not an error: it was a memory that simply never
    matched a paraphrase, which is indistinguishable from having nothing to say.
    """
    from ...ai import embeddings

    return embeddings.schedule_index(memory_id)


def _split(value: str | None) -> tuple[str, ...] | None:
    """Turn ``?kinds=tool,permission`` into the tuple the repos expect."""
    if not value:
        return None
    parts = tuple(p.strip() for p in value.split(",") if p.strip())
    return parts or None


# ----------------------------------------------------------------- conversations


@router.get("/conversations")
def list_conversations(limit: int = 50, archived: bool = False, project_id: str | None = None) -> dict[str, Any]:
    return {
        "conversations": conversations.list_all(limit=limit, archived=archived, project_id=project_id)
    }


@router.post("/conversations")
def create_conversation(
    title: str = Body("New conversation", embed=True),
    project_id: str | None = Body(None, embed=True),
) -> dict[str, Any]:
    return {"conversation": conversations.create(title, project_id=project_id)}


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str, limit: int = 500) -> dict[str, Any]:
    convo = conversations.get(conversation_id)
    if convo is None:
        raise HTTPException(status_code=404, detail="No conversation with that id.")
    return {
        "conversation": convo,
        "messages": conversations.list_messages(conversation_id, limit=limit),
    }


@router.patch("/conversations/{conversation_id}")
def update_conversation(
    conversation_id: str,
    title: str | None = Body(None, embed=True),
    archived: bool | None = Body(None, embed=True),
) -> dict[str, Any]:
    if title is None and archived is None:
        raise HTTPException(status_code=400, detail="Nothing to change.")
    if title is not None and not conversations.rename(conversation_id, title):
        raise HTTPException(status_code=404, detail="No conversation with that id.")
    if archived is not None and not conversations.set_archived(conversation_id, archived):
        raise HTTPException(status_code=404, detail="No conversation with that id.")
    return {"conversation": conversations.get(conversation_id)}


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str) -> dict[str, Any]:
    if not conversations.delete(conversation_id):
        raise HTTPException(status_code=404, detail="No conversation with that id.")
    return {"ok": True, "message": "Conversation deleted."}


@router.delete("/messages/{message_id}")
def delete_message(message_id: str) -> dict[str, Any]:
    if not conversations.delete_message(message_id):
        raise HTTPException(status_code=404, detail="No message with that id.")
    return {"ok": True, "message": "Message deleted."}


# ---------------------------------------------------------------------- memories


@router.get("/memories")
def list_memories(
    limit: int = 100,
    category: str | None = None,
    project_id: str | None = None,
    include_pending: bool = False,
) -> dict[str, Any]:
    """Approved memories by default; pending ones only when asked for.

    The split is the point. Mixing an unapproved proposal into the list would show
    the user something NEXUS has not been allowed to remember as if it already did
    (§32), and there would be no way to tell from the row which it was.
    """
    approved: bool | None = None if include_pending else True
    return {
        "memories": memories.list_all(
            limit=limit, approved=approved, category=category, project_id=project_id
        ),
        "pending_count": memories.pending_count(),
        "coverage": memories.coverage(),
    }


@router.get("/memories/pending")
def pending_memories(limit: int = 100) -> dict[str, Any]:
    return {"memories": memories.list_all(limit=limit, approved=False), "count": memories.pending_count()}


@router.post("/memories")
def create_memory(
    title: str = Body(..., embed=True),
    body: str = Body(..., embed=True),
    category: str = Body("general", embed=True),
    project_id: str | None = Body(None, embed=True),
) -> dict[str, Any]:
    """A memory the *user* wrote, so it is approved on creation.

    ``memories.propose`` is the other door, used by the AI, and it does not
    approve. Which function runs is decided by who is asking, not by a parameter
    the caller can set -- otherwise the model could write itself an approved
    memory through this route.
    """
    if not title.strip() or not body.strip():
        raise HTTPException(status_code=400, detail="A memory needs both a title and something to remember.")
    row = memories.create(title, body, category=category, project_id=project_id)
    return {"memory": row, "indexing": _index(row["id"])}


@router.post("/memories/{memory_id}/approve")
def approve_memory(memory_id: str) -> dict[str, Any]:
    """Approve a proposed memory -- and only now embed it.

    Embedding on *approval* rather than on proposal is the point: an unapproved
    proposal is not a memory (§32), and sending its text to an embedding endpoint
    would mean the model had already left the machine with something the user never
    agreed to remember.
    """
    if not memories.approve(memory_id):
        raise HTTPException(status_code=404, detail="No pending memory with that id.")
    return {"memory": memories.get(memory_id), "indexing": _index(memory_id)}


@router.patch("/memories/{memory_id}")
def update_memory(
    memory_id: str,
    title: str | None = Body(None, embed=True),
    body: str | None = Body(None, embed=True),
    category: str | None = Body(None, embed=True),
    pinned: bool | None = Body(None, embed=True),
    project_id: str | None = Body(None, embed=True),
) -> dict[str, Any]:
    changed = memories.update(
        memory_id, title=title, body=body, category=category, pinned=pinned, project_id=project_id
    )
    if not changed:
        raise HTTPException(status_code=404, detail="No memory with that id, or nothing to change.")
    # Editing the text drops the stored vector in the repo, because a vector that
    # describes the old wording is worse than none: it would keep matching what the
    # user just changed. Re-embedding is scheduled here so the edit does not
    # silently demote that memory to keyword-only for the rest of the session.
    note = _index(memory_id) if (title is not None or body is not None) else ""
    return {"memory": memories.get(memory_id), "indexing": note}


@router.delete("/memories/{memory_id}")
def delete_memory(memory_id: str) -> dict[str, Any]:
    if not memories.delete(memory_id):
        raise HTTPException(status_code=404, detail="No memory with that id.")
    # Verified rather than assumed: §33 says forgetting must actually forget, and
    # a delete that reported success while the row survived would be the worst
    # possible lie in this module.
    still_there = memories.verify_absent([memory_id])
    if still_there:
        raise HTTPException(
            status_code=500,
            detail="The delete was accepted but the memory is still in the database. Nothing was forgotten.",
        )
    return {"ok": True, "message": "Forgotten.", "verified": True}


@router.get("/memories/search")
def search_memories(q: str, limit: int = 20, include_pending: bool = False) -> dict[str, Any]:
    """Text search only.

    Semantic search needs an embedding for the query, which means a provider call,
    which means this route would fail differently when offline. That belongs in the
    models surface; here a text match is always available and always honest about
    being a text match.
    """
    if not q.strip():
        raise HTTPException(status_code=400, detail="Give me something to search for.")
    return {
        "memories": memories.search_text(q, limit=limit, approved_only=not include_pending),
        "mode": "text",
    }


@router.get("/memories/recall")
async def recall_memories(
    q: str,
    limit: int = 8,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Search memory by meaning *and* by keyword -- what the AI itself uses.

    Separate from ``/memories/search``, which is text-only and therefore always
    available. This route can involve a provider call, so it can degrade, and the
    response says which halves actually ran: ``semantic`` is false and ``notice``
    explains why when only keyword matching happened. A merged result set that
    quietly dropped its semantic half would look exactly like a memory with
    nothing relevant in it (§60).
    """
    if not q.strip():
        raise HTTPException(status_code=400, detail="Give me something to recall.")
    from ...ai import embeddings

    return await embeddings.search(q, limit=limit, project_id=project_id)


@router.get("/memories/embeddings")
def embedding_status() -> dict[str, Any]:
    """Whether memory is actually searchable by meaning, answered from stored state.

    No network call, so opening the Memory screen never waits on a provider. The
    number that matters is ``unembedded``: while it is above zero, some memories
    can only be found by the user's exact words, and the screen must say so rather
    than presenting one search box that behaves two different ways.
    """
    from ...ai import embeddings

    return {"embeddings": embeddings.status()}


@router.post("/memories/embeddings/backfill")
async def embedding_backfill(limit: int = Body(500, embed=True)) -> dict[str, Any]:
    """Embed every approved memory that has no vector, now, and report real counts.

    Awaited rather than scheduled, unlike the startup pass: this route exists
    because someone pressed a button and is waiting to see what happened, and
    "embedded 4, failed 2, 2 still pending" is the answer. It is safe to run twice
    -- already-embedded memories are not selected.
    """
    from ...ai import embeddings

    result = await embeddings.backfill(limit=max(1, min(int(limit), 2_000)))
    return {"backfill": result, "coverage": memories.coverage()}


@router.get("/proactive/status")
def proactive_status() -> dict[str, Any]:
    """What the proactive engine (L10) is doing, including having given up.

    Same reason as the sweep status beside it: a stalled engine and a
    switched-off one must not look identical on the Awareness screen, and a
    loop that stopped itself after repeated failures says so here rather than
    looking alive (§60).
    """
    from ...core import proactive

    return {"proactive": proactive.status()}


@router.get("/memories/sweep")
def memory_sweep_status() -> dict[str, Any]:
    """What the periodic extraction pass is doing (task F11).

    Exists so the Memory screen can say whether NEXUS is still reading back
    conversation and how far behind it is. Without it a stalled sweep and an
    up-to-date one look identical -- both show a quiet Memory screen -- which is
    the §78 failure of a background task that appears alive while doing nothing.
    """
    from ...ai import memory_sweep

    return {"sweep": memory_sweep.status()}


@router.post("/memories/sweep/run")
async def memory_sweep_run() -> dict[str, Any]:
    """Run one batch now, instead of waiting for the interval.

    Reports the run's own outcome including its failure, rather than a bare 500: a
    sweep that could not reach a model has not lost anything -- the watermark stays
    put and the exchanges are retried -- and that is a different message from "the
    request was invalid".
    """
    from ...ai import memory_sweep

    result = await memory_sweep.sweep_once()
    return {
        "result": result.to_dict(),
        "sweep": memory_sweep.status(),
        "message": (
            result.error
            or f"Read {result.examined} exchange(s) and proposed {result.proposed} memory(s)."
        ),
    }


# ---------------------------------------------------------------------- activity


@router.get("/activity")
def list_activity(
    limit: int = 100,
    before: str | None = None,
    kinds: str | None = None,
    levels: str | None = None,
) -> dict[str, Any]:
    return {
        "activity": activity.list_recent(
            limit=limit, before=before, kinds=_split(kinds), levels=_split(levels)
        ),
        "counts": activity.counts_by_kind(),
    }


# ------------------------------------------------------------- tasks & projects


@router.get("/tasks")
def list_tasks(limit: int = 50, statuses: str | None = None, project_id: str | None = None) -> dict[str, Any]:
    return {"tasks": tasks.list_all(limit=limit, statuses=_split(statuses), project_id=project_id)}


@router.get("/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    task = tasks.get_with_steps(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="No task with that id.")
    return {
        "task": task,
        "progress": tasks.progress(task_id),
        # Whether a run is in flight *in this process*. Distinct from the stored
        # status: a task left 'running' by a core that was killed is not running, and
        # this is the only thing that can tell the two apart -- so the UI can offer
        # Run rather than a Cancel button for a run that no longer exists.
        "running": agent.is_running(task_id),
        # The steps say what was planned; the activity says what happened. Agent
        # progress has to come from the second one (§81).
        "activity": activity.for_task(task_id),
    }


@router.get("/projects")
def list_projects(status: str | None = "active", limit: int = 100) -> dict[str, Any]:
    return {"projects": projects.list_all(status=status, limit=limit)}


@router.post("/projects")
def create_project(
    title: str = Body(..., embed=True),
    description: str = Body("", embed=True),
) -> dict[str, Any]:
    if not title.strip():
        raise HTTPException(status_code=400, detail="A project needs a name.")
    return {"project": projects.create(title, description)}


@router.patch("/projects/{project_id}")
def update_project(
    project_id: str,
    title: str | None = Body(None, embed=True),
    description: str | None = Body(None, embed=True),
    status: str | None = Body(None, embed=True),
) -> dict[str, Any]:
    if not projects.update(project_id, title=title, description=description, status=status):
        raise HTTPException(status_code=404, detail="No project with that id, or nothing to change.")
    return {"project": projects.get(project_id)}


@router.delete("/projects/{project_id}")
def delete_project(project_id: str) -> dict[str, Any]:
    """Returns what was detached, because deleting a project is not deleting its contents."""
    return projects.delete(project_id)


# ------------------------------------------------------------------------ search


@router.get("/search")
def search(q: str, entities: str | None = None, limit: int = 30) -> dict[str, Any]:
    if not q.strip():
        raise HTTPException(status_code=400, detail="Give me something to search for.")
    return {
        "results": search_repo.search_resolved(q, entities=_split(entities), limit=limit),
        "counts": search_repo.counts(q),
        "index_size": search_repo.index_size(),
    }


# ---------------------------------------------------------------------- settings


@router.get("/settings")
def get_settings() -> dict[str, Any]:
    """Current values, the defaults behind them, and any orphans.

    Defaults ship alongside the values so the UI can render a real "reset to
    default" affordance instead of a button that guesses.
    """
    return {
        "settings": app_settings.get_all(),
        "defaults": app_settings.DEFAULTS,
        "orphans": app_settings.unknown_keys(),
    }


@router.patch("/settings")
def patch_settings(values: dict[str, Any] = Body(..., embed=True)) -> dict[str, Any]:
    """Write settings, rejecting keys nothing reads.

    Awareness settings are accepted here too, but they will not take effect until
    the supervisor reconciles -- so this route refuses them and points at the one
    that does both. Writing them silently would leave a toggle on with nothing
    watching.
    """
    unknown = sorted(k for k in values if k not in app_settings.DEFAULTS)
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown setting(s): {', '.join(unknown)}.")
    awareness = sorted(k for k in values if k.startswith(("monitor_", "ambient_")))
    if awareness:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{', '.join(awareness)} must go through PATCH /api/monitors/settings, "
                "which also starts or stops the monitors."
            ),
        )
    app_settings.set_many(values)
    return {"ok": True, "settings": app_settings.get_all()}


@router.post("/settings/{key}/reset")
def reset_setting(key: str) -> dict[str, Any]:
    if key not in app_settings.DEFAULTS:
        raise HTTPException(status_code=404, detail=f"There is no setting called '{key}'.")
    return {"ok": True, "key": key, "value": app_settings.reset(key)}


# ------------------------------------------------------------------- maintenance


@router.get("/storage")
def storage() -> dict[str, Any]:
    return maintenance.stats()


@router.post("/storage/export")
def export_data() -> dict[str, Any]:
    """Everything, as JSON, in one response (§70: your data is yours to take).

    Returned as a body rather than written to a file the user then has to find.
    The download is the browser's job; choosing where it lands is theirs.
    """
    return maintenance.export_all()


@router.post("/storage/retention-sweep")
def retention_sweep(dry_run: bool = Body(True, embed=True)) -> dict[str, Any]:
    """Apply the retention settings. Defaults to a dry run.

    A sweep deletes rows, so the default has to be the harmless one: a UI that
    calls this with no body gets a report of what *would* go, not a deletion.
    """
    return maintenance.retention_sweep(dry_run=dry_run)


@router.post("/storage/vacuum")
def vacuum() -> dict[str, Any]:
    return maintenance.vacuum()
