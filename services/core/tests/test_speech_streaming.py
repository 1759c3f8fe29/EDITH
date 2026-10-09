"""Streaming speech-to-text: partials, and what a consumer must not do with them.

The audits recorded "hearing is per-utterance" as a standing shortfall three times,
and the reason is worth keeping: NVIDIA serves speech on gRPC, every REST path
answers 404, and the offline recogniser was the only call in use. Riva has had a
streaming recogniser the whole time; nothing reached it.

These tests cover the two things that are easy to get wrong and hard to notice.

**Partials replace, they do not append.** Measured live against the real endpoint,
one sentence arrived as ``'Next'``, then ``'Nexus is'``, then ``'Nexus is online .'``
-- revisions of the same utterance, not additions to it. A consumer that
concatenated them would produce `"NextNexus isNexus is online ."`, which reads as the
machine repeatedly changing its mind. So ``is_final`` is a field the recogniser
reports rather than something a caller infers, and one of these tests asserts exactly
one final result per stream.

**A failure on the worker thread must reach the consumer.** Riva's generator is a
blocking iterator, so it is drained off the event loop and handed back through a
queue. An exception raised during that drain has to cross back over, or the caller
waits forever on a recognition that already died -- a hang, which is a worse bug than
an error because nothing reports it.
"""

from __future__ import annotations

import asyncio
import functools
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from nexus.providers import nvidia_speech
from nexus.providers.base import ProviderError
from nexus.providers.nim_media import Transcript

PCM_32K = b"\x00" * 32_000


def sync(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


class FakeResult:
    """One StreamingRecognitionResult, in the shape the wire delivers."""

    def __init__(self, text: str, *, is_final: bool, stability: float = 0.0) -> None:
        self.alternatives = [SimpleNamespace(transcript=text)]
        self.is_final = is_final
        self.stability = stability


class FakeResponse:
    def __init__(self, *results: FakeResult) -> None:
        self.results = list(results)


class FakeService:
    """A Riva ASR service whose streaming generator the test scripts."""

    def __init__(self, script: Callable[[Any, Any], Any]) -> None:
        self._script = script
        self.config: Any = None
        self.chunks = 0

    def streaming_response_generator(self, audio_chunks: Any, streaming_config: Any) -> Any:
        self.config = streaming_config
        for _ in audio_chunks:
            self.chunks += 1
        yield from self._script(self.config, self.chunks)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Any):
    """Build a client whose service, audio preparation and endpoint are supplied.

    Returns ``(client, path, make_service)`` -- the caller scripts the recogniser,
    which keeps each test's expectation readable as the sequence it is asserting.
    """
    def build(script: Callable[[Any, Any], Any]) -> tuple[Any, Any, FakeService]:
        service = FakeService(script)
        instance = nvidia_speech.NvidiaSpeechClient()
        monkeypatch.setattr(instance, "_service", lambda endpoint, kind: service)
        monkeypatch.setattr(
            nvidia_speech, "_prepare_for_asr", lambda data, filename="", pcm=PCM_32K: pcm
        )
        monkeypatch.setattr(
            nvidia_speech,
            "_endpoint",
            lambda job, model: SimpleNamespace(
                model=model or "test-asr",
                label="Test ASR",
                function_id="f",
                protocol="grpc",
            ),
        )
        path = tmp_path / "clip.wav"
        path.write_bytes(b"RIFF....WAVE")
        return instance, path, service

    return build


def three_step(*, final_text: str = "Nexus is online.") -> Callable[..., Any]:
    """A recogniser script producing the real observed shape: two partials, a final."""

    def script(config: Any, chunks: int) -> Any:
        yield FakeResponse(FakeResult("Nex", is_final=False))
        yield FakeResponse(FakeResult("Nexus is", is_final=False, stability=0.4))
        yield FakeResponse(FakeResult(final_text, is_final=True, stability=1.0))

    return script


async def collect(instance: Any, path: Any, **kwargs: Any) -> list[Transcript]:
    out: list[Transcript] = []
    async for item in instance.transcribe_stream(str(path), **kwargs):
        out.append(item)
    return out


# ---------------------------------------------------------------- the happy path


@sync
async def test_partials_arrive_and_exactly_one_is_final(client: Any) -> None:
    instance, path, _ = client(three_step())
    results = await collect(instance, path)

    assert [r.text for r in results] == ["Nex", "Nexus is", "Nexus is online."]
    assert [r.is_final for r in results] == [False, False, True]
    assert sum(1 for r in results if r.is_final) == 1, (
        "a consumer must be able to find the committed result without guessing"
    )


