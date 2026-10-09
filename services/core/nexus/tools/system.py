"""System status and system control (task D5; spec §38, §60, §78).

Two very different kinds of thing live here, and they are kept apart on purpose:

* **Reading status** -- battery, memory, disks, uptime, network. This is done
  from ``/proc`` and ``/sys`` directly, with no third-party dependency, because
  the numbers are already there and an extra package is an extra way to be
  wrong.
* **Changing state** -- volume, brightness, power. These need external programs,
  and Linux desktops disagree about which ones exist. Each tool tries the
  backends in order of fidelity and reports which one it used. When none is
  present it says exactly what to install rather than silently doing nothing
  (§78).

Brightness deserves a specific note: ``xrandr`` "brightness" is a software gamma
adjustment, not a backlight change. The tool prefers real backlight control and,
when it has to fall back, says which one it did -- because "the screen looks
dimmer" and "the panel is using less power" are different claims.

The process pair at the end of the file straddles that split, and is written so the
two halves stay recognisably apart: ``system.processes`` only reads ``/proc``, and
``system.kill_process`` only sends a signal to one process it has already resolved
to a single pid. There is deliberately no "kill everything matching this" path.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from threading import Lock
from typing import Any

from ..logging_setup import get_logger, redact
from .registry import ToolResult, tool

log = get_logger("tools.system")


def _run(cmd: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess[str] | None:
    if shutil.which(cmd[0]) is None:
        return None
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _read(path: str | Path, default: str = "") -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return default


def _human_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _human_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# ------------------------------------------------------------------- readings


def _meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        number = rest.strip().split(" ")[0]
        if number.isdigit():
            values[key] = int(number) * 1024  # /proc/meminfo is in kB
    return values


def _battery() -> dict[str, Any] | None:
    """Battery state, or None on a machine that has no battery."""
    supplies = sorted(Path("/sys/class/power_supply").glob("BAT*"))
    if not supplies:
        return None
    bat = supplies[0]
    capacity = _read(bat / "capacity")
    status = _read(bat / "status") or "Unknown"

    result: dict[str, Any] = {
        "percent": int(capacity) if capacity.isdigit() else None,
        "status": status,
        "charging": status in ("Charging", "Full"),
        "device": bat.name,
    }

    # Time remaining, when the kernel exposes enough to compute it honestly.
    energy_now = _read(bat / "energy_now") or _read(bat / "charge_now")
    power_now = _read(bat / "power_now") or _read(bat / "current_now")
    if energy_now.isdigit() and power_now.isdigit() and int(power_now) > 0:
        hours = int(energy_now) / int(power_now)
        if status == "Discharging":
            result["time_remaining"] = _human_duration(hours * 3600)
    return result


def _disks() -> list[dict[str, Any]]:
    seen: set[str] = set()
    disks: list[dict[str, Any]] = []
    for mount in ("/", str(Path.home())):
        try:
            usage = shutil.disk_usage(mount)
        except OSError:
            continue
        key = f"{usage.total}:{usage.free}"
        if key in seen:
            continue  # home is on the root filesystem; don't report it twice
        seen.add(key)
        disks.append(
            {
                "mount": mount,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent_used": round(usage.used / usage.total * 100, 1) if usage.total else 0.0,
                "total_human": _human_bytes(usage.total),
                "free_human": _human_bytes(usage.free),
            }
        )
    return disks


def _network() -> dict[str, Any]:
    interfaces: list[dict[str, str]] = []
    for iface in sorted(Path("/sys/class/net").glob("*")):
        if iface.name == "lo":
            continue
        interfaces.append(
            {
                "name": iface.name,
                "state": _read(iface / "operstate", "unknown"),
                "type": "wireless" if (iface / "wireless").exists() else "wired",
            }
        )

    # A default route is the closest thing to "connected" that can be known
    # without sending traffic somewhere, which would be a privacy decision.
    has_default = False
    for line in _read("/proc/net/route").splitlines()[1:]:
        parts = line.split()
        if len(parts) > 2 and parts[1] == "00000000":
            has_default = True
            break

    return {
        "interfaces": interfaces,
        "has_default_route": has_default,
        "note": "A default route means this computer knows where to send traffic. "
                "It is not proof the internet is reachable.",
    }


def _cpu_model() -> str:
    for line in _read("/proc/cpuinfo").splitlines():
        if line.lower().startswith("model name"):
            return line.partition(":")[2].strip()
    return platform.processor() or "unknown"


def _os_name() -> str:
    for line in _read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.partition("=")[2].strip().strip('"')
    return f"{platform.system()} {platform.release()}"


@tool(
    "system.status",
    title="Read system status",
    description=(
        "Report this computer's battery, memory, disk space, CPU load, uptime "
        "and network interfaces."
    ),
    capability="system.info",
    read_only=True,
    parameters={"type": "object", "properties": {}},
)
def system_status(_params: dict[str, Any]) -> ToolResult:
    mem = _meminfo()
    total = mem.get("MemTotal", 0)
    available = mem.get("MemAvailable", 0)
    uptime_raw = _read("/proc/uptime").split(" ")[0]
    uptime = float(uptime_raw) if uptime_raw.replace(".", "").isdigit() else 0.0
    load = _read("/proc/loadavg").split(" ")[:3]
    battery = _battery()
    disks = _disks()

    data = {
        "host": platform.node(),
        "os": _os_name(),
        "kernel": platform.release(),
        "uptime_seconds": uptime,
        "uptime_human": _human_duration(uptime),
        "cpu": {
            "model": _cpu_model(),
            "cores": os.cpu_count(),
            "load_1m": float(load[0]) if load and load[0] else None,
            "load_5m": float(load[1]) if len(load) > 1 else None,
            "load_15m": float(load[2]) if len(load) > 2 else None,
        },
        "memory": {
            "total": total,
            "available": available,
            "used": total - available,
            "percent_used": round((total - available) / total * 100, 1) if total else 0.0,
            "total_human": _human_bytes(total),
            "available_human": _human_bytes(available),
            "swap_total": mem.get("SwapTotal", 0),
            "swap_free": mem.get("SwapFree", 0),
        },
        "disks": disks,
        "battery": battery,
        "network": _network(),
    }

    parts = [f"Up {data['uptime_human']}"]
    if battery and battery.get("percent") is not None:
        state = "charging" if battery["charging"] else "on battery"
        parts.append(f"battery {battery['percent']}% ({state})")
    parts.append(f"{_human_bytes(available)} RAM free of {_human_bytes(total)}")
    if disks:
        parts.append(f"{disks[0]['free_human']} free on {disks[0]['mount']}")

    return ToolResult(ok=True, data=data, summary="; ".join(parts))


# --------------------------------------------------------------------- volume

_PACTL_SINK = "@DEFAULT_SINK@"


def _volume_state() -> dict[str, Any] | None:
    """Current volume via pactl, then amixer. None when neither works."""
    proc = _run(["pactl", "get-sink-volume", _PACTL_SINK])
    if proc and proc.returncode == 0:
        match = re.search(r"(\d+)%", proc.stdout)
        mute = _run(["pactl", "get-sink-mute", _PACTL_SINK])
        if match:
            return {
                "percent": int(match.group(1)),
                "muted": bool(mute and "yes" in mute.stdout.lower()),
                "backend": "pactl",
            }

    proc = _run(["amixer", "-D", "pulse", "sget", "Master"]) or _run(["amixer", "sget", "Master"])
    if proc and proc.returncode == 0:
        match = re.search(r"\[(\d+)%\]", proc.stdout)
        if match:
            return {
                "percent": int(match.group(1)),
                "muted": "[off]" in proc.stdout,
                "backend": "amixer",
            }
    return None


def _verify_volume(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    if not result.data:
        return False, "Nothing was changed."
    time.sleep(0.2)
    state = _volume_state()
    if state is None:
        return False, "Could not read the volume back, so the change is unconfirmed."

    if params.get("mute") is not None or params.get("action") == "toggle_mute":
        expected = result.data.get("muted")
        if state["muted"] == expected:
            return True, f"Confirmed: audio is {'muted' if state['muted'] else 'unmuted'}."
        return False, f"Audio is {'muted' if state['muted'] else 'unmuted'}, which is not what was asked."

    wanted = result.data.get("percent")
    if wanted is None:
        return True, f"Volume is {state['percent']}%."
    # Hardware steps are not always exact; 3 points is within one step everywhere.
    if abs(state["percent"] - int(wanted)) <= 3:
        return True, f"Confirmed: volume is {state['percent']}%."
    return False, f"Volume is {state['percent']}%, not the {wanted}% that was requested."


@tool(
    "system.volume",
    title="Change the volume",
    description=(
        "Read or change the system audio volume. Use action 'get' to read it, "
        "'set' with a percent, 'up'/'down' to step, or 'mute'/'unmute'/'toggle_mute'."
    ),
    capability="system.volume",
    verifier=_verify_volume,
    # Either mixer will do, and there is no third way: unlike brightness, which
    # falls back to writing sysfs directly, an audio server that answers neither
    # pactl nor amixer cannot be driven at all.
    requires=("pactl|amixer",),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["get", "set", "up", "down", "mute", "unmute", "toggle_mute"],
                "default": "get",
            },
            "percent": {"type": "integer", "minimum": 0, "maximum": 100},
            "step": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
        },
    },
)
def system_volume(params: dict[str, Any]) -> ToolResult:
    action = params.get("action", "get")
    current = _volume_state()
    if current is None:
        return ToolResult(
            ok=False,
            error=(
                "NEXUS cannot control the volume on this computer: neither pactl "
                "(PulseAudio/PipeWire) nor amixer (ALSA) responded. Installing "
                "'pulseaudio-utils' or 'alsa-utils' would enable it."
            ),
        )

    if action == "get":
        return ToolResult(
            ok=True,
            data=current,
            summary=f"Volume is {current['percent']}%" + (" (muted)" if current["muted"] else ""),
            verified=True,
            verification="Read directly from the audio system.",
        )

    backend = current["backend"]
    target: int | None = None
    muted: bool | None = None

    if action == "set":
        if params.get("percent") is None:
            return ToolResult(ok=False, error="Setting the volume needs a percent value.")
        target = int(params["percent"])
    elif action == "up":
        target = min(100, current["percent"] + int(params.get("step", 10)))
    elif action == "down":
        target = max(0, current["percent"] - int(params.get("step", 10)))
    elif action == "mute":
        muted = True
    elif action == "unmute":
        muted = False
    else:  # toggle_mute
        muted = not current["muted"]

    if backend == "pactl":
        cmd = (
            ["pactl", "set-sink-volume", _PACTL_SINK, f"{target}%"]
            if target is not None
            else ["pactl", "set-sink-mute", _PACTL_SINK, "1" if muted else "0"]
        )
    else:
        cmd = (
            ["amixer", "-q", "sset", "Master", f"{target}%"]
            if target is not None
            else ["amixer", "-q", "sset", "Master", "mute" if muted else "unmute"]
        )

    proc = _run(cmd)
    if proc is None or proc.returncode != 0:
        detail = proc.stderr.strip() if proc else "the command could not be run"
        return ToolResult(ok=False, error=f"Could not change the volume: {detail}")

    summary = (
        f"Volume set to {target}%" if target is not None
        else ("Audio muted" if muted else "Audio unmuted")
    )
    return ToolResult(
        ok=True,
        data={"percent": target, "muted": muted, "backend": backend, "previous": current},
        summary=summary,
    )


# ----------------------------------------------------------------- brightness


def _backlight_devices() -> list[Path]:
    return sorted(Path("/sys/class/backlight").glob("*"))


def _brightness_state() -> dict[str, Any] | None:
    proc = _run(["brightnessctl", "-m"])
    if proc and proc.returncode == 0 and proc.stdout.strip():
        # device,class,current,percent,max
        fields = proc.stdout.strip().splitlines()[0].split(",")
        if len(fields) >= 5 and fields[4].isdigit():
            return {
                "percent": int(fields[3].rstrip("%")),
                "device": fields[0],
                "backend": "brightnessctl",
                "hardware": True,
            }

    for device in _backlight_devices():
        current = _read(device / "brightness")
        maximum = _read(device / "max_brightness")
        if current.isdigit() and maximum.isdigit() and int(maximum) > 0:
            return {
                "percent": round(int(current) / int(maximum) * 100),
                "device": device.name,
                "backend": "sysfs",
                "hardware": True,
                "writable": os.access(device / "brightness", os.W_OK),
            }
    return None


def _verify_brightness(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    if not result.data or result.data.get("backend") == "xrandr":
        # xrandr gamma is not readable as a percentage, so there is nothing to
        # check against. Saying "verified" here would be a guess.
        return False, ("The screen was dimmed in software; there is no hardware value to read "
                       "back, so this could not be independently confirmed.")
    time.sleep(0.2)
    state = _brightness_state()
    if state is None:
        return False, "Could not read the brightness back."
    wanted = result.data.get("percent")
    if wanted is not None and abs(state["percent"] - int(wanted)) <= 3:
        return True, f"Confirmed: brightness is {state['percent']}%."
    return False, f"Brightness is {state['percent']}%, not the {wanted}% requested."


def _brightness_change_unavailable() -> str | None:
    """Whether the *change* path can actually work on this machine.

    Reading brightness needs only a readable backlight; changing it needs
    either ``brightnessctl`` (which is set up to write the device without root)
    or a ``/sys/class/backlight`` device whose ``brightness`` file this user can
    write directly -- which, without a udev rule, is usually root-only. A laptop
    therefore commonly has a backlight that reads fine and refuses every write,
    with no ``brightnessctl`` installed: ``get`` succeeds and every ``set``
    fails. That is the over-report the registry's ``requires`` note warns about,
    so the tool is reported unavailable here with the one-line fix, rather than
    offered to the model as a control it cannot actually use.

    The tool's job is *changing* brightness (its title says so), so this gates
    the whole tool: a machine that can only read the value cannot do what the
    tool is for.
    """
    if shutil.which("brightnessctl"):
        return None
    devices = _backlight_devices()
    if not devices:
        return ("This computer has no backlight under /sys/class/backlight, which is normal "
                "for a desktop -- the monitor's own buttons control its brightness.")
    if any(os.access(device / "brightness", os.W_OK) for device in devices):
        return None
    return ("This computer's backlight is read-only for NEXUS. Installing 'brightnessctl' "
            "lets it change the brightness without administrator rights.")


@tool(
    "system.brightness",
    title="Change screen brightness",
    description="Read or change the display brightness.",
    capability="system.brightness",
    verifier=_verify_brightness,
    available_probe=_brightness_change_unavailable,
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["get", "set", "up", "down"], "default": "get"},
            "percent": {"type": "integer", "minimum": 1, "maximum": 100},
            "step": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
        },
    },
)
def system_brightness(params: dict[str, Any]) -> ToolResult:
    action = params.get("action", "get")
    current = _brightness_state()

    if action == "get":
        if current is None:
            return ToolResult(
                ok=False,
                error=(
                    "This computer does not expose a readable backlight. That is normal "
                    "for a desktop monitor, where brightness is controlled by the monitor's "
                    "own buttons."
                ),
            )
        return ToolResult(
            ok=True, data=current,
            summary=f"Brightness is {current['percent']}%",
            verified=True, verification=f"Read from {current['device']}.",
        )

    if current is None:
        return ToolResult(
            ok=False,
            error=(
                "NEXUS cannot change the brightness on this computer: there is no backlight "
                "device under /sys/class/backlight. On a desktop monitor, brightness is a "
                "hardware control on the monitor itself."
            ),
        )

    if action == "set":
        if params.get("percent") is None:
            return ToolResult(ok=False, error="Setting brightness needs a percent value.")
        target = int(params["percent"])
    elif action == "up":
        target = min(100, current["percent"] + int(params.get("step", 10)))
    else:
        target = max(1, current["percent"] - int(params.get("step", 10)))

    if current["backend"] == "brightnessctl":
        proc = _run(["brightnessctl", "-q", "set", f"{target}%"])
        if proc is not None and proc.returncode == 0:
            return ToolResult(
                ok=True,
                data={"percent": target, "backend": "brightnessctl", "previous": current},
                summary=f"Brightness set to {target}%",
            )
        detail = proc.stderr.strip() if proc else "brightnessctl did not run"
        return ToolResult(ok=False, error=f"Could not change brightness: {detail}")

    device = Path("/sys/class/backlight") / current["device"]
    maximum = _read(device / "max_brightness")
    if not maximum.isdigit():
        return ToolResult(ok=False, error="Could not read the backlight's maximum value.")
    value = max(1, round(int(maximum) * target / 100))
    try:
        (device / "brightness").write_text(str(value))
    except OSError as exc:
        return ToolResult(
            ok=False,
            error=(
                f"Could not write to the backlight device ({exc}). Changing brightness this way "
                "needs write access to /sys/class/backlight, which normally requires installing "
                "'brightnessctl' so it can be done without administrator rights."
            ),
        )
    return ToolResult(
        ok=True,
        data={"percent": target, "backend": "sysfs", "previous": current},
        summary=f"Brightness set to {target}%",
    )


# ---------------------------------------------------------------------- power

# Ordered by preference. Each entry is a list of candidate commands; the first
# whose binary exists is used.
_POWER_COMMANDS: dict[str, list[list[str]]] = {
    "lock": [
        ["loginctl", "lock-session"],
        ["xdg-screensaver", "lock"],
        ["xflock4"],
        ["i3lock"],
        ["gnome-screensaver-command", "--lock"],
    ],
    "sleep": [["systemctl", "suspend"], ["loginctl", "suspend"]],
    "restart": [["systemctl", "reboot"], ["loginctl", "reboot"]],
    "shutdown": [["systemctl", "poweroff"], ["loginctl", "poweroff"]],
    "logout": [["loginctl", "terminate-session", os.environ.get("XDG_SESSION_ID", "self")]],
}

_POWER_CONSEQUENCE = {
    "lock": "The screen will lock. Nothing is lost.",
    "sleep": "This computer will suspend. Open applications stay open.",
    "restart": "This computer will restart. Unsaved work in any application will be lost.",
    "shutdown": "This computer will shut down. Unsaved work in any application will be lost.",
    "logout": "You will be logged out. Unsaved work in any application will be lost.",
}

#: The union of every candidate above, as one alternatives group. It answers the
#: only question availability can answer for a tool with five actions: whether any
#: of them could work at all. Which *individual* actions are possible depends on
#: which of these is present, and the handler says so per action rather than
#: pretending a machine with only ``i3lock`` can shut itself down.
_POWER_REQUIREMENT = "|".join(
    dict.fromkeys(cmd[0] for candidates in _POWER_COMMANDS.values() for cmd in candidates)
)


@tool(
    "system.power",
    title="Lock, sleep, restart or shut down",
    description=(
        "Lock the screen, suspend, log out, restart or shut down this computer. "
        "Restart, shutdown and logout close every open application."
    ),
    capability="system.power",
    idempotent=False,
    requires=(_POWER_REQUIREMENT,),
    timeout_s=20.0,
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["lock", "sleep", "restart", "shutdown", "logout"],
            },
        },
        "required": ["action"],
    },
)
def system_power(params: dict[str, Any]) -> ToolResult:
    action = str(params["action"])
    candidates = _POWER_COMMANDS[action]

    chosen: list[str] | None = None
    for cmd in candidates:
        if shutil.which(cmd[0]) is not None:
            chosen = cmd
            break
    if chosen is None:
        wanted = ", ".join(sorted({c[0] for c in candidates}))
        return ToolResult(
            ok=False,
            error=f"NEXUS cannot {action} this computer: none of {wanted} is installed.",
        )

    try:
        proc = subprocess.run(chosen, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return ToolResult(ok=False, error=f"Could not {action}: {exc}")

    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"{chosen[0]} exited with {proc.returncode}"
        hint = ""
        if "not authorized" in detail.lower() or "interactive authentication" in detail.lower():
            hint = (
                " This desktop requires you to confirm power actions yourself; NEXUS "
                "deliberately does not run anything with administrator rights."
            )
        return ToolResult(ok=False, error=f"Could not {action} this computer: {detail}.{hint}")

    if action == "lock":
        time.sleep(0.5)
        locked = _run(["loginctl", "show-session", os.environ.get("XDG_SESSION_ID", "self"),
                       "-p", "LockedHint"])
        if locked and "yes" in locked.stdout.lower():
            return ToolResult(
                ok=True, data={"action": action, "via": chosen[0]},
                summary="Screen locked", verified=True,
                verification="Confirmed: the session reports itself locked.",
            )
        return ToolResult(
            ok=True, data={"action": action, "via": chosen[0]},
            summary="Asked the screen to lock",
            verified=None,
            verification="This desktop does not report a lock state, so NEXUS cannot confirm it.",
        )

    # For sleep/restart/shutdown there is nothing left to verify: either the
    # machine is going away, or the request was refused above. Claiming a
    # verified result here would be meaningless (§60).
    return ToolResult(
        ok=True,
        data={"action": action, "via": chosen[0]},
        summary=f"{action.capitalize()} requested. {_POWER_CONSEQUENCE[action]}",
        verified=None,
        verification=f"{chosen[0]} accepted the request. What happens next is out of NEXUS's hands.",
    )


# ------------------------------------------------------------------ clipboard


#: Clipboard readers and writers, in order of preference: (backend name, command).
#: Split by direction rather than kept as one (read, write) pair per backend,
#: because the two halves of wl-clipboard are two separate binaries: pairing them
#: meant a machine with ``wl-paste`` but no ``wl-copy`` could not read the
#: clipboard either, which is neither true nor what the error message said.
_CLIPBOARD_READERS: tuple[tuple[str, list[str]], ...] = (
    ("wayland", ["wl-paste", "--no-newline"]),
    ("xclip", ["xclip", "-selection", "clipboard", "-o"]),
    ("xsel", ["xsel", "--clipboard", "--output"]),
)

_CLIPBOARD_WRITERS: tuple[tuple[str, list[str]], ...] = (
    ("wayland", ["wl-copy"]),
    ("xclip", ["xclip", "-selection", "clipboard", "-in"]),
    ("xsel", ["xsel", "--clipboard", "--input"]),
)

#: Declared requirements, derived from the tables so a new backend cannot be added
#: without the availability probe learning about it. Any one program is enough.
_CLIPBOARD_READ_REQUIREMENT = "|".join(cmd[0] for _, cmd in _CLIPBOARD_READERS)
_CLIPBOARD_WRITE_REQUIREMENT = "|".join(cmd[0] for _, cmd in _CLIPBOARD_WRITERS)


def _clipboard_readers() -> list[tuple[str, list[str]]]:
    return [(name, cmd) for name, cmd in _CLIPBOARD_READERS if shutil.which(cmd[0])]


def _clipboard_writers() -> list[tuple[str, list[str]]]:
    return [(name, cmd) for name, cmd in _CLIPBOARD_WRITERS if shutil.which(cmd[0])]


@tool(
    "clipboard.get",
    title="Read the clipboard",
    description="Read what is currently on the clipboard.",
    capability="clipboard.read",
    read_only=True,
    requires=(_CLIPBOARD_READ_REQUIREMENT,),
    parameters={"type": "object", "properties": {}},
)
def clipboard_get(_params: dict[str, Any]) -> ToolResult:
    for name, read_cmd in _clipboard_readers():
        try:
            proc = subprocess.run(read_cmd, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            text = proc.stdout
            return ToolResult(
                ok=True,
                data={"text": text, "length": len(text), "backend": name,
                      "empty": not text.strip()},
                summary=(f"Clipboard holds {len(text)} character(s)" if text.strip()
                         else "The clipboard is empty"),
                verified=True,
                verification="Read directly from the clipboard.",
            )
    return ToolResult(
        ok=False,
        error=("NEXUS cannot read the clipboard: none of wl-paste, xclip or xsel is "
               "installed. Installing 'xclip' would enable it."),
    )


def _verify_clipboard_set(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    expected = str(params.get("text", ""))
    for _, read_cmd in _clipboard_readers():
        try:
            proc = subprocess.run(read_cmd, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            if proc.stdout.rstrip("\n") == expected.rstrip("\n"):
                return True, "Confirmed: the clipboard now holds that text."
            return False, "The clipboard does not hold the text that was set."
    return False, "Could not read the clipboard back."


@tool(
    "clipboard.set",
    title="Set the clipboard",
    description="Replace the clipboard contents with the given text.",
    capability="clipboard.write",
    idempotent=True,
    verifier=_verify_clipboard_set,
    requires=(_CLIPBOARD_WRITE_REQUIREMENT,),
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "maxLength": 500_000},
        },
        "required": ["text"],
    },
)
def clipboard_set(params: dict[str, Any]) -> ToolResult:
    text = str(params["text"])
    failures: list[str] = []
    for name, write_cmd in _clipboard_writers():
        try:
            # No ``capture_output``, and that is load-bearing rather than a style
            # choice. xclip and wl-copy **fork a child that keeps holding the
            # selection** -- that is how a clipboard survives the program that set
            # it. With stdout/stderr pipes attached, the forked child inherits the
            # write ends, so ``communicate()`` waits for EOF that never comes:
            # measured here at a hard 5-second timeout, every time, on a machine
            # where the write itself takes 8 ms. The old code then swallowed the
            # TimeoutExpired and told the user "none of wl-copy, xclip or xsel is
            # installed" -- naming a missing program as the fix when the program
            # was installed and had in fact already copied the text.
            proc = subprocess.run(write_cmd, input=text, text=True, timeout=5)
        except subprocess.TimeoutExpired:
            # A writer that outlived the timeout has still handed the selection to
            # its forked child; the write below is what decides, not the exit code.
            pass
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append(f"{name}: {exc}")
            continue
        else:
            if proc.returncode != 0:
                failures.append(f"{name}: exited {proc.returncode}")
                continue

        # Verify by reading it back, like every other write tool here. This is also
        # what makes the timeout path above safe: if the text did not stick, the
        # read-back fails and the tool still reports honestly.
        read_back, _ = _read_clipboard_now()
        if read_back == text:
            return ToolResult(
                ok=True,
                data={"length": len(text), "backend": name, "verified_by_read_back": True},
                summary=f"Copied {len(text)} character(s) to the clipboard",
            )
        failures.append(f"{name}: wrote, but the clipboard reads back differently")

    detail = f" ({'; '.join(failures[:2])})" if failures else ""
    return ToolResult(
        ok=False,
        error=(
            "NEXUS could not set the clipboard. "
            + (
                "None of wl-copy, xclip or xsel is installed, so there is no program "
                "to write it with. Installing 'xclip' would enable it."
                if not _clipboard_writers()
                else "A writer is installed but the write did not take."
            )
            + detail
        ),
    )


def _read_clipboard_now() -> tuple[str | None, str]:
    """Read the clipboard back, using the same table ``clipboard.get`` uses."""
    for name, read_cmd in _clipboard_readers():
        try:
            proc = subprocess.run(
                read_cmd, text=True, capture_output=True, timeout=5
            )
        except (OSError, subprocess.SubprocessError) as exc:
            continue
        if proc.returncode == 0:
            return proc.stdout, ""
    return None, "no reader produced a value"


# --------------------------------------------------------------- notification


@tool(
    "system.notify",
    title="Send a desktop notification",
    description="Show a desktop notification with a title and message.",
    capability="notify.send",
    requires=("notify-send",),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "message": {"type": "string", "maxLength": 1000, "default": ""},
            "urgency": {"type": "string", "enum": ["low", "normal", "critical"], "default": "normal"},
        },
        "required": ["title"],
    },
)
def system_notify(params: dict[str, Any]) -> ToolResult:
    title = str(params["title"])
    message = str(params.get("message", ""))
    urgency = params.get("urgency", "normal")
    cmd = ["notify-send", "-a", "NEXUS", "-u", urgency, title]
    if message:
        cmd.append(message)

    proc = _run(cmd)
    if proc is None or proc.returncode != 0:
        detail = proc.stderr.strip() if proc else "notify-send did not run"
        return ToolResult(ok=False, error=f"Could not send the notification: {detail}")
    return ToolResult(
        ok=True,
        data={"title": title, "message": message, "urgency": urgency},
        summary=f"Notification sent: {title}",
        verified=None,
        verification=("notify-send accepted it. Whether a notification daemon actually "
                      "displayed it cannot be checked from here."),
    )


# ------------------------------------------------------------------- processes
#
# Read from /proc, not from `ps`. The reason is the same one the module docstring
# gives for the status readers -- the numbers are already there -- plus one that is
# specific to this pair: `ps` output has to be parsed out of a column layout that
# changes with the user's locale and with which `ps` is installed, and a process
# name containing a space would break the parse silently. /proc gives each field
# separately and never lies about where a value ends.

_CLOCK_TICKS = os.sysconf("SC_CLK_TCK") or 100
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") or 4096

#: How long CPU use is measured over. See :func:`_sampled_processes`. The length is
#: not arbitrary: the kernel counts CPU time in whole clock ticks (100 per second
#: here), so a single tick appearing during the interval is the smallest change
#: that can be seen -- which makes the resolution ``100 / (ticks * interval)``
#: percentage points. At 0.15s that is a granularity of nearly 7 points, coarse
#: enough that idle and mildly busy look the same; at 0.4s it is 2.5 points, for
#: 0.4s of latency in a tool that is asked once and read carefully.
_CPU_SAMPLE_SECONDS = 0.4

#: How long ``system.kill_process`` waits for a process to actually go away
#: before reporting that it did not.
_KILL_WAIT_SECONDS = 3.0

_STATE_WORDS = {
    "R": "running",
    "S": "sleeping",
    "D": "waiting for disk",
    "T": "stopped",
    "t": "stopped by a debugger",
    "Z": "zombie",
    "X": "dead",
    "I": "idle",
}


def _pids() -> list[int]:
    try:
        return sorted(int(entry) for entry in os.listdir("/proc") if entry.isdigit())
    except OSError:
        return []


def _proc_stat(pid: int) -> tuple[str, list[str]] | None:
    """``(name, fields-after-the-name)`` from ``/proc/<pid>/stat``.

    Returns None when the process is gone, which is a normal outcome rather than an
    error: between listing /proc and reading one entry, a process is free to exit.

    The name is taken from the *last* ``)`` in the line rather than by splitting on
    whitespace. A program chooses its own name, so ``comm`` may contain spaces and
    brackets -- ``(foo) 0 0 0`` is a legal process name -- and splitting on spaces
    would shift every field after it. This is the one part of /proc parsing that is
    genuinely easy to get wrong.
    """
    raw = _read(f"/proc/{pid}/stat")
    if not raw:
        return None
    opened = raw.find("(")
    closed = raw.rfind(")")
    if opened == -1 or closed == -1 or closed < opened:
        return None
    return raw[opened + 1:closed], raw[closed + 1:].split()


def _proc_row(pid: int) -> dict[str, Any] | None:
    """One process as a plain dict, or None if it vanished or looks unparseable.

    Field numbers are the ones in ``proc(5)``, counted from the whole line, so
    ``fields`` -- which starts after the name at field 3 -- is indexed at ``n - 3``.
    """
    parsed = _proc_stat(pid)
    if parsed is None:
        return None
    name, fields = parsed
    if len(fields) < 22:
        return None
    try:
        return {
            "pid": pid,
            "name": name,
            "state": fields[0],                        # field 3
            "ppid": int(fields[1]),                    # field 4
            "cpu_ticks": int(fields[11]) + int(fields[12]),  # utime 14 + stime 15
            "threads": int(fields[17]),                # field 20
            "start_ticks": int(fields[19]),            # field 22
            "rss_bytes": int(fields[21]) * _PAGE_SIZE,  # field 24, in pages
        }
    except (IndexError, ValueError):
        return None


def _process_snapshot() -> dict[int, dict[str, Any]]:
    """Every process currently readable, keyed by pid."""
    rows: dict[int, dict[str, Any]] = {}
    for pid in _pids():
        row = _proc_row(pid)
        if row is not None:
            rows[pid] = row
    return rows


def _cmdline(pid: int) -> str:
    """The command line a process was started with, with credentials removed.

    Two things about this string matter. It is NUL-separated on disk, so it has to
    be split on ``\\0`` rather than read as text -- otherwise every argument runs
    into the next. And it is the most revealing field in the whole listing: it
    carries paths, URLs, and sometimes a password passed on a command line. This
    text can reach a cloud model, so the same redactor the logs use is applied here
    for the same reason (§7). A kernel thread has an empty cmdline; that is not a
    failure, it just has no command line.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    parts = [part for part in raw.decode("utf-8", "replace").split("\0") if part]
    return redact(" ".join(parts).strip())


