"""System monitor (task L2; spec §35, §69, §82).

Samples CPU, memory, disk, battery, temperature and network on an interval and
reports the ones that crossed a threshold the user set.

The interesting part is not the sampling -- it is the reporting discipline. CPU
percentage is computed from ``/proc/stat`` deltas between ticks, which is the only
honest way to do it: a single read gives cumulative jiffies since boot, so the
"current CPU" from one read is really the average since the machine started.

Alerts are **edge-triggered with hysteresis**. A threshold at 90% and a machine
sitting at 90.4% would otherwise produce an alert on every tick forever. The
condition has to fall a few points below the threshold before it can fire again,
and the clear is reported too, so "CPU is back to normal" is a real event rather
than something the user infers from silence.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..core.trends import Series, describe_percentage
from ..tools.system import _battery, _human_bytes, _meminfo, _network
from .base import Monitor, Observation, setting

log = get_logger("monitors.system")

#: How far a value must fall below its threshold before the alert can fire again.
HYSTERESIS_POINTS = 5.0

#: Battery percentage a discharging trend is projected towards. Not zero: a machine
#: suspends before it reaches empty, and the useful warning is "you have about this
#: long", which stops being true well above 0.
BATTERY_FLOOR = 5.0

#: A trend is only worth saying once per this many seconds, per series. Trends are
#: computed every tick and a climb lasts many ticks, so without this the same
#: sentence would arrive every minute for as long as the climb continued -- which is
#: the failure mode the Debouncer exists for, except a trend's dedupe key cannot
#: carry it: each report is genuinely a *new* measurement of an ongoing condition.
TREND_COOLDOWN_SECONDS = 600.0

#: Where a rising series becomes worth mentioning before it crosses its threshold.
#: Below this there is nothing to anticipate -- the alert will say it soon enough.
TREND_WARN_AT_PERCENT = 70.0

#: Filesystem types that are not real storage; alerting on a full squashfs mount
#: would be noise the user can do nothing about.
_SKIP_FSTYPES = {
    "tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs", "cgroup",
    "cgroup2", "devpts", "efivarfs", "autofs", "fuse.gvfsd-fuse", "fuse.portal",
    "tracefs", "debugfs", "securityfs", "pstore", "bpf", "configfs", "ramfs",
    "mqueue", "hugetlbfs", "binfmt_misc", "nsfs",
}


@dataclass
class _CpuSample:
    total: float
    idle: float


def _read_cpu() -> _CpuSample | None:
    """Cumulative CPU jiffies from /proc/stat."""
    try:
        with open("/proc/stat", "r", encoding="utf-8") as handle:
            line = handle.readline()
    except OSError:
        return None
    if not line.startswith("cpu "):
        return None
    fields = [float(v) for v in line.split()[1:]]
    if len(fields) < 5:
        return None
    # user nice system idle iowait irq softirq steal ...
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0.0)
    return _CpuSample(total=sum(fields), idle=idle)


def _temperatures() -> list[dict[str, Any]]:
    """Thermal zone readings, where the kernel exposes them."""
    readings: list[dict[str, Any]] = []
    base = Path("/sys/class/thermal")
    if not base.is_dir():
        return readings
    for zone in sorted(base.glob("thermal_zone*")):
        try:
            raw = (zone / "temp").read_text(encoding="utf-8").strip()
            label = (zone / "type").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        try:
            celsius = int(raw) / 1000.0
        except ValueError:
            continue
        # Some zones report implausible values when a sensor is absent; a reading
        # of 0 or 250 degrees is a broken sensor, not a fact worth alerting on.
        if 1.0 < celsius < 150.0:
            readings.append({"zone": label, "celsius": round(celsius, 1)})
    return readings


def _mounts() -> list[tuple[str, str]]:
    """(mountpoint, fstype) for real filesystems."""
    out: list[tuple[str, str]] = []
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mountpoint, fstype = parts[1], parts[2]
                if fstype in _SKIP_FSTYPES or mountpoint.startswith(("/proc", "/sys", "/run")):
                    continue
                out.append((mountpoint.replace("\\040", " "), fstype))
    except OSError:
        return [("/", "unknown")]
    return out


class SystemMonitor(Monitor):
    id = "system"
    title = "System health"
    description = (
        "Watches CPU, memory, disk, battery and network, and says something when one "
        "crosses a limit you set."
    )
    enabled_setting = "monitor_system_enabled"
    interval_setting = "monitor_system_interval_seconds"
    default_interval = 60.0
    capability = "system.info"
    activity_kind = "system"

    def __init__(self) -> None:
        super().__init__()
        self._previous_cpu: _CpuSample | None = None
        #: Thresholds currently breached, so a clear can be reported once.
        self._breached: set[str] = set()
        self._last_network_state: str | None = None
        #: Rolling windows for anticipation (§37). One per measurement, in memory
        #: and lost on restart by design -- see ``core.trends.Series``.
        self._series: dict[str, Series] = {
            "cpu": Series("cpu"),
            "memory": Series("memory"),
            "battery": Series("battery"),
        }
        #: When each series last produced a spoken trend, for the cooldown.
        self._trend_said: dict[str, float] = {}

    async def on_start(self) -> None:
        # Prime the CPU delta so the first real sample measures an interval rather
        # than reporting the average since boot as if it were current load.
        self._previous_cpu = _read_cpu()
        self._breached.clear()
        # A restart is a gap in observation, so the windows start empty rather than
        # asserting continuity across it.
        for series in self._series.values():
            series.clear()
        self._trend_said.clear()

    async def sample(self) -> list[Observation]:
        observations: list[Observation] = []
        readings = self._read_all()

        thresholds = {
            "cpu": float(setting("monitor_cpu_alert_percent", 90)),
            "memory": float(setting("monitor_memory_alert_percent", 90)),
            "disk": float(setting("monitor_disk_alert_percent", 92)),
        }

        cpu_percent = readings["cpu"]["percent"]
        if cpu_percent is not None:
            observations.extend(
                self._threshold(
                    key="cpu",
                    value=cpu_percent,
                    limit=thresholds["cpu"],
                    kind="system.cpu",
                    over=f"CPU is at {cpu_percent:.0f}%",
                    under=f"CPU has settled to {cpu_percent:.0f}%",
                    data=readings["cpu"],
                )
            )

        memory = readings["memory"]
        if memory.get("percent") is not None:
            observations.extend(
                self._threshold(
                    key="memory",
                    value=memory["percent"],
                    limit=thresholds["memory"],
                    kind="system.memory",
                    over=(
                        f"Memory is at {memory['percent']:.0f}% -- "
                        f"{_human_bytes(memory['available_bytes'])} free of "
                        f"{_human_bytes(memory['total_bytes'])}"
                    ),
                    under=f"Memory has eased to {memory['percent']:.0f}%",
                    data=memory,
                )
            )

        for disk in readings["disks"]:
            observations.extend(
                self._threshold(
                    key=f"disk:{disk['mountpoint']}",
                    value=disk["percent"],
                    limit=thresholds["disk"],
                    kind="system.disk",
                    over=(
                        f"{disk['mountpoint']} is {disk['percent']:.0f}% full -- "
                        f"{_human_bytes(disk['free_bytes'])} left"
                    ),
                    under=f"{disk['mountpoint']} is back to {disk['percent']:.0f}% full",
                    data=disk,
                )
            )

        battery = readings.get("battery")
        if battery and battery.get("percent") is not None:
            limit = float(setting("monitor_battery_alert_percent", 15))
            discharging = str(battery.get("status", "")).lower() == "discharging"
            level = float(battery["percent"])
            # Only a *discharging* low battery is a problem. Alerting at 12% while
            # plugged in and charging would be a false alarm.
            if discharging and level <= limit:
                if "battery" not in self._breached:
                    self._breached.add("battery")
                    remaining = battery.get("time_remaining")
                    tail = f", about {remaining} left" if remaining else ""
                    observations.append(
                        Observation(
                            kind="system.battery",
                            summary=f"Battery is at {level:.0f}% and discharging{tail}",
                            data=battery,
                            severity="alert",
                            dedupe_key="battery",
                        )
                    )
            elif "battery" in self._breached and (not discharging or level > limit + HYSTERESIS_POINTS):
                self._breached.discard("battery")
                state = "charging" if not discharging else "discharging"
                observations.append(
                    Observation(
                        kind="system.battery",
                        summary=f"Battery is at {level:.0f}% and {state}",
                        data=battery,
                        severity="notice",
                        dedupe_key="battery",
                        clears=True,
                    )
                )

        network = readings["network"]
        state = "connected" if network.get("has_default_route") else "disconnected"
        if self._last_network_state is not None and state != self._last_network_state:
            observations.append(
                Observation(
                    kind="system.network",
                    summary=(
                        "The network came back"
                        if state == "connected"
                        else "The network went away -- no default route"
                    ),
                    data=network,
                    severity="notice" if state == "connected" else "alert",
                    dedupe_key="network",
                    clears=state == "connected",
                )
            )
        self._last_network_state = state

        observations.extend(self._trends(readings))

        return observations

    # --------------------------------------------------------------- anticipation

    def _trends(self, readings: dict[str, Any]) -> list[Observation]:
        """Report where a measurement is *heading*, where that is worth saying.

        This is the §37 anticipation half, and it is deliberately narrow. A trend is
        only emitted when all four of these hold, because a projection is a claim
        about the future and an eager version of this feature is a liar:

        1. the fit is trustworthy (enough samples, points near the line, not flat),
        2. the direction is the one that costs the user something,
        3. the value is close enough to mattering to be worth pre-empting,
        4. the same series has not spoken within ``TREND_COOLDOWN_SECONDS``.

        Battery is the case the specs single out (§52): a falling charge projects to
        a time, which is the sentence a person actually wants, and the projection is
        refused outright when it would reach further ahead than the window watched.
        """
        out: list[Observation] = []

        cpu = readings["cpu"].get("percent")
        if cpu is not None:
            self._series["cpu"].add(float(cpu))
        memory = readings["memory"].get("percent")
        if memory is not None:
            self._series["memory"].add(float(memory))

        battery = readings.get("battery") or {}
        level = battery.get("percent")
        discharging = str(battery.get("status", "")).lower() == "discharging"
        if level is not None and discharging:
            self._series["battery"].add(float(level))
        elif level is not None:
            # On AC the discharge window is meaningless; keeping it would let a
            # charge-then-discharge cycle fit a line across the plug event.
            self._series["battery"].clear()

        now = time.monotonic()

        for name, threshold, label in (
            ("cpu", float(setting("monitor_cpu_alert_percent", 90)), "CPU"),
            ("memory", float(setting("monitor_memory_alert_percent", 90)), "Memory"),
        ):
            trend = self._series[name].trend()
            if trend is None or not trend.trustworthy or not trend.rising:
                continue
            # Nothing to anticipate while the value is still low: the threshold
            # alert will say it soon enough, and a trend at 20% is noise with a
            # slope.
            if trend.last_value < TREND_WARN_AT_PERCENT:
                continue
            # A climb that is already over the line is the alert's business, not a
            # prediction's.
            if trend.last_value >= threshold:
                continue
            if not self._trend_due(name, now):
                continue

            minutes = trend.minutes_until(threshold)
            summary = describe_percentage(label, trend)
            if minutes is not None:
                summary += f", reaching {threshold:.0f}% in about {minutes:.0f} minutes"
            out.append(
                Observation(
                    kind="system.trend",
                    summary=summary,
                    data={
                        "series": name,
                        "threshold": threshold,
                        "minutes_until_threshold": round(minutes, 1) if minutes else None,
                        **trend.to_dict(),
                    },
                    severity="notice",
                    dedupe_key=f"trend:{name}",
                )
            )
            self._trend_said[name] = now

        out.extend(self._battery_trend(battery, now))
        return out

    def _battery_trend(self, battery: dict[str, Any], now: float) -> list[Observation]:
        """The §52 sentence: how long the charge has left, at the measured rate."""
        trend = self._series["battery"].trend()
        if trend is None or not trend.trustworthy or not trend.falling:
            return []
        if not self._trend_due("battery", now):
            return []
        minutes = trend.minutes_until(BATTERY_FLOOR)
        if minutes is None:
            # Falling, but not far enough along to project honestly inside the
            # window. Saying "it is falling" without a time adds nothing the
            # threshold alert will not say better.
            return []

        self._trend_said["battery"] = now
        return [
            Observation(
                kind="system.trend",
                summary=(
                    f"Battery is draining about "
                    f"{abs(trend.slope_per_minute):.1f}% a minute -- "
                    f"roughly {minutes:.0f} minutes to {BATTERY_FLOOR:.0f}% "
                    f"at this rate"
                ),
                data={
                    "series": "battery",
                    "floor_percent": BATTERY_FLOOR,
                    "minutes_until_floor": round(minutes, 1),
                    **trend.to_dict(),
                },
                severity="notice",
                dedupe_key="trend:battery",
            )
        ]

    def _trend_due(self, name: str, now: float) -> bool:
        said = self._trend_said.get(name)
        return said is None or (now - said) >= TREND_COOLDOWN_SECONDS

    # ---------------------------------------------------------------- readings

    def _read_all(self) -> dict[str, Any]:
        return {
            "cpu": self._cpu(),
            "memory": self._memory(),
            "disks": self._disks(),
            "battery": _battery(),
            "network": _network(),
            "temperatures": _temperatures(),
        }

    def _cpu(self) -> dict[str, Any]:
        """CPU busy percentage over the interval since the last sample."""
        current = _read_cpu()
        percent: float | None = None
        previous = self._previous_cpu
        if current and previous:
            total_delta = current.total - previous.total
            idle_delta = current.idle - previous.idle
            if total_delta > 0:
                percent = max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0))
        self._previous_cpu = current or previous

        try:
            load1, load5, load15 = os.getloadavg()
        except OSError:  # pragma: no cover - unsupported platform
            load1 = load5 = load15 = 0.0

        return {
            "percent": round(percent, 1) if percent is not None else None,
            "cores": os.cpu_count(),
            "load_1m": round(load1, 2),
            "load_5m": round(load5, 2),
            "load_15m": round(load15, 2),
            # None on the very first sample: there is no interval to measure yet,
            # and reporting the since-boot average as "current" would be wrong.
            "measured_over_interval": percent is not None,
        }

    def _memory(self) -> dict[str, Any]:
        info = _meminfo()
        total = info.get("MemTotal", 0) * 1024
        available = info.get("MemAvailable", 0) * 1024
        swap_total = info.get("SwapTotal", 0) * 1024
        swap_free = info.get("SwapFree", 0) * 1024
        used = total - available
        return {
            "total_bytes": total,
            "available_bytes": available,
            "used_bytes": used,
            "percent": round(used / total * 100.0, 1) if total else None,
            "swap_total_bytes": swap_total,
            "swap_used_bytes": swap_total - swap_free,
        }

    def _disks(self) -> list[dict[str, Any]]:
        seen: set[tuple[int, int]] = set()
        disks: list[dict[str, Any]] = []
        for mountpoint, fstype in _mounts():
            try:
                stat = os.stat(mountpoint)
                usage = shutil.disk_usage(mountpoint)
            except OSError:
                continue
            # Bind mounts and subvolumes report the same device; counting them
            # twice would double-alert on one full disk.
            device = (stat.st_dev, usage.total)
            if device in seen or usage.total == 0:
                continue
            seen.add(device)
            disks.append(
                {
                    "mountpoint": mountpoint,
                    "fstype": fstype,
                    "total_bytes": usage.total,
                    "used_bytes": usage.used,
                    "free_bytes": usage.free,
                    "percent": round(usage.used / usage.total * 100.0, 1),
                }
            )
        return disks

    # --------------------------------------------------------------- threshold

    def _threshold(
        self,
        *,
        key: str,
        value: float,
        limit: float,
        kind: str,
        over: str,
        under: str,
        data: dict[str, Any],
    ) -> list[Observation]:
        """Edge-triggered threshold with hysteresis. Returns 0 or 1 observation."""
        breached = key in self._breached

        if value >= limit and not breached:
            self._breached.add(key)
            return [
                Observation(
                    kind=kind,
                    summary=over,
                    data={**data, "threshold": limit},
                    severity="alert",
                    dedupe_key=key,
                )
            ]

        if breached and value <= limit - HYSTERESIS_POINTS:
            self._breached.discard(key)
            return [
                Observation(
                    kind=kind,
                    summary=under,
                    data={**data, "threshold": limit},
                    severity="notice",
                    dedupe_key=key,
                    clears=True,
                )
            ]

        return []

    def snapshot(self) -> dict[str, Any]:
        """Current readings without waiting for a tick, for the UI and for F4."""
        return self._read_all()