@sync
async def test_partials_are_revisions_not_appends(client: Any) -> None:
    """The property a caller would get wrong by joining them.

    Each yielded transcript should be readable on its own as what has been heard so
    far. Asserting monotone growth of a *prefix* is the honest way to state that:
    joining them would not be a prefix of the final text.
    """
    instance, path, _ = client(three_step())
    results = await collect(instance, path)

    final = results[-1].text
    for earlier in results[:-1]:
        assert final.startswith(earlier.text.rstrip(" .")), (
            f"{earlier.text!r} is not a prefix of {final!r}, so partials are not "
            "revisions of one utterance"
        )


@sync
async def test_stability_is_passed_through_rather_than_invented(client: Any) -> None:
    """A UI avoiding flicker needs the recogniser's own number, not a guess."""
    instance, path, _ = client(three_step())
    results = await collect(instance, path)
    assert [r.stability for r in results] == [0.0, 0.4, 1.0]


@sync
async def test_interim_results_are_actually_requested(client: Any) -> None:
    """Without this flag Riva answers once at the end and the method is pointless."""
    instance, path, service = client(three_step())
    await collect(instance, path)
    assert service.config.interim_results is True


@sync
async def test_the_audio_is_pushed_in_chunks_not_as_one_blob(client: Any) -> None:
    """32 KB at 8 KB a step is four pushes; one push would not stream at all."""
    instance, path, service = client(three_step())
    await collect(instance, path)
    assert service.chunks == 4


@sync
async def test_a_single_chunk_still_produces_its_result(client: Any) -> None:
    """The boundary: audio smaller than one step must not fall through the loop."""
    instance, path, service = client(three_step())
    monkeypatch_pcm = b"\x00" * 100  # well under STREAM_CHUNK_BYTES
    nvidia_speech._prepare_for_asr = lambda data, filename="", pcm=monkeypatch_pcm: pcm

    results = await collect(instance, path)
    assert [r.text for r in results][-1] == "Nexus is online."
    assert service.chunks == 1


# ------------------------------------------------------------------- the refusals


@sync
async def test_an_empty_result_is_skipped_rather_than_yielded_blank(client: Any) -> None:
    """A blank partial would clear the line the user is reading."""

    def script(config: Any, chunks: int) -> Any:
        yield FakeResponse(FakeResult("", is_final=False))
        yield FakeResponse(FakeResult("   ", is_final=False))
        yield FakeResponse(FakeResult("real words", is_final=True))

    instance, path, _ = client(script)
    results = await collect(instance, path)
    assert [r.text for r in results] == ["real words"]


@sync
async def test_a_result_with_no_alternatives_is_skipped(client: Any) -> None:
    class NoAlternatives:
        alternatives: list[Any] = []
        is_final = False

    def script(config: Any, chunks: int) -> Any:
        yield FakeResponse(NoAlternatives())  # type: ignore[arg-type]
        yield FakeResponse(FakeResult("heard this", is_final=True))

    instance, path, _ = client(script)
    results = await collect(instance, path)
    assert [r.text for r in results] == ["heard this"]


@sync
async def test_a_failure_inside_the_worker_reaches_the_consumer(client: Any) -> None:
    """The bug this design exists to prevent: a caller waiting on a dead stream.

    The generator is drained on a worker thread, so an exception raised there has to
    cross back through the queue. Without that crossing, the caller waits forever --
    a hang reports nothing, which is why this is asserted rather than assumed.
    """

    class Boom(Exception):
        pass

    def script(config: Any, chunks: int) -> Any:
        raise Boom("gRPC channel exploded")
        yield  # pragma: no cover - makes this a generator function

    instance, path, _ = client(script)
    with pytest.raises(ProviderError):
        await collect(instance, path)


@sync
async def test_a_recording_that_decodes_to_no_audio_is_refused(client: Any) -> None:
    instance, path, _ = client(three_step())
    nvidia_speech._prepare_for_asr = lambda data, filename="": b""

    with pytest.raises(ProviderError):
        await collect(instance, path)


@sync
async def test_a_missing_file_is_refused_with_its_path(client: Any, tmp_path: Any) -> None:
    instance, _, _ = client(three_step())
    missing = tmp_path / "not-here.wav"
    with pytest.raises(ProviderError):
        await collect(instance, missing)


# ------------------------------------------------------------ the shape it implies


def test_a_one_shot_transcript_is_final_by_default() -> None:
    """The offline path must not start looking provisional to every caller."""
    plain = Transcript(text="hello", model="m")
    assert plain.is_final is True
    assert plain.to_dict()["is_final"] is True


def test_the_streaming_fields_survive_to_dict() -> None:
    """A consumer cannot tell a partial from a final without these on the wire."""
    payload = Transcript(
        text="half", model="m", is_final=False, stability=0.3
    ).to_dict()
    assert payload["is_final"] is False
    assert payload["stability"] == 0.3
