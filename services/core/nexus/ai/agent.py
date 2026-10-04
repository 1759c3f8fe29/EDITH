"""Agent mode: plan a multi-step task, run it, and verify each step (Phase G).

Everything a task is made of already existed before this module: ``db.repos.tasks``
stores plans and steps as data, ``tools.runtime.execute`` takes a ``task_id`` and a
``step_id`` and verifies what it did, and the permission engine sits in front of it.
What was missing was the loop between them -- so the three ``agent_*`` settings were
stored and inert, the Tasks screen had a permanent empty state, and NEXUS could do one
thing per sentence and no more.

Four rules this module is built around, each of which the easy version breaks:

1. **A plan is data, not prose.** Steps name a registered tool and validated
   parameters, or they are marked as thinking steps. Nothing re-reads a sentence at
   execution time and guesses which tool it meant.
2. **Progress is counted, never animated.** Every event published here carries real
   step counts from :func:`tasks.progress`. There is no step that exists only to make
   something move (§80).
3. **A step is finished when the machine says so, not when the tool returns.**
   ``runtime.execute`` verifies where a verifier exists, and verification stays
   tri-state all the way to the UI: confirmed, contradicted, or never checked. With
   ``agent_verify_steps`` on, *contradicted* stops the task -- but "no verifier
   exists" is not treated as failure, because that is not a fact about the step.
4. **The plan can be empty and that is an answer.** If no model is reachable, or the
   model returns nothing runnable, the task is left with a real error saying so. A
   task that invented steps to look busy would be the worst possible failure here.

**On ``agent_pause_on_failure``.** The stored state machine has no ``paused`` state
(``schema.py``: pending, running, waiting_permission, completed, failed, cancelled,
interrupted) and inventing one would put a status in the database that no other reader
understands. So the setting maps onto the two behaviours that state machine can
actually express: on -> stop at the first failed step and finish the task as
``failed``, with the failing step and its error kept; off -> ask the planner to
re-plan the remaining steps from what went wrong (§22 adaptation) and keep going,
once. Both are honest about where they stopped.

**On retries.** Before a step is called failed, its failure is classified and a
retry is either taken or refused with a stated reason (:mod:`nexus.ai.recovery`,
§13/§14). Three things about that are deliberate. A retry never invents a step
status -- the row goes back to ``running``, because the stored state machine has no
``retrying`` value and adding one would put a status in the database no other
reader understands. A tool that is not idempotent is never retried automatically,
whatever went wrong, because a failure that may have half-applied is not something
to repeat without a human. And a step that the tool reported as done but the
verifier contradicted is not retried at all: that is a disagreement about what
happened, and repeating the action cannot settle it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ..core.events import bus
from ..db.repos import app_settings as settings
from ..db.repos import tasks
from ..logging_setup import get_logger
from ..permissions.engine import engine
from ..providers.base import ChatRequest, Message
from ..router import NoModelAvailable, router
from ..tools import registry, runtime
from . import persona, recovery

log = get_logger("agent")

#: Hard ceiling on steps per run, whatever ``agent_max_steps`` says. A plan is
#: written by a model, and a model that returns forty steps for "tidy my desktop"
#: should be capped by something that is not itself a setting.
STEP_CEILING = 40

#: How long one step may take. Tool calls that need a permission answer block on a
#: human, so this is generous -- but not unbounded, because a task stuck forever
#: reports "running" forever, which §79 forbids.
STEP_TIMEOUT_S = 600.0


class PlanRejected(RuntimeError):
    """The model's plan could not be turned into steps, and why."""


_INSTRUCTIONS = """\
You are planning a task that will be executed on this computer, step by step, by a
program -- not by a person reading your answer. Return ONLY a JSON object:

{"steps": [
  {"intent": "what this step accomplishes, one short sentence",
   "tool": "exact_tool_name or null",
   "params": {"arg": "value"},
   "depends_on": [0]}
]}

Rules, all of them enforced by the program that reads this:

* "tool" must be one of the tool names listed below, spelled exactly, or null.
  A name that is not on the list is rejected and the whole plan is thrown away --
  the tools below are the only things that exist.
* "params" must match that tool's parameters. Required arguments are marked.
* "tool": null means a thinking step: no action, just working something out from
  the results of earlier steps. Use it for deciding, summarising or drafting.
  Never use a thinking step to pretend an action happened.
* "depends_on" lists the 0-based positions of steps that must finish first. It may
  only refer to EARLIER positions. Use it only for real ordering constraints.
* Plan the smallest number of steps that actually does the job. Do not add steps
  that check your own work: every step is verified automatically.
* If the objective cannot be done with these tools, return {"steps": []} and say
  nothing else. An empty plan is a valid, useful answer. Do not substitute
  something you can do for something you cannot.
"""


