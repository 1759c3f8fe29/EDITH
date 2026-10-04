"""The turn loop: one sentence in, verified action and an honest report out.

This is the file the behaviour specs are actually about. Everything else in the
package prepares for it -- :mod:`persona` says who is answering, :mod:`intent`
says which model, :mod:`context` says what it knows -- and this module runs the
round trip:

    classify -> route -> assemble context -> stream
        -> (validate tool calls -> execute -> feed results back) * N
        -> persist -> report

Four decisions in here are load-bearing, and each one exists because the
alternative is a specific dishonesty the specs prohibit.

**Only a validated tool call can act.** Model output arrives as either text or a
:class:`ToolCall`. Text is streamed to the user and can never become an effect. A
tool call is checked against the registry's schema *before* it reaches
:func:`nexus.tools.runtime.execute`, and a call that fails validation becomes a
tool *error message* fed back to the model, never a silent drop and never a
best-effort guess at what the model meant. So there is no path by which "I opened
Firefox" appears in a reply without ``app.open`` having actually run.

**A malformed call is repaired, not invented.** Models emit invalid JSON
occasionally. The honest response is to hand the parse error back and let the
model try again, bounded by :data:`MAX_REPAIRS`. Guessing the arguments would mean
executing something the user never asked for and the model never actually
requested.

**Tool results go back verbatim, verification included.** The model sees ``ok``,
``verified`` and the summary exactly as the runtime produced them -- including
``verified: null`` for tools with no independent check. A layer that smoothed
``null`` into success here would make the tri-state pointless, because the model
is what turns it into the sentence the user reads.

**A cancelled turn stops the stream and the loop.** The specs are explicit that
stop must actually stop (§21, §79). Cancellation is checked on every streamed
event, before each round and before each tool call -- but a tool already running
is allowed to finish rather than being abandoned half-done, because an interrupted
write is worse than a completed one the user did not want. Text already streamed
is kept and the message is recorded as ``interrupted``: it is what the user saw.
What was already done is reported, because a cancelled turn that says nothing
leaves the user not knowing what state their machine is in.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from ..core.events import bus
from ..db.repos import app_settings, conversations
from ..logging_setup import get_logger, redact_data
from ..providers.base import (
    ChatRequest,
    Message,
    ProviderError,
    StreamInterrupted,
    ToolCall,
    Usage,
)
from ..router import Decision, NoModelAvailable, router
from ..tools import registry, runtime
from . import context as ctx_mod
from . import intent as intent_mod
from . import memory_extract, persona

log = get_logger("ai.core")

#: How many times the model may call tools and see results within one turn.
#:
#: High enough for real multi-step desktop work ("find the file, check it, move
#: it, verify"), low enough that a model looping on a failing tool cannot spend
#: the user's quota indefinitely. When the cap is hit the turn ends with a
#: statement that it was hit -- never with a summary pretending the work finished.
MAX_ROUNDS = 8

#: Repair attempts for a malformed tool call before giving up on it. Two, because
#: a model that cannot produce valid JSON for the same schema twice in a row will
#: not produce it on the fifth try either, and each attempt costs a round trip.
MAX_REPAIRS = 2

#: Cap on how much of a tool's data goes back into the prompt. Tool output can be
#: enormous -- a directory listing, a process table, a file read -- and an
#: unbounded result would push the conversation out of the window mid-turn. The
#: truncation is announced inside the payload so the model knows it is seeing part
#: of something and can ask for the rest instead of concluding from a fragment.
MAX_TOOL_RESULT_CHARS = 6_000


class TurnCancelled(Exception):
    """The user stopped this turn. Carries what had already been done."""

    def __init__(self, message: str = "Stopped.", *, completed: list[str] | None = None):
        super().__init__(message)
        self.completed = completed or []


@dataclass(slots=True)
class ToolRun:
    """One tool call and what actually came of it."""

    call: ToolCall
    ok: bool
    verified: bool | None
    summary: str
    error: str | None = None
    duration_ms: int | None = None
    rejected: str | None = None  # set when it never ran, with why

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.call.name,
            "arguments": self.call.arguments,
            "ok": self.ok,
            "verified": self.verified,
            "summary": self.summary,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "rejected": self.rejected,
        }


@dataclass(slots=True)
class Turn:
    """Everything that happened in one exchange, for the UI and the record."""

    conversation_id: str
    request: str
    message_id: str = ""
    classification: intent_mod.Classification | None = None
    decision: Decision | None = None
    text: str = ""
    reasoning: str = ""
    tools: list[ToolRun] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    rounds: int = 0
    latency_ms: int = 0
    status: str = "streaming"
    error: str | None = None
    notices: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def acted(self) -> bool:
        return any(t.ok for t in self.tools)

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "status": self.status,
            "text": self.text,
            "intent": self.classification.to_dict() if self.classification else None,
            "routing": self.decision.to_dict() if self.decision else None,
            "tools": [t.to_dict() for t in self.tools],
            "usage": self.usage.to_dict(),
            "rounds": self.rounds,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "notices": self.notices,
            "context": self.context,
        }


class AiCore:
    """Runs turns. One instance, because cancellation is per-conversation state."""

    def __init__(self) -> None:
        self._cancelled: set[str] = set()
        self._active: dict[str, Turn] = {}

    # ------------------------------------------------------------ cancellation

    def cancel(self, conversation_id: str) -> bool:
        """Ask the turn on this conversation to stop at its next checkpoint.

        Returns whether there was anything to stop, so the caller can tell the
        user "nothing was running" instead of silently doing nothing.
        """
        if conversation_id not in self._active:
            return False
        self._cancelled.add(conversation_id)
        bus.publish("turn.cancelling", {"conversation_id": conversation_id})
        return True

    def active(self) -> list[dict[str, Any]]:
        return [t.to_dict() for t in self._active.values()]

    def _check_cancelled(self, turn: Turn) -> None:
        if turn.conversation_id in self._cancelled:
            raise TurnCancelled(completed=[t.summary for t in turn.tools if t.ok])

    # -------------------------------------------------------------------- run

    async def stream(
        self,
        request: str,
        *,
        conversation_id: str,
        project_id: str | None = None,
        task_id: str | None = None,
        attachments: list[Any] | None = None,
        mode: str | None = None,
        job: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Run one turn, yielding events as they happen.

        Events are plain dicts so the API layer can put them straight on a
        WebSocket without a second translation. Every one carries ``type``; the
        types are ``routing``, ``context``, ``text``, ``reasoning``, ``tool_call``,
        ``tool_result``, ``notice``, ``error`` and ``done``.
        """
        started = time.monotonic()
        turn = Turn(conversation_id=conversation_id, request=request)
        self._active[conversation_id] = turn
        self._cancelled.discard(conversation_id)

        try:
            async for event in self._run(
                turn,
                project_id=project_id,
                task_id=task_id,
                attachments=attachments or [],
                mode=mode,
                forced_job=job,
            ):
                yield event
        except TurnCancelled as stop:
            turn.status = "interrupted"
            turn.error = None
            yield {"type": "notice", "text": _cancelled_report(stop)}
        except NoModelAvailable as exc:
            turn.status = "failed"
            turn.error = exc.message
            yield {"type": "error", "error": exc.message, "kind": "no_model"}
        except StreamInterrupted as exc:
            # Partial text already reached the user. Saying so is the only honest
            # option -- the alternative is an answer that stops mid-sentence with
            # no explanation.
            turn.status = "interrupted"
            turn.error = exc.message
            yield {"type": "error", "error": exc.message, "kind": "interrupted"}
        except ProviderError as exc:
            turn.status = "failed"
            turn.error = exc.message
            yield {"type": "error", "error": exc.message, "kind": exc.kind}
        except Exception as exc:  # noqa: BLE001 -- a turn must fail visibly, not vanish
            log.exception("turn failed")
            turn.status = "failed"
            turn.error = str(exc)
            yield {
                "type": "error",
                "error": f"Something went wrong running that turn: {exc}",
                "kind": "internal",
            }
        else:
            turn.status = "complete"
        finally:
            turn.latency_ms = int((time.monotonic() - started) * 1000)
            self._active.pop(conversation_id, None)
            self._cancelled.discard(conversation_id)
            self._persist(turn)
            bus.publish("turn.finished", turn.to_dict())
            yield {"type": "done", "turn": turn.to_dict()}

    async def _run(
        self,
        turn: Turn,
        *,
        project_id: str | None,
        task_id: str | None,
        attachments: list[Any],
        mode: str | None,
        forced_job: str | None,
    ) -> AsyncIterator[dict[str, Any]]:
        # ---- classify ------------------------------------------------------
        classification = intent_mod.classify(
            turn.request,
            has_attachments=bool(attachments),
            continuing_task=bool(task_id),
        )

        # A second opinion from a fast model, but only where the rules admitted
        # they were guessing and only when nothing has already been decided. A
        # pinned job (``forced_job``) means the caller knows what this turn is, and
        # asking a model to re-read a sentence whose answer is going to be
        # discarded is pure latency.
        if (
            forced_job is None
            and not classification.confident
            and app_settings.get("intent_model_enabled", True)
        ):
            classification = await intent_mod.refine(
                classification,
                turn.request,
                has_attachments=bool(attachments),
            )

        turn.classification = classification

        # ---- decide which job answers ---------------------------------------
        # Three inputs, in order of authority. ``forced_job`` is the caller being
        # explicit and wins outright. Then the response mode: Expert Mode is the
        # user saying "use the strongest model on this", and a mode the user
        # selected outranks a label a classifier inferred. The classification is
        # the default.
        #
        # Vision is the one thing no override may take away. A request with an
        # image attached needs a model that can see it, and Expert Mode cannot make
        # a blind model look at a screenshot -- so a visual turn keeps its vision
        # job and is told why, rather than being routed somewhere it will have to
        # guess at the picture.
        effective_mode = mode if mode in persona.MODES else persona.current_mode()
        mode_notice = ""
        if forced_job is not None:
            job = forced_job
        elif classification.needs_vision:
            job = classification.job
            if persona.MODE_JOB.get(effective_mode):
                mode_notice = (
                    f"{effective_mode.title()} Mode is on, but this request needs to "
                    "look at an image, so it is routed to a model that can see."
                )
        else:
            job = persona.job_for_mode(effective_mode, classification.job)

        # ---- route ---------------------------------------------------------
        decision = router().resolve(
            job,
            needs_vision=classification.needs_vision,
            needs_tools=classification.needs_tools,
        )
        turn.decision = decision
        yield {
            "type": "routing",
            "intent": classification.to_dict(),
            "routing": decision.to_dict(),
        }
        if mode_notice:
            turn.notices.append(mode_notice)
            yield {"type": "notice", "text": mode_notice}
        for warning in decision.warnings:
            turn.notices.append(warning)
            yield {"type": "notice", "text": warning}

        # ---- context -------------------------------------------------------
        window = decision.spec.context_window if decision.spec else 32_000
        assembled = await ctx_mod.assemble(
            turn.request,
            conversation_id=turn.conversation_id,
            project_id=project_id,
            task_id=task_id,
            max_context_tokens=window,
        )
        turn.context = assembled.to_dict()
        turn.notices.extend(assembled.notices)
        for note in assembled.notices + assembled.dropped:
            yield {"type": "notice", "text": note}
        yield {"type": "context", "context": assembled.to_dict()}

        # ---- build the conversation ----------------------------------------
        # Tools are attached whenever the model supports them and the intent might
        # need them. Withholding them from a turn the classifier misread would
        # leave the model able only to *describe* the action -- the exact failure
        # the specs call out.
        supports_tools = bool(decision.spec and decision.spec.supports_tools)
        use_tools = supports_tools and classification.needs_tools
        schemas = registry.schemas(only_available=True) if use_tools else []

        system = persona.system_prompt(
            job=job,
            mode=effective_mode,
            with_tools=bool(schemas),
            context_block=assembled.system_block(),
        )

        messages: list[Message] = [
            Message(role=str(t.get("role") or "user"), content=str(t.get("content") or ""))
            for t in assembled.turns
        ]
        messages.append(
            Message(role="user", content=turn.request, attachments=list(attachments))
        )

        # ---- the loop ------------------------------------------------------
        repairs = 0
        for round_index in range(MAX_ROUNDS):
            self._check_cancelled(turn)
            turn.rounds = round_index + 1

            request = ChatRequest(
                messages=messages,
                model=decision.model_id,
                system=system,
                tools=schemas,
                max_output_tokens=(
                    decision.spec.max_output_tokens if decision.spec else None
                ),
            )

            text_parts: list[str] = []
            calls: list[ToolCall] = []
            interrupted = False
            # ``aclosing`` because leaving a provider stream early must close it
            # now rather than whenever the generator happens to be collected: the
            # request is holding an HTTP connection open, and a stop the user can
            # see while the socket keeps streaming is not a stop.
            async with aclosing(
                router().stream(request, job=job, decision=decision)
            ) as provider_stream:
                async for event in provider_stream:
                    if event.type == "text" and event.text:
                        text_parts.append(event.text)
                        yield {"type": "text", "text": event.text}
                    elif event.type == "reasoning" and event.text:
                        turn.reasoning += event.text
                        yield {"type": "reasoning", "text": event.text}
                    elif event.type == "tool_call" and event.tool_call is not None:
                        calls.append(event.tool_call)
                    elif event.type == "usage" and event.usage is not None:
                        turn.usage.input_tokens += event.usage.input_tokens
                        turn.usage.output_tokens += event.usage.output_tokens
                        turn.usage.cached_input_tokens += event.usage.cached_input_tokens

                    # Checked per event, not only per round. Everything after this
                    # loop -- the next round, each tool call -- already had a
                    # checkpoint; the answer being typed out did not. So pressing
                    # stop during a long single-round reply set a flag nothing ever
                    # read: ``cancel()`` returned True and published
                    # ``turn.cancelling``, the text kept arriving to the end, and
                    # the turn was stored as ``complete``. A stop control that
                    # reports success while the thing it stopped continues is the
                    # §78 failure, in the one place the user is watching.
                    if turn.conversation_id in self._cancelled:
                        interrupted = True
                        break

            said = "".join(text_parts)
            turn.text += said

            # The partial text is kept, not discarded: it was already delivered to
            # the user, and the record should show what they actually saw. _persist
            # writes it with status "interrupted" (§29, §64).
            if interrupted:
                self._check_cancelled(turn)

            if not calls:
                return  # the model answered and asked for nothing; the turn is done

            messages.append(Message(role="assistant", content=said, tool_calls=calls))

            # Malformed calls never execute. They go back as errors so the model
            # can correct itself, which is a repair attempt and is counted.
            broken = [c for c in calls if c.parse_error]
            if broken and repairs >= MAX_REPAIRS:
                for call in broken:
                    run = ToolRun(
                        call=call, ok=False, verified=None,
                        summary=f"{call.name} was not run.",
                        rejected=f"the model could not produce valid arguments after "
                                 f"{MAX_REPAIRS} attempts ({call.parse_error})",
                    )
                    turn.tools.append(run)
                    yield {"type": "tool_result", "result": run.to_dict()}
                turn.notices.append(
                    "Some tool calls were dropped because the model kept sending "
                    "malformed arguments. Nothing was run for them."
                )
                return
            if broken:
                repairs += 1

            for call in calls:
                self._check_cancelled(turn)
                yield {
                    "type": "tool_call",
                    "call": call.to_dict(),
                    "preview": _safe_preview(call),
                }
                run, payload = await self._invoke(
                    call, conversation_id=turn.conversation_id, task_id=task_id
                )
                turn.tools.append(run)
                messages.append(
                    Message(
                        role="tool",
                        content=payload,
                        tool_call_id=call.id,
                        tool_name=call.name,
                    )
                )
                yield {"type": "tool_result", "result": run.to_dict()}

        # Round cap reached with the model still calling tools. Stating that is the
        # only honest ending -- a closing summary here would describe a finish that
        # did not happen.
        turn.notices.append(
            f"Stopped after {MAX_ROUNDS} rounds of tool calls without reaching an "
            "answer. What was done up to that point is listed above; the rest of "
            "the work has not been carried out."
        )
        yield {"type": "notice", "text": turn.notices[-1]}

    # ------------------------------------------------------------- one tool

    async def _invoke(
        self,
        call: ToolCall,
        *,
        conversation_id: str,
        task_id: str | None,
    ) -> tuple[ToolRun, str]:
        """Validate then execute one call, returning the run and the model's payload.

        Validation happens here rather than trusting the runtime's own gate purely
        for the sake of the message the model gets back: the runtime returns a
        failed :class:`ToolResult`, and a schema error phrased as "expected an
        integer for 'index'" is something a model can act on, while a generic
        failure teaches it nothing and it repeats the same call.
        """
        if call.parse_error:
            reason = (
                f"The arguments were not valid JSON ({call.parse_error}). Nothing "
                "was run. Send the call again with well-formed JSON."
            )
            run = ToolRun(call=call, ok=False, verified=None,
                          summary=f"{call.name} was not run.", rejected=reason)
            return run, _payload({"ok": False, "error": reason, "ran": False})

        tool = registry.get(call.name)
        if tool is None:
            names = ", ".join(sorted(t.name for t in registry.available_tools())[:40])
            reason = (
                f"There is no tool called '{call.name}'. Nothing was run. The tools "
                f"you can call are: {names}."
            )
            run = ToolRun(call=call, ok=False, verified=None,
                          summary=f"No tool named {call.name}.", rejected=reason)
            return run, _payload({"ok": False, "error": reason, "ran": False})

        try:
            params = registry.validate(tool.parameters, call.arguments)
        except registry.SchemaError as exc:
            reason = f"{exc} Nothing was run. Correct the arguments and call it again."
            run = ToolRun(call=call, ok=False, verified=None,
                          summary=f"{call.name} was not run: invalid arguments.",
                          rejected=str(exc))
            return run, _payload({"ok": False, "error": reason, "ran": False})

        # A cancel arriving mid-execution does not abandon the tool: an interrupted
        # file move is worse than a completed one nobody wanted. The loop stops at
        # the next checkpoint instead.
        result = await runtime.execute(
            call.name,
            params,
            conversation_id=conversation_id,
            task_id=task_id,
        )

        run = ToolRun(
            call=call,
            ok=result.ok,
            verified=result.verified,
            summary=result.summary or ("Done." if result.ok else "Failed."),
            error=result.error,
            duration_ms=result.duration_ms,
        )
        return run, _payload(
            {
                "ok": result.ok,
                "ran": True,
                "verified": result.verified,
                "verification": result.verification,
                "summary": result.summary,
                "error": result.error,
                "data": result.data,
            }
        )

    # ------------------------------------------------------------- persistence

    def _persist(self, turn: Turn) -> None:
        """Write the exchange down, including a turn that failed.

        A failed turn is still history: the user asked something and got an error,
        and a conversation that shows the question with no answer is a worse record
        than one that shows what went wrong.
        """
        try:
            conversations.add_message(turn.conversation_id, "user", turn.request)
            message = conversations.add_message(
                turn.conversation_id,
                "assistant",
                turn.text,
                status="streaming",
                capability=turn.classification.job if turn.classification else None,
                provider=turn.decision.provider_id if turn.decision else None,
                model=turn.decision.model_id if turn.decision else None,
            )
            turn.message_id = message["id"]
            conversations.finalize_message(
                message["id"],
                content=turn.text,
                status=turn.status if turn.status != "streaming" else "complete",
                tokens_in=turn.usage.input_tokens or None,
                tokens_out=turn.usage.output_tokens or None,
                latency_ms=turn.latency_ms or None,
                error=turn.error,
            )
        except Exception:  # noqa: BLE001 -- losing the record must not lose the reply
            log.exception("could not persist turn for %s", turn.conversation_id)
            return

        if turn.status == "complete" and bool(
            app_settings.get("memory_extract_enabled", True)
        ):
            # Proposals only, and out of band: the user's reply is already
            # delivered, and nothing is remembered until they approve it.
            asyncio.create_task(
                memory_extract.propose_from_turn(
                    turn.request,
                    turn.text,
                    conversation_id=turn.conversation_id,
                    message_id=turn.message_id,
                )
            )


