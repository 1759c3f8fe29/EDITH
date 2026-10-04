"""The two retry routes, over the real router and the real store (§40, §64).

These exist because the desktop screen now has a Retry button, and everything that
button believes about a task comes from the JSON these two routes return. A change to
either shape breaks the dialog silently -- a missing ``not_idempotent`` does not throw,
it just stops warning.

Three claims are worth a route test rather than a unit test of ``agent.retry_preview``:

**The preview is a GET and changes nothing.** The whole reason it is a separate call is
that the user is shown what would happen *before* it happens. A GET that quietly reset
step statuses would defeat that, and only a test that reads the rows afterwards can say
it does not.

**A refusal is a 409 carrying the reason the preview gave.** Two routes, one sentence:
a caller that skipped the preview must still be told why, in the same words, or the UI
and the API disagree about the same task.

**The step list names the tools whose repeat is not safe.** That is the one fact in the
payload that should change a person's answer, and it comes from ``Tool.idempotent`` --
a flag nothing read at all until the retry policy did.

``agent.start`` is stubbed, and only that: a retry route's job is to rewrite stored
statuses and hand off, so the hand-off is recorded rather than performed. Everything
else here -- the router, the task store, the status CHECK constraint, the registry -- is
the shipped code.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.ai import agent
from nexus.api.routes import agent_routes
from nexus.db.repos import tasks


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The real router, with the hand-off to the run loop recorded instead of run."""
    started: list[str] = []
    monkeypatch.setattr(agent, "start", lambda task_id: started.append(task_id))
    monkeypatch.setattr(agent, "is_running", lambda task_id: False)
    app = FastAPI()
    app.include_router(agent_routes.router)
    test = TestClient(app)
    test.started = started  # type: ignore[attr-defined]
    return test


def make_task(step_specs: list[tuple[str, str | None, str]]) -> dict[str, Any]:
    """A task with steps already in the statuses a real run would have left.

    ``step_specs`` is ``(intent, tool, status)``. The statuses are written through
    ``set_step_status``, so the ``task_steps`` CHECK constraint gets a say -- a spec
    naming a status the schema does not have fails here rather than in production.
    """
    task = tasks.create("check the retry routes")
    rows = tasks.add_steps(
        task["id"],
        [{"intent": intent, "tool": tool, "params": {}} for intent, tool, _ in step_specs],
    )
    for row, (_, _, status) in zip(rows, step_specs):
        if status != "pending":
            tasks.set_step_status(row["id"], status, error="it did not work" if status == "failed" else None)
    return task


# ------------------------------------------------------------------ preview


def test_the_preview_says_what_would_run_and_what_is_kept(client: TestClient) -> None:
    task = make_task(
        [
            ("read the file", "files.read", "completed"),
            ("write the file", "files.write", "failed"),
            ("think about it", None, "failed"),
        ]
    )
    tasks.set_status(task["id"], "failed", error="the write did not work")
    body = client.get(f"/api/tasks/{task['id']}/retry").json()

    assert body["can_retry"] is True
    assert body["reason"] == ""
    assert body["keeping"] == 1
    # In plan order, and only the ones that did not succeed.
    assert [s["intent"] for s in body["steps"]] == ["write the file", "think about it"]
    # Tri-state, and the None is the point: a step with no tool has no idempotence,
    # and False there would read as a warning about a step that touches nothing.
    by_intent = {s["intent"]: s["idempotent"] for s in body["steps"]}
    assert by_intent["think about it"] is None
    # ``files.write`` really is declared non-idempotent (files.py), so this reads the
    # registry flag rather than the fallback an unknown tool name would hit.
    assert by_intent["write the file"] is False
    assert body["not_idempotent"] == ["files.write"]


def test_only_the_unsafe_tools_are_named_as_unsafe(client: TestClient) -> None:
    """``not_idempotent`` is a filter, not a list of everything that would re-run.

    Without this, a bug that named every tool would still satisfy the test above --
    and a dialog that warns about every step warns about nothing.
    """
    task = make_task(
        [
            ("read it again", "files.read", "failed"),
            ("write it again", "files.write", "failed"),
        ]
    )
    tasks.set_status(task["id"], "failed", error="both went wrong")
    body = client.get(f"/api/tasks/{task['id']}/retry").json()

    assert len(body["steps"]) == 2
    assert body["not_idempotent"] == ["files.write"]
    assert {s["tool"]: s["idempotent"] for s in body["steps"]} == {
        "files.read": True,
        "files.write": False,
    }


