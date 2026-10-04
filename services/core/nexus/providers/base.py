"""The provider interface (task E1; spec §9, §10, §11, §12, §60, §79).

Every cloud model NEXUS can talk to is reached through one of these. The router
picks a provider; the orchestrator streams from it. Neither knows what wire
format is underneath.

Three things this file exists to guarantee:

**Errors carry meaning.** A router cannot fall back intelligently from a bare
exception. ``ProviderError`` subclasses say whether the failure was the key, the
quota, the model name, the network, or the provider having a bad day -- and
whether retrying could possibly help. §11 requires the user be told *which*
provider failed and *why*; that is only possible if the failure is typed at the
point it happens.

**Streaming is a sequence of typed events**, not a string. Text, tool calls,
reasoning and usage arrive interleaved on the wire, and the UI needs to
distinguish them (§17: the user sees real progress, never a fake spinner).

**Model capabilities are declared, not assumed.** ``ModelSpec`` states context
window, tool support, vision support and price. The router reads these instead of
pattern-matching on model names, so routing a vision request to a text-only model
is a check that fails loudly rather than a request that fails obscurely (§10).
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal

# --------------------------------------------------------------------- errors


class ProviderError(Exception):
    """Base class. ``message`` is shown to the user, so it must be plain English."""

    # Whether trying the same provider again could plausibly work.
    retryable: bool = False
    # Whether the router should try a different provider instead.
    fall_back: bool = True
    kind: str = "provider_error"

    def __init__(self, message: str, *, provider: str = "", model: str = "", detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.model = model
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": self.message,
            "provider": self.provider,
            "model": self.model,
            "detail": self.detail,
            "retryable": self.retryable,
            "fall_back": self.fall_back,
        }

    def __str__(self) -> str:  # pragma: no cover - trivial
        where = f" ({self.provider})" if self.provider else ""
        return f"{self.message}{where}"


class MissingCredential(ProviderError):
    """No API key configured. Retrying is pointless; another provider may work."""

    kind = "missing_credential"
    retryable = False


class AuthenticationFailed(ProviderError):
    """The key exists but the provider rejected it (revoked, wrong project)."""

    kind = "authentication_failed"
    retryable = False


class QuotaExceeded(ProviderError):
    """Out of credit or over a hard limit. A different provider is the only route."""

    kind = "quota_exceeded"
    retryable = False


class RateLimited(ProviderError):
    """Too many requests. Retrying after ``retry_after`` seconds can work."""

    kind = "rate_limited"
    retryable = True

    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data["retry_after"] = self.retry_after
        return data


class ModelUnavailable(ProviderError):
    """The model name is not served to this account.

    Distinct from a 404 on the endpoint: a model can be visible in a provider's
    catalogue and still not be callable with a given key, which is exactly the
    case that produces a confusing "not found" if it is not named precisely.
    """

    kind = "model_unavailable"
    retryable = False


class ProviderOverloaded(ProviderError):
    """The provider is up but shedding load. Worth one retry, then fall back."""

    kind = "overloaded"
    retryable = True


class RequestRejected(ProviderError):
    """The provider refused the request itself: too long, malformed, filtered.

    Not retryable and *not* worth falling back over -- another provider will
    almost certainly reject the same request, and silently trying six of them
    wastes the user's time and money.
    """

    kind = "request_rejected"
    retryable = False
    fall_back = False


class ProviderTimeout(ProviderError):
    kind = "timeout"
    retryable = True


class NetworkUnreachable(ProviderError):
    """No route to the provider. Usually the computer is offline, in which case
    every provider will fail and the user needs to hear *that*, not six errors."""

    kind = "network_unreachable"
    retryable = True


class StreamInterrupted(ProviderError):
    """The connection dropped mid-response.

    Carries whatever text had already arrived: §79 requires partial work be
    acknowledged rather than thrown away as if nothing happened.
    """

    kind = "stream_interrupted"
    retryable = True

    def __init__(self, message: str, *, partial_text: str = "", **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.partial_text = partial_text


# ------------------------------------------------------------------- messages

Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class ToolCall:
    """A model's request to run a tool."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    # The raw JSON string, kept because a model sometimes emits invalid JSON and
    # the honest report is "the model asked for this, and it was malformed".
    raw_arguments: str = ""
    parse_error: str | None = None

    @classmethod
    def from_raw(cls, call_id: str, name: str, raw: str) -> ToolCall:
        raw = (raw or "").strip() or "{}"
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                return cls(call_id, name, {}, raw, f"expected an object, got {type(parsed).__name__}")
            return cls(call_id, name, parsed, raw)
        except json.JSONDecodeError as exc:
            return cls(call_id, name, {}, raw, f"invalid JSON: {exc.msg} at position {exc.pos}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
            "raw_arguments": self.raw_arguments,
            "parse_error": self.parse_error,
        }