# ------------------------------------------------------------------- helpers


def _payload(data: dict[str, Any]) -> str:
    """The tool result as the model sees it: JSON, truncated with a note if long.

    Scrubbed first (§56). This is the boundary where a tool's output becomes a
    ``role="tool"`` message, so it is where "never expose credentials in model
    context" is either enforced or not -- and it was not. ``redact`` existed but was
    wired only into the log filter, while ``terminal.run`` put its whole stdout and
    ``files.read`` put the whole file into ``data``, which went out verbatim on every
    round. One ``cat .env`` sent every key NEXUS holds to a cloud provider, and it was
    invisible: nothing in the UI or the stored conversation shows this payload.

    Before truncation, not after. Cutting first and scrubbing the remainder would leave
    the front half of a key that straddles the limit sitting in the part that is kept.
    """
    data = redact_data(data)
    try:
        text = json.dumps(data, default=str)
    except (TypeError, ValueError):
        text = json.dumps({"ok": data.get("ok"), "summary": str(data.get("summary"))})
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    keep = MAX_TOOL_RESULT_CHARS - 200
    return (
        text[:keep]
        + '", "_truncated": "This result was cut off here. '
        + f'{len(text) - keep:,} more characters were not included -- narrow the '
        + 'request if you need the rest rather than concluding from this part."}'
    )


