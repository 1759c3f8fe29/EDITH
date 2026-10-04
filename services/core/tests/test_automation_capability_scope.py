"""What an automation is allowed to do while nobody is watching (§5, §34, §36).

Until now the only thing a rule could do was ``notify``, which writes an activity row
and publishes on the in-process bus. It reaches nothing outside NEXUS's own database,
so "may this run unattended" never had to be answered. The moment a second action kind
exists that touches the machine, it does -- and the wrong answer is not a crash. It is
a rule that quietly does more than the user agreed to, on a schedule, with no one
there to notice.

Two checks stand between a stored rule and the machine, and the tests here exist
mostly to prove that *neither one is redundant*:

* the **stored scope**, which is what the user agreed to when they armed the rule;
* the **live engine**, which is whether that permission still holds right now.

The single most important test in this file is
:func:`test_trusted_mode_does_not_widen_a_stored_scope`. Trusted mode is standing
consent to the questions NEXUS would have asked, and it is very easy to write a
fire-time check that consults only the engine -- which, in trusted mode, says yes to
everything it was ever going to ask about. That version passes every other test here.
It also means editing a reminder into a file-reader silently grants it the file
system.

The permission engine is the real one. Nothing is stubbed: the real catalogue, the
real policy layer, the real store.
"""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus import automations as engine
from nexus.api.routes import automations_routes
from nexus.db import conn as db
from nexus.db.repos import app_settings, automations as repo, permissions_store
from nexus.permissions import capabilities

BASE = "/api/automations"

#: A capability a made-up action can plausibly need, chosen because it is real, is
#: not implicit, and has a hard-limit path (a credentials directory) that lets one
#: test tell "refused in code" from "not granted yet".
NEEDED = "file.read"
READABLE = "/home/santosh/nexus-scope-probe.txt"
PROTECTED = "/home/santosh/.ssh/id_rsa"
#: What a person is shown. Taken from the catalogue rather than spelled out, so a
#: reworded title is not a test failure -- but a message built from the raw
#: capability name instead of the catalogue's words still is.
TITLE = capabilities.get(NEEDED).title.lower()


@pytest.fixture(autouse=True)
def only_this_tests_rules() -> None:
    """``run_due`` fires every due rule, so counts are meaningless with leftovers."""
    for row in repo.list_all():
        repo.delete(row["id"])


@pytest.fixture(autouse=True)
def peek():
    """A second action kind, so the question this file is about can be asked at all.

    Registered rather than waited for. The alternative is testing consent only once a
    real capability-using action exists, which is the order that produces the bug:
    the enforcement lands in the same commit as the first thing that needs it, under
    time pressure, untested.
    """
    ran: list[dict] = []

    def run(automation: dict, action: dict) -> engine.Outcome:
        ran.append(action)
        return engine.Outcome(status="completed", summary="peeked")

    engine._ACTIONS["peek"] = engine.ActionKind(
        kind="peek", run=run, capabilities=frozenset({NEEDED})
    )
    try:
        yield ran
    finally:
        del engine._ACTIONS["peek"]


@pytest.fixture(autouse=True)
def permissions_restored():
    keys = ("permission_mode", "confirm_destructive")
    before = {key: app_settings.get(key) for key in keys}
    app_settings.set("permission_mode", "ask")
    engine.permission_engine.clear_session()
    yield
    permissions_store.revoke(NEEDED, "*")
    engine.permission_engine.clear_session()
    for key, value in before.items():
        if value is not None:
            app_settings.set(key, value)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(automations_routes.router)
    return TestClient(app)


def make(action: dict, scope: list[str] | None = None, *, schedule: str = "15m") -> dict:
    return repo.create(
        "scope rule",
        trigger_kind="interval",
        schedule=schedule,
        action=action,
        capability_scope=scope,
    )


def after(automation: dict, seconds: float) -> datetime:
    return datetime.fromisoformat(str(automation["next_run_at"])) + timedelta(seconds=seconds)


