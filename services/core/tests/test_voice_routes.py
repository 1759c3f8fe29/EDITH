"""The voice routes, with the provider stubbed and everything else real.

NEXUS is voice-first, so these routes are the front door: a recording goes in, text
comes out, an answer goes back as audio. Three things about them are worth a test
rather than a live check, because a live check spends the user's quota and still would
not reach them.

**The settings actually reach the provider.** ``input_language`` and ``tts_voice`` are
settings a user changes expecting to be understood in their language and answered in
their voice. Nothing in a happy-path smoke test notices when a route stops reading them
-- the call still succeeds, in the wrong language.

**The audio route is a file read, and it is the one place the UI can name a file.**
Everything else the browser can reach goes through the permission engine;
``/api/voice/audio/{name}`` does not, because it serves only what this core synthesised.
That makes the name check the whole of its security, so it is tested as such.

**A file that is gone is not a file that never existed.** The audio directory is a
cache and gets cleaned; 410 tells the caller to ask for it again, 404 would send them
looking for a bug.

The provider is replaced and nothing else is. The router, the settings store, the
FastAPI app and the real ``audio_dir`` are the shipped ones.
"""
from __future__ import annotations

import wave
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.routes import voice_routes
from nexus.config import settings
from nexus.db.repos import app_settings
from nexus.providers.base import ProviderError
from nexus.providers.nim_media import Speech, Transcript

VOICE_KEYS = ("input_language", "tts_voice", "tts_enabled", "voice_enabled")


@pytest.fixture(autouse=True)
def voice_settings_restored():
    """These are global. A test that changes one must not change it for the next."""
    before = {key: app_settings.get(key) for key in VOICE_KEYS}
    yield
    for key, value in before.items():
        if value is not None:
            app_settings.set(key, value)


class Provider:
    """Stands in for the provider behind the door, recording what it was asked for.

    The point of the recording is that these tests assert on the *arguments*: a route
    that drops ``language`` still returns a perfectly good transcript, so only the
    call itself can show the setting was honoured.
    """

    def __init__(self, wav: Path) -> None:
        self.wav = wav
        self.transcribe_kwargs: list[dict[str, Any]] = []
        self.synthesize_kwargs: list[dict[str, Any]] = []

    async def transcribe(self, data: Any, **kw: Any) -> Transcript:
        self.transcribe_kwargs.append(kw)
        return Transcript(
            text="the machine heard this", model=kw.get("model") or "stub",
            language=kw.get("language"), duration_s=1.5, words=4, latency_ms=12,
        )

    async def synthesize(self, text: str, **kw: Any) -> Speech:
        self.synthesize_kwargs.append({"text": text, **kw})
        return Speech(
            path=self.wav, model=kw.get("model") or "stub", bytes_written=self.wav.stat().st_size,
            media_type="audio/wav", duration_s=0.5, latency_ms=34,
        )


@pytest.fixture
def wav() -> Path:
    """A real WAV in the real audio directory -- the route resolves paths for real."""
    directory = settings().audio_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "nexus-test-speech.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 800)
    yield path
    path.unlink(missing_ok=True)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, wav: Path):
    """The real router on a bare app, so no lifespan work runs for a route test.

    The provider is stubbed at the *door* -- ``nexus.providers.speech``'s dispatch
    table -- rather than as a provider module, because that is the seam the routes
    now call. Patching the table is what makes these tests say anything about the
    migration the routes went through: a route that bypassed the door and called a
    provider directly would miss the stub entirely and fail loudly, where a stubbed
    provider module would be called happily either way.
    """
    provider = Provider(wav)
    door = _Door(provider, monkeypatch)
    monkeypatch.setattr(voice_routes, "_resolved", lambda job: _Door.DECISION)
    app = FastAPI()
    app.include_router(voice_routes.router)
    with TestClient(app) as http:
        yield http, provider


