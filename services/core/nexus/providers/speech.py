"""One door to speech, whichever provider is behind it (spec §11, §12, §26).

The router decides which model hears and which model speaks. Until this module
existed, nothing honoured that decision: ``voice_routes`` and ``voice_stream``
called Mistral directly and then reported the router's answer alongside audio
that had not come from it. That is the silent substitution the specs prohibit,
and it is the kind that is hardest to notice, because everything works -- the
transcript is right, the voice sounds fine, and only the label is a lie.

So the rule here is narrow and absolute: **the decision names the provider, and
the provider named is the one called.** If that provider cannot serve the
request, this module raises rather than quietly using the other one. Falling back
is the router's job, and it does it by *re-deciding*, which produces a new
``Decision`` the user can be shown.

Two providers implement speech and they are deliberately not merged:

* :mod:`nexus.providers.nvidia_speech` -- Parakeet and Magpie, over gRPC.
  Multilingual in both directions, and the default.
* :mod:`nexus.providers.mistral_audio` -- Voxtral, over HTTP. A different vendor
  on a different wire, so it survives what NVIDIA does not.

They already return the same :class:`~nexus.providers.nim_media.Transcript` and
:class:`~nexus.providers.nim_media.Speech`, which is why this file is a dispatch
table rather than an adapter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from . import groq_audio, mistral_audio, nvidia_speech
from .base import ProviderError
from .nim_media import Speech, Transcript

log = get_logger("providers.speech")

#: Which module serves which provider id. A dict rather than an if-chain so that
#: "is there a speech provider called X" and "call X" cannot disagree.
_PROVIDERS: dict[str, Any] = {
    "nvidia": nvidia_speech,
    "mistral": mistral_audio,
    # Speech-to-text only: Groq has no synthesis endpoint. It is in this table
    # because the door should be able to name every provider that does *any* part
    # of speech, and each entry declares which parts it does -- see `_SURFACES`.
    # Leaving it out would make the router's own choice (`groq:whisper-...`)
    # unrepresentable here, which is the silent-substitution shape this module was
    # written to prevent.
    "groq": groq_audio,
}

#: Which halves of speech each provider can serve. Asked rather than inferred from
#: a missing attribute, so ``synthesize`` on a hearing-only provider fails with a
#: sentence instead of an ``AttributeError`` from inside a client.
#:
#: Groq is the case that made this explicit: it is the fastest *hearing* measured
#: on this machine and has no voice at all, so "is a speech provider" and "can
#: speak" stopped being the same question.
_SURFACES: dict[str, frozenset[str]] = {
    "nvidia": frozenset({"hear", "speak"}),
    "mistral": frozenset({"hear", "speak"}),
    "groq": frozenset({"hear"}),
}


def surfaces(provider_id: str) -> frozenset[str]:
    """Which halves of speech this provider serves: a subset of {"hear", "speak"}."""
    return _SURFACES.get(provider_id, frozenset())


class NoSpeechProvider(ProviderError):
    """The decision named a provider that does not do speech."""

    kind = "no_speech_provider"
    retryable = False


class CannotSpeak(ProviderError):
    """The provider hears but has no voice. Distinct from not doing speech at all.

    Kept separate because the fixes differ: "pick another model for this job" versus
    "this provider cannot do this job, so the chain must not offer it here".
    """

    kind = "cannot_speak"
    retryable = False


def provider_for(provider_id: str) -> Any:
    module = _PROVIDERS.get(provider_id)
    if module is None:
        raise NoSpeechProvider(
            f"'{provider_id}' does not provide speech in NEXUS. Choose a different "
            "model in Settings -> Models.",
            provider=provider_id,
        )
    return module


def has_credential(provider_id: str) -> bool:
    """Whether this provider's key is present. Never raises for an unknown id."""
    module = _PROVIDERS.get(provider_id)
    return bool(module and module.has_credential())


