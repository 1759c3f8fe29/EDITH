"""``POST /api/voice/converse``: the voice-first front door, over the real router.

The pipeline itself is tested in ``test_voice_stream.py`` -- ordering, overlap, failure
events. What is only testable here is the *route*: what it refuses before spending
anything, what the client is told before the first token, and what happens to the voice
when the reader goes away.

Three of these tests exist because the wrong version is silent rather than broken.

* A recording of silence must not leave an empty conversation behind. The turn is refused
  before the conversation is created, which is an ordering nothing in the response shows.
* Disconnecting must stop the synthesis and *not* the turn. Two different intentions, and
  the wrong version either bills for audio nobody hears or throws away an answer.
* The transcript the answer was built from must be sent. A wrong answer to a misheard
  question looks exactly like a wrong answer.

Stubbed: the speech provider, the STT routing decision, and the pipeline. Real: the
router, the conversation store, the SSE framing, and the busy-conversation guard.
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, AsyncIterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.routes import voice_routes
from nexus.db.repos import app_settings, conversations
from nexus.providers.nim_media import Transcript


class Decision:
    """The router's answer, three fields: the provider named is the one the door stub
    asserts was called."""

    key = "mistral:voxtral-mini"
    model_id = "voxtral-mini-latest"
    provider_id = "mistral"


class Ears:
    """``mistral_audio.client()`` for the hearing half only."""

    def __init__(self, text: str = "how much disk have I got left") -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    async def transcribe(self, data: Any, **kw: Any) -> Transcript:
        self.calls.append({"bytes": len(bytes(data)), **kw})
        return Transcript(text=self.text, model=kw.get("model") or "stub", language=kw.get("language"))


class Pipeline:
    """Stands in for ``voice_stream.stream``, recording how it was called."""

    def __init__(self, events: list[dict[str, Any]] | None = None, hold: bool = False) -> None:
        self.events = events or [
            {"type": "text", "text": "You have ninety gigabytes free. "},
            {"type": "speech", "index": 0, "text": "You have ninety gigabytes free.", "url": "/api/voice/audio/a.wav"},
            {"type": "done", "status": "complete"},
        ]
        self.hold = hold
        self.kwargs: dict[str, Any] = {}
        self.stopped: asyncio.Event | None = None
        self.finished = asyncio.Event()

    def __call__(self, request: str, **kw: Any) -> AsyncIterator[dict[str, Any]]:
        self.kwargs = {"request": request, **kw}
        self.stopped = kw.get("stop_speaking")
        return self._run()

    async def _run(self) -> AsyncIterator[dict[str, Any]]:
        try:
            for event in self.events:
                yield event
            if self.hold:
                # Keep the turn alive past the client's departure, so the test can look at
                # what the route did to the voice while the thinking was still going.
                for _ in range(200):
                    await asyncio.sleep(0.005)
                    if self.stopped is not None and self.stopped.is_set():
                        break
                yield {"type": "done", "status": "complete"}
        finally:
            self.finished.set()


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    def wire(pipeline: Pipeline | None = None, ears: Ears | None = None) -> tuple[TestClient, Pipeline, Ears]:
        pipeline = pipeline or Pipeline()
        ears = ears or Ears()
        monkeypatch.setattr(voice_routes.voice_stream, "stream", pipeline)
        monkeypatch.setattr(voice_routes, "speech_provider", _Door(ears))
        monkeypatch.setattr(voice_routes, "_resolved", lambda job: Decision())
        app = FastAPI()
        app.include_router(voice_routes.router)
        return TestClient(app), pipeline, ears

    return wire


class _Door:
    """A fake ``nexus.providers.speech`` for the hearing half only.

    The converse route asks the door -- ``transcribe(provider_id=...)`` -- rather than
    a provider, so the stub is door-shaped: it asserts the provider id is the one the
    routing decision named, which is the substitution rule this migration exists to
    enforce, and forwards to :class:`Ears` which records the call.
    """

    def __init__(self, ears: "Ears") -> None:
        self._ears = ears

    async def transcribe(self, data: Any, *, provider_id: str, model: str, language: str | None) -> Transcript:
        assert provider_id == "mistral", (
            "hearing was served by a provider the routing decision did not name"
        )
        return await self._ears.transcribe(data, model=model, language=language)


@pytest.fixture
def input_language_restored():
    """``input_language`` is global and persisted, so a test that sets it must put it back.

    Scoped to the tests that need it rather than autouse: the rest of this file does not
    touch the setting, and a fixture that runs everywhere would hide which tests depend on
    it.
    """
    before = app_settings.get("input_language")
    yield
    if before is not None:
        app_settings.set("input_language", before)


def events_of(body: str) -> list[dict[str, Any]]:
    """Parse the SSE body. Comment lines (heartbeats) are not events and are dropped."""
    out: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


WAV = base64.b64encode(b"RIFF....WAVEfmt ").decode()


# ------------------------------------------------------------------ what it refuses


def test_a_request_with_nothing_in_it_is_refused(wired: Any) -> None:
    http, pipeline, _ = wired()
    response = http.post("/api/voice/converse", json={})
    assert response.status_code == 400
    assert "message or a recording" in response.json()["detail"]
    assert pipeline.kwargs == {}


def test_an_unknown_mode_is_refused_by_name(wired: Any) -> None:
    http, _, _ = wired()
    response = http.post("/api/voice/converse", json={"message": "hello", "mode": "shouty"})
    assert response.status_code == 400
    assert "'shouty' is not a response mode" in response.json()["detail"]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("not base64 at all!!", id="obvious rubbish"),
        # Decodes *leniently* -- ``b64decode`` without ``validate`` silently drops every
        # character outside the alphabet and hands back three bytes of something else. The
        # honest failure is "that is not base64", not audio invented from the parts of it
        # that happened to be legal, so this case is what makes ``validate=True`` load
        # bearing rather than decorative.
        pytest.param("RI!!FF", id="sneaks past a lenient decoder"),
    ],
)
def test_a_recording_that_is_not_base64_says_so(wired: Any, payload: str) -> None:
    http, _, ears = wired()
    response = http.post("/api/voice/converse", json={"audio": payload})
    assert response.status_code == 400
    assert "not valid base64" in response.json()["detail"]
    assert ears.calls == [], "an undecodable recording was still sent to the model"


def test_an_oversized_recording_is_refused_before_it_is_decoded(wired: Any) -> None:
    http, _, ears = wired()
    huge = "A" * (voice_routes.MAX_AUDIO_BASE64 + 4)
    response = http.post("/api/voice/converse", json={"audio": huge})
    assert response.status_code == 413
    assert "MB" in response.json()["detail"]
    assert ears.calls == []


def test_silence_is_refused_without_leaving_a_conversation_behind(wired: Any) -> None:
    """The ordering is the test: refused *before* the conversation is created.

    A conversation created for a recording of silence is a row the user has to tidy up
    after a microphone they had muted, and it would be titled with nothing.
    """
    before = len(conversations.list_all(limit=200))
    http, pipeline, _ = wired(ears=Ears(text="   "))
    response = http.post("/api/voice/converse", json={"audio": WAV})
    assert response.status_code == 400
    assert "No speech was found" in response.json()["detail"]
    assert pipeline.kwargs == {}
    assert len(conversations.list_all(limit=200)) == before


def test_a_conversation_that_is_gone_is_404(wired: Any) -> None:
    http, pipeline, _ = wired()
    response = http.post(
        "/api/voice/converse", json={"message": "hello", "conversation_id": "does-not-exist"}
    )
    assert response.status_code == 404
    assert pipeline.kwargs == {}


def test_the_input_language_setting_reaches_the_voice_first_path(
    wired: Any, input_language_restored: None
) -> None:
    """The setting's fallback is read in ``_hear``, and only a request without a language sees it.

    ``/transcribe`` has this covered, and that is not the same thing: ``_hear`` is a second
    copy of the same two-step read, on the route that is the product's front door. Mutating
    the fallback out of ``_hear`` left every existing test green -- the converse tests all
    post ``language`` explicitly, so ``(language or "").strip()`` answers first and the
    setting is never consulted. The failure that hides behind that is a user who set their
    input language in Settings, spoke, and had the model guess anyway: the transcript comes
    back in transliterated nonsense and nothing anywhere reports a fault.
    """
    app_settings.set("input_language", "hi")
    http, pipeline, ears = wired()
    response = http.post("/api/voice/converse", json={"audio": WAV})

    assert response.status_code == 200, response.text
    assert ears.calls, "the recording never reached the provider"
    assert ears.calls[0]["language"] == "hi", (
        "the language the user set in Settings was not passed on the voice-first route"
    )


def test_a_language_on_the_request_still_beats_the_setting(
    wired: Any, input_language_restored: None
) -> None:
    """The other half of the same read: without this, "always use the setting" also passes."""
    app_settings.set("input_language", "hi")
    http, _, ears = wired()
    response = http.post("/api/voice/converse", json={"audio": WAV, "language": "fr"})

    assert response.status_code == 200, response.text
    assert ears.calls[0]["language"] == "fr"


# ------------------------------------------------------------------- what it streams


def test_the_turn_streams_as_server_sent_events(wired: Any) -> None:
    http, pipeline, _ = wired()
    response = http.post("/api/voice/converse", json={"message": "how is the disk"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    # Without this a reverse proxy buffers the whole response and hands it over in one
    # block at the end -- which is every guarantee in this route undone in the last hop:
    # the audio would arrive after the answer instead of during it.
    assert response.headers["x-accel-buffering"] == "no"
    # So a client that posted without an id can read it from the headers as well as from
    # the first event -- it needs it to send the next message to the same conversation.
    conversation_id = response.headers["x-nexus-conversation"]
    assert conversations.get(conversation_id) is not None

    events = events_of(response.text)
    assert events[0] == {"type": "start", "conversation_id": conversation_id, "spoken": True}
    assert [e["type"] for e in events[1:]] == ["text", "speech", "done"]
    # The pipeline was handed the turn, not a copy of the route's own idea of one.
    assert pipeline.kwargs["request"] == "how is the disk"
    assert pipeline.kwargs["conversation_id"] == conversation_id
    assert pipeline.kwargs["speak"] is True


def test_the_transcript_is_the_first_thing_the_client_is_told(wired: Any) -> None:
    """A wrong answer to a misheard question is otherwise indistinguishable."""
    http, pipeline, ears = wired(ears=Ears(text="how much disk have I got left"))
    response = http.post("/api/voice/converse", json={"audio": WAV, "language": "en"})
    events = events_of(response.text)

    assert events[0]["type"] == "start"
    assert events[1] == {
        "type": "heard",
        "text": "how much disk have I got left",
        "routed": "mistral:voxtral-mini",
    }
    # And the words that were heard are the words the turn was run on.
    assert pipeline.kwargs["request"] == "how much disk have I got left"
    assert ears.calls[0]["language"] == "en"
    assert ears.calls[0]["model"] == "voxtral-mini-latest"


def test_the_voice_and_mode_reach_the_pipeline(wired: Any) -> None:
    """A voice chosen in the UI that never arrives is a setting that looks broken."""
    http, pipeline, _ = wired()
    response = http.post(
        "/api/voice/converse",
        json={"message": "hello", "voice": "en_ava_bright", "mode": "quick", "speak": False},
    )
    assert pipeline.kwargs["voice"] == "en_ava_bright"
    assert pipeline.kwargs["mode"] == "quick"
    assert pipeline.kwargs["speak"] is False
    # And the client is told, before the first token, that no audio is coming. It is the
    # only thing in the stream that says so -- a silent turn and a turn whose synthesis
    # failed look identical otherwise, and a UI that lit SPEAKING over silence would be
    # reporting a state the core never claimed (§61).
    assert events_of(response.text)[0]["spoken"] is False


def test_a_second_turn_on_the_same_conversation_is_refused(
    wired: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``AiCore`` keys cancellation by conversation id, so two turns share one stop button.

    Refusing is the honest outcome: the alternative is a second turn that quietly makes
    the first one's stop button stop the wrong thing.
    """
    http, pipeline, _ = wired()
    existing = conversations.create("busy one")["id"]
    monkeypatch.setattr(
        voice_routes.turns, "ai_core", lambda: _Busy(existing), raising=True
    )
    response = http.post(
        "/api/voice/converse", json={"message": "hello", "conversation_id": existing}
    )
    assert response.status_code == 409
    assert "already running a turn" in response.json()["detail"]
    assert pipeline.kwargs == {}