class _Door:
    """A fake ``nexus.providers.speech``: routing-shaped answers, one provider.

    The routes ask the door -- ``transcribe(provider_id=...)``, ``synthesize``,
    ``voices``, ``has_credential``, ``default_voice`` -- so the fake implements that
    surface and forwards to :class:`Provider`, which records the arguments the tests
    assert on. The decision names ``mistral`` because the stub provider's voices are
    Mistral slugs; the provider id has to agree with them or the door's real
    voice-ownership rule would drop the setting the tests assert is honoured.
    """

    DEFAULT_VOICE = "en_paul_neutral"

    def __init__(self, provider: "Provider", monkeypatch: pytest.MonkeyPatch) -> None:
        self._provider = provider
        self._credential = True
        monkeypatch.setattr(voice_routes, "speech_provider", self)

    # The decision the routes' ``_resolved`` stub returns: the real class's three read
    # fields, nothing else. Names the same provider the stub serves, so the door's
    # voice-ownership rule never interferes with the setting tests.
    class _Decision:
        key = "mistral:stub"
        model_id = "stub-model"
        provider_id = "mistral"

    DECISION = _Decision

    async def transcribe(self, data: Any, *, provider_id: str, model: str, language: str | None) -> Transcript:
        assert provider_id == "mistral", "the door was asked for a provider it was not routed to"
        return await self._provider.transcribe(data, model=model, language=language)

    async def synthesize(
        self, text: str, *, provider_id: str, model: str, voice: str | None, language: str | None = None
    ) -> Speech:
        assert provider_id == "mistral", "the door was asked for a provider it was not routed to"
        return await self._provider.synthesize(text, model=model, voice=voice, language=language)

    async def voices(self, provider_id: str, model: str | None = None) -> list[dict[str, Any]]:
        return [{"slug": self.DEFAULT_VOICE, "name": "Stub", "gender": None, "languages": [], "tags": []}]

    def has_credential(self, provider_id: str) -> bool:
        return self._credential

    def default_voice(self, provider_id: str) -> str:
        return self.DEFAULT_VOICE

    def max_chars(self, provider_id: str) -> int:
        return 4_000

    def voice_for(self, provider_id: str, requested: str | None, configured: str | None) -> str | None:
        """The real precedence minus the door's ownership rule, which is not under test here."""
        chosen = (requested or "").strip()
        if chosen:
            return chosen
        return (configured or "").strip() or None


# ------------------------------------------------------------------ transcription


def test_the_input_language_setting_is_told_to_the_model(client) -> None:
    http, provider = client
    app_settings.set("input_language", "hi")
    response = http.post("/api/voice/transcribe", files={"file": ("a.wav", b"RIFFdata", "audio/wav")})

    assert response.status_code == 200, response.text
    assert provider.transcribe_kwargs[-1]["language"] == "hi", \
        "a language the user set and the model was not told is a setting that does nothing"


def test_the_caller_can_override_the_setting(client) -> None:
    http, provider = client
    app_settings.set("input_language", "hi")
    response = http.post(
        "/api/voice/transcribe",
        files={"file": ("a.wav", b"RIFFdata", "audio/wav")},
        data={"language": "fr"},
    )

    assert response.status_code == 200, response.text
    assert provider.transcribe_kwargs[-1]["language"] == "fr"


def test_an_empty_language_setting_is_sent_as_nothing_not_as_empty(client) -> None:
    # "" would be a language code the provider has to reject. None means "you decide",
    # which is the actual intent of leaving the setting blank.
    http, provider = client
    app_settings.set("input_language", "")
    response = http.post("/api/voice/transcribe", files={"file": ("a.wav", b"RIFFdata", "audio/wav")})

    assert response.status_code == 200, response.text
    assert provider.transcribe_kwargs[-1]["language"] is None


def test_an_empty_recording_is_refused_before_the_provider_is_paid(client) -> None:
    http, provider = client
    response = http.post("/api/voice/transcribe", files={"file": ("a.wav", b"", "audio/wav")})

    assert response.status_code == 400
    assert not provider.transcribe_kwargs, "an empty upload must not reach the provider"


