"""Talking to NEXUS (task A7; spec §57, §58, §60, §65).

This is the route the whole product hangs off. Everything else the core exposes
reads or configures something; this is the one that *runs a turn* -- classify,
route, assemble context, stream tokens, call tools through the permission engine,
verify, persist. :class:`nexus.ai.core.AiCore` does all of that already and yields
plain dicts as it goes. The job here is to get those dicts to a browser without
lying about any of them.

Four decisions, each of which has a wrong version that looks fine until it fails:

* **Server-sent events, not the existing WebSocket.** ``/ws/events`` carries
  *ambient* events -- monitor observations, permission prompts, anything that
  happens whether or not someone asked for it -- and every connected client sees
  all of them. A turn is not ambient: it belongs to the request that started it.
  Multiplexing turns onto the shared socket would need a correlation id on every
  token and would still deliver one window's tokens to another window. SSE gives
  each turn its own lifecycle for free, over plain HTTP, through the same Vite
  proxy the rest of the API uses.

* **The turn runs in a task, not in the response generator.** If the turn ran
  inline, a closed browser tab would throw ``CancelledError`` into the middle of a
  provider call or a half-finished tool, and :meth:`AiCore.stream`'s ``finally``
  -- which is what writes the exchange to the database -- would be unwinding
  during a cancellation. Instead the turn drives a queue and the response drains
  it. Close the tab and the turn finishes, persists, and shows up in the
  conversation when the UI comes back. §71 asks for exactly that: recovery from an
  interrupted request, not loss of the answer.

* **Disconnecting is not cancelling.** They are different intentions and the user
  has a button for the second one (``POST /api/chat/cancel``), which goes through
  the cooperative checkpoint mechanism ``AiCore`` was built with. Treating a
  navigation as a stop would silently abandon work mid-tool.

* **One turn per conversation.** ``AiCore`` keys its active-turn and cancellation
  state by conversation id, so a second concurrent turn on the same conversation
  would overwrite the first one's entry and make ``cancel`` stop the wrong turn.
  That is a 409 with a sentence, not a race.

Attachments are the one place this file makes a security decision. Accepting a
filesystem path from the UI and base64-ing it would be an unpermissioned file read
straight through the engine §65 puts in the way -- so a path is accepted *only*
inside the screenshot directory, which nothing can write to except the
``screen.capture`` tool, which is itself permissioned. Anything else must arrive
as bytes the user personally chose in a file picker.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import StreamingResponse

from .. import sse, turns
from ...ai.core import ai_core
from ...ai import persona
from ...config import settings
from ...logging_setup import get_logger
from ...providers.base import Attachment
from ...router.jobs import BY_JOB

router = APIRouter(prefix="/api/chat", tags=["chat"])
log = get_logger("api.chat")

#: The wire format lives in :mod:`nexus.api.sse` because ``/api/voice/converse``
#: streams the same shape, and two private copies of a wire format is one that drifts.
_END = sse.END

#: Strong references to running turns. Required, not tidiness: ``asyncio`` holds
#: only a weak reference to a task, so a turn whose result nobody awaits can be
#: garbage collected mid-flight -- the tokens stop, the database write in
#: ``stream``'s ``finally`` never happens, and nothing raises.
_TURNS: set[asyncio.Task[None]] = set()

#: Attachment ceilings. Checked before decoding, because learning that 40 MB of
#: base64 was too big *after* holding it in memory is the wrong order.
MAX_ATTACHMENTS = 4
MAX_ATTACHMENT_BYTES = 8_000_000

_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),
)


# --------------------------------------------------------------- attachments


def _sniff(raw: bytes) -> str | None:
    for signature, media_type in _IMAGE_SIGNATURES:
        if raw.startswith(signature):
            return media_type
    return None


def _attachment_from_path(value: str) -> Attachment:
    """Read an image NEXUS itself captured.

    The allow-list is the screenshot directory and nothing else. That directory is
    only ever written by ``tools/screen.py``, which cannot run without a permission
    decision, so a path inside it describes an image the user already agreed to
    let NEXUS take. Any other path would make this route a general-purpose file
    reader with no capability check -- the hole §65 exists to close.
    """
    allowed = settings().screenshot_dir.resolve()
    try:
        path = Path(value).expanduser().resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"That path cannot be read: {exc}") from exc

    if path.parent != allowed:
        raise HTTPException(
            status_code=403,
            detail=(
                "Only images NEXUS captured itself can be attached by path, and "
                f"they live in {allowed}. To attach your own file, choose it in the "
                "file picker so it is sent as data."
            ),
        )
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"{path.name} is no longer there.")

    size = path.stat().st_size
    if size == 0:
        raise HTTPException(status_code=400, detail=f"{path.name} is empty.")
    if size > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"{path.name} is {size // 1_000_000} MB; the limit is "
            f"{MAX_ATTACHMENT_BYTES // 1_000_000} MB.",
        )

    raw = path.read_bytes()
    media_type = _sniff(raw)
    if media_type is None:
        raise HTTPException(
            status_code=400,
            detail=f"{path.name} is not a recognisable image, so no model could look at it.",
        )
    return Attachment(
        kind="image",
        media_type=media_type,
        data=base64.b64encode(raw).decode("ascii"),
        source_path=str(path),
    )


def _attachment_from_data(item: dict[str, Any], index: int) -> Attachment:
    data = str(item.get("data") or "")
    if not data:
        raise HTTPException(
            status_code=400,
            detail=f"Attachment {index + 1} has neither a 'path' nor base64 'data'.",
        )
    # Strip a data: URL prefix rather than rejecting it -- it is what a browser's
    # FileReader.readAsDataURL produces, and refusing the most likely input in
    # favour of a sentence about it is unhelpful when the fix is one split.
    if data.startswith("data:"):
        _, _, data = data.partition(",")

    if len(data) > MAX_ATTACHMENT_BYTES * 4 // 3 + 16:
        raise HTTPException(
            status_code=413,
            detail=f"Attachment {index + 1} is larger than "
            f"{MAX_ATTACHMENT_BYTES // 1_000_000} MB.",
        )
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail=f"Attachment {index + 1} is not valid base64: {exc}"
        ) from exc

    if not raw:
        raise HTTPException(status_code=400, detail=f"Attachment {index + 1} is empty.")
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Attachment {index + 1} is {len(raw) // 1_000_000} MB; the limit is "
            f"{MAX_ATTACHMENT_BYTES // 1_000_000} MB.",
        )

    # The declared type is checked against the bytes, not trusted. A mislabelled
    # image produces a provider 400 that reads as "the model is broken".
    sniffed = _sniff(raw)
    declared = str(item.get("media_type") or "").strip().lower()
    if sniffed is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Attachment {index + 1} is not an image NEXUS can send "
                "(PNG, JPEG, GIF and WebP are understood)."
            ),
        )
    if declared and declared != sniffed:
        log.info("attachment declared %s but is %s; using the real type", declared, sniffed)
    return Attachment(kind="image", media_type=sniffed, data=base64.b64encode(raw).decode("ascii"))


def _attachments(raw: Any) -> list[Attachment]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="'attachments' must be a list.")
    if len(raw) > MAX_ATTACHMENTS:
        raise HTTPException(
            status_code=400,
            detail=f"At most {MAX_ATTACHMENTS} attachments per message; {len(raw)} were sent.",
        )

    built: list[Attachment] = []
    for index, item in enumerate(raw):
        if isinstance(item, str):
            built.append(_attachment_from_path(item))
            continue
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=400,
                detail=f"Attachment {index + 1} must be a path or an object with 'data'.",
            )
        path = item.get("path")
        if path:
            built.append(_attachment_from_path(str(path)))
        else:
            built.append(_attachment_from_data(item, index))
    return built


# ------------------------------------------------------------------ the turn


async def _drive(queue: asyncio.Queue[Any], kwargs: dict[str, Any]) -> None:
    """Run the turn, putting every event on the queue.

    ``put_nowait`` on an unbounded queue, on purpose. The queue is per-turn and a
    turn is bounded by ``MAX_ROUNDS`` and the model's output limit, so it cannot
    grow without end; a bounded queue would mean either dropping tokens on the
    floor or letting a closed browser tab block a running tool call.
    """
    task = asyncio.current_task()
    if task is not None:
        _TURNS.add(task)
        task.add_done_callback(_TURNS.discard)
    try:
        async for event in ai_core().stream(**kwargs):
            queue.put_nowait(event)
    except Exception as exc:  # noqa: BLE001 -- stream() handles its own errors; this is the net
        log.exception("turn driver failed")
        queue.put_nowait(
            {
                "type": "error",
                "error": f"The turn ended unexpectedly: {exc}",
                "kind": "internal",
            }
        )
    finally:
        queue.put_nowait(_END)


def _sse(queue: asyncio.Queue[Any], conversation_id: str) -> AsyncIterator[str]:
    """Drain the turn as server-sent events.

    The ``start`` frame goes first so the UI knows which conversation it is in before any
    token arrives -- it may not have had an id when it posted.
    """
    return sse.drain(queue, first={"type": "start", "conversation_id": conversation_id})


@router.post("")
async def chat(
    message: str = Body(..., embed=True),
    conversation_id: str | None = Body(None, embed=True),
    project_id: str | None = Body(None, embed=True),
    task_id: str | None = Body(None, embed=True),
    mode: str | None = Body(None, embed=True),
    job: str | None = Body(None, embed=True),
    attachments: Any = Body(None, embed=True),
) -> StreamingResponse:
    """Run one turn and stream it.

    Event types are ``AiCore``'s own, unchanged: ``routing``, ``context``,
    ``text``, ``reasoning``, ``tool_call``, ``tool_result``, ``notice``, ``error``,
    ``done`` -- plus a ``start`` this route adds to name the conversation.
    """
    text = message.strip()
    files = _attachments(attachments)
    if not text and not files:
        raise HTTPException(
            status_code=400,
            detail="There is nothing to answer: send a message, an attachment, or both.",
        )

    if mode is not None and mode not in persona.MODES:
        raise HTTPException(
            status_code=400,
            detail=f"'{mode}' is not a response mode. Choose one of: "
            + ", ".join(sorted(persona.MODES)),
        )
    if job is not None and job not in BY_JOB:
        raise HTTPException(
            status_code=400,
            detail=f"'{job}' is not a job NEXUS routes. Known jobs: " + ", ".join(sorted(BY_JOB)),
        )

    conversation_id = turns.resolve_conversation(
        conversation_id, first_message=text or "Image", project_id=project_id
    )
    turns.refuse_if_busy(conversation_id)

    queue: asyncio.Queue[Any] = asyncio.Queue()
    asyncio.create_task(
        _drive(
            queue,
            {
                "request": text,
                "conversation_id": conversation_id,
                "project_id": project_id,
                "task_id": task_id,
                "attachments": files,
                "mode": mode,
                "job": job,
            },
        ),
        name=f"turn:{conversation_id}",
    )

    return StreamingResponse(
        _sse(queue, conversation_id),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache, no-transform",
            "connection": "keep-alive",
            # nginx and friends buffer streamed responses by default, which turns a
            # token stream into one block at the end. Harmless when absent.
            "x-accel-buffering": "no",
            # So a client that posted without an id can read it from the headers
            # too, not only from the first event.
            "x-nexus-conversation": conversation_id,
        },
    )


@router.post("/cancel")
def cancel(conversation_id: str = Body(..., embed=True)) -> dict[str, Any]:
    """Stop the turn running on a conversation, at its next checkpoint.

    Returns ``stopped: false`` when there was nothing running rather than
    pretending to have stopped something -- the UI needs to be able to say "that
    had already finished" instead of leaving a stop button that appears to do
    nothing. The turn itself reports what had already run and was *not* undone.
    """
    stopping = ai_core().cancel(conversation_id)
    return {
        "ok": True,
        "stopped": stopping,
        "message": (
            "Stopping at the next safe point."
            if stopping
            else "Nothing is running on that conversation."
        ),
    }


@router.get("/active")
def active() -> dict[str, Any]:
    """Turns running right now, for a UI that has just connected or reloaded.

    Without this, a reload during a turn leaves the interface unable to tell
    "thinking" from "idle", which is the indefinite THINKING state §71 forbids.
    """
    turns = ai_core().active()
    return {"active": turns, "count": len(turns)}


__all__ = ["router"]
