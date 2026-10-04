"""The Automations routes: what the screen is told, and when it is told the truth.

The engine's own tests cover firing. These cover the two places a screen can be
misled about a rule that is not going to work: a form that accepts an action nothing
implements, and a response that reports success for a rule stored disabled.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.routes import automations_routes
from nexus.db.repos import automations as repo

BASE = "/api/automations"


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(automations_routes.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def clean() -> None:
    for row in repo.list_all():
        repo.delete(row["id"])


def notify(message: str = "study") -> dict:
    return {"kind": "notify", "message": message}


# ------------------------------------------------------- refused at the door


def test_an_action_nothing_implements_is_refused_with_the_kinds_that_exist(
    client: TestClient,
) -> None:
    """Rejected while the user is still looking at the form.

    The engine would also catch this -- it disables the rule the first time it fires
    -- but that is hours later and looks like the automation broke rather than like
    the action was never valid.
    """
    response = client.post(
        BASE, json={"name": "bad", "trigger_kind": "interval", "schedule": "15m", "action": {"kind": "explode"}}
    )

    assert response.status_code == 400, response.text
    assert "notify" in response.json()["detail"]
    assert repo.list_all() == [], "a rule that cannot run was stored anyway"


def test_a_reminder_with_no_message_is_refused(client: TestClient) -> None:
    response = client.post(
        BASE, json={"name": "empty", "trigger_kind": "interval", "schedule": "15m", "action": {"kind": "notify"}}
    )

    assert response.status_code == 400
    assert "message" in response.json()["detail"].lower()


def test_an_automation_with_no_action_is_refused(client: TestClient) -> None:
    response = client.post(BASE, json={"name": "actionless", "trigger_kind": "manual"})

    assert response.status_code == 400
    assert repo.list_all() == []


def test_a_nameless_automation_is_refused(client: TestClient) -> None:
    response = client.post(
        BASE, json={"name": "   ", "trigger_kind": "interval", "schedule": "15m", "action": notify()}
    )

    assert response.status_code == 400


# --------------------------------------------------------------- stored honestly


def test_a_valid_rule_is_stored_armed(client: TestClient) -> None:
    response = client.post(
        BASE, json={"name": "study", "trigger_kind": "schedule", "schedule": "20:30", "action": notify()}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is True
    assert body["next_run_at"], "stored with no next run, so it will never fire"
    assert body["unavailable_reason"] is None


def test_an_unparseable_schedule_comes_back_disabled_and_explained(
    client: TestClient,
) -> None:
    """The route does not raise, and it does not pretend either.

    ``create`` stores an unparseable schedule disabled with a reason, so the honest
    response is the row itself -- ``enabled: false`` plus the reason -- rather than a
    200 that implies it is armed.
    """
    response = client.post(
        BASE, json={"name": "nonsense", "trigger_kind": "schedule", "schedule": "half past tea", "action": notify()}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is False
    assert body["next_run_at"] is None
    assert body["unavailable_reason"], "disabled with no explanation"


def test_the_list_reports_the_engine_state_beside_the_rules(client: TestClient) -> None:
    """A perfect rule still never fires if the engine is not running.

    Without this the screen has the rows and no way to say that nothing is watching
    the clock, which is the same class of lie as a toggle above an inert feature.
    """
    client.post(
        BASE, json={"name": "one", "trigger_kind": "interval", "schedule": "15m", "action": notify()}
    )

    body = client.get(BASE).json()
    assert len(body["automations"]) == 1
    assert "running" in body["engine"]
    assert "notify" in body["engine"]["action_kinds"]


# ------------------------------------------------------------------- run now


def test_running_now_fires_and_records_the_run(client: TestClient) -> None:
    created = client.post(
        BASE, json={"name": "manual", "trigger_kind": "manual", "schedule": "", "action": notify("fire now")}
    ).json()

    response = client.post(f"{BASE}/{created['id']}/run")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    runs = repo.runs(created["id"])
    assert len(runs) == 1, "a test fire that left no trace makes 'last run' a lie"
    assert runs[0]["detail"]["manual"] is True


def test_running_now_does_not_move_the_schedule(client: TestClient) -> None:
    """Checking a reminder works at 3pm must not cancel it at 8pm.

    Uses an **interval** rule deliberately. A daily ``"20:30"`` rule cannot detect
    this bug: ``parse_next_run`` builds it with ``base.replace(..., second=0,
    microsecond=0)``, so recomputing at any point before 20:30 lands on the very
    same instant, and an assertion of equality passes whether or not the rule was
    rescheduled. A mutation flipping ``reschedule=False`` to ``True`` survived a
    schedule-based version of this test for exactly that reason.

    An interval is ``base + delta`` from *now*, so a spurious reschedule moves it --
    and that is also where the real harm is: test-firing an "every 15 minutes" rule
    at 3pm must not push the next real run to 3:15.
    """
    created = client.post(
        BASE, json={"name": "every 15", "trigger_kind": "interval", "schedule": "15m", "action": notify()}
    ).json()

    client.post(f"{BASE}/{created['id']}/run")

    after = repo.get(created["id"])
    assert after is not None
    assert after["next_run_at"] == created["next_run_at"], (
        "a manual test fire reset the interval window, delaying the next real run"
    )


def test_running_now_does_not_move_a_daily_schedule_either(client: TestClient) -> None:
    """The same guarantee for the other trigger kind.

    Weaker than the interval test above by construction -- see its docstring -- and
    kept because it is the shape the user actually writes ("every evening at 20:30")
    and because a future change to how daily rules are stored would land here first.
    """
    created = client.post(
        BASE, json={"name": "evening", "trigger_kind": "schedule", "schedule": "20:30", "action": notify()}
    ).json()

    client.post(f"{BASE}/{created['id']}/run")

    after = repo.get(created["id"])
    assert after is not None
    assert after["next_run_at"] == created["next_run_at"]


def test_running_a_rule_whose_action_is_broken_reports_the_failure(
    client: TestClient,
) -> None:
    """A row can carry an action the routes would now reject -- from a sync, or an
    older build. Running it must report that, not raise."""
    stored = repo.create(
        "legacy", trigger_kind="manual", schedule="", action={"kind": "gone"}
    )

    response = client.post(f"{BASE}/{stored['id']}/run")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert "gone" in body["summary"]


# ------------------------------------------------------------------- missing


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", f"{BASE}/nope"),
        ("patch", f"{BASE}/nope"),
        ("delete", f"{BASE}/nope"),
        ("post", f"{BASE}/nope/run"),
        ("get", f"{BASE}/nope/runs"),
    ],
)
def test_an_unknown_id_is_a_404(client: TestClient, method: str, path: str) -> None:
    call = getattr(client, method)
    response = call(path, json={}) if method == "patch" else call(path)
    assert response.status_code == 404, response.text


def test_deleting_removes_the_rule(client: TestClient) -> None:
    created = client.post(
        BASE, json={"name": "temp", "trigger_kind": "interval", "schedule": "15m", "action": notify()}
    ).json()

    assert client.delete(f"{BASE}/{created['id']}").status_code == 200
    assert repo.get(created["id"]) is None


def test_patching_an_action_is_validated_too(client: TestClient) -> None:
    """The same guard on the edit path. Without it, a rule can be created valid and
    then edited into something the engine will disable on its next fire."""
    created = client.post(
        BASE, json={"name": "editable", "trigger_kind": "interval", "schedule": "15m", "action": notify()}
    ).json()

    response = client.patch(f"{BASE}/{created['id']}", json={"action": {"kind": "explode"}})

    assert response.status_code == 400
    assert repo.get(created["id"])["action"]["kind"] == "notify"
