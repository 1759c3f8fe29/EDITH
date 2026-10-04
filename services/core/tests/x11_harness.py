"""A nested X server the tests can do real input and window management inside.

Extracted from ``test_input_synthesis_x11.py`` when the second file needed it. The
argument for it is the same one that unblocked L11: ``input.*`` and ``window.*`` act on
whatever display they are pointed at, and pointing them at the *session's* display means
seizing the user's keyboard or rearranging the windows of the session doing the work.
Neither task could be tested honestly, and both stayed ``◐`` for a fortnight on that
basis. A display NEXUS owns removes the objection rather than working around it.

``Xephyr`` supplies the display -- its own root window, pointer, keyboard and XTEST --
and ``metacity`` supplies the window manager, which is load-bearing rather than
scenery: without one nothing is ever the *active* window, ``getactivewindow`` has
nothing to name, and neither the credential guard nor ``window.arrange`` has anything
to act on. ``fixtures/x11_probe_app.py`` supplies a window that reports what it
actually received, so a test can assert the effect instead of the exit code.

Everything here reads the nested display by shelling out directly. That is deliberate:
a test that verified ``window.arrange`` by reading the ``after`` dict the tool returned
would be checking the tool against itself.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from nexus.tools import runtime

FIXTURE = Path(__file__).parent / "fixtures" / "x11_probe_app.py"

#: The nested screen. Small on purpose: it is a scratch surface, and a click test that
#: needs to name an off-screen coordinate wants that number to be near.
SCREEN = (800, 600)

#: Xephyr for the display, metacity so something *has* focus, xdotool to send input and
#: read geometry, xprop to read the class the credential guard matches on, xwininfo to
#: confirm the Xephyr window really left the session's screen (see isolate_from_parent).
NEEDS = ("Xephyr", "metacity", "xdotool", "xprop", "xwininfo")


def skip_reason(*also: str) -> str:
    """Why a nested display cannot be built here, or "" if it can.

    Callers pass the binaries they additionally need (``wmctrl`` for window
    management), so a skip names the one thing that is actually missing.
    """
    absent = [name for name in (*NEEDS, *also) if shutil.which(name) is None]
    if absent:
        return f"needs {', '.join(absent)} to build a nested display"
    try:
        import tkinter  # noqa: F401
    except Exception:  # pragma: no cover - depends on the interpreter build
        return "tkinter is missing, so there is no probe window to work with"
    if not os.environ.get("DISPLAY"):
        return "there is no parent DISPLAY for Xephyr to nest inside"
    return ""


def wait(
    predicate: Callable[[], bool],
    what: str,
    seconds: float = 15.0,
    detail: Callable[[], str] | None = None,
) -> None:
    """Poll until true, then fail *naming what was waited for*.

    An X server, a window manager and a Tk window are three asynchronous things. A
    fixed sleep makes this suite either slow or flaky, and a bare assertion after one
    blames the tool for the timing.

    ``detail`` is called only on failure, to say what the display looked like at the
    moment of giving up. Without it every timeout reads the same -- "waited 15s, did
    not happen" -- which names the symptom and hides the cause; with it, a probe that
    died of a Tk error and a window manager that never focused a live window stop being
    indistinguishable.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (OSError, ValueError, KeyError):
            pass  # the state file is being rewritten underneath us
        time.sleep(0.1)
    said = ""
    if detail is not None:
        try:
            said = detail()
        except Exception as exc:  # pragma: no cover - diagnosis must not mask a failure
            said = f"(could not collect diagnostics: {exc!r})"
    pytest.fail(
        f"timed out after {seconds:.0f}s waiting for {what}"
        + (f"\n{said}" if said else "")
    )


def candidate_displays() -> list[str]:
    """Display numbers to try, the least likely to collide first.

    This picked the first number in :90-:99 whose socket file was absent, which is not
    the same question as whether a server is there. Two runs of the same file seconds
    apart both chose :90: the first server was still shutting down, the second run
    connected to it, and its window went with the dying server. The tests that failed
    were arbitrary -- a double click here, a refusal there -- each run took three or
    four times as long as usual because of 15s waits on a window that no longer
    existed, and none of it looked like a display collision. Starting from the process
    id spreads concurrent runs apart, and the caller proves the server is its own.
    """
    first = 90 + os.getpid() % 30
    return [f":{n}" for n in [first, *(n for n in range(90, 120) if n != first)]]


