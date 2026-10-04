"""Process control that changes a live process without ending it (tool batch B1).

The sibling of :func:`nexus.tools.system.system_kill_process`. Killing is the
one process action already built; these three are the rest of the surface a
person reaches for when a program is *misbehaving but should not die*:

* ``system.suspend_process`` -- freeze it (SIGSTOP), so a runaway loop stops
  burning the processor while its output is inspected;
* ``system.resume_process`` -- unfreeze it (SIGCONT);
* ``system.nice`` -- change how much processor time it is *given* relative to
  everything else, without stopping it at all.

All three carry the new ``system.process.signal`` capability (§5), which is
``high`` risk and asks every time: freezing a process that holds a lock can hang
whatever waits on it, and lowering a process's niceness cannot be undone without
root. None is ``system.kill_process`` -- ending a program and pausing one are
different powers, and a grant for one must not imply the other.

Each verifies itself by re-reading ``/proc`` after acting (§60), never by
trusting the return of ``os.kill``/``os.setpriority``: a signal accepted by the
kernel is not the same as the process having reached the intended state. The
pid-or-name resolution, and the /proc read that survives pid reuse, are imported
from :mod:`nexus.tools.system` rather than re-implemented, so the two modules
cannot come to resolve "which process did they mean" two different ways.
"""

from __future__ import annotations

import os
import signal
import time
from typing import Any, Callable

from ..logging_setup import get_logger
from .registry import ToolResult, tool
from .system import _process_snapshot, _resolve_by_pid_or_name, _still_the_same_process

log = get_logger("tools.process_signal")

#: How long to wait for a signalled process to actually reach the intended state
#: before reporting that it did not. Signal delivery is asynchronous: the kernel
#: accepts SIGSTOP immediately, but the process is not marked stopped until it is
#: next scheduled, which is soon but not instant.
_SIGNAL_WAIT_SECONDS = 2.0


def _protected_pid_error(
    target: dict[str, Any], own_pid: int, action: str
) -> ToolResult | None:
    """Refuse the pids and states no process-control action should touch.

    ``action`` is a short verb phrase ("freeze", "resume", "reprioritize") used in
    the messages, which exist to *explain* the refusal: on a non-root machine the
    kernel would often reject the signal anyway, so a bare "permission denied" would
    hide the real reason. Returns an error result, or None when the target is fair
    game. The three checks are the same three ``system.kill_process`` makes; kept
    here so pausing a process is guarded exactly as ending one is.
    """
    pid = target["pid"]
    label = target["name"]
    if pid == own_pid:
        return ToolResult(
            ok=False,
            error=(
                f"pid {pid} is NEXUS itself. It will not {action} the process it is "
                "running as -- that would freeze or slow the very thing being asked to act."
            ),
        )
    if pid == 1:
        return ToolResult(
            ok=False,
            error=(
                f"pid {pid} is the init system that runs the whole machine. NEXUS will "
                f"not {action} it; doing so can make the computer stop responding."
            ),
        )
    if target.get("state") == "Z":
        return ToolResult(
            ok=False,
            error=(
                f"pid {pid} ({label}) is a zombie -- it has already ended and is waiting "
                "for its parent to collect it, so a signal can do nothing to it."
            ),
        )
    return None


def _await_state(
    pid: int, start_ticks: int, matches: Callable[[str], bool]
) -> tuple[dict[str, Any] | None, bool]:
    """Poll ``/proc/<pid>`` until its state satisfies ``matches`` or time runs out.

    Returns ``(row, reached)``. ``row`` is None only if the process vanished or a
    reused pid now names a *different* process (start time no longer matches) -- the
    caller treats both as "cannot confirm". ``reached`` says whether the wanted state
    was observed before the deadline.
    """
    deadline = time.monotonic() + _SIGNAL_WAIT_SECONDS
    while True:
        row = _still_the_same_process(pid, start_ticks)
        if row is None:
            return None, False
        if matches(row["state"]):
            return row, True
        if time.monotonic() >= deadline:
            return row, False
        time.sleep(0.05)


# ------------------------------------------------------------------ suspend


