"""Learning how a recurring job is done, from tasks that succeeded (O8).

The ultra-intelligent audit's third gap and the memory audit's §18 item, in the same
words: there is no learned-workflow memory type, so outcome data accumulates and
nothing *improves* from it. NEXUS could recall that you prefer Neovim; it could not
recall that the way you deploy is build → test → push, which is the thing a
Jarvis-shaped assistant is actually for.

This module detects the pattern and proposes it. It deliberately does **not** store
it. Three constraints, and they are the whole design.

**It proposes; a person approves.** A learned procedure is a claim about how the
user works, and a machine that quietly starts treating its own inference as the
standard is the §32 violation wearing a useful costume. So the detector produces a
*proposal* through the same door every other memory uses
(:mod:`nexus.ai.memory_extract`), and nothing is remembered until the user says so.

**It requires the pattern twice.** One successful run is a run, not a habit. Two
runs that agree on their tool sequence is the smallest evidence that a sequence is
the *way* rather than the way *this time*, and requiring it is what keeps the
proposer from filling the memory store with things nobody does twice.

**It proposes the sequence, never the parameters.** The steps are named by tool and
intent -- "read the file, then write it" -- and the arguments are deliberately left
out. Arguments are where passwords, paths and one-off values live, and a "procedure"
that cargo-culted last Tuesday's file path would be worse than no procedure at all:
it would look like knowledge and act like a bug.

The detection is pure: it takes task rows and returns proposals. Nothing here reads
the clock, a database or a model, which is what makes the sequence-matching rules
testable rather than merely plausible.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

#: How many successful runs of the same shape before it counts as a procedure.
#: Two is the smallest number that distinguishes "the way I do this" from "what
#: happened once", and raising it would mean a rarely-run but genuine procedure is
#: never learned.
MIN_OCCURRENCES = 2

#: Steps a procedure must have to be worth naming. A one-step "procedure" is just a
#: tool call the user made, and remembering it as a workflow teaches nothing.
MIN_STEPS = 2

#: Above this the "sequence" is too long to be a habit and too long to read. A
#: hundred-step run is a one-off script, not something the user does by hand.
MAX_STEPS = 12

#: Fraction of the sequence that must match, in order, for two runs to be the same
#: procedure. Exact equality is too strict -- a run that skipped an optional check
#: is still the same job -- and anything much lower starts grouping unrelated work.
SIMILARITY = 0.8


@dataclass(frozen=True)
class Shape:
    """One task's successful tool sequence, without its arguments."""

    task_id: str
    goal: str
    steps: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "goal": self.goal, "steps": list(self.steps)}


@dataclass
class Proposal:
    """A recurring procedure, offered rather than stored."""

    title: str
    body: str
    occurrences: int
    steps: tuple[str, ...]
    task_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "body": self.body,
            "category": "procedural",
            "occurrences": self.occurrences,
            "steps": list(self.steps),
            "task_ids": self.task_ids,
        }


def shape_of(task: Mapping[str, Any], steps: Sequence[Mapping[str, Any]]) -> Shape | None:
    """A task's tool sequence, or None when the task is not a finished success.

    Only completed tasks count. A failed run's sequence is *how it went wrong*, and
    proposing it as the way to do something would be teaching from the wrong
    example -- the same mistake as training on outcomes that were never checked.

    Steps that never ran (skipped, cancelled) are dropped rather than included: a
    procedure is what the task actually did, not what it intended.
    """
    if str(task.get("status") or "") != "completed":
        return None

    named: list[str] = []
    for step in steps:
        if str(step.get("status") or "") != "completed":
            continue
        tool = str(step.get("tool") or "").strip()
        if tool:
            named.append(tool)
        else:
            # A step with no tool is a judgement or a message, not an action. It
            # is named so the shape is still recognisable, but marked as such.
            capability = str(step.get("capability") or "").strip()
            named.append(f"({capability or 'reasoning'})")

    if len(named) < MIN_STEPS or len(named) > MAX_STEPS:
        return None
    return Shape(
        task_id=str(task.get("id") or ""),
        goal=str(task.get("title") or task.get("goal") or "").strip(),
        steps=tuple(named),
    )


