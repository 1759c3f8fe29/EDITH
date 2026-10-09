"""NVIDIA speech over gRPC: Parakeet hears, Magpie speaks (spec §12, §13, §37).

This module exists because NVIDIA serves speech on a different wire than
everything else it serves. Chat, vision, OCR, embeddings and ranking are HTTP
against ``integrate.api.nvidia.com``; ASR and TTS are gRPC against
``grpc.nvcf.nvidia.com``, addressed by NVCF function id rather than by URL path.
:mod:`nexus.providers.nim_media` cannot carry that -- it builds ``base_url +
path`` and POSTs JSON -- so this is a second client rather than a branch inside
the first one.

It is worth being precise about why, because the previous conclusion in this
codebase was that NIM had no speech at all, and the evidence for *that* was real:
every REST speech path answers ``404 page not found``, and the hosted ``/models``
listing names no ASR or TTS id. Both remain true. What they do not mean is what
was concluded from them. NVCF splits its functions by ``health.protocol`` -- 168
HTTP, 30 gRPC -- and every speech function is gRPC, so the HTTP gateway answers
"no route" for a model that is deployed and working. Same status code as a
deleted model, entirely different fact.

The proof is a closed loop rather than a listing: Magpie synthesised a sentence,
Parakeet transcribed that exact audio, and the words came back the same, in five
languages (hi-IN, es-US, fr-FR, de-DE, ja-JP) at confidence 1.0.

Three contract details are load-bearing and were each found the hard way:

**Voice names carry an uppercase region.** ``Magpie-Multilingual.EN-US.Sofia``
works; the same name lowercased fails. Named speakers exist only for EN-US and
HI-IN -- ``ES-US.Sofia`` and ``FR-FR.Sofia`` do not exist and answer an RPC error
-- while the bare ``Magpie-Multilingual`` works for all twelve languages with the
speaker chosen by ``language_code``. So the bare name is the default and a named
speaker is an opt-in, not a per-language lookup.

**Riva returns raw PCM, not a playable file.** The WAV header is written here.
Getting the sample rate wrong does not raise; it plays back at the wrong speed.

**ASR needs PCM in, and browsers do not send PCM.** A recording arrives as webm
or ogg/opus, which Riva cannot read, so it is transcoded with ffmpeg first. When
ffmpeg is missing the failure says so in those words rather than surfacing a
protobuf error about frame headers.

Everything blocking runs in a worker thread: ``riva.client`` is synchronous, and
calling it directly on the event loop would stall every other request for the
length of a synthesis.
"""

from __future__ import annotations

import asyncio
import io
import shutil
import subprocess
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

from .. import secrets
from ..config import settings
from ..logging_setup import get_logger
from . import nim_catalogue
from .base import (
    MissingCredential,
    NetworkUnreachable,
    ProviderError,
    ProviderTimeout,
    RequestRejected,
)
from .nim_catalogue import NimEndpoint
from .nim_media import MAX_AUDIO_BYTES, MediaUnsupported, ProbeResult, Speech, Transcript

log = get_logger("providers.nvidia_speech")

PROVIDER_ID = "nvidia"
PROVIDER_NAME = "NVIDIA NIM"

#: The speaker used when the caller names none. The bare model name rather than a
#: named voice on purpose: it is the only form that works in all twelve languages
#: (see the module docstring), so it is the one default that cannot mistranslate a
#: language change into an RPC error.
DEFAULT_VOICE = "Magpie-Multilingual"

#: Fallback language when neither the caller nor the settings name one. Magpie
#: requires *some* language code and Parakeet accepts arbitrary tags, so this is
#: only ever a starting point, never a restriction.
DEFAULT_LANGUAGE = "en-US"

#: What ASR is fed. 16 kHz mono is the rate Riva's recognisers are built around;
#: the audio is resampled to it on the way in rather than hoping the recording
#: already matches.
ASR_SAMPLE_RATE = 16_000

#: How much PCM is pushed into Riva per streaming step. 8 KB at 16 kHz mono is
#: 4096 samples -- a quarter of a second of audio. Small enough that a partial
#: lands while the speaker is still talking, large enough that the per-chunk gRPC
#: overhead does not dominate a short recording.
STREAM_CHUNK_BYTES = 8_192

