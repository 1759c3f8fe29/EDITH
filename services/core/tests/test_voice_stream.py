"""Speaking while still thinking: the pipeline, with the model and provider stubbed.

The claim this module makes is a latency claim -- audio for the first sentence exists
before the model has written the last one -- and a latency claim is exactly the kind
that a happy-path test will confirm while being false. So the central test here asserts
on **event order**: a ``speech`` event for segment 0 must appear before later ``text``
events, which can only happen if synthesis really did overlap generation.

The rest is about failure not being silence. A voice-first assistant that goes quiet
has told the user nothing, and there are three separate ways for that to happen -- no
TTS model configured, one segment failing to synthesise, and a turn cancelled mid-answer
-- which are three different events here rather than three silences.

Stubbed: the model stream and the synthesis call. Real: the segmenter, the queueing, the
ordering, the event shapes.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest

from nexus.ai import voice_stream
from nexus.providers.base import ProviderError


class Decision:
    """Stands in for the router's answer. The fields the module reads, nothing else.

    ``provider_id`` is what selects the door's provider, so the stub carries it
    explicitly -- the default is ``nvidia`` rather than mistral because the
    interesting claim is that *the decision's* provider is served, and using the
    non-default one makes a stub that ignores ``provider_id`` fail the assertion
    rather than pass by coincidence.
    """

    def __init__(
        self, key: str = "nvidia:nvidia/magpie-tts-multilingual",
        model_id: str = "magpie-tts", provider: str = "nvidia",
    ) -> None:
        self.key = key
        self.model_id = model_id
        self.provider_id = provider


class Synth:
    """Records every synthesis call, and how many were in flight at once.

    ``peak`` is the assertion that matters: two concurrent requests finish in whichever
    order the provider likes, and audio played out of order is not a slower answer, it is
    a wrong one.

    Called with the same keyword shape as ``providers.speech.synthesize``, and records
    ``provider_id`` so the tests can assert the routed provider is the one that served.
    """

    def __init__(self, *, fail_on: set[str] | None = None, delay: float = 0.0) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_on = fail_on or set()
        self.delay = delay
        self.in_flight = 0
        self.peak = 0

    def client(self) -> "Synth":
        return self

    async def synthesize(
        self, text: str, *, provider_id: str, model: str, voice: str | None
    ) -> Any:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            self.calls.append(
                {"text": text, "voice": voice, "model": model, "provider_id": provider_id}
            )
            if self.delay:
                await asyncio.sleep(self.delay)
            else:
                await asyncio.sleep(0)
            if text in self.fail_on:
                raise ProviderError("the voice service refused this sentence")
            n = len(self.calls)
            return _Speech(f"/tmp/nexus-audio/seg{n}.wav")
        finally:
            self.in_flight -= 1


class _Speech:
    def __init__(self, path: str) -> None:
        self._path = path

    def to_dict(self) -> dict[str, Any]:
        return {"path": self._path, "ms": 100}


def fake_core(chunks: list[str], *, trailing: list[dict[str, Any]] | None = None) -> Any:
    """A stand-in for ``ai_core()`` whose stream yields ``text`` events, then ``done``."""

    class Core:
        async def stream(self, request: str, **kw: Any) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "routing", "model": "mistral-large-latest"}
            for chunk in chunks:
                yield {"type": "text", "text": chunk}
                # Yield the loop so the synthesiser task can actually run. Without this
                # the whole model stream completes before any worker gets scheduled, and
                # the overlap the module claims would be untestable rather than absent.
                await asyncio.sleep(0)
            for event in trailing or []:
                yield event
                await asyncio.sleep(0)
            yield {"type": "done", "status": "complete"}

    return Core()


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    """Patch the three seams: the router, the one speech door, and the model.

    ``nexus.providers.speech`` is patched rather than a specific provider because the
    module under test no longer names one -- it calls whichever provider the routing
    decision named, and that indirection is exactly the behaviour under test. The
    stub records ``provider_id`` on every synthesis call so a test can prove the
    decision's provider is the one that served the audio.
    """

    def wire(chunks: list[str], **kw: Any) -> Synth:
        synth = kw.pop("synth", None) or Synth()
        core = fake_core(chunks, trailing=kw.pop("trailing", None))
        monkeypatch.setattr(voice_stream, "ai_core", lambda: core)
        monkeypatch.setattr(voice_stream, "speech_provider", _Door(synth))
        monkeypatch.setattr(
            voice_stream, "resolve_job", lambda job: Decision(provider="nvidia")
        )
        return synth

    return wire


class _Door:
    """``nexus.providers.speech`` as this module uses it: one ``synthesize`` function.

    A module-shaped stand-in rather than a provider one, because that is the seam the
    module under test now calls. Everything the real door does before dispatch --
    resolving the provider, raising on an unknown id -- is real code with its own
    tests; what needs stubbing here is only "a call comes out the other end".
    """

    def __init__(self, synth: Synth) -> None:
        self._synth = synth

    async def synthesize(
        self, text: str, *, provider_id: str, model: str, voice: str | None
    ) -> Any:
        return await self._synth.synthesize(
            text, provider_id=provider_id, model=model, voice=voice
        )

    def voice_for(self, provider_id: str, requested: str | None, configured: str | None) -> str | None:
        """The door's voice precedence, which ``stream`` delegates to rather than re-implementing.

        The stub mirrors the real caller-then-setting terms so the precedence tests
        still exercise a real decision. The provider-crossing rule -- a stored voice
        from *another* provider is dropped -- is the real door's own logic and is
        tested against the door itself in ``test_speech_door.py``; asserting it
        through this stub would be pinning the fake.
        """
        chosen = (requested or "").strip()
        if chosen:
            return chosen
        return (configured or "").strip() or None


async def collect(**kw: Any) -> list[dict[str, Any]]:
    return [event async for event in voice_stream.stream(**kw)]


def run(**kw: Any) -> list[dict[str, Any]]:
    kw.setdefault("conversation_id", "c1")
    kw.setdefault("request", "how is the disk")
    return asyncio.run(collect(**kw))


def types(events: list[dict[str, Any]]) -> list[str]:
    return [str(e.get("type")) for e in events]


# ------------------------------------------------------------------ the latency claim


def test_audio_arrives_before_the_answer_is_finished(wired: Any) -> None:
    """The reason this module exists, asserted as event order.

    A ``speech`` event for segment 0 sitting before later ``text`` events is only
    possible if synthesis ran while the model was still writing. If the pipeline waited
    for the stream to end, every ``speech`` would be at the tail -- which is exactly what
    the old ``/speak``-after-the-fact path did, and it is what this test forbids.
    """
    synth = wired(
        [
            "The disk is fine. ",
            "It is at seventy one per cent, ",
            "which leaves about ninety gigabytes free, ",
            "and the backup finished at four this morning without errors. ",
            "Nothing needs doing.",
        ]
    )
    events = run()
    kinds = types(events)

    first_speech = kinds.index("speech")
    last_text = max(i for i, k in enumerate(kinds) if k == "text")
    assert first_speech < last_text, kinds
    assert synth.calls[0]["text"] == "The disk is fine."


def test_every_segment_is_offered_before_its_audio(wired: Any) -> None:
    """``segment`` precedes its own ``speech``, so the UI can show what is queued."""
    wired(["Ready. ", "The long part of the answer follows here, at some length. "])
    events = run()
    for event in events:
        if event.get("type") == "speech":
            index = event["index"]
            positions = [
                i
                for i, e in enumerate(events)
                if e.get("type") == "segment" and e.get("index") == index
            ]
            assert positions, f"no segment event for index {index}"
            assert positions[0] < events.index(event)


# -------------------------------------------------------------------------- ordering


def test_speech_events_are_in_segment_order(wired: Any) -> None:
    wired(
        [
            "One. ",
            "Two, and this one is long enough to be a segment of its own without help. ",
            "Three, likewise long enough that the accumulator lets it go on its own. ",
            "Four.",
        ]
    )
    events = run()
    indices = [e["index"] for e in events if e.get("type") == "speech"]
    assert indices == sorted(indices)
    assert indices == list(range(len(indices)))


def test_synthesis_is_never_concurrent(wired: Any) -> None:
    """One at a time, on purpose. Two in flight finish in the provider's order.

    ``delay`` makes overlap possible if the code allowed it: without the sequential
    worker, several segments would be in flight during the sleep and ``peak`` would
    exceed one.
    """
    synth = wired(
        [
            "First sentence here. ",
            "Second sentence, long enough to stand on its own as a segment please. ",
            "Third sentence, also long enough to stand on its own as a segment now. ",
        ],
        synth=Synth(delay=0.01),
    )
    run()
    # Two calls, not three: the segmenter accumulates short later segments rather than
    # dribbling them, so sentences two and three go out together. Two is enough for the
    # assertion that matters -- with a fan-out worker both would be in flight during the
    # sleep and ``peak`` would be 2.
    assert len(synth.calls) >= 2
    assert synth.peak == 1


# ----------------------------------------------------------------- failure is not silence


def test_a_segment_that_cannot_be_spoken_is_named(wired: Any) -> None:
    """Silence where a sentence should be is indistinguishable from having nothing to say."""
    bad = "The disk is failing."
    synth = wired(
        [
            f"{bad} ",
            "You should back up the photos directory before anything else happens here. ",
        ],
        synth=Synth(fail_on={bad}),
    )
    events = run()

    errors = [e for e in events if e.get("type") == "speech_error"]
    assert len(errors) == 1
    assert errors[0]["index"] == 0
    assert errors[0]["text"] == bad
    assert "refused" in errors[0]["error"]
    # And the rest of the answer was still spoken -- one bad sentence is not a mute turn.
    assert [e for e in events if e.get("type") == "speech"]
    assert len(synth.calls) >= 2


def test_no_tts_model_is_a_notice_and_not_a_failed_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Losing the answer to protect the delivery would be the wrong trade."""
    from nexus.router import NoModelAvailable

    core = fake_core(["The disk is fine. ", "Nothing needs doing."])
    monkeypatch.setattr(voice_stream, "ai_core", lambda: core)

    def refuse(job: str) -> Any:
        raise NoModelAvailable("No TTS model is configured.")

    monkeypatch.setattr(voice_stream, "resolve_job", refuse)
    events = run()
    kinds = types(events)

    assert "speech" not in kinds
    assert "voice" not in kinds
    notices = [e for e in events if e.get("type") == "notice"]
    assert len(notices) == 1
    assert notices[0]["kind"] == "tts_unavailable"
    assert "No TTS model" in notices[0]["text"]
    # The answer itself still streamed. That is the whole point of the notice.
    assert "text" in kinds
    assert kinds[-1] == "done"