@tool(
    "system.suspend_process",
    title="Freeze a running process",
    description=(
        "Freeze one running process by pid or by name, so it stops using the "
        "processor but stays in memory. Its work is paused, not ended; use "
        "system.resume_process to let it continue. Nothing unsaved is lost."
    ),
    capability="system.process.signal",
    idempotent=True,
    timeout_s=10.0,
    parameters={
        "type": "object",
        "properties": {
            "pid": {
                "type": "integer",
                "minimum": 1,
                "description": "The process id to freeze. Use system.processes to find it.",
            },
            "name": {
                "type": "string",
                "maxLength": 200,
                "description": (
                    "The process name to freeze. Refused when more than one process "
                    "matches, rather than guessing."
                ),
            },
        },
    },
)
def system_suspend_process(params: dict[str, Any]) -> ToolResult:
    own = os.getpid()
    snapshot = _process_snapshot()

    target, problem = _resolve_by_pid_or_name(params, snapshot, own, verb="freeze")
    if problem is not None:
        return problem
    guard = _protected_pid_error(target, own, "freeze")
    if guard is not None:
        return guard

    pid, label = target["pid"], target["name"]
    if target["state"] == "T":
        # Already frozen. Sending SIGSTOP again is harmless, but saying so is more
        # honest than reporting a freeze that this call did not cause.
        return ToolResult(
            ok=True,
            data={"pid": pid, "name": label, "state": "stopped", "signal": None},
            summary=f"{label} (pid {pid}) was already frozen.",
            verified=True,
            verification=f"/proc/{pid} reports state T (stopped).",
        )

    try:
        os.kill(pid, signal.SIGSTOP)
    except ProcessLookupError:
        return ToolResult(
            ok=False,
            error=f"pid {pid} ({label}) exited before it could be frozen.",
        )
    except PermissionError:
        return ToolResult(
            ok=False,
            error=(
                f"pid {pid} ({label}) belongs to another user, so NEXUS is not allowed to "
                "freeze it. That needs administrator rights."
            ),
        )
    except OSError as exc:
        return ToolResult(ok=False, error=f"Could not freeze pid {pid} ({label}): {exc}")

    log.info("suspend_process signalled", extra={"pid": pid, "name": label, "signal": "SIGSTOP"})

    row, reached = _await_state(pid, target["start_ticks"], lambda s: s == "T")
    if reached:
        return ToolResult(
            ok=True,
            data={"pid": pid, "name": label, "state": "stopped", "signal": "SIGSTOP"},
            summary=f"Froze {label} (pid {pid}).",
            verified=True,
            verification=f"/proc/{pid} now reports state T (stopped).",
        )
    if row is None:
        return ToolResult(
            ok=False,
            data={"pid": pid, "name": label, "signal": "SIGSTOP"},
            error=f"pid {pid} ({label}) is no longer the process it was; NEXUS cannot confirm it froze.",
            verified=False,
            verification=f"/proc/{pid} no longer names the same process.",
        )
    return ToolResult(
        ok=False,
        data={"pid": pid, "name": label, "signal": "SIGSTOP"},
        error=(
            f"{label} (pid {pid}) was sent SIGSTOP but is still in state "
            f"'{row['state']}' after {_SIGNAL_WAIT_SECONDS:.0f}s, not frozen."
        ),
        verified=False,
        verification=f"/proc/{pid} reports state {row['state']}, not T.",
    )


# ------------------------------------------------------------------- resume


@tool(
    "system.resume_process",
    title="Unfreeze a paused process",
    description=(
        "Let a frozen process continue by pid or by name (the counterpart of "
        "system.suspend_process). A process that was not frozen is left running "
        "and the call reports that nothing needed to change."
    ),
    capability="system.process.signal",
    idempotent=True,
    timeout_s=10.0,
    parameters={
        "type": "object",
        "properties": {
            "pid": {
                "type": "integer",
                "minimum": 1,
                "description": "The process id to unfreeze. Use system.processes to find it.",
            },
            "name": {
                "type": "string",
                "maxLength": 200,
                "description": (
                    "The process name to unfreeze. Refused when more than one process "
                    "matches, rather than guessing."
                ),
            },
        },
    },
)
def system_resume_process(params: dict[str, Any]) -> ToolResult:
    own = os.getpid()
    snapshot = _process_snapshot()

    target, problem = _resolve_by_pid_or_name(params, snapshot, own, verb="resume")
    if problem is not None:
        return problem
    guard = _protected_pid_error(target, own, "resume")
    if guard is not None:
        return guard

    pid, label = target["pid"], target["name"]
    was_frozen = target["state"] == "T"

    try:
        os.kill(pid, signal.SIGCONT)
    except ProcessLookupError:
        return ToolResult(
            ok=False,
            error=f"pid {pid} ({label}) exited before it could be resumed.",
        )
    except PermissionError:
        return ToolResult(
            ok=False,
            error=(
                f"pid {pid} ({label}) belongs to another user, so NEXUS is not allowed to "
                "resume it. That needs administrator rights."
            ),
        )
    except OSError as exc:
        return ToolResult(ok=False, error=f"Could not resume pid {pid} ({label}): {exc}")

    log.info("resume_process signalled", extra={"pid": pid, "name": label, "signal": "SIGCONT"})

    if not was_frozen:
        # SIGCONT to a process that was already running is a no-op. Report it as a
        # verified success that changed nothing, rather than implying it unfroze
        # something -- the process is running, which is the intended end state.
        row = _still_the_same_process(pid, target["start_ticks"])
        state = row["state"] if row else "?"
        return ToolResult(
            ok=True,
            data={"pid": pid, "name": label, "state": state, "signal": "SIGCONT", "was_frozen": False},
            summary=f"{label} (pid {pid}) was not frozen; nothing needed to change.",
            verified=True,
            verification=f"/proc/{pid} reports state {state} (not stopped).",
        )

    row, reached = _await_state(pid, target["start_ticks"], lambda s: s != "T")
    if reached:
        return ToolResult(
            ok=True,
            data={"pid": pid, "name": label, "state": row["state"], "signal": "SIGCONT", "was_frozen": True},
            summary=f"Resumed {label} (pid {pid}).",
            verified=True,
            verification=f"/proc/{pid} now reports state {row['state']} (no longer stopped).",
        )
    if row is None:
        return ToolResult(
            ok=False,
            data={"pid": pid, "name": label, "signal": "SIGCONT"},
            error=f"pid {pid} ({label}) is no longer the process it was; NEXUS cannot confirm it resumed.",
            verified=False,
            verification=f"/proc/{pid} no longer names the same process.",
        )
    return ToolResult(
        ok=False,
        data={"pid": pid, "name": label, "signal": "SIGCONT"},
        error=(
            f"{label} (pid {pid}) was sent SIGCONT but is still stopped after "
            f"{_SIGNAL_WAIT_SECONDS:.0f}s."
        ),
        verified=False,
        verification=f"/proc/{pid} still reports state T (stopped).",
    )


