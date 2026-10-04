"""The system monitor's threshold path, driven through crossings it never saw.

The real monitor reads /proc and /sys, and no test can hold the machine at 95%
CPU while it watches. What a test *can* do is exactly what the monitor itself
does after reading: feed `_read_all` values and assert the reporting discipline
-- the edge-trigger with hysteresis that L2's docstring promises and no run of
the service has ever been observed exercising.

These tests pin three behaviours the docstring states:

- one alert per crossing, not one per tick (a machine sitting at 90.4% with the
  threshold at 90 must not produce an alert every minute forever);
- the clear is reported once, when the value falls a few points *below* the
  threshold, not the instant it grazes it;
- only a *discharging* low battery alerts, and only a *change* in network state
  does -- the two edge-triggered paths that are not plain thresholds.

Values are plain dicts in the shapes `_read_all` builds, so the contract being
tested is the one production code consumes.
"""

from __future__ import annotations

import functools
from typing import Any, Callable

from nexus.monitors.system_monitor import SystemMonitor


def sync(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


import asyncio  # noqa: E402  (kept beside its only use, as the sibling tests do)


def readings(
    *,
    cpu: float | None = 95.0,
    memory: float = 40.0,
    disks: list[dict[str, Any]] | None = None,
    battery: dict[str, Any] | None = None,
    network: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One `_read_all` payload with every value over/under every threshold."""
    return {
        "cpu": {
            "percent": cpu,
            "cores": 4,
            "load_1m": 1.0,
            "load_5m": 1.0,
            "load_15m": 1.0,
            "measured_over_interval": cpu is not None,
        },
        "memory": {
            "total_bytes": 8 * 1024**3,
            "available_bytes": int((1 - memory / 100) * 8 * 1024**3),
            "used_bytes": int(memory / 100 * 8 * 1024**3),
            "percent": memory,
            "swap_total_bytes": 0,
            "swap_used_bytes": 0,
        },
        "disks": disks if disks is not None else [],
        "battery": battery,
        "network": network
        if network is not None
        else {"has_default_route": True, "interface": "wlan0"},
        "temperatures": [],
    }


async def samples(monitor: SystemMonitor, payloads: list[dict[str, Any]]):
    """Run `sample()` over each payload and return the flat observation list."""
    out = []
    for payload in payloads:
        monitor._read_all = lambda p=payload: p  # type: ignore[method-assign]
        out.extend(await monitor.sample())
    return out


@sync
async def test_one_alert_per_crossing_not_per_tick():
    """Three ticks at 95% produce one CPU alert; hovering near it produces none."""
    monitor = SystemMonitor()
    await monitor.on_start()

    # default threshold: monitor_cpu_alert_percent = 90
    result = await samples(monitor, [readings(cpu=95.0)] * 3)

    assert len(result) == 1, f"three ticks over threshold gave {len(result)} alerts"
    assert result[0].kind == "system.cpu"
    assert result[0].severity == "alert"
    assert result[0].dedupe_key == "cpu"
    assert "threshold" in result[0].data


@sync
async def test_hysteresis_holds_until_value_falls_well_below():
    """95 -> 88 (within hysteresis) is silence; 88 -> 85 is one clear."""
    monitor = SystemMonitor()
    await monitor.on_start()

    result = await samples(
        monitor,
        [readings(cpu=95.0), readings(cpu=88.0), readings(cpu=88.0), readings(cpu=85.0)],
    )

    # First tick: the alert. Then 88 is within the 5-point hysteresis band
    # (90 - 5), so no clear yet -- twice, proving it is not edge-triggered on
    # "value changed". Only 85, below the band, clears.
    alerts = [o for o in result if o.severity == "alert"]
    clears = [o for o in result if o.severity == "notice"]
    assert len(alerts) == 1
    assert len(clears) == 1
    assert clears[0].dedupe_key == "cpu"
    assert clears[0].clears is True


@sync
async def test_recross_after_clear_alerts_again():
    """A new crossing after a clear is a new alert -- not suppressed forever."""
    monitor = SystemMonitor()
    await monitor.on_start()

    result = await samples(
        monitor,
        [readings(cpu=95.0), readings(cpu=80.0), readings(cpu=95.0)],
    )
    alerts = [o for o in result if o.severity == "alert"]
    assert len(alerts) == 2, "second crossing after a clear must alert again"
    assert all(o.kind == "system.cpu" for o in alerts)


@sync
async def test_first_sample_below_threshold_is_silent():
    """A monitor starting under threshold reports nothing -- no fake baseline."""
    monitor = SystemMonitor()
    await monitor.on_start()

    result = await samples(monitor, [readings(cpu=12.0)] * 3)
    assert result == []


@sync
async def test_memory_and_disk_thresholds_share_the_discipline():
    """Memory over 90 alerts once; a disk mount over 92 alerts once each."""
    monitor = SystemMonitor()
    await monitor.on_start()

    disk = {
        "mountpoint": "/",
        "fstype": "ext4",
        "total_bytes": 500 * 1024**3,
        "used_bytes": 470 * 1024**3,  # 94%
        "free_bytes": 30 * 1024**3,
        "percent": 94.0,
    }
    result = await samples(
        monitor, [readings(cpu=None, memory=96.0, disks=[disk])] * 2
    )
    kinds = [(o.kind, o.dedupe_key) for o in result]
    assert kinds == [
        ("system.memory", "memory"),
        ("system.disk", "disk:/"),
    ], f"two ticks over both thresholds gave {kinds}"


@sync
async def test_only_discharging_low_battery_alerts():
    """Low battery while plugged in is not a problem; discharging alerts once."""
    monitor = SystemMonitor()
    await monitor.on_start()

    plugged_low = {"percent": 12, "status": "Charging", "time_remaining": None}
    result = await samples(monitor, [readings(battery=plugged_low, cpu=10.0)] * 2)
    assert result == [], "low battery on AC must not alert"

    discharging = {"percent": 12, "status": "Discharging", "time_remaining": "45 min"}
    result = await samples(
        monitor,
        [readings(battery=discharging), readings(battery=discharging)],
    )
    alerts = [o for o in result if o.kind == "system.battery"]
    assert len(alerts) == 1
    assert "discharging" in alerts[0].summary
    assert alerts[0].severity == "alert"

    # Plugging back in clears it once.
    recovered = {"percent": 40, "status": "Charging", "time_remaining": None}
    result = await samples(monitor, [readings(battery=recovered, cpu=10.0)])
    clears = [o for o in result if o.kind == "system.battery"]
    assert len(clears) == 1
    assert clears[0].clears is True


@sync
async def test_network_change_reported_once_per_transition():
    """Connected -> disconnected -> disconnected yields one alert; back is one clear."""
    monitor = SystemMonitor()
    await monitor.on_start()

    connected = {"has_default_route": True, "interface": "wlan0"}
    gone = {"has_default_route": False, "interface": None}
    result = await samples(
        monitor,
        [readings(cpu=10.0, network=connected),
         readings(cpu=10.0, network=gone),
         readings(cpu=10.0, network=gone),
         readings(cpu=10.0, network=connected)],
    )
    net = [o for o in result if o.kind == "system.network"]
    assert len(net) == 2, f"transitions gave {len(net)} observations, want 2"
    assert net[0].severity == "alert" and net[0].summary.startswith("The network went away")
    assert net[1].severity == "notice" and net[1].clears is True
