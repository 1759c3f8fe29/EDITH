"""Speech in and speech out, via Mistral Voxtral (spec §12, §37).

NEXUS is voice-first, so this module is not optional decoration -- it is how the
user talks to the app at all.

It exists on Mistral rather than NVIDIA because of a fact that had to be measured
rather than assumed. The NIM catalogue names speech models (``canary-1b-asr``,
``magpie-tts-multilingual``), and an earlier version of NEXUS routed speech to
them over HTTP paths like ``/v1/asr/nvidia/canary-1b-asr``. Every one of those
paths answers ``404 page not found`` in ``text/plain`` -- byte-identical to what a
deliberately invented path returns, and nothing like the
``application/problem+json`` "Function ... not found for account" that a real but
unentitled model produces. NVIDIA's speech models are served over gRPC/NVCF, not
REST; the hosted ``/models`` listing contains no ASR or TTS id at all (its only
``riva`` entries are text translation). So there was no route to fix -- the whole
surface was absent, and the four endpoints were removed instead of retried.

Mistral, on the same key NEXUS already uses for its main model, does serve both
over ordinary HTTP. Verified end to end before this file was written: synthesised
"Nexus is online and listening." and fed that exact audio back to transcription,
which returned the same sentence.

Two shape details that are easy to get wrong and cost a round trip each:

* Synthesis returns a **JSON envelope** with base64 in ``audio_data`` -- not raw
  audio bytes. Writing the response body straight to a file produces a file
  starting ``{"au`` that no player will touch, and transcription rejects it as
  undecodable.
* ``response_format: "wav"`` is what makes it WAV. The default is MP3, and this
  machine has ``aplay``/``paplay`` but no ffmpeg or mpg123 -- so MP3 would arrive
  unplayable. WAV also lets duration be read straight from the header, which is
  how the UI knows how long a reply will take to speak.
"""

from __future__ import annotations

import base64
import logging
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import secrets
from ..config import settings
from .base import MissingCredential, ProviderError, RequestRejected
from .nim_media import MAX_AUDIO_BYTES, ProbeResult, Speech, Transcript
from .openai_compat import map_http_error

log = logging.getLogger(__name__)

PROVIDER_ID = "mistral"
PROVIDER_NAME = "Mistral"
CREDENTIAL = "MISTRAL_API_KEY"
BASE_URL = "https://api.mistral.ai/v1"

#: Audio understanding. ``voxtral-mini-latest`` is the transcription workhorse;
#: the realtime variants are a different protocol (they reject this endpoint with
#: "only supports realtime transcription") and are deliberately not listed here.
STT_MODEL = "voxtral-mini-latest"

#: There is deliberately no STT fallback. ``voxtral-small-latest`` looks like the
#: obvious one and is *not* usable here: it is an audio-understanding model for the
#: chat surface, and ``/audio/transcriptions`` rejects it outright with
#: ``invalid_model``. Listing it would put a model in the chain that the router
#: retries on every failure and that can never succeed -- worse than having no
#: fallback, because the 400 it earns is correctly classed as transient and so
#: never gets benched.

TTS_MODEL = "voxtral-mini-tts-latest"

#: Voices are named by slug, and the set is account-specific -- ``alloy``/``nova``
#: style names from other vendors all 404 here. ``voices()`` reads the live list;
#: this is only the default when the user has not chosen one, picked because a
#: neutral read is right for an assistant that mostly states facts.
DEFAULT_VOICE = "en_paul_neutral"

#: One synthesis request's worth of text. Long answers are spoken in parts rather
#: than truncated, so this is a chunking boundary, not a limit on what NEXUS says.
MAX_TTS_CHARS = 4_000


def has_credential() -> bool:
    return bool(secrets.get(CREDENTIAL))


def _key() -> str:
    key = secrets.get(CREDENTIAL)
    if not key:
        raise MissingCredential(
            f"No {PROVIDER_NAME} API key is configured. Add one in "
            "Settings -> Providers to enable voice.",
            provider=PROVIDER_ID,
        )
    return key


def _headers(*, json_body: bool) -> dict[str, str]:
    """Auth headers. ``Content-Type`` is omitted for multipart uploads.

    httpx builds the ``multipart/form-data`` content type itself, including the
    boundary. Setting it by hand produces a body the server cannot parse.
    """
    head = {"Authorization": f"Bearer {_key()}"}
    if json_body:
        head["Content-Type"] = "application/json"
    return head