def test_the_preview_changes_nothing(client: TestClient) -> None:
    """It is a GET. A preview that reset the rows would be the retry, done silently."""
    task = make_task([("write the file", "files.write", "failed")])
    tasks.set_status(task["id"], "failed", error="the write did not work")
    before = [(s["id"], s["status"], s["error"]) for s in tasks.list_steps(task["id"])]

    body = client.get(f"/api/tasks/{task['id']}/retry")
    assert body.status_code == 200
    # Retryable, so this is the case where a preview *could* have reset something.
    assert body.json()["can_retry"] is True

    after = [(s["id"], s["status"], s["error"]) for s in tasks.list_steps(task["id"])]
    assert after == before
    assert tasks.get(task["id"])["status"] == "failed"
    assert client.started == []  # type: ignore[attr-defined]


def test_an_unknown_task_is_404_on_both_routes(client: TestClient) -> None:
    assert client.get("/api/tasks/nope/retry").status_code == 404
    assert client.post("/api/tasks/nope/retry").status_code == 404


def test_a_completed_task_has_nothing_to_retry(client: TestClient) -> None:
    task = make_task([("read the file", "files.read", "completed")])
    tasks.set_status(task["id"], "completed", result="done")
    body = client.get(f"/api/tasks/{task['id']}/retry").json()

    assert body["can_retry"] is False
    assert body["steps"] == []
    assert "completed" in body["reason"]


def test_a_failed_task_whose_steps_all_completed_says_so(client: TestClient) -> None:
    """Reachable, and the one refusal the screen cannot work out for itself.

    The task failed after its last step succeeded -- finalising, or a verifier at the
    task level. Status alone says "retryable"; only the step rows say there is nothing
    to re-run, which is why the dialog asks the core instead of deciding.
    """
    task = make_task([("read the file", "files.read", "completed")])
    tasks.set_status(task["id"], "failed", error="the summary could not be written")
    body = client.get(f"/api/tasks/{task['id']}/retry").json()

    assert body["can_retry"] is False
    assert "No step" in body["reason"]


# -------------------------------------------------------------------- retry


def test_retrying_queues_the_failed_steps_and_hands_off(client: TestClient) -> None:
    task = make_task(
        [
            ("read the file", "files.read", "completed"),
            ("write the file", "files.write", "failed"),
        ]
    )
    tasks.set_status(task["id"], "failed", error="the write did not work")

    response = client.post(f"/api/tasks/{task['id']}/retry")
    assert response.status_code == 200

    steps = {s["intent"]: s for s in tasks.list_steps(task["id"])}
    # Completed work is not redone. That is the whole meaning of "from the last good step".
    assert steps["read the file"]["status"] == "completed"
    assert steps["write the file"]["status"] == "pending"
    # The task's own verdict is cleared, not left to be read as this attempt's outcome.
    row = tasks.get(task["id"])
    assert row["status"] == "pending"
    assert row["error"] is None
    assert client.started == [task["id"]]  # type: ignore[attr-defined]

    # And the response is the same payload shape every other task route returns, so
    # the screen can redraw from it without a second request.
    body = response.json()
    assert body["task"]["id"] == task["id"]
    assert body["progress"]["total"] == 2
    assert body["progress"]["completed"] == 1


def test_a_refusal_is_409_with_the_reason_the_preview_gave(client: TestClient) -> None:
    task = make_task([("read the file", "files.read", "completed")])
    tasks.set_status(task["id"], "completed", result="done")

    preview = client.get(f"/api/tasks/{task['id']}/retry").json()
    response = client.post(f"/api/tasks/{task['id']}/retry")

    assert response.status_code == 409
    # Same sentence, so a caller that skipped the preview is told the same thing.
    assert response.json()["detail"] == preview["reason"]
    assert client.started == []  # type: ignore[attr-defined]


def test_a_running_task_is_not_retried(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    """A retry rewrites step rows. Doing that under a live run corrupts its own record."""
    task = make_task([("write the file", "files.write", "failed")])
    tasks.set_status(task["id"], "failed", error="the write did not work")
    monkeypatch.setattr(agent, "is_running", lambda task_id: True)

    assert client.get(f"/api/tasks/{task['id']}/retry").json()["can_retry"] is False
    response = client.post(f"/api/tasks/{task['id']}/retry")
    assert response.status_code == 409
    assert "in flight" in response.json()["detail"]
    # Untouched: still failed, not quietly reset by the refused call.
    assert tasks.list_steps(task["id"])[0]["status"] == "failed"