def peek_action(path: str = READABLE) -> dict:
    return {"kind": "peek", "path": path}


def grant() -> None:
    permissions_store.upsert_grant(NEEDED, scope="*", grant_kind="always")


# ------------------------------------------------- the declarations themselves


def test_notify_declares_no_capabilities_and_that_is_why_it_runs_unattended() -> None:
    """The floor this whole design rests on.

    If ``notify`` needed a capability, every existing automation would be disarmed by
    migration v4 -- ``DEFAULT '[]'`` would be the wrong default and the upgrade would
    silently retire the user's reminders. Asserted as an empty set rather than assumed
    from reading ``_notify``, because the day someone makes it call the desktop
    notification daemon, this is the test that says what else has to change.
    """
    assert engine.required_capabilities({"kind": "notify"}) == frozenset()


def test_every_declared_capability_exists_in_the_catalogue() -> None:
    """A typo in a declaration is a rule that can never run, not a rule that over-reaches.

    ``evaluate`` denies an unknown capability as a hard limit, so ``file.raed`` would
    disable the rule with a refusal that reads like NEXUS being locked down. The
    registry checks this at import; this is the same check where a failure names the
    action.
    """
    for kind, spec in engine._ACTIONS.items():
        for name in sorted(spec.capabilities):
            assert capabilities.exists(name), f"{kind} declares {name!r}, which is not real"


def test_an_unregistered_kind_has_unknown_powers_not_no_powers() -> None:
    """Fail closed on the thing that has not been written yet.

    Returning ``frozenset()`` here would be the whole enforcement undone by omission:
    a future action kind would reach the machine by not being in the registry.
    """
    with pytest.raises(engine.UnknownActionKind):
        engine.required_capabilities({"kind": "some-future-thing"})
    with pytest.raises(engine.UnknownActionKind):
        engine.required_capabilities({})


def test_a_corrupt_scope_grants_nothing() -> None:
    """A consent record that cannot be read is not a consent record.

    Anything that is not a JSON array of strings has to decode to the empty scope. The
    other direction -- treating an unreadable value as permissive -- would make a
    hand-edited or half-synced row the way past both checks.
    """
    for junk in (None, "file.read", 7, {"file.read": True}, [], [1, None, ""]):
        assert engine.normalise_scope(junk) == [], junk
    assert engine.scope_gap(peek_action(), "file.read") == [NEEDED], (
        "a string that happens to contain the capability name is not a list of one"
    )


def test_a_row_whose_stored_scope_is_not_a_list_grants_nothing() -> None:
    """The threat model for this decoder is a row that did not come through the API.

    A sync from another device, a hand-edited database, an older version of this file:
    all of them can put something in that column the routes would have refused. The
    repository has to read that as the empty scope, because the alternative -- passing
    it through and letting the set comparison decide -- means a malformed value that
    happens to contain the right substring authorises the action.
    """
    automation = make(peek_action(), [NEEDED])
    with db.transaction() as c:
        c.execute(
            "UPDATE automations SET capability_scope = ? WHERE id = ?",
            ('"file.read"', automation["id"]),
        )

    assert (repo.get(automation["id"]) or {})["capability_scope"] == []
    assert repo.due(at=after(automation, 1))[0]["capability_scope"] == [], (
        "due() is the path the engine actually reads, and it decoded the junk"
    )


# ----------------------------------------------------- the stored-scope check