def send(name: str, params: dict[str, Any]) -> Any:
    """Drive a tool the way agent mode does: through the runtime.

    Permission is off deliberately. What is under test is the tool and its guards, and
    routing every keystroke through the engine would re-test the engine while waiting
    on a prompt no one can answer.
    """
    return asyncio.run(runtime.execute(name, params, require_permission=False))


class Nested:
    """A nested X server with a window manager, and the means to look inside it."""

    def __init__(self, display: str, parent: dict[str, str]) -> None:
        self.display = display
        self._parent = parent
        self._procs: list[subprocess.Popen[bytes]] = []
        #: Where each child's output goes. Not /dev/null: a probe app that dies of a
        #: Tk error looks identical, from the outside, to a window manager that never
        #: focused it -- a 15s wait and a failure naming the wrong thing. Keeping the
        #: output means the failure can quote the traceback that caused it.
        self.logs = Path(tempfile.mkdtemp(prefix="nexus-x11-"))

    def spawn(self, args: list[str], *, inside: bool = True) -> subprocess.Popen[bytes]:
        env = dict(self._parent)
        if inside:
            env["DISPLAY"] = self.display
        log = self.logs / f"{Path(args[0]).name}-{len(self._procs)}.log"
        handle = log.open("wb")
        proc = subprocess.Popen(
            args, env=env, stdout=handle, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        proc._nexus_log = log  # type: ignore[attr-defined]
        self._procs.append(proc)
        return proc

    def output_of(self, proc: subprocess.Popen[bytes], limit: int = 1200) -> str:
        """The tail of what a child said, for a failure message that names the cause."""
        log = getattr(proc, "_nexus_log", None)
        if log is None or not Path(log).exists():
            return ""
        return Path(log).read_text(errors="replace")[-limit:].strip()

    def run(self, args: list[str], timeout: float = 5.0) -> tuple[int, str]:
        env = dict(self._parent)
        env["DISPLAY"] = self.display
        try:
            done = subprocess.run(
                args, env=env, capture_output=True, text=True, timeout=timeout
            )
        except FileNotFoundError:
            # A missing binary is a fact worth returning rather than an exception to
            # handle at every call site: the diagnostics below ask for `wmctrl` even in
            # the module that does not require it, and a raise there would throw away
            # the four other things the failure had already collected.
            return 127, f"{args[0]} is not installed"
        return done.returncode, (done.stdout or "").strip()

    def run_parent(self, args: list[str], timeout: float = 5.0) -> tuple[int, str]:
        """The same, on the session's own display -- where the Xephyr window lives."""
        done = subprocess.run(
            args, env=dict(self._parent), capture_output=True, text=True, timeout=timeout
        )
        return done.returncode, (done.stdout or "").strip()

    def active_title(self) -> str:
        code, out = self.run(["xdotool", "getactivewindow", "getwindowname"])
        return out if code == 0 else ""

    def focused_title(self) -> str:
        """The window X would deliver a keystroke to, which is not always the above."""
        code, out = self.run(["xdotool", "getwindowfocus", "getwindowname"])
        return out if code == 0 else ""

    # -------------------------------------------------- independent read-back

    def managed_id(self, title: str) -> str:
        """The window id the *window manager* manages for ``title``.

        Tk wraps its toplevels, so ``winfo_id()`` names a window one level below the
        one metacity frames: the wrapper is what wmctrl lists, what
        ``getactivewindow`` returns and what carries ``_NET_WM_STATE``. Reading state
        from the inner id returns "not found" for every property, which reads exactly
        like a window manager that ignored the request -- twelve tests failed that way
        before this existed.

        Anchored, because ``xdotool search --name`` takes a regular expression: without
        the anchors "Report draft" also matches "Report draft extended", which is the
        very pair one of these tests is about.
        """
        code, out = self.run(["xdotool", "search", "--name", f"^{re.escape(title)}$"])
        ids = out.split() if code == 0 else []
        assert len(ids) == 1, f"expected one window named {title!r}, found {ids}"
        return hex(int(ids[0]))

    def absolute_position(self, window_id: str) -> tuple[int, int]:
        """The window's true origin in root coordinates, from ``xwininfo``.

        Deliberately not ``xdotool getwindowgeometry``: that adds the window's offset
        inside its frame to the translated origin, so it reports a window at 120,90 as
        being at 146,187 (and ``wmctrl -lG`` agrees with it, being wrong the same way).
        This is the reading the rest of the file trusts.
        """
        code, out = self.run(["xwininfo", "-id", str(window_id)])
        assert code == 0, out
        x = re.search(r"Absolute upper-left X:\s+(-?\d+)", out)
        y = re.search(r"Absolute upper-left Y:\s+(-?\d+)", out)
        assert x and y, out
        return int(x.group(1)), int(y.group(1))

    def frame_extents(self, window_id: str) -> tuple[int, int, int, int]:
        """``left, right, top, bottom`` decoration widths the window manager published."""
        code, out = self.run(["xprop", "-id", str(window_id), "_NET_FRAME_EXTENTS"])
        if code != 0 or "=" not in out:
            return 0, 0, 0, 0
        numbers = [int(n) for n in re.findall(r"-?\d+", out.split("=", 1)[1])]
        return tuple(numbers[:4]) if len(numbers) >= 4 else (0, 0, 0, 0)  # type: ignore[return-value]

    def size(self, window_id: str) -> tuple[int, int]:
        """The window's own width and height, from ``xwininfo``."""
        code, out = self.run(["xwininfo", "-id", str(window_id)])
        assert code == 0, out
        width = re.search(r"Width:\s+(\d+)", out)
        height = re.search(r"Height:\s+(\d+)", out)
        assert width and height, out
        return int(width.group(1)), int(height.group(1))

    def wm_state(self, window_id: str) -> str:
        """The raw ``_NET_WM_STATE`` text, so a test can name the flag it wants."""
        code, out = self.run(["xprop", "-id", str(window_id), "_NET_WM_STATE"])
        return out if code == 0 else ""

    def workspace_of(self, title: str) -> int | None:
        """Which desktop wmctrl lists the window on, or None when it is not listed."""
        code, out = self.run(["wmctrl", "-l"])
        if code != 0:
            return None
        for line in out.splitlines():
            parts = line.split(None, 3)
            if len(parts) == 4 and parts[3] == title:
                return int(parts[1])
        return None

    def close(self) -> None:
        for proc in reversed(self._procs):
            proc.terminate()
        deadline = time.monotonic() + 4
        for proc in reversed(self._procs):
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
                proc.kill()
        # And the display's socket has to be gone too, or the *next* module's
        # start_server can be told there is a server on the number and skip it for no
        # visible reason, while a still-vanishing Xephyr window on the parent display
        # makes isolate_from_parent's search come back empty -- which reads as "unsafe
        # to run" and skips a whole module. Both are teardown races this suite created
        # for itself by running module after module against the same parent display.
        socket_file = Path(f"/tmp/.X11-unix/X{self.display[1:]}")
        gone = time.monotonic() + 5
        while time.monotonic() < gone and socket_file.exists():
            time.sleep(0.1)


def isolate_from_parent(server: Nested) -> str:
    """Take the Xephyr window off the session's screen, or say why this is unsafe.

    Xephyr forwards the real mouse and keyboard into the nested display whenever its
    window has them, so with that window on screen the user's own hand is driving the
    display these tests assert about. Measured: **18 pointer jumps in 6 idle seconds**,
    which cost a double click that never arrived, a pointer read 7 pixels from where the
    tool had just put it, and -- earlier, before this was understood -- a probe state
    file recording 11 clicks and the text "dh" that nobody had aimed at it. Iconified,
    the same measurement is 0 drift events, the server still answers ``xdpyinfo`` and
    XTEST still delivers, because an X server is a process rather than a picture.

    Set ``NEXUS_X11_WATCH=1`` to keep the window on screen and watch the tests drive it,
    accepting that your own pointer is then part of the experiment.
    """
    if os.environ.get("NEXUS_X11_WATCH"):
        return ""
    # The window can lag the server by a moment -- immediately after another module's
    # teardown this search has measured empty for ~300ms while the server itself was
    # already answering -- and a single empty read used to skip the whole module for
    # being "unsafe". A window that never appears is genuinely unsafe (nothing to
    # iconify, so the user's hand reaches the display under test); a window that is
    # still mapping is startup, and is waited for.
    ids: list[str] = []
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        code, found = server.run_parent(
            ["xdotool", "search", "--name", f"Xephyr on {server.display}"]
        )
        ids = found.split() if code == 0 else []
        if ids:
            break
        time.sleep(0.1)
    if not ids:
        return (
            f"the Xephyr window for {server.display} could not be found on the parent "
            "display, so real input could leak into it and these tests would be "
            "measuring the user's hand"
        )
    for window_id in ids:
        server.run_parent(["xdotool", "windowminimize", window_id])
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if all(_is_unmapped(server, window_id) for window_id in ids):
            return ""
        time.sleep(0.1)
    # A window manager that ignores iconify requests still cannot argue with unmap.
    for window_id in ids:
        server.run_parent(["xdotool", "windowunmap", window_id])
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if all(_is_unmapped(server, window_id) for window_id in ids):
            return ""
        time.sleep(0.1)
    return (
        "the Xephyr window stayed on screen, so the session's own mouse and keyboard "
        "would reach the display under test"
    )


def _is_unmapped(server: Nested, window_id: str) -> bool:
    code, out = server.run_parent(["xwininfo", "-id", window_id])
    return code != 0 or "IsUnMapped" in out


def start_server() -> Nested:
    """A running Xephyr we can prove is ours, or a failure that says what happened.

    ``Xephyr`` on a display that is already active exits rather than serving, so the
    liveness of the child process is the test of whether the display was really free:
    a dead child means try the next number, and an ``xdpyinfo`` that succeeds against a
    live child means the server answering is the one just started. Waiting on
    ``xdpyinfo`` alone cannot tell those apart -- it succeeds just as happily against
    somebody else's server.
    """
    problems: list[str] = []
    for display in candidate_displays():
        if Path(f"/tmp/.X11-unix/X{display[1:]}").exists():
            continue
        server = Nested(display, dict(os.environ))
        child = server.spawn(
            ["Xephyr", display, "-screen", f"{SCREEN[0]}x{SCREEN[1]}",
             "-ac", "-br", "-noreset"],
            inside=False,
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if child.poll() is not None:
                problems.append(f"{display}: Xephyr exited {child.returncode}")
                break
            if server.run(["xdpyinfo"])[0] == 0:
                return server
            time.sleep(0.1)
        else:
            problems.append(f"{display}: Xephyr did not accept connections within 8s")
        server.close()
    pytest.skip("could not start a nested X server: " + "; ".join(problems[:4]))
    raise AssertionError  # unreachable; keeps the return type honest


@pytest.fixture(scope="module")
def nested() -> Iterator[Nested]:
    """A display of NEXUS's own, torn down with the module that asked for it."""
    server = start_server()
    unsafe = isolate_from_parent(server)
    if unsafe:
        server.close()
        pytest.skip(unsafe)
    server.spawn(["metacity"])
    wait(lambda: server.run(["xprop", "-root", "_NET_SUPPORTING_WM_CHECK"])[0] == 0,
         "a window manager to claim the nested display")

    before = os.environ.get("DISPLAY"), os.environ.get("XDG_SESSION_TYPE")
    os.environ["DISPLAY"] = server.display
    # The nested server is a real X11 server with XTEST no matter what the outer
    # session is, so this is a statement about the display being driven -- not a way
    # around the Wayland refusal, which is about a compositor that would block XTEST.
    os.environ["XDG_SESSION_TYPE"] = "x11"
    try:
        yield server
    finally:
        for name, value in zip(("DISPLAY", "XDG_SESSION_TYPE"), before):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        server.close()


class Probe:
    """A window on the nested display that reports what it actually received."""

    def __init__(self, path: Path, title: str) -> None:
        self.path = path
        self.title = title

    def state(self) -> dict[str, Any]:
        return json.loads(self.path.read_text())

    def window_id(self) -> str:
        """The X id the window itself reports, in the form xdotool and xprop want."""
        return hex(int(self.state()["window_id"]))

    def centre(self, widget: str) -> tuple[int, int]:
        """Where the widget says it is.

        §12 asks for targeting that is not hard-coded pixels. A widget reporting its
        own rectangle stands in for the accessibility API a real application exposes,
        and it means these tests keep working if the window manager places the window
        somewhere else.
        """
        box = self.state()[widget]
        return box["x"] + box["width"] // 2, box["y"] + box["height"] // 2

    def close(self) -> None:
        self.path.with_suffix(".quit").write_text("", encoding="utf-8")


@pytest.fixture
def probe(nested: Nested, tmp_path: Path) -> Iterator[Callable[..., Probe]]:
    """Launch probe windows, and take them down again when the test ends."""
    made: list[tuple[Probe, subprocess.Popen[bytes]]] = []

    def launch(title: str = "NEXUS input probe", wm_class: str | None = None) -> Probe:
        path = tmp_path / f"probe{len(made)}.json"
        args = ["python3", str(FIXTURE), str(path), title]
        if wm_class:
            args.append(wm_class)
        app = nested.spawn(args)
        window = Probe(path, title)

        def diagnose() -> str:
            """What the display looked like when the wait gave up.

            Every one of these waits used to fail with the same sentence, which said
            which condition was unmet and nothing about why. The three causes are
            genuinely different -- the app died, the app is alive but unmanaged, the
            window manager is focusing something else -- and they are told apart by
            exactly this: whether the process is still running, what it printed, and
            what the window manager lists.
            """
            alive = "running" if app.poll() is None else f"exited {app.returncode}"
            lines = [
                f"  probe process: {alive}",
                f"  active window: {nested.active_title()!r}",
                f"  focused window: {nested.focused_title()!r}",
                f"  wmctrl -l: {nested.run(['wmctrl', '-l'])[1]!r}",
                f"  state file: {'present' if path.exists() else 'absent'}",
            ]
            if path.exists():
                lines.append(f"  state: {path.read_text(errors='replace')[:400]}")
            said = nested.output_of(app)
            if said:
                lines.append(f"  probe said:\n{said}")
            return "\n".join(lines)

        wait(path.exists, f"the probe window {title!r} to start", detail=diagnose)
        wait(lambda: nested.active_title() == title, f"{title!r} to take focus",
             detail=diagnose)
        # Being _NET_ACTIVE_WINDOW is the window manager's opinion; having the X input
        # focus is what decides where a keystroke actually lands, and on a freshly
        # started window manager the two lag apart by enough to lose a whole test's
        # typing. Waiting on the second one is what made this file deterministic.
        wait(lambda: nested.focused_title() == title,
             f"{title!r} to hold the input focus", detail=diagnose)
        # Tk reports every widget as 1x1 until the window is mapped and laid out.
        # Clicking then aims at the corner of the frame and hits nothing, which would
        # look exactly like a broken click.
        wait(lambda: window.state()["confirm"]["width"] > 10,
             f"{title!r} to lay itself out", detail=diagnose)
        # And the entry must hold Tk's keyboard focus, which is a separate fact from the
        # window holding X's. When it does not, typing lands nowhere while the tool
        # still correctly reports delivery to the right window -- a failure that reads
        # as a broken keyboard 15 seconds later. The probe app publishes this precisely
        # so it can be waited on rather than assumed.
        wait(lambda: window.state()["focus"] == "field", f"{title!r} to focus its entry",
             detail=diagnose)
        made.append((window, app))
        return window

    yield launch
    for window, app in made:
        window.close()
        # The quit file asks the app to leave on its next 200ms tick, but the window is
        # still mapped -- still findable by title, still able to hold the input focus
        # -- until the process actually exits. The next test launches a window with
        # the same title, and its "take focus" waits are answered just as happily by a
        # dying window as by a live one, which is how one test's teardown became the
        # next test's flake: `managed_id` found two windows named the same thing and
        # asserted on the pair. Waiting for the exit is what stops that.
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and app.poll() is None:
            time.sleep(0.05)
        if app.poll() is None:  # pragma: no cover - a probe that ignored the file
            app.terminate()
