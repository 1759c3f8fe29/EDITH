"""Screen capture (task D7; spec §39, §60, §65).

Captures the screen, the active window, or a named window, using whichever
screenshot backend the desktop actually has. The result is a **file path**, never
the image bytes: tool results are written to the audit log, and putting a
base64-encoded screenshot in there would bloat the database and duplicate the
user's screen contents into a second place. The orchestrator reads the file when
it needs to attach the image to a vision request.

Verification is genuine here: the file must exist, be non-empty, and start with a
real image signature. The pixel dimensions are read out of the PNG header so the
report says what was actually captured rather than what was requested.

Screen capture is high-risk on purpose. Whatever is on screen -- an open password
manager, a private message -- ends up in an image that may be sent to a cloud
model. The capability's consequence text says exactly that, and this module keeps
captures in a single directory so they can be listed and deleted.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import settings
from ..logging_setup import get_logger
from .registry import ToolResult, tool

log = get_logger("tools.screen")

# Ordered by preference: a dedicated CLI screenshotter first, ImageMagick last.
# Each entry: (binary, full-screen args, active-window args, wayland?)
_BACKENDS: tuple[tuple[str, list[str], list[str] | None, bool], ...] = (
    ("scrot", ["-o", "{path}"], ["-o", "-u", "{path}"], False),
    ("xfce4-screenshooter", ["-f", "-s", "{path}"], ["-w", "-s", "{path}"], False),
    ("gnome-screenshot", ["-f", "{path}"], ["-w", "-f", "{path}"], False),
    ("spectacle", ["-b", "-n", "-f", "-o", "{path}"], ["-b", "-n", "-a", "-o", "{path}"], False),
    ("grim", ["{path}"], None, True),
    ("import", ["-window", "root", "{path}"], None, False),
)

_IMAGE_SIGNATURES = {
    b"\x89PNG\r\n\x1a\n": "png",
    b"\xff\xd8\xff": "jpeg",
}


#: The capture tool's declared requirement, derived from the table above rather
#: than written out again: any one of these programs is enough, and a new backend
#: added to ``_BACKENDS`` must not silently leave the declaration behind.
_BACKEND_REQUIREMENT = "|".join(b[0] for b in _BACKENDS)


def _available_backends() -> list[tuple[str, list[str], list[str] | None, bool]]:
    return [b for b in _BACKENDS if shutil.which(b[0]) is not None]


def _png_size(path: Path) -> tuple[int, int] | None:
    """Read width/height from a PNG's IHDR chunk."""
    try:
        with path.open("rb") as handle:
            header = handle.read(24)
    except OSError:
        return None
    if len(header) < 24 or not header.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    width, height = struct.unpack(">II", header[16:24])
    return int(width), int(height)


