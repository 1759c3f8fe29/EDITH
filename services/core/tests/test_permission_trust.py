"""Trusted mode: what "never ask" does and does not mean.

Turning off the questions is the kind of change that is easy to get subtly wrong in
the dangerous direction, so the point of this file is the *negative* half. Trusted
mode must move exactly one line — whether a decision is put to the user — and leave
every refusal above it standing: the hard limits (which no setting can reach), a
capability switched off in Settings, and a scope the user explicitly denied.

The engine is the real one. Nothing is stubbed: these are the real capability
catalogue, the real policy rules and the real store.
"""
from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable

import pytest

from nexus.db.repos import activity, app_settings, permissions_store
from nexus.permissions.engine import PermissionEngine

HOME_FILE = {"path": "/home/santosh/nexus-trust-probe.txt", "content": "hi"}


def sync(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


@pytest.fixture(autouse=True)
def permission_settings_restored():
    keys = ("permission_mode", "confirm_destructive", "terminal_allowed")
    before = {key: app_settings.get(key) for key in keys}
    yield
    for key, value in before.items():
        app_settings.set(key, value)


@pytest.fixture
def engine() -> PermissionEngine:
    app_settings.set("permission_mode", "trusted")
    app_settings.set("confirm_destructive", False)
    app_settings.set("terminal_allowed", True)
    return PermissionEngine()


# ------------------------------------------------------------------ it does ask nothing


@pytest.mark.parametrize("capability,params", [
    ("file.write", HOME_FILE),
    ("file.delete", {"path": HOME_FILE["path"]}),
    ("input.type", {"text": "hello"}),
    ("system.power", {"action": "shutdown"}),
    ("terminal.execute", {"command": "apt list --installed"}),
])
def test_trusted_allows_what_would_have_been_asked(engine, capability, params) -> None:
    decision = engine.evaluate(capability, params)

    assert decision.outcome == "allow", decision.reason
    # The source is how the Permissions screen and the audit log can tell standing
    # consent from a grant the user actually clicked. Collapsing them would make the
    # log unable to answer "did I agree to this, or did the setting?"
    assert decision.source == "trusted"
    assert "trusted" in decision.reason


def test_ask_mode_puts_the_question_back(engine) -> None:
    app_settings.set("permission_mode", "ask")

    decision = engine.evaluate("file.write", HOME_FILE)

    assert decision.outcome == "ask"
    assert decision.source == "prompt"


# ------------------------------------------------------- and it lowers nothing else


@pytest.mark.parametrize("capability,params,because", [
    ("file.write", {"path": "/etc/passwd", "content": "x"}, "system files"),
    ("file.read", {"path": "/home/santosh/.ssh/id_rsa"}, "credentials or private keys"),
    ("terminal.execute", {"command": "rm -rf ~"}, "recursively delete"),
    ("terminal.execute", {"command": "rm -rf /"}, "recursive delet"),
])
def test_a_hard_limit_is_not_a_question_so_trust_cannot_reach_it(
    engine, capability, params, because
) -> None:
    decision = engine.evaluate(capability, params)

    assert decision.outcome == "deny", f"trusted mode must not unlock this: {decision.reason}"
    assert decision.source == "hard_limit"
    assert because in (decision.reason + " " + (decision.detail or "")).lower()


def test_a_capability_switched_off_in_settings_stays_off(engine) -> None:
    app_settings.set("terminal_allowed", False)

    decision = engine.evaluate("terminal.execute", {"command": "echo hi"})

    assert decision.outcome == "deny"
    assert decision.source == "disabled", "the toggle is authoritative, not advisory"


def test_a_scope_the_user_denied_stays_denied(engine) -> None:
    permissions_store.upsert_grant("notify.send", scope="*", grant_kind="deny")
    try:
        decision = engine.evaluate("notify.send", {"message": "hi"})

        assert decision.outcome == "deny"
        assert decision.source == "stored", \
            "a decision the user made by hand outranks a blanket setting"
    finally:
        permissions_store.revoke("notify.send", "*")


# ------------------------------------------------------------- the destructive exception


@pytest.mark.parametrize("capability,params", [
    ("file.delete", {"path": HOME_FILE["path"]}),
    ("system.power", {"action": "shutdown"}),
])
def test_confirm_destructive_brings_back_one_question(engine, capability, params) -> None:
    app_settings.set("confirm_destructive", True)

    assert engine.evaluate(capability, params).outcome == "ask"


def test_but_only_for_the_destructive_ones(engine) -> None:
    app_settings.set("confirm_destructive", True)

    # Reading the clipboard is a privacy decision, not a destructive one, so this is
    # still covered by trust. If this ever flips to "ask", the destructive test above
    # stops proving anything.
    assert engine.evaluate("clipboard.read", {}).outcome == "allow"
    assert engine.evaluate("screen.capture", {}).outcome == "allow"


# ------------------------------------------------------------------------ still recorded


@sync
async def test_not_asking_is_not_the_same_as_leaving_no_trace(engine) -> None:
    before = len(activity.list_recent(kinds=("permission",), limit=50))

    decision = await engine.require("file.write", HOME_FILE)

    assert decision.outcome == "allow"
    entries = activity.list_recent(kinds=("permission",), limit=50)
    assert len(entries) > before, "a decision nobody was asked about must still be reviewable"
    latest = entries[0]
    assert latest["detail"]["source"] == "trusted"
    assert latest["detail"]["capability"] == "file.write"