def _wav_duration(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            return round(handle.getnframes() / rate, 3) if rate else None
    except Exception:  # noqa: BLE001 -- a duration we cannot read is not an error
        return None


def _is_timeout(exc: BaseException) -> bool:
    """True for httpx's timeout family, without importing httpx at module scope.

    Matched by class name rather than isinstance so this module stays importable
    when httpx is absent -- the rest of the provider layer defers the import too.
    """
    names = {type(base).__name__ for base in (exc,)} | {
        cls.__name__ for cls in type(exc).__mro__
    }
    return bool(names & {"TimeoutException", "ConnectTimeout", "ReadTimeout",
                         "WriteTimeout", "PoolTimeout", "TimeoutError"})


#: Which models serve which media job. The router consults this instead of the NIM
#: catalogue for Mistral candidates, because "media transport" describes a request
#: shape -- audio bytes rather than chat messages -- and not a single vendor.
_SERVES: dict[str, tuple[str, ...]] = {
    "stt": (STT_MODEL,),
    "tts": (TTS_MODEL,),
}


def serves(job: str) -> tuple[str, ...]:
    return _SERVES.get(job, ())


class MistralAudioClient:
    """Transcription and synthesis over Mistral's HTTP audio endpoints."""

    def __init__(self) -> None:
        self._client: Any = None

    async def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                base_url=BASE_URL,
                # Read timeout is generous: synthesis of a long paragraph and
                # transcription of a long recording both take real seconds, and a
                # premature timeout here reads to the user as "voice is broken".
                timeout=httpx.Timeout(connect=10.0, read=180.0, write=60.0, pool=10.0),
                follow_redirects=False,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def has_credential(self) -> bool:
        return has_credential()

    # --------------------------------------------------------------- voices

    async def voices(self) -> list[dict[str, Any]]:
        """The voices this account can synthesise with.

        Read live rather than hard-coded: the set is per-account (cloned voices
        appear here too), so a baked-in list would eventually name a voice that
        404s and offer none of the ones the user actually has.
        """
        http = await self._http()
        response = await http.get("/audio/voices", headers=_headers(json_body=True))
        if response.status_code >= 400:
            raise map_http_error(
                response.status_code, response.content,
                provider_id=PROVIDER_ID, provider_name=PROVIDER_NAME,
            )
        items = response.json().get("items") or []
        return [
            {
                "slug": item.get("slug"),
                "name": item.get("name"),
                "gender": item.get("gender"),
                "languages": item.get("languages") or [],
                "tags": item.get("tags") or [],
            }
            for item in items
            if item.get("slug")
        ]

    # ---------------------------------------------------------- transcription

    async def transcribe(
        self,
        audio: str | Path | bytes,
        *,
        model: str | None = None,
        language: str | None = None,
    ) -> Transcript:
        """Turn recorded audio into text.

        Accepts a path or raw bytes so the caller can hand over a microphone
        buffer without writing a temporary file first.
        """
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
            filename = "recording.wav"

        if not data:
            raise RequestRejected(
                "The recording is empty, so there is nothing to transcribe.",
                provider=PROVIDER_ID,
            )
        if len(data) > MAX_AUDIO_BYTES:
            raise RequestRejected(
                f"The recording is {len(data) / 1_000_000:.1f} MB, over the "
                f"{MAX_AUDIO_BYTES / 1_000_000:.0f} MB limit for one request. "
                "Record in shorter takes.",
                provider=PROVIDER_ID,
            )

        chosen = model or STT_MODEL
        form: dict[str, str] = {"model": chosen}
        if language:
            form["language"] = language

        http = await self._http()
        started = time.monotonic()
        response = await http.post(
            "/audio/transcriptions",
            headers=_headers(json_body=False),
            files={"file": (filename, data, "audio/wav")},
            data=form,
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        if response.status_code >= 400:
            raise map_http_error(
                response.status_code, response.content,
                provider_id=PROVIDER_ID, provider_name=PROVIDER_NAME, model=chosen,
            )

        body = response.json()
        text = str(body.get("text") or "").strip()
        usage = body.get("usage") or {}
        seconds = usage.get("prompt_audio_seconds")
        return Transcript(
            text=text,
            model=chosen,
            language=body.get("language") or language,
            duration_s=float(seconds) if isinstance(seconds, (int, float)) else None,
            words=len(text.split()),
            latency_ms=latency_ms,
            raw_keys=sorted(body.keys()),
        )

    # ------------------------------------------------------------- synthesis

    async def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        model: str | None = None,
    ) -> Speech:
        """Speak text, writing a playable WAV to the audio cache directory."""
        cleaned = (text or "").strip()
        if not cleaned:
            raise RequestRejected("There is no text to speak.", provider=PROVIDER_ID)
        if len(cleaned) > MAX_TTS_CHARS:
            raise RequestRejected(
                f"The text is {len(cleaned):,} characters, longer than one synthesis "
                f"request can carry ({MAX_TTS_CHARS:,}). Speak it in parts.",
                provider=PROVIDER_ID,
            )

        chosen = model or TTS_MODEL
        payload = {
            "model": chosen,
            "input": cleaned,
            "voice": voice or DEFAULT_VOICE,
            # Without this the endpoint returns MP3, which this machine cannot play.
            "response_format": "wav",
        }

        http = await self._http()
        started = time.monotonic()
        response = await http.post(
            "/audio/speech", headers=_headers(json_body=True), json=payload
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        if response.status_code >= 400:
            raise map_http_error(
                response.status_code, response.content,
                provider_id=PROVIDER_ID, provider_name=PROVIDER_NAME, model=chosen,
            )

        encoded = response.json().get("audio_data")
        if not encoded:
            raise ProviderError(
                f"{PROVIDER_NAME} answered the synthesis request but included no "
                "audio, so there is nothing to play.",
                provider=PROVIDER_ID, model=chosen,
            )
        try:
            audio = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ProviderError(
                f"{PROVIDER_NAME} returned audio that could not be decoded.",
                provider=PROVIDER_ID, model=chosen, detail=str(exc),
            ) from exc

        directory = settings().audio_dir
        directory.mkdir(parents=True, exist_ok=True)
        out = directory / f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-speech.wav"
        out.write_bytes(audio)

        return Speech(
            path=out,
            model=chosen,
            bytes_written=len(audio),
            media_type="audio/wav",
            duration_s=_wav_duration(out),
            latency_ms=latency_ms,
        )

    # ----------------------------------------------------------------- probe

    async def probe_model(self, job: str, model: str) -> ProbeResult:
        """Prove one speech model answers, with the smallest real request.

        Returns the same ``ProbeResult`` the NIM media client does so the router's
        probe can treat both providers identically -- the point of the probe is a
        uniform answer to "can this key call this model", and a second result shape
        would mean a second set of branches everywhere it is consumed.
        """
        if not self.has_credential():
            return ProbeResult(
                job=job, model=model, ok=False,
                message=f"No {PROVIDER_NAME} API key is configured.",
                error_kind="missing_credential",
            )

        started = time.monotonic()
        try:
            if job == "tts":
                speech = await self.synthesize("Ready.", model=model)
                latency = int((time.monotonic() - started) * 1000)
                # Delete the probe's audio: it is a fixed word nobody asked to hear,
                # and leaving one file per probe run slowly fills the cache.
                try:
                    speech.path.unlink(missing_ok=True)
                except OSError:
                    pass
                return ProbeResult(
                    job=job, model=model, ok=True, latency_ms=latency,
                    message=f"Synthesised {speech.bytes_written:,} bytes of playable WAV.",
                )
            if job == "stt":
                # Transcription needs audio, so synthesis supplies it. This makes the
                # STT probe a genuine round trip -- it proves the pair works together,
                # which is the only way voice is actually usable.
                spoken = await self.synthesize("Ready.")
                try:
                    heard = await self.transcribe(spoken.path, model=model)
                finally:
                    try:
                        spoken.path.unlink(missing_ok=True)
                    except OSError:
                        pass
                latency = int((time.monotonic() - started) * 1000)
                if not heard.text:
                    return ProbeResult(
                        job=job, model=model, ok=False, latency_ms=latency,
                        message="The model accepted the audio but returned no text.",
                        error_kind="stream_interrupted",
                    )
                return ProbeResult(
                    job=job, model=model, ok=True, latency_ms=latency,
                    message=f"Transcribed synthesised speech as {heard.text!r}.",
                )
            return ProbeResult(
                job=job, model=model, ok=False,
                message=f"{PROVIDER_NAME} audio does not serve the '{job}' job.",
                error_kind="model_unavailable",
            )
        except ProviderError as exc:
            return ProbeResult(
                job=job, model=model, ok=False, error_kind=exc.kind, message=exc.message,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 -- a probe must never take the app down
            # A timeout is an ordinary transient, not a surprise, so it gets a named
            # kind and a readable sentence instead of a stack trace. Without this it
            # was logged as an unexpected crash with an empty reason, which reads on
            # the Settings screen as "failed, cause unknown".
            if _is_timeout(exc):
                return ProbeResult(
                    job=job, model=model, ok=False, error_kind="timeout",
                    latency_ms=int((time.monotonic() - started) * 1000),
                    message=f"{PROVIDER_NAME} did not answer in time. Recorded as a "
                            "one-off, so the model stays in the chain.",
                )
            log.warning("mistral audio probe of %s raised", model, exc_info=True)
            return ProbeResult(
                job=job, model=model, ok=False, error_kind="provider_error",
                message=str(exc), latency_ms=int((time.monotonic() - started) * 1000),
            )


_CLIENT: MistralAudioClient | None = None


def client() -> MistralAudioClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = MistralAudioClient()
    return _CLIENT
