"""In-process event bus (backs every live surface in the UI).

One hub, many subscribers, each with its own bounded queue. Used for streaming
assistant tokens, task step transitions, permission requests, activity entries
and device state -- so the UI never polls for something that already happened.

Two properties matter:

* **A slow subscriber cannot stall a producer.** ``publish`` never awaits a
  consumer. If a queue is full the oldest event is dropped and a ``dropped``
  marker is emitted, so the client learns its view is incomplete instead of
  quietly missing events (§80: no silent lies about state).
* **Publishing is safe from any thread.** Tool execution runs in a threadpool;
  those code paths still need to emit progress. ``publish_threadsafe`` hands the
  work to the loop that owns the subscribers.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from ..logging_setup import get_logger

log = get_logger("events")

QUEUE_LIMIT = 256
REPLAY_LIMIT = 100


@dataclass
class Event:
    type: str
    payload: dict[str, Any]
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "type": self.type, "at": self.at, **self.payload}


class _Subscriber:
    def __init__(self, topics: tuple[str, ...] | None) -> None:
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self.topics = topics
        self.dropped = 0

    def wants(self, event_type: str) -> bool:
        if self.topics is None:
            return True
        return any(
            event_type == topic or event_type.startswith(topic + ".")
            for topic in self.topics
        )

    def offer(self, event: Event) -> None:
        if not self.wants(event.type):
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            self.dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(event)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[_Subscriber] = set()
        self._seq = 0
        self._recent: deque[Event] = deque(maxlen=REPLAY_LIMIT)
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Remember the loop that owns subscribers, for threadsafe publishing."""
        self._loop = loop or asyncio.get_running_loop()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """The loop bound at startup, or None before it is running.

        Exposed because tool handlers run in worker threads and some of them have
        follow-up work that is genuinely async -- embedding a memory that was just
        written, for instance. They need a way to hand that back to the loop, and
        the bus is already the one object that knows which loop that is.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return None
        return loop

    def publish(self, event_type: str, payload: dict[str, Any] | None = None) -> Event:
        self._seq += 1
        event = Event(type=event_type, payload=payload or {}, seq=self._seq)
        self._recent.append(event)
        for subscriber in list(self._subscribers):
            subscriber.offer(event)
        return event

    def publish_threadsafe(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """Publish from a worker thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            # No loop yet (startup, or a unit test): the event still belongs in the
            # replay buffer so a subscriber connecting later sees it.
            self._seq += 1
            self._recent.append(Event(type=event_type, payload=payload or {}, seq=self._seq))
            return
        loop.call_soon_threadsafe(self.publish, event_type, payload or {})

    def replay(self, after_seq: int = 0, topics: tuple[str, ...] | None = None) -> list[Event]:
        """Events still in the buffer newer than ``after_seq``.

        Lets a reconnecting client catch up. The buffer is small and bounded, so
        a client that was away for a long time gets a gap -- and is told about it
        by the ``resumed`` event's ``complete`` flag.
        """
        probe = _Subscriber(topics)
        return [e for e in self._recent if e.seq > after_seq and probe.wants(e.type)]

    def oldest_seq(self) -> int:
        return self._recent[0].seq if self._recent else self._seq

    async def subscribe(
        self,
        topics: tuple[str, ...] | None = None,
        *,
        after_seq: int = 0,
        heartbeat_seconds: float = 15.0,
    ) -> AsyncIterator[Event]:
        """Yield events until the consumer stops iterating."""
        subscriber = _Subscriber(topics)
        self._subscribers.add(subscriber)
        try:
            missed = self.replay(after_seq, topics) if after_seq else []
            complete = after_seq == 0 or after_seq >= self.oldest_seq() - 1
            yield Event(
                type="stream.resumed",
                payload={
                    "replayed": len(missed),
                    "complete": complete,
                    "subscribers": self.subscriber_count,
                },
                seq=self._seq,
            )
            for event in missed:
                yield event

            while True:
                try:
                    event = await asyncio.wait_for(
                        subscriber.queue.get(), timeout=heartbeat_seconds
                    )
                except asyncio.TimeoutError:
                    # A periodic heartbeat keeps proxies from closing an idle
                    # connection, and lets the UI show a live/stale indicator.
                    yield Event(type="stream.heartbeat", payload={}, seq=self._seq)
                    continue

                if subscriber.dropped:
                    dropped, subscriber.dropped = subscriber.dropped, 0
                    yield Event(
                        type="stream.dropped",
                        payload={"count": dropped},
                        seq=self._seq,
                    )
                yield event
        finally:
            self._subscribers.discard(subscriber)


bus = EventBus()
