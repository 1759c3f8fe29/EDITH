"""The provider registry (task E2).

One place that knows which providers exist, holds a single instance of each, and
answers "which of these can actually be used right now". Everything above this
layer -- router, orchestrator, settings screen -- goes through here rather than
constructing providers, so there is exactly one connection pool per provider and
one answer to "is this configured".

The registry never validates a key by looking at it. ``configured()`` means a key
is present; ``verify_all()`` makes real calls. Those are different facts and the
UI shows them differently (§13, §78).
"""

from __future__ import annotations

from typing import Any

from ..logging_setup import get_logger
from . import mistral_audio, nim_catalogue, nim_media, nvidia_speech, speech
from .base import (
    Attachment,
    ChatRequest,
    Message,
    ModelSpec,
    Provider,
    ProviderError,
    StreamEvent,
    ToolCall,
    Usage,
    VerificationResult,
)
from .anthropic import AnthropicProvider
from .openai_compat import MistralProvider, NvidiaProvider, OpenAIProvider

log = get_logger("providers")

#: Order matters: it is the order the Settings screen lists providers in, and the
#: tie-break the router uses when two providers can do a job equally well.
#: Mistral sits first as a legacy of ``mistral-large-latest`` having been the main model (``deepseek-v4-flash-0731`` now is). Anthropic
#: sits second: it heads no job (``expert`` moved to ``deepseek-v4-pro-0813``), so it should never win a
#: tie-break for ordinary conversation, but it outranks the general-purpose
#: fallbacks everywhere it is a declared candidate.
_CLASSES: tuple[type[Provider], ...] = (
    MistralProvider,
    AnthropicProvider,
    NvidiaProvider,
    OpenAIProvider,
)

_INSTANCES: dict[str, Provider] = {}


def _build() -> dict[str, Provider]:
    global _INSTANCES
    if not _INSTANCES:
        _INSTANCES = {cls.id: cls() for cls in _CLASSES}  # type: ignore[abstract]
    return _INSTANCES


def all_providers() -> list[Provider]:
    return list(_build().values())


def get(provider_id: str) -> Provider | None:
    return _build().get(provider_id)


def configured() -> list[Provider]:
    """Providers with a key present. Says nothing about whether the key works."""
    return [p for p in all_providers() if p.has_credential()]


def find_model(key: str) -> tuple[Provider, ModelSpec] | None:
    """Resolve a "provider:model" key. Model ids contain slashes, so split once."""
    if ":" not in key:
        return None
    provider_id, model_id = key.split(":", 1)
    provider = get(provider_id)
    if provider is None:
        return None
    spec = provider.model(model_id)
    if spec is None:
        return None
    return provider, spec


def all_models() -> list[ModelSpec]:
    return [spec for provider in all_providers() for spec in provider.models]


def available_models() -> list[ModelSpec]:
    """Models on providers that have a key."""
    return [spec for provider in configured() for spec in provider.models]


async def verify_all() -> dict[str, VerificationResult]:
    """Verify every configured provider with a real request (§13)."""
    results: dict[str, VerificationResult] = {}
    for provider in all_providers():
        if not provider.has_credential():
            results[provider.id] = VerificationResult(
                ok=False,
                message=f"No {provider.name} API key is configured.",
                error_kind="missing_credential",
            )
            continue
        try:
            results[provider.id] = await provider.verify()
        except ProviderError as exc:
            results[provider.id] = VerificationResult(
                ok=False, message=exc.message, error_kind=exc.kind
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("verify failed for %s", provider.id)
            results[provider.id] = VerificationResult(
                ok=False,
                message=f"Checking {provider.name} failed unexpectedly: {exc}",
                error_kind="unknown",
            )
    return results


async def close_all() -> None:
    for provider in list(_INSTANCES.values()):
        try:
            await provider.close()
        except Exception:  # pragma: no cover - shutdown must not raise
            log.warning("closing %s failed", provider.id, exc_info=True)
    await nim_media.client().close()
    await mistral_audio.client().close()
    await nvidia_speech.client().close()


def snapshot() -> dict[str, Any]:
    """What the Settings screen renders, without making any network calls."""
    return {
        "providers": [p.to_dict() for p in all_providers()],
        "nim_specialists": nim_catalogue.catalogue(),
    }


__all__ = [
    "Attachment",
    "ChatRequest",
    "Message",
    "ModelSpec",
    "Provider",
    "ProviderError",
    "StreamEvent",
    "ToolCall",
    "Usage",
    "VerificationResult",
    "all_models",
    "all_providers",
    "available_models",
    "close_all",
    "configured",
    "find_model",
    "get",
    "mistral_audio",
    "nim_catalogue",
    "nim_media",
    "nvidia_speech",
    "snapshot",
    "speech",
    "verify_all",
]