class _Busy:
    def __init__(self, conversation_id: str) -> None:
        self._id = conversation_id

    def active(self) -> list[dict[str, Any]]:
        return [{"conversation_id": self._id}]


def test_the_driver_reports_a_pipeline_that_raises(wired: Any) -> None:
    """A turn that blows up must say so on the stream, not end it mid-sentence.

    Without the net in ``_drive`` the exception kills the driver task, the sentinel is
    never queued, and the response hangs on the heartbeat until the client gives up --
    which the user sees as NEXUS thinking forever.
    """

    class Broken(Pipeline):
        async def _run(self) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "text", "text": "starting"}
            raise RuntimeError("the pipeline fell over")

    http, _, _ = wired(pipeline=Broken())
    response = http.post("/api/voice/converse", json={"message": "hello"})
    events = events_of(response.text)
    assert [e["type"] for e in events] == ["start", "text", "error"]
    assert "the pipeline fell over" in events[-1]["error"]
    assert events[-1]["kind"] == "internal"


def test_leaving_stops_the_voice_and_not_the_turn(wired: Any) -> None:
    """Two intentions, two outcomes: the answer finishes, the synthesis does not.

    ``stop_speaking`` is the route's whole answer to a closed tab, so a test that only
    checked the happy path would let a regression bill for audio nobody hears -- or, worse,
    throw away an answer the user is going to come back for.
    """
    http, pipeline, _ = wired(pipeline=Pipeline(hold=True))
    with http.stream("POST", "/api/voice/converse", json={"message": "hello"}) as response:
        for line in response.iter_lines():
            if '"speech"' in line:
                break

    assert pipeline.stopped is not None
    assert pipeline.stopped.is_set(), "the voice was left running after the client left"


