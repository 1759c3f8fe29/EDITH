"""Behaviour of the additive system-read tools (batch B1).

Each test drives a *crafted* ``/proc``/``/sys`` reading or command output through
the real tool, so the assertion turns on the tool's own parsing and arithmetic --
not on whatever this machine happens to report. A test that merely called the
tool and checked ``ok is True`` would pass against a stub; these are written so a
mutant that swapped busy for idle, forgot to filter pseudo-filesystems, or handed
back an unredacted secret would fail.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nexus.tools import registry, system_read


def _call(name: str, params: dict[str, Any] | None = None) -> registry.ToolResult:
    tool = registry.get(name)
    assert tool is not None, f"{name} is not registered"
    cleaned = registry.validate(tool.parameters, params or {})
    return tool.handler(cleaned)


# ------------------------------------------------------------------- CPU


def test_cpu_percentage_is_the_busy_delta_over_the_total_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The number a user reads must be the change in busy time over the interval,
    not the cumulative-since-boot figure and not idle time."""
    # Two /proc/stat samples for a single core.
    #   sample 1: busy = (100+0+50) = 150, idle+iowait = 1000, total = 1150
    #   sample 2: busy = (150+0+75) = 225, idle+iowait = 1200, total = 1425
    #   delta busy = 75, delta total = 275  ->  27.3%
    samples = iter(
        [
            "cpu  100 0 50 1000 0 0 0 0 0 0\ncpu0 100 0 50 1000 0 0 0 0 0 0\n",
            "cpu  150 0 75 1200 0 0 0 0 0 0\ncpu0 150 0 75 1200 0 0 0 0 0 0\n",
        ]
    )
    monkeypatch.setattr(system_read, "_read", lambda *_a, **_k: next(samples))
    monkeypatch.setattr(system_read, "_cpu_frequencies", lambda: {})

    result = _call("system.cpu", {"sample_ms": 50})
    assert result.ok
    assert result.data["core_count"] == 1
    assert result.data["cores"][0]["busy_percent"] == pytest.approx(27.3, abs=0.1)
    assert result.data["overall_busy_percent"] == pytest.approx(27.3, abs=0.1)


def test_cpu_idle_core_reads_zero_not_one_hundred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A core whose only movement is idle time is 0% busy. This kills the mutant
    that counts idle as busy."""
    samples = iter(
        [
            "cpu0 10 0 5 1000 0 0 0 0 0 0\n",
            "cpu0 10 0 5 2000 0 0 0 0 0 0\n",  # only idle advanced
        ]
    )
    monkeypatch.setattr(system_read, "_read", lambda *_a, **_k: next(samples))
    monkeypatch.setattr(system_read, "_cpu_frequencies", lambda: {})
    result = _call("system.cpu", {"sample_ms": 50})
    assert result.data["cores"][0]["busy_percent"] == 0.0


def test_cpu_reports_failure_when_proc_stat_has_no_cores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_read, "_read", lambda *_a, **_k: "cpu 1 2 3 4\n")
    monkeypatch.setattr(system_read, "_cpu_frequencies", lambda: {})
    result = _call("system.cpu")
    assert result.ok is False
    assert "CPU" in (result.error or "")


# ------------------------------------------------------------- temperature


def test_temperature_prefers_labelled_sensors_over_bare_zones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When lm-sensors is present its named readings replace the anonymous
    thermal-zone numbers, and the input value is in °C already (not milli-°C)."""
    monkeypatch.setattr(
        system_read, "_thermal_from_sysfs",
        lambda: [{"sensor": "thermal_zone0", "celsius": 40.0}],
    )

    class _Proc:
        returncode = 0
        stdout = '{"coretemp-isa-0000": {"Core 0": {"temp2_input": 58.0}}}'

    monkeypatch.setattr(system_read, "_run", lambda *_a, **_k: _Proc())
    result = _call("system.temperature")
    assert result.ok
    assert result.data["source"] == "sensors"
    assert result.data["readings"] == [{"sensor": "coretemp-isa-0000/Core 0", "celsius": 58.0}]


def test_temperature_falls_back_to_sysfs_when_sensors_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        system_read, "_thermal_from_sysfs",
        lambda: [{"sensor": "x86_pkg_temp", "celsius": 51.0}],
    )
    monkeypatch.setattr(system_read, "_run", lambda *_a, **_k: None)  # no sensors binary
    result = _call("system.temperature")
    assert result.ok
    assert result.data["source"] == "/sys/class/thermal"
    assert result.data["readings"][0]["celsius"] == 51.0


def test_temperature_says_so_when_nothing_can_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_read, "_thermal_from_sysfs", lambda: [])
    monkeypatch.setattr(system_read, "_run", lambda *_a, **_k: None)
    result = _call("system.temperature")
    assert result.ok is False
    assert "no temperature sensors" in (result.error or "").lower()


# ----------------------------------------------------------------- mounts


