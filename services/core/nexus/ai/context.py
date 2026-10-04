"""What NEXUS knows that is relevant to this turn, under a budget (task F4; §19).

A model's context window is a fixed resource that several things want at once:
the conversation so far, remembered facts, the active project, what the ambient
monitors noticed, tasks still in flight, and the request itself. Handing over
everything is not generous -- past a point it makes answers worse, because the
sentence that mattered is buried among forty that did not.

So this module does two things: it decides **what is relevant**, and it enforces
**a budget**.

Relevance is not recency. The last twelve messages are the cheapest thing to
include and often the least useful -- a memory recorded three months ago
("deployments go through staging first") can decide an answer that the last six
messages cannot. So memories are retrieved semantically against *this* request
rather than listed by date, and conversation history is included as a tail
because that is genuinely what it is for: keeping pronouns and follow-ups
resolvable.

The budget is enforced in a fixed priority order, and the order encodes what is
load-bearing. Two consequences worth stating plainly:

* **The conversation tail is trimmed before memories are.** A dropped memory
  makes an answer less informed; a dropped conversation tail makes "do that again"
  meaningless. But the tail is trimmed from the *oldest* end, so what survives is
  always contiguous with the request.
* **What was dropped is recorded, not hidden.** :attr:`Context.dropped` lists it,
  and the routing panel shows it. A context that silently truncated would produce
  an answer that ignores something the user knows they said, with no explanation
  available for why.

Everything here reads from stores that already hold what they hold. No monitor is
started, no screen is captured, no tool runs: assembling context must not itself
require permission, or every turn would open a dialog. What the ambient monitors
already recorded *under* permission is fair to read; anything not yet observed is
absent, and its absence is stated rather than guessed at.
"""

from __future__ import annotations

import platform
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..db.repos import ambient, app_settings, conversations, projects, tasks
from ..logging_setup import get_logger
from . import embeddings

log = get_logger("ai.context")

#: Characters per token, used to size text without a tokeniser. Deliberately
#: pessimistic: real English runs about 4.0, and code and paths run lower, so 3.6
#: over-counts slightly. Over-counting costs a little unused window; under-
#: counting costs a mid-generation truncation, which the user experiences as the
#: answer stopping in the middle of a sentence.
CHARS_PER_TOKEN = 3.6

#: Share of the model's window that context may occupy. The rest is the system
#: prompt, the tool schemas, and -- the part that is easy to forget -- room for
#: the answer itself. A context that fills the window leaves nothing to reply with.
CONTEXT_SHARE = 0.45

#: Floor for that share, so a small-window model still gets usable context rather
#: than three memories and nothing else.
MIN_CONTEXT_TOKENS = 1_500


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


@dataclass(slots=True)
class Block:
    """One labelled section of context, with what it cost to include."""

    kind: str
    heading: str
    body: str
    tokens: int = 0
    #: Lower sheds first. Not a display order -- see :data:`_PRIORITY`.
    priority: int = 50

    def __post_init__(self) -> None:
        if not self.tokens:
            self.tokens = estimate_tokens(self.heading) + estimate_tokens(self.body)

    def render(self) -> str:
        return f"# {self.heading}\n\n{self.body}"


#: What gets shed first when the budget binds, lowest first.
#:
#: ``memories`` outranks ``project`` and ``ambient`` because a remembered
#: preference changes the *answer*, while a project note mostly changes the
#: framing. ``ambient`` is lowest of the informational blocks because it is
#: observational trivia unless the request is about it -- and when it is about
#: it, the request is an inspection and a tool will fetch it live and current,
#: which is better than a stale observation anyway. ``world`` sits just above
#: ``ambient``: it is the same observational material, but held current rather
#: than as history, so a model that reads "battery 41%, discharging (3 minutes
#: ago)" does not reach for the ``system.status`` tool to learn what it already
#: knows.
_PRIORITY: dict[str, int] = {
    "environment": 95,
    "task": 90,
    "conversation": 80,
    "memories": 70,
    "project": 50,
    "world": 40,
    "ambient": 30,
}


