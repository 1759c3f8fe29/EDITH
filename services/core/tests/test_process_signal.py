"""``system.suspend_process`` / ``resume_process`` / ``nice`` against real processes.

The mutating half of process control (batch B1), held to the same bar as the
kill tests next door and for the same reason: these tools change live processes,
so a test that trusted the tool's own ``verified`` flag would be asking the tool
whether it agrees with itself.

Two rules, inherited from ``test_process_tools.py``:

**Nothing is asserted from the tool's own output.** Whether a process is frozen,
resumed or reprioritized is read straight from ``/proc/<pid>/stat`` and from
``os.getpriority`` -- the kernel's own record -- not from the ``ToolResult``.

**Every process a test touches is one it started.** The targets are ``sleep``
children spawned and reaped by the test itself, so nothing of the user's is ever
signalled. The one exception is the guard tests (self, init, zombie, ambiguous),
which are written precisely to prove the tool refuses *before* any signal leaves.

Each test is shaped to fail a specific mutant:

* a suspend that sent SIGCONT (or nothing) would leave state ``S``, not ``T``;
* a resume that sent SIGSTOP would leave a woken process stuck at ``T``;
* a ``was_frozen`` that was hard-coded True would misreport a process that was
  already running;
* a nice that skipped ``setpriority`` would leave ``getpriority`` unchanged;
* a self/init/zombie guard that was dropped would signal the forbidden target --
  so those tests assert the refusal *and* that the target is untouched.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any, Iterator

import pytest

import nexus.tools  # noqa: F401  -- importing registers every tool
from nexus.permissions import capabilities
from nexus.tools import registry


@pytest.fixture
def children() -> Iterator[list[subprocess.Popen[bytes]]]:
    spawned: list[subprocess.Popen[bytes]] = []
    yield spawned
    for child in spawned:
        if child.poll() is None:
            child.kill()  # SIGKILL wakes and ends even a stopped (frozen) child
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover -- a stuck child
            pass


def sleeper(children: list[subprocess.Popen[bytes]]) -> subprocess.Popen[bytes]:
    child = subprocess.Popen(["sleep", "120"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    children.append(child)
    _await_pid(child.pid)
    return child


def _await_pid(pid: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(f"/proc/{pid}"):
            return
        time.sleep(0.02)
    raise AssertionError(f"pid {pid} never appeared in /proc")


def proc_state(pid: int) -> str | None:
    """The state letter from /proc/<pid>/stat, read independently of the tool.

    Taken from after the *last* ``)`` so a process named ``(sleep) 120`` cannot
    shift the field -- the same care the production parser takes.
    """
    try:
        raw = open(f"/proc/{pid}/stat").read()
    except OSError:
        return None
    closed = raw.rfind(")")
    tail = raw[closed + 1:].split()
    return tail[0] if tail else None


def await_state(pid: int, want: str, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc_state(pid) == want:
            return
        time.sleep(0.02)
    raise AssertionError(f"pid {pid} never reached state {want} (last seen {proc_state(pid)})")


def call(tool_name: str, /, **params: Any) -> registry.ToolResult:
    tool = registry.get(tool_name)
    assert tool is not None, f"{tool_name} is not registered"
    return tool.handler(registry.validate(tool.parameters, params))


# --------------------------------------------------------------- registration


def test_all_three_share_the_signal_capability_and_are_not_implicit() -> None:
    for name in ("system.suspend_process", "system.resume_process", "system.nice"):
        tool = registry.get(name)
        assert tool is not None, f"{name} is not registered"
        assert tool.capability == "system.process.signal"
        assert tool.read_only is False
        assert tool.risk == "high"
    # High risk: allowed per-use or per-session, never granted "always" and never
    # implicit -- freezing a process must ask, like ending one does.
    assert capabilities.is_implicit("system.process.signal") is False
    assert "always" not in capabilities.offered_grant_kinds("system.process.signal")
    assert capabilities.get("system.process.signal").reversible is False


# ------------------------------------------------------------------- suspend


def test_suspend_actually_freezes_the_process(children) -> None:
    child = sleeper(children)
    assert proc_state(child.pid) in ("S", "R")  # sleeping/running before

    result = call("system.suspend_process", pid=child.pid)

    assert result.ok is True
    # The proof is the process table, not the tool: a SIGCONT (or no signal) here
    # would leave the child sleeping, and this would fail.
    await_state(child.pid, "T")
    assert proc_state(child.pid) == "T"


def test_suspend_of_an_already_frozen_process_does_not_resignal(children) -> None:
    child = sleeper(children)
    os.kill(child.pid, signal.SIGSTOP)  # freeze it ourselves, outside the tool
    await_state(child.pid, "T")

    result = call("system.suspend_process", pid=child.pid)

    assert result.ok is True
    # It recognised the process was already stopped and reports that no signal was
    # sent, rather than claiming to have frozen it.
    assert result.data["signal"] is None
    assert "already frozen" in result.summary


# -------------------------------------------------------------------- resume


def test_resume_unfreezes_a_frozen_process(children) -> None:
    child = sleeper(children)
    os.kill(child.pid, signal.SIGSTOP)
    await_state(child.pid, "T")

    result = call("system.resume_process", pid=child.pid)

    assert result.ok is True
    assert result.data["was_frozen"] is True
    # A resume that sent SIGSTOP instead of SIGCONT would leave this at 'T'.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and proc_state(child.pid) == "T":
        time.sleep(0.02)
    assert proc_state(child.pid) != "T"


def test_resume_of_a_running_process_reports_it_was_not_frozen(children) -> None:
    child = sleeper(children)
    assert proc_state(child.pid) in ("S", "R")

    result = call("system.resume_process", pid=child.pid)

    assert result.ok is True
    # Distinguishes "unfroze it" from "it was already running" -- a hard-coded
    # was_frozen=True would fail here, and the process stays running either way.
    assert result.data["was_frozen"] is False
    assert proc_state(child.pid) != "T"


def test_suspend_then_resume_end_to_end_uses_only_the_tools(children) -> None:
    child = sleeper(children)

    call("system.suspend_process", pid=child.pid)
    await_state(child.pid, "T")

    call("system.resume_process", pid=child.pid)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and proc_state(child.pid) == "T":
        time.sleep(0.02)
    assert proc_state(child.pid) != "T"


# ---------------------------------------------------------------------- nice


def test_nice_raises_niceness_and_the_kernel_agrees(children) -> None:
    child = sleeper(children)
    before = os.getpriority(os.PRIO_PROCESS, child.pid)
    target_nice = min(before + 5, 19)

    result = call("system.nice", pid=child.pid, niceness=target_nice)

    assert result.ok is True
    # Read the priority the kernel holds, not the number the tool returned.
    assert os.getpriority(os.PRIO_PROCESS, child.pid) == target_nice
    assert result.data["previous_niceness"] == before


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root can lower niceness, so there is no refusal to observe"
)
def test_nice_cannot_lower_priority_without_privilege(children) -> None:
    child = sleeper(children)
    before = os.getpriority(os.PRIO_PROCESS, child.pid)
    if before - 5 < -20:  # pragma: no cover -- only if started at an odd niceness
        pytest.skip("process already near the priority floor")

    result = call("system.nice", pid=child.pid, niceness=before - 5)

    # An unprivileged process may raise its niceness but not lower it: the kernel
    # refuses, and the tool must surface that as a failure with the reason, not
    # swallow it and claim success. The kernel's record must be unchanged.
    assert result.ok is False
    assert "administrator" in result.error.lower()
    assert os.getpriority(os.PRIO_PROCESS, child.pid) == before


def test_nice_requires_a_niceness_value() -> None:
    tool = registry.get("system.nice")
    with pytest.raises(registry.SchemaError):
        registry.validate(tool.parameters, {"pid": 1234})


# ----------------------------------------------------------------- the guards


def test_refuses_to_freeze_nexus_itself() -> None:
    result = call("system.suspend_process", pid=os.getpid())
    # The guard returns before any signal is sent, which is the only reason calling
    # this on our own pid is safe: a dropped guard would freeze the test runner.
    assert result.ok is False
    assert "itself" in result.error


def test_refuses_to_freeze_the_init_system() -> None:
    result = call("system.suspend_process", pid=1)
    assert result.ok is False
    assert "init" in result.error.lower()
    assert os.path.exists("/proc/1")  # the guard, not a missing target, is the reason


def test_refuses_to_signal_a_zombie(children) -> None:
    # A child that has exited but not been reaped is a zombie: present in /proc with
    # state Z, but a signal can do nothing to it.
    zombie = subprocess.Popen(["true"])
    children.append(zombie)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and proc_state(zombie.pid) != "Z":
        time.sleep(0.02)
    assert proc_state(zombie.pid) == "Z", "child did not become a zombie"

    result = call("system.suspend_process", pid=zombie.pid)
    assert result.ok is False
    assert "zombie" in result.error.lower()


def test_an_ambiguous_name_freezes_nothing(children) -> None:
    """Two processes called ``sleep`` and neither is touched.

    The most important safety property: when a name is ambiguous the tool must
    refuse *before* signalling, or it would freeze a process the user did not name.
    """
    trio = [sleeper(children) for _ in range(2)]

    result = call("system.suspend_process", name="sleep")

    assert result.ok is False
    assert "will not guess" in result.error
    # Proof from the process table that nothing was frozen.
    time.sleep(0.3)
    for child in trio:
        assert proc_state(child.pid) in ("S", "R"), f"pid {child.pid} was frozen despite the refusal"


def test_a_pid_and_a_name_together_are_refused(children) -> None:
    child = sleeper(children)
    result = call("system.suspend_process", pid=child.pid, name="sleep")
    assert result.ok is False
    assert proc_state(child.pid) in ("S", "R"), "a refused call must not signal anything"


def test_an_unknown_pid_is_reported_not_running() -> None:
    result = call("system.resume_process", pid=4_000_000)
    assert result.ok is False
    assert "4000000" in result.error