def test_a_running_turn_is_held_by_a_strong_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    """``asyncio`` keeps only a weak reference to a task, and this turn is awaited by nobody.

    The driver is fired with ``create_task`` and its result is never collected -- the
    response reads the queue, not the task -- so without a strong reference the garbage
    collector may take a suspended turn mid-answer. Nothing raises when that happens: the
    audio simply stops and the conversation write in ``AiCore.stream``'s ``finally`` never
    runs, which is the worst shape a bug can have.

    Run against ``_drive`` directly rather than through ``TestClient``: the client buffers
    the whole SSE response, so by the time the first line is readable the turn has already
    finished and discarded itself -- the set is empty either way and a regression would
    pass. Here the event loop is real, the turn is held open by ``release``, and the set can
    be read while it runs. Both halves matter, so both are asserted without resting the
    first on when the collector fires: held while running, released once it ends.
    """
    release = asyncio.Event()

    async def stub(**kw: Any) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "text", "text": "still thinking"}
        await release.wait()
        yield {"type": "done", "status": "complete"}

    monkeypatch.setattr(voice_routes.voice_stream, "stream", stub)

    async def go() -> None:
        queue: asyncio.Queue = asyncio.Queue()
        driver = asyncio.create_task(
            voice_routes._drive(queue, {"request": "hello"}, asyncio.Event()),
            name="voice-turn:test",
        )
        # Await the first event rather than sleeping a fixed amount: the event can only
        # reach the queue after ``_drive`` has done its book-keeping, so this wait is the
        # assertion's own readiness probe and a hang here surfaces as a timeout, not a flap.
        first = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert first["type"] == "text"
        assert driver in voice_routes._TURNS, "the running turn is not strongly referenced"

        release.set()
        await driver
        assert driver not in voice_routes._TURNS, "a finished turn is still referenced"

    asyncio.run(go())
