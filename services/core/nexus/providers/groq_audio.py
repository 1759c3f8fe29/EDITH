"""Groq speech: Whisper for hearing (spec §6, §7, §12).

This exists because of a measurement, not a preference. Transcribing the same
sentence on this machine:

    Groq   whisper-large-v3-turbo     0.55 s
    NVIDIA parakeet-1-1b-rnnt         7.87 s

Fourteen times faster for an identical transcript. Microsoft's rule for NEXUS is
"use whichever is actually faster", so Groq heads the ``stt`` chain and this module
is what makes that possible.

Three things are worth knowing about this client, and the first two are traps.

**Groq sits behind Cloudflare, which rejects library User-Agents.** The default
``urllib`` UA gets ``403 error code: 1010`` -- which looks exactly like an expired
key and is not one. All four keys failed identically until a browser UA was sent.
So this client always sends one, and a 403 from here is reported as *what it is*
rather than as an authentication problem.

**The endpoint is multipart, not JSON.** ``/audio/transcriptions`` takes a file
part and a model part; sending a JSON body with base64 gives a 400 that says
nothing useful. The multipart is built by hand rather than through a library so the
one place that composes it is readable and testable.

**It does speech-to-text only.** Groq has no TTS endpoint, so speaking stays with
NVIDIA Magpie and Mistral Voxtral. That asymmetry is real and the ``tts`` chain
does not mention Groq, which is the honest way to represent a capability that is
not there rather than a candidate that can only fail.

Rotation across accounts is inherited: ``secrets.pool("GROQ_API_KEY")`` gathers
``GROQ_API_KEY``, ``_1``, ``_2`` ... and each request takes the next, so one
account's rate limit is not the ceiling for the install.

**Groq Whisper expects simple language codes.** Whisper's language list uses
primary subtags (``hi``, ``en``, ``es``) not BCP-47 region tags (``hi-IN``,
``en-US``, ``es-US``). A request with ``hi-IN`` gets a 400 "unsupported
language" even though the model speaks Hindi perfectly. This module normalizes
any incoming BCP-47 tag to its primary subtag before sending.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .. import secrets
from ..logging_setup import get_logger
from .base import (
    MissingCredential,
    ProviderError,
    ProviderTimeout,
    RequestRejected,
)
from .nim_media import Transcript

log = get_logger("providers.groq_audio")

PROVIDER_ID = "groq"
BASE_URL = "https://api.groq.com/openai/v1"

#: Which job each model serves. One entry, because Groq does not do text-to-speech.
_MODELS: dict[str, tuple[str, ...]] = {
    "whisper-large-v3-turbo": ("stt",),
    "whisper-large-v3": ("stt",),
}

DEFAULT_MODEL = "whisper-large-v3-turbo"

#: Whisper's own ceiling. A longer file is rejected by the API, so refusing here
#: with a sentence beats uploading 40 MB to be told the same thing.
MAX_AUDIO_BYTES = 25_000_000

CALL_TIMEOUT_S = 120.0

#: Groq's edge rejects requests whose User-Agent looks like a library. The same
#: browser string the browser tool and the Groq chat provider send, for the same
#: reason -- and the reason it is duplicated rather than imported is that this
#: module must not depend on the chat provider to make a speech call.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


class GroqAudioClient:
    """Speech-to-text against Groq's OpenAI-compatible audio endpoint."""

    def __init__(self) -> None:
        self._cursor = 0

    # ------------------------------------------------------------- credentials

    def has_credential(self) -> bool:
        return bool(secrets.pool("GROQ_API_KEY"))

    def account_count(self) -> int:
        return len(secrets.pool("GROQ_API_KEY"))

    def _key(self) -> str:
        keys = secrets.pool("GROQ_API_KEY")
        if not keys:
            raise MissingCredential(
                "No Groq API key is configured, so voice is unavailable. Add one in "
                "Settings -> Providers.",
                provider=PROVIDER_ID,
            )
        key = keys[self._cursor % len(keys)]
        self._cursor += 1
        return key

    # ------------------------------------------------------------------ surface

    def serves(self, job: str) -> bool:
        """Whether any Groq model does this job. The router asks before choosing."""
        return any(job in jobs for jobs in _MODELS.values())

    def models_for(self, job: str) -> tuple[str, ...]:
        return tuple(name for name, jobs in _MODELS.items() if job in jobs)

    def available(self) -> bool:
        """Present and credentialed. Matches the shape the NIM clients expose."""
        return self.has_credential()

    # ------------------------------------------------------------------- speak

    async def transcribe(
        self,
        audio: str | Path | bytes,
        *,
        model: str | None = None,
        language: str | None = None,
    ) -> Transcript:
        """Turn recorded audio into text. Mirrors the other clients' signature.

        Same ``(audio, model, language)`` shape as ``nvidia_speech`` and
        ``mistral_audio``, so the speech door can call any of the three without a
        branch -- which is the whole point of a door.
        """
        data, filename = _read(audio)
        chosen = (model or DEFAULT_MODEL).strip()
        if chosen not in _MODELS:
            raise RequestRejected(
                f"'{chosen}' is not a Groq speech model NEXUS knows. It offers "
                f"{', '.join(sorted(_MODELS))}.",
                provider=PROVIDER_ID,
                model=chosen,
            )
        if len(data) > MAX_AUDIO_BYTES:
            raise RequestRejected(
                f"That recording is {len(data) / 1_000_000:.1f} MB, above the "
                f"{MAX_AUDIO_BYTES // 1_000_000} MB limit. Split it into shorter pieces.",
                provider=PROVIDER_ID,
                model=chosen,
            )

        # Groq Whisper expects simple language codes (e.g., "hi" not "hi-IN"),
        # so normalize BCP-47 tags to their primary subtag.
        spoken = _normalize_language(language)

        body, content_type = _multipart(
            data, filename=filename, model=chosen, language=spoken
        )
        payload = await asyncio.to_thread(self._post, body, content_type)

        text = str(payload.get("text") or "").strip()
        return Transcript(
            text=text,
            model=chosen,
            language=language or None,
            # Whisper does not report a language on this endpoint; leaving it None
            # rather than echoing the request would be less honest, and echoing it
            # would claim a detection that did not happen.
            duration_s=payload.get("duration"),
            words=len(text.split()),
            latency_ms=None,
            raw_keys=sorted(payload.keys()),
        )

    # -------------------------------------------------------------------- wire

    def _post(self, body: bytes, content_type: str) -> dict[str, Any]:
        """One blocking POST, off the event loop, with Groq's errors translated."""
        request = urllib.request.Request(
            f"{BASE_URL}/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {self._key()}",
                "Content-Type": content_type,
                # Load-bearing: see the module docstring. Without it this endpoint
                # answers 403 "error code: 1010" for every key.
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=CALL_TIMEOUT_S) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            raise _translate(exc.code, raw) from exc
        except urllib.error.URLError as exc:
            raise ProviderTimeout(
                f"Groq could not be reached: {getattr(exc, 'reason', exc)}. Check "
                "that this computer is online.",
                provider=PROVIDER_ID,
            ) from exc
        except TimeoutError as exc:
            raise ProviderTimeout(
                f"Groq did not answer within {CALL_TIMEOUT_S:.0f} seconds.",
                provider=PROVIDER_ID,
            ) from exc


