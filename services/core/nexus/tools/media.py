"""Media playback control (task L13; spec §35, §55, §60).

Play, pause, skip and report what is playing, over **MPRIS** -- the D-Bus interface
every modern Linux media player, including browsers playing video, exposes itself on.

Two ways of speaking it, in this order:

* ``playerctl``, if it is installed. It is the specialist client and papers over a
  few player quirks.
* **D-Bus directly**, through ``busctl`` (part of systemd, so present on any systemd
  desktop). MPRIS is not playerctl's protocol -- it is a bus interface, and this asks
  the players on the bus the same questions playerctl would. This is the difference
  between "playback control needs a package installed" and playback control working.

Neither is a fallback that guesses: both read the player's own reported state.

MPRIS is the reason this is honest rather than a guess: the player reports its own
status, so "paused Spotify" can be verified by asking Spotify what state it is in
afterwards, rather than by assuming the command worked. Where a player does not
implement the part of MPRIS being used (some browsers do not implement seeking),
the tool reports what the player said instead of pretending.

Multiple players can be active at once -- a browser tab and a music app. Without a
named player, playerctl acts on whichever is playing, and the result says which
one that turned out to be, so an unexpected target is visible rather than silent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import Any

from ..logging_setup import get_logger
from .registry import ToolResult, tool

log = get_logger("tools.media")

_ACTIONS = ("play", "pause", "toggle", "stop", "next", "previous")

#: The playerctl verb for each action. "toggle" is play-pause, which is what a
#: media key does, and is the one most players implement most reliably.
_ACTIONS_TO_COMMAND = {
    "play": "play",
    "pause": "pause",
    "toggle": "play-pause",
    "stop": "stop",
    "next": "next",
    "previous": "previous",
}

#: playerctl exits 1 with this on stderr when nothing is running. That is a real
#: answer -- "nothing is playing" -- not a failure to be reported as an error.
_NO_PLAYER = "no players found"

SETTLE_SECONDS = 0.4


def _playerctl(args: list[str], timeout: float = 8.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["playerctl"] + args, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return 124, "", "playerctl did not respond in time."
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


#: The MPRIS bus name prefix, object path and interfaces. Fixed by the spec, which
#: is why talking to a player over D-Bus does not need a per-player adapter.
MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_ROOT_IFACE = "org.mpris.MediaPlayer2"
MPRIS_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"

#: The MPRIS method for each action. Same list as playerctl's verbs, because
#: playerctl is calling these.
_ACTIONS_TO_MPRIS = {
    "play": "Play",
    "pause": "Pause",
    "toggle": "PlayPause",
    "stop": "Stop",
    "next": "Next",
    "previous": "Previous",
}


def backend() -> str:
    """Which way of speaking MPRIS is available: ``playerctl``, ``busctl`` or none."""
    if shutil.which("playerctl"):
        return "playerctl"
    if shutil.which("busctl"):
        return "busctl"
    return ""


def _busctl(args: list[str], timeout: float = 8.0) -> tuple[int, str, str]:
    """Run busctl on the session bus. ``--user`` is the session bus, not the system one."""
    try:
        proc = subprocess.run(
            ["busctl", "--user", *args, "--json=short"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "busctl did not respond in time."
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _unwrap(variant: Any) -> Any:
    """Take the value out of busctl's ``{"type": ..., "data": ...}`` wrapper.

    Applied recursively, because a metadata dict is variants inside a variant. An
    MPRIS artist is an array of strings (``xesam:artist`` is ``as``) while a title is
    a single string, so the shape has to survive rather than be flattened here.
    """
    if isinstance(variant, dict) and set(variant) == {"type", "data"}:
        return _unwrap(variant["data"])
    if isinstance(variant, dict):
        return {key: _unwrap(value) for key, value in variant.items()}
    if isinstance(variant, list):
        return [_unwrap(item) for item in variant]
    return variant


def _mpris_names() -> list[str]:
    """Every MPRIS player currently on the session bus, as full bus names."""
    code, out, _ = _busctl(
        ["call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
         "org.freedesktop.DBus", "ListNames"],
        timeout=6,
    )
    if code != 0 or not out:
        return []
    try:
        names = _unwrap(json.loads(out))
    except (json.JSONDecodeError, TypeError):
        return []
    # ListNames returns one argument -- an array of strings -- so busctl gives a
    # single-element list holding the array.
    flat = names[0] if isinstance(names, list) and len(names) == 1 else names
    if not isinstance(flat, list):
        return []
    return sorted({n for n in flat if isinstance(n, str) and n.startswith(MPRIS_PREFIX)})


def _mpris_property(name: str, prop: str, iface: str = MPRIS_PLAYER_IFACE) -> Any:
    """One property from one player, or None if the player will not say.

    A player refusing a property is normal and not an error: rhythmbox answers
    "Playback position not available" for ``Position`` even while playing, and some
    browsers do not implement seeking at all.
    """
    code, out, _ = _busctl(["get-property", name, MPRIS_PATH, iface, prop], timeout=6)
    if code != 0 or not out:
        return None
    try:
        return _unwrap(json.loads(out))
    except (json.JSONDecodeError, TypeError):
        return None


def _mpris_now_playing(name: str) -> dict[str, Any] | None:
    """The same shape ``_playerctl_now_playing`` returns, read over D-Bus."""
    status = _mpris_property(name, "PlaybackStatus")
    if not isinstance(status, str) or not status:
        return None

    metadata = _mpris_property(name, "Metadata")
    metadata = metadata if isinstance(metadata, dict) else {}

    def text(key: str) -> str:
        value = metadata.get(key)
        if isinstance(value, list):
            # An array of one artist reads as one artist, not as ['Artist'].
            return ", ".join(str(v) for v in value if v)
        return str(value) if value not in (None, "") else ""

    identity = _mpris_property(name, "Identity", iface=MPRIS_ROOT_IFACE)
    state: dict[str, Any] = {
        "status": status.lower(),
        "artist": text("xesam:artist"),
        "title": text("xesam:title"),
        "album": text("xesam:album"),
        "player": identity if isinstance(identity, str) and identity else _short_name(name),
    }
    position = _mpris_property(name, "Position")
    if isinstance(position, (int, float)):
        state["position_seconds"] = round(position / 1_000_000, 1)
    length = metadata.get("mpris:length")
    if isinstance(length, (int, float)):
        state["length_seconds"] = round(length / 1_000_000, 1)
    return state


def _short_name(bus_name: str) -> str:
    """``org.mpris.MediaPlayer2.rhythmbox`` -> ``rhythmbox``.

    Instance suffixes are kept (``chromium.instance1234``): two windows of the same
    browser are two players, and collapsing them would make one of them
    unaddressable.
    """
    return bus_name[len(MPRIS_PREFIX):] if bus_name.startswith(MPRIS_PREFIX) else bus_name


def _mpris_full_name(short: str) -> str:
    return short if short.startswith(MPRIS_PREFIX) else MPRIS_PREFIX + short


def _mpris_command(short: str, action: str) -> tuple[int, str]:
    """Call the MPRIS method for ``action``. Returns (exit code, stderr)."""
    code, _out, err = _busctl(
        ["call", _mpris_full_name(short), MPRIS_PATH, MPRIS_PLAYER_IFACE,
         _ACTIONS_TO_MPRIS[action]],
        timeout=10,
    )
    return code, err


# ------------------------------------------------------- one interface, two backends


def _playerctl_players() -> list[str]:
    code, out, _ = _playerctl(["-l"], timeout=5)
    if code != 0 or not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _players() -> list[str]:
    """Player identifiers, in whichever way this machine can be asked."""
    if backend() == "playerctl":
        return _playerctl_players()
    if backend() == "busctl":
        return [_short_name(name) for name in _mpris_names()]
    return []


def _playerctl_now_playing(player: str = "") -> dict[str, Any] | None:
    """Ask a player what it is playing, through playerctl. None when it will not say."""
    scope = ["-p", player] if player else []
    code, status, stderr = _playerctl(scope + ["status"], timeout=5)
    if code != 0:
        if _NO_PLAYER in stderr.lower():
            return None
        return None

    fields = {
        "artist": "{{artist}}",
        "title": "{{title}}",
        "album": "{{album}}",
    }
    metadata: dict[str, Any] = {"status": status.lower()}
    for key, template in fields.items():
        code, value, _ = _playerctl(scope + ["metadata", "--format", template], timeout=5)
        metadata[key] = value if code == 0 else ""

    code, position, _ = _playerctl(scope + ["position"], timeout=5)
    if code == 0 and position:
        try:
            metadata["position_seconds"] = round(float(position), 1)
        except ValueError:
            metadata["position_seconds"] = None

    code, name, _ = _playerctl(scope + ["-f", "{{playerName}}", "metadata"], timeout=5)
    metadata["player"] = name if code == 0 and name else (player or "")
    return metadata


def _now_playing(player: str = "") -> dict[str, Any] | None:
    """Ask a player what it is playing. None when it will not say.

    With no player named, this reports whichever one is actually playing -- the same
    choice playerctl makes -- falling back to the first that answers at all, so a
    paused player is still visible when nothing is playing.
    """
    if backend() == "playerctl":
        return _playerctl_now_playing(player)
    if backend() != "busctl":
        return None
    if player:
        return _mpris_now_playing(_mpris_full_name(player))
    states = [(name, _mpris_now_playing(name)) for name in _mpris_names()]
    answered = [state for _name, state in states if state]
    return next((s for s in answered if s.get("status") == "playing"), answered[0] if answered else None)


def _command(player: str, action: str) -> tuple[int, str]:
    """Send one playback command. Returns (exit code, stderr) from whichever backend."""
    if backend() == "playerctl":
        scope = ["-p", player] if player else []
        code, _out, err = _playerctl(scope + [_ACTIONS_TO_COMMAND[action]], timeout=10)
        return code, err
    if backend() != "busctl":
        return 1, "No way to reach a media player on this computer."
    if player:
        return _mpris_command(player, action)
    # No player named: act on the one that is playing, and say which in the result.
    names = _mpris_names()
    playing = [n for n in names if _mpris_property(n, "PlaybackStatus") == "Playing"]
    target = (playing or names)
    if not target:
        return 1, "no players found"
    return _mpris_command(target[0], action)


def _describe(metadata: dict[str, Any] | None) -> str:
    if not metadata:
        return "nothing"
    title = metadata.get("title") or ""
    artist = metadata.get("artist") or ""
    player = metadata.get("player") or "a player"
    if title and artist:
        return f"{title} by {artist} in {player}"
    if title:
        return f"{title} in {player}"
    return player


def _verify_media(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    """Ask the player what state it is in now, rather than trusting the command."""
    if not result.ok or not result.data:
        return False, "Nothing was changed."

    action = result.data.get("action")
    player = result.data.get("player") or ""
    after = _now_playing(player)
    if after is None:
        if action == "stop":
            return True, "Confirmed: no player is reporting playback any more."
        return False, "The player stopped reporting its state, so this cannot be confirmed."

    status = after.get("status", "")
    expected = {
        "play": "playing",
        "pause": "paused",
        "stop": "stopped",
    }.get(str(action))

    if expected and status == expected:
        return True, f"Confirmed: {after.get('player') or 'the player'} reports {status}."
    if expected:
        return False, (
            f"{after.get('player') or 'The player'} reports {status!r} rather than "
            f"{expected!r}. Some players ignore commands while loading."
        )

    if action in ("next", "previous"):
        before = result.data.get("before") or {}
        if (after.get("title") or "") != (before.get("title") or ""):
            return True, f"Confirmed: now playing {_describe(after)}."
        return False, (
            "The track did not change. The player may have only one item queued, or "
            "may not support skipping."
        )

    if action == "toggle":
        before = result.data.get("before") or {}
        if status != before.get("status"):
            return True, f"Confirmed: the player went from {before.get('status')} to {status}."
        return False, f"The player still reports {status}."

    return True, f"The player reports {status}."


@tool(
    "media.control",
    title="Control playback",
    description=(
        "Play, pause, stop, or skip to the next or previous track in whatever is "
        "playing -- a music player, or a browser playing video."
    ),
    capability="media.control",
    idempotent=False,
    requires=("playerctl|busctl",),
    verifier=_verify_media,
    timeout_s=25.0,
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(_ACTIONS),
                "description": "What to do.",
            },
            "player": {
                "type": "string",
                "description": (
                    "Which player, e.g. 'spotify' or 'firefox'. Omit to act on "
                    "whichever is playing."
                ),
            },
        },
        "required": ["action"],
    },
)
def media_control(params: dict[str, Any]) -> ToolResult:
    if not backend():
        return ToolResult(
            ok=False,
            error=(
                "NEXUS cannot reach a media player on this computer: neither "
                "'playerctl' nor 'busctl' is installed, and MPRIS needs one of them."
            ),
        )

    action = str(params.get("action") or "").strip().lower()
    if action not in _ACTIONS:
        return ToolResult(
            ok=False,
            error=f"{action!r} is not a playback action. Choose from: " + ", ".join(_ACTIONS),
        )

    requested_player = str(params.get("player") or "").strip()
    available = _players()
    if not available:
        return ToolResult(
            ok=False,
            error="Nothing is playing and no media player is running, so there is nothing to control.",
        )

    player = ""
    if requested_player:
        matches = [p for p in available if requested_player.lower() in p.lower()]
        if not matches:
            return ToolResult(
                ok=False,
                error=(
                    f"No player matching {requested_player!r} is running. "
                    f"Currently running: {', '.join(available)}."
                ),
            )
        player = matches[0]

    before = _now_playing(player)
    code, stderr = _command(player, action)
    if code != 0:
        detail = stderr or f"the player rejected {action} (exit {code})"
        if "not implement" in detail.lower() or "no such method" in detail.lower():
            return ToolResult(
                ok=False,
                error=(
                    f"{player or 'That player'} does not support {action}. "
                    "Some browsers only implement play and pause."
                ),
            )
        return ToolResult(ok=False, error=f"Could not {action}: {detail}")

    time.sleep(SETTLE_SECONDS)
    after = _now_playing(player)
    resolved = (after or before or {}).get("player") or player or "the active player"

    return ToolResult(
        ok=True,
        data={
            "action": action,
            "player": player,
            "resolved_player": resolved,
            "players_running": available,
            "before": before,
            "after": after,
        },
        summary={
            "play": f"Playing {_describe(after)}",
            "pause": f"Paused {resolved}",
            "toggle": f"Toggled {resolved} -- now {(after or {}).get('status', 'unknown')}",
            "stop": f"Stopped {resolved}",
            "next": f"Skipped to {_describe(after)}",
            "previous": f"Went back to {_describe(after)}",
        }[action],
    )


@tool(
    "media.status",
    title="What is playing",
    description="Report what is currently playing, and in which player.",
    capability="media.control",
    read_only=True,
    requires=("playerctl|busctl",),
    parameters={
        "type": "object",
        "properties": {
            "player": {"type": "string", "description": "Limit to one player."},
        },
    },
)
def media_status(params: dict[str, Any]) -> ToolResult:
    if not backend():
        return ToolResult(
            ok=False,
            error=(
                "NEXUS cannot see what is playing: neither 'playerctl' nor 'busctl' "
                "is installed, and MPRIS needs one of them."
            ),
        )

    requested = str(params.get("player") or "").strip()
    available = _players()
    if not available:
        return ToolResult(
            ok=True,
            data={"players": [], "playing": None},
            summary="Nothing is playing -- no media player is running.",
            verified=True,
            verification=f"No MPRIS player is on the session bus (asked with {backend()}).",
        )

    targets = (
        [p for p in available if requested.lower() in p.lower()] if requested else available
    )
    if requested and not targets:
        return ToolResult(
            ok=False,
            error=(
                f"No player matching {requested!r} is running. "
                f"Currently running: {', '.join(available)}."
            ),
        )

    states = [state for state in (_now_playing(p) for p in targets) if state]
    playing = next((s for s in states if s.get("status") == "playing"), None)

    if playing:
        summary = f"Playing {_describe(playing)}"
    elif states:
        summary = (
            f"{states[0].get('player') or 'A player'} is "
            f"{states[0].get('status', 'idle')} on {_describe(states[0])}"
        )
    else:
        summary = f"{len(targets)} player(s) running, none reporting a track."

    return ToolResult(
        ok=True,
        data={"players": available, "states": states, "playing": playing},
        summary=summary,
        verified=True,
        verification=f"Read from each player over MPRIS, with {backend()}.",
    )