# ------------------------------------------------------------------ tool names
#
# NEXUS names its tools with a dot -- ``apps.close``, ``system.status`` -- because
# the prefix is the capability group, and that grouping runs all the way through
# the permission engine and the Settings screen. Renaming the tools themselves is
# not an option.
#
# Most model APIs will not accept that name. The grammar they enforce is
# ``[a-zA-Z0-9_-]{1,64}``, and a dot fails it:
#
# * Anthropic rejects the whole request with "Invalid tool use format", naming
#   neither the tool nor the offending character.
# * NVIDIA NIM is blunter and more useful: 'Function at index 0 has an invalid
#   name: "apps.close". Only a-z, A-Z, 0-9, underscores, and dashes are allowed.'
# * Mistral accepts dots, which is why this went unnoticed -- the main model works
#   and every NVIDIA-routed turn with tools attached was failing on its first
#   request.
#
# So names are translated on the way out and translated back on the way in, for
# every provider, uniformly. Uniform rather than per-provider on purpose: a quirk
# table of who-tolerates-what drifts out of date silently, and the failure mode is
# a whole job losing the ability to act. An underscore name is legal everywhere,
# including on the providers that would also have taken the dot.
#
# The map is built per request from the declared tool list rather than derived by a
# rule, because the substitution is not reversible: ``memory_recall`` and
# ``memory.recall`` both spell ``memory_recall`` on the wire, and guessing which
# one the model meant would eventually run the wrong tool. A map cannot guess.

_WIRE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


def wire_tool_names(
    tools: list[dict[str, Any]] | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Build one request's tool-name translation: ``(to_wire, from_wire)``.

    Collisions are resolved by numbering rather than by letting one tool overwrite
    another. A silently dropped tool is a capability that disappears with no error
    anywhere: the model never calls it, and the user is told the computer cannot do
    something it can.
    """
    to_wire: dict[str, str] = {}
    from_wire: dict[str, str] = {}
    for schema in tools or []:
        original = str(schema.get("name") or "")
        if not original or original in to_wire:
            continue
        candidate = _WIRE_NAME_RE.sub("_", original)[:64] or "tool"
        if candidate in from_wire:
            stem = candidate[:60]
            suffix = 2
            while f"{stem}_{suffix}" in from_wire:
                suffix += 1
            candidate = f"{stem}_{suffix}"
        to_wire[original] = candidate
        from_wire[candidate] = original
    return to_wire, from_wire


@dataclass
class Attachment:
    """An image or document sent with a message."""

    kind: Literal["image"]
    media_type: str
    # Base64 payload. Kept out of the database deliberately (see tools/screen.py).
    data: str
    source_path: str | None = None


@dataclass
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Set on role='tool' to say which call this is answering.
    tool_call_id: str | None = None
    tool_name: str | None = None
    attachments: list[Attachment] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": [c.to_dict() for c in self.tool_calls],
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "has_attachments": bool(self.attachments),
        }


@dataclass
class ChatRequest:
    messages: list[Message]
    model: str
    system: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    temperature: float | None = None
    max_output_tokens: int | None = None
    # Set when the caller needs one tool call and no prose, e.g. structured
    # extraction. Providers that cannot force this must say so, not ignore it.
    tool_choice: Literal["auto", "required", "none"] = "auto"
    stop: list[str] = field(default_factory=list)

    def needs_vision(self) -> bool:
        return any(m.attachments for m in self.messages)

    def approximate_input_tokens(self) -> int:
        """A rough size estimate for routing and context-limit checks.

        Four characters per token is wrong in the third decimal place and right
        enough to decide "will this fit in 8k". Providers report real usage after
        the fact; this is only ever used for a pre-flight decision.
        """
        characters = len(self.system) + sum(
            len(m.content) + sum(len(c.raw_arguments) for c in m.tool_calls)
            for m in self.messages
        )
        images = sum(len(m.attachments) for m in self.messages)
        return characters // 4 + images * 1_200


# -------------------------------------------------------------- stream events

EventType = Literal[
    "text",           # a fragment of the visible answer
    "reasoning",      # a fragment of thinking, where the provider exposes it
    "tool_call",      # a complete, parsed tool call
    "usage",          # token counts, once known
    "done",           # the response ended normally
]


@dataclass
class StreamEvent:
    type: EventType
    text: str = ""
    tool_call: ToolCall | None = None
    usage: Usage | None = None
    finish_reason: str | None = None
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.type}
        if self.text:
            data["text"] = self.text
        if self.tool_call is not None:
            data["tool_call"] = self.tool_call.to_dict()
        if self.usage is not None:
            data["usage"] = self.usage.to_dict()
        if self.finish_reason:
            data["finish_reason"] = self.finish_reason
        if self.model:
            data["model"] = self.model
        return data


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "total_tokens": self.total,
        }

    def cost_usd(self, spec: ModelSpec) -> float | None:
        """Actual cost, when the model's prices are known."""
        if spec.input_cost_per_million is None or spec.output_cost_per_million is None:
            return None
        return (
            self.input_tokens / 1_000_000 * spec.input_cost_per_million
            + self.output_tokens / 1_000_000 * spec.output_cost_per_million
        )


# ----------------------------------------------------------------- model specs

Strength = Literal[
    "fast", "cheap", "reasoning", "coding", "long_context", "vision",
    "instruction_following", "creative",
]


@dataclass(frozen=True)
class ModelSpec:
    """What a model can do, declared rather than guessed (§10)."""

    id: str                       # the provider's own model identifier
    provider: str
    label: str                    # what the user sees
    context_window: int
    max_output_tokens: int
    supports_tools: bool = True
    supports_vision: bool = False
    supports_streaming: bool = True
    supports_system_prompt: bool = True
    strengths: tuple[Strength, ...] = ()
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None
    # Rough first-token latency class, used when the user's request is urgent.
    latency: Literal["fast", "medium", "slow"] = "medium"
    # Set when a model is known to be free at the point of use, which changes the
    # routing calculus entirely.
    free: bool = False
    notes: str = ""

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "id": self.id,
            "provider": self.provider,
            "label": self.label,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "supports_tools": self.supports_tools,
            "supports_vision": self.supports_vision,
            "supports_streaming": self.supports_streaming,
            "strengths": list(self.strengths),
            "input_cost_per_million": self.input_cost_per_million,
            "output_cost_per_million": self.output_cost_per_million,
            "latency": self.latency,
            "free": self.free,
            "notes": self.notes,
        }