def test_speak_false_is_not_the_same_as_no_voice_available(wired: Any) -> None:
    """A choice and a limitation produce different events, because they are different.

    Text-only by request says nothing about the machine; text-only because nothing can
    speak is a thing the user may want to fix. Collapsing them into the same event would
    make the second one unreportable.
    """
    synth = wired(["Ready. ", "And the rest of it."])
    events = run(speak=False)
    kinds = types(events)

    assert synth.calls == []
    assert "speech" not in kinds
    assert "voice" not in kinds
    # No notice either: nothing is wrong, so there is nothing to report.
    assert [e for e in events if e.get("kind") == "tts_unavailable"] == []
    # Segments are still offered, so a transcript can show sentence structure.
    assert "segment" in kinds


def test_an_error_from_the_model_is_spoken(wired: Any) -> None:
    """"I could not reach the model" is exactly what a voice assistant must say aloud."""
    synth = wired(
        [],
        trailing=[{"type": "error", "error": "No model is reachable right now.", "kind": "no_model"}],
    )
    events = run()
    assert any(e.get("type") == "error" for e in events)
    assert synth.calls, "the failure was never given to the voice"
    assert "No model is reachable" in synth.calls[0]["text"]


def test_a_notice_from_the_model_is_spoken_too(wired: Any) -> None:
    """A notice is not a footnote. "I could not reach the model" has to be audible.

    This replaces a test that asserted the ``tts_unavailable`` notice is not itself
    spoken. That one could not fail: the notice is emitted before the loop that feeds the
    voice, so it never reaches it whatever the code says. A test that passes under every
    mutation is not coverage, it is decoration.
    """
    synth = wired(
        [],
        trailing=[{"type": "notice", "text": "The screen recorder is not installed.", "kind": "tool"}],
    )
    events = run()
    assert any(e.get("type") == "notice" for e in events)
    assert synth.calls, "the notice was printed but never spoken"
    assert "screen recorder is not installed" in synth.calls[0]["text"]