@dataclass(slots=True)
class Context:
    """Assembled context, plus an honest account of what did not fit."""

    blocks: list[Block] = field(default_factory=list)
    #: Conversation turns, kept separate because they belong in the messages
    #: array as real turns, not flattened into the system prompt. A model shown
    #: its own past replies as system text stops treating them as things it said.
    turns: list[dict[str, Any]] = field(default_factory=list)
    memories: list[dict[str, Any]] = field(default_factory=list)
    #: Human-readable notes about what was left out or degraded, e.g. "semantic
    #: search unavailable" or "4 older messages trimmed to fit".
    dropped: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    tokens: int = 0
    budget: int = 0

    def system_block(self) -> str:
        """The context that belongs in the system prompt, in reading order."""
        order = ["environment", "project", "task", "memories", "world", "ambient"]
        parts = [
            b.render()
            for kind in order
            for b in self.blocks
            if b.kind == kind
        ]
        return "\n\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocks": [
                {"kind": b.kind, "heading": b.heading, "tokens": b.tokens}
                for b in self.blocks
            ],
            "turns": len(self.turns),
            "memories": len(self.memories),
            "dropped": self.dropped,
            "notices": self.notices,
            "tokens": self.tokens,
            "budget": self.budget,
        }


# -------------------------------------------------------------- environment


def environment_summary() -> str:
    """Facts about this computer that are true without asking anything.

    Only what is knowable from the process itself: kernel, desktop session, disk
    headroom, the date. Notably *not* here: what is running, what is on screen,
    what the user is doing. Those need tools and permission, and putting a guess
    at them in the system prompt is how a model ends up stating a process name it
    never looked up.
    """
    now = datetime.now()
    lines = [
        f"Date and time: {now.strftime('%A %d %B %Y, %H:%M')} "
        f"({now.astimezone().tzname() or 'local time'}).",
        f"Operating system: {platform.system()} {platform.release()}.",
    ]
    try:
        usage = shutil.disk_usage(str(_home()))
        lines.append(
            f"Home disk: {usage.free / 1_000_000_000:.0f} GB free of "
            f"{usage.total / 1_000_000_000:.0f} GB."
        )
    except OSError:
        pass
    lines.append(
        "Anything about running applications, windows, the screen or files must "
        "come from a tool call in this turn, not from this summary."
    )
    return "\n".join(lines)


def _home() -> Any:
    from pathlib import Path

    return Path.home()


# ----------------------------------------------------------------- assembly


async def assemble(
    request: str,
    *,
    conversation_id: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    max_context_tokens: int,
    turn_limit: int = 12,
    include_memories: bool = True,
    include_ambient: bool = True,
) -> Context:
    """Gather everything relevant to ``request`` and fit it in the budget.

    ``max_context_tokens`` is the whole window, not the context share -- the share
    is applied here so callers pass the one number they actually know from the
    model spec.
    """
    budget = max(MIN_CONTEXT_TOKENS, int(max_context_tokens * CONTEXT_SHARE))
    ctx = Context(budget=budget)

    ctx.blocks.append(
        Block("environment", "This computer", environment_summary(),
              priority=_PRIORITY["environment"])
    )

    if conversation_id:
        ctx.turns = conversations.recent_turns(conversation_id, limit=turn_limit)

    if project_id:
        block = _project_block(project_id)
        if block:
            ctx.blocks.append(block)

    if task_id:
        block = _task_block(task_id)
        if block:
            ctx.blocks.append(block)

    if include_memories:
        await _add_memories(ctx, request, project_id=project_id)

    if include_ambient and bool(app_settings.get("monitor_enabled", False)):
        block = _ambient_block()
        if block:
            ctx.blocks.append(block)

    # The world model block (C2). Deliberately not behind the ``monitor_enabled``
    # master switch: the world model only ever holds what a monitor already
    # published, so including it cannot observe anything new -- it is a read of
    # existing state, the same way the conversation tail is. An empty block that
    # says "nothing observed" is the honest answer when every watcher is off.
    block = _world_block()
    if block:
        ctx.blocks.append(block)

    _fit(ctx)
    return ctx


