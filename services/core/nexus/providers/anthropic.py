"""Anthropic's Messages API, which backs Expert Mode (spec §9, §11, §13).

This is the one provider that does not go through :mod:`openai_compat`, and the
reason is the wire format rather than preference. The Messages API differs from
chat-completions in four ways that each break a shared implementation:

* The system prompt is a **top-level field**, not a message with ``role:
  "system"``. Sent as a message it is either rejected or silently demoted to user
  text, which quietly discards the entire behavioural contract.
* ``max_tokens`` is **required**. Omitting it is a 400, not a default.
* Tool schemas are flat -- ``{name, description, input_schema}`` -- not nested
  under a ``function`` key, and tool *results* come back as a **user** message
  containing ``tool_result`` blocks rather than as their own role. All the results
  for one assistant turn must arrive in a single user message, so the core's
  one-message-per-tool shape has to be merged on the way out (see
  :meth:`_payload`). Getting this wrong produces a 400 that reads like a model
  problem.
* Streaming is a **typed event protocol** (``content_block_start`` /
  ``content_block_delta`` / ``message_delta``) rather than a series of deltas,
  and tool arguments arrive as ``input_json_delta`` fragments tied to a block
  index.

**The base URL is configurable, and that is the point.** ``ANTHROPIC_BASE_URL``
is honoured exactly as the Anthropic SDKs honour it, so this provider works
against api.anthropic.com or against a gateway that speaks the same protocol.
Because a gateway's model catalogue is its own, this module declares the models
it intends to use and lets the router's probe discover which of them the
configured endpoint actually serves -- the same "listed is not callable"
discipline every other provider here follows.

**Both auth headers are sent.** Anthropic's own API authenticates with
``x-api-key``; gateways generally expect ``Authorization: Bearer``. Each ignores
the other's header, so sending both makes one credential work against either
without asking the user which kind of endpoint they pasted.
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

from ..config import settings
from ..logging_setup import get_logger
from .. import secrets
from .base import (
    AuthenticationFailed,
    ChatRequest,
    Message,
    MissingCredential,
    ModelSpec,
    NetworkUnreachable,
    Provider,
    ProviderError,
    ProviderTimeout,
    StreamEvent,
    StreamInterrupted,
    ToolCall,
    Usage,
    VerificationResult,
    wire_tool_names,
)
from .openai_compat import _describe_exception, map_http_error

log = get_logger("providers.anthropic")

#: The Messages API version. Pinned rather than tracking latest: a new version can
#: change response shapes, and this parser is written against this one.
API_VERSION = "2023-06-01"

DEFAULT_BASE_URL = "https://api.anthropic.com"

#: Credential names checked in order. ``ANTHROPIC_AUTH_TOKEN`` is second because
#: it is the gateway/OAuth-style name while ``ANTHROPIC_API_KEY`` is the direct
#: one, and a user who has set both almost certainly means the direct key.
CREDENTIAL_NAMES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

#: How NEXUS identifies itself over HTTP. Set explicitly because httpx's default
#: (``python-httpx/x.y``) is on Cloudflare's generic-client list, and a gateway
#: sitting behind Cloudflare answers it with a 403 HTML interstitial instead of
#: the API. Measured against the configured endpoint: identical request, default
#: agent → 403 HTML, this agent → 200 JSON. Naming the client is what every SDK
#: does and what a server operator needs in order to see who is calling.
USER_AGENT = "nexus/0.1 (+https://github.com/nexus-ai/nexus)"



def base_url() -> str:
    """Where to send requests, from ``ANTHROPIC_BASE_URL`` or Anthropic itself.

    The project's ``.env`` is checked **before** the process environment, which is
    the opposite of the usual precedence and is deliberate. ``ANTHROPIC_BASE_URL``
    and ``ANTHROPIC_AUTH_TOKEN`` are the standard names every Anthropic client
    reads, so any such tool running in the same shell exports them -- and NEXUS
    inheriting them means silently talking to another program's endpoint with
    another program's credential. That was not hypothetical: the first run of this
    provider picked up a gateway from the surrounding shell and paired it with the
    key from ``.env``, which the gateway correctly rejected. The project's own file
    is the project's own configuration; the environment is a fallback for
    deployments that have no ``.env`` at all.

    Normalised so that a value with or without a trailing ``/v1`` behaves the
    same, because both spellings are in circulation and the difference would
    otherwise show up as a 404 on a perfectly good configuration.
    """
    raw = (secrets.env_value("ANTHROPIC_BASE_URL", prefer_file=True) or "").strip()
    if not raw:
        return DEFAULT_BASE_URL
    raw = raw.rstrip("/")
    if raw.endswith("/v1"):
        raw = raw[: -len("/v1")]
    return raw


# ------------------------------------------------------------------ tool names
#
# The name translation itself is shared -- see :func:`nexus.providers.base.
# wire_tool_names`, which documents why every provider needs it. Only the
# parameter-schema shaping below is specific to this API.


def _wire_input_schema(parameters: Any) -> dict[str, Any]:
    """A tool's parameter schema in the shape this API insists on.

    It requires an object schema with a ``properties`` map, and rejects a bare
    ``{}`` -- which is what a no-argument tool would otherwise send.
    """
    if not isinstance(parameters, dict) or not parameters:
        return {"type": "object", "properties": {}}
    schema = dict(parameters)
    schema.setdefault("type", "object")
    if schema["type"] == "object":
        schema.setdefault("properties", {})
    return schema


class AnthropicProvider(Provider):
    """Claude models. A keyed provider; heads no job in the current table -- ``expert`` moved to ``deepseek-v4-pro-0813``.

    Expert Mode exists because the other chains are tuned for a different trade:
    the main model answers in a couple of seconds and is right about ordinary
    work, and that is the correct default. When the user explicitly asks for the
    strongest available reasoning -- a hard bug, an architectural decision, a
    plan with consequences -- they are choosing to wait, and this is what they
    are choosing to wait for.
    """

    id = "anthropic"
    name = "Anthropic"
    credential = "ANTHROPIC_API_KEY"
    console_url = "https://console.anthropic.com/settings/keys"

    #: Claude models think before answering on the thinking variants, so a cold
    #: request can legitimately sit silent for a while before the first token.
    #: Shorter than NVIDIA's 120 s because there is no model cold-start here --
    #: this is thinking time, not scheduling time.
    probe_timeout_s: float = 60.0
    stream_cold_start_s: float | None = 90.0

    def __init__(self) -> None:
        self._client: Any = None

    # ------------------------------------------------------------------- models

    @property
    def models(self) -> tuple[ModelSpec, ...]:
        """The Claude models this endpoint serves.

        This list is not aspirational: it is the catalogue the configured endpoint
        returned from ``/v1/models``, verified by calling it. Pointing
        ``ANTHROPIC_BASE_URL`` at api.anthropic.com directly makes more models
        reachable (Sonnet and Haiku among them) and they belong here when that is
        the setup -- but declaring candidates the current endpoint does not serve
        would only lengthen the Expert chain with entries that 404.
        """
        note = (
            "Reached through ANTHROPIC_BASE_URL, so availability follows that "
            "endpoint's catalogue rather than Anthropic's full range."
        )
        return (
            ModelSpec(
                id="claude-opus-5-thinking",
                provider=self.id,
                label="Claude Opus 5 (thinking)",
                context_window=200_000,
                max_output_tokens=64_000,
                supports_vision=True,
                strengths=("reasoning", "coding", "long_context", "vision"),
                latency="slow",
                notes="Expert Mode's first choice: extended thinking before it answers. " + note,
            ),
            ModelSpec(
                id="claude-opus-5",
                provider=self.id,
                label="Claude Opus 5",
                context_window=200_000,
                max_output_tokens=64_000,
                supports_vision=True,
                strengths=("reasoning", "coding", "long_context", "vision"),
                latency="medium",
                notes="Opus without the thinking budget: same capability, faster to first token. "
                      + note,
            ),
            ModelSpec(
                id="claude-opus-4-8-thinking",
                provider=self.id,
                label="Claude Opus 4.8 (thinking)",
                context_window=200_000,
                max_output_tokens=64_000,
                supports_vision=True,
                strengths=("reasoning", "coding", "long_context", "vision"),
                latency="slow",
                notes="The previous Opus generation, kept as Expert Mode's fallback. " + note,
            ),
            ModelSpec(
                id="claude-opus-4-8",
                provider=self.id,
                label="Claude Opus 4.8",
                context_window=200_000,
                max_output_tokens=64_000,
                supports_vision=True,
                strengths=("reasoning", "coding", "long_context", "vision"),
                latency="medium",
                notes=note,
            ),
        )

    # -------------------------------------------------------------- credentials

    def _key(self) -> str:
        for name in CREDENTIAL_NAMES:
            value = secrets.get(name)
            if value:
                return value
        raise MissingCredential(
            "No Anthropic key is configured. Set ANTHROPIC_API_KEY (or "
            "ANTHROPIC_AUTH_TOKEN for a gateway) in Settings -> Providers.",
            provider=self.id,
        )

    def has_credential(self) -> bool:
        return any(secrets.get(name) for name in CREDENTIAL_NAMES)

    def _headers(self) -> dict[str, str]:
        key = self._key()
        return {
            # Both, deliberately -- see the module docstring. Anthropic reads
            # x-api-key and ignores Authorization; gateways do the reverse.
            "x-api-key": key,
            "Authorization": f"Bearer {key}",
            "anthropic-version": API_VERSION,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": USER_AGENT,
        }

    # ------------------------------------------------------------------- client

    async def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                base_url=base_url(),
                timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
                follow_redirects=False,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -------------------------------------------------------------- translation

    def _content_blocks(
        self, message: Message, names: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        """One message's content as Messages-API blocks.

        ``names`` maps NEXUS tool names to their wire spelling; see
        :func:`~nexus.providers.base.wire_tool_names`. Historical ``tool_use``
        blocks must be renamed
        with exactly the same map as the tool list, because the API validates a
        replayed call against the declared tools and a mismatch invalidates the
        whole conversation, not just that block.
        """
        blocks: list[dict[str, Any]] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        for attachment in message.attachments:
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": attachment.media_type,
                        "data": attachment.data,
                    },
                }
            )
        for call in message.tool_calls:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": (names or {}).get(call.name, call.name),
                    # The parsed dict, not the raw string: the API wants an object,
                    # and a call that failed to parse never reaches a provider --
                    # the core rejects it and reports the parse error instead.
                    "input": call.arguments or {},
                }
            )
        return blocks

    def _payload(self, request: ChatRequest, *, stream: bool = True) -> dict[str, Any]:
        """Translate a :class:`ChatRequest` into a Messages API body.

        The interesting work is collapsing roles. NEXUS's core emits one
        ``role="tool"`` message per tool call, but this API expects every
        ``tool_result`` answering one assistant turn to sit in a *single* user
        message, and it rejects two consecutive messages with the same role. So
        adjacent same-role messages are merged rather than sent as-is.

        The second piece of translation is tool *names*: NEXUS names tools
        ``apps.close``, and this API's grammar has no dot in it. See
        :func:`~nexus.providers.base.wire_tool_names`.
        """
        to_wire, _ = wire_tool_names(request.tools)
        merged: list[dict[str, Any]] = []

        def append(role: str, blocks: list[dict[str, Any]]) -> None:
            if not blocks:
                return
            if merged and merged[-1]["role"] == role:
                merged[-1]["content"].extend(blocks)
            else:
                merged.append({"role": role, "content": blocks})

        for message in request.messages:
            if message.role == "tool":
                append(
                    "user",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.tool_call_id or "",
                            "content": message.content or "",
                        }
                    ],
                )
            elif message.role == "system":
                # A system message inside the list is a caller mistake, but
                # dropping it silently would discard instructions. Carry it as
                # user text and say so, so the model does not treat it as the
                # user's own words.
                append("user", [{"type": "text", "text": f"[system] {message.content}"}])
            else:
                append(message.role, self._content_blocks(message, to_wire))

        # The API requires the first message to be from the user. This only
        # happens if a caller hands over a history that opens with an assistant
        # turn; refusing outright would be worse than stating the situation.
        if merged and merged[0]["role"] != "user":
            merged.insert(
                0,
                {"role": "user", "content": [{"type": "text", "text": "(continuing)"}]},
            )

        spec = self.model(request.model)
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": merged,
            # Required by this API. The request's own cap wins; otherwise the
            # model's declared ceiling, and 4096 for a model we do not know
            # (a gateway may serve ids not in our catalogue).
            "max_tokens": request.max_output_tokens
            or (spec.max_output_tokens if spec else 4_096),
            "stream": stream,
        }
        if request.system:
            payload["system"] = request.system
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.stop:
            payload["stop_sequences"] = request.stop
        if request.tools:
            payload["tools"] = [
                {
                    "name": to_wire.get(
                        str(schema.get("name", "")), str(schema.get("name", ""))
                    ),
                    "description": schema.get("description", ""),
                    "input_schema": _wire_input_schema(schema.get("parameters")),
                }
                for schema in request.tools
            ]
            if request.tool_choice == "required":
                payload["tool_choice"] = {"type": "any"}
            elif request.tool_choice == "none":
                payload["tool_choice"] = {"type": "none"}
        return payload

    # -------------------------------------------------------------------- errors

    def _map_error(self, status: int, body: bytes, model: str) -> ProviderError:
        """Reuse the shared HTTP mapping, then correct the two cases it gets wrong.

        The shared mapper reads OpenAI-shaped bodies. Anthropic's envelope is
        ``{"type":"error","error":{"type":...,"message":...}}``, whose *message*
        lands in the right place but whose 401-vs-404 distinction does not: a
        gateway with a bad token often answers 404 rather than 401. Anthropic's
        own ``authentication_error`` type is the reliable signal, so it is read
        directly here instead of inferred from the status code.

        The second correction matters more. A 403 carrying an **HTML** body is not
        the API rejecting a credential -- it is something in front of the API (a
        WAF, a proxy, a captive portal) refusing the client before the request
        ever arrived. Reported as "rejected the API key" it sends the user off to
        rotate a key that was never seen, which is exactly the misdiagnosis §78
        is about. So the body is sniffed and named for what it is.
        """
        text = body.decode("utf-8", errors="replace")
        stripped = text.lstrip()[:200].lower()
        if stripped.startswith("<!doctype") or stripped.startswith("<html"):
            return ProviderError(
                f"{base_url()} returned a web page instead of an API response "
                f"(HTTP {status}). The credential was never checked -- something in "
                "front of the API refused the connection. If the endpoint is behind "
                "Cloudflare this is usually a blocked client rather than a bad key.",
                provider=self.id, model=model, detail=text[:500],
            )

        mapped = map_http_error(
            status, body, provider_id=self.id, provider_name=self.name, model=model
        )
        try:
            parsed = json.loads(text)
            error_type = str((parsed.get("error") or {}).get("type") or "")
        except (json.JSONDecodeError, AttributeError):
            return mapped
        if error_type in ("authentication_error", "permission_error"):
            return AuthenticationFailed(
                f"{self.name} rejected the credential: {mapped.message}",
                provider=self.id, model=model, detail=mapped.detail,
            )
        return mapped

    # ----------------------------------------------------------------- streaming

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        """Stream a Messages API response as :class:`StreamEvent`s.

        The two stall guards are the same idea as in ``openai_compat``: a
        provider that never starts is a different failure from one that stops
        halfway, and only the second has partial work to report.
        """
        client = await self._http()
        payload = self._payload(request, stream=True)
        # The same map ``_payload`` used, rebuilt rather than threaded through: it is
        # a pure function of the declared tool list, so the two agree by
        # construction. Names come back from the wire and must be turned into the
        # names the runtime will look up.
        _, from_wire = wire_tool_names(request.tools)
        cold_start = self.stream_cold_start_s or settings().stream_cold_start_s
        idle = settings().stream_idle_s

        import asyncio

        import httpx

        text_so_far: list[str] = []
        usage = Usage()
        stop_reason: str | None = None
        saw_first_event = False
        # Tool-use blocks under construction, keyed by the stream's block index.
        # Keyed rather than kept as a single "current" slot because the protocol
        # does not promise blocks are closed before the next one opens.
        pending: dict[int, dict[str, str]] = {}
        completed: list[ToolCall] = []

        try:
            async with client.stream(
                "POST", "/v1/messages", json=payload, headers=self._headers()
            ) as response:
                if response.status_code >= 400:
                    raise self._map_error(
                        response.status_code, await response.aread(), request.model
                    )

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
                            f"{len(''.join(text_so_far))} characters and went quiet "
                            f"for {idle:.0f} seconds.",
                            partial_text="".join(text_so_far),
                            provider=self.id, model=request.model,
                        ) from None

                    if not line or not line.startswith("data:"):
                        # ``event:`` lines carry the same type as the JSON body's
                        # own "type" field, so the body is the single source and
                        # these are skipped rather than parsed twice.
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        log.warning("unparseable stream chunk", extra={"provider": self.id})
                        continue

                    saw_first_event = True
                    kind = str(chunk.get("type") or "")

                    if kind == "error":
                        detail = chunk.get("error") or {}
                        raise StreamInterrupted(
                            f"{self.name} reported an error mid-response: "
                            f"{detail.get('message') or 'no detail given'}",
                            partial_text="".join(text_so_far),
                            provider=self.id, model=request.model,
                        )

                    if kind == "message_start":
                        raw = (chunk.get("message") or {}).get("usage") or {}
                        usage.input_tokens = int(raw.get("input_tokens") or 0)
                        usage.cached_input_tokens = int(
                            raw.get("cache_read_input_tokens") or 0
                        )
                        continue

                    if kind == "content_block_start":
                        block = chunk.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            wire_name = str(block.get("name") or "")
                            pending[int(chunk.get("index", 0))] = {
                                "id": str(block.get("id") or ""),
                                # Translated here, once, so both places that finalise
                                # a call already hold the runtime's name. An unknown
                                # name passes through unchanged: the model inventing a
                                # tool is a real event and the runtime's "no such
                                # tool" is the honest report of it.
                                "name": from_wire.get(wire_name, wire_name),
                                "arguments": "",
                            }
                        continue

                    if kind == "content_block_delta":
                        delta = chunk.get("delta") or {}
                        delta_type = str(delta.get("type") or "")
                        if delta_type == "text_delta":
                            fragment = str(delta.get("text") or "")
                            if fragment:
                                text_so_far.append(fragment)
                                yield StreamEvent(type="text", text=fragment)
                        elif delta_type == "thinking_delta":
                            fragment = str(delta.get("thinking") or "")
                            if fragment:
                                yield StreamEvent(type="reasoning", text=fragment)
                        elif delta_type == "input_json_delta":
                            slot = pending.get(int(chunk.get("index", 0)))
                            if slot is not None:
                                slot["arguments"] += str(delta.get("partial_json") or "")
                        continue

                    if kind == "content_block_stop":
                        slot = pending.pop(int(chunk.get("index", 0)), None)
                        if slot and slot["name"]:
                            completed.append(
                                ToolCall.from_raw(
                                    slot["id"] or f"call_{len(completed)}",
                                    slot["name"],
                                    slot["arguments"],
                                )
                            )
                        continue

                    if kind == "message_delta":
                        reason = (chunk.get("delta") or {}).get("stop_reason")
                        if reason:
                            stop_reason = str(reason)
                        raw = chunk.get("usage") or {}
                        if raw.get("output_tokens") is not None:
                            usage.output_tokens = int(raw["output_tokens"])
                        continue

        except ProviderError:
            raise
        except httpx.ConnectError as exc:
            raise NetworkUnreachable(
                f"Could not reach {self.name} at {base_url()}. Check that this "
                "computer is online and that the base URL is right.",
                provider=self.id, model=request.model, detail=str(exc),
            ) from exc
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise ProviderTimeout(
                f"{self.name} timed out -- {_describe_exception(exc)}.",
                provider=self.id, model=request.model, detail=_describe_exception(exc),
            ) from exc
        except httpx.HTTPError as exc:
            raise StreamInterrupted(
                f"The connection to {self.name} broke: {_describe_exception(exc)}.",
                partial_text="".join(text_so_far),
                provider=self.id, model=request.model,
            ) from exc

        # A block left open when the stream ended is still a real request the
        # model made; dropping it would lose an action the user asked for.
        for index in sorted(pending):
            slot = pending[index]
            if slot["name"]:
                completed.append(
                    ToolCall.from_raw(
                        slot["id"] or f"call_{index}", slot["name"], slot["arguments"]
                    )
                )

        for call in completed:
            yield StreamEvent(type="tool_call", tool_call=call)

        if usage.total:
            yield StreamEvent(type="usage", usage=usage)

        if not saw_first_event:
            raise StreamInterrupted(
                f"{self.name} accepted the request but sent no response.",
                provider=self.id, model=request.model,
            )

        yield StreamEvent(
            type="done",
            # "end_turn" is this API's spelling of a normal finish; the rest of
            # NEXUS speaks the chat-completions vocabulary, so translate rather
            # than leaking a provider-specific token upward.
            finish_reason={"end_turn": "stop", "max_tokens": "length",
                           "tool_use": "tool_calls"}.get(
                stop_reason or "end_turn", stop_reason or "stop"
            ),
            model=request.model,
            usage=usage if usage.total else None,
        )

    # -------------------------------------------------------------- verification

    async def verify(self) -> VerificationResult:
        """List models, then actually ask one for a word (§13, §78)."""
        if not self.has_credential():
            return VerificationResult(
                ok=False,
                message=(
                    "No Anthropic key is configured. Set ANTHROPIC_API_KEY, or "
                    "ANTHROPIC_AUTH_TOKEN if you are using a gateway."
                ),
                error_kind="missing_credential",
            )

        started = time.monotonic()
        client = await self._http()
        import httpx

        where = base_url()
        models_seen: list[str] = []
        try:
            listing = await client.get(
                "/v1/models",
                headers={k: v for k, v in self._headers().items()
                         if k != "Accept"},
                timeout=20.0,
            )
            if listing.status_code < 400:
                body = listing.json()
                entries = body.get("data") if isinstance(body, dict) else None
                if isinstance(entries, list):
                    models_seen = [
                        str(e.get("id")) for e in entries if isinstance(e, dict)
                    ][:200]
        except (httpx.HTTPError, ValueError):
            # Not every gateway implements /v1/models. Not fatal -- the real
            # check is the completion below.
            pass

        probe = ChatRequest(
            messages=[Message(role="user", content="Reply with the single word: ok")],
            model=self.default_model().id,
            max_output_tokens=8,
            temperature=0.0,
        )
        try:
            response = await client.post(
                "/v1/messages",
                json=self._payload(probe, stream=False),
                headers=self._headers(),
                timeout=self.probe_timeout_s,
            )
            if response.status_code >= 400:
                error = self._map_error(
                    response.status_code, await response.aread(), probe.model
                )
                return VerificationResult(
                    ok=False, message=error.message, models_seen=models_seen,
                    error_kind=error.kind,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            body = response.json()
            reply = "".join(
                str(block.get("text") or "")
                for block in (body.get("content") or [])
                if isinstance(block, dict) and block.get("type") == "text"
            )
            latency = int((time.monotonic() - started) * 1000)
            return VerificationResult(
                ok=True,
                message=(
                    f"{self.name} responded in {latency} ms using "
                    f"{body.get('model') or probe.model} at {where}"
                    + (f" ({len(models_seen)} models available)" if models_seen else "")
                    + (f": {reply.strip()[:40]}" if reply.strip() else "")
                ),
                models_seen=models_seen,
                latency_ms=latency,
            )
        except httpx.ConnectError:
            return VerificationResult(
                ok=False,
                message=(
                    f"Could not reach {self.name} at {where}. Check that this "
                    "computer is online and that ANTHROPIC_BASE_URL is correct."
                ),
                models_seen=models_seen, error_kind="network_unreachable",
            )
        except (httpx.HTTPError, ValueError) as exc:
            if models_seen and isinstance(exc, httpx.TimeoutException):
                return VerificationResult(
                    ok=False,
                    message=(
                        f"Your {self.name} credential works -- it listed "
                        f"{len(models_seen)} models -- but {probe.model} did not "
                        f"answer within {self.probe_timeout_s:.0f} seconds. That is "
                        "usually a busy endpoint rather than a problem with the key."
                    ),
                    models_seen=models_seen, error_kind="timeout",
                )
            return VerificationResult(
                ok=False,
                message=f"{self.name} could not be checked: {_describe_exception(exc)}",
                models_seen=models_seen, error_kind="provider_error",
            )

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        # The endpoint is part of what "configured" means for this provider, and a
        # user pointing at a gateway needs to see which one they are pointing at.
        data["base_url"] = base_url()
        data["credential_names"] = list(CREDENTIAL_NAMES)
        return data


__all__ = ["API_VERSION", "AnthropicProvider", "CREDENTIAL_NAMES", "base_url"]
