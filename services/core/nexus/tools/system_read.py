"""Additive system readings (tool batch B1; spec §38, §60, §78).

``system.status`` already answers the broad question -- battery, memory, disk,
CPU load, uptime, network -- in one call. The tools here deliberately do **not**
re-slice that snapshot into a dozen thin wrappers; a ``system.memory`` that
returned the same number ``system.status`` already reports would be a tool whose
only purpose is to look like progress. Each tool below reads something
``system.status`` does *not* cover:

* ``system.cpu`` -- per-core busy percentage and current frequency, sampled from
  ``/proc/stat`` over a short interval. ``system.status`` gives one load average
  for the whole machine; this says *which* core is hot.
* ``system.temperature`` -- thermal sensors, which ``system.status`` never reads.
* ``system.mounts`` -- the full mount table with filesystem types and options,
  where ``system.status`` reports only usage of ``/`` and ``$HOME``.
* ``system.datetime`` -- clock, timezone and NTP sync state.
* ``system.locale`` -- language and regional settings.
* ``system.users`` -- who is logged in.
* ``system.env`` -- environment variables, with credential-shaped values redacted.

All of these are reads of *machine* state and carry the existing, implicit
``system.info`` capability -- the same one ``system.status`` uses. None changes
anything, so none has a verifier: there is nothing to confirm (§60), and saying
so is more honest than attaching a check that always passes.

The ``/proc`` and ``/sys`` readers are imported from :mod:`nexus.tools.system`
rather than copied, so the two modules cannot drift into reading the same file
two different ways.
"""

from __future__ import annotations

import os
import platform
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger, redact_data
from .registry import ToolResult, tool
from .system import _human_duration, _read, _run, _uptime_seconds

log = get_logger("tools.system_read")


# --------------------------------------------------------------------- CPU


def _cpu_times() -> dict[str, tuple[int, int]]:
    """Per-core (busy, total) jiffies from /proc/stat.

    Keyed by core label ("cpu0", "cpu1", ...). "busy" excludes idle and iowait,
    so a core waiting on disk does not read as loaded -- the same distinction the
    kernel itself draws.
    """
    times: dict[str, tuple[int, int]] = {}
    for line in _read("/proc/stat").splitlines():
        parts = line.split()
        if not parts or not parts[0].startswith("cpu") or parts[0] == "cpu":
            continue
        try:
            nums = [int(x) for x in parts[1:]]
        except ValueError:
            continue
        if len(nums) < 5:
            continue
        idle = nums[3] + nums[4]  # idle + iowait
        total = sum(nums)
        times[parts[0]] = (total - idle, total)
    return times


def _cpu_frequencies() -> dict[str, float]:
    """Current MHz per core, from cpufreq when the kernel exposes it."""
    freqs: dict[str, float] = {}
    for cpu_dir in sorted(Path("/sys/devices/system/cpu").glob("cpu[0-9]*")):
        cur = _read(cpu_dir / "cpufreq" / "scaling_cur_freq")
        if cur.isdigit():
            freqs[cpu_dir.name] = int(cur) / 1000.0  # kHz -> MHz
    return freqs


