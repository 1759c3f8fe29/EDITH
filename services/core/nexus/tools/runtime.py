"""Tool execution runtime (task D2; spec §35, §57, §59-§62).

The single path from intent to effect. Every call goes through the same six
gates, in this order:

1. **Exists** -- an unknown tool name is an error, never a guess.
2. **Available** -- required binaries present, or a specific reason why not.
3. **Valid** -- parameters checked against the tool's schema.
4. **Permitted** -- the permission engine decides, prompting if needed.
5. **Executed** -- in a worker thread with a timeout.
6. **Verified** -- an independent check that the effect really happened.

Every outcome is recorded in ``tool_invocations`` and ``activity``, and published
on the event bus, whether it succeeded or not. A failure must be visible (§79);
a failure reported as success is the specific thing this file exists to prevent.

Voice feedback: When a tool starts, times out, or fails, NEXUS speaks a brief
announcement so the user knows what's happening without watching the screen.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..config import settings
from ..core.events import bus
from ..db.repos import activity, app_settings, permissions_store
from ..logging_setup import get_logger
from ..permissions import capabilities, policy
from ..permissions.engine import engine
from . import registry
from .registry import SchemaError, Tool, ToolResult

log = get_logger("tools.runtime")


class ToolNotFound(KeyError):
    pass


def _format_voice(text: str | None, params: dict[str, Any], default: str) -> str:
    """Format a voice announcement template with parameters."""
    if text is None:
        return default
    try:
        return text.format(**params)
    except (KeyError, ValueError):
        return default


async def _speak_tool_voice(text: str, conversation_id: str | None = None) -> None:
    """Speak a tool voice announcement via TTS if enabled.

    Supports bilingual (Hindi/English) announcements based on detected language.
    """
    try:
        # Check if TTS is enabled
        tts_enabled = bool(app_settings.get("tts_enabled", True))
        if not tts_enabled:
            return
        # Use the voice_stream module to synthesize
        from ..ai import voice_stream
        # We only need to synthesize, not stream the whole turn
        provider, reason = voice_stream._tts_model()
        if provider is None:
            return
        voice = voice_stream._voice(None, provider.provider_id, text)
        language = voice_stream._tts_language(text)
        path, error = await voice_stream.synthesize_one(
            text, provider_id=provider.provider_id, model=provider.model_id, voice=voice, language=language
        )
        # The file is written by the provider; we don't need to play it here
        # The UI will pick it up via the audio endpoint
    except Exception:
        # Voice is best-effort; never fail the tool because voice failed
        log.debug("tool voice announcement failed", exc_info=True)


def _bilingual_text(english: str, hindi: str, language: str | None = None) -> str:
    """Return English or Hindi text based on language detection."""
    if language:
        # Normalize to primary subtag
        primary = language.split("-")[0].lower() if "-" in language else language.lower()
        if primary in ("hi", "mr", "bn", "gu", "pa", "ta", "te", "kn", "ml", "or", "as"):
            return hindi
    return english


def _bilingual_voice(
    english_start: str, hindi_start: str,
    english_timeout: str, hindi_timeout: str,
    english_error: str, hindi_error: str,
    cleaned: dict[str, Any],
    tool: Tool
) -> tuple[str, str, str]:
    """Generate bilingual voice announcements for a tool."""
    # Detect language from parameters or use English as default
    # Check for text fields that might indicate language
    detected_lang = None
    for key, value in cleaned.items():
        if isinstance(value, str) and value.strip():
            detected_lang = value
            break

    # Also check conversation context if available
    # (This would need conversation history, for now use params)

    start = _bilingual_text(english_start, hindi_start, detected_lang)
    timeout = _bilingual_text(english_timeout, hindi_timeout, detected_lang)
    error = _bilingual_text(english_error, hindi_error, detected_lang)

    # Format with parameters
    start = _format_voice(start, cleaned, start)
    timeout = _format_voice(timeout, cleaned, timeout)
    error = _format_voice(error, cleaned, error)

    return start, timeout, error


async def execute(
    name: str,
    params: dict[str, Any] | None = None,
    *,
    task_id: str | None = None,
    step_id: str | None = None,
    conversation_id: str | None = None,
    require_permission: bool = True,
    permission_timeout: float | None = None,
) -> ToolResult:
    params = dict(params or {})
    started = time.monotonic()

    tool = registry.get(name)
    if tool is None:
        close = _suggest(name)
        hint = f" Did you mean {close}?" if close else ""
        return _fail(
            name,
            params,
            f"There is no tool called '{name}'.{hint}",
            task_id=task_id,
            step_id=step_id,
            conversation_id=conversation_id,
            started=started,
        )

    reason = tool.unavailable_reason()
    if reason:
        return _fail(
            tool.name,
            params,
            f"{tool.title} is not available on this computer. {reason}",
            task_id=task_id,
            step_id=step_id,
            conversation_id=conversation_id,
            started=started,
        )

    try:
        cleaned = registry.validate(tool.parameters, params)
    except SchemaError as exc:
        return _fail(
            tool.name,
            params,
            f"{tool.title} was called incorrectly: {exc}",
            task_id=task_id,
            step_id=step_id,
            conversation_id=conversation_id,
            started=started,
        )

    if require_permission:
        decision = await engine.require(
            tool.capability,
            cleaned,
            task_id=task_id,
            conversation_id=conversation_id,
            timeout=permission_timeout,
        )
        if not decision.allowed:
            result = ToolResult(
                ok=False,
                error=decision.reason,
                summary=decision.reason,
                duration_ms=_elapsed(started),
            )
            _record(tool, cleaned, result, task_id, step_id, conversation_id,
                    denied=True, denial_source=decision.source, detail=decision.detail)
            return result

    bus.publish(
        "tool.started",
        {
            "tool": tool.name,
            "title": tool.title,
            "description": policy.describe_request(tool.capability, cleaned),
            "task_id": task_id,
            "step_id": step_id,
        },
    )

    # Voice announcement: tool starting
    start_text = _format_voice(
        tool.voice_start,
        cleaned,
        f"Starting {tool.title.lower()}"
    )
    asyncio.create_task(_speak_tool_voice(start_text, conversation_id))

    timeout = tool.timeout_s or settings().tool_timeout_s
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(tool.handler, cleaned), timeout=timeout
        )
    except asyncio.TimeoutError:
        # The worker thread cannot be forcibly killed, so we say what we actually
        # know: we stopped waiting. Claiming it was cancelled would be a lie.
        result = ToolResult(
            ok=False,
            error=(
                f"{tool.title} did not finish within {timeout:.0f} seconds, so NEXUS "
                "stopped waiting. If it had already started changing something, that "
                "change may still complete in the background."
            ),
            summary=f"{tool.title} timed out after {timeout:.0f}s",
        )
        # Voice announcement: tool timeout
        timeout_text = _format_voice(
            tool.voice_timeout,
            cleaned,
            f"{tool.title} is not responding. Trying again."
        )
        asyncio.create_task(_speak_tool_voice(timeout_text, conversation_id))
    except Exception as exc:  # noqa: BLE001 - the boundary where tools fail
        log.error("tool raised", extra={"tool": tool.name}, exc_info=True)
        result = ToolResult(
            ok=False,
            error=f"{tool.title} failed: {exc}",
            summary=f"{tool.title} failed",
        )
        # Voice announcement: tool error
        error_text = _format_voice(
            tool.voice_error,
            cleaned,
            f"{tool.title} failed."
        )
        asyncio.create_task(_speak_tool_voice(error_text, conversation_id))

    if not isinstance(result, ToolResult):  # pragma: no cover - handler contract
        result = ToolResult(
            ok=False,
            error=f"{tool.name} returned {type(result).__name__}, not a ToolResult",
        )

    result.duration_ms = _elapsed(started)

    if result.ok and tool.verifier is not None:
        result = _verify(tool, cleaned, result)
    elif result.ok and not result.verification:
        # No verifier, and the handler did not describe its own evidence. Say so
        # rather than leaving `verified` looking like a confirmed True (§60).
        result.verified = None
        result.verification = "This tool has no independent check; the result is what the system reported."

    if not result.summary:
        result.summary = (
            policy.describe_request(tool.capability, cleaned)
            if result.ok
            else (result.error or f"{tool.title} failed")
        )

    _record(tool, cleaned, result, task_id, step_id, conversation_id)
    return result


def _verify(tool: Tool, params: dict[str, Any], result: ToolResult) -> ToolResult:
    """Run the tool's verifier (§60).

    A verifier that raises is treated as "could not verify" -- not as success and
    not as failure of the action itself, because those are different facts.
    """
    assert tool.verifier is not None
    try:
        confirmed, detail = tool.verifier(params, result)
    except Exception as exc:  # noqa: BLE001
        log.warning("verifier raised", extra={"tool": tool.name}, exc_info=True)
        result.verified = False
        result.verification = f"Could not confirm the result: {exc}"
        return result

    result.verified = bool(confirmed)
    result.verification = detail
    if not confirmed:
        # The action reported success but the world does not show it. The honest
        # outcome is failure (§79).
        result.ok = False
        result.error = result.error or (
            f"{tool.title} reported success, but the check did not confirm it: {detail}"
        )
        result.summary = f"{tool.title} could not be confirmed"
    return result


def _record(
    tool: Tool,
    params: dict[str, Any],
    result: ToolResult,
    task_id: str | None,
    step_id: str | None,
    conversation_id: str | None,
    *,
    denied: bool = False,
    denial_source: str | None = None,
    detail: str = "",
) -> None:
    permissions_store.record_invocation(
        tool.name,
        params,
        ok=result.ok,
        # Tri-state is preserved into the audit log: collapsing None to False
        # would make the History screen report "not verified" for actions that
        # were never verifiable in the first place.
        verified=result.verified,
        result=result.data,
        error=result.error,
        duration_ms=result.duration_ms,
        task_id=task_id,
        step_id=step_id,
    )

    cap = capabilities.get(tool.capability)
    if denied:
        level = "warning"
        summary = f"Blocked: {policy.describe_request(tool.capability, params)}"
    elif result.ok:
        level = "success"
        summary = result.summary
    else:
        level = "error"
        summary = result.summary or f"{tool.title} failed"

    activity.record(
        _activity_kind(cap.group),
        summary,
        detail={
            "tool": tool.name,
            "capability": tool.capability,
            "params": _redact_params(params),
            "ok": result.ok,
            "verified": result.verified,
            "verification": result.verification,
            "error": result.error,
            "duration_ms": result.duration_ms,
            "denial_source": denial_source,
            "denial_detail": detail,
        },
        level=level,  # type: ignore[arg-type]
        task_id=task_id,
        conversation_id=conversation_id,
    )

    bus.publish(
        "tool.finished" if not denied else "tool.blocked",
        {
            "tool": tool.name,
            "title": tool.title,
            "ok": result.ok,
            "verified": result.verified,
            "verification": result.verification,
            "summary": summary,
            "error": result.error,
            "duration_ms": result.duration_ms,
            "task_id": task_id,
            "step_id": step_id,
        },
    )


_GROUP_TO_KIND = {
    "Applications": "application",
    "Files": "file",
    "Terminal": "terminal",
    "Browser": "browser",
    "System": "system",
    "Screen": "screen",
    "Memory": "memory",
}


def _activity_kind(group: str) -> str:
    return _GROUP_TO_KIND.get(group, "tool")


_SENSITIVE_KEYS = ("content", "text", "body", "password", "token")


def _redact_params(params: dict[str, Any]) -> dict[str, Any]:
    """Keep the audit useful without copying whole file bodies into the log."""
    out: dict[str, Any] = {}
    for key, value in params.items():
        if key in _SENSITIVE_KEYS and isinstance(value, str) and len(value) > 200:
            out[key] = f"<{len(value)} characters>"
        else:
            out[key] = value
    return out


def _fail(
    name: str,
    params: dict[str, Any],
    message: str,
    *,
    task_id: str | None,
    step_id: str | None,
    conversation_id: str | None,
    started: float,
) -> ToolResult:
    result = ToolResult(
        ok=False, error=message, summary=message, duration_ms=_elapsed(started)
    )
    permissions_store.record_invocation(
        name, params, ok=False, result=None, error=message,
        duration_ms=result.duration_ms, task_id=task_id, step_id=step_id,
    )
    activity.record(
        "error",
        message,
        detail={"tool": name, "params": _redact_params(params)},
        level="error",
        task_id=task_id,
        conversation_id=conversation_id,
    )
    bus.publish("tool.finished", {"tool": name, "ok": False, "error": message,
                                  "summary": message, "task_id": task_id, "step_id": step_id})
    return result


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _suggest(name: str) -> str | None:
    """Nearest registered tool name, for a useful error instead of a dead end."""
    import difflib

    matches = difflib.get_close_matches(name, [t.name for t in registry.all_tools()], n=1, cutoff=0.6)
    return f"'{matches[0]}'" if matches else None


# ------------------------------------------------------------------ dry runs


def preview(name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """What would happen, without doing it.

    Powers the confirmation dialog and agent-mode plan review (§21): the user
    sees the concrete effect, the capability at stake, and whether they will be
    asked -- before anything runs.
    """
    params = dict(params or {})
    tool = registry.get(name)
    if tool is None:
        return {"ok": False, "error": f"There is no tool called '{name}'."}

    problem: str | None = tool.unavailable_reason()
    cleaned = params
    if problem is None:
        try:
            cleaned = registry.validate(tool.parameters, params)
        except SchemaError as exc:
            problem = str(exc)

    decision = engine.evaluate(tool.capability, cleaned)
    cap = capabilities.get(tool.capability)
    return {
        "ok": problem is None,
        "error": problem,
        "tool": tool.name,
        "title": tool.title,
        "capability": tool.capability,
        "risk": cap.risk,
        "reversible": cap.reversible,
        "read_only": tool.read_only,
        "effect": policy.describe_request(tool.capability, cleaned),
        "consequence": cap.consequence,
        "permission": decision.to_dict(),
        "will_prompt": decision.outcome == "ask",
        "verifiable": tool.verifier is not None,
    }
