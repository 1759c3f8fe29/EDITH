"""Automations the assistant can actually set (system-control audit, gap 3).

Before these tools, *"every evening at seven, remind me to study"* was a request
NEXUS could discuss and not carry out: it would describe the rule it was going to
create, create nothing, and the user would find out next week. The audit called that
gap 3; these tests are what stop it reopening.

The assertions divide into two kinds, and the second kind matters more.

**It works**: a created rule is really in the database, armed, with a next run time,
and the verifier confirms it by reading the row back rather than trusting the return
value.

**It refuses, and refuses for the right reason**: no message (a rule that fires and
says nothing is indistinguishable from one that never fired), no schedule on a timed
trigger (there is no moment at which it would run), an unparseable schedule reported
as *stored but disabled* rather than as success, and a delete or toggle against an id
that does not exist refused with a pointer to ``automation.list`` instead of a guess.
That last one is the ``memory.forget`` rule restated: "cancel my reminders" could
mean one rule or nine, and a natural-language guess is not a mandate to delete.

The verifier tests are the load-bearing ones. The repository deliberately stores a
schedule it cannot parse as disabled-and-labelled, so "the row exists" is *not* the
same as "the automation will run" -- and a verifier that could not tell those apart
would report a rule that will never fire as a success.
"""

from __future__ import annotations

from typing import Any

import pytest

import nexus.tools  # noqa: F401  -- registers every tool module
from nexus.db.repos import automations as repo
from nexus.permissions import capabilities
from nexus.tools import registry
from nexus.tools.automations import (
    automation_create,
    automation_delete,
    automation_list,
    automation_set_enabled,
)


@pytest.fixture(autouse=True)
def clean_automations():
    """Each test starts with no rules, and leaves none behind."""
    for row in repo.list_all():
        repo.delete(row["id"])
    yield
    for row in repo.list_all():
        repo.delete(row["id"])


def created(**overrides: Any) -> dict[str, Any]:
    params = {
        "name": "Evening study",
        "trigger_kind": "schedule",
        "schedule": "19:00",
        "message": "Time to study.",
    }
    params.update(overrides)
    return params


# --------------------------------------------------------------- it works


def test_a_scheduled_reminder_is_really_scheduled() -> None:
    """The sentence the audit said was not executable, executed."""
    result = automation_create(created())
    assert result.ok, result.error

    row = repo.get(result.data["id"])
    assert row is not None
    assert row["name"] == "Evening study"
    assert row["trigger_kind"] == "schedule"
    assert row["enabled"] is True
    assert row["next_run_at"], "an armed schedule must have a next run time"
    assert row["unavailable_reason"] in (None, "")
    assert row["action"]["kind"] == "notify"
    assert row["action"]["message"] == "Time to study."


def test_the_rule_is_armed_with_only_the_power_it_needs() -> None:
    """An automation must not be a way to launder a capability past the engine."""
    result = automation_create(created())
    row = repo.get(result.data["id"])
    assert row is not None
    # Exactly the one capability notifying requires -- not "whatever is allowed at
    # fire time", which is what makes a scheduled rule safe to arm in advance.
    assert row["capability_scope"] == ["system.notify"]


def test_an_interval_rule_is_accepted() -> None:
    result = automation_create(
        created(trigger_kind="interval", schedule="20m", name="Look away")
    )
    assert result.ok, result.error
    row = repo.get(result.data["id"])
    assert row is not None and row["trigger_kind"] == "interval"
    assert row["next_run_at"]


def test_listing_reports_what_exists_and_says_so_when_nothing_does() -> None:
    empty = automation_list({})
    assert empty.ok and empty.data["count"] == 0
    assert "No automations" in empty.summary

    automation_create(created())
    filled = automation_list({})
    assert filled.data["count"] == 1
    assert "Evening study" in filled.summary


def test_disabling_keeps_the_rule_so_it_can_come_back() -> None:
    """'Pause my reminders' must not be implemented as a delete."""
    made = automation_create(created())
    aid = made.data["id"]

    off = automation_set_enabled({"automation_id": aid, "enabled": False})
    assert off.ok
    assert repo.get(aid) is not None, "disabling must not remove the rule"
    assert repo.get(aid)["enabled"] is False

    on = automation_set_enabled({"automation_id": aid, "enabled": True})
    assert on.ok and repo.get(aid)["enabled"] is True


def test_deleting_removes_it() -> None:
    made = automation_create(created())
    aid = made.data["id"]
    gone = automation_delete({"automation_id": aid})
    assert gone.ok
    assert repo.get(aid) is None


