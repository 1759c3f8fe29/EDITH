"""The live channel (§97, A6).

Everything that happens *while* the user is looking arrives here: permission
prompts, tool progress, monitor observations, streaming tokens, task steps. The
alternative -- polling -- cannot be made honest. A UI that polls every second
will show a permission dialog for up to a second after the request timed out, and
a progress bar that is always slightly behind the work it claims to describe.

Three properties are load-bearing:

* **Sequence numbers, not timestamps.** The client sends back the last ``seq`` it
  saw; the bus replays from there. Reconnecting is therefore lossless within the
  buffer, and *visibly* lossy beyond it -- ``stream.resumed`` carries
  ``complete: false`` so the UI can say "some events were missed" rather than
  silently having a hole in the activity feed.
* **A slow client loses events rather than blocking the core.** The bus drops the
  oldest event for a full queue and counts it, then emits ``stream.dropped``. A
  design that applied backpressure instead would let a stalled UI tab pause a
  running agent.
* **Answering a prompt goes through the same engine call as the HTTP route.**
  There is one implementation of "record this decision", not two that could drift.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..core.events import bus
from ..logging_setup import get_logger
from ..permissions.engine import engine

router = APIRouter(tags=["events"])
log = get_logger("api.ws")

#: What the client may ask the core to do over this socket. Deliberately tiny:
#: the socket exists to carry events *out*, and every state-changing action has an
#: HTTP route with its own status codes. The one exception is answering a
#: permission prompt, which arrived on this channel and is most correctly answered
#: on it -- a dialog that has to open a second connection to reply can fail in
#: ways the user would read as "the button did nothing".
_INBOUND = ("ping", "permission.respond", "permission.cancel", "subscribe")


@router.websocket("/ws/events")
async def events(socket: WebSocket) -> None:
    """Stream events, optionally filtered by topic prefix.

    Query parameters:
      ``topics``    comma-separated prefixes; ``permission`` matches
                    ``permission.requested`` and friends. Omit for everything.
      ``after_seq`` last sequence the client saw, for gapless reconnect.
    """
    await socket.accept()

    raw_topics = socket.query_params.get("topics")
    topics = tuple(t.strip() for t in raw_topics.split(",") if t.strip()) if raw_topics else None
    try:
        after_seq = int(socket.query_params.get("after_seq") or 0)
    except ValueError:
        after_seq = 0

    # Two tasks: one pushing events out, one reading commands in. Neither can be
    # folded into the other -- a single loop awaiting ``receive`` would stall the
    # outbound stream whenever the client is quiet, which is most of the time.
    outbound = asyncio.create_task(_pump(socket, topics, after_seq), name="ws-out")
    inbound = asyncio.create_task(_listen(socket), name="ws-in")

    done, pending = await asyncio.wait({outbound, inbound}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    for task in done:
        exc = task.exception()
        if exc and not isinstance(exc, WebSocketDisconnect):
            log.warning("event socket ended with an error", exc_info=exc)


async def _pump(socket: WebSocket, topics: tuple[str, ...] | None, after_seq: int) -> None:
    """Bus -> client. Ends when the client goes away."""
    async for event in bus.subscribe(topics, after_seq=after_seq):
        try:
            await socket.send_text(json.dumps(event.to_dict(), default=str))
        except (WebSocketDisconnect, RuntimeError):
            # RuntimeError is what Starlette raises for a send on a closed socket.
            return


async def _listen(socket: WebSocket) -> None:
    """Client -> core. Ends when the client goes away."""
    while True:
        try:
            raw = await socket.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            return

        try:
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise ValueError("expected an object")
        except (json.JSONDecodeError, ValueError) as exc:
            await _reply(socket, {"type": "error", "message": f"Could not read that message: {exc}"})
            continue

        kind = str(message.get("type", ""))
        if kind not in _INBOUND:
            await _reply(
                socket,
                {
                    "type": "error",
                    "message": f"'{kind}' is not something this socket accepts.",
                    "detail": f"Accepted: {', '.join(_INBOUND)}. Everything else has an HTTP route.",
                },
            )
            continue

        await _handle(socket, kind, message)


async def _handle(socket: WebSocket, kind: str, message: dict[str, Any]) -> None:
    if kind == "ping":
        # Client-driven liveness, distinct from the bus heartbeat: this one proves
        # the round trip works, not just that the core is emitting.
        await _reply(socket, {"type": "pong", "at": message.get("at")})
        return

    if kind == "subscribe":
        # Not supported mid-stream, and saying so beats ignoring it. Changing the
        # filter means a new subscription with its own replay position, which is a
        # reconnect -- the client should just open a new socket with the topics it
        # wants.
        await _reply(
            socket,
            {
                "type": "error",
                "message": "Topics are fixed for the life of a socket.",
                "detail": "Reconnect with ?topics=... and ?after_seq=<last seq> to change the filter without a gap.",
            },
        )
        return

    request_id = str(message.get("request_id") or "")
    if not request_id:
        await _reply(socket, {"type": "error", "message": "That needs a request_id."})
        return

    if kind == "permission.cancel":
        ok = engine.cancel_pending(request_id)
        await _reply(
            socket,
            {
                "type": "permission.cancel_result",
                "request_id": request_id,
                "ok": ok,
                "message": None if ok else "That request is no longer waiting for an answer.",
            },
        )
        return

    # permission.respond -- the same engine call the HTTP route makes.
    result = engine.respond(
        request_id,
        str(message.get("choice", "")),
        constraints=message.get("constraints"),
    )
    await _reply(socket, {"type": "permission.respond_result", "request_id": request_id, **result})


async def _reply(socket: WebSocket, payload: dict[str, Any]) -> None:
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        await socket.send_text(json.dumps(payload, default=str))