def _read(audio: str | Path | bytes) -> tuple[bytes, str]:
    """Path-or-bytes to (bytes, filename), with the refusals the other clients make."""
    if isinstance(audio, (str, Path)):
        path = Path(audio).expanduser()
        if not path.is_file():
            raise RequestRejected(
                f"There is no audio file at {path}.", provider=PROVIDER_ID
            )
        return path.read_bytes(), path.name
    data = bytes(audio)
    if not data:
        raise RequestRejected(
            "That recording is empty, so there is nothing to transcribe.",
            provider=PROVIDER_ID,
        )
    # A synthetic name: the API needs a filename to decide a decoder from, and
    # "audio.wav" is a better guess than an empty string for bytes from a
    # microphone that were already normalised somewhere upstream.
    return data, "audio.wav"


def _multipart(
    data: bytes, *, filename: str, model: str, language: str | None
) -> tuple[bytes, str]:
    """Compose the multipart body by hand. Returns (body, content-type).

    Hand-built rather than via ``requests`` because it is fifteen lines, it needs no
    dependency, and a test can assert the exact bytes -- which is how the "it must
    send a file part and a model part" contract stays honest.
    """
    boundary = "----nexusgroqboundary7d1a"
    parts: list[bytes] = []

    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + data
        + b"\r\n"
    )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="model"\r\n\r\n'
            f"{model}\r\n"
        ).encode()
    )
    if language:
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="language"\r\n\r\n'
                f"{language}\r\n"
            ).encode()
        )
    parts.append(f"--{boundary}--\r\n".encode())

    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _translate(status: int, raw: str) -> ProviderError:
    """Groq's HTTP answer to a NEXUS error. Says which of the two it really is."""
    detail = ""
    try:
        parsed = json.loads(raw)
        detail = str(parsed.get("error", {}).get("message") or parsed.get("message") or raw)
    except ValueError:
        detail = raw

    # The trap, named explicitly. 1010 is Cloudflare's, not Groq's, and it appears
    # when the User-Agent looks like a library -- not when the key is bad. Reporting
    # it as an auth failure sends the user to rotate a key that works.
    if "1010" in detail or "1010" in raw:
        return ProviderError(
            "Groq's edge rejected this request as automated traffic (Cloudflare error "
            "1010). That is a User-Agent problem rather than a key problem; NEXUS "
            "sends a browser User-Agent for exactly this reason, so seeing it means "
            "something upstream replaced the headers.",
            provider=PROVIDER_ID,
        )
    if status in (401, 403):
        return ProviderError(
            f"Groq rejected the API key ({status}). {detail}".strip(),
            provider=PROVIDER_ID,
        )
    if status == 400:
        return RequestRejected(
            f"Groq could not read that audio: {detail}".strip(), provider=PROVIDER_ID
        )
    if status == 429:
        return ProviderError(
            "Groq is rate limiting this account. NEXUS rotates across every Groq key "
            "it has, so seeing this means they are all limited at once.",
            provider=PROVIDER_ID,
        )
    if status >= 500:
        return ProviderError(
            f"Groq had a server error ({status}). {detail}".strip(),
            provider=PROVIDER_ID,
        )
    return ProviderError(f"Groq refused the request ({status}). {detail}".strip(),
                         provider=PROVIDER_ID)


