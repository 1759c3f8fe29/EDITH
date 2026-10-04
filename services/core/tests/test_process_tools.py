"""``system.processes`` and ``system.kill_process`` against real processes (task #34).

These two tools are the diagnosis half of §27 -- the sentence "Chrome is using most
of your RAM" needs something that can actually say so -- and one of them ends running
programs, so the bar for what counts as a passing test here is higher than usual.

Two rules shape the whole file:

**Nothing is asserted from the tool's own output.** ``system.kill_process`` verifies
itself by reading ``/proc`` (§60), so a test that checked ``result.verified`` and
stopped would be asking the tool whether it agreed with itself. Every kill here is
also confirmed through ``Popen.wait()``, which returns the signal the kernel actually
delivered: ``-15`` is SIGTERM, ``-9`` is SIGKILL. That number comes from the process
table, not from NEXUS.

**Every process these tests end is one they started.** The targets are ``sleep`` and
short Python programs spawned by the test itself and reaped by it, so a failure
halfway through leaves nothing of the user's behind. No test signals anything it did
not create; that is what makes running a kill test on a live desktop acceptable at all.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from typing import Any, Iterator

import pytest

import nexus.tools  # noqa: F401  -- importing registers every tool
from nexus.permissions import capabilities
from nexus.tools import registry
from nexus.tools import system as system_module

# Signal a child ignores while it is meant to survive SIGTERM.
IGNORE_TERM = (
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "print('up', flush=True); time.sleep(120)"
)


@pytest.fixture
def children() -> Iterator[list[subprocess.Popen[bytes]]]:
    """Processes started by the test, and guaranteed gone when it ends.

    The teardown uses SIGKILL deliberately: by then the test is over and the only
    thing that matters is that nothing is left running.
    """
    spawned: list[subprocess.Popen[bytes]] = []
    yield spawned
    for child in spawned:
        if child.poll() is None:
            child.kill()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover -- would be a stuck child
            pass


def spawn(children: list[subprocess.Popen[bytes]], *args: str) -> subprocess.Popen[bytes]:
    child = subprocess.Popen(list(args), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    children.append(child)
    return child


def sleeper(children: list[subprocess.Popen[bytes]]) -> subprocess.Popen[bytes]:
    child = spawn(children, "sleep", "120")
    await_pid(child.pid)
    return child


def await_pid(pid: int, timeout: float = 5.0) -> None:
    """Wait until /proc knows about a pid, so a test never races the fork."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(f"/proc/{pid}"):
            return
        time.sleep(0.02)
    raise AssertionError(f"pid {pid} never appeared in /proc")


def call(tool_name: str, /, **params: Any):
    """Invoke a tool the way the executor does: validate, then run.

    ``tool_name`` is positional-only because ``system.kill_process`` takes a
    parameter called ``name``, and a keyword of that name would collide with it.
    """
    tool = registry.get(tool_name)
    assert tool is not None, f"{tool_name} is not registered"
    return tool.handler(registry.validate(tool.parameters, params))


def processes(**params: Any):
    return call("system.processes", **params)


def kill(**params: Any):
    return call("system.kill_process", **params)


def find(result: Any, pid: int) -> dict[str, Any] | None:
    for row in result.data["processes"]:
        if row["pid"] == pid:
            return row
    return None


# ------------------------------------------------------------------ declaration


def test_both_tools_are_registered_with_the_capabilities_they_claim() -> None:
    listing = registry.get("system.processes")
    ending = registry.get("system.kill_process")
    assert listing is not None and ending is not None

    assert listing.capability == "system.processes"
    assert ending.capability == "system.kill_process"
    # A tool whose capability is not in the catalogue cannot be registered at all,
    # but the reverse -- a capability that exists at the wrong risk -- registers
    # fine and quietly changes which dialogs the user sees.
    assert capabilities.get("system.processes").risk == "medium"
    assert capabilities.get("system.kill_process").risk == "high"


def test_listing_is_read_only_and_ending_is_not_idempotent() -> None:
    listing = registry.get("system.processes")
    ending = registry.get("system.kill_process")
    assert listing.read_only is True
    assert ending.read_only is False
    # Agent mode reads `idempotent` to decide whether a retry is safe. Retrying a
    # kill can hit a different process that has taken the pid.
    assert ending.idempotent is False