def _image_kind(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            head = handle.read(8)
    except OSError:
        return None
    for signature, kind in _IMAGE_SIGNATURES.items():
        if head.startswith(signature):
            return kind
    return None


def _capture_dir() -> Path:
    directory = settings().screenshot_dir
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _target_window_id(name: str) -> tuple[str | None, str]:
    """Resolve a window name to an X window id via wmctrl."""
    if shutil.which("wmctrl") is None:
        return None, "wmctrl is not installed, so a window cannot be found by name."
    try:
        proc = subprocess.run(["wmctrl", "-lx"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"Could not list windows: {exc}"
    if proc.returncode != 0:
        return None, "Could not list windows."

    needle = name.strip().lower()
    for line in proc.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        win_id, _, wm_class, _, title = parts
        if needle in title.lower() or needle in wm_class.lower():
            return win_id, title
    return None, f"No open window matches '{name}'."


def _verify_capture(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    """A screenshot exists only if there is a real image file on disk."""
    if not result.data or not result.data.get("path"):
        return False, "No screenshot file was recorded."
    path = Path(result.data["path"])
    if not path.exists():
        return False, "The screenshot file does not exist, so nothing was captured."
    size = path.stat().st_size
    if size == 0:
        return False, "The screenshot file is empty, so the capture failed."
    kind = _image_kind(path)
    if kind is None:
        return False, f"{path.name} is {size:,} bytes but is not a recognisable image."

    dimensions = _png_size(path)
    if dimensions:
        return True, (
            f"Confirmed: {dimensions[0]}x{dimensions[1]} {kind.upper()} image, "
            f"{size:,} bytes, at {path}."
        )
    return True, f"Confirmed: {kind.upper()} image of {size:,} bytes at {path}."


@tool(
    "screen.capture",
    title="Capture the screen",
    description=(
        "Take a screenshot of the whole screen, the active window, or a named "
        "window. Returns the file path of the saved image; use it when you need "
        "to see what is on screen to answer a question."
    ),
    capability="screen.capture",
    idempotent=False,
    verifier=_verify_capture,
    requires=(_BACKEND_REQUIREMENT,),
    timeout_s=25.0,
    voice_start="Taking a screenshot",
    voice_timeout="The screenshot is taking too long.",
    voice_error="Could not take the screenshot.",
    parameters={
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "'screen' for everything, 'window' for the active window, or "
                    "part of a window title to capture that window."
                ),
                "default": "screen",
            },
            "delay_seconds": {
                "type": "integer", "minimum": 0, "maximum": 10, "default": 0,
                "description": "Wait before capturing, e.g. to let a menu open.",
            },
        },
    },
)
def screen_capture(params: dict[str, Any]) -> ToolResult:
    target = str(params.get("target") or "screen").strip()
    delay = int(params.get("delay_seconds", 0))

    backends = _available_backends()
    if not backends:
        return ToolResult(
            ok=False,
            error=(
                "NEXUS cannot capture the screen: no screenshot program is installed. "
                "Installing 'scrot' would enable it (on Wayland, 'grim')."
            ),
        )

    wants_window = target.lower() in ("window", "active", "active_window", "focused")
    named_window = not wants_window and target.lower() not in ("screen", "full", "all", "desktop")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    label = "window" if wants_window or named_window else "screen"
    path = _capture_dir() / f"{stamp}-{label}.png"

    if delay:
        time.sleep(delay)

    # A specific named window: raise it first, then capture the active window.
    # Capturing an obscured window is not possible with these backends, and
    # returning an image of whatever happened to be on top would be worse than
    # saying so.
    window_title = ""
    if named_window:
        win_id, detail = _target_window_id(target)
        if win_id is None:
            return ToolResult(ok=False, error=detail)
        window_title = detail
        try:
            subprocess.run(["wmctrl", "-ia", win_id], capture_output=True, timeout=5)
            time.sleep(0.6)  # give the window manager time to raise and redraw
        except (OSError, subprocess.SubprocessError) as exc:
            return ToolResult(ok=False, error=f"Could not bring that window forward: {exc}")

    attempts: list[str] = []
    for binary, full_args, window_args, is_wayland in backends:
        args = window_args if (wants_window or named_window) else full_args
        if args is None:
            attempts.append(f"{binary} cannot capture a single window")
            continue

        command = [binary] + [a.format(path=str(path)) for a in args]
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            attempts.append(f"{binary}: {exc}")
            continue

        if proc.returncode == 0 and path.exists() and path.stat().st_size > 0:
            size = path.stat().st_size
            dimensions = _png_size(path)
            data = {
                "path": str(path),
                "backend": binary,
                "target": target,
                "window_title": window_title,
                "bytes": size,
                "width": dimensions[0] if dimensions else None,
                "height": dimensions[1] if dimensions else None,
                "display_server": "wayland" if is_wayland else "x11",
            }
            what = (
                f"the window {window_title!r}" if named_window
                else "the active window" if wants_window
                else "the whole screen"
            )
            return ToolResult(
                ok=True, data=data,
                summary=f"Captured {what} to {path.name} ({size // 1024} KB)",
            )

        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        attempts.append(f"{binary}: {detail[-1] if detail else f'exited {proc.returncode}'}")
        # A failed backend may have left a truncated file behind.
        if path.exists() and path.stat().st_size == 0:
            path.unlink(missing_ok=True)

    return ToolResult(
        ok=False,
        error=(
            "Could not capture the screen. "
            + "; ".join(attempts)
            + ". On a Wayland session, X11 screenshot tools cannot see the screen at all."
        ),
        data={"attempted": attempts},
    )


@tool(
    "screen.captures",
    title="List saved screenshots",
    description="List the screenshots NEXUS has taken, newest first.",
    capability="screen.capture",
    read_only=True,
    parameters={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        },
    },
)
def screen_captures(params: dict[str, Any]) -> ToolResult:
    limit = int(params.get("limit", 20))
    directory = _capture_dir()
    files = sorted(
        (p for p in directory.glob("*.png") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    shown = files[:limit]
    total_bytes = sum(p.stat().st_size for p in files)
    return ToolResult(
        ok=True,
        data={
            "directory": str(directory),
            "screenshots": [
                {
                    "path": str(p),
                    "name": p.name,
                    "bytes": p.stat().st_size,
                    "taken_at": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
                }
                for p in shown
            ],
            "total": len(files),
            "total_bytes": total_bytes,
        },
        summary=(
            f"{len(files)} screenshot(s) in {directory}, {total_bytes // 1024} KB total"
            if files else f"No screenshots saved yet in {directory}"
        ),
        verified=True,
        verification="Read from the screenshot directory on disk.",
    )