#: One synthesis request's ceiling. Long answers are spoken in segments by
#: :mod:`nexus.ai.voice_stream`, so this bounds a segment, not an answer.
MAX_TTS_CHARS = 4_000

#: Raw PCM is 2 bytes per sample per channel, so an hour at 44.1 kHz is ~317 MB.
#: The cap is on the *decoded* size for the same reason the upload cap exists: to
#: fail with a sentence about length rather than an allocation.
MAX_PCM_BYTES = 200_000_000

#: Beyond this a call is abandoned. Synthesis of a sentence takes ~1-2s and
#: transcription of a short utterance about the same; a minute means something is
#: wrong on the other end, and a request that hangs forever is worse than one that
#: fails with a reason.
CALL_TIMEOUT_S = 60.0

#: Audio containers ffmpeg is asked to decode. WAV skips ffmpeg entirely -- it is
#: read with the standard library -- so it is deliberately not in this list.
_TRANSCODE_SUFFIXES = frozenset(
    {".webm", ".ogg", ".opus", ".mp3", ".m4a", ".mp4", ".aac", ".flac", ".wma"}
)


class SpeechToolMissing(ProviderError):
    """ffmpeg is needed to read this recording and is not installed."""

    kind = "speech_tool_missing"
    retryable = False
    fall_back = True


def has_credential() -> bool:
    return bool(secrets.get("NVIDIA_API_KEY"))


def serves(job: str) -> tuple[str, ...]:
    """The model ids this provider uses for a job.

    Read from the catalogue rather than hard-coded, so adding a speech model is a
    catalogue edit. Mirrors :func:`nexus.providers.mistral_audio.serves` because
    the router asks both the same question.
    """
    if job not in ("stt", "tts"):
        return ()
    return tuple(
        e.model
        for e in nim_catalogue.for_job(job)  # type: ignore[arg-type]
        if e.protocol == "grpc"
    )


def _key() -> str:
    key = secrets.get("NVIDIA_API_KEY")
    if not key:
        raise MissingCredential(
            "No NVIDIA API key is configured, so speech is unavailable. Add one in "
            "Settings -> Providers.",
            provider=PROVIDER_ID,
        )
    return key


def _endpoint(job: str, model: str | None) -> NimEndpoint:
    """The speech endpoint for a job, or a refusal naming what went wrong."""
    if model:
        endpoint = nim_catalogue.BY_MODEL.get(model)
        if endpoint is None:
            raise MediaUnsupported(
                f"'{model}' is not in the NIM specialist catalogue.",
                provider=PROVIDER_ID,
                model=model,
            )
    else:
        endpoint = nim_catalogue.preferred(job)  # type: ignore[arg-type]
        if endpoint is None:
            raise MediaUnsupported(
                f"NEXUS has no NVIDIA model configured for {job}.", provider=PROVIDER_ID
            )

    wanted = "asr" if job == "stt" else "tts"
    if endpoint.modality != wanted:
        raise MediaUnsupported(
            f"{endpoint.label} is not a {'speech recognition' if job == 'stt' else 'speech synthesis'} model.",
            provider=PROVIDER_ID,
            model=endpoint.model,
        )
    if endpoint.protocol != "grpc":
        # Routed to the wrong client. Worth its own message: the HTTP media client
        # would answer this with a 404 that reads like a missing model.
        raise MediaUnsupported(
            f"{endpoint.label} is an HTTP endpoint and cannot be called over gRPC. "
            "This is a routing mistake in NEXUS, not a problem with your key.",
            provider=PROVIDER_ID,
            model=endpoint.model,
        )
    return endpoint


# ------------------------------------------------------------------ audio shapes


def _pcm_from_wav(data: bytes) -> tuple[bytes, int, int]:
    """Unwrap a WAV into ``(pcm, sample_rate, channels)``.

    Raises :class:`ValueError` when the bytes are not a WAV NEXUS can read, which
    the caller turns into "hand this to ffmpeg instead".
    """
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"{w.getsampwidth() * 8}-bit WAV, and Riva takes 16-bit")
        if w.getcomptype() not in ("NONE",):
            raise ValueError(f"compressed WAV ({w.getcompname()})")
        return w.readframes(w.getnframes()), w.getframerate(), w.getnchannels()