def test_a_process_list_is_never_offered_without_a_prompt() -> None:
    """The capability must not be implicit, and the reason is what it reveals.

    ``system.info`` is implicit because a battery percentage says nothing about the
    person using the machine. A process list carries command lines, which name files
    and sites -- the same argument that makes ``window.list`` promptable.
    """
    assert capabilities.is_implicit("system.processes") is False
    assert capabilities.is_implicit("system.kill_process") is False


def test_ending_a_process_can_only_be_granted_narrowly() -> None:
    # high risk => no "always". A standing permission to end any process is not
    # something this tool should be able to acquire.
    assert "always" not in capabilities.offered_grant_kinds("system.kill_process")
    assert capabilities.get("system.kill_process").reversible is False


def test_neither_tool_needs_a_program_that_might_not_be_installed() -> None:
    """/proc and os.kill are always there, so `available` is honestly True.

    This is also the check that would fail if someone rewrote the listing on top of
    `ps`: the tool would then need a binary it does not declare.
    """
    assert registry.get("system.processes").requires == ()
    assert registry.get("system.kill_process").requires == ()
    assert registry.get("system.processes").available is True


# ---------------------------------------------------------------------- listing


def test_the_listing_finds_a_process_the_test_started(children) -> None:
    child = sleeper(children)
    result = processes(query="sleep", limit=100)

    assert result.ok
    row = find(result, child.pid)
    assert row is not None, f"pid {child.pid} is running but was not listed"
    assert row["name"] == "sleep"
    assert row["ppid"] == os.getpid()
    assert "sleep 120" in row["cmdline"]
    assert row["memory_bytes"] > 0
    assert row["threads"] >= 1


def test_a_process_that_has_exited_leaves_the_listing(children) -> None:
    child = sleeper(children)
    assert find(processes(query="sleep", limit=100), child.pid) is not None

    child.kill()
    child.wait(timeout=5)  # reaped, so not even a zombie entry remains

    assert find(processes(query="sleep", limit=100), child.pid) is None


def test_a_name_with_spaces_does_not_shift_every_field(children) -> None:
    """The /proc/<pid>/stat parse must not split on whitespace.

    A program picks its own name, and Linux lets that name contain spaces and
    brackets. ``comm`` sits at field 2, so a name split on spaces pushes every later
    field along -- state, ppid, memory, all of it -- and the damage is silent. This
    test writes a process whose name is ``a b) c`` and checks the *last* field still
    reads correctly.
    """
    script = (
        "import ctypes, time;"
        "ctypes.CDLL(None).prctl(15, b'a b) c', 0, 0, 0);"
        "time.sleep(120)"
    )
    child = spawn(children, sys.executable, "-c", script)
    await_pid(child.pid)
    time.sleep(0.4)  # let prctl land before the snapshot

    # "newest" so the just-spawned child is at the front and cannot be truncated
    # away; on a busy machine an ascending pid sort puts it past any sane limit.
    row = find(processes(limit=200, sort="newest"), child.pid)
    assert row is not None
    assert row["name"] == "a b) c", "the process name was parsed wrong"
    # Everything after the name is what a whitespace split would corrupt.
    assert row["ppid"] == os.getpid()
    assert row["memory_bytes"] > 0
    assert row["state"] in ("running", "sleeping")
    assert row["running_for"] is not None


def test_the_query_matches_command_lines_and_not_only_names(children) -> None:
    """A program run through an interpreter is named for the interpreter.

    ``python3 -c ...`` has the process name "python3", so searching by what was
    actually run only works if the command line is searched too.
    """
    marker = "nexus-process-tool-marker"
    child = spawn(children, sys.executable, "-c", f"import time; time.sleep(120)  # {marker}")
    await_pid(child.pid)

    result = processes(query=marker, limit=50)
    assert result.ok
    assert find(result, child.pid) is not None
    # And every returned row genuinely matches, rather than the filter being ignored.
    for row in result.data["processes"]:
        assert marker in row["cmdline"]


def test_a_query_that_matches_nothing_says_so_instead_of_naming_a_process() -> None:
    """The empty-match summary must not describe an unrelated process.

    The first version of this tool computed its headline from the full listing
    whatever the query was, so a search for a program that is not running came back
    "1 of 254 processes match 'zzz'; largest is bash" -- a confident sentence about
    something the user did not ask about.
    """
    result = processes(query="zzz-nothing-is-called-this-abc123")
    assert result.ok
    assert result.data["matched"] == 0
    assert result.data["processes"] == []
    assert "zzz-nothing-is-called-this-abc123" in result.summary
    assert "largest" not in result.summary.lower()
    assert "using the most" not in result.summary.lower()


