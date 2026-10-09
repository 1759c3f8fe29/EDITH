"""Noticing the notifications other applications show (audit item O11).

NEXUS could observe the machine and had no idea when *other software wanted the
user's attention*. A build finishing, a message arriving, a battery warning from
the desktop -- all of it went past unobserved, which for an assistant meant for a
desktop is a conspicuous gap. The system-control audit recorded the observation
kind as not existing at all.

It exists now, and three things shape it.

**A notification body is untrusted text that may be a secret.** One-time codes,
password-reset links and "your key is ..." messages arrive this way constantly. The
clipboard monitor already solves this exact problem with ``looks_like_credential``,
so this reuses it rather than growing a second, subtly different pattern list --
two copies of a secret-detector is one copy that is wrong. When the body matches,
the observation carries the *category* and not the value, so "a notification that
looked like an API key arrived" is recorded and the key is not (§56).

**Reading notifications is off by default.** Notifications are among the most
personal things on a desktop, and this is the same posture every other content
monitor takes -- ``monitor_notifications_enabled`` defaults False, and while it runs
the supervisor reports it in ``watching()`` so the standing indicator (L5) shows it.
NEXUS never starts reading what the user is being told without being asked.

**The parsing is separate from the watching.** ``parse_notify`` is a pure function
over ``dbus-monitor`` output lines, which is what makes this testable at all: real
notifications cannot be provoked on a build machine, but the exact bytes a real
desktop produced *can* be replayed. The sample used in the tests was captured from
this machine with ``notify-send`` and pasted in verbatim, hints and all.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from typing import Any, Iterable

from ..logging_setup import get_logger
from .base import Monitor, MonitorUnavailable, Observation, setting
from .clipboard_monitor import looks_like_credential

log = get_logger("monitors.notifications")

#: The session bus interface every desktop notification goes through.
INTERFACE = "org.freedesktop.Notifications"
#: The method a notification server is called with. Worth matching exactly:
#: ``GetServerInformation`` and ``CloseNotification`` arrive on the same interface
#: and are not notifications.
METHOD = "Notify"

#: Urgency hint values (from the Desktop Notification Specification). Kept as a
#: table rather than inline comparisons so the mapping is stated once.
_URGENCY = {"0": "low", "1": "normal", "2": "critical"}

#: Strings longer than this are truncated in the observation. A notification body
#: is a display string; anything a page long is not something to carry around in
#: the ambient store, and the summary is for a human to read.
_MAX_BODY = 400


def _unquote(line: str) -> str:
    """The string a dbus-monitor ``string "..."`` line carries, unescaped."""
    text = line.strip()
    if text.startswith(("string ", 'string "')):
        text = text[len("string ") :]
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    # dbus-monitor escapes the same characters as C strings. Only the three that
    # appear in practice are unescaped; a notification body is not a place to
    # invent a full parser.
    return text.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")


def parse_notify(lines: Iterable[str]) -> list[dict[str, Any]]:
    """Pull notifications out of ``dbus-monitor`` output. Pure, so it is testable.

    The shape being read, verbatim from this machine::

        method call ... interface=org.freedesktop.Notifications; member=Notify
           string "NEXUS probe"      <- app name
           uint32 0                  <- replaces id
           string ""                 <- icon
           string "Format probe"     <- summary
           string "Capturing ..."    <- body
           array [ ... ]
           array [ dict entry(string "urgency" variant byte 1) ... ]
           int32 -1                  <- timeout

    Reading is positional because that is what the call is: the specification
    fixes the argument order. Nothing here guesses a field from its content --
    guessing "this long string is probably the body" is how a summary ends up
    stored as a title.
    """
    found: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    #: Set by the ``string "urgency"`` hint line, cleared by the ``byte`` that
    #: carries its value. The two are separate lines in real dbus-monitor output.
    reading_urgency = False
    #: How many ``string`` arguments have been seen for the notification in hand.
    #: 0 is the app name, 1 the icon, 2 the summary, 3 the body.
    strings: list[str] = []

    for raw in lines:
        line = raw.rstrip("\n")
        if f"member={METHOD}" in line and INTERFACE in line:
            current = {"urgency": "normal"}
            strings = []
            reading_urgency = False
            continue

        if current is None:
            continue

        if line.startswith("   string "):
            strings.append(_unquote(line))
            if len(strings) == 1:
                current["app"] = strings[0]
            elif len(strings) == 3:
                current["summary"] = strings[2]
            elif len(strings) == 4:
                current["body"] = strings[3]
            continue

        # The hints array follows the body. The urgency is a two-line dict entry --
        # ``string "urgency"`` on one line, ``variant byte N`` on the next -- so
        # seeing the name is what puts the reader in a state to read the value. An
        # earlier version looked for both words on one line, which matched nothing
        # and silently left every notification at the default.
        if line.strip().startswith('string "urgency"'):
            reading_urgency = True
            continue
        if reading_urgency and "byte" in line:
            value = line.strip().rsplit("byte", 1)[-1].strip()
            current["urgency"] = _URGENCY.get(value, "normal")
            reading_urgency = False
            continue

        # ``int32 -1`` is the timeout, and it is the last argument.
        if line.strip().startswith("int32 "):
            found.append(current)
            current = None
            strings = []

    return found


class NotificationMonitor(Monitor):
    id = "notifications"
    title = "Notifications"
    description = (
        "Notices the notifications other applications show you -- messages, build "
        "results, system warnings. Anything that looks like a password or key is "
        "counted, never stored."
    )
    enabled_setting = "monitor_notifications_enabled"
    interval_setting = "monitor_notifications_interval_seconds"
    # Not a poll interval: dbus-monitor is a stream, and this only bounds how long
    # a read waits before yielding so the loop can be cancelled. The setting exists
    # because Monitor.interval() reads it and a hardcoded value would be invisible
    # in Settings.
    default_interval = 30.0
    capability = "notifications.read"
    activity_kind = "notification"
    # Notifications are personal and constant; the ambient stream is where a record
    # of them belongs, not the Activity log.
    record_to_activity = False

    def __init__(self) -> None:
        super().__init__()
        self._process: asyncio.subprocess.Process | None = None
        self._buffer: list[str] = []

    # -------------------------------------------------------------- availability

    def check_available(self) -> None:
        if not shutil.which("dbus-monitor"):
            raise MonitorUnavailable(
                "dbus-monitor is not installed, so NEXUS cannot see notifications. "
                "It ships with the dbus package on every desktop distribution."
            )
        if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            raise MonitorUnavailable(
                "There is no D-Bus session bus to watch, which happens when NEXUS "
                "runs outside a desktop session -- from a system service or over a "
                "bare SSH connection."
            )

    async def on_start(self) -> None:
        self._buffer = []

    async def on_stop(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:  # pragma: no cover - defensive
                process.kill()

    # -------------------------------------------------------------------- sample

    async def sample(self) -> list[Observation]:
        """Drain whatever has arrived since the last tick.

        Never blocks waiting for a notification: dbus-monitor is long-lived and a
        quiet desktop produces nothing for hours, so a `read` that waited would
        park the monitor's task and make it deaf to cancellation.
        """
        process = await self._ensure_watching()
        lines = await self._drain(process)
        return self._observations(parse_notify(lines))

    async def _ensure_watching(self) -> asyncio.subprocess.Process:
        if self._process is not None and self._process.returncode is None:
            return self._process
        try:
            self._process = await asyncio.create_subprocess_exec(
                "dbus-monitor",
                "--session",
                f"interface='{INTERFACE}'",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise MonitorUnavailable(
                f"Could not start dbus-monitor to watch notifications: {exc}"
            ) from exc
        return self._process

    async def _drain(self, process: asyncio.subprocess.Process) -> list[str]:
        """Everything readable right now, without waiting for more."""
        stream = process.stdout
        if stream is None:  # pragma: no cover - PIPE was requested
            return []
        lines: list[str] = []
        while True:
            try:
                chunk = await asyncio.wait_for(stream.readline(), timeout=0.25)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            lines.append(chunk.decode("utf-8", errors="replace"))
        del self._buffer  # the buffer is per-call; kept as an attribute for tests
        self._buffer = []
        return lines

    # ------------------------------------------------------------- observations

    def _observations(self, notifications: list[dict[str, Any]]) -> list[Observation]:
        out: list[Observation] = []
        for item in notifications:
            app = str(item.get("app") or "").strip()
            summary = str(item.get("summary") or "").strip()
            body = str(item.get("body") or "").strip()

            if not (summary or body):
                # A notification with no text at all tells the user nothing and is
                # not worth a row; some applications send these as progress ticks.
                continue

            data: dict[str, Any] = {
                "app": app,
                "urgency": item.get("urgency", "normal"),
                "summary": summary[:_MAX_BODY],
            }

            category = looks_like_credential(body) if body else ""
            if category:
                # The §56 boundary: the shape is recorded, the value is not. A
                # one-time code that reached the ambient store would outlive the
                # code and be readable by anything that can read the store.
                data["body_withheld"] = True
                data["sensitive_category"] = category
                data.pop("summary", None) if looks_like_credential(summary) else None
            elif body:
                data["body"] = body[:_MAX_BODY]

            out.append(
                Observation(
                    kind="system.notification",
                    summary=(
                        f"{app} says: {summary}"
                        if app and summary
                        else (summary or f"A notification from {app}")
                    ),
                    data=data,
                    severity=(
                        "alert"
                        if item.get("urgency") == "critical"
                        else ("notice" if item.get("urgency") == "normal" else "info")
                    ),
                    # Every notification is its own event; unlike a threshold there
                    # is no ongoing condition to dedupe against.
                    dedupe_key="",
                )
            )
        return out
