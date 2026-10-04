"""Window management (task L12; spec §35, §55, §60).

Moving, resizing and arranging windows. Separate from ``apps.py`` -- which opens,
focuses and closes applications -- because arranging a window is a low-risk,
fully reversible thing, while closing one can lose work. Keeping them apart means
the user can grant "rearrange my windows" without also granting "quit my
applications".

Every action here is verified by reading the window's state back from the X server
afterwards: position and size from ``xwininfo`` and ``_NET_FRAME_EXTENTS``, maximised
and fullscreen from ``_NET_WM_STATE``. Window managers are within their rights to
ignore or adjust a request -- a tiling WM will refuse to move a tiled window, and some
managers snap geometry to increments -- so "the command exited 0" is not evidence
and is not reported as such (§60). Where the result differs from the request, the
tool says what actually happened.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from typing import Any

from ..logging_setup import get_logger
from .apps import _active_window_id, _wmctrl_windows
from .registry import ToolResult, tool

log = get_logger("tools.windows")

#: How long to give the window manager to apply a change before reading it back.
SETTLE_SECONDS = 0.35

_ACTIONS = (
    "maximize", "unmaximize", "minimize", "restore",
    "fullscreen", "unfullscreen", "center", "move", "resize", "workspace",
)

_STATE_FLAGS = {
    "maximized_horizontally": "_NET_WM_STATE_MAXIMIZED_HORZ",
    "maximized_vertically": "_NET_WM_STATE_MAXIMIZED_VERT",
    "fullscreen": "_NET_WM_STATE_FULLSCREEN",
    "hidden": "_NET_WM_STATE_HIDDEN",
    "above": "_NET_WM_STATE_ABOVE",
}


def _missing_tools() -> list[str]:
    # xwininfo reads the geometry (see _geometry) and xprop the frame extents and the
    # window state. Both ship in x11-utils, the package xprop was already required
    # from, so this asks for nothing a working X11 session does not already have.
    return [
        b for b in ("wmctrl", "xdotool", "xprop", "xwininfo") if shutil.which(b) is None
    ]


def _environment_refusal() -> str | None:
    missing = _missing_tools()
    if missing:
        return (
            f"NEXUS cannot manage windows: {', '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} not installed."
        )
    if (os.environ.get("XDG_SESSION_TYPE") or "").lower() == "wayland":
        return (
            "This is a Wayland session. Its compositor does not let other programs "
            "move or resize windows, so NEXUS cannot rearrange them here. An X11 "
            "session would allow it."
        )
    if not os.environ.get("DISPLAY"):
        return "There is no X display available to this process, so no window can be managed."
    return None


def _run(command: list[str], timeout: float = 8.0) -> tuple[bool, str]:
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return False, detail or f"{command[0]} exited {proc.returncode}"
    return True, proc.stdout.strip()


def _find_window(name: str) -> tuple[dict[str, Any] | None, str]:
    """Resolve a window by title or class, or the active one when name is empty."""
    windows = _wmctrl_windows()
    if windows is None:
        return None, "The list of open windows could not be read."
    if not windows:
        return None, "There are no open windows."

    if not name.strip():
        active = _active_window_id()
        if active:
            for window in windows:
                if window["id"].lower() == active.lower():
                    return window, ""
        return None, (
            "NEXUS could not tell which window is focused, so name the window to act on."
        )

    needle = name.strip().lower()
    exact = [w for w in windows if w["title"].lower() == needle]
    if exact:
        return exact[0], ""
    partial = [
        w for w in windows
        if needle in w["title"].lower() or needle in w["wm_class"].lower()
    ]
    if not partial:
        return None, f"No open window matches {name!r}."
    if len(partial) > 1:
        titles = ", ".join(f"{w['title']!r}" for w in partial[:5])
        return None, (
            f"{len(partial)} windows match {name!r}: {titles}. "
            "Be more specific about which one."
        )
    return partial[0], ""


def _frame_extents(window_id: str) -> tuple[int, int, int, int]:
    """The decorations the window manager draws, as ``left, right, top, bottom``.

    Zeros when the property is absent, which is the honest reading of it: either no
    window manager is running or it draws nothing around this window.
    """
    ok, output = _run(["xprop", "-id", window_id, "_NET_FRAME_EXTENTS"], timeout=5)
    if not ok or "=" not in output:
        return 0, 0, 0, 0
    numbers = [int(n) for n in re.findall(r"-?\d+", output.split("=", 1)[1])]
    if len(numbers) != 4:
        return 0, 0, 0, 0
    return numbers[0], numbers[1], numbers[2], numbers[3]


def _geometry(window_id: str) -> dict[str, int] | None:
    """Where the window is, in the same coordinates a move request is written in.

    Not ``xdotool getwindowgeometry``, which was used here first and is wrong under
    every reparenting window manager: it translates the window's origin into root
    coordinates and then adds the window's offset *inside its frame* on top, so a
    window whose visible top-left is 120,90 reads back as 146,187. ``wmctrl -lG``
    performs the same arithmetic and agrees with it, which is how this survived --
    two independent-looking tools telling one lie.

    What it cost, measured under metacity (task L12): every successful move came back
    *unverified*, with the tool reporting "the window manager applied something
    different: x is 146 rather than 120" about a window sitting exactly where it had
    been asked to go. A verifier that is wrong in this direction is not a safe kind of
    wrong -- it teaches the user that a confirmation means nothing.

    So the position comes from ``xwininfo``, which reports the origin without adding
    the offset a second time, and the frame's visible left and top edges are subtracted
    from it, because that corner is the one the user sees and the one ``xdotool
    windowmove`` places. The size is the client's own, which is what a resize sets.
    """
    ok, output = _run(["xwininfo", "-id", window_id], timeout=5)
    if not ok:
        return None
    values: dict[str, int] = {}
    for key, pattern in (
        ("x", r"Absolute upper-left X:\s+(-?\d+)"),
        ("y", r"Absolute upper-left Y:\s+(-?\d+)"),
        ("width", r"Width:\s+(\d+)"),
        ("height", r"Height:\s+(\d+)"),
    ):
        found = re.search(pattern, output)
        if found is None:
            return None
        values[key] = int(found.group(1))
    left, _right, top, _bottom = _frame_extents(window_id)
    values["x"] -= left
    values["y"] -= top
    return values


def _state(window_id: str) -> dict[str, bool]:
    ok, output = _run(["xprop", "-id", window_id, "_NET_WM_STATE"], timeout=5)
    text = output if ok else ""
    return {label: flag in text for label, flag in _STATE_FLAGS.items()}


def _screen_size() -> tuple[int, int] | None:
    ok, output = _run(["xdotool", "getdisplaygeometry"], timeout=5)
    if not ok:
        return None
    parts = output.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1])


def _workspace_count() -> int | None:
    ok, output = _run(["wmctrl", "-d"], timeout=5)
    if not ok:
        return None
    return len([line for line in output.splitlines() if line.strip()]) or None


def _verify_arrange(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    """Read the window's real state back and compare it with what was asked for."""
    if not result.ok or not result.data:
        return False, "Nothing was changed."

    data = result.data
    window_id = data.get("window_id")
    action = data.get("action")
    if not window_id:
        return False, "No window was recorded, so nothing can be verified."

    after_geometry = _geometry(window_id)
    after_state = _state(window_id)

    if action in ("maximize", "fullscreen"):
        flag = "fullscreen" if action == "fullscreen" else "maximized_horizontally"
        if after_state.get(flag):
            return True, f"Confirmed: the window reports itself {action}d."
        return False, (
            f"The request was sent but the window does not report itself {action}d. "
            "Some window managers refuse this for tiled or fixed-size windows."
        )

    if action in ("unmaximize", "unfullscreen"):
        flag = "fullscreen" if action == "unfullscreen" else "maximized_horizontally"
        if not after_state.get(flag):
            return True, "Confirmed: the window is no longer in that state."
        return False, "The window still reports itself in that state."

    if action == "minimize":
        if after_state.get("hidden"):
            return True, "Confirmed: the window is hidden."
        # A window whose id vanished from wmctrl was minimised out of the list.
        windows = _wmctrl_windows() or []
        if not any(w["id"].lower() == str(window_id).lower() for w in windows):
            return True, "Confirmed: the window is no longer listed as visible."
        return False, "The window does not report itself minimised."

    if action in ("move", "resize", "center"):
        if after_geometry is None:
            return False, "The window's geometry could not be read back."
        requested = data.get("requested") or {}
        differences = [
            f"{key} is {after_geometry[key]} rather than {value}"
            for key, value in requested.items()
            if key in after_geometry and abs(after_geometry[key] - int(value)) > 4
        ]
        if not differences:
            return True, (
                f"Confirmed: the window is at {after_geometry['x']},{after_geometry['y']} "
                f"and {after_geometry['width']}x{after_geometry['height']}."
            )
        return False, (
            "The window manager applied something different: "
            + "; ".join(differences)
            + ". This is normal for tiling window managers and size-stepped terminals."
        )

    if action == "workspace":
        windows = _wmctrl_windows() or []
        for window in windows:
            if window["id"].lower() == str(window_id).lower():
                target = data.get("requested", {}).get("workspace")
                if target is not None and window.get("desktop") == int(target):
                    return True, f"Confirmed: the window is on workspace {target}."
                return False, (
                    f"The window is on workspace {window.get('desktop')}, "
                    f"not {target}."
                )
        return False, "The window could not be found after the move."

    if action == "restore":
        if not after_state.get("hidden"):
            return True, "Confirmed: the window is visible again."
        return False, "The window still reports itself hidden."

    return True, "The request was sent."