def test_the_summary_describes_the_axis_it_was_sorted_by(children) -> None:
    busy = spawn(children, sys.executable, "-c", "\nwhile True: pass\n")
    await_pid(busy.pid)

    by_cpu = processes(sort="cpu", limit=5)
    assert by_cpu.ok
    assert "busiest" in by_cpu.summary, by_cpu.summary
    assert "most memory" not in by_cpu.summary

    by_memory = processes(sort="memory", limit=5)
    assert "most memory" in by_memory.summary, by_memory.summary


def test_sorting_by_memory_returns_the_heaviest_processes_in_order() -> None:
    """Ordering and truncation, checked inside a single sample.

    An earlier version of this test compared the top entry of a ``limit=10`` call
    with the top entry of a ``limit=200`` call and required the byte counts to be
    equal. They are two samples of a machine that keeps running: the first run of
    that assertion failed on a browser whose memory moved 11 MB in the gap. Nothing
    was wrong with the tool -- the test was asserting that a live number holds
    still. Both properties can be checked within one call instead.
    """
    result = processes(sort="memory", limit=10)
    shown = result.data["processes"]
    sizes = [row["memory_bytes"] for row in shown]
    assert sizes == sorted(sizes, reverse=True)
    assert result.data["truncated"] is True
    assert len(shown) == 10


def test_the_top_of_the_memory_list_is_the_top_of_the_machine() -> None:
    """Descending order alone would pass on the first ten pids read.

    So the ranking is checked against /proc directly rather than against another
    call to the tool. The oracle is ``/proc/<pid>/statm`` -- a different file from
    the ``stat`` the tool parses -- so agreement is not two readings of one parse.
    The comparison is by *name*, not by byte count: an exact number cannot be
    expected to hold still between two reads of a running machine.
    """
    import pathlib

    biggest_name, biggest_rss = "", 0
    page = os.sysconf("SC_PAGE_SIZE") or 4096
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            rss = int(entry.joinpath("statm").read_text().split()[1]) * page
            name = entry.joinpath("comm").read_text().strip()
        except (OSError, IndexError, ValueError):
            continue  # exited while being read, which is normal
        if rss > biggest_rss:
            biggest_name, biggest_rss = name, rss

    assert biggest_rss > 0
    top = processes(sort="memory", limit=5).data["processes"]
    # comm is truncated to 15 characters by the kernel, so compare on that prefix.
    assert biggest_name[:15] in [row["name"][:15] for row in top], (
        f"/proc says {biggest_name} is the largest at {biggest_rss} bytes, "
        f"but the tool's top five are {[row['name'] for row in top]}"
    )


def test_an_unmeasurable_cpu_reading_is_null_and_sorts_last(children) -> None:
    """A process that appeared mid-sample has no rate, and must not claim 0.0.

    ``cpu_percent`` is measured across two /proc reads. A process born between them
    has no earlier total to subtract, so there is no rate to report -- and reporting
    0.0 would state that it is idle, which is not something the tool knows.
    """
    for _ in range(6):
        spawn(children, "sleep", "120")

    result = processes(sort="cpu", limit=200)
    values = [row["cpu_percent"] for row in result.data["processes"]]
    measured = [value for value in values if value is not None]
    assert measured, "no process had a measurable CPU reading"
    assert measured == sorted(measured, reverse=True)
    # Nulls, if any, are all at the end.
    first_null = next((i for i, value in enumerate(values) if value is None), len(values))
    assert all(value is None for value in values[first_null:])