# ------------------------------------------------------------- it refuses


def test_a_rule_with_no_message_is_refused() -> None:
    """A rule that fires and says nothing cannot be told from one that never fired."""
    result = automation_create(created(message=""))
    assert not result.ok
    assert "message" in (result.error or "").lower()
    assert repo.list_all() == []


def test_a_timed_rule_with_no_schedule_is_refused() -> None:
    """There is no moment at which it would ever run."""
    for kind in ("schedule", "interval"):
        result = automation_create(created(trigger_kind=kind, schedule=""))
        assert not result.ok, f"{kind} with no schedule was accepted"
        assert "schedule" in (result.error or "").lower()
    assert repo.list_all() == []


def test_an_unknown_trigger_kind_is_refused() -> None:
    result = automation_create(created(trigger_kind="whenever"))
    assert not result.ok
    assert repo.list_all() == []


def test_a_nameless_rule_is_refused() -> None:
    result = automation_create(created(name="   "))
    assert not result.ok
    assert repo.list_all() == []


def test_an_unreadable_schedule_is_stored_disabled_and_says_so() -> None:
    """Honest about not working beats both losing the intent and faking success."""
    result = automation_create(created(schedule="half past nine"))
    # Stored, because the user's intent is worth keeping visible...
    assert result.ok
    row = repo.get(result.data["id"])
    assert row is not None
    # ...but disabled and labelled, not armed.
    assert row["enabled"] is False
    assert row["unavailable_reason"]
    assert "disabled" in result.summary.lower()


def test_the_verifier_refuses_to_call_a_rule_that_cannot_run_a_success() -> None:
    """The load-bearing one: 'the row exists' is not 'the automation will run'."""
    tool = registry.get("automation.create")
    assert tool is not None and tool.verifier is not None

    good = automation_create(created())
    ok, note = tool.verifier({}, good)
    assert ok is True, note

    broken = automation_create(created(schedule="quarter to five", name="Broken"))
    ok, note = tool.verifier({}, broken)
    assert ok is False, "a rule that will never fire was verified as working"
    assert "will not run" in note or "disabled" in note


def test_changing_an_id_that_does_not_exist_is_refused_with_a_pointer() -> None:
    """Never guess an id: 'cancel my reminders' could match one rule or nine."""
    for call in (
        lambda: automation_delete({"automation_id": "nope"}),
        lambda: automation_set_enabled({"automation_id": "nope", "enabled": False}),
    ):
        result = call()
        assert not result.ok
        assert "automation.list" in (result.error or "")


def test_a_missing_id_is_refused_rather_than_defaulted() -> None:
    assert not automation_delete({}).ok
    assert not automation_set_enabled({"enabled": False}).ok


# ------------------------------------------------------- wiring and posture


def test_the_tools_are_registered_and_declare_real_capabilities() -> None:
    known = {c.name for c in capabilities.CAPABILITIES}
    for name in (
        "automation.list",
        "automation.create",
        "automation.set_enabled",
        "automation.delete",
    ):
        tool = registry.get(name)
        assert tool is not None, f"{name} is not registered"
        assert tool.capability in known, f"{name} declares an unknown capability"


def test_writing_is_gated_harder_than_reading() -> None:
    """An automation is consent that fires later, so creating one must cost more."""
    by_name = {c.name: c for c in capabilities.CAPABILITIES}
    order = ["low", "medium", "high", "critical"]
    assert order.index(by_name["automation.write"].risk) > order.index(
        by_name["automation.read"].risk
    )
    # And writing a rule that acts on a schedule is gated at least as hard as
    # writing a memory, which only ever gets read back to the user.
    assert order.index(by_name["automation.write"].risk) >= order.index(
        by_name["memory.write"].risk
    )


def test_the_description_does_not_promise_actions_it_cannot_perform() -> None:
    """The engine only notifies; saying otherwise would be a promise NEXUS breaks."""
    tool = registry.get("automation.create")
    assert tool is not None
    assert "notify" in tool.description.lower()
    # And the schedule field must describe the parser the engine actually has:
    # implying cron would make every rule the model creates arrive disabled.
    schedule_doc = tool.parameters["properties"]["schedule"]["description"].lower()
    assert "19:00" in schedule_doc and "cron" in schedule_doc
    schema = tool.parameters["properties"]
    # No free-form "action" or "command" field the engine would silently ignore.
    assert "command" not in schema and "action" not in schema
