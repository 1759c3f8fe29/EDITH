"""Application control tools (task D3; spec §35, §55, §60).

Application discovery reads real ``.desktop`` entries from the XDG application
directories, so the list matches what the user's launcher shows -- there is no
hardcoded table of "supported apps".

Window operations use ``wmctrl``. When it is absent the tools report themselves
unavailable with the package name to install, rather than silently degrading
(§78, §100): a tool that says "done" without a window manager to act on would be
the worst possible outcome.

Launch verification polls for a real process, or a real new window, for up to a
few seconds. "The launcher command exited 0" is not evidence that an application
started, so it is not treated as such (§60).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from .registry import ToolResult, tool

log = get_logger("tools.apps")

# Field codes in a .desktop Exec line (%f, %U, ...) are placeholders the launcher
# substitutes. Passing them through literally makes the app open a file named "%U".
_FIELD_CODES = re.compile(r"%[fFuUdDnNickvm]")


def _app_dirs() -> list[Path]:
    dirs = [Path.home() / ".local" / "share" / "applications"]
    xdg_data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    dirs.extend(Path(p) / "applications" for p in xdg_data_dirs.split(":") if p)
    dirs.append(Path("/var/lib/flatpak/exports/share/applications"))
    dirs.append(Path.home() / ".local/share/flatpak/exports/share/applications")
    dirs.append(Path("/var/lib/snapd/desktop/applications"))
    return [d for d in dirs if d.is_dir()]


@dataclass(frozen=True)
class DesktopApp:
    id: str
    name: str
    exec_line: str
    terminal: bool
    no_display: bool
    categories: tuple[str, ...]
    source: str

    def command(self) -> list[str]:
        cleaned = _FIELD_CODES.sub("", self.exec_line).strip()
        try:
            return shlex.split(cleaned)
        except ValueError:
            return cleaned.split()


def _parse_desktop_file(path: Path) -> DesktopApp | None:
    """Minimal .desktop parser: the [Desktop Entry] group only."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    in_entry = False
    fields: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_entry = stripped == "[Desktop Entry]"
            continue
        if not in_entry or "=" not in stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        # Ignore localised keys such as Name[de]; the C locale value is what we want.
        if "[" in key:
            continue
        fields.setdefault(key, value.strip())

    if fields.get("Type") not in (None, "Application"):
        return None
    exec_line = fields.get("Exec", "").strip()
    name = fields.get("Name", "").strip()
    if not exec_line or not name:
        return None

    return DesktopApp(
        id=path.stem,
        name=name,
        exec_line=exec_line,
        terminal=fields.get("Terminal", "false").lower() == "true",
        no_display=fields.get("NoDisplay", "false").lower() == "true",
        categories=tuple(c for c in fields.get("Categories", "").split(";") if c),
        source=str(path),
    )


def _installed_apps() -> list[DesktopApp]:
    seen: dict[str, DesktopApp] = {}
    for directory in _app_dirs():
        for entry in sorted(directory.glob("*.desktop")):
            app = _parse_desktop_file(entry)
            # Earlier directories win: a user override in ~/.local shadows /usr.
            if app and app.id not in seen:
                seen[app.id] = app
    return sorted(seen.values(), key=lambda a: a.name.lower())


def _match_app(query: str) -> tuple[DesktopApp | None, list[DesktopApp]]:
    """Resolve a natural name to one application.

    Returns (match, candidates). An ambiguous query returns no match and the
    candidate list, so the caller can ask which one instead of picking blindly
    (§57: never guess at an ambiguous instruction).
    """
    apps = [a for a in _installed_apps() if not a.no_display]
    needle = query.strip().lower()
    if not needle:
        return None, []

    exact = [a for a in apps if a.name.lower() == needle or a.id.lower() == needle]
    if len(exact) == 1:
        return exact[0], exact
    if exact:
        return exact[0], exact

    starts = [a for a in apps if a.name.lower().startswith(needle) or a.id.lower().startswith(needle)]
    if len(starts) == 1:
        return starts[0], starts

    contains = [a for a in apps if needle in a.name.lower() or needle in a.id.lower()]
    if len(contains) == 1:
        return contains[0], contains

    return None, (starts or contains)[:8]


# ------------------------------------------------------------------ wmctrl