def _tool_menu(schemas: list[dict[str, Any]]) -> str:
    """The tool list, as the planner sees it.

    Built from the same registry that executes, filtered to what is actually
    installed and runnable on this machine, so a plan cannot name a tool that will
    turn out not to exist (§78). Required parameters are called out because the
    commonest planner failure is a real tool called with half its arguments.
    """
    lines: list[str] = []
    for schema in schemas:
        params = schema.get("parameters") or {}
        props = params.get("properties") or {}
        required = set(params.get("required") or [])
        rendered: list[str] = []
        for key, spec in props.items():
            kind = (spec or {}).get("type", "any")
            mark = " (required)" if key in required else ""
            rendered.append(f"{key}: {kind}{mark}")
        args = "; ".join(rendered) if rendered else "no arguments"
        lines.append(f"- {schema['name']}({args})\n    {schema.get('description', '').strip()}")
    return "\n".join(lines)


async def _one_shot(job: str, system: str, user: str, *, max_tokens: int) -> str:
    """One non-conversational model call, collected into a string.

    Raises :class:`~nexus.router.NoModelAvailable` when nothing can do the job.
    Deliberately not caught here: a planner with no model must produce a task that
    says so, not a task with no steps that looks like "nothing to do".
    """
    decision = router().resolve(job)
    request = ChatRequest(
        messages=[Message(role="user", content=user)],
        model=decision.model_id,
        system=system,
        temperature=0.1,
        max_output_tokens=max_tokens,
    )
    chunks: list[str] = []
    async for event in router().stream(request, job=job, decision=decision):
        if event.type == "text" and event.text:
            chunks.append(event.text)
    return "".join(chunks)


def _extract_json(text: str) -> dict[str, Any]:
    """Read the planner's JSON, tolerating the wrappers models add.

    A fenced block or a sentence of preamble arrives regularly even under an
    instruction not to add one. The alternative to recovering from it here is a
    task that fails for a formatting reason and reports that as "cannot be done".
    """
    raw = (text or "").strip()
    if not raw:
        raise PlanRejected("the planning model returned nothing")
    if "```" in raw:
        for chunk in raw.split("```"):
            stripped = chunk.strip()
            if stripped.startswith("json"):
                stripped = stripped[4:].strip()
            if stripped.startswith("{"):
                raw = stripped
                break
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise PlanRejected("the planning model did not return JSON")
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise PlanRejected(f"the plan was not valid JSON ({exc.msg})") from exc
    if not isinstance(parsed, dict):
        raise PlanRejected("the plan was not a JSON object")
    return parsed


