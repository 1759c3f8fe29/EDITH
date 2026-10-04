"""OpenAI-compatible providers (task E3; spec §9, §11, §12, §17).

One wire format, several vendors. NVIDIA NIM, OpenAI and Mistral all speak the
``/v1/chat/completions`` protocol with server-sent events, so the transport,
tool-call assembly, error mapping and stall detection are written once here and
the concrete providers only declare their endpoint, credential and model list.

Two details that matter more than they look:

**Tool-call assembly.** Tool calls arrive as fragments -- an id in one chunk, the
function name in the next, the arguments a character at a time -- and the index
field is the only thing tying them together. Assembling them incorrectly produces
a tool call with plausible-looking but wrong arguments, which is worse than an
error. ``_ToolCallAccumulator`` keeps them separated by index and only emits a
call once the stream says it is complete.

**Two different stall guards.** A provider that never sends a first token is a
different failure from one that stops halfway through: the first means the request
never really started, the second means partial work exists and must be reported
(§79). The cold-start deadline is cleared by the first event; the idle deadline
resets on every event afterwards.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator

from ..config import settings
from ..logging_setup import get_logger
from .. import secrets
from . import nim_catalogue
from .base import (
    AuthenticationFailed,
    ChatRequest,
    Message,
    MissingCredential,
    ModelSpec,
    ModelUnavailable,
    NetworkUnreachable,
    Provider,
    ProviderError,
    ProviderOverloaded,
    ProviderTimeout,
    QuotaExceeded,
    RateLimited,
    RequestRejected,
    StreamEvent,
    StreamInterrupted,
    ToolCall,
    Usage,
    VerificationResult,
    wire_tool_names,
)

log = get_logger("providers.openai_compat")


class _ToolCallAccumulator:
    """Reassembles streamed tool-call fragments, keyed by their stream index."""

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, str]] = {}
        self._order: list[int] = []

    def add(self, fragment: dict[str, Any]) -> None:
        index = int(fragment.get("index", 0))
        if index not in self._calls:
            self._calls[index] = {"id": "", "name": "", "arguments": ""}
            self._order.append(index)
        slot = self._calls[index]

        if fragment.get("id"):
            slot["id"] = str(fragment["id"])
        function = fragment.get("function") or {}
        if function.get("name"):
            # Names can also arrive in fragments on some backends.
            slot["name"] += str(function["name"]) if not slot["name"] else ""
            if not slot["name"]:
                slot["name"] = str(function["name"])
        if function.get("arguments"):
            slot["arguments"] += str(function["arguments"])

    def drain(self, names: dict[str, str] | None = None) -> list[ToolCall]:
        """The finished calls, with wire names translated back to NEXUS's own.

        ``names`` is the reverse map from :func:`base.wire_tool_names`. A name that
        is not in it passes through unchanged: a model inventing a tool is a real
        event, and the runtime's "no such tool" is the honest report of it.
        """
        calls: list[ToolCall] = []
        for index in self._order:
            slot = self._calls[index]
            if not slot["name"]:
                continue  # a fragment with no function name is not a callable request
            call_id = slot["id"] or f"call_{index}"
            name = (names or {}).get(slot["name"], slot["name"])
            calls.append(ToolCall.from_raw(call_id, name, slot["arguments"]))
        self._calls.clear()
        self._order.clear()
        return calls

    def __bool__(self) -> bool:
        return bool(self._order)


def map_http_error(
    status: int,
    body: bytes,
    *,
    provider_id: str,
    provider_name: str,
    model: str = "",
) -> ProviderError:
    """Turn an HTTP failure into a typed error the router can act on.

    Module-level rather than a method because the speech, OCR and embedding
    endpoints in ``nim_media`` are not chat-completions but return the same error
    envelopes, and two copies of this mapping would drift apart.
    """
    text = body.decode("utf-8", errors="replace")[:2000]
    detail = text
    message = text
    try:
        parsed = json.loads(text)
        error = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(error, dict):
            message = str(error.get("message") or text)
        elif isinstance(error, str):
            message = error
        elif isinstance(parsed, dict) and parsed.get("detail"):
            message = str(parsed["detail"])
    except json.JSONDecodeError:
        pass

    common = {"provider": provider_id, "model": model, "detail": detail}
    lowered = message.lower()

    if status in (401, 403):
        return AuthenticationFailed(f"{provider_name} rejected the API key: {message}", **common)
    if status == 404:
        return ModelUnavailable(
            f"{provider_name} does not serve '{model}' to this account. {message}", **common
        )
    if status == 429:
        if "quota" in lowered or "credit" in lowered or "billing" in lowered:
            return QuotaExceeded(
                f"{provider_name} reports the account is out of quota: {message}", **common
            )
        return RateLimited(f"{provider_name} is rate limiting this key: {message}", **common)
    if status == 402:
        return QuotaExceeded(f"{provider_name} requires payment: {message}", **common)
    if status == 400:
        if "context" in lowered or ("token" in lowered and "long" in lowered):
            return RequestRejected(f"The request is too long for {model}: {message}", **common)
        return RequestRejected(f"{provider_name} rejected the request: {message}", **common)
    if status == 413:
        return RequestRejected(f"The request is too large for {provider_name}: {message}", **common)
    if status in (500, 502, 503, 504, 529):
        return ProviderOverloaded(
            f"{provider_name} is unavailable right now (HTTP {status}). {message}", **common
        )
    return ProviderError(f"{provider_name} returned HTTP {status}: {message}", **common)


#: Timeout classes carry no message of their own -- ``str(httpx.ReadTimeout())`` is
#: the empty string -- so formatting one into "could not be checked: {exc}" produces
#: a sentence that stops mid-thought and tells the user nothing. Each one is given
#: the cause in words instead.
#:
#: Keyed by class *name* rather than by the class, because httpx is imported lazily
#: inside the functions that use it (see below) and is deliberately not a module-level
#: import here.
_EXCEPTION_CAUSES: dict[str, str] = {
    "ConnectTimeout": "the connection timed out before it was established",
    "ReadTimeout": "it accepted the request but sent no response in time",
    "WriteTimeout": "the request could not be uploaded in time",
    "PoolTimeout": "no connection slot became free in time",
    "ReadError": "the connection dropped mid-response",
    "WriteError": "the connection dropped while the request was being sent",
    "RemoteProtocolError": "it replied with something that is not valid HTTP",
    "TooManyRedirects": "it redirected in a loop",
    "ProxyError": "the configured proxy refused the request",
    "UnsupportedProtocol": "the endpoint address is not a URL this client can use",
}


def _describe_exception(exc: BaseException) -> str:
    """A cause worth showing a user, never an empty string.

    Spec 78 and 80: a failure has to say what happened. An exception whose str() is
    blank -- which every httpx timeout is -- would otherwise reach the Credentials
    screen as "could not be checked: " and leave the user guessing between an
    expired key, a firewall, and a service outage.
    """
    cause = _EXCEPTION_CAUSES.get(exc.__class__.__name__)
    if cause:
        return cause
    detail = str(exc).strip()
    if detail:
        return detail
    if isinstance(exc, ValueError):
        return "it returned a body that is not the JSON this endpoint should return"
    return f"the request failed with {exc.__class__.__name__} and no further detail"


class OpenAICompatible(Provider):
    """Base for any provider speaking the OpenAI chat-completions protocol."""

    base_url: str = ""

    #: How long the credential probe waits for a chat completion. Declared per
    #: provider because it is a real property of the host, not a tuning knob:
    #: NVIDIA's free tier cold-starts a model on first use and has been measured
    #: taking 53 seconds to answer a five-token request, while Mistral answers the
    #: same probe in under two. A single shared deadline would either report a
    #: perfectly good NVIDIA key as unverifiable or make every other provider's
    #: failure take a minute to surface.
    probe_timeout_s: float = 45.0
    #: Overrides ``settings().stream_cold_start_s`` for this provider, for the same
    #: reason ``probe_timeout_s`` is per provider. Without it the probe and the real
    #: request disagree: the probe waits 120 s for a cold NVIDIA model and reports
    #: the key good, then the first actual chat is killed at the global 45 s and
    #: told "the request never started" -- the identical false report, one layer
    #: down. ``None`` means "use the setting", so the user's tuning still applies to
    #: every provider that has no measured reason to differ.
    stream_cold_start_s: float | None = None
    #: Some backends reject ``stream_options``; declared so a subclass can opt out
    #: rather than having usage silently missing.
    supports_stream_usage: bool = True

    def __init__(self) -> None:
        self._client: Any = None

    # ------------------------------------------------------------- credentials

    def has_credential(self) -> bool:
        return bool(secrets.get(self.credential))

    def _key(self) -> str:
        key = secrets.get(self.credential)
        if not key:
            raise MissingCredential(
                f"No {self.name} API key is configured. Add one in Settings -> Providers.",
                provider=self.id,
            )
        return key

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key()}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

    # ------------------------------------------------------------------ client

    async def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
                follow_redirects=False,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------ translation

    def _message_payload(
        self, message: Message, names: dict[str, str] | None = None
    ) -> dict[str, Any]:
        if message.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": message.tool_call_id or "",
                "content": message.content,
            }

        if message.tool_calls:
            return {
                "role": "assistant",
                "content": message.content or None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            # Renamed with the same map as the declared tools: a
                            # replayed call whose name is not among them is a
                            # validation error on the whole request, not just that
                            # message. See base.wire_tool_names.
                            "name": (names or {}).get(call.name, call.name),
                            "arguments": call.raw_arguments or "{}",
                        },
                    }
                    for call in message.tool_calls
                ],
            }

        if message.attachments:
            parts: list[dict[str, Any]] = []
            if message.content:
                parts.append({"type": "text", "text": message.content})
            for attachment in message.attachments:
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{attachment.media_type};base64,{attachment.data}"
                        },
                    }
                )
            return {"role": message.role, "content": parts}

        return {"role": message.role, "content": message.content}

    def _payload(self, request: ChatRequest, *, stream: bool = True) -> dict[str, Any]:
        to_wire, _ = wire_tool_names(request.tools)
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend(self._message_payload(m, to_wire) for m in request.messages)

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "stream": stream,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_output_tokens:
            payload["max_tokens"] = request.max_output_tokens
        if request.stop:
            payload["stop"] = request.stop
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        **schema,
                        "name": to_wire.get(
                            str(schema.get("name", "")), str(schema.get("name", ""))
                        ),
                    },
                }
                for schema in request.tools
            ]
            if request.tool_choice == "required":
                payload["tool_choice"] = "required"
            elif request.tool_choice == "none":
                payload["tool_choice"] = "none"
        if stream and self.supports_stream_usage:
            payload["stream_options"] = {"include_usage": True}
        return payload

    # ---------------------------------------------------------- error mapping

    def _map_error(self, status: int, body: bytes, model: str) -> ProviderError:
        return map_http_error(
            status, body, provider_id=self.id, provider_name=self.name, model=model
        )

    # ---------------------------------------------------------------- streaming

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        client = await self._http()
        payload = self._payload(request, stream=True)
        cold_start = self.stream_cold_start_s or settings().stream_cold_start_s
        idle = settings().stream_idle_s

        import httpx

        accumulator = _ToolCallAccumulator()
        # The reverse of the map ``_payload`` applied. Rebuilt rather than threaded
        # through: it is a pure function of the declared tool list, so the two agree
        # by construction.
        _, from_wire = wire_tool_names(request.tools)
        text_so_far: list[str] = []
        usage: Usage | None = None
        finish_reason: str | None = None
        saw_first_event = False

        try:
            async with client.stream(
                "POST", "/chat/completions", json=payload, headers=self._headers()
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    raise self._map_error(response.status_code, body, request.model)

                lines = response.aiter_lines()
                while True:
                    deadline = cold_start if not saw_first_event else idle
                    try:
                        line = await asyncio.wait_for(lines.__anext__(), timeout=deadline)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        if not saw_first_event:
                            raise ProviderTimeout(
                                f"{self.name} did not send anything within "
                                f"{cold_start:.0f} seconds, so the request never started.",
                                provider=self.id, model=request.model,
                            ) from None
                        raise StreamInterrupted(
                            f"{self.name} stopped sending after "
                            f"{len(''.join(text_so_far))} characters and went quiet for "
                            f"{idle:.0f} seconds.",
                            partial_text="".join(text_so_far),
                            provider=self.id, model=request.model,
                        ) from None

                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        log.warning("unparseable stream chunk", extra={"provider": self.id})
                        continue

                    saw_first_event = True

                    if chunk.get("usage"):
                        raw = chunk["usage"]
                        usage = Usage(
                            input_tokens=int(raw.get("prompt_tokens") or 0),
                            output_tokens=int(raw.get("completion_tokens") or 0),
                            cached_input_tokens=int(
                                (raw.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
                            ),
                        )

                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}

                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if reasoning:
                            yield StreamEvent(type="reasoning", text=str(reasoning))

                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            text_so_far.append(content)
                            yield StreamEvent(type="text", text=content)
                        elif isinstance(content, list):
                            # Some backends send content as typed parts.
                            for part in content:
                                if isinstance(part, dict) and part.get("type") == "text":
                                    fragment = str(part.get("text") or "")
                                    if fragment:
                                        text_so_far.append(fragment)
                                        yield StreamEvent(type="text", text=fragment)

                        for fragment in delta.get("tool_calls") or []:
                            accumulator.add(fragment)

                        if choice.get("finish_reason"):
                            finish_reason = str(choice["finish_reason"])

        except ProviderError:
            raise
        except httpx.ConnectError as exc:
            raise NetworkUnreachable(
                f"Could not reach {self.name}. Check that this computer is online.",
                provider=self.id, model=request.model, detail=str(exc),
            ) from exc
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise ProviderTimeout(
                f"{self.name} timed out -- {_describe_exception(exc)}.",
                provider=self.id, model=request.model,
                detail=_describe_exception(exc),
            ) from exc
        except httpx.HTTPError as exc:
            raise StreamInterrupted(
                f"The connection to {self.name} broke: {_describe_exception(exc)}.",
                partial_text="".join(text_so_far),
                provider=self.id, model=request.model,
            ) from exc

        for call in accumulator.drain(from_wire):
            yield StreamEvent(type="tool_call", tool_call=call)

        if usage is not None:
            yield StreamEvent(type="usage", usage=usage)

        if not saw_first_event:
            # A 200 response that produced no events at all. Reporting an empty
            # answer as success would be a silent failure (§79).
            raise StreamInterrupted(
                f"{self.name} accepted the request but sent no response.",
                provider=self.id, model=request.model,
            )

        yield StreamEvent(
            type="done", finish_reason=finish_reason or "stop", model=request.model, usage=usage
        )

    # ------------------------------------------------------------ verification

    async def verify(self) -> VerificationResult:
        """Real request, real answer (§13, §78).

        Lists the account's models, then makes a one-token completion. Listing
        alone is not enough: on some backends a key can list a catalogue it cannot
        actually call.
        """
        if not self.has_credential():
            return VerificationResult(
                ok=False,
                message=f"No {self.name} API key is configured.",
                error_kind="missing_credential",
            )

        started = time.monotonic()
        client = await self._http()
        import httpx

        models_seen: list[str] = []
        try:
            listing = await client.get(
                "/models", headers={"Authorization": f"Bearer {self._key()}"}, timeout=20.0
            )
            if listing.status_code < 400:
                body = listing.json()
                entries = body.get("data") if isinstance(body, dict) else None
                if isinstance(entries, list):
                    models_seen = [str(e.get("id")) for e in entries if isinstance(e, dict)][:200]
        except (httpx.HTTPError, ValueError):
            # Not every compatible backend implements /models. Not fatal.
            pass

        probe = ChatRequest(
            messages=[Message(role="user", content="Reply with the single word: ok")],
            model=self.default_model().id,
            max_output_tokens=5,
            temperature=0.0,
        )
        try:
            payload = self._payload(probe, stream=False)
            response = await client.post(
                "/chat/completions", json=payload, headers=self._headers(),
                timeout=self.probe_timeout_s,
            )
            if response.status_code >= 400:
                error = self._map_error(response.status_code, await response.aread(), probe.model)
                return VerificationResult(
                    ok=False, message=error.message, models_seen=models_seen,
                    error_kind=error.kind,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            body = response.json()
            reply = ""
            for choice in body.get("choices") or []:
                reply += str((choice.get("message") or {}).get("content") or "")
            latency = int((time.monotonic() - started) * 1000)
            return VerificationResult(
                ok=True,
                message=(
                    f"{self.name} responded in {latency} ms using "
                    f"{body.get('model') or probe.model}"
                    + (f" ({len(models_seen)} models available)" if models_seen else "")
                ),
                models_seen=models_seen,
                latency_ms=latency,
            )
        except httpx.ConnectError:
            return VerificationResult(
                ok=False,
                message=f"Could not reach {self.name}. Check that this computer is online.",
                models_seen=models_seen, error_kind="network_unreachable",
            )
        except (httpx.HTTPError, ValueError) as exc:
            # A timeout after the model listing already succeeded is a specific and
            # common situation worth naming: the listing is an authenticated call,
            # so a key that fetched it is a key that works. Reporting a flat "could
            # not be checked" here would send the user off to rotate a perfectly
            # good credential when the real story is a cold or overloaded model.
            if models_seen and isinstance(exc, httpx.TimeoutException):
                return VerificationResult(
                    ok=False,
                    message=(
                        f"Your {self.name} key works -- it listed {len(models_seen)} "
                        f"models -- but {probe.model} did not answer within "
                        f"{self.probe_timeout_s:.0f} seconds. That is usually a cold "
                        "or busy model rather than a problem with the key. Trying "
                        "again shortly generally succeeds."
                    ),
                    models_seen=models_seen, error_kind="timeout",
                )
            return VerificationResult(
                ok=False,
                message=f"{self.name} could not be checked: {_describe_exception(exc)}",
                models_seen=models_seen, error_kind="provider_error",
            )


# ---------------------------------------------------------------------- NVIDIA


class NvidiaProvider(OpenAICompatible):
    """NVIDIA NIM, treated as a set of specialists rather than one model.

    The chat and vision models live here because they speak chat-completions and
    so fit this transport. The rest of NIM -- speech, OCR, embeddings, ranking --
    does not, and lives in ``nim_media.py`` behind the same job vocabulary. The
    single source of truth for "which NIM model does which job" is
    ``nim_catalogue``; nothing in this class hardcodes a model name.
    """

    id = "nvidia"
    name = "NVIDIA NIM"
    credential = "NVIDIA_API_KEY"
    console_url = "https://build.nvidia.com/settings/api-keys"
    base_url = nim_catalogue.HOSTED_BASE
    # Measured: a cold free-tier model took 53 s to return five tokens, while the
    # model listing answered in under a second. The key was fine both times. The
    # streaming deadline matches, because a cold start is a cold start whether the
    # caller is the probe or the user waiting for a reply.
    probe_timeout_s = 120.0
    stream_cold_start_s = 120.0

    @property
    def models(self) -> tuple[ModelSpec, ...]:
        return nim_catalogue.all_models()

    def model_for_job(self, job: str) -> ModelSpec | None:
        """The chat-shaped model that serves a job, if one does.

        Returns None for jobs this transport cannot carry at all (speech, OCR,
        embeddings) so the caller reaches for ``nim_media`` rather than sending an
        audio file to a chat endpoint and reading a confusing 400.
        """
        endpoint = nim_catalogue.preferred(job)  # type: ignore[arg-type]
        if endpoint is None or endpoint.modality not in ("chat", "vision"):
            return None
        return self.model(endpoint.model)

    def specialists(self) -> list[dict[str, object]]:
        """Every NIM specialist and how it is called, for the Settings screen."""
        return nim_catalogue.catalogue()


# ---------------------------------------------------------------------- OpenAI


class OpenAIProvider(OpenAICompatible):
    id = "openai"
    name = "OpenAI"
    credential = "OPENAI_API_KEY"
    console_url = "https://platform.openai.com/api-keys"
    base_url = "https://api.openai.com/v1"

    @property
    def models(self) -> tuple[ModelSpec, ...]:
        return (
            ModelSpec(
                id="gpt-4.1-mini",
                provider=self.id,
                label="GPT-4.1 mini",
                context_window=1_000_000,
                max_output_tokens=32_768,
                supports_vision=True,
                strengths=("fast", "cheap", "instruction_following", "long_context"),
                input_cost_per_million=0.40,
                output_cost_per_million=1.60,
                latency="fast",
            ),
            ModelSpec(
                id="gpt-4.1",
                provider=self.id,
                label="GPT-4.1",
                context_window=1_000_000,
                max_output_tokens=32_768,
                supports_vision=True,
                strengths=("reasoning", "coding", "long_context", "vision"),
                input_cost_per_million=2.00,
                output_cost_per_million=8.00,
                latency="medium",
            ),
            ModelSpec(
                id="gpt-4o-mini",
                provider=self.id,
                label="GPT-4o mini",
                context_window=128_000,
                max_output_tokens=16_384,
                supports_vision=True,
                strengths=("fast", "cheap"),
                input_cost_per_million=0.15,
                output_cost_per_million=0.60,
                latency="fast",
            ),
        )


# --------------------------------------------------------------------- Mistral


class MistralProvider(OpenAICompatible):
    """Mistral. A keyed provider whose flagship, ``mistral-large-latest``, was
    NEXUS's main model until ``deepseek-v4-flash-0731`` took that role.

    It is first in this tuple, so it remains what ``default_model()`` returns for
    this provider -- but it heads no routing chain now: ``chat`` heads
    ``deepseek-v4-flash-0731``, ``reasoning`` heads ``nemotron-3-ultra-550b``, and
    ``coding``/``expert`` head ``deepseek-v4-pro-0813``. Mistral backs up the
    ``fast``, ``vision`` and speech jobs.
    """

    id = "mistral"
    name = "Mistral"
    credential = "MISTRAL_API_KEY"
    console_url = "https://console.mistral.ai/api-keys"
    base_url = "https://api.mistral.ai/v1"
    # Mistral's API rejects unknown top-level fields, and stream_options is one.
    supports_stream_usage = False

    #: The Mistral provider's own default; no longer the router's main model.
    MAIN_MODEL = "mistral-large-latest"

    @property
    def models(self) -> tuple[ModelSpec, ...]:
        return (
            ModelSpec(
                id=self.MAIN_MODEL,
                provider=self.id,
                label="Mistral Large",
                context_window=128_000,
                max_output_tokens=8_192,
                strengths=("reasoning", "coding", "instruction_following", "long_context"),
                latency="medium",
                notes=(
                    "Mistral flagship; no longer NEXUS's main model (that is "
                    "deepseek-v4-flash-0731), but still the Mistral provider's default. "
                    "check console.mistral.ai for current rates."
                ),
            ),
            ModelSpec(
                id="mistral-small-latest",
                provider=self.id,
                label="Mistral Small",
                context_window=128_000,
                max_output_tokens=8_192,
                strengths=("fast", "cheap", "instruction_following"),
                latency="fast",
                notes="Used for short, latency-sensitive work: titles, classification, "
                      "quick acknowledgements.",
            ),
            ModelSpec(
                id="pixtral-12b-2409",
                provider=self.id,
                label="Pixtral 12B",
                context_window=128_000,
                max_output_tokens=4_096,
                supports_vision=True,
                strengths=("vision", "cheap"),
                latency="fast",
                notes="Vision fallback for when NVIDIA's vision endpoints are unavailable.",
            ),
        )