# ---------------------------------------------------------------------------- cleanup


def test_leaving_the_stream_early_cancels_synthesis(wired: Any) -> None:
    """§21, §79: stop means stop. A cancelled turn must stop paying for audio.

    Asserted by what happens *after* the generator is closed: the queue still holds
    segments, so a worker that was not cancelled would keep calling the provider during
    the sleep below. Counting calls before and after the close is the only way to tell
    a stopped worker from one the event loop simply had not scheduled yet.
    """
    synth = wired(
        ["One sentence. "] + ["More text that keeps arriving for a while yet. "] * 20,
        synth=Synth(delay=0.02),
    )

    async def bail() -> tuple[int, int]:
        gen = voice_stream.stream("how is the disk", conversation_id="c1", speak=True)
        seen = 0
        async for event in gen:
            seen += 1
            if event.get("type") == "speech":
                break
        await gen.aclose()
        during = len(synth.calls)
        await asyncio.sleep(0.15)
        return during, len(synth.calls)

    during, after = asyncio.run(bail())
    assert during > 0
    assert after == during, "synthesis carried on after the turn was abandoned"
    assert synth.in_flight == 0


def test_done_is_the_last_event(wired: Any) -> None:
    """After ``done`` there is nothing more -- including the last sentence's audio.

    The final segment can only be cut once the stream has ended, so it and its audio are
    produced after the model has finished. Passing ``done`` straight through would put
    them behind a terminal event, and a client that stops reading at ``done`` -- which is
    what ``done`` is for -- would silently lose the end of every answer.
    """
    wired(["The disk is fine. ", "Nothing needs doing at all right now, none of it."])
    events = run()
    kinds = types(events)
    assert kinds[-1] == "done"
    assert kinds.count("done") == 1
    speech = [i for i, k in enumerate(kinds) if k in {"speech", "speech_error", "segment"}]
    assert max(speech) < len(kinds) - 1