async def _add_memories(ctx: Context, request: str, *, project_id: str | None) -> None:
    """Retrieve memories by relevance and render them with their provenance.

    A failure here is degradation, not an error: the turn proceeds with no
    remembered facts and the notice explains why. Raising would mean an
    unreachable embedding endpoint stops NEXUS from answering "what time is it".
    """
    limit = int(app_settings.get("memory_recall_limit", 6) or 6)
    if limit <= 0:
        return
    try:
        found = await embeddings.search(request, limit=limit, project_id=project_id)
    except Exception as exc:  # noqa: BLE001 -- recall must never fail a turn
        log.info("memory recall failed: %s", exc)
        ctx.notices.append(
            "Memory recall was unavailable for this turn, so remembered facts "
            f"were not consulted. {exc}"
        )
        return

    if found.get("notice"):
        ctx.notices.append(str(found["notice"]))
    items = found.get("items") or []
    if not items:
        return

    ctx.memories = items
    lines = [
        "Facts the user has approved for you to remember. Treat them as true "
        "unless this conversation contradicts them, and do not repeat them back "
        "unprompted.",
        "",
    ]
    for item in items:
        title = str(item.get("title") or "").strip()
        body = str(item.get("body") or "").strip()
        lines.append(f"- {title}: {body}" if title else f"- {body}")
    ctx.blocks.append(
        Block("memories", "What you remember", "\n".join(lines),
              priority=_PRIORITY["memories"])
    )


def _project_block(project_id: str) -> Block | None:
    project = projects.get(project_id)
    if project is None:
        return None
    title = str(project.get("title") or "Untitled project")
    description = str(project.get("description") or "").strip()
    lines = [f"The user is working inside the project \"{title}\"."]
    if description:
        lines.append(description)
    lines.append(
        "Keep this project's context separate from others: do not bring in "
        "memories or files from a different project unless asked."
    )
    return Block("project", "Active project", "\n".join(lines),
                 priority=_PRIORITY["project"])


def _task_block(task_id: str) -> Block | None:
    """The live state of a task in progress, from the record rather than from memory.

    Step status is read fresh every turn. A model told "step 3 is running" when
    step 3 failed two minutes ago will report progress that did not happen, which
    is the fake-agent-progress prohibition (§80).
    """
    task = tasks.get_with_steps(task_id)
    if task is None:
        return None
    counts = tasks.progress(task_id)
    lines = [
        f"Objective: {task.get('objective')}",
        f"Status: {task.get('status')} "
        f"({counts.get('completed', 0)} of {counts.get('total', 0)} steps done).",
        "",
    ]
    for step in task.get("steps") or []:
        status = str(step.get("status") or "pending")
        mark = {
            "completed": "done",
            "failed": "FAILED",
            "running": "running now",
            "blocked": "blocked",
            "skipped": "skipped",
        }.get(status, "not started")
        line = f"- {step.get('description')} -- {mark}"
        if status == "failed" and step.get("error"):
            line += f" ({step['error']})"
        lines.append(line)
    lines += [
        "",
        "Continue from the first step that is not done. Do not re-run completed "
        "steps and do not describe a step as finished unless it says so here.",
    ]
    return Block("task", "Task in progress", "\n".join(lines),
                 priority=_PRIORITY["task"])


def _ambient_block(*, limit: int = 6) -> Block | None:
    """What the monitors noticed recently, if the user turned them on.

    Only ``warning`` and ``critical`` observations. Routine ones are noise in a
    prompt -- the user did not ask about window focus changes, and including a
    hundred of them would crowd out their actual question.
    """
    try:
        rows = [
            row
            for severity in ("critical", "warning")
            for row in ambient.events(severity=severity, limit=limit)
        ]
    except Exception as exc:  # noqa: BLE001
        log.debug("ambient context skipped: %s", exc)
        return None
    if not rows:
        return None
    rows.sort(key=lambda r: str(r.get("observed_at") or ""), reverse=True)
    lines = [
        "Recent observations from the monitors the user enabled. These are "
        "already-recorded history, not live readings -- check with a tool before "
        "stating any of it as current.",
        "",
    ]
    for row in rows[:limit]:
        stamp = str(row.get("observed_at") or "")[:16].replace("T", " ")
        lines.append(
            f"- {stamp} [{row.get('severity')}] {row.get('summary') or row.get('kind')}"
        )
    return Block("ambient", "What NEXUS noticed", "\n".join(lines),
                 priority=_PRIORITY["ambient"])


# ------------------------------------------------------------------- world (C2)

#: The agent-facing name for each world domain. A domain with no entry here is
#: shown under its internal name rather than hidden -- hiding it would imply
#: the monitors said nothing when really this table forgot to name it.
_WORLD_HEADINGS: dict[str, str] = {
    "battery": "Battery",
    "network": "Network",
    "disk": "Disk space",
    "cpu": "CPU",
    "memory": "Memory",
    "usb": "USB devices",
    "bluetooth": "Bluetooth",
    "window": "Focused window",
    "clipboard": "Clipboard",
    "screen": "Screen",
    "process": "Processes",
}


