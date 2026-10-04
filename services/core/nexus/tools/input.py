"""Synthesised keyboard and mouse input (task L11; spec §38, §60, §65, §81).

This is how NEXUS acts inside applications that have no other interface: type into
a field, press a shortcut, click a button. It is also the most dangerous thing in
the codebase, for a reason worth stating plainly:

**A synthesised keystroke is indistinguishable from a real one.** Every
application on the machine will treat it as the user's own. There is no sandbox,
no undo, and no way for this module to know what has focus. Typing into what turns
out to be a password field, a terminal at a `sudo` prompt, or a chat window that
sends on Enter are all the same operation from here.

Three consequences are built in rather than documented:

**No blind typing.** Every call reports which window was focused when it ran, read
immediately before the keystrokes are sent. If the focused window cannot be
identified, the tool refuses instead of typing into the unknown.

**Refusal near credentials.** A focused window whose *title or class* looks like a
password prompt or a password manager is refused outright. Both channels matter: a
password manager can be identifiable only by its class (title "Vault", class
"Bitwarden"), and for a while on this machine the class channel was dead without
saying so. This is still a heuristic and it says so -- a password field inside an
ordinary window is invisible to X11, and no amount of care here changes that. The
honest position is a narrow guard, both of its channels actually working, plus a
clear statement of what it cannot catch (§78).

**Verification is about delivery, not effect.** These tools confirm the keystrokes
were dispatched and which window received them. Whether the application *did*
anything with them is not observable from here, and the verification text says so
rather than implying more (§60).

Everything here is X11 via ``xdotool``. On Wayland, synthesised input is blocked
by the compositor's security model, and the tools report that instead of silently
doing nothing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from typing import Any

from ..logging_setup import get_logger
from .registry import ToolResult, tool

log = get_logger("tools.input")

#: Typing delay per keystroke in milliseconds. Some applications drop input sent
#: faster than a human could produce it, so the default is deliberately unhurried.
DEFAULT_TYPE_DELAY_MS = 12

#: Longest text accepted in one call. Beyond this, the clipboard is the right
#: mechanism -- a 50,000-character xdotool invocation is a hang, not a paste.
MAX_TYPE_LENGTH = 5_000

#: Window titles that suggest a credential is on screen. Matching one is a
#: refusal, not a warning.
_CREDENTIAL_WINDOW = re.compile(
    r"(?i)\b(?:"
    r"password|passphrase|passwd|"
    r"keyring|keychain|"
    r"authenticat|"
    r"unlock|"
    r"sudo|polkit|pkexec|"
    r"bitwarden|1password|lastpass|keepass|dashlane|enpass|proton pass|"
    r"gnome-keyring|seahorse|"
    r"two-factor|2fa|one-time|verification code"
    r")\b"
)

#: Keys accepted in a shortcut. An allowlist rather than a filter: passing
#: arbitrary strings to xdotool's key parser is how "type this" becomes "run
#: this" on a machine where a key name happens to collide with something else.
_MODIFIERS = {"ctrl", "control", "alt", "shift", "super", "meta", "altgr"}
_NAMED_KEYS = {
    "return", "enter", "tab", "escape", "esc", "space", "backspace", "delete",
    "home", "end", "prior", "page_up", "next", "page_down", "insert",
    "up", "down", "left", "right", "menu", "print",
    "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
    "minus", "plus", "equal", "comma", "period", "slash", "backslash",
    "semicolon", "apostrophe", "grave", "bracketleft", "bracketright",
    "audioplay", "audiopause", "audionext", "audioprev", "audiostop",
    "audioraisevolume", "audiolowervolume", "audiomute",
}
_KEY_ALIASES = {
    "control": "ctrl",
    "meta": "super",
    "esc": "Escape",
    "enter": "Return",
    "page_up": "Prior",
    "page_down": "Next",
}

_BUTTONS = {"left": 1, "middle": 2, "right": 3, "scroll_up": 4, "scroll_down": 5}


# ------------------------------------------------------------------ environment


def _wayland() -> bool:
    return (os.environ.get("XDG_SESSION_TYPE") or "").lower() == "wayland"


def _environment_refusal() -> str | None:
    """Why input synthesis cannot work here, if it cannot."""
    if shutil.which("xdotool") is None:
        return (
            "NEXUS cannot send keyboard or mouse input: 'xdotool' is not installed. "
            "Installing it would enable this."
        )
    if _wayland():
        return (
            "This is a Wayland session, where the compositor blocks synthesised "
            "input from other programs. NEXUS cannot type or click here. An X11 "
            "session would allow it."
        )
    if not os.environ.get("DISPLAY"):
        return (
            "There is no X display available to this process, so NEXUS cannot send "
            "input to any window."
        )
    return None


def _xdotool(args: list[str], timeout: float = 15.0) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["xdotool"] + args, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return False, "xdotool did not finish in time."
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return False, detail or f"xdotool exited {proc.returncode}"
    return True, proc.stdout.strip()


#: Said when the class of the focused window could not be read at all. The guard
#: below is deliberately two-channel -- title *and* class -- so losing one channel
#: silently would leave a narrower guard than this module's docstring promises.
_CLASS_READER_MISSING = (
    "'xprop' is not installed, so NEXUS could not read the focused window's class. "
    "A credential window identifiable only by its class was therefore not checked."
)


def _class_caveat() -> str:
    """What the credential guard could not check here, if anything."""
    return "" if shutil.which("xprop") else _CLASS_READER_MISSING


def _wm_class(window_id: str) -> str:
    """The window's WM_CLASS, or "" when it genuinely has none or cannot be read.

    Deliberately not ``xdotool getwindowclassname``: that subcommand does not exist
    in xdotool 3.20160805 (what Debian and Ubuntu ship), and :func:`_xdotool`
    returns its stderr as the value. A caller that ignores the ok flag therefore
    stores *"xdotool: Unknown command: getwindowclassname"* as the window class --
    which is what happened here, and it is worse than a missing value: the class
    channel of :func:`_target_refusal` spent that whole time searching an error
    message for the word "bitwarden". A failure that becomes plausible-looking data
    is the failure mode §48 exists for.

    ``xprop`` ships in x11-utils alongside the tools already required, and has read
    WM_CLASS unchanged for decades. ``window_monitor`` already parses it this way.
    """
    if shutil.which("xprop") is None:
        return ""
    try:
        proc = subprocess.run(
            ["xprop", "-id", window_id, "WM_CLASS"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    # WM_CLASS(STRING) = "instance", "Class". The class is the second and friendlier
    # name -- "Bitwarden" rather than "bitwarden" -- and is the one a user would
    # recognise, so it is the one reported and matched against.
    names = re.findall(r'"((?:[^"\\]|\\.)*)"', proc.stdout)
    return names[-1].strip() if names else ""


def _focused() -> tuple[str, str, str]:
    """(window_id, title, wm_class) of the focused window; empty strings if unknown."""
    ok, window_id = _xdotool(["getactivewindow"], timeout=5)
    if not ok or not window_id:
        return "", "", ""
    ok, title = _xdotool(["getwindowname", window_id], timeout=5)
    return window_id, (title.strip() if ok else ""), _wm_class(window_id)


def _target_refusal(title: str, wm_class: str) -> str | None:
    """Refuse to send input at a window that looks like a credential prompt.

    Which channel matched is named in the refusal, because "its title suggests a
    password prompt" about a window titled "Vault" reads as a bug and invites the
    user to work around a guard that was right.
    """
    if _CREDENTIAL_WINDOW.search(title):
        looks = f"its title ({title!r})"
    elif _CREDENTIAL_WINDOW.search(wm_class):
        looks = f"its window class ({wm_class!r})"
    else:
        return None
    return (
        f"NEXUS will not send input to this window: {looks} suggests a password or "
        f"authentication prompt. If that is wrong, focus the window you meant and "
        f"ask again."
    )


def _focus_context() -> tuple[dict[str, str] | None, str]:
    """Resolve the focused window, or explain why input must not be sent."""
    window_id, title, wm_class = _focused()
    if not window_id:
        return None, (
            "NEXUS could not determine which window has focus, so it will not send "
            "input. Typing into an unknown window is not something it will guess at."
        )
    refusal = _target_refusal(title, wm_class)
    if refusal:
        return None, refusal
    context = {"window_id": window_id, "window_title": title, "window_class": wm_class}
    caveat = _class_caveat()
    if caveat:
        context["class_caveat"] = caveat
    return context, ""


# ----------------------------------------------------------------- verification


def _verify_dispatch(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    """Confirm the input was dispatched, and be explicit about the limit of that.

    There is no way from outside an application to observe that it acted on a
    keystroke. Claiming otherwise would be the failure §60 exists to prevent, so
    this verifies delivery and names what it cannot verify.
    """
    if not result.ok or not result.data:
        return False, "Nothing was sent."
    data = result.data
    window = data.get("window_title") or data.get("window_class") or "the focused window"
    still_focused = data.get("still_focused")

    if still_focused is False:
        return False, (
            f"Input was sent to {window}, but focus changed while it was being sent, "
            "so part of it may have gone somewhere else."
        )
    caveat = data.get("class_caveat")
    return True, (
        f"Confirmed dispatched to {window}. Whether the application acted on it "
        "cannot be observed from outside, so this confirms delivery only."
        + (f" {caveat}" if caveat else "")
    )


# ------------------------------------------------------------------------ tools


@tool(
    "input.type",
    title="Type text",
    description=(
        "Type text into whatever window currently has focus, as if it came from "
        "the keyboard. Use only when there is no other way to put text where it "
        "needs to go; check what is focused first."
    ),
    capability="input.type",
    idempotent=False,
    requires=("xdotool",),
    verifier=_verify_dispatch,
    timeout_s=90.0,
    parameters={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The exact text to type.",
            },
            "press_enter": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Press Enter afterwards. In a chat window or a terminal this "
                    "submits or runs what was typed."
                ),
            },
            "delay_ms": {
                "type": "integer", "minimum": 0, "maximum": 200,
                "default": DEFAULT_TYPE_DELAY_MS,
                "description": "Milliseconds between keystrokes.",
            },
        },
        "required": ["text"],
    },
)
def input_type(params: dict[str, Any]) -> ToolResult:
    blocked = _environment_refusal()
    if blocked:
        return ToolResult(ok=False, error=blocked)

    text = str(params.get("text") or "")
    if not text:
        return ToolResult(ok=False, error="There is no text to type.")
    if len(text) > MAX_TYPE_LENGTH:
        return ToolResult(
            ok=False,
            error=(
                f"That is {len(text):,} characters, over the {MAX_TYPE_LENGTH:,} "
                "NEXUS will type in one go. Typing that much keystroke by keystroke "
                "would take minutes and drop characters; put it on the clipboard and "
                "paste it instead."
            ),
        )

    context, refusal = _focus_context()
    if context is None:
        return ToolResult(ok=False, error=refusal)

    delay = int(params.get("delay_ms", DEFAULT_TYPE_DELAY_MS))
    press_enter = bool(params.get("press_enter", False))

    ok, detail = _xdotool(
        ["type", "--clearmodifiers", "--delay", str(delay), "--", text],
        timeout=max(20.0, len(text) * delay / 1000.0 + 15.0),
    )
    if not ok:
        return ToolResult(ok=False, error=f"Could not type that text: {detail}")

    if press_enter:
        ok, detail = _xdotool(["key", "--clearmodifiers", "Return"], timeout=5)
        if not ok:
            return ToolResult(
                ok=False,
                error=(
                    f"The text was typed into {context['window_title']!r}, but Enter "
                    f"could not be pressed: {detail}"
                ),
                data={**context, "typed": True, "entered": False},
            )

    after_id, _, _ = _focused()
    data = {
        **context,
        "characters": len(text),
        "lines": text.count("\n") + 1,
        "press_enter": press_enter,
        "delay_ms": delay,
        # Focus can move mid-type; the caller is told rather than left to assume.
        "still_focused": after_id == context["window_id"],
    }
    where = context["window_title"] or context["window_class"] or "the focused window"
    return ToolResult(
        ok=True,
        data=data,
        summary=(
            f"Typed {len(text)} character(s) into {where}"
            + (" and pressed Enter" if press_enter else "")
        ),
    )


def _normalise_combo(combo: str) -> tuple[str, str]:
    """Validate a shortcut and return (xdotool form, error)."""
    parts = [p.strip().lower() for p in re.split(r"[+\-\s]+", combo) if p.strip()]
    if not parts:
        return "", "No key was given."

    resolved: list[str] = []
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if part in _MODIFIERS and not last:
            resolved.append(_KEY_ALIASES.get(part, part))
            continue
        if part in _MODIFIERS and last and len(parts) > 1:
            return "", f"A shortcut cannot end with the modifier {part!r}."
        if len(part) == 1 and (part.isalnum() or part in "`~!@#$%^&*()_=[]{};':\",./<>?\\|"):
            resolved.append(part)
            continue
        if part in _NAMED_KEYS:
            resolved.append(_KEY_ALIASES.get(part, part.capitalize() if part.isalpha() else part))
            continue
        return "", (
            f"{part!r} is not a key NEXUS recognises. Use letters, digits, or names "
            f"like Return, Tab, Escape, F5, Up, with modifiers such as ctrl+shift."
        )
    return "+".join(resolved), ""


@tool(
    "input.key",
    title="Press keys",
    description=(
        "Press a key or keyboard shortcut in the focused window, for example "
        "'ctrl+s', 'alt+Tab', 'Escape', 'F5'. Use for actions an application only "
        "exposes as a shortcut."
    ),
    capability="input.key",
    idempotent=False,
    requires=("xdotool",),
    verifier=_verify_dispatch,
    timeout_s=30.0,
    parameters={
        "type": "object",
        "properties": {
            "keys": {
                "type": "string",
                "description": "One key or shortcut, e.g. 'ctrl+s' or 'Escape'.",
            },
            "repeat": {
                "type": "integer", "minimum": 1, "maximum": 20, "default": 1,
                "description": "How many times to press it.",
            },
        },
        "required": ["keys"],
    },
)
def input_key(params: dict[str, Any]) -> ToolResult:
    blocked = _environment_refusal()
    if blocked:
        return ToolResult(ok=False, error=blocked)

    combo, error = _normalise_combo(str(params.get("keys") or ""))
    if error:
        return ToolResult(ok=False, error=error)

    context, refusal = _focus_context()
    if context is None:
        return ToolResult(ok=False, error=refusal)

    repeat = int(params.get("repeat", 1))
    for attempt in range(repeat):
        ok, detail = _xdotool(["key", "--clearmodifiers", combo], timeout=10)
        if not ok:
            return ToolResult(
                ok=False,
                error=(
                    f"Could not press {combo}: {detail}"
                    + (f" (after {attempt} of {repeat} presses)" if attempt else "")
                ),
                data={**context, "pressed": attempt},
            )
        if repeat > 1:
            time.sleep(0.05)

    after_id, _, _ = _focused()
    where = context["window_title"] or context["window_class"] or "the focused window"
    return ToolResult(
        ok=True,
        data={
            **context,
            "keys": combo,
            "repeat": repeat,
            "still_focused": after_id == context["window_id"],
        },
        summary=(
            f"Pressed {combo}" + (f" {repeat} times" if repeat > 1 else "") + f" in {where}"
        ),
    )


@tool(
    "input.click",
    title="Click the mouse",
    description=(
        "Click at a screen position, or where the pointer already is. Prefer a "
        "keyboard shortcut when one exists: a click targets a coordinate, not a "
        "button, so it will click whatever happens to be there."
    ),
    capability="input.pointer",
    idempotent=False,
    requires=("xdotool",),
    timeout_s=20.0,
    parameters={
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "Screen x, omit to click in place."},
            "y": {"type": "integer", "description": "Screen y, omit to click in place."},
            "button": {
                "type": "string",
                "enum": ["left", "middle", "right"],
                "default": "left",
            },
            "double": {"type": "boolean", "default": False, "description": "Double-click."},
        },
    },
)
def input_click(params: dict[str, Any]) -> ToolResult:
    blocked = _environment_refusal()
    if blocked:
        return ToolResult(ok=False, error=blocked)

    button = str(params.get("button") or "left")
    if button not in ("left", "middle", "right"):
        return ToolResult(ok=False, error=f"{button!r} is not a mouse button.")

    has_x, has_y = params.get("x") is not None, params.get("y") is not None
    if has_x != has_y:
        return ToolResult(
            ok=False,
            error="Both x and y are needed to click at a position, or neither to click in place.",
        )

    moved_to = None
    if has_x:
        x, y = int(params["x"]), int(params["y"])
        bounds = _screen_size()
        if bounds and not (0 <= x < bounds[0] and 0 <= y < bounds[1]):
            return ToolResult(
                ok=False,
                error=(
                    f"({x}, {y}) is outside the screen, which is {bounds[0]}x{bounds[1]}."
                ),
            )
        # ``mousemove --sync`` waits for the pointer to move *away from* where it
        # started, so asking it to go somewhere it already is never returns: xdotool
        # sits there until the tool's timeout and the click is reported as a pointer
        # failure that never happened. Clicking the same button twice -- which agent
        # mode does constantly -- is the ordinary way to hit that, and it cost ten
        # seconds and a false failure every time. Nothing to move is not an error.
        at = _pointer_position()
        already_there = at is not None and (at["x"], at["y"]) == (x, y)
        if not already_there:
            ok, detail = _xdotool(["mousemove", "--sync", str(x), str(y)], timeout=10)
            if not ok:
                return ToolResult(ok=False, error=f"Could not move the pointer: {detail}")
            # Let the application under the pointer register the hover before the
            # click; some menus only open their target on hover.
            time.sleep(0.12)
        moved_to = {"x": x, "y": y}

    # Read the window *after* moving, since the move is what decides the target.
    window_id, title, wm_class = _focused()
    refusal = _target_refusal(title, wm_class)
    if refusal:
        return ToolResult(ok=False, error=refusal)

    args = ["click", "--clearmodifiers"]
    if bool(params.get("double")):
        args += ["--repeat", "2", "--delay", "80"]
    args.append(str(_BUTTONS[button]))

    ok, detail = _xdotool(args, timeout=10)
    if not ok:
        return ToolResult(ok=False, error=f"Could not click: {detail}")

    position = _pointer_position()
    where = f" at ({moved_to['x']}, {moved_to['y']})" if moved_to else ""
    return ToolResult(
        ok=True,
        data={
            "button": button,
            "double": bool(params.get("double")),
            "moved_to": moved_to,
            "pointer": position,
            "window_id": window_id,
            "window_title": title,
            "window_class": wm_class,
            **({"class_caveat": caveat} if (caveat := _class_caveat()) else {}),
        },
        summary=(
            f"{'Double-clicked' if params.get('double') else 'Clicked'} {button}{where}"
            + (f" on {title}" if title else "")
        ),
    )


@tool(
    "input.scroll",
    title="Scroll",
    description="Scroll up or down in the window under the pointer.",
    capability="input.pointer",
    idempotent=False,
    requires=("xdotool",),
    timeout_s=20.0,
    parameters={
        "type": "object",
        "properties": {
            "direction": {"type": "string", "enum": ["up", "down"], "default": "down"},
            "amount": {
                "type": "integer", "minimum": 1, "maximum": 50, "default": 3,
                "description": "Number of scroll steps.",
            },
        },
    },
)
def input_scroll(params: dict[str, Any]) -> ToolResult:
    blocked = _environment_refusal()
    if blocked:
        return ToolResult(ok=False, error=blocked)

    direction = str(params.get("direction") or "down")
    if direction not in ("up", "down"):
        return ToolResult(ok=False, error=f"{direction!r} is not 'up' or 'down'.")
    amount = int(params.get("amount", 3))

    button = _BUTTONS["scroll_up" if direction == "up" else "scroll_down"]
    ok, detail = _xdotool(
        ["click", "--clearmodifiers", "--repeat", str(amount), "--delay", "40", str(button)],
        timeout=max(10.0, amount * 0.1 + 5.0),
    )
    if not ok:
        return ToolResult(ok=False, error=f"Could not scroll: {detail}")

    _, title, wm_class = _focused()
    return ToolResult(
        ok=True,
        data={
            "direction": direction,
            "amount": amount,
            "window_title": title,
            "window_class": wm_class,
            "pointer": _pointer_position(),
        },
        summary=f"Scrolled {direction} {amount} step(s)" + (f" in {title}" if title else ""),
    )


@tool(
    "input.pointer_position",
    title="Where is the pointer",
    description="Report the pointer position and the screen size.",
    capability="input.pointer",
    read_only=True,
    requires=("xdotool",),
    parameters={"type": "object", "properties": {}},
)
def input_pointer_position(params: dict[str, Any]) -> ToolResult:
    blocked = _environment_refusal()
    if blocked:
        return ToolResult(ok=False, error=blocked)

    position = _pointer_position()
    if position is None:
        return ToolResult(ok=False, error="The pointer position could not be read.")
    size = _screen_size()
    return ToolResult(
        ok=True,
        data={
            "x": position["x"],
            "y": position["y"],
            "screen_width": size[0] if size else None,
            "screen_height": size[1] if size else None,
        },
        summary=(
            f"Pointer at ({position['x']}, {position['y']})"
            + (f" on a {size[0]}x{size[1]} screen" if size else "")
        ),
        verified=True,
        verification="Read directly from the X server.",
    )


# ---------------------------------------------------------------------- helpers


def _pointer_position() -> dict[str, int] | None:
    ok, output = _xdotool(["getmouselocation", "--shell"], timeout=5)
    if not ok:
        return None
    values: dict[str, int] = {}
    for line in output.splitlines():
        key, _, value = line.partition("=")
        if key.lower() in ("x", "y") and value.strip().lstrip("-").isdigit():
            values[key.lower()] = int(value)
    return values if {"x", "y"} <= values.keys() else None


def _screen_size() -> tuple[int, int] | None:
    ok, output = _xdotool(["getdisplaygeometry"], timeout=5)
    if not ok:
        return None
    parts = output.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1])