def _validate_plan(parsed: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    """Turn the model's JSON into storable steps, or refuse it.

    Refusing is the point. A plan that names a tool which does not exist, or calls
    a real tool with arguments it does not take, cannot be executed -- and finding
    that out one step at a time means a task that runs three steps, changes the
    machine, and then dies. Everything checkable is checked here, before the first
    step runs, against the same registry that will run it.
    """
    raw_steps = parsed.get("steps")
    if raw_steps is None or not isinstance(raw_steps, list):
        raise PlanRejected("the plan had no 'steps' list")
    if not raw_steps:
        return []

    steps: list[dict[str, Any]] = []
    for position, raw in enumerate(raw_steps[:limit]):
        if not isinstance(raw, dict):
            raise PlanRejected(f"step {position + 1} was not an object")
        intent = str(raw.get("intent") or "").strip()
        tool_name = raw.get("tool")
        tool_name = str(tool_name).strip() if tool_name else None
        params = raw.get("params")
        params = dict(params) if isinstance(params, dict) else {}

        capability: str | None = None
        if tool_name:
            tool = registry.get(tool_name)
            if tool is None:
                raise PlanRejected(f"step {position + 1} names a tool that does not exist: '{tool_name}'")
            unavailable = tool.unavailable_reason()
            if unavailable:
                raise PlanRejected(
                    f"step {position + 1} needs {tool.title}, which cannot run here: {unavailable}"
                )
            try:
                # Validated now with the same function ``runtime.execute`` uses, so a
                # plan that would be rejected at execution is rejected while nothing
                # has happened yet.
                registry.validate(tool.parameters, params)
            except Exception as exc:
                raise PlanRejected(f"step {position + 1} calls {tool.name} incorrectly: {exc}") from exc
            capability = tool.capability
            if not intent:
                intent = tool.title
        elif not intent:
            raise PlanRejected(f"step {position + 1} has neither a tool nor anything to work out")

        deps: list[int] = []
        for dep in raw.get("depends_on") or []:
            # Only backwards references survive. A step that depends on a later step
            # -- or on itself -- can never become runnable, and the loop would stop
            # early with pending steps and no explanation.
            if isinstance(dep, bool) or not isinstance(dep, int):
                continue
            if 0 <= dep < position:
                deps.append(dep)
        steps.append(
            {"intent": intent, "capability": capability, "tool": tool_name,
             "params": params, "depends_on": deps}
        )
    return steps


def _offset_deps(steps: list[dict[str, Any]], base: int) -> list[dict[str, Any]]:
    """Rewrite plan-local dependency positions as stored ``idx`` values.

    ``add_steps`` numbers new steps from ``MAX(idx) + 1``, so a re-plan's step 0 is
    not row 0. ``next_runnable_step`` compares integer dependencies against stored
    ``idx`` values, which means an un-offset re-plan would either wait on the wrong
    step or -- worse -- consider a dependency already satisfied.
    """
    if not base:
        return steps
    for step in steps:
        step["depends_on"] = [dep + base for dep in step["depends_on"]]
    return steps


def _limit() -> int:
    """How many steps this run may execute. Setting first, ceiling always."""
    try:
        configured = int(settings.get("agent_max_steps", 12) or 12)
    except (TypeError, ValueError):
        configured = 12
    return max(1, min(configured, STEP_CEILING))


def _flag(key: str, default: bool) -> bool:
    value = settings.get(key, default)
    return default if value is None else bool(value)


def _snapshot(task_id: str) -> dict[str, Any]:
    """Task, steps and real counts -- the payload every event carries."""
    task = tasks.get_with_steps(task_id) or {"id": task_id}
    task["progress"] = tasks.progress(task_id)
    return task


async def _ask_for_plan(objective: str, *, limit: int, context: str = "") -> list[dict[str, Any]]:
    """Plan once, then once more if the first plan was rejected for a fixable reason.

    The retry exists because the rejections above are specific enough to act on
    ("names a tool that does not exist: 'open_browser'"), and handing that back
    converts most first-attempt failures into a working plan. It happens exactly
    once: a model that cannot produce a valid plan with the error in front of it
    will not produce one on the fifth try either, and the user is waiting.
    """
    schemas = registry.schemas(only_available=True)
    if not schemas:
        raise PlanRejected(
            "no tools are available on this computer, so there is nothing a task could do"
        )
    system = (
        persona.system_prompt(job="reasoning", with_tools=False)
        + "\n\n---\n\n"
        + _INSTRUCTIONS
        + "\n\nTools available on this computer:\n"
        + _tool_menu(schemas)
        + f"\n\nUse at most {limit} steps."
    )
    prompt = f"OBJECTIVE: {objective.strip()}"
    if context:
        prompt += f"\n\n{context}"

    attempt_note = ""
    last_error = ""
    for attempt in (1, 2):
        text = await _one_shot(
            "reasoning", system, prompt + attempt_note, max_tokens=1_800
        )
        try:
            return _validate_plan(_extract_json(text), limit=limit)
        except PlanRejected as exc:
            last_error = str(exc)
            log.warning("plan attempt %d rejected: %s", attempt, last_error)
            attempt_note = (
                f"\n\nYour previous plan was rejected: {last_error}\n"
                "Return a corrected plan as JSON. Use only the tools listed."
            )
    raise PlanRejected(last_error)


async def plan(
    objective: str,
    *,
    project_id: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Create a task and write its plan. Nothing is executed here.

    Planning and running are separate calls so the user can read a plan before it
    touches the machine -- which is the whole reason plans are stored as data. A
    task whose planning failed is still created and still stored, with the real
    reason in ``error``: the alternative is a button that does nothing and says
    nothing (§78, §79).
    """
    objective = (objective or "").strip()
    if not objective:
        raise ValueError("a task needs an objective")

    task = tasks.create(objective, project_id=project_id, conversation_id=conversation_id)
    task_id = task["id"]
    limit = _limit()
    try:
        steps = await _ask_for_plan(objective, limit=limit)
    except (PlanRejected, NoModelAvailable) as exc:
        tasks.set_status(task_id, "failed", error=f"Could not plan this task: {exc}")
        log.warning("planning failed for task %s: %s", task_id, exc)
        snapshot = _snapshot(task_id)
        bus.publish("task.failed", snapshot)
        return snapshot

    if not steps:
        # The planner read the objective, looked at the tools, and said no. That is a
        # real answer and it is recorded as one rather than as an empty task that
        # would sit in the list looking unstarted forever.
        tasks.set_status(
            task_id,
            "failed",
            error="This cannot be done with the tools available on this computer.",
        )
        snapshot = _snapshot(task_id)
        bus.publish("task.failed", snapshot)
        return snapshot

    tasks.add_steps(task_id, steps)
    snapshot = _snapshot(task_id)
    bus.publish("task.planned", snapshot)
    log.info("planned task %s with %d steps", task_id, len(steps))
    return snapshot


def _prior_context(task_id: str) -> str:
    """What earlier steps actually produced, for a thinking step to reason over.

    Truncated per step rather than in total, so the most recent step is never the
    one that gets cut off.
    """
    lines: list[str] = []
    for step in tasks.list_steps(task_id):
        if step["status"] not in ("completed", "failed"):
            continue
        outcome = step.get("result") or step.get("error") or ""
        outcome = str(outcome).strip().replace("\n", " ")[:600]
        verdict = "" if step["status"] == "completed" else " [FAILED]"
        lines.append(f"{step['idx'] + 1}. {step['intent']}{verdict}: {outcome or '(no output)'}")
    return "\n".join(lines)


async def _think(task: dict[str, Any], step: dict[str, Any]) -> tuple[bool, str]:
    """Run a tool-less step as a real model call over what earlier steps produced.

    This is the one kind of step with no tool behind it, so it is also the one that
    could most easily be faked -- a sentence written by nothing, stored as a result.
    It is a genuine ``reasoning`` request, and if no reasoning model is reachable
    the step fails and says so rather than inventing a conclusion.
    """
    prior = _prior_context(task["id"])
    prompt = (
        f"OBJECTIVE: {task['objective']}\n\n"
        f"WHAT HAS HAPPENED SO FAR:\n{prior or '(nothing yet -- this is the first step)'}\n\n"
        f"THIS STEP: {step['intent']}\n\n"
        "Do only this step. Answer in at most 120 words, plainly, with no preamble. "
        "If the results above do not contain what this step needs, say exactly that "
        "instead of guessing."
    )
    system = persona.system_prompt(job="reasoning", with_tools=False)
    try:
        text = (await _one_shot("reasoning", system, prompt, max_tokens=500)).strip()
    except NoModelAvailable as exc:
        return False, f"No reasoning model is reachable, so this step could not be worked out: {exc}"
    except Exception as exc:  # provider failures are the step's failure, not a crash
        log.warning("thinking step failed on task %s: %s", task["id"], exc, exc_info=True)
        return False, f"The reasoning model failed: {exc}"
    if not text:
        return False, "The reasoning model returned nothing."
    return True, text


async def _watch_permission(task_id: str, step_id: str) -> None:
    """Reflect a real permission wait in the step's status, then stand down.

    ``waiting_permission`` is in the stored state machine and this is the only
    thing that can legitimately set it: the step is not running, it is waiting on a
    human. The status is read back from the engine's own pending list rather than
    assumed, so it can never be left showing a wait that already ended.
    """
    try:
        while True:
            await asyncio.sleep(0.2)
            waiting = any(p.get("task_id") == task_id for p in engine.pending())
            current = tasks.get_step(step_id)
            if current is None or current["status"] not in ("running", "waiting_permission"):
                return
            target = "waiting_permission" if waiting else "running"
            if current["status"] != target:
                tasks.set_step_status(step_id, target)
                bus.publish("task.step.updated", {"task_id": task_id, "step": tasks.get_step(step_id)})
    except asyncio.CancelledError:
        return


async def _run_step(task: dict[str, Any], step: dict[str, Any], *, verify: bool) -> tuple[bool, str]:
    """Execute one step and record what really happened. ``(ok, message)``."""
    task_id, step_id = task["id"], step["id"]
    tasks.set_step_status(step_id, "running")
    bus.publish("task.step.started", {"task_id": task_id, "step": tasks.get_step(step_id),
                                      "progress": tasks.progress(task_id)})

    if not step["tool"]:
        ok, message = await _think(task, step)
        tasks.set_step_status(
            step_id,
            "completed" if ok else "failed",
            result=message if ok else None,
            error=None if ok else message,
        )
        bus.publish("task.step.finished", {"task_id": task_id, "step": tasks.get_step(step_id),
                                           "progress": tasks.progress(task_id)})
        return ok, message

    tool = registry.get(step["tool"])
    # An unknown tool name is not the same as a tool that declares itself safe to
    # repeat. Such a step fails on its first attempt with a tool error, which is not
    # retryable anyway, and False is the reading that cannot cause a repeat of
    # something unrepeatable.
    idempotent = bool(tool.idempotent) if tool else False
    # A tool whose program is not installed cannot succeed on any attempt. That is
    # passed in as a fact rather than left to be guessed from the error text.
    unavailable = tool.unavailable_reason() if tool else None

    attempts = 0
    cleared: recovery.Failure | None = None
    while True:
        attempts += 1
        if attempts > 1:
            # Back to ``running`` -- not a new status. This also clears the previous
            # attempt's error off the row, so a step being retried never displays a
            # failure it is in the middle of retrying.
            tasks.set_step_status(step_id, "running")
            bus.publish(
                "task.step.updated", {"task_id": task_id, "step": tasks.get_step(step_id)}
            )

        watcher = asyncio.create_task(_watch_permission(task_id, step_id))
        result: registry.ToolResult | None = None
        timed_out = False
        try:
            result = await asyncio.wait_for(
                runtime.execute(
                    step["tool"],
                    step["params"],
                    task_id=task_id,
                    step_id=step_id,
                    conversation_id=task.get("conversation_id"),
                ),
                timeout=STEP_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            timed_out = True
        finally:
            watcher.cancel()

        if result is None:
            ok = False
            message = f"{step['tool']} did not finish within {int(STEP_TIMEOUT_S)} seconds."
        else:
            # On failure the *error* is the useful half: ``summary`` is a short label
            # ("could not write it") while ``error`` says why ("the disk is full").
            # The reason has two readers who both need it -- the user, and the planner
            # when it re-plans around the failure -- so a failed step keeps the error
            # and only adds the summary when it says something different.
            if result.ok:
                message = result.summary or "Done."
            elif result.error and result.summary and result.summary != result.error:
                message = f"{result.error} ({result.summary})"
            else:
                message = result.error or result.summary or "the tool did not say why it failed"
            ok = result.ok
            if ok and verify and result.verified is False:
                # The tool reported success and the verifier disagreed. The verifier
                # wins: that disagreement is the entire reason ``agent_verify_steps``
                # exists, and continuing on top of a step that did not actually happen
                # is how an agent produces a confident wrong answer.
                ok = False
                message = (
                    f"{message} -- but this could not be confirmed afterwards: "
                    f"{result.verification or 'the check did not pass'}"
                )

        if ok:
            if cleared is not None:
                # Say that it took more than one go. A step that silently succeeded
                # on the third attempt reads as a step that worked first time, and
                # the difference matters when the same task is run again.
                message = (
                    f"{message} (succeeded on attempt {attempts} of {recovery.MAX_ATTEMPTS}, "
                    f"after a {cleared.kind} failure that cleared)"
                )
            break

        if result is not None and result.ok:
            # ok was turned off by the verifier, not by the tool. A contradiction
            # between the two is not a flaky call, so it goes to the planner rather
            # than round the retry loop.
            break

        failure = recovery.classify(message, timed_out=timed_out, unavailable_reason=unavailable)
        decision = recovery.plan(failure.kind, idempotent=idempotent, attempts=attempts)
        if not decision.retry:
            if failure.kind != "ambiguous":
                # Why it was *not* retried is part of the failure, for the same two
                # readers: the user deciding whether to intervene, and the planner
                # deciding what to try instead. Not for an ambiguous failure, where
                # "the failure does not say what went wrong" adds nothing to a
                # message that already is everything the tool said.
                message = f"{message} -- {decision.why}"
            break

        log.info(
            "task %s step %s: %s failure on attempt %s, retrying in %.1fs",
            task_id, step_id, failure.kind, attempts, decision.delay_s,
        )
        bus.publish(
            "task.step.retry",
            {
                "task_id": task_id,
                "step": tasks.get_step(step_id),
                "attempt": attempts,
                "next_attempt": attempts + 1,
                "max_attempts": recovery.MAX_ATTEMPTS,
                "failure": failure.to_dict(),
                "why": decision.why,
                "delay_s": decision.delay_s,
                "progress": tasks.progress(task_id),
            },
        )
        cleared = failure
        await asyncio.sleep(decision.delay_s)

    # ``verified`` stays tri-state all the way into the row: None means no verifier
    # exists for this tool, which is not the same as unverified and must not be
    # stored as False. A step that timed out has no result at all, which is also
    # None -- nothing was checked, because nothing finished.
    tasks.set_step_status(
        step_id,
        "completed" if ok else "failed",
        result=message if ok else None,
        error=None if ok else (message or "the tool did not say why it failed"),
        verified=result.verified if result is not None else None,
    )
    bus.publish("task.step.finished", {"task_id": task_id, "step": tasks.get_step(step_id),
                                       "progress": tasks.progress(task_id)})
    return ok, message


#: The one in-flight run per task. Two ``POST /run`` calls on the same task would
#: otherwise execute the same steps twice, which for a tool that is not idempotent
#: means doing the thing twice.
_RUNNING: dict[str, asyncio.Task[Any]] = {}


def is_running(task_id: str) -> bool:
    existing = _RUNNING.get(task_id)
    return existing is not None and not existing.done()


async def _replan(task: dict[str, Any], failure: str, *, limit: int) -> int:
    """Re-plan the not-yet-started steps after a failure (§22). Returns how many.

    Only pending steps are replaced: what already happened stays on the record
    exactly as it happened, because a re-plan that rewrote history would make the
    task's own log a lie. Returns 0 when the planner has nothing better to offer,
    which ends the task honestly rather than looping.
    """
    stored = tasks.list_steps(task["id"])
    remaining = [s for s in stored if s["status"] == "pending"]
    if not remaining:
        return 0
    context = (
        f"WHAT HAS HAPPENED SO FAR:\n{_prior_context(task['id'])}\n\n"
        f"THE LAST STEP FAILED: {failure}\n\n"
        "STEPS NOT YET STARTED, which you are replacing:\n"
        + "\n".join(f"- {s['intent']}" for s in remaining)
        + "\n\nPlan the remaining work again, taking the failure into account. Do not "
        "repeat steps that already succeeded. If the objective can no longer be "
        'reached, return {"steps": []}.'
    )
    try:
        steps = await _ask_for_plan(task["objective"], limit=limit, context=context)
    except (PlanRejected, NoModelAvailable) as exc:
        log.warning("re-plan failed for task %s: %s", task["id"], exc)
        return 0
    if not steps:
        return 0
    base = max((s["idx"] for s in stored if s["status"] != "pending"), default=-1) + 1
    tasks.replace_pending_steps(task["id"], _offset_deps(steps, base))
    bus.publish("task.replanned", _snapshot(task["id"]))
    log.info("re-planned task %s into %d steps after a failure", task["id"], len(steps))
    return len(steps)


def _finish(task_id: str, status: str, *, result: str | None = None, error: str | None = None) -> dict[str, Any]:
    """Write the final status, and stop pretending unrun steps are still coming.

    A task that stopped at a failed step leaves later steps untouched. Left at
    ``pending`` they read as "about to run", which is exactly wrong for a task that
    has finished -- so they are marked ``skipped``, the status the schema already
    has for this, with the reason on each one. The UI then shows 1 failed and 2
    skipped, which is what happened, instead of a finished task with work still
    apparently queued (§80).
    """
    if status == "failed":
        reason = "Not run: the task stopped before reaching this step."
        for step in tasks.list_steps(task_id):
            if step["status"] == "pending":
                tasks.set_step_status(step["id"], "skipped", error=reason)
    tasks.set_status(task_id, status, result=result, error=error)
    snapshot = _snapshot(task_id)
    bus.publish(f"task.{status}", snapshot)
    return snapshot


async def run(task_id: str) -> dict[str, Any]:
    """Execute a planned task to the end, or to the first thing that stops it.

    The loop is deliberately dull: take the next runnable step, run it, record the
    truth, repeat. Everything interesting is a stopping condition, and each one
    leaves a different, specific reason on the task -- failed step, step budget
    exhausted, cancelled, or a plan whose remaining steps can never become runnable.
    "Finished" is never the default.
    """
    task = tasks.get(task_id)
    if task is None:
        raise KeyError(task_id)
    stored = tasks.list_steps(task_id)
    if not stored:
        return _finish(task_id, "failed", error="This task has no steps, so there is nothing to run.")
    if task["status"] == "running":
        return _snapshot(task_id)

    limit = _limit()
    verify = _flag("agent_verify_steps", True)
    stop_on_failure = _flag("agent_pause_on_failure", True)
    tasks.set_status(task_id, "running")
    bus.publish("task.started", _snapshot(task_id))
    log.info("running task %s (limit %d steps, verify=%s, stop_on_failure=%s)",
             task_id, limit, verify, stop_on_failure)

    executed = 0
    replans = 0
    last_message = ""
    try:
        while True:
            current = tasks.get(task_id)
            if current is None:
                raise KeyError(task_id)
            if current["status"] == "cancelled":
                return _cancel_remaining(task_id)
            if executed >= limit:
                return _finish(
                    task_id,
                    "failed",
                    error=(
                        f"Stopped after {executed} steps, the limit set by agent_max_steps. "
                        "The remaining steps were not run."
                    ),
                )
            step = tasks.next_runnable_step(task_id)
            if step is None:
                break

            ok, last_message = await _run_step(task, step, verify=verify)
            executed += 1
            if ok:
                continue

            failure = f"Step {step['idx'] + 1} ({step['intent']}) failed: {last_message}"
            if stop_on_failure:
                # agent_pause_on_failure, expressed in a state machine that has no
                # 'paused': stop where we are and keep the record. Nothing is
                # half-run and nothing is retried behind the user's back.
                return _finish(task_id, "failed", error=failure)
            if replans:
                return _finish(
                    task_id, "failed",
                    error=f"{failure} The re-planned steps failed as well, so the task stopped.",
                )
            replans += 1
            if not await _replan(task, failure, limit=max(1, limit - executed)):
                return _finish(
                    task_id, "failed",
                    error=f"{failure} There was no way to continue after it.",
                )
    except asyncio.CancelledError:
        _cancel_remaining(task_id)
        raise
    except Exception as exc:  # a bug here must not leave a task 'running' forever
        log.error("task %s crashed", task_id, exc_info=True)
        return _finish(task_id, "failed", error=f"The task stopped unexpectedly: {exc}")

    final = tasks.list_steps(task_id)
    failed = [s for s in final if s["status"] == "failed"]
    pending = [s for s in final if s["status"] in ("pending", "running", "waiting_permission")]
    if failed:
        first = failed[0]
        return _finish(task_id, "failed",
                       error=f"Step {first['idx'] + 1} failed: {first.get('error') or 'no reason recorded'}")
    if pending:
        # No step is runnable and none failed: every remaining step is waiting on
        # something that will never complete. Rare, and reported rather than
        # rounded up to success.
        return _finish(
            task_id, "failed",
            error=(f"{len(pending)} step(s) could not be started because their "
                   "dependencies never completed."),
        )
    done = [s for s in final if s["status"] == "completed"]
    tail = ""
    for s in reversed(done):
        if s.get("result"):
            tail = str(s["result"]).strip()
            break
    result = f"{len(done)} step(s) completed." + (f" {tail}" if tail else "")
    return _finish(task_id, "completed", result=result[:4_000])


def _cancel_remaining(task_id: str) -> dict[str, Any]:
    """Mark what never ran as cancelled, so no step is left claiming to be running."""
    for step in tasks.list_steps(task_id):
        if step["status"] in ("pending", "running", "waiting_permission"):
            tasks.set_step_status(step["id"], "cancelled", error="The task was cancelled.")
    if (tasks.get(task_id) or {}).get("status") != "cancelled":
        tasks.set_status(task_id, "cancelled", error="The task was cancelled.")
    snapshot = _snapshot(task_id)
    bus.publish("task.cancelled", snapshot)
    return snapshot


#: Statuses a run may be started from. ``interrupted`` is included on purpose: it
#: means the process died mid-task, the completed steps are still completed, and
#: continuing picks up at the next pending one. Starting from ``failed`` or
#: ``completed`` is refused instead of silently doing nothing -- the failed steps are
#: not pending, so a "re-run" would run nothing and then report a fresh failure.
STARTABLE = ("pending", "interrupted")


def start(task_id: str) -> dict[str, Any]:
    """Begin (or resume) a run in the background and return the task immediately.

    Must be called from the event loop thread: the run is scheduled on the running
    loop, so a caller in a threadpool gets "no running event loop" rather than a
    started task. Every HTTP route that reaches this is ``async def`` for that
    reason.

    The caller gets the task as it is right now; progress arrives as ``task.*``
    events on the bus, carrying real step counts. Refusing a second start on a
    running task matters more than it looks: steps call tools, and running
    ``file.write`` or ``terminal.execute`` twice is not a cosmetic bug.
    """
    task = tasks.get(task_id)
    if task is None:
        raise KeyError(task_id)
    if is_running(task_id):
        return _snapshot(task_id)
    if task["status"] not in STARTABLE:
        raise ValueError(
            f"This task is {task['status']}, so it cannot be started. "
            "Plan a new task to do the work again."
        )
    if not tasks.list_steps(task_id):
        raise ValueError("This task has no steps, so there is nothing to run.")

    runner = asyncio.create_task(run(task_id), name=f"nexus-task-{task_id}")
    _RUNNING[task_id] = runner

    def done(finished: asyncio.Task[Any]) -> None:
        if _RUNNING.get(task_id) is finished:
            _RUNNING.pop(task_id, None)
        if finished.cancelled():
            return
        exc = finished.exception()
        if exc is not None:
            log.error("task %s ended with an unhandled error", task_id, exc_info=exc)

    runner.add_done_callback(done)
    return _snapshot(task_id)


#: Statuses a *retry* may be started from. Terminal, and terminal with work left
#: undone -- which is what separates them from ``completed``. Retrying is not the
#: same operation as starting: ``start`` runs the pending steps of a task that still
#: has some, while ``retry`` first puts the steps that did not succeed back to
#: pending. Offering Run on a failed task instead would run nothing and then report
#: a fresh failure, which is why ``STARTABLE`` excludes these.
RETRYABLE = ("failed", "cancelled")


def retry_preview(task_id: str) -> dict[str, Any]:
    """What a retry would actually do, without doing it.

    Exists because a retry is not free: it re-runs real tools against this machine,
    and some of them are not idempotent. The user pressing the button is the human
    the step-level policy defers to (``recovery.plan`` refuses to repeat a
    non-idempotent tool *automatically*), so this reports which steps would run
    again and names the ones where running twice is not the same as running once.
    Confirming a thing you were shown is consent; confirming "Retry?" is not.
    """
    task = tasks.get(task_id)
    if task is None:
        raise KeyError(task_id)
    steps = tasks.list_steps(task_id)
    again = [s for s in steps if s["status"] in tasks.RETRYABLE_STEP_STATUSES]
    kept = [s for s in steps if s["status"] == "completed"]
    repeatable: list[dict[str, Any]] = []
    for step in again:
        tool = registry.get(step["tool"]) if step["tool"] else None
        repeatable.append(
            {
                "id": step["id"],
                "idx": step["idx"],
                "intent": step["intent"],
                "tool": step["tool"],
                "status": step["status"],
                # None for a thinking step: it calls no tool, so idempotence is not a
                # fact about it either way, and False would read as a warning.
                "idempotent": None if not step["tool"] else bool(tool.idempotent) if tool else False,
            }
        )
    if is_running(task_id):
        reason = "A run is already in flight on this task."
    elif task["status"] not in RETRYABLE:
        reason = f"This task is {task['status']}, which is not a state a retry applies to."
    elif not again:
        reason = "No step on this task is in a state a retry would re-run."
    else:
        reason = ""
    return {
        "can_retry": not reason,
        "reason": reason,
        "steps": repeatable,
        "keeping": len(kept),
        # Named separately because it is the fact that changes the decision, and a
        # count buried in a list is not a warning.
        "not_idempotent": [s["tool"] for s in repeatable if s["idempotent"] is False],
    }


def retry(task_id: str) -> dict[str, Any]:
    """Re-run the steps of a finished task that did not succeed (§40, §64).

    Deliberately not the same call as ``start``. A retry *changes stored state*
    before running -- the steps that failed, were skipped, or were cancelled go back
    to ``pending`` -- and that is a decision, not a resumption. Completed steps stay
    completed, so this is "retry from the last good step" and not "do all of it
    again".

    No new status anywhere: the task returns to ``pending`` and is then started by
    the same code path as any other run, so nothing downstream has to learn what a
    retried task is.
    """
    preview = retry_preview(task_id)
    if not preview["can_retry"]:
        raise ValueError(preview["reason"])
    reset = tasks.reset_steps_for_retry(task_id)
    # ``pending`` and not ``running``: ``start`` writes ``running`` itself, and
    # setting it here would leave a task marked running that nothing had started if
    # the call below refused.
    tasks.set_status(task_id, "pending", error=None)
    log.info("retrying task %s: %d step(s) queued again", task_id, len(reset))
    bus.publish(
        "task.retried",
        {**_snapshot(task_id), "retried_steps": len(reset), "kept_steps": preview["keeping"]},
    )
    return start(task_id)


def cancel(task_id: str) -> dict[str, Any]:
    """Stop a task now. Safe to call whether or not it is running.

    Order matters. The status is written first so the loop sees it at its next
    check, then any permission request this task is waiting on is withdrawn --
    otherwise cancelling a task that is sitting on a permission prompt would leave
    the prompt on screen with nothing behind it.
    """
    task = tasks.get(task_id)
    if task is None:
        raise KeyError(task_id)
    if task["status"] in ("completed", "failed", "cancelled"):
        return _snapshot(task_id)

    tasks.set_status(task_id, "cancelled", error="The task was cancelled.")
    engine.cancel_all_for_task(task_id)
    runner = _RUNNING.pop(task_id, None)
    if runner is not None and not runner.done():
        runner.cancel()
    return _cancel_remaining(task_id)


async def plan_and_run(
    objective: str,
    *,
    project_id: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Plan a task and start it. Used when the user has already said "do it"."""
    task = await plan(objective, project_id=project_id, conversation_id=conversation_id)
    if task.get("status") == "failed":
        return task
    return start(task["id"])