def _world_block() -> Block | None:
    """The fused current state the monitors have built, redacted.

    This is the block that stops the model re-reading ``/proc`` through a tool
    for facts it already holds. Three honesty rules carry over from the world
    model itself, and none of them may be softened at render time:

    * **A domain with no observation is omitted**, not filled in -- an omitted
      "Battery" line means no monitor reported a battery, and the model should
      treat that as unknown, not as fine.
    * **Stale facts are shown with their caveat**, so "battery 41%" from forty
      minutes ago reads as old information, not current.
    * **Everything the model reads went through redaction** -- window titles
      and device names are user data, and the summary the prompt carries is
      deliberately blinder than what the process holds (C2's privacy rule).
    """
    from ..core import world

    try:
        state = world.redacted_state()
    except Exception as exc:  # noqa: BLE001 -- context must never fail a turn
        log.debug("world context skipped: %s", exc)
        return None

    domains = state.get("domains") or {}
    lines: list[str] = []
    for domain, heading in _WORLD_HEADINGS.items():
        fact = domains.get(domain)
        if not fact or fact.get("data") is None:
            continue
        # Staleness is shown as a caveat on the line, never as a reason to
        # drop it: the model should know the fact is old, not have to guess.
        caveat = " (stale reading)" if fact.get("stale") else ""
        lines.append(f"- {heading}{caveat}: {fact.get('summary') or domain}")

    if not lines:
        # No monitor has spoken. That is itself a fact, and stating it plainly
        # is what stops the model from assuming anything about an omitted domain.
        lines.append("- No monitor has reported any state yet.")

    trailer = (
        "This is what the ambient monitors observed. Anything not listed is "
        "unknown, not known-to-be-fine -- check with a tool before stating it."
    )
    if state.get("degraded"):
        # A broken world model must not read as a complete one: the model is
        # told the snapshot may be missing observations before it trusts them.
        trailer += (
            " The world model's subscriber failed, so this may be incomplete "
            f"({state.get('error') or 'unknown error'})."
        )
    return Block("world", "Current machine state (observed)", "\n".join([*lines, "", trailer]),
                 priority=_PRIORITY["world"])


# -------------------------------------------------------------------- budget


def _turn_tokens(turns: list[dict[str, Any]]) -> int:
    return sum(estimate_tokens(str(t.get("content") or "")) for t in turns)


def _fit(ctx: Context) -> None:
    """Trim to the budget in priority order, recording every trim.

    Two-stage on purpose. Whole blocks are shed first, because a half-rendered
    memory list is worse than an absent one -- the model cannot tell that the
    third memory was cut and reasons as though it saw everything. The conversation
    tail is different: it is a sequence, so dropping its oldest end leaves
    something still coherent, and that happens second.
    """
    total = sum(b.tokens for b in ctx.blocks) + _turn_tokens(ctx.turns)
    if total <= ctx.budget:
        ctx.tokens = total
        return

    # Stage one: shed the least important blocks entirely.
    for block in sorted(ctx.blocks, key=lambda b: b.priority):
        if total <= ctx.budget:
            break
        if block.priority >= _PRIORITY["conversation"]:
            break  # environment and task state are not negotiable
        ctx.blocks.remove(block)
        total -= block.tokens
        if block.kind == "memories":
            ctx.memories = []
        ctx.dropped.append(
            f"{block.heading} was left out: the context budget of "
            f"{ctx.budget:,} tokens would have been exceeded."
        )

    # Stage two: trim the conversation from the oldest end, keeping the tail
    # contiguous with the request.
    trimmed = 0
    while total > ctx.budget and len(ctx.turns) > 2:
        removed = ctx.turns.pop(0)
        total -= estimate_tokens(str(removed.get("content") or ""))
        trimmed += 1
    if trimmed:
        ctx.dropped.append(
            f"{trimmed} older message{'s' if trimmed != 1 else ''} trimmed from "
            "the start of the conversation to fit the context budget."
        )

    ctx.tokens = total


__all__ = [
    "CHARS_PER_TOKEN",
    "CONTEXT_SHARE",
    "Block",
    "Context",
    "assemble",
    "environment_summary",
    "estimate_tokens",
]
