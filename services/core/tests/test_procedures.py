"""Procedural memory: learning a repeated way of working, and the limits on it (O8).

The audits named this gap twice in the same words -- no learned-workflow memory
type, so outcome data accumulates and nothing improves from it. NEXUS could recall a
preference; it could not recall that the way you deploy is build → test → push.

Most of these tests are about what the detector must *refuse*, because the
interesting failure of this feature is not missing a pattern. It is confidently
recording one that is not there, or recording a pattern correctly but including the
arguments -- and arguments are where passwords, paths and one-off values live. A
procedure that cargo-culted last Tuesday's file path would look like knowledge and
act like a bug, which is worse than having no procedure at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus.ai import procedures
from nexus.ai.procedures import Proposal, Shape, detect, proposals_from, shape_of


def task(
    *, tid: str = "t1", status: str = "completed", title: str = "Deploy the app"
) -> dict[str, Any]:
    return {"id": tid, "status": status, "title": title}


def step(tool: str, *, status: str = "completed") -> dict[str, Any]:
    return {"tool": tool, "status": status, "params": {"path": "/secret/place"}}

def run(*tools: str, tid: str = "t1", title: str = "Deploy the app", status: str = "completed"):
    return task(tid=tid, title=title, status=status), [step(t) for t in tools]


# ------------------------------------------------------------------ the shapes


def test_only_completed_tasks_produce_a_shape() -> None:
    """A failed run is how it went wrong, not how it is done."""
    assert shape_of(task(status="failed"), [step("files.read"), step("files.write")]) is None
    assert shape_of(task(status="running"), [step("files.read"), step("files.write")]) is None


def test_steps_that_never_ran_are_left_out() -> None:
    """A procedure is what the task did, not what it intended to do."""
    shape = shape_of(
        task(),
        [step("files.read"), step("files.write", status="skipped"), step("net.fetch")],
    )
    assert shape is not None
    assert shape.steps == ("files.read", "net.fetch")


def test_a_single_step_is_not_a_procedure() -> None:
    """Remembering one tool call as a "workflow" teaches nothing."""
    assert shape_of(task(), [step("files.read")]) is None


def test_an_enormous_sequence_is_not_a_habit() -> None:
    """A hundred-step run is a one-off script, not something a person does by hand."""
    many = [step("files.read")] * (procedures.MAX_STEPS + 1)
    assert shape_of(task(), many) is None


def test_a_step_with_no_tool_is_named_as_reasoning() -> None:
    """Judgement steps matter to the shape even though they run nothing."""
    shape = shape_of(
        task(),
        [{"tool": None, "capability": "reasoning", "status": "completed"}, step("files.write")],
    )
    assert shape is not None
    assert shape.steps == ("(reasoning)", "files.write")


# ---------------------------------------------------------------- the detection


def test_the_same_sequence_twice_becomes_a_proposal() -> None:
    # Once is not a pattern: a single successful run is a run, not a habit.
    once = [shape_of(task(tid="solo"), [step("files.read"), step("files.write")])]
    assert detect([s for s in once if s]) == []

    shapes = [
        shape_of(task(tid="a"), [step("files.read"), step("files.write"), step("terminal.run")]),
        shape_of(task(tid="b"), [step("files.read"), step("files.write"), step("terminal.run")]),
    ]
    found = detect([s for s in shapes if s])
    assert len(found) == 1
    assert found[0].occurrences == 2
    assert found[0].task_ids == ["a", "b"]
    assert "Deploy the app" in found[0].title


def test_order_matters_to_whether_two_runs_are_the_same_job() -> None:
    """[read, write, test] and [write, read, test] are different jobs."""
    shapes = [
        shape_of(task(tid="a"), [step("files.read"), step("files.write"), step("terminal.run")]),
        shape_of(task(tid="b"), [step("files.write"), step("files.read"), step("terminal.run")]),
    ]
    found = detect([s for s in shapes if s])
    assert found == [], "reordered steps were treated as the same procedure"


def test_a_run_that_skipped_an_optional_step_still_counts() -> None:
    """A habit with an optional check in it is still the habit."""
    shapes = [
        shape_of(task(tid="a"), [step("files.read"), step("files.write"), step("terminal.run")]),
        shape_of(task(tid="b"), [step("files.read"), step("files.write")]),
    ]
    found = detect([s for s in shapes if s])
    assert len(found) == 1
    assert found[0].occurrences == 2
    # The most complete sequence is the one written down.
    assert found[0].steps == ("files.read", "files.write", "terminal.run")


def test_unrelated_work_is_not_grouped() -> None:
    """Grouping by shared tools would invent habits out of a shared toolbox."""
    shapes = [
        shape_of(task(tid="a", title="Deploy"), [step("files.read"), step("files.write")]),
        shape_of(task(tid="b", title="Check mail"), [step("net.fetch"), step("system.notify")]),
    ]
    assert detect([s for s in shapes if s]) == []


def test_three_runs_of_one_habit_are_one_proposal() -> None:
    shapes = [
        shape_of(task(tid=t), [step("files.read"), step("files.write")])
        for t in ("a", "b", "c")
    ]
    found = detect([s for s in shapes if s])
    assert len(found) == 1
    assert found[0].occurrences == 3


def test_proposals_are_ordered_by_how_often_the_habit_is_practised() -> None:
    shapes = []
    for tid in ("a", "b"):
        shapes.append(shape_of(task(tid=tid, title="Rare"), [step("net.fetch"), step("files.read")]))
    for tid in ("c", "d", "e"):
        shapes.append(shape_of(task(tid=tid, title="Often"), [step("files.write"), step("terminal.run")]))
    found = detect([s for s in shapes if s])
    assert [p.occurrences for p in found] == [3, 2]
    assert "Often" in found[0].title


# ---------------------------------------------------- what must never be stored


def test_the_arguments_are_never_recorded() -> None:
    """The constraint that matters most: args are where secrets and one-off values live."""
    shapes = [
        shape_of(task(tid="a"), [step("files.read"), step("files.write")]),
        shape_of(task(tid="b"), [step("files.read"), step("files.write")]),
    ]
    found = detect([s for s in shapes if s])
    assert found
    blob = found[0].body + found[0].title + str(found[0].steps)
    assert "/secret/place" not in blob, "a step's parameters reached the memory"
    assert "params" not in blob


def test_the_body_states_its_own_evidence() -> None:
    """"Done 2 times" is what separates a learned procedure from an assertion."""
    shapes = [
        shape_of(task(tid="a"), [step("files.read"), step("files.write")]),
        shape_of(task(tid="b"), [step("files.read"), step("files.write")]),
    ]
    found = detect([s for s in shapes if s])
    assert "Done 2 times" in found[0].body
    assert "learned from 2 completed tasks" in found[0].body.lower()


def test_a_proposal_is_offered_as_a_procedural_memory() -> None:
    """It goes through the same approval door as every other memory (§32)."""
    shapes = [
        shape_of(task(tid="a"), [step("files.read"), step("files.write")]),
        shape_of(task(tid="b"), [step("files.read"), step("files.write")]),
    ]
    payload = detect([s for s in shapes if s])[0].to_dict()
    assert payload["category"] == "procedural"
    assert "occurrences" in payload and "task_ids" in payload


def test_procedural_is_a_real_memory_category() -> None:
    """A category the store rejects would make every proposal silently vanish."""
    from nexus.db.repos import memories

    assert "procedural" in memories.CATEGORIES


# --------------------------------------------------------------- the entry point


def test_the_entry_point_skips_unfinished_work() -> None:
    pairs = [
        run("files.read", "files.write", tid="a"),
        run("files.read", "files.write", tid="b"),
        run("files.read", "files.write", tid="c", status="failed"),
    ]
    found = proposals_from(pairs)
    assert len(found) == 1
    assert found[0].task_ids == ["a", "b"], "a failed run contributed to a procedure"


def test_the_summary_says_what_was_noticed_and_says_nothing_when_nothing_was() -> None:
    assert procedures.summarise([]) == ""
    found = proposals_from([run("files.read", "files.write", tid="a"),
                            run("files.read", "files.write", tid="b")])
    line = procedures.summarise(found)
    assert "repeated way of working" in line
    assert "2 times" in line