@tool(
    "system.cpu",
    title="Read CPU load per core",
    description=(
        "Report each processor core's busy percentage and current speed, sampled "
        "over a short interval. Use this to see which core is under load, not just "
        "the overall average."
    ),
    capability="system.info",
    read_only=True,
    parameters={
        "type": "object",
        "properties": {
            "sample_ms": {
                "type": "integer",
                "minimum": 50,
                "maximum": 2000,
                "default": 200,
                "description": "How long to sample core activity, in milliseconds.",
            }
        },
    },
)
def system_cpu(params: dict[str, Any]) -> ToolResult:
    sample_ms = int(params.get("sample_ms", 200))
    first = _cpu_times()
    time.sleep(sample_ms / 1000.0)
    second = _cpu_times()
    freqs = _cpu_frequencies()

    cores: list[dict[str, Any]] = []
    for label in sorted(first, key=lambda s: int(s[3:])):
        if label not in second:
            continue
        busy0, total0 = first[label]
        busy1, total1 = second[label]
        dtotal = total1 - total0
        pct = round((busy1 - busy0) / dtotal * 100, 1) if dtotal > 0 else 0.0
        core: dict[str, Any] = {"core": int(label[3:]), "busy_percent": pct}
        if label in freqs:
            core["mhz"] = round(freqs[label], 0)
        cores.append(core)

    if not cores:
        return ToolResult(
            ok=False,
            error="Could not read per-core CPU statistics from /proc/stat on this computer.",
            summary="No CPU statistics available",
        )

    overall = round(sum(c["busy_percent"] for c in cores) / len(cores), 1)
    hottest = max(cores, key=lambda c: c["busy_percent"])
    data = {
        "cores": cores,
        "core_count": len(cores),
        "overall_busy_percent": overall,
        "model": platform.processor() or None,
    }
    summary = (
        f"{len(cores)} cores, {overall}% busy overall; "
        f"busiest is core {hottest['core']} at {hottest['busy_percent']}%"
    )
    return ToolResult(ok=True, data=data, summary=summary)


# ------------------------------------------------------------- temperature


def _thermal_from_sysfs() -> list[dict[str, Any]]:
    """Temperatures from /sys/class/thermal, the reading every Linux has."""
    readings: list[dict[str, Any]] = []
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        raw = _read(zone / "temp")
        if not raw.lstrip("-").isdigit():
            continue
        celsius = int(raw) / 1000.0
        readings.append(
            {
                "sensor": _read(zone / "type", zone.name) or zone.name,
                "celsius": round(celsius, 1),
            }
        )
    return readings


@tool(
    "system.temperature",
    title="Read hardware temperatures",
    description=(
        "Report temperature sensor readings for this computer's CPU and other "
        "components, in degrees Celsius."
    ),
    capability="system.info",
    read_only=True,
    parameters={"type": "object", "properties": {}},
)
def system_temperature(_params: dict[str, Any]) -> ToolResult:
    readings = _thermal_from_sysfs()
    source = "/sys/class/thermal"

    # lm-sensors, when installed, labels sensors far better than the bare thermal
    # zones do ("Core 0" instead of "thermal_zone2"). Prefer it, but only as an
    # enrichment: the sysfs read above already guarantees an answer.
    proc = _run(["sensors", "-j"], timeout=4.0)
    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        import json

        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            labelled: list[dict[str, Any]] = []
            for chip, features in parsed.items():
                if not isinstance(features, dict):
                    continue
                for feature, values in features.items():
                    if not isinstance(values, dict):
                        continue
                    for key, val in values.items():
                        if key.endswith("_input") and isinstance(val, (int, float)):
                            labelled.append(
                                {"sensor": f"{chip}/{feature}", "celsius": round(float(val), 1)}
                            )
            if labelled:
                readings = labelled
                source = "sensors"

    if not readings:
        return ToolResult(
            ok=False,
            error=(
                "This computer exposes no temperature sensors NEXUS can read "
                "(no /sys/class/thermal zones and 'sensors' returned nothing)."
            ),
            summary="No temperature sensors available",
        )

    hottest = max(readings, key=lambda r: r["celsius"])
    return ToolResult(
        ok=True,
        data={"readings": readings, "source": source},
        summary=f"Hottest sensor {hottest['sensor']} at {hottest['celsius']}°C "
                f"({len(readings)} sensor(s) read)",
    )


# ----------------------------------------------------------------- mounts