def _safe_preview(call: ToolCall) -> dict[str, Any] | None:
    """The dry-run description of what a call would do, for the UI.

    Never raises: this is decoration for the activity feed, and a preview that
    fails must not take down the turn it was describing.
    """
    if call.parse_error:
        return None
    try:
        return runtime.preview(call.name, call.arguments)
    except Exception:  # noqa: BLE001
        return None


def _cancelled_report(stop: TurnCancelled) -> str:
    """What to tell the user after a stop, which is never just "stopped".

    If tools ran, the machine changed, and the user needs to know what changed --
    otherwise they are left guessing whether the half-finished work happened.
    """
    if not stop.completed:
        return "Stopped. Nothing had run yet, so nothing on this computer changed."
    done = " ".join(s.rstrip(".") + "." for s in stop.completed)
    return f"Stopped. These had already finished and were not undone: {done}"


_CORE: AiCore | None = None


def ai_core() -> AiCore:
    """The one :class:`AiCore`, because cancellation is per-process state.

    Named ``ai_core`` rather than ``core`` so that ``nexus.ai.core`` always means
    this module: an accessor called ``core`` exported from the package __init__
    shadows the submodule of the same name, and ``from nexus.ai import core``
    then silently hands back a function.
    """
    global _CORE
    if _CORE is None:
        _CORE = AiCore()
    return _CORE


__all__ = [
    "MAX_REPAIRS",
    "MAX_ROUNDS",
    "AiCore",
    "ToolRun",
    "Turn",
    "TurnCancelled",
    "ai_core",
]
