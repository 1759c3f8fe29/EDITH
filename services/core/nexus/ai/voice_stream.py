"""Speaking while still thinking: one turn, streamed end to end (§12, §37, §94).

Three things already stream on their own and were never joined up. The model streams
tokens (:meth:`nexus.ai.core.AiCore.stream`). The segmenter says where it is safe
to cut (:mod:`nexus.ai.speech_stream`). Synthesis is a request that takes about as long
as the audio it produces. Waiting for the last token before the first request is what
makes an assistant feel slow, and it is entirely avoidable: by the time the model has
written its second sentence, the first one can already be playing.

So this module is the join, and it is mostly about two orderings.

**Synthesis overlaps generation.** Segments go onto a queue as the segmenter finds them
and a worker synthesises them while the model keeps writing. On a long answer the first
audio is ready after one sentence instead of after the whole thing.

**Audio is emitted in order, always.** The worker synthesises one segment at a time
rather than fanning out. Two concurrent requests finish in whichever order the provider
feels like, and audio played out of order is not a slower answer -- it is a wrong one.
Sequential synthesis still overlaps generation, which is where the latency actually
went, so the concurrency that would buy the rest is not worth the reordering buffer it
would need.

Everything else here is about not lying when a part fails:

* **No TTS model configured is not a failed turn.** The text still streams and one
  ``notice`` says the answer is on screen and not out loud. Refusing the whole turn
  because the voice is unavailable would be losing the answer to protect the delivery.
* **A segment that fails to synthesise is reported, not skipped.** Silence where a
  sentence should have been is indistinguishable from the assistant having nothing to
  say, so the failure is an event with the segment's index and the provider's reason.
* **Cancellation stops synthesis too.** §21 and §79: stop means stop. A cancelled turn
  that keeps paying for audio nobody will hear is not stopped.
* **Code is described, not read.** That decision lives in the segmenter; this module
  only notices that a code segment's spoken form differs from its source and sends both,
  so the transcript can show the code the voice declined to recite.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from ..db.repos import app_settings
from ..logging_setup import get_logger
from ..providers import speech as speech_provider
from ..providers.base import ProviderError
from ..router import NoModelAvailable, resolve as resolve_job
from ..tools.language_detect import detect_language_from_text, get_tts_language
from . import speech_stream
from .core import ai_core
from ..core.events import bus

log = get_logger("ai.voice_stream")

#: How many finished segments may wait for synthesis before generation is left to run
#: ahead unqueued. Not a performance knob: it bounds how much audio has been paid for
#: and not yet heard when the user presses stop.
QUEUE_LIMIT = 8

#: Sentinel closing the queue.
#:
#: A dedicated object rather than ``None``, and the reason is not the one that used to be
#: written here. It said an empty segment could otherwise be mistaken for the end of the
#: stream, which cannot happen: the queue carries ``(index, segment)`` tuples one way and
#: :class:`Spoken` instances the other, so ``None`` is not a value either side can produce.
#: ``_DONE = None`` was measured against the tests and survived -- it is an equivalent
#: change today. It stays an ``object()`` because a sentinel that cannot collide with any
#: value is worth nothing to maintain, not because a collision is currently possible.
_DONE = object()


@dataclass(frozen=True)
class Spoken:
    """One synthesised segment, or the reason there is no audio for it."""

    index: int
    text: str
    kind: str
    path: str | None = None
    url: str | None = None
    error: str | None = None

    def to_event(self) -> dict[str, Any]:
        if self.error:
            return {
                "type": "speech_error",
                "index": self.index,
                "text": self.text,
                "error": self.error,
            }
        return {
            "type": "speech",
            "index": self.index,
            "text": self.text,
            "kind": self.kind,
            "path": self.path,
            "url": self.url,
        }


def _tts_model() -> tuple[Any | None, str]:
    """The routed TTS model, or ``None`` and the reason there is not one.

    Resolved once per turn rather than per segment: the router's answer cannot change
    mid-answer, and asking it twelve times would put twelve identical rows in the
    routing log for one turn.
    """
    try:
        return resolve_job("tts"), ""
    except NoModelAvailable as exc:
        return None, str(exc)


def _voice(requested: str | None, provider_id: str, text: str = "") -> str | None:
    """The caller's voice, else the ``tts_voice`` setting, else the provider's own.

    In that order, so choosing a voice in Settings actually changes what is heard --
    the same precedence ``/api/voice/speak`` uses, because two paths that pick a voice
    differently is a bug report nobody can reproduce. Resolved through
    :func:`nexus.providers.speech.voice_for` rather than read here, because it is the
    one place that knows a stored voice belonging to a *different* provider must be
    dropped rather than sent: a user who picked ``en_paul_neutral`` while Mistral was
    serving speech would otherwise have every NVIDIA synthesis refused with "not a
    voice Magpie has", after a routing change they never made.

    ``None`` out rather than ``""`` so the provider picks its own default, which is
    the only one that cannot be rejected.
    """
    return speech_provider.voice_for(
        provider_id, requested, str(app_settings.get("tts_voice", "") or "")
    )


def _tts_language(text: str, requested: str | None = None) -> str | None:
    """Get the TTS language, auto-detecting from text if not provided."""
    language = requested
    if not language:
        # Try output_language setting
        language = str(app_settings.get("output_language", "") or "").strip()
    if not language and text:
        # Auto-detect from text
        language = detect_language_from_text(text)
    # Convert to best TTS language
    if language:
        language = get_tts_language(language)
    return language


async def synthesize_one(
    text: str,
    *,
    provider_id: str,
    model: str,
    voice: str | None,
    language: str | None,
) -> tuple[str | None, str | None]:
    """One synthesis call. Returns ``(path, error)`` -- never raises.

    The provider named by the routing decision is the one called, through
    :mod:`nexus.providers.speech` -- the one door. Swallowing the exception here is
    deliberate and is the opposite of hiding it: the caller turns the error into an
    event with the segment's index, so a sentence that could not be spoken is
    *named*. A raise would take the rest of the answer's audio down with it, which is
    a worse outcome from the same information.
    """
    try:
        speech = await speech_provider.synthesize(
            text, provider_id=provider_id, model=model, voice=voice, language=language
        )
    except ProviderError as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 -- a broken provider must not kill the turn
        log.exception("synthesis failed for a segment")
        return None, f"{type(exc).__name__}: {exc}"
    body = speech.to_dict()
    return str(body.get("path") or "") or None, None


async def _synthesiser(
    queue: "asyncio.Queue[Any]",
    out: "asyncio.Queue[Spoken | object]",
    *,
    provider_id: str,
    model: str,
    voice: str | None,
    language: str | None,
) -> None:
    """Drain segments, synthesise them one at a time, put results on ``out`` in order.

    The blanket ``except`` at the bottom is not defensive padding. The caller waits on
    ``out`` for one result per segment and then for ``_DONE``; a worker that dies without
    putting ``_DONE`` there leaves it waiting forever, so an unexpected exception in here
    would turn a missing sentence into a hung turn -- text and all. Closing the queue on
    the way out, and saying why, is what keeps the failure proportional to itself.
    """
    try:
        while True:
            item = await queue.get()
            if item is _DONE:
                await out.put(_DONE)
                return
            index, segment = item
            path, error = await synthesize_one(
                segment.spoken, provider_id=provider_id, model=model, voice=voice, language=language
            )
            name = Path(path).name if path else ""
            await out.put(
                Spoken(
                    index=index,
                    text=segment.spoken,
                    kind=segment.kind,
                    path=path,
                    url=f"/api/voice/audio/{name}" if name else None,
                    error=error,
                )
            )
    except asyncio.CancelledError:
        # A cancelled turn is not a broken one, and nobody is reading ``out`` any more.
        raise
    except BaseException as exc:  # noqa: BLE001 -- see the docstring
        log.exception("the synthesis worker died")
        await out.put(
            Spoken(
                index=-1,
                text="",
                kind="prose",
                error=f"the voice pipeline stopped: {type(exc).__name__}: {exc}",
            )
        )
        await out.put(_DONE)


async def stream(
    request: str,
    *,
    conversation_id: str,
    project_id: str | None = None,
    voice: str | None = None,
    speak: bool = True,
    mode: str | None = None,
    forced_model: str | None = None,  # Direct model key to bypass router
    stop_speaking: "asyncio.Event | None" = None,
) -> AsyncIterator[dict[str, Any]]:
    """One turn, yielding the model's events and audio for each sentence as it lands.

    The event types are :meth:`core.AiCore.stream`'s, unchanged and passed
    through, plus three of ours:

    ``segment``
        A cut has been made and this text is about to be synthesised. Sent before the
        audio exists so the UI can show what is queued rather than a spinner.
    ``speech``
        Audio for segment ``index`` is ready, with a ``path`` for the desktop shell and
        a ``url`` for the web view. Emitted in segment order.
    ``speech_error``
        Segment ``index`` has no audio, and why. The turn continues.

    ``speak=False`` runs the same path with synthesis off, which is what the text-only
    UI wants and is not the same thing as a turn with no voice available -- one is a
    choice and the other is a limitation, and they produce different events.

    ``stop_speaking`` stops the *voice* without stopping the turn. Set it when the reader
    has gone -- a closed tab, a dropped connection -- and generation carries on to its end
    so the exchange is still written to the conversation, while nothing further is sent for
    synthesis. The two halves are separable because the reasons to end them differ: an
    answer outlives the window that asked for it, and audio nobody is listening to does
    not.
    """
    segmenter = speech_stream.Segmenter()
    decision, reason = _tts_model() if speak else (None, "")
    # The voice is resolved against the routed provider, so a stored preference from
    # the other one is dropped here rather than sent and refused per segment.
    # Get TTS language (auto-detect from request text if not specified)
    tts_language = _tts_language(request, voice) if speak and decision is not None else None
    chosen = (
        _voice(voice, decision.provider_id, request) if speak and decision is not None else None
    )

    if speak and decision is None:
        # The answer is not withheld because the voice is missing. One notice, then text.
        yield {
            "type": "notice",
            "text": f"Answering on screen only: {reason}",
            "kind": "tts_unavailable",
        }

    # An event that is already set at the door means the reader went away before the turn
    # started -- a retried request whose first attempt is still finishing, typically. No
    # worker at all then, rather than one started and immediately cancelled.
    hushed = stop_speaking is not None and stop_speaking.is_set()
    # ``speak and`` is redundant as this function stands, and deliberately kept. The line
    # above resolves no model when ``speak`` is false, so ``decision is not None`` already
    # carries the answer -- deleting the conjunct changed nothing under mutation. It stays
    # because the redundancy is between two lines that could reasonably be edited apart:
    # the day someone resolves the model unconditionally, to log the routing decision or
    # to show the voice in the UI before a turn, this conjunct is the only thing left
    # stopping a silent turn from synthesising. There is no test for it because the state
    # it guards cannot currently be constructed, and a test that fakes an impossible state
    # to assert a guard against it would be pinning the fake, not the guard.
    live = speak and decision is not None and not hushed
    inbox: asyncio.Queue[Any] = asyncio.Queue(maxsize=QUEUE_LIMIT)
    results: asyncio.Queue[Any] = asyncio.Queue()
    worker: asyncio.Task[None] | None = None
    if live:
        # Publish voice.speaking start event for attention model
        await bus.publish("voice.speaking", {"speaking": True})
        yield {"type": "voice", "routed": decision.key, "model": decision.model_id, "voice": chosen, "language": tts_language}
        worker = asyncio.create_task(
            _synthesiser(
                inbox,
                results,
                provider_id=decision.provider_id,
                model=decision.model_id,
                voice=chosen,
                language=tts_language,
            )
        )

    index = 0

    def speaking() -> bool:
        """Whether there is still a live worker to send segments to.

        Two ways for this to be false: synthesis was never on, or the worker is finished --
        because it died, or because :func:`hush` cancelled it. Both matter because ``inbox``
        is bounded: queueing into a queue nobody drains blocks the loop that yields the
        model's text, so a dead voice would stop the *answer*.

        The stop event is deliberately not re-checked here. :func:`hush` is the one thing
        that acts on it, and it acts by ending the worker -- so ``worker.done()`` already
        carries the answer, and a second reading of the flag would be a branch no test
        could tell from its absence.
        """
        return live and worker is not None and not worker.done()

    def hush() -> None:
        """Stop the voice now. Cancelling the worker is the whole of it.

        Not merely "queue nothing further": the point of the stop is audio nobody will
        hear, and a worker left to chew through an eight-sentence backlog is exactly
        that. But the backlog needs no separate disposal, and this used to drain ``inbox``
        by hand -- a loop that could not be observed to run. The worker is the only thing
        that ever reads that queue, so once it is cancelled the queued segments are
        already unreachable; emptying it changes what the object holds and not what the
        turn does. It read as protection while protecting nothing, which is the same
        mistake as a splitting loop guarding a length that cannot occur, so it is gone.

        Anything already synthesised is still reported; it is paid for, and the file
        exists whether or not it is mentioned.
        """
        if worker is not None and not worker.done():
            worker.cancel()

    def drain_ready() -> list[dict[str, Any]]:
        """Whatever audio has finished while we were waiting on the model."""
        out: list[dict[str, Any]] = []
        while True:
            try:
                item = results.get_nowait()
            except asyncio.QueueEmpty:
                return out
            if item is _DONE:
                return out
            out.append(item.to_event())

    async def offer(segments: list[speech_stream.Segment]) -> list[dict[str, Any]]:
        nonlocal index
        events: list[dict[str, Any]] = []
        for segment in segments:
            events.append(
                {
                    "type": "segment",
                    "index": index,
                    "text": segment.spoken,
                    "kind": segment.kind,
                    # Only when it differs: sending the source for every prose segment
                    # would double the payload to say the same thing twice.
                    "source": segment.source if segment.is_code else None,
                }
            )
            if speaking():
                await inbox.put((index, segment))
            index += 1
        return events

    # ``done`` is held back rather than passed straight through. The last sentence of an
    # answer is only safe to cut once the stream has ended, and its audio lands later
    # still -- so a client that treats ``done`` as terminal, which is what ``done`` means,
    # would drop the final segment and never play it. Emitting it last costs nothing and
    # makes the contract true: after ``done`` there is nothing more.
    final: dict[str, Any] | None = None

    try:
        async for event in ai_core().stream(
            request, conversation_id=conversation_id, project_id=project_id, mode=mode
        ):
            if stop_speaking is not None and stop_speaking.is_set():
                hush()
            if event.get("type") == "done":
                final = event
                continue
            yield event
            if event.get("type") == "text":
                for produced in await offer(segmenter.feed(str(event.get("text") or ""))):
                    yield produced
                for ready in drain_ready():
                    yield ready
            elif event.get("type") in {"error", "notice"}:
                # Spoken, not just printed: "I could not reach the model" is exactly the
                # kind of thing a voice-first assistant has to say out loud, and a turn
                # that fails silently is the failure the user cannot even report.
                #
                # Only events from the model's stream reach here. Our own
                # ``tts_unavailable`` notice is emitted above, before this loop, so it
                # cannot be fed to a voice whose absence it is reporting -- a filter for
                # it here would be a guard against a path that does not exist.
                text = str(event.get("text") or event.get("error") or "")
                if text:
                    for produced in await offer(speech_stream.segments_of(text)):
                        yield produced

        for produced in await offer(segmenter.flush()):
            yield produced

        if speaking() and worker is not None:
            await inbox.put(_DONE)
            while not (worker.done() and results.empty()):
                # Checked here as well as in the generation loop, because this is where a
                # stop usually lands: the model has finished, the reader has gone, and the
                # only thing still running is the tail of the audio they left behind.
                if stop_speaking is not None and stop_speaking.is_set():
                    hush()
                    break
                item = await results.get()
                if item is _DONE:
                    break
                yield item.to_event()

        # Outside the guard on purpose. A worker that died, or one that was told to stop
        # speaking, has still put results on the queue -- including the report of its own
        # death -- and skipping this because there is no longer a voice would swallow the
        # explanation for why there is no longer a voice.
        for ready in drain_ready():
            yield ready

        if final is not None:
            yield final
    finally:
        if worker is not None and not worker.done():
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        # Publish voice.speaking end event for attention model
        if live:
            await bus.publish("voice.speaking", {"speaking": False})