_FAKE_MOUNTS = (
    "proc /proc proc rw,nosuid 0 0\n"
    "/dev/sda2 / ext4 rw,relatime 0 0\n"
    "tmpfs /run tmpfs rw,nosuid,nodev 0 0\n"
    "/dev/sda1 /boot/efi vfat rw,relatime 0 0\n"
    "cgroup2 /sys/fs/cgroup cgroup2 rw,nsdelegate 0 0\n"
    "/dev/sdb1 /mnt/My\\040Backup ext4 rw 0 0\n"
)


def test_mounts_hides_pseudo_filesystems_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_read, "_read", lambda *_a, **_k: _FAKE_MOUNTS)
    result = _call("system.mounts")
    assert result.ok
    types = {m["type"] for m in result.data["mounts"]}
    assert "proc" not in types and "tmpfs" not in types and "cgroup2" not in types
    assert {"ext4", "vfat"} <= types
    assert "pseudo" not in result.data  # not included unless asked
    assert result.data["mounts"][0]["options"] == ["rw", "relatime"]  # options are split


def test_mounts_decodes_octal_escaped_spaces_in_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/proc/mounts writes a space as \\040; a mount point must come back readable."""
    monkeypatch.setattr(system_read, "_read", lambda *_a, **_k: _FAKE_MOUNTS)
    result = _call("system.mounts")
    backups = [m for m in result.data["mounts"] if m["device"] == "/dev/sdb1"]
    assert backups and backups[0]["mount"] == "/mnt/My Backup"


def test_mounts_can_include_pseudo_on_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_read, "_read", lambda *_a, **_k: _FAKE_MOUNTS)
    result = _call("system.mounts", {"include_pseudo": True})
    assert "pseudo" in result.data
    pseudo_types = {m["type"] for m in result.data["pseudo"]}
    assert {"proc", "tmpfs", "cgroup2"} <= pseudo_types


# ---------------------------------------------------------------- datetime


def test_datetime_ntp_is_tri_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """yes -> True, no -> False, no timedatectl -> None. Collapsing the third case
    to False would claim the clock is unsynced when we simply do not know."""

    def make(stdout: str | None):
        if stdout is None:
            return lambda *_a, **_k: None

        class _P:
            returncode = 0

        _P.stdout = stdout
        return lambda *_a, **_k: _P()

    monkeypatch.setattr(system_read, "_run", make("yes\n"))
    assert _call("system.datetime").data["ntp_synchronized"] is True

    monkeypatch.setattr(system_read, "_run", make("no\n"))
    assert _call("system.datetime").data["ntp_synchronized"] is False

    monkeypatch.setattr(system_read, "_run", make(None))
    assert _call("system.datetime").data["ntp_synchronized"] is None


# ------------------------------------------------------------------- users


def test_users_parses_who_output(monkeypatch: pytest.MonkeyPatch) -> None:
    class _P:
        returncode = 0
        stdout = (
            "santosh  :0           2026-09-10 08:15 (:0)\n"
            "santosh  pts/1        2026-09-10 09:02 (192.168.1.5)\n"
        )

    monkeypatch.setattr(system_read, "_run", lambda *_a, **_k: _P())
    result = _call("system.users")
    assert result.ok
    assert len(result.data["sessions"]) == 2
    assert result.data["sessions"][1]["from"] == "192.168.1.5"


def test_users_falls_back_to_current_account_when_utmp_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _P:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(system_read, "_run", lambda *_a, **_k: _P())
    monkeypatch.setattr(system_read.os, "getlogin", lambda: "santosh")
    result = _call("system.users")
    assert result.ok
    assert result.data["sessions"] == []
    assert result.data["current_user"] == "santosh"


# --------------------------------------------------------------------- env


def test_env_redacts_a_secret_shaped_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reading a token by name must not hand it back in the clear."""
    monkeypatch.setenv("NEXUS_TEST_APIKEY", "sk-abcdef0123456789abcdef0123456789")
    result = _call("system.env", {"name": "NEXUS_TEST_APIKEY"})
    assert result.ok
    assert result.data["variables"]["NEXUS_TEST_APIKEY"] != "sk-abcdef0123456789abcdef0123456789"


def test_env_returns_plain_values_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_TEST_PLAIN", "/usr/local/bin")
    result = _call("system.env", {"name": "NEXUS_TEST_PLAIN"})
    assert result.data["variables"]["NEXUS_TEST_PLAIN"] == "/usr/local/bin"


def test_env_missing_variable_is_an_honest_failure() -> None:
    result = _call("system.env", {"name": "NEXUS_DEFINITELY_NOT_SET_XYZ"})
    assert result.ok is False
    assert "NEXUS_DEFINITELY_NOT_SET_XYZ" in (result.error or "")


# ---------------------------------------------------------- registration


def test_all_seven_tools_are_registered_read_only_and_low_risk() -> None:
    names = [
        "system.cpu", "system.temperature", "system.mounts", "system.datetime",
        "system.locale", "system.users", "system.env",
    ]
    for name in names:
        tool = registry.get(name)
        assert tool is not None, f"{name} missing"
        assert tool.read_only is True, f"{name} should be read-only"
        assert tool.capability == "system.info", f"{name} should use system.info"
        # No verifier: nothing changed, so there is nothing to confirm (§60).
        assert tool.verifier is None
