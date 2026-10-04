"""Window and focus monitor (task L3; spec §36, §44, §66).

Follows which window has focus, and turns that into a timeline with real
durations. This is what makes "what was I working on this afternoon" answerable
from records instead of from guesswork.

Window titles are content -- a document name, a message subject, a URL. So this
monitor is off by default, and the title of the *current* window is only ever
reported while it is running. Nothing is reconstructed after the fact from
somewhere else.

Focus is read through ``xdotool`` when present and ``xprop`` otherwise, both of
which are X11-only. On a Wayland session neither can see the focused window, and
this monitor says so and stops rather than reporting an empty timeline that looks
like an idle afternoon (§78).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..logging_setup import get_logger
from .base import Monitor, MonitorUnavailable, Observation

log = get_logger("monitors.window")

#: Focus changes are cheap to observe and numerous; the timeline keeps a bounded
#: window of them in memory and the ambient store (L9) keeps the rest.
SESSION_BUFFER = 500

#: A focus that lasted less than this is a pass-through -- alt-tabbing across a
#: window on the way somewhere else. Recorded in totals, not as a timeline entry.
MIN_SESSION_SECONDS = 2.0

_HEX_ID = re.compile(r"(0x[0-9a-fA-F]+)")
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')


@dataclass
class FocusSession:
    """One continuous period a window held focus."""

    app: str
    title: str
    window_id: str
    pid: int | None
    started_at: str
    started_monotonic: float
    ended_at: str | None = None
    seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "app": self.app,
            "title": self.title,
            "window_id": self.window_id,
            "pid": self.pid,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "seconds": round(self.seconds, 1),
        }


@dataclass
class _Focus:
    window_id: str
    title: str
    app: str
    pid: int | None = None

    def same_window(self, other: "_Focus | None") -> bool:
        return other is not None and other.window_id == self.window_id


def _run(command: list[str], timeout: float = 3.0) -> str | None:
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _via_xdotool() -> _Focus | None:
    window_id = _run(["xdotool", "getactivewindow"])
    if not window_id or not window_id.strip():
        return None
    wid = window_id.strip()

    title = (_run(["xdotool", "getwindowname", wid]) or "").strip()
    app = (_run(["xdotool", "getwindowclassname", wid]) or "").strip()
    pid_raw = (_run(["xdotool", "getwindowpid", wid]) or "").strip()
    pid = int(pid_raw) if pid_raw.isdigit() else None

    return _Focus(window_id=wid, title=title, app=app or _app_from_pid(pid) or "unknown", pid=pid)


def _via_xprop() -> _Focus | None:
    root = _run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
    if not root:
        return None
    match = _HEX_ID.search(root)
    if not match:
        return None
    wid = match.group(1)
    if int(wid, 16) == 0:
        # No window has focus -- the desktop itself, or a session just unlocked.
        return None

    props = _run(["xprop", "-id", wid, "_NET_WM_NAME", "WM_NAME", "WM_CLASS", "_NET_WM_PID"])
    if props is None:
        return None

    title = ""
    app = ""
    pid: int | None = None
    for line in props.splitlines():
        if line.startswith(("_NET_WM_NAME", "WM_NAME")) and not title:
            found = _QUOTED.search(line)
            if found:
                title = found.group(1)
        elif line.startswith("WM_CLASS"):
            classes = _QUOTED.findall(line)
            if classes:
                # WM_CLASS is (instance, class); the class is the friendlier name.
                app = classes[-1]
        elif line.startswith("_NET_WM_PID"):
            digits = re.search(r"(\d+)", line)
            if digits:
                pid = int(digits.group(1))

    return _Focus(
        window_id=wid,
        title=title,
        app=app or _app_from_pid(pid) or "unknown",
        pid=pid,
    )


def _app_from_pid(pid: int | None) -> str | None:
    if not pid:
        return None
    try:
        with open(f"/proc/{pid}/comm", "r", encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


class WindowMonitor(Monitor):
    id = "window"
    title = "Window focus"
    description = (
        "Follows which application has focus so NEXUS can tell you what you were "
        "working on and for how long."
    )
    enabled_setting = "monitor_window_enabled"
    interval_setting = "monitor_window_interval_seconds"
    default_interval = 5.0
    capability = "window.list"
    activity_kind = "application"
    # A focus change every few seconds would flood the Activity log. The timeline
    # keeps them; Activity gets only what the proactive engine escalates.
    record_to_activity = False

    def __init__(self) -> None:
        super().__init__()
        self._current: FocusSession | None = None
        self._sessions: deque[FocusSession] = deque(maxlen=SESSION_BUFFER)
        self._usage_seconds: dict[str, float] = {}
        self._reader = None  # set in check_available

    # -------------------------------------------------------------- availability

    def check_available(self) -> None:
        session_type = (os.environ.get("XDG_SESSION_TYPE") or "").lower()
        has_display = bool(os.environ.get("DISPLAY"))

        if shutil.which("xdotool"):
            self._reader = _via_xdotool
        elif shutil.which("xprop"):
            self._reader = _via_xprop
        else:
            raise MonitorUnavailable(
                "Neither xdotool nor xprop is installed, so NEXUS cannot tell which "
                "window has focus. Installing xdotool would enable this."
            )

        if session_type == "wayland" or not has_display:
            raise MonitorUnavailable(
                "This looks like a Wayland session, where X11 tools cannot read the "
                "focused window. NEXUS would report an empty timeline rather than a "
                "real one, so window tracking stays off."
            )

    # -------------------------------------------------------------------- sample

    async def sample(self) -> list[Observation]:
        if self._reader is None:  # pragma: no cover - start always sets it
            self.check_available()

        focus = self._reader()  # type: ignore[misc]
        now_mono = time.monotonic()

        if focus is None:
            # Nothing focused: end the current session so its duration is real
            # rather than continuing to accrue while the desktop is empty.
            closed = self._close_current(now_mono)
            return [self._changed(closed, None)] if closed else []

        if self._current and focus.window_id == self._current.window_id:
            if focus.title != self._current.title:
                # Same window, new title -- a different tab or document. Worth
                # noticing, but not a new focus session.
                previous_title = self._current.title
                self._current.title = focus.title
                return [
                    Observation(
                        kind="window.title",
                        summary=f"{self._current.app}: {focus.title or 'untitled'}",
                        data={
                            "app": self._current.app,
                            "title": focus.title,
                            "previous_title": previous_title,
                            "window_id": focus.window_id,
                        },
                        dedupe_key=f"title:{focus.window_id}:{focus.title}",
                    )
                ]
            return []

        closed = self._close_current(now_mono)
        self._current = FocusSession(
            app=focus.app,
            title=focus.title,
            window_id=focus.window_id,
            pid=focus.pid,
            started_at=datetime.now(timezone.utc).isoformat(),
            started_monotonic=now_mono,
        )
        return [self._changed(closed, self._current)]

    async def on_stop(self) -> None:
        self._close_current(time.monotonic())

    # ------------------------------------------------------------------ helpers

    def _close_current(self, now_mono: float) -> FocusSession | None:
        session = self._current
        self._current = None
        if session is None:
            return None

        session.seconds = now_mono - session.started_monotonic
        session.ended_at = datetime.now(timezone.utc).isoformat()
        # Totals count every second, including short passes: time spent is time
        # spent. The timeline drops the noise.
        self._usage_seconds[session.app] = (
            self._usage_seconds.get(session.app, 0.0) + session.seconds
        )
        if session.seconds >= MIN_SESSION_SECONDS:
            self._sessions.append(session)
        return session

    def _changed(
        self, closed: FocusSession | None, opened: FocusSession | None
    ) -> Observation:
        if opened and closed:
            summary = (
                f"Switched to {opened.app} after {self._duration(closed.seconds)} "
                f"in {closed.app}"
            )
        elif opened:
            summary = f"Focused {opened.app}"
        else:
            summary = f"Left {closed.app} after {self._duration(closed.seconds)}"  # type: ignore[union-attr]

        return Observation(
            kind="window.focus",
            summary=summary,
            data={
                "from": closed.to_dict() if closed else None,
                "to": opened.to_dict() if opened else None,
            },
            # Deduped per target window: alt-tabbing back and forth between two
            # windows is one situation, not twenty events.
            dedupe_key=f"focus:{opened.window_id if opened else 'none'}",
        )

    @staticmethod
    def _duration(seconds: float) -> str:
        if seconds < 60:
            return f"{seconds:.0f}s"
        if seconds < 3600:
            return f"{seconds / 60:.0f}m"
        return f"{seconds / 3600:.1f}h"

    # ------------------------------------------------------------------- queries

    def timeline(self, limit: int = 50) -> list[dict[str, Any]]:
        """Recent focus sessions, newest first, including the open one."""
        entries: list[dict[str, Any]] = []
        if self._current is not None:
            live = self._current.to_dict()
            live["seconds"] = round(time.monotonic() - self._current.started_monotonic, 1)
            live["ongoing"] = True
            entries.append(live)
        entries.extend(reversed([s.to_dict() for s in self._sessions]))
        return entries[:limit]

    def usage(self) -> list[dict[str, Any]]:
        """Time per application since the monitor started, most used first."""
        totals = dict(self._usage_seconds)
        if self._current is not None:
            totals[self._current.app] = totals.get(self._current.app, 0.0) + (
                time.monotonic() - self._current.started_monotonic
            )
        return [
            {"app": app, "seconds": round(seconds, 1), "human": self._duration(seconds)}
            for app, seconds in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        ]

    def current(self) -> dict[str, Any] | None:
        if self._current is None:
            return None
        data = self._current.to_dict()
        data["seconds"] = round(time.monotonic() - self._current.started_monotonic, 1)
        return data
