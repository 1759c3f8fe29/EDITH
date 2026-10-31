"""A Chrome/Chromium session NEXUS owns, driven over the DevTools Protocol (C8).

Three decisions define this module, and each is a place where the obvious
implementation is the wrong one.

**It launches its own browser, in its own profile.** The tempting shortcut is to
attach to whatever Chrome is already running with ``--remote-debugging-port``, or
to reuse the user's profile directory. Both are how "summarise this page" turns
into "read every session I am signed in to": a fresh profile has no cookies, no
saved logins and no history, so the blast radius of this tool is exactly that of
a browser the user has never touched. The profile lives under the cache tree,
where the user can delete it wholesale without losing anything NEXUS cannot
regenerate.

**The port is asked for, not assumed.** ``--remote-debugging-port=0`` makes Chrome
choose a free port and write it to ``DevToolsActivePort`` inside the profile,
which avoids colliding with a debugging port the user may already be using. The
file is deleted before launch so a stale one from an earlier crash cannot be read
as this run's port.

**A dead connection is reported, never silently retried.** Chrome drops the
debug connection when a navigation moves the page into a new process, and the
temptation is to reconnect and resend. For ``Runtime.evaluate`` that would be
harmless; for a click it would mean pressing a button twice because the first
press happened to navigate. So a command that fails *mid-flight* is reported as
"this may or may not have happened", and only a connection that is already known
to be dead is replaced before sending.

The ``Origin`` header is deliberately not sent on the WebSocket handshake. Current
Chrome rejects a debug connection whose ``Origin`` it has not been told to allow,
and the fix usually suggested is ``--remote-allow-origins=*`` on the command line
-- which would let *any* page the browser visits talk to the debug port. Sending
no origin satisfies the check without opening that door.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import CACHE_DIR
from ..core.ws_client import WebSocket, WebSocketClosed, WebSocketError
from ..logging_setup import get_logger

log = get_logger("browser.driver")

#: Chromium-family browsers, most preferred first. Firefox is not on this list
#: and cannot be: it does not speak this protocol, and pretending it might would
#: produce a confusing failure rather than an honest refusal.
BROWSER_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "brave-browser",
)

#: Under the cache tree, not the data tree: this directory holds cookies and
#: session state for a throwaway browser, and deleting it must cost the user
#: nothing.
PROFILE_DIR = CACHE_DIR / "browser-profile"
_SESSION_FILE = "nexus-session.json"
_PORT_FILE = "DevToolsActivePort"

#: A browser left open forever is a window the user did not ask for and a process
#: holding memory. Five minutes of no commands is well past any tool sequence.
IDLE_SECONDS = 300.0
LAUNCH_TIMEOUT_S = 25.0
COMMAND_TIMEOUT_S = 30.0

#: Files Chromium writes to claim exclusive use of a profile directory. Cleared
#: only after confirming no live process owns the directory -- deleting them
#: while a browser is running would let two instances share one profile.
_SINGLETON_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")


class BrowserUnavailable(RuntimeError):
    """No browser could be started, or the one that was is no longer usable."""


class ChromeCommandError(RuntimeError):
    """Chrome answered a command with an error."""

    def __init__(self, method: str, error: dict[str, Any]) -> None:
        self.method = method
        self.code = error.get("code")
        self.data = error.get("data", "")
        message = error.get("message", "no message")
        super().__init__(f"Chrome refused {method}: {message}{f' ({self.data})' if self.data else ''}")


@dataclass
class PageTarget:
    id: str
    url: str
    title: str
    ws_url: str


def find_browser() -> str | None:
    """The first Chromium-family browser on PATH, or None."""
    for name in BROWSER_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _signal_group(pid: int, sig: int) -> bool:
    """Signal a process group, falling back to the single process."""
    try:
        os.killpg(pid, sig)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        try:
            os.kill(pid, sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False


def _wait_gone(pid: int, proc: subprocess.Popen[bytes] | None, timeout: float) -> bool:
    """Wait for a process to disappear. Reaps it when it is our own child."""
    if proc is not None:
        try:
            proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return not _pid_alive(pid)


def _terminate(pid: int, proc: subprocess.Popen[bytes] | None) -> str:
    """SIGTERM the browser's whole process group, then SIGKILL what survives.

    Chrome is a tree of renderer and GPU children; signalling only the parent
    leaves the rest running with no window to close them from.
    """
    if not _pid_alive(pid):
        return "already gone"
    if not _signal_group(pid, signal.SIGTERM):
        return "could not be killed"
    if _wait_gone(pid, proc, 4.0):
        return "stopped with SIGTERM"
    if not _signal_group(pid, signal.SIGKILL):
        return "could not be killed"
    if _wait_gone(pid, proc, 4.0):
        return "force-killed with SIGKILL"
    return "still running after SIGKILL"


class ChromeSession:
    """One browser, one page, one debug connection.

    Not thread-safe by construction but *made* safe by :attr:`_lock`, because the
    tool runtime calls into this from worker threads. Every command holds the lock
    for its whole request/response cycle: the protocol multiplexes by id, but this
    client's connection is a single blocking stream, so two threads interleaving
    writes would read each other's replies.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.proc: subprocess.Popen[bytes] | None = None
        self.pid: int | None = None
        self.port: int | None = None
        self.version: str = ""
        self.binary: str = ""
        self.adopted: bool = False
        self._ws: WebSocket | None = None
        self._target: PageTarget | None = None
        self._next_id = 0
        self._last_used = 0.0

    # ------------------------------------------------------------------ state

    @property
    def running(self) -> bool:
        if self.pid is None:
            return False
        if self.proc is not None:
            return self.proc.poll() is None
        return _pid_alive(self.pid)

    def describe(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "pid": self.pid,
            "port": self.port,
            "browser": self.version,
            "binary": self.binary,
            "profile": str(PROFILE_DIR),
            "page": self._target.url if self._target else None,
            "adopted": self.adopted,
        }

    def touch(self) -> None:
        self._last_used = time.monotonic()

    def maybe_reap(self) -> bool:
        """Stop an idle browser. Returns True when one was stopped."""
        with self._lock:
            if not self.running or self._last_used == 0.0:
                return False
            if time.monotonic() - self._last_used < IDLE_SECONDS:
                return False
            log.info("browser idle for %.0fs; stopping it", time.monotonic() - self._last_used)
            self.close()
            return True

    # --------------------------------------------------------------- starting

    def start(self) -> None:
        """Launch the browser if it is not already running, then attach to a page."""
        with self._lock:
            if self.running:
                self.touch()
                return
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            self._recover_previous()
            if self.running:
                # An earlier NEXUS left its browser open. Adopt it rather than
                # starting a second one: Chromium refuses to share a profile, so
                # launching here would fail with a confusing "profile in use".
                self._read_version()
                self._attach()
                self._wait_ready()
                self._write_session_file()
                self.touch()
                return

            binary = find_browser()
            if binary is None:
                raise BrowserUnavailable(
                    "No Chromium-based browser is installed, so NEXUS cannot drive a "
                    "browser. Install one of: "
                    + ", ".join(BROWSER_CANDIDATES)
                    + ". (NEXUS cannot use Firefox for this -- it does not speak the "
                    "Chrome DevTools Protocol.)"
                )
            self.binary = binary
            self._clear_stale_profile()
            self._launch(binary)
            self._await_port()
            self._read_version()
            self._attach()
            self._wait_ready()
            self._write_session_file()
            self.touch()

    def _launch(self, binary: str) -> None:
        # No --headless: the user asked for an assistant that can see the screen,
        # and a visible window is also the honest one -- screenshots then agree
        # with what the user is looking at, and a task can be watched as it runs.
        flags = [
            binary,
            "--remote-debugging-port=0",
            f"--user-data-dir={PROFILE_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-features=Translate,MediaRouter",
            # Keep Chrome out of the login keyring.
            #
            # By default Chrome keeps its cookie and password encryption key in the
            # Secret Service (gnome-keyring or kwallet, over D-Bus). When that
            # collection is locked or absent, the call does not fail -- it never
            # returns, and it blocks Chrome's IO thread, so *every* command after it
            # gets no reply, including the one that triggered it. Measured here:
            # without this flag `Page.navigate` to about:blank answered instantly
            # and to https://example.com never answered at all, and the next
            # Runtime.evaluate hung too; with it, the same navigation completes in
            # about 0.4s. `basic` writes the key into the profile directory instead.
            # Wrong for a person's real browser; right for a throwaway automation
            # profile that must not reach into their keyring in the first place --
            # which is the same bargain the dedicated profile above already makes.
            "--password-store=basic",
            "--window-size=1280,900",
            "about:blank",
        ]
        try:
            self.proc = subprocess.Popen(
                flags,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Own process group so shutdown kills the renderers too.
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise BrowserUnavailable(f"Could not start {binary}: {exc}") from exc
        self.pid = self.proc.pid
        self.adopted = False

    def _await_port(self) -> None:
        """Wait for Chrome to publish the port it chose, or report why it did not."""
        port_file = PROFILE_DIR / _PORT_FILE
        deadline = time.monotonic() + LAUNCH_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                code = self.proc.returncode
                hint = ""
                if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
                    hint = (
                        " There is no DISPLAY, so a visible browser window cannot open; "
                        "this happens when NEXUS is started outside a desktop session."
                    )
                raise BrowserUnavailable(
                    f"{self.binary} exited immediately with code {code} instead of "
                    f"starting.{hint} Its own output was discarded, so there is no "
                    "further detail to report."
                )
            if port_file.exists():
                try:
                    lines = port_file.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    lines = []
                if lines and lines[0].strip().isdigit():
                    self.port = int(lines[0].strip())
                    return
            time.sleep(0.1)
        self.close()
        raise BrowserUnavailable(
            f"{self.binary} did not publish a debugging port within "
            f"{LAUNCH_TIMEOUT_S:.0f} seconds, so NEXUS could not attach to it."
        )

    def _opener(self) -> urllib.request.OpenerDirector:
        # Explicitly no proxies. A user with http_proxy set would otherwise have
        # every call to 127.0.0.1 routed through it, which either fails or --
        # worse -- reaches a proxy that logs the DevTools URLs.
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _http_json(self, path: str, *, method: str = "GET", timeout: float = 5.0) -> Any:
        if self.port is None:
            raise BrowserUnavailable("The browser is not running.")
        url = f"http://127.0.0.1:{self.port}{path}"
        request = urllib.request.Request(url, method=method)
        try:
            with self._opener().open(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise BrowserUnavailable(
                f"The browser's debug endpoint answered {exc.code} for {path}."
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BrowserUnavailable(f"The browser's debug endpoint is not reachable: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise BrowserUnavailable(f"The browser's debug endpoint sent unreadable JSON: {exc}") from exc

    def _read_version(self) -> None:
        info = self._http_json("/json/version")
        self.version = str(info.get("Browser", "unknown browser"))

    def _page_target(self) -> PageTarget:
        targets = self._http_json("/json/list")
        if not isinstance(targets, list):
            raise BrowserUnavailable("The browser returned no target list.")
        pages = [
            t for t in targets
            if isinstance(t, dict) and t.get("type") == "page" and t.get("webSocketDebuggerUrl")
        ]
        if not pages:
            raise BrowserUnavailable(
                "The browser is running but has no page to drive. This is unusual; "
                "closing the browser window by hand causes it."
            )
        chosen = pages[0]
        return PageTarget(
            id=str(chosen.get("id", "")),
            url=str(chosen.get("url", "")),
            title=str(chosen.get("title", "")),
            ws_url=str(chosen["webSocketDebuggerUrl"]),
        )

    def _attach(self) -> None:
        """Connect the debug socket to the page target."""
        target = self._page_target()
        try:
            ws = WebSocket.connect(target.ws_url, timeout=COMMAND_TIMEOUT_S)
        except WebSocketError as exc:
            raise BrowserUnavailable(
                f"Could not open a debug connection to the browser page: {exc}"
            ) from exc
        self._ws = ws
        self._target = target
        self._next_id = 0

    def _wait_ready(self) -> None:
        """Wait until the page actually answers a command, not merely until Chrome
        has opened a socket.

        Chrome writes ``DevToolsActivePort`` as soon as the browser process is
        listening, and ``/json/list`` then reports the ``about:blank`` tab it was
        launched with -- while a command that has to reach the *renderer* is queued
        behind a renderer that may not have started yet. The WebSocket connects to
        the browser process either way, so the connection looks healthy right up
        until the first real command, which then gets no reply.

        ``Runtime.evaluate`` is the probe because it must reach the renderer, which
        is the process whose absence would cause that.

        **This is not what made ``browser.navigate`` fail.** It was written for the
        race above, and the failure that looked like the race had a different cause
        entirely -- see ``--password-store`` in :meth:`_launch`. The wait is kept
        because it is bounded, returns on the first success, and turns "the first
        tool call of the session failed for no visible reason" into either a
        working call or a sentence naming the browser as the thing that is broken.
        """
        deadline = time.monotonic() + LAUNCH_TIMEOUT_S
        last = "it did not answer at all"
        while time.monotonic() < deadline:
            try:
                self.command(
                    "Runtime.evaluate",
                    {"expression": "1", "returnByValue": True},
                    timeout=3.0,
                )
                return
            except ChromeCommandError:
                # The renderer answered, and said this command was wrong. That is
                # proof the channel works, which is the only thing being asked.
                return
            except BrowserUnavailable as exc:
                last = str(exc)
                time.sleep(0.25)
        self.close()
        raise BrowserUnavailable(
            f"{self.binary or 'The browser'} opened its debugging port but its page "
            f"never became able to run a command within {LAUNCH_TIMEOUT_S:.0f} seconds "
            f"({last}). The browser may be too damaged or too slow to drive on this "
            "machine right now."
        )

    # ---------------------------------------------------------------- commands

    def command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = COMMAND_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Send one CDP command and return its result.

        Reconnects only when the connection is *already* dead. A command that
        fails after being sent is reported as unknown, never retried: see the
        module docstring.
        """
        with self._lock:
            if not self.running:
                raise BrowserUnavailable("The browser is not running.")
            if self._ws is None or self._ws.closed:
                self._attach()
            assert self._ws is not None

            self.touch()
            self._next_id += 1
            message_id = self._next_id
            payload = json.dumps({"id": message_id, "method": method, "params": params or {}})

            try:
                self._ws.send_text(payload)
            except WebSocketError as exc:
                self._ws = None
                raise BrowserUnavailable(
                    f"The debug connection failed while sending {method}: {exc}"
                ) from exc

            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BrowserUnavailable(
                        f"Chrome did not answer {method} within {timeout:.0f} seconds."
                    )
                try:
                    raw = self._ws.recv(timeout=remaining)
                except socket.timeout as exc:
                    raise BrowserUnavailable(
                        f"Chrome did not answer {method} within {timeout:.0f} seconds."
                    ) from exc
                except (WebSocketClosed, WebSocketError) as exc:
                    self._ws = None
                    raise BrowserUnavailable(
                        f"The debug connection dropped while {method} was in flight: {exc} "
                        "The command may or may not have taken effect, so NEXUS will not "
                        "repeat it."
                    ) from exc

                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    # Chrome does not send malformed frames; if one arrives, skipping
                    # it silently would hide a real protocol problem.
                    raise BrowserUnavailable(
                        f"Chrome sent something that is not a JSON message: {raw[:200]!r}"
                    ) from None

                if message.get("id") != message_id:
                    # An unsolicited event, or a late reply to a timed-out command.
                    continue
                if "error" in message:
                    raise ChromeCommandError(method, message["error"])
                result = message.get("result")
                return result if isinstance(result, dict) else {}

    def evaluate(
        self,
        expression: str,
        *,
        await_promise: bool = False,
        timeout: float = COMMAND_TIMEOUT_S,
    ) -> Any:
        """Run JavaScript in the page and return the value.

        ``returnByValue`` makes Chrome serialise the result as JSON, so a page's
        objects arrive as plain Python data. A value Chrome cannot serialise (a
        DOM node, a function) comes back as ``None`` with the type in ``_type``
        -- the caller asked for a value and there is not one, which is reported
        rather than guessed.
        """
        result = self.command(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            text = (
                details.get("exception", {}).get("description")
                or details.get("text")
                or "the page script threw"
            )
            raise BrowserUnavailable(f"The page script failed: {text}")
        inner = result.get("result", {})
        if "value" in inner:
            return inner["value"]
        return None

    def goto(self, url: str, *, timeout: float = 25.0) -> dict[str, Any]:
        """Navigate the page and wait for it to finish loading.

        Readiness is polled through ``document.readyState`` rather than by
        subscribing to ``Page.loadEventFired``: a page that is already loaded
        never fires the event again, and every navigation here is followed by
        reading the DOM back anyway, so one extra round trip is cheaper than an
        event subscription that can be missed.
        """
        result = self.command("Page.navigate", {"url": url}, timeout=timeout)
        error_text = result.get("errorText")
        if error_text:
            raise BrowserUnavailable(f"The browser refused to open {url}: {error_text}")

        deadline = time.monotonic() + timeout
        state = ""
        while time.monotonic() < deadline:
            try:
                state = str(self.evaluate("document.readyState", timeout=5.0) or "")
            except BrowserUnavailable:
                # Mid-navigation the execution context is torn down and recreated,
                # which surfaces as an error. Waiting is the correct response.
                state = ""
            if state == "complete":
                break
            time.sleep(0.15)

        return {
            "requested_url": url,
            "frame_id": result.get("frameId"),
            "ready_state": state or "unknown",
            "completed": state == "complete",
        }

    # ---------------------------------------------------------------- teardown

    def close(self) -> dict[str, Any]:
        """Close the debug connection and stop the browser."""
        with self._lock:
            if self._ws is not None:
                try:
                    self._ws.close()
                except Exception:  # noqa: BLE001 - teardown must not raise
                    pass
                self._ws = None
            self._target = None

            detail = "was not running"
            if self.pid is not None and self.running:
                detail = _terminate(self.pid, self.proc)

            stopped_pid = self.pid
            self.proc = None
            self.pid = None
            self.port = None
            self.version = ""
            self._last_used = 0.0
            self._clear_session_file()
            return {"stopped": stopped_pid is not None, "pid": stopped_pid, "detail": detail}

    # ------------------------------------------------- profile bookkeeping

    def _write_session_file(self) -> None:
        try:
            (_SESSION_FILE_PATH()).write_text(
                json.dumps({"pid": self.pid, "port": self.port, "binary": self.binary}),
                encoding="utf-8",
            )
        except OSError as exc:
            # Not fatal: the browser works, NEXUS just cannot adopt it after a
            # restart and will start a fresh one instead.
            log.warning("could not record the browser session: %s", exc)

    def _clear_session_file(self) -> None:
        try:
            _SESSION_FILE_PATH().unlink(missing_ok=True)
        except OSError:
            pass

    def _recover_previous(self) -> None:
        """Adopt a browser left running by an earlier NEXUS, or clean up after one.

        A core that restarts while its browser is still open would otherwise
        either leak that browser forever or start a second one on the same
        profile, which Chromium refuses to do.
        """
        path = _SESSION_FILE_PATH()
        if not path.exists():
            return
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._clear_session_file()
            return

        pid = record.get("pid")
        if isinstance(pid, int) and _pid_alive(pid):
            # The process exists, but that does not mean it is the browser: pids
            # are recycled. Prove it by asking the recorded port for its version,
            # and treat "answers" as proof rather than assumption.
            port = record.get("port")
            if isinstance(port, int):
                self.pid = pid
                self.port = port
                self.proc = None
                self.adopted = True
                try:
                    self._read_version()
                except BrowserUnavailable:
                    log.info("recorded browser pid %s is not answering; stopping it", pid)
                    _terminate(pid, None)
                    self.pid = None
                    self.port = None
                    self.adopted = False
            else:
                _terminate(pid, None)
        self._clear_session_file()

    def _clear_stale_profile(self) -> None:
        """Remove Chromium's profile locks left behind by a crash.

        Safe because this runs only after :meth:`_recover_previous` has confirmed
        that no live process owns the directory.
        """
        for name in (*_SINGLETON_FILES, _PORT_FILE):
            try:
                (PROFILE_DIR / name).unlink(missing_ok=True)
            except OSError:
                pass


def _SESSION_FILE_PATH() -> Path:
    return PROFILE_DIR / _SESSION_FILE


# ------------------------------------------------------------------- singleton

_SESSION: ChromeSession | None = None
_SESSION_LOCK = threading.Lock()


def session() -> ChromeSession:
    """The one browser session for this process.

    Started lazily by the tools that need it, so importing NEXUS never opens a
    browser the user did not ask for.
    """
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            _SESSION = ChromeSession()
        _SESSION.maybe_reap()
        return _SESSION


def shutdown() -> None:
    """Stop the browser. Registered with atexit so a core exit does not leak one."""
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is not None and _SESSION.running:
            try:
                _SESSION.close()
            except Exception as exc:  # noqa: BLE001 - never block interpreter exit
                log.warning("could not close the browser cleanly: %s", exc)


atexit.register(shutdown)