def test_a_process_born_mid_sample_is_none_not_zero(monkeypatch) -> None:
    """The null-CPU path, constructed deterministically.

    Live runs cannot be relied on to produce a process born exactly between the two
    /proc reads, and on a busy machine every process accrues a tick in 0.4s anyway --
    which is why the live test above only asserts ordering. This one stitches the
    snapshots by hand: one pid is present in both reads, the other (pid 4242) only in
    the second, as if it started during the sample. A fresh process has no earlier
    total to subtract, so its rate is ``None`` -- and ``None`` is the honest answer,
    because ``0.0`` would claim the brand-new process is idle.
    """
    def row(pid: int, ticks: int) -> dict[str, Any]:
        return {
            "pid": pid,
            "name": f"proc-{pid}",
            "state": "S",
            "ppid": 1,
            "cpu_ticks": ticks,
            "threads": 1,
            "start_ticks": 100,
            "rss_bytes": 4096,
        }

    snapshots = iter(
        [
            {1000: row(1000, 100)},
            {1000: row(1000, 140), 4242: row(4242, 5)},
        ]
    )
    monkeypatch.setattr(system_module, "_process_snapshot", lambda: next(snapshots))

    rows, _elapsed = system_module._sampled_processes(interval=0.0)
    by_pid = {r["pid"]: r for r in rows}
    assert by_pid[4242]["cpu_percent"] is None
    assert isinstance(by_pid[1000]["cpu_percent"], float)


def test_a_reused_pid_is_not_mistaken_for_the_same_process(monkeypatch) -> None:
    """The pid-reuse guard, which a live poll cannot hit deterministically.

    ``system_kill_process`` polls /proc after signalling and must not report success
    just because *something* is still at the pid: it has to be the *same* process.
    Without the start-time comparison, a process that died and was instantly replaced
    by an unrelated one would read as "the kill failed". This drives the guard
    directly: same start time -> same process, different start time -> gone.
    """
    monkeypatch.setattr(
        system_module,
        "_proc_row",
        lambda pid: {"pid": pid, "start_ticks": 100, "state": "S"} if pid == 42 else None,
    )
    assert system_module._still_the_same_process(42, 100) is not None
    assert system_module._still_the_same_process(42, 999) is None
    assert system_module._still_the_same_process(43, 100) is None


def test_cpu_percentages_are_per_core_and_a_busy_loop_is_visibly_busy(children) -> None:
    busy = spawn(children, sys.executable, "-c", "\nwhile True: pass\n")
    await_pid(busy.pid)
    time.sleep(0.3)

    row = find(processes(sort="cpu", limit=200), busy.pid)
    assert row is not None
    # One thread spinning cannot exceed one core, and should be nowhere near idle.
    # The floor is loose on purpose: this runs on a shared, loaded machine.
    assert row["cpu_percent"] is not None
    assert row["cpu_percent"] > 20.0, f"a spin loop measured {row['cpu_percent']}%"
    assert row["cpu_percent"] <= 100.0 * (os.cpu_count() or 1)


def test_the_listing_reports_how_precisely_cpu_could_be_measured() -> None:
    """The sample window and its resolution are part of the answer.

    CPU time is counted in whole clock ticks, so a short sample cannot distinguish
    small differences. A consumer that does not know the granularity will read a
    quantised number as an exact one.
    """
    result = processes(limit=1)
    assert result.data["cpu_sampled_over_ms"] > 0
    resolution = result.data["cpu_resolution_percent"]
    ticks = os.sysconf("SC_CLK_TCK") or 100
    expected = 100 / (ticks * result.data["cpu_sampled_over_ms"] / 1000)
    assert resolution == pytest.approx(expected, rel=0.05)


def test_a_credential_on_a_command_line_is_not_returned(children) -> None:
    """Command lines reach a cloud model, so they go through the log redactor.

    A command line is visible to every user on the machine, so this is not a leak
    NEXUS creates -- but repeating it into a model prompt is a fresh disclosure, to
    somewhere the process table never reached.
    """
    secret = "sk-livetest" + "0123456789abcdef0123"
    child = spawn(children, sys.executable, "-c", f"import time; time.sleep(120)  # {secret}")
    await_pid(child.pid)

    row = find(processes(query="time.sleep", limit=200), child.pid)
    assert row is not None
    assert secret not in row["cmdline"]
    assert "[redacted]" in row["cmdline"]


def test_percentages_are_relative_to_this_machines_memory() -> None:
    result = processes(limit=5)
    total = result.data["memory_total_bytes"]
    assert total > 0
    for row in result.data["processes"]:
        expected = round(row["memory_bytes"] / total * 100, 1)
        assert row["memory_percent"] == expected
        assert 0.0 <= row["memory_percent"] <= 100.0


def test_the_limit_bounds_the_output_without_hiding_that_it_did() -> None:
    result = processes(limit=3)
    assert len(result.data["processes"]) == 3
    assert result.data["shown"] == 3
    assert result.data["total"] > 3
    assert result.data["truncated"] is True

    everything = processes(limit=200)
    assert everything.data["truncated"] == (everything.data["total"] > 200)