def test_a_rule_whose_scope_misses_a_capability_is_disarmed_not_retried() -> None:
    """Consent short of what the action needs stops the rule, permanently, with a reason.

    Permanent because retrying changes nothing: the shortfall is in what the user
    agreed to, not in what the machine will allow. And ``next_run_at`` must be the
    thing that gets cleared -- ``due()`` selects on it, so a rule left with a past
    timestamp is re-selected every tick and this becomes a run row every 20 seconds.
    """
    grant()  # the engine would say yes; the stored scope is what refuses
    automation = make(peek_action(), [])

    result = engine.run_due(at=after(automation, 1))
    assert result.failed == 1, result

    row = repo.get(automation["id"]) or {}
    assert row["enabled"] is False
    assert row["next_run_at"] is None, "left due, so this refusal fires every tick"
    assert TITLE in (row["unavailable_reason"] or ""), row["unavailable_reason"]
    assert repo.runs(automation["id"])[0]["denied_capability"] == NEEDED

    later = engine.run_due(at=after(automation, 3600))
    assert later.failed == 0 and later.fired == 0, f"the disarmed rule ran again: {later}"


def test_trusted_mode_does_not_widen_a_stored_scope(peek) -> None:
    """The test that proves the stored scope is not decoration.

    Trusted mode answers yes to every question NEXUS would have asked. A fire-time
    check that consulted only the permission engine would therefore let a rule armed
    as a reminder read files the moment someone edited its action -- no prompt, no
    record, nothing on screen. Every other test in this file passes against that
    version. This one does not.
    """
    app_settings.set("permission_mode", "trusted")
    automation = make(peek_action(), [])

    result = engine.run_due(at=after(automation, 1))

    assert result.failed == 1, result
    assert peek == [], "the action ran with consent it never had"
    row = repo.get(automation["id"]) or {}
    assert row["enabled"] is False
    assert TITLE in (row["unavailable_reason"] or "")


def test_a_scope_covering_the_action_is_still_not_enough(peek) -> None:
    """The other half: consent recorded once is not permission held forever.

    With ``permission_mode`` at "ask" and no grant, the engine returns ``ask``. Nobody
    is there to answer, so the run is refused -- but the rule keeps its schedule,
    because granting the permission later must be all it takes to fix this.
    """
    automation = make(peek_action(), [NEEDED])

    result = engine.run_due(at=after(automation, 1))

    assert result.failed == 1, result
    assert peek == []
    row = repo.get(automation["id"]) or {}
    assert row["enabled"] is True, "an ungranted permission retired the rule"
    assert row["next_run_at"] is not None
    assert row["next_run_at"] > automation["next_run_at"]


# ------------------------------------------------------- the live-engine check


def test_a_granted_capability_lets_the_rule_run(peek) -> None:
    """Both checks satisfied, and the action actually happens.

    Without this the file would only prove things are refused, which is also what a
    gate that refuses everything would prove. ``peek`` records its call, so this is
    the assertion that the gate lets go as well as holds.
    """
    grant()
    automation = make(peek_action(), [NEEDED])

    result = engine.run_due(at=after(automation, 1))

    assert result.fired == 1, result
    assert len(peek) == 1, "granted on both counts and it still did not run"
    assert [r["status"] for r in repo.runs(automation["id"])] == ["completed"]
    assert repo.runs(automation["id"])[0]["denied_capability"] is None


def test_revoking_a_permission_stops_a_rule_that_was_working(peek) -> None:
    """What makes the Permissions screen true rather than decorative.

    A stored scope alone would mean revocation applied only to rules armed after it.
    The one thing a person expects from taking a permission away is that it stops
    being used -- including by whatever was already running on a timer.
    """
    grant()
    automation = make(peek_action(), [NEEDED])
    engine.run_due(at=after(automation, 1))
    assert len(peek) == 1

    permissions_store.revoke(NEEDED, "*")
    row = repo.get(automation["id"]) or {}
    later = engine.run_due(at=after(row, 1))

    assert later.failed == 1, later
    assert len(peek) == 1, "it kept reading files after the permission was revoked"


def test_an_explicit_denial_does_not_retire_the_rule(peek) -> None:
    """A denial is a decision the user can change, so the schedule survives it.

    The distinction that matters: disabling the rule here would mean un-denying a
    permission is not enough to get the reminder back -- the user would have to
    notice a rule was disarmed weeks ago and re-arm it.
    """
    permissions_store.upsert_grant(NEEDED, scope="*", grant_kind="deny")
    automation = make(peek_action(), [NEEDED])

    result = engine.run_due(at=after(automation, 1))

    assert result.failed == 1, result
    assert peek == []
    row = repo.get(automation["id"]) or {}
    assert row["enabled"] is True, "a changeable denial permanently retired the rule"
    assert row["next_run_at"] is not None


