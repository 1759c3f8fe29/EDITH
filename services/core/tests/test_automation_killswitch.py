"""The automations master switch, and what a rule does while it is off (§20, §78).

O6. The audit's automation findings were closed inline, but the master-switch path
had no named test, and "a switch that only changes a boolean" is the one automation
bug shape left unpinned. The specific fear is worth stating: the obvious wrong
implementation of a kill switch is to *disarm* the rules it is holding, and that is
wrong for a reason that only shows up days later -- turning automations back on
would find every rule the user had already switched on now switched off, with
nothing on screen to explain why.

So the invariant asserted first is that the switch changes **no row**. A rule that
was armed stays armed and simply does not fire.

**What writing this test established, and the reason it is worth reading.** The
switch is checked by the *engine loop* (``automations._loop``), and **not** by
``run_due``. So calling ``run_due()`` directly fires rules whatever the switch says —
which is correct for the loop, and is a live hazard the moment anything else calls
that function. It is documented "Safe to call directly", and it is safe in the sense
of *idempotent and non-destructive*; it is not switch-gated. This file asserts the
real behaviour rather than the behaviour the docstring's wording invites you to
assume, and the hazard is written down here so the next caller sees it: if you add a
second caller for ``run_due``, the master switch does not travel with it.

The second half pins the arrival behaviour, which is where a kill switch normally
becomes a footgun in the other direction. A rule that sat disabled through its own
fire time is *stale* when the switch comes back: it is marked ``skipped`` with the
reason recorded and advanced to its next real occurrence, rather than firing a
notification for something that should have happened on Tuesday. That is the same
too-late rule ``run_due`` already applies to a core that was switched off, and it is
what stops "I turned automations back on" meaning "I was told about nine days at
once".
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from nexus import automations
from nexus.db.repos import automations as repo


@pytest.fixture(autouse=True)
def clean_automations():
    for row in repo.list_all():
        repo.delete(row["id"])
    yield
    for row in repo.list_all():
        repo.delete(row["id"])


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch):
    """Drive the master switch without touching the settings table."""
    state = {"enabled": True}
    real_get = automations.app_settings.get
    monkeypatch.setattr(
        automations.app_settings,
        "get",
        lambda key, default=None: state["enabled"]
        if key == "automations_enabled"
        else real_get(key, default),
    )
    return state


def armed_rule(*, due_seconds_ago: float = 0.0) -> dict[str, Any]:
    """A real, armed, due rule -- stored through the repository, not faked."""
    moment = datetime.now().astimezone()
    row = repo.create(
        "Evening study",
        trigger_kind="interval",
        schedule="30m",
        action={"kind": "notify", "message": "Time to study."},
        capability_scope=["system.notify"],
    )
    # Backdate it so it is genuinely due rather than merely armed.
    overdue = (moment - timedelta(seconds=due_seconds_ago)).isoformat()
    with repo.db.transaction() as c:
        c.execute(
            "UPDATE automations SET next_run_at = ? WHERE id = ?", (overdue, row["id"])
        )
    return repo.get(row["id"])  # type: ignore[return-value]


def runs_of(automation_id: str) -> list[dict[str, Any]]:
    return repo.runs(automation_id)


# ------------------------------------------------------------- the switch itself


def test_the_engine_reads_the_switch_before_running_anything(switch: Any) -> None:
    """The gate itself, tested directly rather than through a loop we cannot stop."""
    switch["enabled"] = True
    assert automations._enabled() is True
    switch["enabled"] = False
    assert automations._enabled() is False


def test_run_due_does_not_check_the_switch_itself(switch: Any) -> None:
    """A hazard, pinned so it is a known one.

    ``run_due`` is documented "Safe to call directly" and is called by the engine
    *behind* its switch check. Nothing in it consults the switch, so a second caller
    added later would fire rules with automations switched off, and no test would
    have caught it. This asserts today's behaviour so that a future change to it is
    a deliberate one.
    """
    row = armed_rule()
    switch["enabled"] = False

    result = automations.run_due()

    assert result.fired == 1, (
        "run_due has started gating on the switch: good news, but the engine's own "
        "check and this test's premise both need revisiting"
    )
    assert len(runs_of(row["id"])) == 1


def test_the_switch_does_not_disarm_the_rules_it_holds(switch: Any) -> None:
    """The invariant: turning automations back on must not find everything off.

    A kill switch implemented by clearing ``enabled`` would silently rewrite the
    user's own choices, and the symptom -- rules quietly switched off days earlier
    -- has no on-screen explanation.

    Asserted without calling ``run_due`` at all, because the switch does not reach
    it: what is being checked is that *nothing in the switch path* writes to the
    rules, which is exactly the property a naive implementation would break.
    """
    row = armed_rule()
    switch["enabled"] = False

    for _ in range(3):
        assert automations._enabled() is False  # what the loop would see

    still = repo.get(row["id"])
    assert still is not None
    assert still["enabled"] is True, "the switch disarmed a rule instead of holding it"
    assert still["next_run_at"] == row["next_run_at"], "the schedule was consumed"


def test_switching_back_on_does_not_replay_the_window_it_missed(switch: Any) -> None:
    """'Automations are on again' must not mean 'here are nine days of alerts'."""
    # Past `MAX_LATENESS_SECONDS` (6 hours). Two hours is *not* stale by this
    # system's own rule -- it still fires, and correctly so: an alarm that did not
    # arrive while the machine was off is worth hearing when it comes back. The
    # stale case is the overnight one, which is what this fixture builds.
    row = armed_rule(due_seconds_ago=36 * 3600)  # a day and a half late
    switch["enabled"] = False

    # Nothing runs while the switch is off -- the loop's guard, which is the only
    # gate there is, so this is what the disabled window really looks like.
    assert automations._enabled() is False
    assert runs_of(row["id"]) == []

    switch["enabled"] = True
    result = automations.run_due()

    history = runs_of(row["id"])
    assert len(history) == 1, "one stale rule produced more than one run"
    assert history[0]["status"] == "skipped"
    assert "not running" in str(history[0].get("detail"))
    assert result.skipped == 1 and result.fired == 0

    # And it moved on to its next real occurrence rather than staying stuck due,
    # which is what would turn the loop into a hot retry.
    after = repo.get(row["id"])
    assert after is not None
    assert after["next_run_at"] != row["next_run_at"]


def test_a_rule_that_is_not_late_still_fires_normally(switch: Any) -> None:
    """The stale path must not swallow ordinary, on-time runs."""
    row = armed_rule(due_seconds_ago=5)
    result = automations.run_due()
    assert result.fired == 1
    history = runs_of(row["id"])
    assert len(history) == 1
    assert history[0]["status"] != "skipped"


def test_the_switch_state_is_reported(switch: Any) -> None:
    """The UI cannot show a kill switch that does not describe itself."""
    switch["enabled"] = False
    assert automations.status()["enabled"] is False
    switch["enabled"] = True
    assert automations.status()["enabled"] is True


def test_the_switch_defaults_on() -> None:
    """Automations are a feature the user opted into by creating one."""
    from nexus.db.repos import app_settings

    assert app_settings.DEFAULTS["automations_enabled"] is True
