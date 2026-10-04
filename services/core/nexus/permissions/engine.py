"""The permission engine (task C3; spec §5-§8, §35, §54).

This sits between AI intent and OS execution. Nothing gets through it implicitly.

The contract, in order:

1. **Hard limits first.** ``policy.check_hard_limits`` can refuse outright. No
   stored grant, and no user click, overrides that.
2. **Stored decisions second.** A matching ``deny`` refuses; a matching ``always``
   or unexpired ``session`` allows. Absence of a row is *not* permission.
3. **Otherwise ask.** ``request`` publishes a permission request and waits for a
   real answer. If nobody answers within the timeout it resolves to **deny** --
   failing closed, because "the user did not respond" is not consent (§5).

Session grants live in this process's memory, never in the database, so a restart
genuinely clears them rather than relying on an expiry timestamp being honoured.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from ..core.events import bus
from ..db.repos import activity, app_settings, permissions_store
from ..logging_setup import get_logger
from . import capabilities, policy

log = get_logger("permissions")

Outcome = Literal["allow", "deny", "ask"]

# How long a permission request waits for an answer before failing closed.
DEFAULT_REQUEST_TIMEOUT_S = 180.0

#: Completes the sentence in an implicit allow's reason, so the Activity screen
#: explains the absence of a prompt rather than leaving it unexplained.
_IMPLICIT_BECAUSE = "it reads this computer's own state and no personal content"


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    capability: str
    scope: str
    reason: str
    # Where the decision came from: hard_limit | stored | session | user | timeout
    # | disabled | implicit. Recorded so the Activity screen can show *why*, not
    # just what.
    source: str
    detail: str = ""
    constraints: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.outcome == "allow"

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "capability": self.capability,
            "scope": self.scope,
            "reason": self.reason,
            "source": self.source,
            "detail": self.detail,
            "constraints": self.constraints,
        }


@dataclass
class PendingRequest:
    id: str
    capability: str
    scope: str
    params: dict[str, Any]
    description: str
    future: asyncio.Future[Decision]
    created_at: str
    task_id: str | None = None
    conversation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        cap = capabilities.get(self.capability)
        return {
            "id": self.id,
            "capability": self.capability,
            "scope": self.scope,
            "description": self.description,
            "title": cap.title,
            "group": cap.group,
            "risk": cap.risk,
            "reversible": cap.reversible,
            "consequence": cap.consequence,
            "grant_kinds": capabilities.offered_grant_kinds(self.capability),
            "second_confirmation": capabilities.requires_second_confirmation(self.capability),
            "destructive": _is_destructive(self.capability, self.params),
            "params": self.params,
            "task_id": self.task_id,
            "conversation_id": self.conversation_id,
            "created_at": self.created_at,
        }


class PermissionEngine:
    def __init__(self) -> None:
        # (capability, scope) pairs granted for this process only.
        self._session_grants: set[tuple[str, str]] = set()
        self._pending: dict[str, PendingRequest] = {}

    # ------------------------------------------------------------- evaluation

    def evaluate(self, capability: str, params: dict[str, Any] | None = None) -> Decision:
        """Decide without side effects and without asking.

        Returns ``ask`` when a user decision is needed. Callers that can prompt
        should use ``require``; callers that only want to know (for example, to
        grey out a control) use this.
        """
        params = params or {}
        if not capabilities.exists(capability):
            return Decision(
                outcome="deny",
                capability=capability,
                scope="*",
                reason="Unknown capability.",
                source="hard_limit",
                detail=f"{capability!r} is not a capability NEXUS has.",
            )

        cap = capabilities.get(capability)
        scope = policy.scope_for(capability, params)

        refusal = policy.check_hard_limits(capability, params)
        if refusal is not None:
            return Decision(
                outcome="deny",
                capability=capability,
                scope=scope,
                reason=refusal.reason,
                source="hard_limit",
                detail=refusal.detail,
            )

        # A capability the user switched off in Settings is refused before any
        # grant lookup, so the toggle is authoritative.
        disabled = self._disabled_reason(capability)
        if disabled:
            return Decision(
                outcome="deny",
                capability=capability,
                scope=scope,
                reason=disabled,
                source="disabled",
                detail="Enable it in Settings if you want NEXUS to be able to do this.",
            )

        stored = permissions_store.find_grant(capability, scope)
        if stored is not None:
            if stored["grant_kind"] == "deny":
                return Decision(
                    outcome="deny",
                    capability=capability,
                    scope=stored["scope"],
                    reason=f"You previously denied {cap.title.lower()} for {stored['scope']}.",
                    source="stored",
                    detail="You can change this on the Permissions screen.",
                )
            if stored["grant_kind"] in ("always", "constraint", "constrained"):
                violation = self._constraint_violation(stored, capability, params)
                if violation:
                    return Decision(
                        outcome="deny",
                        capability=capability,
                        scope=stored["scope"],
                        reason=violation,
                        source="stored",
                        detail="The stored grant is narrower than this request.",
                    )
                return Decision(
                    outcome="allow",
                    capability=capability,
                    scope=stored["scope"],
                    reason=f"You granted {cap.title.lower()} for {stored['scope']}.",
                    source="stored",
                    constraints=stored.get("constraints") or {},
                )

        if self._has_session_grant(capability, scope):
            return Decision(
                outcome="allow",
                capability=capability,
                scope=scope,
                reason=f"Granted for this session ({scope}).",
                source="session",
            )

        # Last, so that a hard limit, a Settings toggle, or an explicit denial
        # all still override it: the exemption is "we would not have asked", not
        # "the user cannot refuse".
        if cap.implicit:
            return Decision(
                outcome="allow",
                capability=capability,
                scope=scope,
                reason=f"{cap.title} without asking, because {_IMPLICIT_BECAUSE}.",
                source="implicit",
                detail=cap.consequence,
            )

        # Trusted mode: the user has said, in Settings, that NEXUS may act without
        # stopping to ask. Placed *here*, dead last, so it changes exactly one thing --
        # whether a question is asked -- and nothing about what is allowed. Every
        # refusal above still refuses: a hard limit (a protected path, a destructive
        # command), a capability switched off in Settings, a scope the user explicitly
        # denied. Trust is not a bypass; it is standing consent to the questions that
        # would have been asked, and the audit log still records each decision, which
        # is what makes it reviewable afterwards instead of invisible.
        if app_settings.get("permission_mode", "ask") == "trusted":
            # One exception, and it has its own switch: something destructive and
            # irreversible still gets asked while ``confirm_destructive`` is on.
            # Standing consent to "open the browser" is not standing consent to
            # ``rm -rf``, and the two are told apart by consequence rather than by
            # wording (see ``_is_destructive``). Turn that setting off too -- it is off
            # by default -- and even this asks nothing.
            destructive = app_settings.get("confirm_destructive", False) and _is_destructive(
                capability, params
            )
            if not destructive:
                return Decision(
                    outcome="allow",
                    capability=capability,
                    scope=scope,
                    reason=f"{cap.title} without asking: permissions are set to trusted.",
                    source="trusted",
                    detail=cap.consequence,
                )

        return Decision(
            outcome="ask",
            capability=capability,
            scope=scope,
            reason=policy.describe_request(capability, params),
            source="prompt",
            detail=cap.consequence,
        )

    def _has_session_grant(self, capability: str, scope: str) -> bool:
        if (capability, "*") in self._session_grants:
            return True
        if (capability, scope) in self._session_grants:
            return True
        # A session grant on a parent directory covers paths inside it.
        for granted_cap, granted_scope in self._session_grants:
            if granted_cap != capability or not granted_scope.startswith(("/", "~")):
                continue
            if scope.startswith(("/", "~")) and policy.path_in_scope(scope, granted_scope):
                return True
        return False

    def _constraint_violation(
        self, stored: dict[str, Any], capability: str, params: dict[str, Any]
    ) -> str | None:
        """Enforce a constrained grant (§7 'constrained' grant kind)."""
        constraints = stored.get("constraints") or {}
        if not constraints:
            return None

        max_bytes = constraints.get("max_bytes")
        if max_bytes and capability == "file.write":
            content = params.get("content") or ""
            if len(str(content).encode("utf-8")) > int(max_bytes):
                return f"This grant is limited to {max_bytes} bytes per write."

        allowed_paths = constraints.get("paths")
        if allowed_paths and (target := params.get("path")):
            if not any(policy.path_in_scope(str(target), p) for p in allowed_paths):
                return "This grant only covers specific folders, and this path is not one of them."

        allowed_commands = constraints.get("commands")
        if allowed_commands and capability == "terminal.execute":
            program = policy.scope_for_command(str(params.get("command", "")))
            if program not in allowed_commands:
                return f"This grant only covers: {', '.join(allowed_commands)}."

        allowed_hosts = constraints.get("hosts")
        if allowed_hosts and (url := params.get("url")):
            if policy.scope_for_url(str(url)) not in allowed_hosts:
                return "This grant only covers specific websites."

        return None

    def _disabled_reason(self, capability: str) -> str | None:
        cfg = app_settings.get_all()
        if capability == "terminal.execute" and not cfg.get("terminal_allowed", False):
            return "Terminal access is turned off."
        if capability.startswith("memory.") and not cfg.get("memory_enabled", True):
            return "Memory is turned off."
        return None

    # ---------------------------------------------------------------- asking

    async def require(
        self,
        capability: str,
        params: dict[str, Any] | None = None,
        *,
        task_id: str | None = None,
        conversation_id: str | None = None,
        timeout: float | None = None,
    ) -> Decision:
        """Get a final allow/deny, prompting the user if necessary."""
        params = params or {}
        decision = self.evaluate(capability, params)
        if decision.outcome != "ask":
            self._audit(decision, params, task_id=task_id, conversation_id=conversation_id)
            return decision

        request = PendingRequest(
            id=uuid.uuid4().hex,
            capability=capability,
            scope=decision.scope,
            params=params,
            description=decision.reason,
            future=asyncio.get_running_loop().create_future(),
            created_at=datetime.now(timezone.utc).isoformat(),
            task_id=task_id,
            conversation_id=conversation_id,
        )
        self._pending[request.id] = request

        bus.publish("permission.requested", request.to_dict())
        activity.record(
            "permission",
            f"Asked to {request.description}",
            detail={"capability": capability, "scope": decision.scope, "request_id": request.id},
            level="info",
            task_id=task_id,
            conversation_id=conversation_id,
        )

        wait_for = timeout if timeout is not None else DEFAULT_REQUEST_TIMEOUT_S
        try:
            answered = await asyncio.wait_for(asyncio.shield(request.future), timeout=wait_for)
        except asyncio.TimeoutError:
            answered = Decision(
                outcome="deny",
                capability=capability,
                scope=decision.scope,
                reason="No answer, so nothing was done.",
                source="timeout",
                detail=(
                    f"NEXUS waited {int(wait_for)} seconds for you to approve "
                    f"'{request.description}' and stopped rather than assume yes."
                ),
            )
            bus.publish("permission.timeout", {"id": request.id, "capability": capability})
        except asyncio.CancelledError:
            answered = Decision(
                outcome="deny",
                capability=capability,
                scope=decision.scope,
                reason="Cancelled before it ran.",
                source="cancelled",
            )
            bus.publish("permission.cancelled", {"id": request.id, "capability": capability})
            raise
        finally:
            self._pending.pop(request.id, None)

        self._audit(answered, params, task_id=task_id, conversation_id=conversation_id)
        return answered

    def respond(
        self,
        request_id: str,
        choice: str,
        *,
        constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Deliver the user's answer.

        ``choice`` is one of: deny, deny_always, once, session, always,
        constrained.
        """
        request = self._pending.get(request_id)
        if request is None:
            # Already answered, timed out, or the caller went away. Saying so is
            # better than pretending the click worked (§79).
            return {
                "ok": False,
                "reason": "That permission request is no longer waiting for an answer.",
            }

        offered = capabilities.offered_grant_kinds(request.capability)
        if choice in ("once", "session", "always", "constrained") and choice not in offered:
            return {
                "ok": False,
                "reason": (
                    f"'{choice}' is not offered for {request.capability} "
                    f"(risk {capabilities.get(request.capability).risk}). "
                    f"Allowed: {', '.join(offered)}."
                ),
            }

        cap = capabilities.get(request.capability)
        decision: Decision

        if choice == "deny":
            decision = Decision(
                outcome="deny",
                capability=request.capability,
                scope=request.scope,
                reason="You declined.",
                source="user",
            )
        elif choice == "deny_always":
            permissions_store.upsert_grant(
                request.capability, scope=request.scope, grant_kind="deny"
            )
            decision = Decision(
                outcome="deny",
                capability=request.capability,
                scope=request.scope,
                reason=f"You declined, and {cap.title.lower()} is now blocked for {request.scope}.",
                source="user",
            )
        elif choice == "once":
            decision = Decision(
                outcome="allow",
                capability=request.capability,
                scope=request.scope,
                reason="You allowed it once.",
                source="user",
            )
        elif choice == "session":
            self._session_grants.add((request.capability, request.scope))
            decision = Decision(
                outcome="allow",
                capability=request.capability,
                scope=request.scope,
                reason=f"Allowed for this session ({request.scope}).",
                source="user",
            )
        elif choice == "always":
            permissions_store.upsert_grant(
                request.capability, scope=request.scope, grant_kind="always"
            )
            decision = Decision(
                outcome="allow",
                capability=request.capability,
                scope=request.scope,
                reason=f"Allowed, and remembered for {request.scope}.",
                source="user",
            )
        elif choice == "constrained":
            if not constraints:
                return {
                    "ok": False,
                    "reason": "A constrained grant needs the limits it should apply.",
                }
            permissions_store.upsert_grant(
                request.capability,
                scope=request.scope,
                grant_kind="constrained",
                constraints=constraints,
            )
            decision = Decision(
                outcome="allow",
                capability=request.capability,
                scope=request.scope,
                reason=f"Allowed within limits for {request.scope}.",
                source="user",
                constraints=constraints,
            )
        else:
            return {"ok": False, "reason": f"Unknown choice {choice!r}."}

        if not request.future.done():
            request.future.set_result(decision)
        self._pending.pop(request_id, None)
        bus.publish(
            "permission.resolved",
            {"id": request_id, "choice": choice, **decision.to_dict()},
        )
        return {"ok": True, "decision": decision.to_dict()}

    # ------------------------------------------------------------ inspection

    def pending(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self._pending.values()]

    def cancel_pending(self, request_id: str) -> bool:
        """Withdraw a request (its task was cancelled, or the UI closed)."""
        request = self._pending.pop(request_id, None)
        if request is None:
            return False
        if not request.future.done():
            request.future.set_result(
                Decision(
                    outcome="deny",
                    capability=request.capability,
                    scope=request.scope,
                    reason="The request was withdrawn before you answered.",
                    source="cancelled",
                )
            )
        bus.publish("permission.cancelled", {"id": request_id})
        return True

    def cancel_all_for_task(self, task_id: str) -> int:
        ids = [r.id for r in self._pending.values() if r.task_id == task_id]
        return sum(1 for rid in ids if self.cancel_pending(rid))

    def session_grants(self) -> list[dict[str, str]]:
        return [
            {"capability": cap, "scope": scope}
            for cap, scope in sorted(self._session_grants)
        ]

    def clear_session(self) -> int:
        count = len(self._session_grants)
        self._session_grants.clear()
        bus.publish("permission.session_cleared", {"cleared": count})
        return count

    def revoke(self, capability: str, scope: str = "*") -> bool:
        self._session_grants.discard((capability, scope))
        removed = permissions_store.revoke(capability, scope)
        bus.publish("permission.revoked", {"capability": capability, "scope": scope})
        activity.record(
            "permission",
            f"Revoked {capability} for {scope}",
            detail={"capability": capability, "scope": scope},
            level="warning",
        )
        return removed

    def revoke_all(self) -> dict[str, int]:
        session = self.clear_session()
        stored = permissions_store.revoke_all()
        activity.record(
            "permission",
            "Revoked every permission",
            detail={"stored": stored, "session": session},
            level="warning",
        )
        return {"stored": stored, "session": session}

    def overview(self) -> dict[str, Any]:
        """Everything the Permissions screen needs (§43)."""
        grants = permissions_store.list_grants()
        by_capability: dict[str, list[dict[str, Any]]] = {}
        for grant in grants:
            by_capability.setdefault(grant["capability"], []).append(grant)
        return {
            "capabilities": capabilities.catalogue(),
            "groups": list(capabilities.GROUPS),
            "grants": grants,
            "grants_by_capability": by_capability,
            "session_grants": self.session_grants(),
            "pending": self.pending(),
        }

    def startup(self) -> dict[str, Any]:
        """Clear anything that must not survive a restart (§7)."""
        legacy = permissions_store.clear_session_grants()
        self._session_grants.clear()
        self._pending.clear()
        if legacy:
            log.info("cleared stale session grants", extra={"count": legacy})
        return {"cleared_session_rows": legacy}

    # ----------------------------------------------------------------- audit

    def _audit(
        self,
        decision: Decision,
        params: dict[str, Any],
        *,
        task_id: str | None,
        conversation_id: str | None,
    ) -> None:
        verb = "Allowed" if decision.allowed else "Refused"
        summary = f"{verb}: {policy.describe_request(decision.capability, params)}"
        activity.record(
            "permission",
            summary,
            detail={
                "capability": decision.capability,
                "scope": decision.scope,
                "outcome": decision.outcome,
                "source": decision.source,
                "reason": decision.reason,
            },
            level="info" if decision.allowed else "warning",
            task_id=task_id,
            conversation_id=conversation_id,
        )


def _is_destructive(capability: str, params: dict[str, Any]) -> bool:
    cap = capabilities.get(capability)
    if capability == "terminal.execute":
        return policy.command_is_destructive(str(params.get("command", "")))
    return cap.risk in ("high", "critical") and not cap.reversible


engine = PermissionEngine()