# -------------------------------------------------------------------- provider


@dataclass
class VerificationResult:
    """The outcome of a live credential check (§13, §78).

    A key is "working" only when a real request to the real endpoint succeeded.
    Checking that the string is non-empty and calling that verified is precisely
    the fake validation the spec forbids.
    """

    ok: bool
    message: str
    models_seen: list[str] = field(default_factory=list)
    latency_ms: int | None = None
    error_kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "models_seen": self.models_seen,
            "latency_ms": self.latency_ms,
            "error_kind": self.error_kind,
        }


class Provider(ABC):
    """One cloud model API.

    Implementations must translate their wire errors into the typed
    ``ProviderError`` family. Anything that escapes as a raw exception becomes an
    unhelpful "something went wrong" for the user and an unroutable failure for
    the router, so the base class treats that as a bug rather than a style issue.
    """

    #: Stable short identifier, e.g. "anthropic". Used in settings and logs.
    id: str = ""
    #: Human name for the UI.
    name: str = ""
    #: Environment/keyring credential this provider needs.
    credential: str = ""
    #: Where the user goes to get a key. Shown in the Settings screen when the
    #: credential is missing, so an empty state has a way out (§80).
    console_url: str = ""

    @property
    @abstractmethod
    def models(self) -> tuple[ModelSpec, ...]:
        """Models this provider serves, in preference order."""

    def model(self, model_id: str) -> ModelSpec | None:
        for spec in self.models:
            if spec.id == model_id:
                return spec
        return None

    def default_model(self) -> ModelSpec:
        return self.models[0]

    @abstractmethod
    def has_credential(self) -> bool:
        """Whether a key is configured. Never validates it -- see ``verify``."""

    @abstractmethod
    async def verify(self) -> VerificationResult:
        """Make a real, minimal request to prove the credential works (§13)."""

    @abstractmethod
    def stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        """Stream a response.

        Must raise a ``ProviderError`` subclass on failure. Must yield a final
        ``done`` event on success so the caller can distinguish "the model
        finished" from "the connection stopped producing events".
        """

    async def close(self) -> None:
        """Release connections. Safe to call more than once."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "credential": self.credential,
            "console_url": self.console_url,
            "configured": self.has_credential(),
            "models": [m.to_dict() for m in self.models],
        }
