"""Automations as tools, so the assistant can actually set one (§21, §34, §53).

The system-control audit's gap 3. Automations existed, worked, survived restart and
had a screen -- and were reachable only through that screen. So *"every evening at
seven, remind me to study"* was a request the assistant could discuss intelligently
and not carry out: it would describe the automation it was going to make, make
nothing, and the user would discover next week that nothing had been scheduled.
That is the same "fake capability" shape §78 forbids, wearing a politer costume
than usual, and it is why memory had to become a tool for exactly the same reason.

Four things here are deliberate.

**Creating is medium risk, reading is low.** An automation is consent that *fires
later*. A memory written wrongly is a sentence the user corrects next time they see
it; a rule written wrongly runs on its own schedule whether or not anyone is
watching. Reading the list, by contrast, is reading the user's own schedule --
refusing to show it while happily writing to it would be the wrong way round.

**A rule the model makes cannot outrank the model.** ``capability_scope`` records
what a rule was armed with, and the engine re-checks it at every firing (schema
v4), so an automation is never a way to launder a power past the permission engine
by scheduling it. These tools pass the scope through explicitly rather than letting
it default to "whatever is allowed at fire time".

**Deleting resolves before it destroys.** ``automation.delete`` takes an id, never
a description. "Cancel my reminders" could mean one rule or nine, and a
natural-language guess is not a mandate to delete -- the same rule
``memory.forget`` follows. The model is expected to call ``automation.list``
first and name what it found.

**Every write is verified by reading the row back.** A schedule NEXUS cannot parse
is stored *disabled and labelled* rather than silently never firing, so the
verifier checks what the database actually holds -- including that an unparseable
schedule reports itself as unavailable instead of as success.
"""

from __future__ import annotations

from typing import Any

from ..db.repos import automations as repo
from ..logging_setup import get_logger
from .registry import ToolResult, tool

log = get_logger("tools.automations")

#: What the model may put in an automation's action. Today the engine performs
#: exactly one kind, and the enum says so rather than accepting an ``action``
#: dict the engine would ignore -- a rule that stores an action nobody runs is a
#: rule that looks armed and does nothing.
ACTION_KINDS = ("notify",)


def _summarise(row: dict[str, Any]) -> str:
    """One line a person can check the rule against."""
    when = row.get("schedule") or row.get("trigger_kind") or "manually"
    state = "armed" if row.get("enabled") else "disabled"
    tail = ""
    if row.get("unavailable_reason"):
        tail = f" -- {row['unavailable_reason']}"
        state = "disabled"
    return f"'{row.get('name')}' is {state}, {when}{tail}"


def _verify_created(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    """Read the stored row back, and do not call a disabled rule a success.

    The repository stores an unparseable schedule disabled-and-labelled rather than
    rejecting it, which is the right call -- it keeps the user's intent visible
    instead of losing it -- but it means "the row exists" is not the same as "the
    automation will run". This checks both.
    """
    if not result.ok or not isinstance(result.data, dict):
        return False, "No automation was returned to check."
    row = repo.get(str(result.data.get("id") or ""))
    if row is None:
        return False, "The automation was not found in the database afterwards."
    if row.get("unavailable_reason"):
        return False, (
            f"Stored, but it will not run: {row['unavailable_reason']}"
        )
    if not row.get("enabled"):
        return False, "Stored, but disabled, so it will not fire."
    if not row.get("next_run_at") and row.get("trigger_kind") != "manual":
        return False, "Stored and enabled, but with no next run time."
    return True, f"Read back from the database: {_summarise(row)}."


def _verify_absent(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    automation_id = str(params.get("automation_id") or "")
    if repo.get(automation_id) is not None:
        return False, "The automation is still in the database."
    return True, "Confirmed gone: the automation is no longer in the database."


def _verify_enabled_state(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    row = repo.get(str(params.get("automation_id") or ""))
    if row is None:
        return False, "The automation was not found afterwards."
    wanted = bool(params.get("enabled"))
    if bool(row.get("enabled")) != wanted:
        return False, (
            f"The stored rule is {'armed' if row.get('enabled') else 'disabled'}, "
            f"which is not what was asked for."
        )
    return True, f"Read back from the database: {_summarise(row)}."


@tool(
    "automation.list",
    title="List automations",
    description=(
        "List the automations that exist, with their schedules, whether each is "
        "armed, and why any of them cannot run. Call this before changing or "
        "deleting one, so you refer to a real id rather than guessing."
    ),
    capability="automation.read",
    idempotent=True,
    parameters={"type": "object", "properties": {}},
)
def automation_list(params: dict[str, Any]) -> ToolResult:
    rows = repo.list_all()
    return ToolResult(
        ok=True,
        data={"automations": rows, "count": len(rows)},
        summary=(
            "No automations are set up."
            if not rows
            else f"{len(rows)} automation(s): "
            + "; ".join(_summarise(row) for row in rows[:5])
        ),
    )


@tool(
    "automation.create",
    title="Create an automation",
    description=(
        "Create a rule that fires on a schedule. Use this when the user asks for "
        "something recurring -- 'every evening at seven, remind me to study', "
        "'every twenty minutes, tell me to look away from the screen'. For a one-off "
        "thing you are doing right now, just do it instead. The rule can only notify; "
        "it cannot run other tools, and saying otherwise would be a promise NEXUS "
        "cannot keep."
    ),
    capability="automation.write",
    idempotent=False,
    verifier=_verify_created,
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "A short name the user will recognise in the list.",
            },
            "trigger_kind": {
                "type": "string",
                "enum": list(repo.TRIGGER_KINDS),
                "description": (
                    "'schedule' for a clock time, 'interval' for a repeating gap, "
                    "'manual' for a rule the user runs by hand."
                ),
            },
            "schedule": {
                "type": "string",
                # Spelled out exactly, because the engine's parser is deliberately
                # small and anything it cannot read is stored *disabled*. A
                # description that implied cron would make every rule the model
                # created arrive dead on the Automations screen.
                "description": (
                    "For 'schedule': a 24-hour local time like '19:00', optionally "
                    "with weekdays first, like 'mon,wed,fri 19:00'. Cron expressions "
                    "are not supported. For 'interval': a gap like '15m', '2h' or "
                    "'90s', minimum 30 seconds. Leave empty for 'manual'."
                ),
            },
            "message": {
                "type": "string",
                "description": "The notification text the user will see when it fires.",
            },
        },
        "required": ["name", "message"],
    },
)
def automation_create(params: dict[str, Any]) -> ToolResult:
    name = str(params.get("name") or "").strip()
    message = str(params.get("message") or "").strip()
    if not name:
        return ToolResult(ok=False, error="An automation needs a name.")
    if not message:
        return ToolResult(
            ok=False,
            error=(
                "An automation needs the message it will show. A rule that fires "
                "and says nothing is indistinguishable from one that never fired."
            ),
        )

    trigger_kind = str(params.get("trigger_kind") or "schedule").strip()
    if trigger_kind not in repo.TRIGGER_KINDS:
        return ToolResult(
            ok=False,
            error=f"trigger_kind must be one of {', '.join(repo.TRIGGER_KINDS)}.",
        )
    schedule = str(params.get("schedule") or "").strip()
    if trigger_kind != "manual" and not schedule:
        return ToolResult(
            ok=False,
            error=(
                f"A '{trigger_kind}' automation needs a schedule. Without one there "
                "is no time at which it would ever run."
            ),
        )

    try:
        row = repo.create(
            name,
            trigger_kind=trigger_kind,
            schedule=schedule,
            action={"kind": "notify", "message": message},
            # The rule is armed with exactly the power it needs to notify, and
            # nothing else. Passing the scope explicitly is what stops a scheduled
            # rule becoming a way to hold a capability its author never had.
            capability_scope=["system.notify"],
        )
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))

    if row.get("unavailable_reason"):
        # Stored, visible, and honest about not working -- which is better than
        # either losing the user's intent or pretending it is armed.
        return ToolResult(
            ok=True,
            data=row,
            summary=(
                f"Saved '{name}', but it is disabled because the schedule could not "
                f"be read: {row['unavailable_reason']}"
            ),
        )
    return ToolResult(ok=True, data=row, summary=f"Created: {_summarise(row)}.")