def test_an_oversized_recording_is_refused_before_the_provider_is_paid(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    http, provider = client
    # The real limit is 25 MB, and a test that posts 25 MB to prove a length check is a
    # slow way to spend memory. The check is what is under test, not the constant.
    monkeypatch.setattr(voice_routes, "MAX_UPLOAD_BYTES", 16)
    response = http.post(
        "/api/voice/transcribe", files={"file": ("big.wav", b"R" * 100, "audio/wav")}
    )

    assert response.status_code == 413
    assert "MB" in response.json()["detail"]
    assert not provider.transcribe_kwargs, "an oversized upload must not reach the provider"


def test_a_transcription_provider_error_is_a_502_not_a_crash(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An outage is a fact to report, not an exception to leak as a 500."""
    http, provider = client

    async def failing(data: Any, **kw: Any) -> Transcript:
        raise ProviderError("the hearing provider is down")

    monkeypatch.setattr(provider, "transcribe", failing)
    response = http.post("/api/voice/transcribe", files={"file": ("a.wav", b"RIFFdata", "audio/wav")})

    assert response.status_code == 502
    assert "hearing provider is down" in response.json()["detail"]


def test_a_recording_that_hears_nothing_says_so(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence is a successful call that heard nothing -- not text, and not an error."""
    http, provider = client

    async def silent(data: Any, **kw: Any) -> Transcript:
        return Transcript(text="", model="stub", language=None, duration_s=0.0, words=0, latency_ms=1)

    monkeypatch.setattr(provider, "transcribe", silent)
    response = http.post("/api/voice/transcribe", files={"file": ("a.wav", b"RIFFdata", "audio/wav")})

    body = response.json()
    assert response.status_code == 200, response.text
    assert body["empty"] is True, "silence reported as speech is a message invented by the UI"
    assert "No speech" in body["message"]


def test_a_synthesis_provider_error_is_a_502_not_a_crash(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The speaking half has the same outage contract as the hearing half."""
    http, provider = client

    async def failing(text: str, **kw: Any) -> Speech:
        raise ProviderError("the speaking provider is down")

    monkeypatch.setattr(provider, "synthesize", failing)
    response = http.post("/api/voice/speak", json={"text": "hello"})

    assert response.status_code == 502
    assert "speaking provider is down" in response.json()["detail"]


# --------------------------------------------------------------------- synthesis


def test_speak_returns_a_url_the_browser_can_play(client, wav: Path) -> None:
    http, _ = client
    response = http.post("/api/voice/speak", json={"text": "all systems are online"})
    body = response.json()

    assert response.status_code == 200, response.text
    # The path is for a shell with filesystem access; the url is the only thing a web
    # view can use, and a UI given only a path shows a player that plays nothing.
    assert body["path"] == str(wav)
    assert body["url"] == f"/api/voice/audio/{wav.name}"


def test_the_tts_voice_setting_is_used_when_the_caller_names_none(client) -> None:
    http, provider = client
    app_settings.set("tts_voice", "en_alice_calm")
    response = http.post("/api/voice/speak", json={"text": "speak up"})

    assert response.status_code == 200, response.text
    assert provider.synthesize_kwargs[-1]["voice"] == "en_alice_calm"
    assert response.json()["voice"] == "en_alice_calm", "the answer must name the voice used"


def test_a_named_voice_beats_the_setting(client) -> None:
    http, provider = client
    app_settings.set("tts_voice", "en_alice_calm")
    response = http.post("/api/voice/speak", json={"text": "speak up", "voice": "en_paul_neutral"})

    assert response.status_code == 200, response.text
    assert provider.synthesize_kwargs[-1]["voice"] == "en_paul_neutral"


def test_an_empty_setting_leaves_the_choice_to_the_provider(client) -> None:
    http, provider = client
    app_settings.set("tts_voice", "")
    response = http.post("/api/voice/speak", json={"text": "speak up"})

    assert response.status_code == 200, response.text
    assert provider.synthesize_kwargs[-1]["voice"] is None
    assert response.json()["voice"], "the response still has to say which voice spoke"


# ------------------------------------------------------------------------- voices


def test_voices_reports_whether_a_credential_is_present(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settings panel disables the picker for this fact and not for a round-trip."""
    http, _ = client
    monkeypatch.setattr(voice_routes.speech_provider, "has_credential", lambda pid: False)
    response = http.get("/api/voice/voices")

    assert response.status_code == 200, response.text
    assert response.json()["configured"] is False


def test_the_live_voice_list_is_refused_without_a_credential(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No key means the provider cannot be asked at all, and a 409 says so not a guess."""
    http, _ = client
    monkeypatch.setattr(voice_routes.speech_provider, "has_credential", lambda pid: False)
    response = http.get("/api/voice/voices/live")

    assert response.status_code == 409
    assert "api key" in response.json()["detail"].lower()


# ------------------------------------------------------------------ serving audio


def test_the_audio_route_serves_the_bytes_and_refuses_to_cache_them(client, wav: Path) -> None:
    http, _ = client
    response = http.get(f"/api/voice/audio/{wav.name}")

    assert response.status_code == 200, response.text
    assert response.content == wav.read_bytes()
    assert response.headers["content-type"] == "audio/wav"
    # The directory is a cache and gets swept. A cached 200 for a file that is gone is
    # worse than a fresh 410, because the browser would replay from memory and the
    # user would never learn the file had expired.
    assert response.headers["cache-control"] == "no-store"


def test_a_swept_file_is_gone_rather_than_never_there(client) -> None:
    http, _ = client
    response = http.get("/api/voice/audio/20200101-000000-000000-speech.wav")

    assert response.status_code == 410, "404 sends the user looking for a bug in the core"
    assert "spoken again" in response.json()["detail"]


@pytest.mark.parametrize(
    "name",
    [
        "../../../etc/passwd",          # the classic
        "..",                            # the directory itself
        "sub/dir.wav",                  # a separator at all
        "notes.txt",                    # a real file, wrong kind
        "",                              # nothing
    ],
)
def test_only_a_plain_wav_name_is_accepted(name: str) -> None:
    # Called directly rather than over HTTP on purpose: an HTTP client normalises
    # ``..`` and percent-decodes before the route ever sees it, so a green HTTP test
    # would be measuring httpx rather than this function. The function is what any
    # future caller will use, and it is what has to refuse.
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as raised:
        voice_routes._audio_path(name)
    assert raised.value.status_code == 400, name


def test_a_symlink_out_of_the_directory_is_refused(client) -> None:
    # The name has no separators and ends in .wav, so every string check passes. Only
    # resolving the link catches it -- which is why the check is on the resolved
    # parent and not on the name. Without it, one symlink in a world-writable cache
    # directory turns this route into "read any file on the machine".
    from fastapi import HTTPException

    directory = settings().audio_dir
    directory.mkdir(parents=True, exist_ok=True)
    link = directory / "nexus-test-escape.wav"
    link.unlink(missing_ok=True)
    link.symlink_to("/etc/passwd")
    try:
        with pytest.raises(HTTPException) as raised:
            voice_routes._audio_path(link.name)
        assert raised.value.status_code == 400
    finally:
        link.unlink(missing_ok=True)


def test_a_real_name_resolves_to_the_real_file(wav: Path) -> None:
    assert voice_routes._audio_path(wav.name) == wav.resolve()


def test_the_route_is_registered_on_the_real_app() -> None:
    # The tests above run the router on a bare app, which proves the handler and not
    # the wiring. If `create_app` ever stops including this router the UI loses
    # playback with no test failing anywhere else.
    from nexus.api import create_app

    paths = {getattr(route, "path", "") for route in create_app().routes}
    assert "/api/voice/audio/{name}" in paths