def _normalize_language(language: str | None) -> str | None:
    """Normalize BCP-47 language tags to Whisper's primary subtags.

    Groq Whisper expects simple language codes (e.g., "hi" not "hi-IN"),
    so convert any BCP-47 tag to its primary language subtag.
    """
    if not language:
        return None
    # Take only the primary subtag before the first hyphen
    return language.split("-")[0].lower()


_CLIENT: GroqAudioClient | None = None


def client() -> GroqAudioClient:
    """The process-wide client. Stateless apart from its rotation cursor."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = GroqAudioClient()
    return _CLIENT


# --- module-level surface -------------------------------------------------
# ``mistral_audio`` and ``nvidia_speech`` are both addressed at module level by the
# router and the speech door (``providers.groq_audio.serves("stt")``), so these
# forward to the shared client rather than making callers reach through ``client()``.
# Kept as thin wrappers so there is exactly one implementation of each answer.


def has_credential() -> bool:
    return client().has_credential()


def serves(job: str) -> bool:
    """True when Groq has a model for this job. Mirrors ``mistral_audio.serves``.

    Note the return type differs from Mistral's, which returns the tuple of model
    names for the job. That is not a slip: Mistral's callers use the tuple as a
    membership test against a *named* candidate, while the router asks Groq the
    boolean question "can this provider serve this job at all". Returning a tuple
    here would invite a caller to treat an empty tuple as true.
    """
    return any(job in jobs for jobs in _MODELS.values())


def models_for(job: str) -> tuple[str, ...]:
    return tuple(name for name, jobs in _MODELS.items() if job in jobs)


def account_count() -> int:
    return client().account_count()


def available() -> bool:
    return client().has_credential()