def test_a_hard_limit_is_permanent_because_no_grant_can_change_it(peek) -> None:
    """The one engine refusal that *should* retire the rule.

    A protected path is refused in code, above every grant and every setting, so this
    rule will be refused identically tomorrow. Rescheduling it would be a warning a
    day about an answer that cannot change -- which is the same hot-loop reasoning as
    an unimplementable action kind, reached from the permission side.
    """
    app_settings.set("permission_mode", "trusted")
    grant()
    automation = make(peek_action(PROTECTED), [NEEDED])

    result = engine.run_due(at=after(automation, 1))

    assert result.failed == 1, result
    assert peek == []
    row = repo.get(automation["id"]) or {}
    assert row["enabled"] is False, "a refusal no grant can lift was rescheduled"
    assert row["next_run_at"] is None
    assert row["unavailable_reason"]
    run = repo.runs(automation["id"])[0]
    assert run["denied_capability"] == NEEDED
    assert run["detail"] and "hard_limit" in str(run["detail"])


def test_the_action_is_scoped_on_its_own_parameters(peek) -> None:
    """The same capability, two paths, two answers.

    The gate hands the action's fields to the policy layer as params. If it passed
    nothing, ``check_hard_limits`` would find no path to object to and a rule aimed at
    a private key would be indistinguishable from one aimed at a text file.
    """
    app_settings.set("permission_mode", "trusted")
    grant()
    ok = make(peek_action(READABLE), [NEEDED])
    engine.run_due(at=after(ok, 1))
    assert len(peek) == 1, "a permitted path was refused"

    for row in repo.list_all():
        repo.delete(row["id"])
    blocked = make(peek_action(PROTECTED), [NEEDED])
    engine.run_due(at=after(blocked, 1))
    assert len(peek) == 1, "a protected path was read anyway"


# --------------------------------------------------------- refused at the door


def test_an_action_needing_more_than_the_scope_is_refused_on_save(client: TestClient) -> None:
    """Told while the user is still looking at the form.

    The fire-time gate would also catch this, hours later, and it would look like the
    automation broke rather than like it was never authorised. Both paths are honest;
    only one of them is useful.
    """
    r = client.post(BASE, json={"name": "peeker", "trigger_kind": "interval",
                                "schedule": "15m", "action": peek_action(),
                                "capability_scope": []})
    assert r.status_code == 400, r.text
    assert NEEDED in r.json()["detail"]
    assert repo.list_all() == [], "refused, but stored anyway"


def test_a_matching_scope_is_stored_in_one_canonical_form(client: TestClient) -> None:
    """Sorted and de-duplicated, because the fire-time check is a set comparison.

    Two rows granting the same powers in a different order must not read as different
    consent, and a repeated name must not read as more of it.
    """
    r = client.post(BASE, json={"name": "peeker", "trigger_kind": "interval",
                                "schedule": "15m", "action": peek_action(),
                                "capability_scope": [NEEDED, NEEDED]})
    assert r.status_code == 200, r.text
    assert r.json()["capability_scope"] == [NEEDED]
    assert (repo.get(r.json()["id"]) or {})["capability_scope"] == [NEEDED]


def test_a_scope_wider_than_the_action_is_refused(client: TestClient) -> None:
    """A rule recorded as allowed to do something it cannot do is a false statement.

    Nothing unsafe follows -- the gate only ever checks coverage -- but the Permissions
    and Automations screens would both be describing a power this rule does not have,
    and the disagreement is more likely a bug in whoever built the consent than a
    deliberate choice.
    """
    r = client.post(BASE, json={"name": "reminder", "trigger_kind": "interval",
                                "schedule": "15m", "action": {"kind": "notify", "message": "study"},
                                "capability_scope": [NEEDED]})
    assert r.status_code == 400, r.text
    assert NEEDED in r.json()["detail"]