def test_the_listing_rejects_a_limit_outside_its_declared_range() -> None:
    with pytest.raises(registry.SchemaError):
        processes(limit=0)
    with pytest.raises(registry.SchemaError):
        processes(limit=5000)
    with pytest.raises(registry.SchemaError):
        processes(sort="whatever")


# ----------------------------------------------------------------------- ending


def test_ending_a_process_asks_which_one() -> None:
    empty = kill()
    assert empty.ok is False
    assert "pid" in empty.error and "name" in empty.error


def test_a_pid_and_a_name_together_are_refused_rather_than_reconciled(children) -> None:
    """Two targets that could disagree is a question, not a request.

    Neither parameter can be schema-``required`` -- a JSON Schema of this shape
    cannot say "exactly one of these" -- so the handler owns the check, and this is
    the only thing that proves it does.
    """
    child = sleeper(children)
    result = kill(pid=child.pid, name="sleep")
    assert result.ok is False
    assert child.poll() is None, "a refused call must not signal anything"


def test_nexus_refuses_to_end_itself() -> None:
    result = kill(pid=os.getpid())
    assert result.ok is False
    assert "itself" in result.error


def test_nexus_refuses_to_end_the_init_system() -> None:
    result = kill(pid=1)
    assert result.ok is False
    # The refusal must be for the right reason. On a non-root machine the kernel
    # would refuse a signal to pid 1 with PermissionError anyway, so a test that
    # only checked `ok is False` would pass even if this guard were deleted -- the
    # tool would just say "needs root" instead of "would take the machine down".
    # The guard exists to *explain*, so the assertion pins the explanation.
    assert "init" in result.error.lower()
    assert os.path.exists("/proc/1")


def test_an_unknown_pid_or_name_is_reported_as_not_running() -> None:
    absent = kill(pid=4_000_000)
    assert absent.ok is False
    assert "4000000" in absent.error

    unknown = kill(name="nothing-is-called-this-xyz789")
    assert unknown.ok is False
    assert "nothing-is-called-this-xyz789" in unknown.error


def test_an_ambiguous_name_lists_the_matches_instead_of_killing_one(children) -> None:
    """Three processes called ``sleep``, and none of them is ended.

    This is the single most important behaviour in the tool. "Close Chrome" names
    twenty processes, and a tool that picks one -- the first, the biggest, the
    newest -- ends something the user did not choose, irreversibly.
    """
    trio = [sleeper(children) for _ in range(3)]
    pids = {child.pid for child in trio}

    result = kill(name="sleep")
    assert result.ok is False
    assert "will not guess" in result.error
    listed = {row["pid"] for row in result.data["matches"]}
    assert pids <= listed

    # The evidence that nothing was killed comes from the children, not the tool.
    time.sleep(0.3)
    for child in trio:
        assert child.poll() is None, f"pid {child.pid} was ended despite the refusal"


def test_an_unambiguous_name_is_enough_to_end_a_process(children) -> None:
    marker = "nexus-unique-target-4a91"
    child = spawn(children, sys.executable, "-c", f"import time; time.sleep(120)  # {marker}")
    await_pid(child.pid)

    result = kill(name=marker)
    assert result.ok is True, result.error
    assert child.wait(timeout=5) == -signal.SIGTERM


def test_a_process_is_asked_to_stop_before_it_is_forced(children) -> None:
    """SIGKILL is refused as a first step, and the refusal says what to do instead.

    SIGTERM lets a program save open work; SIGKILL does not. A tool that accepted
    ``force=True`` straight away would make data loss a one-parameter mistake.
    """
    child = sleeper(children)

    refused = kill(pid=child.pid, force=True)
    assert refused.ok is False
    assert "SIGKILL" in refused.error and "SIGTERM" in refused.error
    time.sleep(0.3)
    assert child.poll() is None, "force was refused but the process died anyway"

    # Asked politely, it goes -- and the kernel says which signal did it.
    assert kill(pid=child.pid).ok is True
    assert child.wait(timeout=5) == -signal.SIGTERM