# --------------------------------------------------------------------- nice


@tool(
    "system.nice",
    title="Change a process's priority",
    description=(
        "Change how much processor time a running process is given, by pid or by "
        "name, without stopping it. Niceness runs from -20 (most processor time) to "
        "19 (least). Raising niceness always works; lowering it below the current "
        "value needs administrator rights."
    ),
    capability="system.process.signal",
    idempotent=True,
    timeout_s=10.0,
    parameters={
        "type": "object",
        "properties": {
            "pid": {
                "type": "integer",
                "minimum": 1,
                "description": "The process id to reprioritize. Use system.processes to find it.",
            },
            "name": {
                "type": "string",
                "maxLength": 200,
                "description": (
                    "The process name to reprioritize. Refused when more than one "
                    "process matches, rather than guessing."
                ),
            },
            "niceness": {
                "type": "integer",
                "minimum": -20,
                "maximum": 19,
                "description": (
                    "The new niceness: -20 gives the process the most processor time, "
                    "19 the least. 0 is the default."
                ),
            },
        },
        "required": ["niceness"],
    },
)
def system_nice(params: dict[str, Any]) -> ToolResult:
    niceness = int(params["niceness"])
    own = os.getpid()
    snapshot = _process_snapshot()

    target, problem = _resolve_by_pid_or_name(params, snapshot, own, verb="reprioritize")
    if problem is not None:
        return problem
    guard = _protected_pid_error(target, own, "reprioritize")
    if guard is not None:
        return guard

    pid, label = target["pid"], target["name"]

    try:
        previous = os.getpriority(os.PRIO_PROCESS, pid)
    except ProcessLookupError:
        return ToolResult(
            ok=False,
            error=f"pid {pid} ({label}) exited before its priority could be changed.",
        )
    except OSError as exc:
        return ToolResult(ok=False, error=f"Could not read the priority of pid {pid} ({label}): {exc}")

    try:
        os.setpriority(os.PRIO_PROCESS, pid, niceness)
    except ProcessLookupError:
        return ToolResult(
            ok=False,
            error=f"pid {pid} ({label}) exited before its priority could be changed.",
        )
    except PermissionError:
        return ToolResult(
            ok=False,
            error=(
                f"NEXUS is not allowed to set pid {pid} ({label}) to niceness {niceness}. "
                "That needs administrator rights -- either because the process belongs to "
                "another user, or because lowering a process's niceness (raising its "
                f"priority) below its current {previous} is a privileged action."
            ),
        )
    except OSError as exc:
        return ToolResult(ok=False, error=f"Could not reprioritize pid {pid} ({label}): {exc}")

    log.info("nice changed", extra={"pid": pid, "name": label, "niceness": niceness})

    # Verify by re-reading the priority the kernel actually holds, but only after
    # confirming pid still names the same process -- a reused pid would otherwise let
    # a different process's niceness pass as proof.
    still = _still_the_same_process(pid, target["start_ticks"])
    if still is None:
        return ToolResult(
            ok=False,
            data={"pid": pid, "name": label, "niceness": niceness},
            error=f"pid {pid} ({label}) is no longer the process it was; NEXUS cannot confirm the change.",
            verified=False,
            verification=f"/proc/{pid} no longer names the same process.",
        )
    current = os.getpriority(os.PRIO_PROCESS, pid)
    if current == niceness:
        return ToolResult(
            ok=True,
            data={"pid": pid, "name": label, "niceness": current, "previous_niceness": previous},
            summary=f"Set {label} (pid {pid}) to niceness {current} (was {previous}).",
            verified=True,
            verification=f"getpriority for pid {pid} now reports {current}.",
        )
    return ToolResult(
        ok=False,
        data={"pid": pid, "name": label, "niceness": current, "previous_niceness": previous},
        error=(
            f"Asked to set pid {pid} ({label}) to niceness {niceness}, but it now reads "
            f"{current}."
        ),
        verified=False,
        verification=f"getpriority for pid {pid} reports {current}, not {niceness}.",
    )