def test_a_capability_that_does_not_exist_is_refused(client: TestClient) -> None:
    """Otherwise the rule is armed and permanently inert.

    ``evaluate`` denies an unknown capability as a hard limit, so a misspelled name
    stores a rule that fires once, disables itself, and reports a refusal that reads
    like NEXUS being locked down instead of like a typo.
    """
    r = client.post(BASE, json={"name": "typo", "trigger_kind": "interval",
                                "schedule": "15m", "action": peek_action(),
                                "capability_scope": ["file.raed"]})
    assert r.status_code == 400, r.text
    assert "file.raed" in r.json()["detail"]


def test_widening_an_action_without_renewing_consent_is_refused(client: TestClient) -> None:
    """The edit this column exists for.

    A reminder is saved needing nothing. Later its action is changed to one that reads
    files, and nothing else -- no new scope, and under trusted mode no prompt either.
    The check has to be against the row as it *will* be, not against either half as it
    arrives, or this is the request that slips through.
    """
    app_settings.set("permission_mode", "trusted")
    created = client.post(BASE, json={"name": "reminder", "trigger_kind": "interval",
                                      "schedule": "15m",
                                      "action": {"kind": "notify", "message": "study"}}).json()
    assert created["capability_scope"] == []

    r = client.patch(f"{BASE}/{created['id']}", json={"action": peek_action()})

    assert r.status_code == 400, r.text
    assert NEEDED in r.json()["detail"]
    assert (repo.get(created["id"]) or {})["action"]["kind"] == "notify", "widened anyway"


def test_widening_with_renewed_consent_is_allowed(client: TestClient) -> None:
    created = client.post(BASE, json={"name": "reminder", "trigger_kind": "interval",
                                      "schedule": "15m",
                                      "action": {"kind": "notify", "message": "study"}}).json()

    r = client.patch(f"{BASE}/{created['id']}",
                     json={"action": peek_action(), "capability_scope": [NEEDED]})

    assert r.status_code == 200, r.text
    assert r.json()["capability_scope"] == [NEEDED]
    assert r.json()["action"]["kind"] == "peek"


def test_editing_an_action_without_changing_its_powers_needs_no_new_consent(
    client: TestClient,
) -> None:
    """Re-consent is for a widened rule, not for a corrected typo.

    A mutation test found this gap: dropping the fall back to the stored scope left
    every PATCH that touched an action demanding fresh consent, and no test noticed,
    because all of them were about widening. Nagging on an unchanged set of powers is
    how a consent dialog becomes something people click through without reading, which
    costs more than it buys.
    """
    created = client.post(BASE, json={"name": "peeker", "trigger_kind": "interval",
                                      "schedule": "15m", "action": peek_action(READABLE),
                                      "capability_scope": [NEEDED]}).json()

    r = client.patch(f"{BASE}/{created['id']}",
                     json={"action": peek_action("/home/santosh/nexus-other-probe.txt")})

    assert r.status_code == 200, r.text
    assert r.json()["capability_scope"] == [NEEDED], "consent was reset by an edit"
    assert r.json()["action"]["path"].endswith("nexus-other-probe.txt")


def test_narrowing_a_scope_below_the_stored_action_is_refused(client: TestClient) -> None:
    """The mirror image: the action stays, the consent shrinks.

    Sending only a scope has to be checked against the action already on the row, or
    a rule could be recorded as consented to nothing while still doing everything.
    """
    created = client.post(BASE, json={"name": "peeker", "trigger_kind": "interval",
                                      "schedule": "15m", "action": peek_action(),
                                      "capability_scope": [NEEDED]}).json()

    r = client.patch(f"{BASE}/{created['id']}", json={"capability_scope": []})

    assert r.status_code == 400, r.text
    assert (repo.get(created["id"]) or {})["capability_scope"] == [NEEDED]