def _sampled_processes(
    interval: float = _CPU_SAMPLE_SECONDS,
) -> tuple[list[dict[str, Any]], float]:
    """Every process, with CPU use measured rather than guessed.

    A single read of /proc gives a process's CPU total *since it started*, which
    answers "how much has this used in its lifetime" -- not the question anyone
    actually asks, which is "what is busy right now". A four-day-old browser and a
    runaway loop that started a minute ago can show the same lifetime total. So this
    reads /proc twice, ``interval`` apart, and divides the difference by the wall
    time that passed. The cost is being deliberately a fraction of a second slow
    instead of instant and wrong.

    Percentages are per core: a process using four cores fully reads as 400. They
    are also quantised -- see :data:`_CPU_SAMPLE_SECONDS` -- so the returned
    ``cpu_sampled_over_ms`` and ``cpu_resolution_percent`` are part of the answer,
    not decoration.
    """
    first = _process_snapshot()
    started = time.monotonic()
    time.sleep(max(0.0, interval))
    second = _process_snapshot()
    elapsed = time.monotonic() - started

    rows: list[dict[str, Any]] = []
    for pid, row in second.items():
        before = first.get(pid)
        if before is not None and elapsed > 0:
            delta = row["cpu_ticks"] - before["cpu_ticks"]
            cpu: float | None = round(max(0.0, delta / _CLOCK_TICKS / elapsed * 100.0), 1)
        else:
            # Started during the sample, so there is no earlier total to subtract
            # from and therefore no rate to report. None says exactly that; 0.0
            # would claim the process is idle, which is not something we know.
            cpu = None
        rows.append({**row, "cpu_percent": cpu})
    return rows, elapsed