def test_a_code_block_is_described_not_read(wired: Any) -> None:
    synth = wired(["Here is the fix.\n\n```python\ndef f():\n    return 1\n```\n\nDone.\n"])
    events = run()
    code = [e for e in events if e.get("type") == "segment" and e.get("kind") == "code"]
    assert len(code) == 1
    assert "def f():" in code[0]["source"]
    assert "def f():" not in code[0]["text"]
    assert not any("def f():" in call["text"] for call in synth.calls)


def test_the_voice_event_names_what_is_speaking(wired: Any) -> None:
    """§26: routing must be visible. A wrong-sounding voice needs a model name to blame."""
    wired(["Fine."])
    events = run(voice="en_ava_bright")
    voice = [e for e in events if e.get("type") == "voice"]
    assert len(voice) == 1
    assert voice[0]["routed"] == "nvidia:nvidia/magpie-tts-multilingual"
    assert voice[0]["voice"] == "en_ava_bright"


def test_a_dead_voice_does_not_hang_the_answer(
    wired: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one failure worse than a missing sentence is a turn that never ends.

    ``synthesize_one`` catches provider errors, so reaching this state takes a bug rather
    than a bad network -- which is exactly why it is worth a test: the queues make it a
    *deadlock* rather than a traceback. The worker holds the only handle on the results
    queue and the text loop blocks on the bounded inbox once it fills, so a worker that
    died would stop the answer as well as the audio.

    The ``wait_for`` is the assertion. A regression here does not fail, it hangs, and a
    hanging test suite is a much worse signal than a failing one.
    """
    synth = wired(["Sentence number %d, long enough to be its own segment. " % i for i in range(30)])

    async def explode(text: str, *, provider_id: str, model: str, voice: str | None) -> Any:
        raise RuntimeError("the audio directory is gone")

    monkeypatch.setattr(voice_stream, "synthesize_one", explode)

    async def go() -> list[dict[str, Any]]:
        return await asyncio.wait_for(
            collect(request="how is the disk", conversation_id="c1"), timeout=5.0
        )

    events = asyncio.run(go())
    kinds = types(events)

    assert kinds[-1] == "done"
    errors = [e for e in events if e.get("type") == "speech_error"]
    assert len(errors) == 1
    assert errors[0]["index"] == -1
    assert "the audio directory is gone" in errors[0]["error"]
    # The answer itself is all there. Losing the voice must not lose the words.
    assert "".join(e["text"] for e in events if e.get("type") == "text").count("Sentence") == 30
    assert synth.calls == []


def test_a_voice_that_died_after_the_last_token_is_still_reported(
    wired: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report of the death must outlive the death.

    Audio finished mid-answer is collected between ``text`` events. Nothing collects
    between *other* events -- ``reasoning`` here, but a run of tool calls does the same --
    so a worker that dies during them is finished by the time the turn ends, and the guard
    that skips the final collection when there is no longer a voice would swallow the one
    event explaining why there is no longer a voice.

    The reasoning events are load-bearing, not colour: with plain text after the failure
    the mid-stream collection would pick the report up and this test would prove nothing.
    """
    wired(
        ["Everything is fine. "],
        trailing=[{"type": "reasoning", "text": "considering"} for _ in range(4)],
    )

    async def explode(text: str, *, provider_id: str, model: str, voice: str | None) -> Any:
        raise RuntimeError("no audio device")

    monkeypatch.setattr(voice_stream, "synthesize_one", explode)
    events = run()
    errors = [e for e in events if e.get("type") == "speech_error"]
    assert len(errors) == 1
    assert "no audio device" in errors[0]["error"]
    assert types(events)[-1] == "done"


def test_stopping_the_voice_does_not_stop_the_turn(wired: Any) -> None:
    """A closed tab ends the listening, not the thinking.

    The exchange is written to the conversation by the model stream's own ``finally``, so
    abandoning generation to save synthesis would trade the answer for the audio. Setting
    the event stops only what the absent reader was going to hear.

    Measured against a control run of the same answer with nothing stopped, because the
    segmenter merges short sentences: any fixed number to compare against would be a
    property of the accumulator rather than of the stop, and would pass whether or not the
    stop did anything at all.
    """
    sentences = ["Sentence %d, long enough on its own to be cut as a segment here. " % i for i in range(8)]

    def go(stop: "asyncio.Event | None") -> tuple[list[dict[str, Any]], Synth]:
        synth = wired(sentences, synth=Synth(delay=0.01))

        async def drive() -> list[dict[str, Any]]:
            events: list[dict[str, Any]] = []
            async for event in voice_stream.stream(
                "how is the disk", conversation_id="c1", speak=True, stop_speaking=stop
            ):
                events.append(event)
                if stop is not None and event.get("type") == "speech":
                    stop.set()
            return events

        return asyncio.run(drive()), synth

    whole, whole_synth = go(None)
    partial, partial_synth = go(asyncio.Event())

    # The turn ran to the end either way: every chunk of text, and a proper ``done``.
    for events in (whole, partial):
        assert "".join(e["text"] for e in events if e.get("type") == "text").count("Sentence") == 8
        assert types(events)[-1] == "done"

    # And the voice stopped early rather than working through the backlog.
    assert len(partial_synth.calls) < len(whole_synth.calls)
    spoken = lambda evs: len([e for e in evs if e.get("type") == "speech"])  # noqa: E731
    assert spoken(partial) < spoken(whole)


def test_a_stop_mid_answer_drops_the_backlog(wired: Any) -> None:
    """Stopping is not "queue nothing further" -- the queue itself has to go.

    Eight sentences can already be waiting for synthesis by the time a stop arrives, and a
    worker left to finish them is precisely the audio nobody will hear that §21 is about.
    The model here keeps streaming for a while after the stop, which is the window a
    backlog would be chewed through in; the control run shows how many calls that is.
    """
    sentences = [
        "Sentence %d, long enough on its own to be cut as a segment here. " % i for i in range(12)
    ]

    def go(stop: "asyncio.Event | None") -> tuple[list[dict[str, Any]], Synth]:
        synth = wired(sentences, synth=Synth(delay=0.01))

        async def drive() -> list[dict[str, Any]]:
            events: list[dict[str, Any]] = []
            async for event in voice_stream.stream(
                "how is the disk", conversation_id="c1", speak=True, stop_speaking=stop
            ):
                events.append(event)
                if stop is not None and event.get("type") == "speech":
                    stop.set()
                # Slow the reader so the worker has time to work through a backlog if it
                # was left holding one. Without this the turn ends before the difference
                # between cancelling and not cancelling can show up at all.
                await asyncio.sleep(0.005)
            return events

        return asyncio.run(drive()), synth

    _, whole = go(None)
    _, partial = go(asyncio.Event())
    assert len(whole.calls) >= 4, "the control run never built a backlog worth dropping"
    assert len(partial.calls) <= 2, f"the backlog was synthesised anyway: {partial.calls}"


def test_a_stop_set_before_the_turn_never_starts_a_voice(wired: Any) -> None:
    """Nothing to cancel is better than something cancelled a tick later."""
    stop = asyncio.Event()
    stop.set()
    synth = wired(["The disk is fine. ", "Nothing needs doing."])

    async def drive() -> list[dict[str, Any]]:
        return [
            event
            async for event in voice_stream.stream(
                "how is the disk", conversation_id="c1", speak=True, stop_speaking=stop
            )
        ]

    events = asyncio.run(drive())
    kinds = types(events)
    assert synth.calls == []
    assert "voice" not in kinds
    assert "speech" not in kinds
    # And the answer is untouched: a stopped voice is not a stopped turn.
    assert "text" in kinds
    assert kinds[-1] == "done"


def test_a_speech_event_carries_a_url_the_web_view_can_play(wired: Any) -> None:
    """``path`` is for the desktop shell; ``url`` is the only thing the browser can use.

    The desktop client's ``playable()`` reads ``url`` and nothing else, returning null
    without one -- so a ``speech`` event with a real ``path`` and a missing ``url`` is not
    a cosmetic omission: every sentence would be reported as unplayable and the turn would
    be silent while claiming success. Asserted against the stub's own filename so the test
    fails if the route prefix changes as well as if it disappears.
    """
    wired(["The disk is fine. ", "Nothing needs doing at all right now, none of it."])
    events = run()
    speech = [e for e in events if e.get("type") == "speech"]
    assert speech, types(events)
    assert speech[0]["path"] == "/tmp/nexus-audio/seg1.wav"
    assert speech[0]["url"] == "/api/voice/audio/seg1.wav"
    for event in speech:
        assert event["url"] == f"/api/voice/audio/{event['path'].rsplit('/', 1)[-1]}"


def test_silence_by_request_never_asks_the_router_for_a_voice(
    wired: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``speak=False`` costs nothing, which is a claim about calls and not about events.

    Asserting "no ``speech`` events" cannot see the difference: with ``speak=False`` the
    worker is never started either way, so a version that resolved a TTS model and then
    threw the answer away would produce identical output while putting a spurious row in
    the routing log for every silent turn -- and, once a real provider is behind it,
    charging for a model nobody asked to hear. The only witness is the router itself.
    """
    wired(["Ready. ", "And the rest of the answer, which is long enough to be its own cut."])
    asked: list[str] = []

    def spy(job: str) -> Any:
        asked.append(job)
        return Decision()

    monkeypatch.setattr(voice_stream, "resolve_job", spy)
    events = run(speak=False)

    assert asked == [], f"the router was asked for {asked} on a silent turn"
    assert "voice" not in types(events)
    assert types(events)[-1] == "done"


# ------------------------------------------------------- the voice, chosen directly

# ``_voice`` promises a precedence -- the caller's choice, then the ``tts_voice`` setting,
# then the provider default -- and the tests above could not see two thirds of it. With no
# setting in the database every reading of it is the empty string, so swapping the first
# two terms changed nothing anywhere, and dropping the default entirely changed nothing
# either. Both survived a mutation run. This is the same gap the segmenter had: a helper
# with a documented contract, exercised only through a caller that happens not to
# distinguish it.
#
# It matters because the precedence is duplicated in ``/api/voice/speak``. Two paths that
# pick a voice differently is a bug report nobody can reproduce -- "it used the wrong
# voice, but only when I use the microphone".


class _Settings:
    """``app_settings`` as this module uses it: one ``get``."""

    def __init__(self, **values: str) -> None:
        self._values = values

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)


@pytest.mark.parametrize(
    ("requested", "setting", "expected", "why"),
    [
        ("en_ava_bright", "en_kim_calm", "en_ava_bright", "the caller's choice wins"),
        (None, "en_kim_calm", "en_kim_calm", "the setting is used when the caller is silent"),
        ("", "en_kim_calm", "en_kim_calm", "an empty request is not a choice"),
        ("   ", "en_kim_calm", "en_kim_calm", "nor is whitespace"),
        ("  en_ava_bright  ", "en_kim_calm", "en_ava_bright", "and a choice is stripped"),
        (None, "", None, "the provider default when neither says anything"),
        (None, None, None, "and when the setting was never written"),
    ],
)
def test_the_voice_precedence_is_caller_then_setting_then_default(
    monkeypatch: pytest.MonkeyPatch,
    requested: str | None,
    setting: str | None,
    expected: str | None,
    why: str,
) -> None:
    """``_voice`` delegates the precedence to :func:`nexus.providers.speech.voice_for`,
    so this asserts against the real door: the same arguments in, the same answer out.
    The table's voices are Mistral slugs, so ``mistral`` is the provider asked for --
    the door's ownership rule (a stored voice from *another* provider is dropped)
    must not interfere here, and is covered by the door's own tests.
    ``None`` out rather than a provider constant: the door's "no choice" answer is
    ``None`` and letting the provider pick, which is the only default that cannot
    be rejected.
    """
    values = {} if setting is None else {"tts_voice": setting}
    monkeypatch.setattr(voice_stream, "app_settings", _Settings(**values))
    assert voice_stream._voice(requested, "mistral") == expected, why


def test_the_setting_reaches_the_voice_event_and_the_provider(
    wired: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end, because a helper that is right and unused would pass the test above.

    Choosing a voice in Settings has to change what is heard, which means the value has to
    arrive at two places: the ``voice`` event the UI shows, and the synthesis call itself.
    """
    synth = wired(["The disk is fine. ", "Nothing needs doing at all right now, none of it."])
    monkeypatch.setattr(voice_stream, "app_settings", _Settings(tts_voice="en_kim_calm"))
    events = run()
    voice = [e for e in events if e.get("type") == "voice"]
    assert [e["voice"] for e in voice] == ["en_kim_calm"]
    assert synth.calls, types(events)
    assert {call["voice"] for call in synth.calls} == {"en_kim_calm"}


def test_the_routed_provider_is_the_one_that_serves_the_audio(wired: Any) -> None:
    """The decision names the provider, and the provider named is the one called.

    This is the substitution rule the whole migration exists to enforce: ``voice``
    events used to report the router's answer next to audio that had not come from
    it. The stub is wired to record ``provider_id`` on every call, and the router
    stub answers ``nvidia`` -- the non-default head for this module's history -- so a
    pipeline that ignored the decision and called the old hard-coded provider would
    fail the assertion rather than pass by coincidence.
    """
    synth = wired(["The disk is fine. ", "Nothing needs doing at all right now, none of it."])
    events = run()
    assert synth.calls, types(events)
    assert {call["provider_id"] for call in synth.calls} == {"nvidia"}, (
        "synthesis was served by a provider the router did not name"
    )
    # And the event the UI shows agrees with the provider that actually served.
    voice = [e for e in events if e.get("type") == "voice"]
    assert [e["routed"] for e in voice] == ["nvidia:nvidia/magpie-tts-multilingual"]


# ------------------------------------------------------------------ the segment payload


def test_only_a_code_segment_carries_its_source(wired: Any) -> None:
    """``source`` is sent when it differs from what is spoken, and not otherwise.

    For prose the two are the same string, so sending both doubles the payload of every
    segment event to say one thing twice -- on a long answer, over a stream the UI is
    already reading token by token. The code path is asserted above; this is the other
    half of the rule, and without it the "only when it differs" comment is a comment
    rather than a behaviour.
    """
    wired(["Here is the fix.\n\n```python\ndef f():\n    return 1\n```\n\nAll done now, thanks.\n"])
    events = run()
    segments = [e for e in events if e.get("type") == "segment"]
    assert segments, types(events)
    prose = [e for e in segments if e["kind"] != "code"]
    code = [e for e in segments if e["kind"] == "code"]
    assert prose and code, segments
    for event in prose:
        assert event["source"] is None, f"prose segment {event['index']} duplicated its text"
    for event in code:
        assert event["source"] and event["source"] != event["text"]
