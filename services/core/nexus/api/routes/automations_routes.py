"""The Automations surface (§31-§34, §47 Automations screen).

Reads and writes the ``automations`` table and exposes the engine's own state. The
engine is in :mod:`nexus.automations`; nothing here schedules or fires anything, so
a broken route cannot make a reminder go off at the wrong time.

Two things this module refuses to do, both of them the difference between a screen
that tells the truth and one that looks fine:

* **It never reports a rule as armed when it is not.** ``next_run_at`` and
  ``unavailable_reason`` come straight from the row. A rule whose schedule or action
  cannot work is stored disabled with a reason, and that reason is what the list
  shows -- rather than a toggle in the "on" position above something inert (§78).
* **It validates the action against the kinds that actually exist.** ``POST`` with
  an unimplementable action is rejected at the door with the available kinds named,
  instead of being accepted, stored, and disabled by the engine the first time it
  fires. Both paths end up honest; only one of them tells the user while they are
  still looking at the form.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from ... import automations as engine
from ...db.repos import automations as repo
from ...logging_setup import get_logger

router = APIRouter(prefix="/api/automations", tags=["automations"])
log = get_logger("api.automations")


def _validate_action(action: dict[str, Any] | None) -> dict[str, Any]:
    """Reject an action the engine could never run, naming what it can.

    Deliberately the same judgement the engine makes, reached earlier. It is not a
    duplicated rule so much as the same rule applied at the two moments it matters:
    here, so a typo is a form error; there, because a row can also arrive from a
    sync or an older version of this file.
    """
    if not isinstance(action, dict) or not action:
        raise HTTPException(
            status_code=400,
            detail="An automation needs an action. Available kinds: "
            + ", ".join(engine.action_kinds()),
        )
    kind = str(action.get("kind") or "").strip()
    if kind not in engine.action_kinds():
        raise HTTPException(
            status_code=400,
            detail=f"{kind or 'an action with no kind'} is not something NEXUS can "
            f"run. Available kinds: {', '.join(engine.action_kinds())}",
        )
    if kind == "notify" and not str(action.get("message") or "").strip():
        raise HTTPException(
            status_code=400, detail="A reminder needs a message to deliver."
        )
    return action


def _validate_scope(action: dict[str, Any], scope: object) -> None:
    """Reject a consent record that does not match the action it belongs to.

    ``capability_scope`` is what the user agreed this rule may do. It is never
    inferred from the action here, and that restraint is the whole point: filling it
    in automatically would turn a record of consent into a rubber stamp, and the
    check below into one that can never fail. So the caller states the scope, and
    this refuses the three ways it can disagree with reality:

    * a name no capability answers to -- otherwise the engine denies it as a hard
      limit and the rule is armed but permanently inert;
    * **less** than the action needs -- which is what a widened action looks like
      when the consent was not renewed, and the case this column exists for;
    * **more** than the action needs -- a rule recorded as authorised for something
      it cannot do, which is a false statement about the system even though nothing
      unsafe follows from it.

    A rule that arrives from a sync, or from an older version of this file, has not
    been through here. That is why :func:`nexus.automations._capability_gate` checks
    again at fire time and is the actual enforcement; this is the same judgement
    reached while the user is still looking at the form.
    """
    unknown = engine.unknown_capabilities(scope)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Not a permission NEXUS has: {', '.join(unknown)}.",
        )
    try:
        gap = engine.scope_gap(action, scope)
        surplus = engine.scope_surplus(action, scope)
    except engine.UnknownActionKind as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{exc or 'this action'} is not something NEXUS can run. "
            f"Available kinds: {', '.join(engine.action_kinds())}",
        ) from None
    if gap:
        raise HTTPException(
            status_code=400,
            detail="This action needs permission the rule was not given: "
            + ", ".join(gap)
            + ". Confirm it and save again.",
        )
    if surplus:
        raise HTTPException(
            status_code=400,
            detail="This rule does not need " + ", ".join(surplus) + ", so it "
            "should not be recorded as allowed to.",
        )


@router.get("")
def list_automations() -> dict[str, Any]:
    """Every rule, plus the engine's live state.

    Returned together because the list is not interpretable without it: a rule with
    a perfectly good schedule still never fires if the engine is not running, and a
    screen showing only the rows would have no way to say so.
    """
    return {
        "automations": repo.list_all(),
        "engine": engine.status(),
    }


@router.post("")
def create_automation(
    name: str = Body(..., embed=True),
    trigger_kind: str = Body("schedule", embed=True),
    schedule: str = Body("", embed=True),
    action: dict[str, Any] | None = Body(None, embed=True),
    enabled: bool = Body(True, embed=True),
    capability_scope: list[str] | None = Body(None, embed=True),
) -> dict[str, Any]:
    if not name.strip():
        raise HTTPException(status_code=400, detail="An automation needs a name.")
    _validate_action(action)
    _validate_scope(action or {}, capability_scope or [])
    try:
        created = repo.create(
            name.strip(),
            trigger_kind=trigger_kind,
            schedule=schedule,
            action=action,
            enabled=enabled,
            capability_scope=capability_scope,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    # ``create`` stores an unparseable schedule disabled with a reason rather than
    # raising, so a 201 here does not mean "this will fire". Reported rather than
    # smoothed over: the caller gets the row, including the reason, and the screen
    # can say what is wrong with it.
    if created.get("unavailable_reason"):
        log.info(
            "automation %r stored but disabled: %s",
            created["name"],
            created["unavailable_reason"],
        )
    return created


@router.get("/{automation_id}")
def get_automation(automation_id: str) -> dict[str, Any]:
    row = repo.get(automation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such automation.")
    row["runs"] = repo.runs(automation_id)
    return row


@router.patch("/{automation_id}")
def update_automation(
    automation_id: str,
    name: str | None = Body(None, embed=True),
    trigger_kind: str | None = Body(None, embed=True),
    schedule: str | None = Body(None, embed=True),
    action: dict[str, Any] | None = Body(None, embed=True),
    enabled: bool | None = Body(None, embed=True),
    capability_scope: list[str] | None = Body(None, embed=True),
) -> dict[str, Any]:
    if action is not None:
        _validate_action(action)
    if action is not None or capability_scope is not None:
        # Checked against the row as it will be, not as either half arrives: editing
        # only the action is exactly how a rule comes to need more than it was
        # granted, and that is the edit this refuses.
        current = repo.get(automation_id)
        if current is None:
            raise HTTPException(status_code=404, detail="No such automation.")
        _validate_scope(
            action if action is not None else current["action"],
            capability_scope
            if capability_scope is not None
            else current["capability_scope"],
        )
    try:
        updated = repo.update(
            automation_id,
            name=name,
            trigger_kind=trigger_kind,
            schedule=schedule,
            action=action,
            enabled=enabled,
            capability_scope=capability_scope,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if updated is None:
        raise HTTPException(status_code=404, detail="No such automation.")
    return updated


@router.delete("/{automation_id}")
def delete_automation(automation_id: str) -> dict[str, Any]:
    if not repo.delete(automation_id):
        raise HTTPException(status_code=404, detail="No such automation.")
    return {"deleted": automation_id}


@router.post("/{automation_id}/run")
def run_now(automation_id: str) -> dict[str, Any]:
    """Fire one rule immediately, without waiting for its schedule.

    This is what makes a ``manual`` trigger usable at all, and it is also the only
    way to find out that a rule works before the evening it is meant to run. It
    writes a real ``automation_runs`` row like any other fire -- a test fire that
    left no trace would make "last run" a lie by omission.

    ``reschedule=False``, because running early must not move the rule's own next
    occurrence: checking a reminder works at 3pm should not cancel it at 8pm.
    """
    automation = repo.get(automation_id)
    if automation is None:
        raise HTTPException(status_code=404, detail="No such automation.")

    run_id = repo.start_run(automation_id)
    outcome = engine.execute(automation, attended=True)
    detail = dict(outcome.detail)
    detail["summary"] = outcome.summary
    detail["manual"] = True
    repo.finish_run(
        run_id,
        automation_id,
        status=outcome.status,
        detail=detail,
        reschedule=False,
        denied_capability=outcome.denied_capability,
    )
    return {
        "status": outcome.status,
        "summary": outcome.summary,
        "detail": detail,
        "automation": repo.get(automation_id),
    }


@router.get("/{automation_id}/runs")
def automation_runs(automation_id: str, limit: int = 20) -> dict[str, Any]:
    if repo.get(automation_id) is None:
        raise HTTPException(status_code=404, detail="No such automation.")
    return {"runs": repo.runs(automation_id, limit=limit)}