def _wav_of(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """Wrap raw PCM in a WAV header so it can be played or written to disk."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise SpeechToolMissing(
            "That recording is not a WAV file, and ffmpeg -- which NEXUS uses to "
            "convert it -- is not installed. Install ffmpeg, or record in WAV.",
            provider=PROVIDER_ID,
        )
    return path


def _transcode(data: bytes, *, rate: int) -> bytes:
    """Decode any container ffmpeg knows into mono 16-bit PCM at ``rate``.

    Piped rather than written to a temporary file: the bytes are already in
    memory, and a temp file for a microphone buffer is a file that outlives a
    crash.
    """
    proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell, path from which()
        [
            _ffmpeg(), "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ac", "1", "-ar", str(rate),
            "pipe:1",
        ],
        input=data,
        capture_output=True,
        timeout=CALL_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()[:400]
        raise RequestRejected(
            "That recording could not be decoded. It may be truncated or in a "
            "format NEXUS cannot read.",
            provider=PROVIDER_ID,
            detail=detail,
        )
    return proc.stdout


def _resample(pcm: bytes, *, from_rate: int, to_rate: int, channels: int) -> bytes:
    """Convert PCM to mono at ``to_rate``, via ffmpeg.

    Used when a WAV is readable but at the wrong rate. ``audioop`` would do this
    without a subprocess, but it was removed from the standard library in 3.13 and
    ffmpeg is already a dependency of the path above.
    """
    if from_rate == to_rate and channels == 1:
        return pcm
    proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell, path from which()
        [
            _ffmpeg(), "-hide_banner", "-loglevel", "error",
            "-f", "s16le", "-ar", str(from_rate), "-ac", str(channels),
            "-i", "pipe:0",
            "-f", "s16le", "-ac", "1", "-ar", str(to_rate),
            "pipe:1",
        ],
        input=pcm,
        capture_output=True,
        timeout=CALL_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()[:400]
        raise RequestRejected(
            "That recording could not be converted to the format speech "
            "recognition needs.",
            provider=PROVIDER_ID,
            detail=detail,
        )
    return proc.stdout


def _prepare_for_asr(data: bytes, *, filename: str = "") -> bytes:
    """Turn whatever the caller sent into mono 16 kHz PCM.

    WAV is unwrapped in-process and only resampled if it has to be; everything
    else goes through ffmpeg. The suffix is a hint, not the decision -- a ``.wav``
    that is really opus fails the header read and falls through to ffmpeg, which
    is the same path it would have taken if it had been named honestly.
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in _TRANSCODE_SUFFIXES:
        try:
            pcm, rate, channels = _pcm_from_wav(data)
        except (wave.Error, ValueError, EOFError) as exc:
            log.debug("not a readable WAV (%s); using ffmpeg", exc)
        else:
            if len(pcm) > MAX_PCM_BYTES:
                raise RequestRejected(
                    "That recording is too long to transcribe in one request. "
                    "Split it into shorter pieces.",
                    provider=PROVIDER_ID,
                )
            return _resample(
                pcm, from_rate=rate, to_rate=ASR_SAMPLE_RATE, channels=channels
            )
    return _transcode(data, rate=ASR_SAMPLE_RATE)


# ------------------------------------------------------------------- the client


class NvidiaSpeechClient:
    """Riva gRPC clients for Parakeet and Magpie, built once and reused.

    One channel per function id, cached: a gRPC channel is a connection pool and
    rebuilding it per request would pay the TLS handshake on every sentence of a
    spoken answer.
    """

    def __init__(self) -> None:
        #: function id -> (riva service, the key it was built with). The key is
        #: kept so that changing it in Settings rebuilds the client instead of
        #: silently using the old credential for the life of the process.
        self._services: dict[str, tuple[Any, str]] = {}

    def has_credential(self) -> bool:
        return has_credential()

    def available(self) -> bool:
        """Whether the gRPC client library is importable.

        Separate from :meth:`has_credential` because they fail differently: a
        missing key is fixed in Settings, a missing library is fixed with pip, and
        telling the user the wrong one wastes their time.
        """
        try:
            import riva.client  # noqa: F401
        except ImportError:
            return False
        return True

    def _service(self, endpoint: NimEndpoint, kind: str) -> Any:
        key = _key()
        cached = self._services.get(endpoint.function_id)
        if cached is not None and cached[1] == key:
            return cached[0]

        try:
            import riva.client as riva
        except ImportError as exc:
            raise SpeechToolMissing(
                "NVIDIA speech needs the 'nvidia-riva-client' package, which is "
                "not installed. Install it, or choose a different speech model in "
                "Settings.",
                provider=PROVIDER_ID,
                model=endpoint.model,
                detail=str(exc),
            ) from exc

        # The function id travels as call metadata -- this is the whole of how a
        # gRPC function is addressed, since there is no URL path to put it in.
        auth = riva.Auth(
            None,
            True,
            endpoint.base_url,
            [
                ["function-id", endpoint.function_id],
                ["authorization", f"Bearer {key}"],
            ],
        )
        service = (
            riva.SpeechSynthesisService(auth)
            if kind == "tts"
            else riva.ASRService(auth)
        )
        self._services[endpoint.function_id] = (service, key)
        return service

    async def _call(self, fn: Any, *args: Any, endpoint: NimEndpoint, **kwargs: Any) -> Any:
        """Run one blocking Riva call off the event loop, with errors translated."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(fn, *args, **kwargs), timeout=CALL_TIMEOUT_S
            )
        except asyncio.TimeoutError as exc:
            raise ProviderTimeout(
                f"{endpoint.label} did not answer within {CALL_TIMEOUT_S:.0f} seconds.",
                provider=PROVIDER_ID,
                model=endpoint.model,
            ) from exc
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 -- grpc errors are not a NEXUS type
            raise _translate(exc, endpoint) from exc

    # ------------------------------------------------------------ speech to text

    async def transcribe_stream(
        self,
        audio: str | Path | bytes,
        *,
        model: str | None = None,
        language: str | None = None,
        chunk_bytes: int = STREAM_CHUNK_BYTES,
    ) -> AsyncIterator[Transcript]:
        """Transcribe with live partial results, yielding as they firm up.

        The difference from :meth:`transcribe` is the whole reason the voice
        audits kept recording "hearing is per-utterance": this yields a
        ``Transcript`` repeatedly for one recording, with ``is_final=False`` while
        the words may still change and ``is_final=True`` once Riva has committed
        them. A caller can therefore show the sentence *forming* rather than
        waiting for the speaker to stop.

        Two things make this honest rather than merely newer. Partials are
        **replacements, not appends** -- a caller must show the latest one, never
        concatenate them, because Riva revises as more audio arrives and joined
        partials read as the machine repeatedly changing its mind. And a partial
        carries its ``stability``, so a UI that wants to avoid flicker can hold
        back anything below a threshold instead of guessing.

        The whole recording is still sent as one stream: this is streaming
        *recognition* over a finished clip, not a live microphone feed. A live feed
        needs the microphone held open and pushed into ``audio_chunks`` as it
        arrives, which is a change to the capture path rather than to this client.
        """
        data = await asyncio.to_thread(self._read_audio, audio)
        endpoint = _endpoint("stt", model)
        spoken = (language or "").strip() or DEFAULT_LANGUAGE
        pcm = await asyncio.to_thread(_prepare_for_asr, data, filename="")

        if not pcm:
            raise RequestRejected(
                "That recording decoded to no audio at all.", provider=PROVIDER_ID
            )

        service = self._service(endpoint, "asr")
        import riva.client as riva

        config = riva.StreamingRecognitionConfig(
            config=riva.RecognitionConfig(
                encoding=riva.AudioEncoding.LINEAR_PCM,
                language_code=spoken,
                sample_rate_hertz=ASR_SAMPLE_RATE,
                audio_channel_count=1,
                max_alternatives=1,
                enable_automatic_punctuation=True,
            ),
            # The flag this whole method exists for. Without it Riva answers once,
            # at the end, and the method would be a slower spelling of transcribe().
            interim_results=True,
        )

        chunks = [
            pcm[offset : offset + chunk_bytes]
            for offset in range(0, len(pcm), chunk_bytes)
        ]

        started = time.monotonic()
        # Riva's generator is a blocking gRPC iterator. It is drained on a worker
        # thread and handed back through a queue, because iterating it on the event
        # loop would stall every other task for the length of the recording --
        # including the SSE stream the transcript is being shown in.
        queue: asyncio.Queue[Any] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        sentinel = object()

        def drain() -> None:
            try:
                for response in service.streaming_response_generator(
                    audio_chunks=iter(chunks), streaming_config=config
                ):
                    loop.call_soon_threadsafe(queue.put_nowait, response)
            except BaseException as exc:  # noqa: BLE001 - re-raised on the consumer
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, sentinel)

        worker = asyncio.create_task(asyncio.to_thread(drain))
        try:
            while True:
                item = await queue.get()
                if item is sentinel:
                    break
                if isinstance(item, BaseException):
                    # ``_translate`` is module-level, like ``transcribe`` uses it.
                    raise _translate(item, endpoint)
                for result in getattr(item, "results", []) or []:
                    alternatives = getattr(result, "alternatives", None)
                    if not alternatives:
                        continue
                    text = (alternatives[0].transcript or "").strip()
                    if not text:
                        continue
                    yield Transcript(
                        text=text,
                        model=endpoint.model,
                        language=spoken,
                        is_final=bool(getattr(result, "is_final", False)),
                        stability=float(getattr(result, "stability", 0.0) or 0.0),
                        latency_ms=int((time.monotonic() - started) * 1000),
                    )
        finally:
            # A consumer that stops early (the user interrupted) must not leave the
            # worker draining a stream nobody reads.
            if not worker.done():
                worker.cancel()

    def _read_audio(self, audio: str | Path | bytes) -> bytes:
        """Path-or-bytes to bytes, with the same refusals ``transcribe`` makes."""
        if isinstance(audio, (str, Path)):
            path = Path(audio).expanduser()
            if not path.is_file():
                raise RequestRejected(
                    f"There is no audio file at {path}.", provider=PROVIDER_ID
                )
            data = path.read_bytes()
        else:
            data = bytes(audio)

        if not data:
            raise RequestRejected(
                "That recording is empty, so there is nothing to transcribe.",
                provider=PROVIDER_ID,
            )
        if len(data) > MAX_AUDIO_BYTES:
            raise RequestRejected(
                f"That recording is {len(data) / 1_000_000:.1f} MB, above the "
                f"{MAX_AUDIO_BYTES // 1_000_000} MB NEXUS will send. Split it into "
                "shorter pieces.",
                provider=PROVIDER_ID,
            )
        return data

    async def transcribe(
        self,
        audio: str | Path | bytes,
        *,
        model: str | None = None,
        language: str | None = None,
    ) -> Transcript:
        """Turn recorded audio into text.

        Accepts a path or raw bytes, matching
        :meth:`nexus.providers.mistral_audio.MistralAudioClient.transcribe`, so the
        two are interchangeable behind a routing decision.
        """
        filename = ""
        if isinstance(audio, (str, Path)):
            path = Path(audio).expanduser()
            if not path.is_file():
                raise RequestRejected(
                    f"There is no audio file at {path}.", provider=PROVIDER_ID
                )
            data = path.read_bytes()
            filename = path.name
        else:
            data = bytes(audio)

        if not data:
            raise RequestRejected(
                "That recording is empty, so there is nothing to transcribe.",
                provider=PROVIDER_ID,
            )
        if len(data) > MAX_AUDIO_BYTES:
            raise RequestRejected(
                f"That recording is {len(data) / 1_000_000:.1f} MB, above the "
                f"{MAX_AUDIO_BYTES // 1_000_000} MB NEXUS will send. Split it into "
                "shorter pieces.",
                provider=PROVIDER_ID,
            )

        endpoint = _endpoint("stt", model)
        # No whitelist check. Parakeet reports its languages as a *pattern*
        # rather than a list -- it is a universal multilingual model -- so
        # refusing a tag that is not in a catalogue tuple would be inventing a
        # restriction the model does not have.
        spoken = (language or "").strip() or DEFAULT_LANGUAGE

        pcm = await asyncio.to_thread(_prepare_for_asr, data, filename=filename)
        if not pcm:
            raise RequestRejected(
                "That recording decoded to no audio at all.", provider=PROVIDER_ID
            )

        # Service first: it is what turns a missing riva package into a sentence
        # about installing it. Importing before that call would raise a bare
        # ImportError from here instead, past the translation.
        service = self._service(endpoint, "asr")
        import riva.client as riva

        config = riva.RecognitionConfig(
            encoding=riva.AudioEncoding.LINEAR_PCM,
            language_code=spoken,
            sample_rate_hertz=ASR_SAMPLE_RATE,
            audio_channel_count=1,
            max_alternatives=1,
            enable_automatic_punctuation=True,
        )

        started = time.monotonic()
        response = await self._call(
            service.offline_recognize,
            _wav_of(pcm, ASR_SAMPLE_RATE),
            config,
            endpoint=endpoint,
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        pieces: list[str] = []
        for result in getattr(response, "results", []) or []:
            alternatives = getattr(result, "alternatives", None)
            if alternatives:
                pieces.append(alternatives[0].transcript)
        text = " ".join(p.strip() for p in pieces if p and p.strip()).strip()

        return Transcript(
            text=text,
            model=endpoint.model,
            language=spoken,
            duration_s=round(len(pcm) / (ASR_SAMPLE_RATE * 2), 3) or None,
            words=len(text.split()),
            latency_ms=latency_ms,
            raw_keys=["results"],
        )

    # ------------------------------------------------------------ text to speech

    async def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        language: str | None = None,
        model: str | None = None,
    ) -> Speech:
        """Speak text, writing a playable WAV into the audio cache."""
        cleaned = (text or "").strip()
        if not cleaned:
            raise RequestRejected("There is no text to speak.", provider=PROVIDER_ID)
        if len(cleaned) > MAX_TTS_CHARS:
            raise RequestRejected(
                f"The text is {len(cleaned):,} characters, longer than one synthesis "
                f"request can carry ({MAX_TTS_CHARS:,}). Speak it in parts.",
                provider=PROVIDER_ID,
            )

        endpoint = _endpoint("tts", model)
        spoken = (language or "").strip() or (
            endpoint.languages[0] if endpoint.languages else DEFAULT_LANGUAGE
        )
        if endpoint.languages and spoken not in endpoint.languages:
            raise RequestRejected(
                f"{endpoint.label} speaks {', '.join(endpoint.languages)}. It cannot "
                f"speak '{spoken}'.",
                provider=PROVIDER_ID,
                model=endpoint.model,
            )

        chosen = (voice or "").strip() or DEFAULT_VOICE
        if endpoint.voices and chosen not in endpoint.voices:
            raise RequestRejected(
                f"'{chosen}' is not a voice {endpoint.label} has. Available: "
                + ", ".join(endpoint.voices),
                provider=PROVIDER_ID,
                model=endpoint.model,
            )
        # A named speaker exists only for the language it names, so pairing
        # EN-US.Sofia with fr-FR is an RPC error rather than an accent. Caught
        # here, where the reason can be explained, instead of at the wire.
        region = _region_of(chosen)
        if region and region.lower() != spoken.lower():
            raise RequestRejected(
                f"The voice '{chosen}' only speaks {region}, but this was asked for "
                f"in {spoken}. Use '{DEFAULT_VOICE}', which speaks every language "
                f"{endpoint.label} supports.",
                provider=PROVIDER_ID,
                model=endpoint.model,
            )

        # Service first -- see the note in ``transcribe``.
        service = self._service(endpoint, "tts")
        import riva.client as riva

        rate = endpoint.sample_rate or 44_100

        started = time.monotonic()
        response = await self._call(
            service.synthesize,
            endpoint=endpoint,
            text=cleaned,
            voice_name=chosen,
            language_code=spoken,
            sample_rate_hz=rate,
            encoding=riva.AudioEncoding.LINEAR_PCM,
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        pcm = getattr(response, "audio", b"") or b""
        if not pcm:
            raise ProviderError(
                f"{endpoint.label} answered with no audio, so there is nothing to play.",
                provider=PROVIDER_ID,
                model=endpoint.model,
            )

        # Riva hands back raw PCM. The header is written here, at the rate that
        # was actually requested -- a mismatch does not raise, it plays fast.
        audio = _wav_of(pcm, rate)
        directory = settings().audio_dir
        directory.mkdir(parents=True, exist_ok=True)
        out = directory / f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-speech.wav"
        out.write_bytes(audio)

        return Speech(
            path=out,
            model=endpoint.model,
            bytes_written=len(audio),
            media_type="audio/wav",
            duration_s=round(len(pcm) / (rate * 2), 3) or None,
            latency_ms=latency_ms,
        )

    # ------------------------------------------------------------------- voices

    def voices(self, model: str | None = None) -> list[dict[str, Any]]:
        """The speakers this model has, for the voice picker.

        Local rather than a network call, and that is a real difference from
        Mistral's ``/audio/voices``: Riva has no list-voices RPC on this
        deployment, so the catalogue's tuple is the source. It was measured, not
        copied from documentation -- each name below was called.
        """
        endpoint = _endpoint("tts", model)
        out: list[dict[str, Any]] = []
        for name in endpoint.voices:
            region = _region_of(name)
            out.append(
                {
                    "slug": name,
                    "name": name.rsplit(".", 1)[-1] if region else "Default",
                    "gender": None,
                    # The bare name speaks everything; a named speaker speaks the
                    # one language its name carries.
                    "languages": [region] if region else list(endpoint.languages),
                    "tags": ["default"] if not region else [],
                }
            )
        return out

    # -------------------------------------------------------------------- probe

    async def probe(self, jobs: tuple[str, ...] = ("stt", "tts")) -> list[ProbeResult]:
        """Call each speech model once and report what actually works."""
        results: list[ProbeResult] = []
        for job in jobs:
            started = time.monotonic()
            try:
                endpoint = _endpoint(job, None)
            except ProviderError as exc:
                results.append(
                    ProbeResult(
                        job=job, model="", ok=False, message=exc.message,
                        error_kind=exc.kind,
                    )
                )
                continue
            try:
                await self._probe_one(job, endpoint)
            except ProviderError as exc:
                results.append(
                    ProbeResult(
                        job=job, model=endpoint.model, ok=False, message=exc.message,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        error_kind=exc.kind,
                    )
                )
                continue
            results.append(
                ProbeResult(
                    job=job, model=endpoint.model, ok=True,
                    message=f"{endpoint.label} answered a live request.",
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            )
        return results

    async def probe_model(self, job: str, model: str) -> ProbeResult:
        """Prove one named speech model answers, with the smallest real request.

        Same shape and purpose as :meth:`mistral_audio.Client.probe_model` and
        ``nim_media``'s: the router probes every candidate in a chain by name, and
        before this existed a chain led by Parakeet or Magpie went to the HTTP media
        client -- which POSTs to a ``grpc://`` URL, a request that cannot succeed, so
        every NVIDIA speech model was benched as unreachable on a key that could
        call them.
        """
        started = time.monotonic()
        try:
            endpoint = _endpoint(job, model)
        except ProviderError as exc:
            return ProbeResult(
                job=job, model=model, ok=False, message=exc.message,
                error_kind=exc.kind,
            )
        try:
            await self._probe_one(job, endpoint)
        except ProviderError as exc:
            return ProbeResult(
                job=job, model=model, ok=False, message=exc.message,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_kind=exc.kind,
            )
        return ProbeResult(
            job=job, model=model, ok=True,
            message=f"{endpoint.label} answered a live request.",
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    async def _probe_one(self, job: str, endpoint: NimEndpoint) -> None:
        """The smallest real call that proves a speech model is reachable.

        Both probes are genuine round trips rather than empty-payload pokes. There
        is no cheaper option here: gRPC has no equivalent of "400 proves the route
        exists", because a malformed request and an unreachable function surface
        the same way. So TTS speaks one word, and STT transcribes that word.
        """
        if job == "tts":
            speech = await self.synthesize("Ready.", model=endpoint.model)
            speech.path.unlink(missing_ok=True)
            return
        if job == "stt":
            # A second of silence is a valid recording and a valid transcript --
            # empty text is the *expected* answer, and it still proves the round
            # trip, because an unreachable function raises instead.
            silence = _wav_of(b"\x00\x00" * ASR_SAMPLE_RATE, ASR_SAMPLE_RATE)
            await self.transcribe(silence, model=endpoint.model)
            return
        raise MediaUnsupported(f"No probe defined for {job}.", provider=PROVIDER_ID)

    async def close(self) -> None:
        """Drop the cached channels.

        ``riva.client`` owns the channel and exposes no close, so this releases
        the references and lets garbage collection take the sockets. Present so
        shutdown has one shape across providers.
        """
        self._services.clear()


def _region_of(voice: str) -> str:
    """The language a named speaker is bound to, or '' for the bare model name.

    ``Magpie-Multilingual.EN-US.Sofia`` -> ``en-US``; ``Magpie-Multilingual`` ->
    ``''``. Lowercased on the way out because the catalogue's language tags are
    BCP-47 (``en-US``) while the voice name carries the region uppercased
    (``EN-US``), and comparing the two without normalising is a bug that only
    shows up in the language the developer did not test.
    """
    parts = voice.split(".")
    if len(parts) < 2:
        return ""
    region = parts[1]
    if "-" not in region:
        return ""
    language, _, country = region.partition("-")
    return f"{language.lower()}-{country.upper()}"


def _translate(exc: Exception, endpoint: NimEndpoint) -> ProviderError:
    """Turn a gRPC failure into an error that names a cause and a fix.

    The raw exception is a wall of protobuf status text. What the user needs is
    which of four things went wrong: the key, the entitlement, the request, or the
    network.
    """
    detail = str(exc)
    code = getattr(exc, "code", None)
    name = ""
    if callable(code):
        try:
            name = str(code().name)
        except Exception:  # noqa: BLE001 -- diagnosis must not raise
            name = ""

    if name in ("UNAUTHENTICATED", "PERMISSION_DENIED"):
        return RequestRejected(
            f"NVIDIA refused the key for {endpoint.label}. Check it in "
            "Settings -> Providers.",
            provider=PROVIDER_ID, model=endpoint.model, detail=detail[:600],
        )
    if name == "NOT_FOUND":
        return MediaUnsupported(
            f"NVIDIA does not serve {endpoint.label} to this account. Enable it on "
            "build.nvidia.com, or choose a different speech model in Settings.",
            provider=PROVIDER_ID, model=endpoint.model, detail=detail[:600],
        )
    if name in ("UNAVAILABLE", "DEADLINE_EXCEEDED"):
        return NetworkUnreachable(
            f"Could not reach {PROVIDER_NAME} for {endpoint.label}. The computer may "
            "be offline, or the service may be busy.",
            provider=PROVIDER_ID, model=endpoint.model, detail=detail[:600],
        )
    if name in ("INVALID_ARGUMENT", "OUT_OF_RANGE"):
        return RequestRejected(
            f"{endpoint.label} refused the request. The voice or language may not be "
            "one it has.",
            provider=PROVIDER_ID, model=endpoint.model, detail=detail[:600],
        )
    if name == "RESOURCE_EXHAUSTED":
        error = ProviderError(
            f"{endpoint.label} is over capacity right now. Trying again shortly "
            "usually works.",
            provider=PROVIDER_ID, model=endpoint.model, detail=detail[:600],
        )
        error.retryable = True
        return error
    return ProviderError(
        f"{endpoint.label} failed: {type(exc).__name__}.",
        provider=PROVIDER_ID, model=endpoint.model, detail=detail[:600],
    )


_CLIENT: NvidiaSpeechClient | None = None


def client() -> NvidiaSpeechClient:
    """The process-wide speech client. One channel pool, not one per call."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = NvidiaSpeechClient()
    return _CLIENT