def _similar(a: Sequence[str], b: Sequence[str]) -> bool:
    """Whether two sequences are the same procedure, in order.

    Compared as prefix-overlap rather than by set, because order is the thing that
    makes a procedure a procedure: [read, write, test] and [write, read, test] are
    different jobs even though they use the same tools.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    # The shorter run must appear as a contiguous subsequence of the longer,
    # allowing the longer to have skipped or added optional steps.
    width = len(shorter)
    for start in range(len(longer) - width + 1):
        window = longer[start : start + width]
        agreeing = sum(1 for x, y in zip(window, shorter) if x == y)
        if agreeing / width >= SIMILARITY:
            return True
    return False


def detect(shapes: Iterable[Shape]) -> list[Proposal]:
    """Group successful runs into recurring procedures. Pure, so it is testable."""
    groups: list[list[Shape]] = []
    for shape in shapes:
        for group in groups:
            if _similar(group[0].steps, shape.steps):
                group.append(shape)
                break
        else:
            groups.append([shape])

    proposals: list[Proposal] = []
    for group in groups:
        if len(group) < MIN_OCCURRENCES:
            continue

        # The most *complete* sequence in the group is the one to record: a run that
        # skipped an optional step still counts as an occurrence, but should not be
        # the version written down.
        exemplar = max(group, key=lambda s: (len(s.steps), s.task_id))
        goal = _common_goal(group)
        proposals.append(
            Proposal(
                title=f"How you {goal}" if goal else "A recurring sequence of steps",
                body=_describe(goal, exemplar.steps, len(group)),
                occurrences=len(group),
                steps=exemplar.steps,
                task_ids=[s.task_id for s in group],
            )
        )

    # Most-practised first: the thing done most often is the most useful to know.
    proposals.sort(key=lambda p: (-p.occurrences, p.title))
    return proposals


def _common_goal(group: Sequence[Shape]) -> str:
    """The goal the group agrees on, or "" when they do not.

    Compared case-insensitively because the model titles the same job slightly
    differently each time, and a procedure called "How you Deploy the app" next to
    one called "How you deploy the app" is two memories of one habit.
    """
    lowered = {shape.goal.strip().lower() for shape in group if shape.goal.strip()}
    if len(lowered) == 1:
        for shape in group:
            if shape.goal.strip():
                return shape.goal.strip()
    return ""


def _describe(goal: str, steps: Sequence[str], occurrences: int) -> str:
    """The sentence stored as the memory body. States its own evidence."""
    numbered = " then ".join(_humanise(step) for step in steps)
    where = f" for '{goal}'" if goal else ""
    return (
        f"Done {occurrences} times{where}: {numbered}. "
        f"Learned from {occurrences} completed tasks that used this sequence; "
        "the arguments are deliberately not recorded, so this is the shape of the "
        "job rather than a replay of one run."
    )


def _humanise(step: str) -> str:
    """``files.read`` reads better as "read a file" in a sentence."""
    if step.startswith("("):
        return step.strip("()")
    if "." not in step:
        return step
    prefix, _, action = step.partition(".")
    noun = {
        "files": "a file",
        "apps": "an application",
        "window": "a window",
        "system": "the system",
        "input": "input",
        "screen": "the screen",
        "clipboard": "the clipboard",
        "browser": "the browser",
        "memory": "memory",
        "terminal": "a command",
        "net": "the network",
        "automation": "an automation",
        "media": "media",
        "process": "a process",
    }.get(prefix, f"{prefix}")
    return f"{action.replace('_', ' ')} {noun}"


def proposals_from(
    tasks: Iterable[tuple[Mapping[str, Any], Sequence[Mapping[str, Any]]]],
) -> list[Proposal]:
    """The whole detection over (task, steps) pairs. The entry point callers use."""
    shapes: list[Shape] = []
    for task, steps in tasks:
        shape = shape_of(task, steps)
        if shape is not None:
            shapes.append(shape)
    return detect(shapes)


def summarise(proposals: Sequence[Proposal]) -> str:
    """One line for the Activity log, or "" when there is nothing to say."""
    if not proposals:
        return ""
    best = proposals[0]
    tail = f" (+{len(proposals) - 1} more)" if len(proposals) > 1 else ""
    return (
        f"Noticed a repeated way of working: {best.title} "
        f"({best.occurrences} times){tail}"
    )


__all__ = [
    "MIN_OCCURRENCES",
    "MIN_STEPS",
    "MAX_STEPS",
    "SIMILARITY",
    "Shape",
    "Proposal",
    "shape_of",
    "detect",
    "proposals_from",
    "summarise",
]