def _wmctrl_windows() -> list[dict[str, Any]] | None:
    """Parse ``wmctrl -lpx``. Returns None when wmctrl is unusable."""
    try:
        proc = subprocess.run(
            ["wmctrl", "-lpx"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None

    windows: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        win_id, desktop, pid, wm_class, host, title = parts
        windows.append(
            {
                "id": win_id,
                "desktop": int(desktop) if desktop.lstrip("-").isdigit() else None,
                "pid": int(pid) if pid.isdigit() else None,
                "wm_class": wm_class,
                "app": wm_class.split(".")[-1] if "." in wm_class else wm_class,
                "host": host,
                "title": title,
            }
        )
    return windows


def _active_window_id() -> str | None:
    try:
        proc = subprocess.run(
            ["xdotool", "getactivewindow"], capture_output=True, text=True, timeout=3
        )
        if proc.returncode == 0 and proc.stdout.strip().isdigit():
            return f"0x{int(proc.stdout.strip()):08x}"
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _process_running(name: str) -> bool:
    try:
        proc = subprocess.run(["pgrep", "-f", name], capture_output=True, timeout=3)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ------------------------------------------------------------------- tools


@tool(
    "apps.installed",
    title="List installed applications",
    description=(
        "List the applications installed on this computer, as the desktop "
        "launcher sees them. Use this to find the right name before opening one."
    ),
    capability="app.list",
    read_only=True,
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Optional filter on the name."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
        },
    },
)
def apps_installed(params: dict[str, Any]) -> ToolResult:
    apps = [a for a in _installed_apps() if not a.no_display]
    query = (params.get("query") or "").strip().lower()
    if query:
        apps = [a for a in apps if query in a.name.lower() or query in a.id.lower()]
    limit = int(params.get("limit", 100))
    shown = apps[:limit]
    return ToolResult(
        ok=True,
        data={
            "applications": [
                {"id": a.id, "name": a.name, "categories": list(a.categories)}
                for a in shown
            ],
            "total": len(apps),
            "returned": len(shown),
        },
        summary=f"Found {len(apps)} installed application(s)"
        + (f" matching '{query}'" if query else ""),
    )


@tool(
    "apps.windows",
    title="List open windows",
    description="List the windows currently open, with their application and title.",
    capability="window.list",
    read_only=True,
    requires=("wmctrl",),
    parameters={"type": "object", "properties": {}},
)
def apps_windows(_params: dict[str, Any]) -> ToolResult:
    windows = _wmctrl_windows()
    if windows is None:
        return ToolResult(
            ok=False,
            error=(
                "Could not read the window list. wmctrl is installed but did not respond, "
                "which usually means this session is not running X11."
            ),
        )
    active = _active_window_id()
    for window in windows:
        window["active"] = active is not None and window["id"].lower().endswith(
            active.lower().removeprefix("0x").lstrip("0")
        )
    return ToolResult(
        ok=True,
        data={"windows": windows, "count": len(windows)},
        summary=f"{len(windows)} window(s) open",
    )