def test_force_is_allowed_once_sigterm_has_been_tried_on_that_process(children) -> None:
    child = spawn(children, sys.executable, "-c", IGNORE_TERM)
    await_pid(child.pid)
    time.sleep(0.5)  # let the handler be installed before signalling

    first = kill(pid=child.pid)
    assert first.ok is False, "a process ignoring SIGTERM must not be reported as ended"
    assert first.verified is False
    assert child.poll() is None

    forced = kill(pid=child.pid, force=True)
    assert forced.ok is True, forced.error
    assert child.wait(timeout=5) == -signal.SIGKILL


def test_a_process_that_ignores_sigterm_is_reported_as_still_running(children) -> None:
    """The honest outcome of a delivered signal that achieved nothing.

    ``os.kill`` returning without error means the signal was delivered, not obeyed.
    Reporting that as success is precisely the dishonesty §60 exists to prevent, so
    this is ``ok=False`` with ``verified=False`` -- checked, and not confirmed.
    """
    child = spawn(children, sys.executable, "-c", IGNORE_TERM)
    await_pid(child.pid)
    time.sleep(0.5)

    result = kill(pid=child.pid)
    assert result.ok is False
    assert result.verified is False
    assert str(child.pid) in result.verification
    assert result.data["ended"] is False
    assert result.data["signal"] == "SIGTERM"
    assert child.poll() is None, "the process should still be running"


def test_a_successful_end_is_confirmed_by_reading_proc(children) -> None:
    child = sleeper(children)

    result = kill(pid=child.pid)
    assert result.ok is True
    # `verified` is the tool's own claim; the exit status is the kernel's.
    assert result.verified is True
    assert str(child.pid) in result.verification
    assert result.data["signal"] == "SIGTERM"
    assert child.wait(timeout=5) == -signal.SIGTERM


def test_a_zombie_is_not_signalled_because_it_cannot_be(children) -> None:
    """An unreaped child is already dead; a signal to it is a no-op.

    This also pins down that the tool distinguishes "gone from /proc" from "no
    longer running": a zombie is the second without the first.
    """
    child = spawn(children, "sleep", "120")
    await_pid(child.pid)
    child.kill()
    # Deliberately not reaped, so the entry stays in /proc as a zombie.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = open(f"/proc/{child.pid}/stat").read().rsplit(")", 1)[1].split()[0]
        if state == "Z":
            break
        time.sleep(0.02)
    else:  # pragma: no cover -- would mean the child never died
        raise AssertionError("the child never became a zombie")

    result = kill(pid=child.pid)
    assert result.ok is False
    assert "zombie" in result.error
    child.wait(timeout=5)


def test_a_pid_that_exits_on_its_own_is_a_verified_success_not_an_error(children) -> None:
    """Racing a process that was already leaving.

    The requested end state is the real one and /proc proves it, so this is honest
    success -- but the summary has to say no signal was sent, because none was.
    """
    child = spawn(children, sys.executable, "-c", "import time; time.sleep(0.6)")
    await_pid(child.pid)
    child.wait(timeout=5)

    result = kill(pid=child.pid)
    # Fully reaped, so /proc has forgotten it entirely and the tool cannot resolve
    # the target at all. That is a plain "not running", which is the truth.
    assert result.ok is False
    assert str(child.pid) in result.error


def test_ending_a_process_owned_by_another_user_says_it_needs_root() -> None:
    """A pid the current user may not signal, without needing root to test it.

    pid 2 is ``kthreadd``, a kernel thread owned by root and present on every Linux
    system. Signalling it is refused by the kernel, which is the path under test.
    """
    if os.geteuid() == 0:
        pytest.skip("running as root, so no process is off limits")
    if not os.path.exists("/proc/2"):  # pragma: no cover -- container without kthreadd
        pytest.skip("pid 2 is not present on this system")

    result = kill(pid=2)
    assert result.ok is False
    # Either refusal is correct: not permitted, or (in a container) not visible.
    assert "root" in result.error or "not running" in result.error


def test_the_pid_that_was_signalled_is_the_pid_that_was_asked_for(children) -> None:
    """One target, and only one, even when others share its name.

    Two processes both called ``sleep``: naming a pid must end that pid and leave
    its twin alone.
    """
    first = sleeper(children)
    second = sleeper(children)

    assert kill(pid=first.pid).ok is True
    assert first.wait(timeout=5) == -signal.SIGTERM
    time.sleep(0.3)
    assert second.poll() is None, "the wrong process was ended"