def default_voice(provider_id: str) -> str:
    """The provider's own default voice.

    Per-provider because the two have nothing in common: Mistral's is a slug
    (``en_paul_neutral``), NVIDIA's is a model name whose speaker is chosen by
    language. Using one provider's default with the other is not a degraded voice,
    it is a rejected request.
    """
    module = _PROVIDERS.get(provider_id)
    return getattr(module, "DEFAULT_VOICE", "") if module else ""


def max_chars(provider_id: str) -> int:
    """How much text this provider will speak in one request.

    Asked rather than assumed, for the same reason as the default voice: the two
    ceilings differ, and a constant copied into a route would be a third number
    that drifts from both. Zero means unknown, which callers should treat as "let
    the provider decide" rather than "refuse everything".
    """
    module = _PROVIDERS.get(provider_id)
    return int(getattr(module, "MAX_TTS_CHARS", 0)) if module else 0


def voice_for(provider_id: str, requested: str | None, configured: str | None) -> str | None:
    """The voice to use, or ``None`` to let the provider choose.

    The caller's choice wins, then the ``tts_voice`` setting, then nothing. The
    setting is dropped when it belongs to a *different* provider, and that is the
    whole reason this function exists: a user who picked ``en_paul_neutral`` while
    Mistral was serving speech would otherwise have every NVIDIA synthesis refused
    with "not a voice Magpie has" -- a stored preference breaking a working
    feature after a routing change they never made.
    """
    chosen = (requested or "").strip()
    if chosen:
        return chosen
    stored = (configured or "").strip()
    if not stored:
        return None
    if _owner_of(stored) not in (provider_id, ""):
        log.debug("ignoring voice %r: it belongs to another provider", stored)
        return None
    return stored


def _owner_of(voice: str) -> str:
    """Which provider a voice name belongs to, or '' when it is not recognisable.

    Name-shaped rather than a lookup: Mistral's voice list is per-account and read
    live, so an exhaustive table is not available at this layer. NVIDIA's names
    all start with the model's own name, which is what makes them identifiable.
    """
    if voice.startswith("Magpie-"):
        return "nvidia"
    if "_" in voice and voice.islower():
        return "mistral"
    return ""


async def transcribe(
    audio: str | Path | bytes,
    *,
    provider_id: str,
    model: str,
    language: str | None = None,
) -> Transcript:
    """Hear, using exactly the provider the router chose."""
    module = provider_for(provider_id)
    return await module.client().transcribe(audio, model=model, language=language)


async def synthesize(
    text: str,
    *,
    provider_id: str,
    model: str,
    voice: str | None = None,
    language: str | None = None,
) -> Speech:
    """Speak, using exactly the provider the router chose.

    ``language`` is passed only to providers that take it. Mistral's synthesis has
    no language parameter -- the voice carries it -- so forwarding one would be a
    ``TypeError`` at the call site rather than a better answer.
    """
    module = provider_for(provider_id)
    if "speak" not in surfaces(provider_id):
        # Groq's case: it is a real speech provider with no voice. Saying so beats
        # an AttributeError from inside a client that has no `synthesize`.
        raise CannotSpeak(
            f"{provider_id} does not do text-to-speech -- it only transcribes. "
            "Choose a speaking model in Settings -> Models.",
            provider=provider_id,
            model=model,
        )
    if module is nvidia_speech:
        return await module.client().synthesize(
            text, model=model, voice=voice, language=language
        )
    return await module.client().synthesize(text, model=model, voice=voice)


async def voices(provider_id: str, model: str | None = None) -> list[dict[str, Any]]:
    """The voices this provider can speak with.

    NVIDIA's are local (the deployment has no list-voices RPC); Mistral's are a
    network call, because the set is per-account and includes cloned voices. Both
    answer the same shape, so the picker does not branch.
    """
    module = provider_for(provider_id)
    if module is nvidia_speech:
        return module.client().voices(model)
    return await module.client().voices()