def _uptime_seconds() -> float:
    raw = _read("/proc/uptime").split(" ")[0]
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _describe(row: dict[str, Any], total_memory: int, uptime: float) -> dict[str, Any]:
    """One process in the shape the tool returns and the model reads."""
    rss = row["rss_bytes"]
    age = uptime - row["start_ticks"] / _CLOCK_TICKS
    return {
        "pid": row["pid"],
        "name": row["name"],
        "state": _STATE_WORDS.get(row["state"], row["state"]),
        "ppid": row["ppid"],
        "threads": row["threads"],
        "cpu_percent": row["cpu_percent"],
        "memory_bytes": rss,
        "memory_human": _human_bytes(rss),
        "memory_percent": round(rss / total_memory * 100, 1) if total_memory else None,
        "running_for": _human_duration(age) if age >= 0 else None,
        "cmdline": _cmdline(row["pid"]),
    }


@tool(
    "system.processes",
    title="List running processes",
    description=(
        "List the programs running on this computer with how much memory and CPU each "
        "is using. Use this to answer what is running, or what is using the most memory "
        "or CPU, or to find the pid of a program before ending it."
    ),
    capability="system.processes",
    read_only=True,
    timeout_s=20.0,
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "maxLength": 200,
                "default": "",
                "description": "Only processes whose name or command line contains this.",
            },
            "sort": {
                "type": "string",
                "enum": ["memory", "cpu", "name", "pid", "newest"],
                "default": "memory",
                "description": "Which way to order the list. Defaults to heaviest memory first.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "default": 15,
                "description": "How many processes to return.",
            },
        },
    },
)
def system_processes(params: dict[str, Any]) -> ToolResult:
    query = str(params.get("query", "") or "").strip()
    sort = str(params.get("sort", "memory") or "memory")
    limit = int(params.get("limit", 15) or 15)

    rows, elapsed = _sampled_processes()
    if not rows:
        return ToolResult(
            ok=False,
            error="Could not read /proc, so NEXUS cannot say what is running.",
        )

    total_memory = _meminfo().get("MemTotal", 0)
    uptime = _uptime_seconds()
    described = [_describe(row, total_memory, uptime) for row in rows]

    if query:
        needle = query.lower()
        matched = [
            proc
            for proc in described
            if needle in proc["name"].lower() or needle in proc["cmdline"].lower()
        ]
    else:
        matched = described

    if sort == "cpu":
        # None means "could not be measured", not zero, so those sort last rather
        # than mixing in among genuinely idle processes.
        matched.sort(key=lambda p: (p["cpu_percent"] is not None, p["cpu_percent"] or 0.0), reverse=True)
    elif sort == "name":
        matched.sort(key=lambda p: (p["name"].lower(), p["pid"]))
    elif sort == "pid":
        matched.sort(key=lambda p: p["pid"])
    elif sort == "newest":
        matched.sort(key=lambda p: p["pid"], reverse=True)
    else:
        matched.sort(key=lambda p: p["memory_bytes"], reverse=True)

    shown = matched[:limit]
    data = {
        "total": len(described),
        "matched": len(matched),
        "shown": len(shown),
        "query": query,
        "sort": sort,
        "truncated": len(matched) > len(shown),
        "memory_total_bytes": total_memory,
        "cpu_sampled_over_ms": int(elapsed * 1000),
        "cpu_resolution_percent": (
            round(100 / (_CLOCK_TICKS * elapsed), 1) if elapsed > 0 else None
        ),
        "cores": os.cpu_count(),
        "processes": shown,
    }

    if query and not matched:
        return ToolResult(
            ok=True,
            data=data,
            summary=f"Nothing running matches '{query}' ({len(described)} processes checked).",
        )

    # The headline follows the sort, because a listing ordered by CPU whose one-line
    # summary talks about memory answers a question nobody asked.
    if sort == "cpu" and any(p["cpu_percent"] for p in matched):
        busiest = max(matched, key=lambda p: p["cpu_percent"] or 0.0)
        headline = f"{busiest['name']} is the busiest at {busiest['cpu_percent']}% of a core"
    else:
        heaviest = max(matched, key=lambda p: p["memory_bytes"])
        share = f" ({heaviest['memory_percent']}% of memory)" if heaviest["memory_percent"] else ""
        headline = f"{heaviest['name']} is using the most memory at {heaviest['memory_human']}{share}"

    if query:
        summary = f"{len(matched)} of {len(described)} processes match '{query}'; {headline}."
    else:
        summary = f"{len(described)} processes running; {headline}."
    return ToolResult(ok=True, data=data, summary=summary)