def _verify_launch(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    """Poll for a real process or window before claiming the app opened."""
    if not result.data:
        return False, "No launch was recorded."
    binary = result.data.get("binary") or ""
    app_id = result.data.get("id") or ""
    before = set(result.data.get("windows_before") or [])

    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        windows = _wmctrl_windows()
        if windows is not None:
            new = [w for w in windows if w["id"] not in before]
            for window in new:
                haystack = f"{window['wm_class']} {window['title']}".lower()
                if binary and binary.lower() in haystack:
                    return True, f"Confirmed: a new window appeared -- {window['title']!r}."
            if new:
                return True, f"Confirmed: a new window appeared -- {new[0]['title']!r}."
        if binary and _process_running(binary):
            return True, f"Confirmed: a '{binary}' process is running."
        time.sleep(0.4)

    if binary and _process_running(binary):
        return True, f"Confirmed: a '{binary}' process is running."
    return (
        False,
        f"Started {app_id or binary} but no window or process appeared within 6 seconds. "
        "It may be slow to start, or it may have exited immediately.",
    )


@tool(
    "apps.open",
    title="Open an application",
    description=(
        "Start an installed application by name. If the name matches several "
        "applications, the tool asks which one rather than guessing."
    ),
    capability="app.launch",
    idempotent=False,
    verifier=_verify_launch,
    timeout_s=15.0,
    parameters={
        "type": "object",
        "properties": {
            "app": {"type": "string", "minLength": 1, "description": "Application name, e.g. 'Firefox'."},
        },
        "required": ["app"],
    },
)
def apps_open(params: dict[str, Any]) -> ToolResult:
    query = str(params["app"])
    app, candidates = _match_app(query)

    if app is None:
        if candidates:
            names = ", ".join(a.name for a in candidates)
            return ToolResult(
                ok=False,
                error=f"'{query}' matches several applications: {names}. Which one?",
                data={"candidates": [{"id": a.id, "name": a.name} for a in candidates]},
            )
        return ToolResult(
            ok=False,
            error=f"No installed application matches '{query}'. Use apps.installed to see what is available.",
        )

    command = app.command()
    if not command:
        return ToolResult(ok=False, error=f"{app.name} has no runnable command in its desktop entry.")

    windows_before = [w["id"] for w in (_wmctrl_windows() or [])]

    try:
        # start_new_session detaches the child so the app outlives this request
        # and does not receive our signals.
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return ToolResult(ok=False, error=f"Could not start {app.name}: {exc}")

    return ToolResult(
        ok=True,
        data={
            "id": app.id,
            "name": app.name,
            "binary": Path(command[0]).name,
            "command": command,
            "windows_before": windows_before,
        },
        summary=f"Opened {app.name}",
    )


def _verify_focus(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    if not result.data:
        return False, "No focus change was recorded."
    expected = str(result.data.get("window_id", "")).lower()
    time.sleep(0.4)
    active = _active_window_id()
    if active is None:
        return False, "Could not read which window is active, so the switch is unconfirmed."
    if active.lower().lstrip("0x").lstrip("0") == expected.lstrip("0x").lstrip("0"):
        return True, f"Confirmed: {result.data.get('title')!r} is now the active window."
    return False, f"The active window is {active}, not the one that was requested."


@tool(
    "apps.focus",
    title="Switch to a window",
    description="Bring an open window to the front by application name or window title.",
    capability="app.focus",
    idempotent=True,
    verifier=_verify_focus,
    requires=("wmctrl",),
    parameters={
        "type": "object",
        "properties": {
            "window": {"type": "string", "minLength": 1, "description": "Application name or part of the window title."},
        },
        "required": ["window"],
    },
)
def apps_focus(params: dict[str, Any]) -> ToolResult:
    needle = str(params["window"]).strip().lower()
    windows = _wmctrl_windows()
    if windows is None:
        return ToolResult(ok=False, error="Could not read the window list.")
    if not windows:
        return ToolResult(ok=False, error="There are no open windows to switch to.")

    matches = [
        w for w in windows
        if needle in w["title"].lower() or needle in w["wm_class"].lower()
    ]
    if not matches:
        open_apps = ", ".join(sorted({w["app"] for w in windows}))
        return ToolResult(
            ok=False,
            error=f"No open window matches '{params['window']}'. Currently open: {open_apps}.",
        )
    target = matches[0]

    try:
        proc = subprocess.run(
            ["wmctrl", "-ia", target["id"]], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ToolResult(ok=False, error=f"Could not switch windows: {exc}")
    if proc.returncode != 0:
        return ToolResult(
            ok=False,
            error=f"Could not switch to {target['title']!r}: {proc.stderr.strip() or 'wmctrl failed'}",
        )

    return ToolResult(
        ok=True,
        data={"window_id": target["id"], "title": target["title"], "app": target["app"],
              "other_matches": len(matches) - 1},
        summary=f"Switched to {target['title']!r}",
    )


def _verify_close(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    if not result.data:
        return False, "No close was recorded."
    window_id = result.data.get("window_id")
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        windows = _wmctrl_windows()
        if windows is not None and not any(w["id"] == window_id for w in windows):
            return True, f"Confirmed: the window {result.data.get('title')!r} is closed."
        time.sleep(0.4)
    return (
        False,
        "The window is still open. The application may be asking about unsaved work, "
        "which NEXUS deliberately does not answer for you.",
    )


@tool(
    "apps.close",
    title="Close a window",
    description=(
        "Ask a window to close, the same way clicking its close button does. The "
        "application may prompt about unsaved work; NEXUS does not answer that "
        "prompt."
    ),
    capability="app.close",
    idempotent=False,
    verifier=_verify_close,
    requires=("wmctrl",),
    parameters={
        "type": "object",
        "properties": {
            "window": {"type": "string", "minLength": 1},
        },
        "required": ["window"],
    },
)
def apps_close(params: dict[str, Any]) -> ToolResult:
    needle = str(params["window"]).strip().lower()
    windows = _wmctrl_windows()
    if windows is None:
        return ToolResult(ok=False, error="Could not read the window list.")

    matches = [
        w for w in windows
        if needle in w["title"].lower() or needle in w["wm_class"].lower()
    ]
    if not matches:
        return ToolResult(ok=False, error=f"No open window matches '{params['window']}'.")
    if len(matches) > 1:
        titles = "; ".join(w["title"] for w in matches[:5])
        return ToolResult(
            ok=False,
            error=f"'{params['window']}' matches {len(matches)} windows: {titles}. Be more specific.",
            data={"matches": matches},
        )
    target = matches[0]

    try:
        # -c is a polite close request, which lets the app save or prompt.
        proc = subprocess.run(
            ["wmctrl", "-ic", target["id"]], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ToolResult(ok=False, error=f"Could not close the window: {exc}")
    if proc.returncode != 0:
        return ToolResult(ok=False, error=f"Could not close {target['title']!r}.")

    return ToolResult(
        ok=True,
        data={"window_id": target["id"], "title": target["title"], "app": target["app"]},
        summary=f"Asked {target['title']!r} to close",
    )