@tool(
    "automation.set_enabled",
    title="Arm or disable an automation",
    description=(
        "Arm or disable an existing automation without deleting it. Use this for "
        "'pause my reminders' -- disabling keeps the rule so it can be turned back "
        "on, which deleting does not. Get the id from automation.list first."
    ),
    capability="automation.write",
    idempotent=True,
    verifier=_verify_enabled_state,
    parameters={
        "type": "object",
        "properties": {
            "automation_id": {
                "type": "string",
                "description": "The id from automation.list.",
            },
            "enabled": {
                "type": "boolean",
                "description": "True to arm it, false to disable it.",
            },
        },
        "required": ["automation_id", "enabled"],
    },
)
def automation_set_enabled(params: dict[str, Any]) -> ToolResult:
    automation_id = str(params.get("automation_id") or "").strip()
    if not automation_id:
        return ToolResult(ok=False, error="Which automation? Call automation.list first.")
    if repo.get(automation_id) is None:
        return ToolResult(
            ok=False,
            error=(
                f"No automation with id '{automation_id}'. Call automation.list to "
                "see what exists rather than guessing an id."
            ),
        )
    wanted = bool(params.get("enabled"))
    row = repo.update(automation_id, enabled=wanted)
    if row is None:  # pragma: no cover - the existence check above covers this
        return ToolResult(ok=False, error="The automation could not be updated.")
    return ToolResult(
        ok=True,
        data=row,
        summary=f"{'Armed' if wanted else 'Disabled'}: {_summarise(row)}.",
    )


@tool(
    "automation.delete",
    title="Delete an automation",
    description=(
        "Permanently delete an automation by id. Prefer automation.set_enabled for "
        "'stop reminding me', since that is reversible and this is not. Never guess "
        "an id: call automation.list and name what you found, because a description "
        "like 'my reminders' could match one rule or nine."
    ),
    capability="automation.write",
    idempotent=True,
    verifier=_verify_absent,
    parameters={
        "type": "object",
        "properties": {
            "automation_id": {
                "type": "string",
                "description": "The exact id from automation.list.",
            }
        },
        "required": ["automation_id"],
    },
)
def automation_delete(params: dict[str, Any]) -> ToolResult:
    automation_id = str(params.get("automation_id") or "").strip()
    if not automation_id:
        return ToolResult(ok=False, error="Which automation? Call automation.list first.")
    existing = repo.get(automation_id)
    if existing is None:
        return ToolResult(
            ok=False,
            error=(
                f"No automation with id '{automation_id}'. Call automation.list to "
                "see what exists rather than guessing an id."
            ),
        )
    repo.delete(automation_id)
    return ToolResult(
        ok=True,
        data={"id": automation_id, "name": existing.get("name")},
        summary=f"Deleted '{existing.get('name')}'.",
    )


__all__ = [
    "automation_list",
    "automation_create",
    "automation_set_enabled",
    "automation_delete",
]