# A pid this tool has already sent SIGTERM to, remembered with the process's start
# time so a reused pid does not inherit the permission. This is what makes the
# "TERM before KILL" rule in `system_kill_process` enforceable rather than merely
# recommended: force=True is refused for a process that has not been asked politely
# first. Tools can run on more than one thread, so the map is locked.
_TERM_SENT: dict[int, int] = {}
_TERM_LOCK = Lock()

#: Entries are removed as soon as the process is confirmed gone, so this only holds
#: processes that survived SIGTERM. That set is small, but it is not self-clearing in
#: a service that runs for weeks, so it is capped: oldest entry out first.
_TERM_SENT_MAX = 64


def _remember_term(pid: int, start_ticks: int) -> None:
    with _TERM_LOCK:
        _TERM_SENT[pid] = start_ticks
        while len(_TERM_SENT) > _TERM_SENT_MAX:
            _TERM_SENT.pop(next(iter(_TERM_SENT)))


def _still_the_same_process(pid: int, start_ticks: int) -> dict[str, Any] | None:
    """The process, if pid still names the *same* process it did a moment ago.

    Comparing start time matters because pids are reused. Without it, a poll that
    saw "something is still at pid 4242" could be looking at a brand-new process
    that took the pid of the one that just died, and the tool would report a
    successful kill as a failure -- or, worse, a later force-kill would fire at an
    innocent process.
    """
    row = _proc_row(pid)
    if row is None or row["start_ticks"] != start_ticks:
        return None
    return row