@tool(
    "window.arrange",
    title="Move, resize or arrange a window",
    description=(
        "Maximise, minimise, restore, fullscreen, centre, move, resize a window, or "
        "send it to another workspace. Names the window by title or application; "
        "acts on the focused window when no name is given."
    ),
    capability="window.manage",
    idempotent=True,
    requires=("wmctrl", "xdotool", "xprop", "xwininfo"),
    verifier=_verify_arrange,
    timeout_s=25.0,
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(_ACTIONS),
                "description": "What to do with the window.",
            },
            "window": {
                "type": "string",
                "description": (
                    "Part of the window title or the application name. Omit to act "
                    "on the focused window."
                ),
            },
            "x": {"type": "integer", "description": "Left edge, for 'move'."},
            "y": {"type": "integer", "description": "Top edge, for 'move'."},
            "width": {"type": "integer", "minimum": 100, "description": "For 'resize'."},
            "height": {"type": "integer", "minimum": 100, "description": "For 'resize'."},
            "workspace": {
                "type": "integer", "minimum": 0,
                "description": "Workspace number, for 'workspace'.",
            },
        },
        "required": ["action"],
    },
)
def window_arrange(params: dict[str, Any]) -> ToolResult:
    blocked = _environment_refusal()
    if blocked:
        return ToolResult(ok=False, error=blocked)

    action = str(params.get("action") or "").strip().lower()
    if action not in _ACTIONS:
        return ToolResult(
            ok=False,
            error=f"{action!r} is not something NEXUS can do to a window. Choose from: "
            + ", ".join(_ACTIONS),
        )

    window, problem = _find_window(str(params.get("window") or ""))
    if window is None:
        return ToolResult(ok=False, error=problem)

    window_id = window["id"]
    before_geometry = _geometry(window_id)
    before_state = _state(window_id)
    requested: dict[str, int] = {}

    if action == "maximize":
        command = ["wmctrl", "-i", "-r", window_id, "-b", "add,maximized_vert,maximized_horz"]
    elif action == "unmaximize":
        command = ["wmctrl", "-i", "-r", window_id, "-b", "remove,maximized_vert,maximized_horz"]
    elif action == "fullscreen":
        command = ["wmctrl", "-i", "-r", window_id, "-b", "add,fullscreen"]
    elif action == "unfullscreen":
        command = ["wmctrl", "-i", "-r", window_id, "-b", "remove,fullscreen"]
    elif action == "minimize":
        command = ["xdotool", "windowminimize", window_id]
    elif action == "restore":
        # Activating a minimised window is what un-minimises it; there is no
        # "unminimize" verb in either tool.
        command = ["wmctrl", "-i", "-a", window_id]
    elif action == "move":
        if params.get("x") is None or params.get("y") is None:
            return ToolResult(ok=False, error="Moving a window needs both x and y.")
        x, y = int(params["x"]), int(params["y"])
        bounds = _screen_size()
        if bounds and not (-bounds[0] < x < bounds[0] and -bounds[1] < y < bounds[1]):
            return ToolResult(
                ok=False,
                error=(
                    f"({x}, {y}) would put the window off a {bounds[0]}x{bounds[1]} "
                    "screen, where you could not reach it."
                ),
            )
        # The corner being on the screen is not the same as the window being on it: a
        # 560-pixel window moved to x=700 keeps its corner reachable and parks the rest
        # past the edge, where nothing can click on it. A window manager that clamps
        # (metacity does) saves this and the move comes back honestly unverified; one
        # that honors the request blindly would let it be *verified* about a window the
        # user cannot reach -- so the size is checked here rather than trusted to the
        # WM. When the frame is itself wider or taller than the screen no position fits
        # and every move would be refused, so in that case only the corner is checked.
        if bounds and before_geometry:
            left, right, top, bottom = _frame_extents(window_id)
            frame_w = before_geometry["width"] + left + right
            frame_h = before_geometry["height"] + top + bottom
            spills = (
                (frame_w <= bounds[0] and x + frame_w > bounds[0])
                or (frame_h <= bounds[1] and y + frame_h > bounds[1])
            )
            if spills:
                return ToolResult(
                    ok=False,
                    error=(
                        f"({x}, {y}) would put the window off a {bounds[0]}x{bounds[1]} "
                        "screen, where you could not reach it."
                    ),
                )
        requested = {"x": x, "y": y}
        command = ["xdotool", "windowmove", window_id, str(x), str(y)]
    elif action == "resize":
        if params.get("width") is None or params.get("height") is None:
            return ToolResult(ok=False, error="Resizing a window needs both width and height.")
        width, height = int(params["width"]), int(params["height"])
        requested = {"width": width, "height": height}
        command = ["xdotool", "windowsize", window_id, str(width), str(height)]
    elif action == "center":
        bounds = _screen_size()
        geometry = before_geometry
        if bounds is None or geometry is None:
            return ToolResult(
                ok=False,
                error="The screen or window size could not be read, so it cannot be centred.",
            )
        # The frame is what the user sees, so the frame is what gets centred. Centring
        # the client area alone leaves a window with a 37px titlebar sitting visibly
        # high, by half its own decoration.
        left, right, top, bottom = _frame_extents(window_id)
        x = max(0, (bounds[0] - (geometry["width"] + left + right)) // 2)
        y = max(0, (bounds[1] - (geometry["height"] + top + bottom)) // 2)
        requested = {"x": x, "y": y}
        command = ["xdotool", "windowmove", window_id, str(x), str(y)]
    else:  # workspace
        if params.get("workspace") is None:
            return ToolResult(ok=False, error="Which workspace should the window move to?")
        target = int(params["workspace"])
        count = _workspace_count()
        if count is not None and not 0 <= target < count:
            return ToolResult(
                ok=False,
                error=(
                    f"There are {count} workspaces, numbered 0 to {count - 1}, "
                    f"so {target} does not exist."
                ),
            )
        requested = {"workspace": target}
        command = ["wmctrl", "-i", "-r", window_id, "-t", str(target)]

    ok, detail = _run(command, timeout=10)
    if not ok:
        return ToolResult(ok=False, error=f"Could not {action} that window: {detail}")

    time.sleep(SETTLE_SECONDS)
    after_geometry = _geometry(window_id)
    after_state = _state(window_id)

    title = window["title"] or window["wm_class"]
    described = {
        "maximize": f"Maximised {title}",
        "unmaximize": f"Un-maximised {title}",
        "minimize": f"Minimised {title}",
        "restore": f"Restored {title}",
        "fullscreen": f"Put {title} into fullscreen",
        "unfullscreen": f"Took {title} out of fullscreen",
        "center": f"Centred {title}",
        "move": f"Moved {title}",
        "resize": f"Resized {title}",
        "workspace": f"Moved {title} to workspace {requested.get('workspace')}",
    }[action]

    return ToolResult(
        ok=True,
        data={
            "action": action,
            "window_id": window_id,
            "title": window["title"],
            "wm_class": window["wm_class"],
            "requested": requested,
            "before": {"geometry": before_geometry, "state": before_state},
            "after": {"geometry": after_geometry, "state": after_state},
        },
        summary=described,
    )


@tool(
    "window.workspaces",
    title="List workspaces",
    description="List the workspaces (virtual desktops) and which one is current.",
    capability="window.list",
    read_only=True,
    requires=("wmctrl",),
    parameters={"type": "object", "properties": {}},
)
def window_workspaces(_params: dict[str, Any]) -> ToolResult:
    ok, output = _run(["wmctrl", "-d"], timeout=5)
    if not ok:
        return ToolResult(ok=False, error=f"The workspace list could not be read: {output}")

    workspaces: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        number = int(parts[0]) if parts[0].isdigit() else None
        current = parts[1] == "*"
        name_match = re.search(r"\s(\S+)\s*$", parts[2]) if len(parts) > 2 else None
        workspaces.append(
            {
                "number": number,
                "current": current,
                "name": name_match.group(1) if name_match else str(number),
            }
        )

    if not workspaces:
        return ToolResult(
            ok=False,
            error="This desktop reports no workspaces, so there is nothing to list.",
        )

    current = next((w["number"] for w in workspaces if w["current"]), None)
    return ToolResult(
        ok=True,
        data={"workspaces": workspaces, "current": current},
        summary=f"{len(workspaces)} workspace(s), currently on {current}",
        verified=True,
        verification="Read from the window manager.",
    )
