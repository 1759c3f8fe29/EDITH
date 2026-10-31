"""Groq speech: the multipart it must send, and the two traps it exists around.

Three things are pinned here.

**The Cloudflare trap.** ``403 error code: 1010`` is not an auth failure, and reading
it as one sends the user to rotate a key that works. Every one of the four keys
failed identically until a browser User-Agent was sent. So there is a test that the
UA is actually sent, and a test that 1010 is *reported* as a UA problem rather than
as a rejection.

**The multipart body.** The endpoint takes a file part and a model part; sending
JSON gives a 400 that says nothing useful. The body is composed by hand precisely so
this can be asserted byte-for-byte.

**The asymmetry.** Groq hears and cannot speak, and that is a fact about the
provider rather than an oversight. ``speech.synthesize`` must refuse it with a
sentence instead of raising ``AttributeError`` from inside a client that has no
``synthesize`` -- and the router must never be able to offer it a ``tts`` job.
"""

from __future__ import annotations

import asyncio
import functools
import json
import urllib.error
from typing import Any, Callable

import pytest

from nexus.providers import groq_audio, speech
from nexus.providers.base import ProviderError, RequestRejected


def sync(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


# --------------------------------------------------------------- the multipart


def test_the_body_carries_a_file_part_and_a_model_part() -> None:
    """The shape the endpoint requires. JSON here is a 400 with no explanation."""
    body, content_type = groq_audio._multipart(
        b"RIFFfake-audio", filename="clip.wav", model="whisper-large-v3-turbo", language=None
    )
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=")[1]

    assert body.startswith(f"--{boundary}\r\n".encode())
    assert body.rstrip().endswith(f"--{boundary}--".encode())
    assert b'name="file"; filename="clip.wav"' in body
    assert b"RIFFfake-audio" in body
    assert b'name="model"' in body
    assert b"whisper-large-v3-turbo" in body


def test_the_language_part_is_omitted_when_not_asked_for() -> None:
    """An empty language field is not the same as not sending one."""
    without, _ = groq_audio._multipart(b"x", filename="a.wav", model="m", language=None)
    assert b'name="language"' not in without

    with_it, _ = groq_audio._multipart(b"x", filename="a.wav", model="m", language="en")
    assert b'name="language"' in with_it


# ------------------------------------------------------------ the two refusals


def test_an_oversized_recording_is_refused_before_the_upload() -> None:
    """Refusing locally beats uploading 30 MB to be told the same thing."""
    huge = b"\x00" * (groq_audio.MAX_AUDIO_BYTES + 1)
    with pytest.raises(RequestRejected) as caught:
        asyncio.run(groq_audio.client().transcribe(huge, model="whisper-large-v3-turbo"))
    assert "MB" in str(caught.value)


def test_an_empty_recording_is_refused() -> None:
    with pytest.raises(RequestRejected):
        asyncio.run(groq_audio.client().transcribe(b"", model="whisper-large-v3-turbo"))


def test_a_model_groq_does_not_have_is_refused_by_name() -> None:
    """Naming what it does offer is the difference between a fix and a guess."""
    with pytest.raises(RequestRejected) as caught:
        asyncio.run(groq_audio.client().transcribe(b"x", model="whisper-99"))
    assert "whisper-large-v3-turbo" in str(caught.value)


def test_a_missing_file_is_refused_with_its_path(tmp_path: Any) -> None:
    with pytest.raises(RequestRejected) as caught:
        asyncio.run(
            groq_audio.client().transcribe(tmp_path / "absent.wav", model="whisper-large-v3-turbo")
        )
    assert "absent.wav" in str(caught.value)


# --------------------------------------------------------- the Cloudflare trap


def test_1010_is_reported_as_a_user_agent_problem_not_a_bad_key() -> None:
    """The misdiagnosis this exists to prevent.

    Cloudflare's 1010 looks exactly like an expired key. Reading it that way sends
    the user to rotate a credential that is working -- so the message must name the
    real cause, and must not read like an auth failure.
    """
    error = groq_audio._translate(403, "error code: 1010")
    text = str(error)
    assert isinstance(error, ProviderError)
    assert "1010" in text
    assert "User-Agent" in text
    # It must not tell the user their key is bad, because it is not.
    assert "rejected the API key" not in text


def test_a_real_401_still_says_the_key_is_the_problem() -> None:
    error = groq_audio._translate(401, json.dumps({"error": {"message": "Invalid API Key"}}))
    assert "rejected the API key" in str(error)
    assert "Invalid API Key" in str(error)


def test_a_rate_limit_names_the_pool_because_that_is_what_changed_it() -> None:
    """With four accounts the only way to be limited is for all four to be."""
    error = groq_audio._translate(429, "rate limit exceeded")
    assert "rotates" in str(error)


def test_a_bad_request_is_a_rejection_rather_than_a_retryable_failure() -> None:
    """A 400 is the audio's fault; retrying the same bytes cannot help."""
    error = groq_audio._translate(400, json.dumps({"error": {"message": "Invalid file format"}}))
    assert isinstance(error, RequestRejected)
    assert error.retryable is False


def test_the_browser_user_agent_is_actually_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole 1010 finding is worthless if the header is dropped in a refactor."""
    seen: dict[str, Any] = {}

    class FakeResponse:
        def read(self) -> bytes:
            return json.dumps({"text": "hello"}).encode()

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    def fake_urlopen(request: Any, timeout: float = 0) -> Any:
        seen["headers"] = dict(request.headers)
        return FakeResponse()

    monkeypatch.setattr(groq_audio.urllib.request, "urlopen", fake_urlopen)
    result = groq_audio.client()._post(b"body", "multipart/form-data; boundary=x")

    assert result["text"] == "hello"
    # urllib title-cases header names, so compare case-insensitively.
    lowered = {k.lower(): v for k, v in seen["headers"].items()}
    assert "user-agent" in lowered, "no User-Agent sent: Groq's edge will answer 1010"
    assert "Mozilla" in lowered["user-agent"]


# ------------------------------------------------------- the rotation pool


def test_keys_rotate_across_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    """One account's rate limit must not be the ceiling for the install."""
    monkeypatch.setattr(
        groq_audio.secrets, "pool", lambda name: ["key-a", "key-b", "key-c"]
    )
    client = groq_audio.GroqAudioClient()
    assert [client._key() for _ in range(4)] == ["key-a", "key-b", "key-c", "key-a"]


def test_a_single_key_is_unaffected_by_the_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one-key install must behave exactly as it did before pools existed."""
    monkeypatch.setattr(groq_audio.secrets, "pool", lambda name: ["only-key"])
    client = groq_audio.GroqAudioClient()
    assert [client._key() for _ in range(3)] == ["only-key"] * 3


def test_no_keys_gives_a_message_that_names_where_to_add_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.providers.base import MissingCredential

    monkeypatch.setattr(groq_audio.secrets, "pool", lambda name: [])
    with pytest.raises(MissingCredential) as caught:
        groq_audio.GroqAudioClient()._key()
    assert "Settings" in str(caught.value)


# --------------------------------------------------------------- the asymmetry


def test_groq_hears_and_does_not_speak() -> None:
    """A fact about the provider, not an oversight: Groq has no TTS endpoint."""
    assert groq_audio.serves("stt") is True
    assert groq_audio.serves("tts") is False
    assert groq_audio.models_for("tts") == ()


def test_the_door_says_groq_cannot_speak_rather_than_crashing() -> None:
    """An AttributeError from inside a client is not an explanation."""
    with pytest.raises(speech.CannotSpeak) as caught:
        asyncio.run(
            speech.synthesize("hello", provider_id="groq", model="whisper-large-v3-turbo")
        )
    message = str(caught.value)
    assert "text-to-speech" in message
    assert "transcribes" in message


def test_the_door_knows_each_provider_s_surfaces() -> None:
    """Asked rather than inferred from a missing attribute."""
    assert speech.surfaces("nvidia") == frozenset({"hear", "speak"})
    assert speech.surfaces("mistral") == frozenset({"hear", "speak"})
    assert speech.surfaces("groq") == frozenset({"hear"})
    # An unknown provider claims nothing, which is the safe default.
    assert speech.surfaces("nobody") == frozenset()


def test_the_tts_chain_does_not_offer_a_provider_that_cannot_speak() -> None:
    """The chain must not contain a candidate that can only fail."""
    from nexus.router import jobs

    tts = jobs.spec("tts")
    providers_in_chain = {jobs.split_key(k)[0] for k in tts.chain}
    assert "groq" not in providers_in_chain, (
        "groq is in the tts chain but has no synthesis endpoint"
    )


def test_the_stt_chain_heads_groq_because_it_was_measured_faster() -> None:
    """The ordering is a measurement, so it is worth pinning against a tidy-up.

    Recorded: 0.55 s Groq against 7.87 s NVIDIA on this machine, same sentence, same
    transcript. A future reader reordering this "back" for familiarity would be
    undoing fourteen times the speed for no reason, so the reason is asserted.
    """
    from nexus.router import jobs

    stt = jobs.spec("stt")
    assert stt.chain[0] == "groq:whisper-large-v3-turbo"
    # And the slower-but-stronger models stay behind it rather than being dropped.
    assert "nvidia:nvidia/parakeet-1-1b-rnnt-multilingual-asr" in stt.chain