# Pseudo-filesystems that describe the kernel, not storage the user has. Reported
# separately from real mounts so "what disks are mounted" is not drowned in fifty
# cgroup and proc entries.
_PSEUDO_FS = {
    "proc", "sysfs", "cgroup", "cgroup2", "devpts", "devtmpfs", "tmpfs",
    "mqueue", "hugetlbfs", "debugfs", "tracefs", "securityfs", "pstore",
    "bpf", "configfs", "fusectl", "autofs", "binfmt_misc", "ramfs",
    "efivarfs", "rpc_pipefs", "nsfs", "overlay", "squashfs",
}


@tool(
    "system.mounts",
    title="List mounted filesystems",
    description=(
        "List the filesystems mounted on this computer -- device, mount point, "
        "type and options -- with free space for real disks. Kernel pseudo-"
        "filesystems are listed separately so the real disks stand out."
    ),
    capability="system.info",
    read_only=True,
    parameters={
        "type": "object",
        "properties": {
            "include_pseudo": {
                "type": "boolean",
                "default": False,
                "description": "Also include kernel pseudo-filesystems (proc, cgroup, tmpfs, ...).",
            }
        },
    },
)
def system_mounts(params: dict[str, Any]) -> ToolResult:
    include_pseudo = bool(params.get("include_pseudo", False))
    import shutil

    real: list[dict[str, Any]] = []
    pseudo: list[dict[str, Any]] = []
    for line in _read("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        device, mountpoint, fstype, options = parts[0], parts[1], parts[2], parts[3]
        # /proc/mounts octal-escapes spaces and tabs in paths.
        mountpoint = mountpoint.encode().decode("unicode_escape")
        entry: dict[str, Any] = {
            "device": device,
            "mount": mountpoint,
            "type": fstype,
            "options": options.split(","),
        }
        if fstype in _PSEUDO_FS:
            pseudo.append(entry)
            continue
        try:
            usage = shutil.disk_usage(mountpoint)
            entry["total"] = usage.total
            entry["free"] = usage.free
            entry["percent_used"] = (
                round(usage.used / usage.total * 100, 1) if usage.total else 0.0
            )
        except OSError:
            pass
        real.append(entry)

    data: dict[str, Any] = {"mounts": real}
    if include_pseudo:
        data["pseudo"] = pseudo
    summary = f"{len(real)} real filesystem(s) mounted"
    if not include_pseudo and pseudo:
        summary += f" ({len(pseudo)} kernel pseudo-filesystems hidden)"
    return ToolResult(ok=True, data=data, summary=summary)


# ---------------------------------------------------------------- datetime


@tool(
    "system.datetime",
    title="Read the date, time and timezone",
    description=(
        "Report this computer's current local time, UTC time, timezone and whether "
        "its clock is synchronised with a network time server."
    ),
    capability="system.info",
    read_only=True,
    parameters={"type": "object", "properties": {}},
)
def system_datetime(_params: dict[str, Any]) -> ToolResult:
    now_local = datetime.now().astimezone()
    now_utc = datetime.now(timezone.utc)

    tz_name = None
    localtime = Path("/etc/localtime")
    if localtime.is_symlink():
        target = os.readlink(localtime)
        if "zoneinfo/" in target:
            tz_name = target.split("zoneinfo/", 1)[1]
    tz_name = tz_name or now_local.tzname()

    ntp_synced: bool | None = None
    proc = _run(["timedatectl", "show", "--property=NTPSynchronized", "--value"])
    if proc is not None and proc.returncode == 0:
        ntp_synced = proc.stdout.strip() == "yes"

    data = {
        "local": now_local.isoformat(timespec="seconds"),
        "utc": now_utc.isoformat(timespec="seconds"),
        "timezone": tz_name,
        "utc_offset": now_local.strftime("%z"),
        "ntp_synchronized": ntp_synced,
        "uptime_human": _human_duration(_uptime_seconds()),
    }
    sync = "" if ntp_synced is None else (
        ", clock synced to network time" if ntp_synced else ", clock NOT network-synced"
    )
    return ToolResult(
        ok=True,
        data=data,
        summary=f"{now_local.strftime('%Y-%m-%d %H:%M')} {tz_name}{sync}",
    )


# ------------------------------------------------------------------ locale


@tool(
    "system.locale",
    title="Read language and regional settings",
    description=(
        "Report this computer's language, character encoding and regional "
        "formatting settings (the LANG and LC_* settings)."
    ),
    capability="system.info",
    read_only=True,
    parameters={"type": "object", "properties": {}},
)
def system_locale(_params: dict[str, Any]) -> ToolResult:
    keys = ("LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_NUMERIC", "LC_TIME",
            "LC_MONETARY", "LC_MESSAGES")
    settings_map = {k: os.environ[k] for k in keys if os.environ.get(k)}

    # `locale` resolves what the settings actually evaluate to, including defaults
    # the environment leaves implicit.
    resolved: dict[str, str] = {}
    proc = _run(["locale"])
    if proc is not None and proc.returncode == 0:
        for line in proc.stdout.splitlines():
            key, _, value = line.partition("=")
            if key:
                resolved[key] = value.strip().strip('"')

    lang = settings_map.get("LANG") or resolved.get("LANG") or "unset"
    return ToolResult(
        ok=True,
        data={"environment": settings_map, "resolved": resolved},
        summary=f"Language {lang}",
    )


# ------------------------------------------------------------------- users


@tool(
    "system.users",
    title="See who is logged in",
    description=(
        "List the user sessions currently logged in to this computer, with where "
        "and since when."
    ),
    capability="system.info",
    read_only=True,
    parameters={"type": "object", "properties": {}},
)
def system_users(_params: dict[str, Any]) -> ToolResult:
    sessions: list[dict[str, str]] = []
    proc = _run(["who"])
    if proc is not None and proc.returncode == 0:
        for line in proc.stdout.splitlines():
            parts = line.split(None, 4)
            if len(parts) >= 2:
                sessions.append(
                    {
                        "user": parts[0],
                        "terminal": parts[1],
                        "since": " ".join(parts[2:4]) if len(parts) >= 4 else "",
                        "from": parts[4].strip("()") if len(parts) >= 5 else "",
                    }
                )

    current = None
    try:
        current = os.getlogin()
    except OSError:
        current = os.environ.get("USER")

    if not sessions and current:
        # `who` reads utmp, which a graphical-only login may not populate. Say what
        # is certain (the account this process runs as) rather than "nobody".
        return ToolResult(
            ok=True,
            data={"sessions": [], "current_user": current},
            summary=f"No login sessions recorded in utmp; running as {current}",
        )

    users = sorted({s["user"] for s in sessions})
    return ToolResult(
        ok=True,
        data={"sessions": sessions, "current_user": current},
        summary=f"{len(sessions)} session(s): {', '.join(users)}" if sessions else "No sessions",
    )


# --------------------------------------------------------------------- env


@tool(
    "system.env",
    title="Read environment variables",
    description=(
        "Report this computer's environment variables. Values that look like "
        "passwords, tokens or keys are redacted before they are returned."
    ),
    capability="system.info",
    read_only=True,
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Read only this one variable. Omit to read all of them.",
            }
        },
    },
)
def system_env(params: dict[str, Any]) -> ToolResult:
    name = params.get("name")
    if name:
        if name not in os.environ:
            return ToolResult(
                ok=False,
                error=f"There is no environment variable called '{name}'.",
                summary=f"{name} is not set",
            )
        # redact_data scrubs the value if it is credential-shaped, so reading
        # e.g. an API key by name still does not hand it back in the clear.
        safe = redact_data({name: os.environ[name]})
        return ToolResult(ok=True, data={"variables": safe}, summary=f"Read {name}")

    safe_all = redact_data(dict(os.environ))
    return ToolResult(
        ok=True,
        data={"variables": safe_all},
        summary=f"Read {len(safe_all)} environment variables (secret-shaped values redacted)",
    )