def _resolve_by_pid_or_name(
    params: dict[str, Any],
    snapshot: dict[int, dict[str, Any]],
    own_pid: int,
    verb: str = "signal",
) -> tuple[dict[str, Any] | None, ToolResult | None]:
    """Turn a ``pid`` *or* a ``name`` into exactly one process, or an error.

    Returns ``(target_row, None)`` when the request names one process, and
    ``(None, error_result)`` when it names zero, both a pid and a name, or an
    ambiguous set. Every mutating process tool -- end, freeze, resume, reprioritize
    -- resolves its target the same way, so the rule lives here once rather than in
    four private copies that would drift. ``verb`` only fills the two messages that
    read more naturally with the specific action ("which one to end" vs "to freeze");
    every other string is action-neutral.

    The self / init / zombie policy is deliberately *not* enforced here: those lines
    are drawn differently by each caller (freezing pid 1 is catastrophic; renicing it
    just fails harmlessly), so each handler applies its own after resolving.
    """
    raw_pid = params.get("pid")
    name = str(params.get("name", "") or "").strip()

    if raw_pid in (None, "") and not name:
        return None, ToolResult(
            ok=False,
            error=f"Say which process to {verb}: either a pid or a name.",
        )
    if raw_pid not in (None, "") and name:
        return None, ToolResult(
            ok=False,
            error=(
                "Give a pid or a name, not both. If they disagree there is no safe way "
                "to decide which one was meant."
            ),
        )

    if name:
        needle = name.lower()
        # Names first, command lines only if no name matched. A search for "code"
        # should find the editor, not every process whose arguments mention a path
        # with "code" in it -- and the fallback still finds things started through
        # an interpreter, where the process name is "python3".
        candidates = [
            row for pid, row in snapshot.items()
            if pid != own_pid and needle in row["name"].lower()
        ]
        if not candidates:
            candidates = [
                row for pid, row in snapshot.items()
                if pid != own_pid and needle in _cmdline(pid).lower()
            ]
        if not candidates:
            return None, ToolResult(ok=False, error=f"Nothing running is called '{name}'.")
        if len(candidates) > 1:
            candidates.sort(key=lambda row: row["rss_bytes"], reverse=True)
            listed = ", ".join(
                f"{row['pid']} ({row['name']}, {_human_bytes(row['rss_bytes'])})"
                for row in candidates[:8]
            )
            more = "" if len(candidates) <= 8 else f", and {len(candidates) - 8} more"
            return None, ToolResult(
                ok=False,
                error=(
                    f"{len(candidates)} processes match '{name}', so NEXUS will not guess "
                    f"which one to {verb}. Name a pid: {listed}{more}."
                ),
                data={
                    "matches": [
                        {
                            "pid": row["pid"],
                            "name": row["name"],
                            "memory_bytes": row["rss_bytes"],
                            "memory_human": _human_bytes(row["rss_bytes"]),
                            "cmdline": _cmdline(row["pid"]),
                        }
                        for row in candidates
                    ]
                },
            )
        return candidates[0], None

    try:
        pid = int(raw_pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None, ToolResult(ok=False, error=f"'{raw_pid}' is not a pid.")
    found = snapshot.get(pid)
    if found is None:
        return None, ToolResult(ok=False, error=f"No process with pid {pid} is running.")
    return found, None


@tool(
    "system.kill_process",
    title="End a running process",
    description=(
        "End one running process, by pid or by name. Sends SIGTERM, which asks the "
        "program to shut down, and then checks whether it actually exited. Set force "
        "only after a plain attempt has been made and the process is still running: "
        "that sends SIGKILL, which gives it no chance to save anything."
    ),
    capability="system.kill_process",
    idempotent=False,
    timeout_s=15.0,
    parameters={
        "type": "object",
        # Neither `pid` nor `name` can be marked required: exactly one of them is
        # wanted, and a schema of this shape cannot say "one of these two". The
        # handler checks it instead, and says which mistake was made.
        "properties": {
            "pid": {
                "type": "integer",
                "minimum": 1,
                "description": "The process id to end. Use system.processes to find it.",
            },
            "name": {
                "type": "string",
                "maxLength": 200,
                "description": (
                    "The process name to end. Refused when more than one process matches, "
                    "rather than guessing."
                ),
            },
            "force": {
                "type": "boolean",
                "default": False,
                "description": "Send SIGKILL instead of SIGTERM. Only allowed after SIGTERM was tried.",
            },
        },
    },
)
def system_kill_process(params: dict[str, Any]) -> ToolResult:
    force = bool(params.get("force", False))

    own = os.getpid()
    snapshot = _process_snapshot()

    target, problem = _resolve_by_pid_or_name(params, snapshot, own, verb="end")
    if problem is not None:
        return problem

    pid = target["pid"]
    label = target["name"]

    if pid == own:
        return ToolResult(
            ok=False,
            error=(
                f"pid {pid} is NEXUS itself. Ending it would kill the thing being asked "
                "to do the ending."
            ),
        )
    if pid == 1:
        return ToolResult(
            ok=False,
            error="pid 1 is the init system. Ending it would take the whole machine down.",
        )
    if target["state"] == "Z":
        return ToolResult(
            ok=False,
            error=(
                f"pid {pid} ({label}) is already dead -- it is a zombie waiting for its "
                "parent to collect it. A signal cannot do anything to it."
            ),
        )

    if force:
        with _TERM_LOCK:
            remembered = _TERM_SENT.get(pid)
        if remembered != target["start_ticks"]:
            return ToolResult(
                ok=False,
                error=(
                    f"NEXUS will not send SIGKILL to pid {pid} ({label}) as a first step. "
                    "SIGKILL gives a program no chance to save open work or clean up after "
                    "itself. Ask again without force -- that sends SIGTERM -- and if it is "
                    "still running afterwards, force is then allowed."
                ),
            )

    sig = signal.SIGKILL if force else signal.SIGTERM
    sig_name = "SIGKILL" if force else "SIGTERM"
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        # It exited between the snapshot and the signal. The requested end state is
        # the real one, and /proc is the evidence, so this is a verified success and
        # not an error -- but it should say what actually happened.
        with _TERM_LOCK:
            _TERM_SENT.pop(pid, None)
        return ToolResult(
            ok=True,
            data={"pid": pid, "name": label, "signal": None, "already_gone": True},
            summary=f"{label} (pid {pid}) had already exited; no signal was needed.",
            verified=True,
            verification=f"/proc/{pid} is gone.",
        )
    except PermissionError:
        return ToolResult(
            ok=False,
            error=(
                f"pid {pid} ({label}) belongs to another user, so NEXUS is not allowed to "
                "signal it. Ending it needs root."
            ),
        )
    except OSError as exc:
        return ToolResult(ok=False, error=f"Could not signal pid {pid} ({label}): {exc}")

    if not force:
        _remember_term(pid, target["start_ticks"])

    log.info("kill_process signalled", extra={"pid": pid, "name": label, "signal": sig_name})

    deadline = time.monotonic() + _KILL_WAIT_SECONDS
    became_zombie = False
    gone = False
    while time.monotonic() < deadline:
        time.sleep(0.05)
        current = _still_the_same_process(pid, target["start_ticks"])
        if current is None:
            gone = True
            break
        if current["state"] == "Z":
            # The program itself has ended; the entry stays until its parent reaps
            # it. Worth saying precisely, because "still in /proc" and "still
            # running" are different things.
            gone = True
            became_zombie = True
            break

    data = {
        "pid": pid,
        "name": label,
        "signal": sig_name,
        "ended": gone,
        "waited_ms": int(_KILL_WAIT_SECONDS * 1000),
    }

    if gone:
        with _TERM_LOCK:
            _TERM_SENT.pop(pid, None)
        detail = (
            f"/proc/{pid} now holds a zombie entry, which means the program has ended and "
            "its parent has not collected it yet."
            if became_zombie
            else f"/proc/{pid} is gone."
        )
        return ToolResult(
            ok=True,
            data=data,
            summary=f"Ended {label} (pid {pid}) with {sig_name}.",
            verified=True,
            verification=detail,
        )

    # The signal was delivered and the process is still there. That is not an
    # unverifiable action -- it is a known failure to achieve what was asked, which
    # is why this is ok=False rather than ok=True with verified=False.
    return ToolResult(
        ok=False,
        data=data,
        error=(
            f"{label} (pid {pid}) was sent {sig_name} but was still running "
            f"{_KILL_WAIT_SECONDS:.0f} seconds later."
            + (
                " It is ignoring SIGTERM; asking again with force will send SIGKILL."
                if not force
                else " A process that survives SIGKILL is stuck in the kernel, and only a "
                "restart will clear it."
            )
        ),
        verified=False,
        verification=f"/proc/{pid} still holds the same process.",
    )