def test_the_list_reports_what_each_rule_was_allowed_to_do(client: TestClient) -> None:
    """The screen cannot show consent it is not sent."""
    client.post(BASE, json={"name": "peeker", "trigger_kind": "interval",
                            "schedule": "15m", "action": peek_action(),
                            "capability_scope": [NEEDED]})
    rows = client.get(BASE).json()["automations"]
    assert [r["capability_scope"] for r in rows] == [[NEEDED]]


def test_the_kinds_the_routes_validate_against_are_the_ones_the_engine_dispatches() -> None:
    """One source of truth for "what can NEXUS do".

    ``action_kinds()`` was a tuple built at import. Every route message and the form's
    own validation read it, while ``execute`` dispatched from the dict -- so a kind
    registered after import was rejected at the door and runnable underneath. The
    direction that matters is the other one: a kind removed from the registry but left
    in the snapshot would be accepted by the form and fail at fire time.
    """
    assert set(engine.action_kinds()) == set(engine._ACTIONS)
    assert "peek" in engine.action_kinds(), "the snapshot is back"


# ------------------------------------------------------------------- migration


def _apply(c: sqlite3.Connection, statements: tuple[str, ...]) -> None:
    for statement in statements:
        c.execute(statement)


def test_v4_leaves_every_existing_automation_armed_and_intact() -> None:
    """The upgrade a populated database has to survive.

    Reminders are the feature this project's own vision leads with, so a migration
    that disarmed them would be the worst possible regression: silent, and only
    noticeable as an evening where nothing happened. ``capability_scope`` defaults to
    ``'[]'`` and that is exactly right for every row that exists, because ``notify``
    declares no capabilities -- which is why
    :func:`test_notify_declares_no_capabilities_and_that_is_why_it_runs_unattended`
    is not a redundant assertion but this test's premise.

    Run against raw sqlite rather than the migrated test database, because what is
    being checked is whether the ALTER statements work on a table that already has
    rows in it -- which is the one condition the suite's own fresh database never has.
    """
    from nexus.db.schema import MIGRATIONS

    by_version = {m.version: m for m in MIGRATIONS}
    with tempfile.TemporaryDirectory(prefix="nexus-v4-") as tmp:
        c = sqlite3.connect(Path(tmp) / "old.db")
        c.row_factory = sqlite3.Row
        for version in (1, 2, 3):
            _apply(c, by_version[version].statements)

        c.execute(
            """INSERT INTO automations
               (id, name, trigger_kind, schedule, action, enabled, next_run_at,
                created_at, updated_at, rev)
               VALUES ('a1', 'study', 'schedule', '20:00',
                       '{"kind":"notify","message":"time to study"}', 1,
                       '2026-09-04T20:00:00', 'now', 'now', 1)"""
        )
        c.execute(
            """INSERT INTO automation_runs (id, automation_id, started_at, status, detail)
               VALUES ('r1', 'a1', 'then', 'completed', '{"summary":"time to study"}')"""
        )
        c.commit()

        _apply(c, by_version[4].statements)
        c.commit()

        row = dict(c.execute("SELECT * FROM automations WHERE id = 'a1'").fetchone())
        assert row["capability_scope"] == "[]"
        assert row["enabled"] == 1, "the upgrade disarmed an existing reminder"
        assert row["next_run_at"] == "2026-09-04T20:00:00", "the schedule was disturbed"
        assert row["action"] == '{"kind":"notify","message":"time to study"}'
        assert engine.scope_gap(
            {"kind": "notify", "message": "x"}, engine.normalise_scope([])
        ) == [], "the default scope does not cover the only action kind that exists"

        run = dict(c.execute("SELECT * FROM automation_runs WHERE id = 'r1'").fetchone())
        assert run["denied_capability"] is None
        assert run["status"] == "completed"
        c.close()
