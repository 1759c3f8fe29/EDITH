"""Server-sent events, once, for every route that streams a turn.

``/api/chat`` and ``/api/voice/converse`` stream the same kind of thing: a per-request
sequence of event dicts produced by a task, drained by a response. The framing, the
sentinel, the heartbeat and the "one JSON object per ``data:`` line" rule are identical
in both, and identical is the problem -- a second private copy of a wire format is a
format that drifts. When one route learns to send a keep-alive comment and the other does
not, the browser closes the second one mid-answer and the bug looks like the model.

So this module owns the wire and the two routes own their events.

The heartbeat is not decoration. A cold NVIDIA endpoint has been measured taking over
100 seconds to produce its first token, and both an intermediary proxy and the browser
itself will decide a silent response is a dead one well before that. A comment line
(``: ...``) keeps it open without the client having to filter a fake event out of the
stream it is parsing.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

#: Put on a queue when the producer is finished. An object rather than ``None`` so an
#: event that failed to build could never be mistaken for the end of the stream.
END: object = object()

#: Seconds of silence before a comment line is written. See the module docstring for why
#: this is measured in "how long a cold model takes" and not in "how long feels right".
HEARTBEAT_SECONDS = 15.0


def frame(event: dict[str, Any]) -> str:
    """One SSE message. ``json.dumps`` never emits a raw newline, so one line is safe."""
    try:
        body = json.dumps(event, default=str)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        body = json.dumps(
            {"type": "notice", "text": f"One event could not be sent to the interface: {exc}"}
        )
    return f"data: {body}\n\n"


async def drain(
    queue: "asyncio.Queue[Any]",
    *,
    first: dict[str, Any] | None = None,
    on_close: "asyncio.Event | None" = None,
) -> AsyncIterator[str]:
    """Yield the queue as SSE text until ``END``, with a heartbeat while it is quiet.

    ``first`` is sent before anything is waited for, which is how a route tells the client
    something it needs before the first token -- the conversation id, typically, which the
    client may not have had when it posted.

    ``on_close`` is set when this generator stops for any reason, including the client
    disconnecting. The producer can watch it to stop work that only exists for a reader
    that is no longer there. It is deliberately *not* a cancellation: a turn that is
    already running should finish and persist, because the answer outlives the tab.
    """
    if first is not None:
        yield frame(first)

    beats = 0
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                beats += 1
                # The count is there so a stuck turn is visible in a raw ``curl``.
                yield f": waiting {beats * int(HEARTBEAT_SECONDS)}s\n\n"
                continue
            if item is END:
                return
            yield frame(item)
    finally:
        if on_close is not None:
            on_close.set()


__all__ = ["END", "HEARTBEAT_SECONDS", "drain", "frame"]
