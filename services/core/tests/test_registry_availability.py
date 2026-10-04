"""The availability probe (C5): a tool can be unavailable for a reason a binary
list cannot express, and the registry must say so instead of offering it.

Two layers are tested. The registry layer is the contract: a probe that returns
a reason makes the tool unavailable, a probe that returns None does not, a
missing *binary* is reported ahead of the probe (the more basic fix), and a
probe that *raises* reads as unavailable, never as a working tool -- the §78
rule that an uncertain check must not look like a passing one.

The ``system.brightness`` layer is the real case the probe exists for. It is
driven against the actual filesystem -- a real directory whose ``brightness``
file is chmod-ed writable or not -- rather than against a mock that returns
whatever the test wants, so the assertion turns on the same ``os.access`` call
the shipped probe makes. The one thing mocked is ``brightnessctl`` presence,
because installing a binary is not something a test can arrange.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from nexus.tools import registry
from nexus.tools import system as system_mod
from nexus.tools.registry import Tool


def _noop(_: dict[str, Any]) -> registry.ToolResult:
    return registry.ToolResult(ok=True)


def _tool(**kw: Any) -> Tool:
    """A Tool built directly, so availability is tested without registering it.

    ``system.info`` is a real, low-risk capability; using it keeps ``risk`` and
    ``to_dict`` honest should a test ever touch them.
    """
    base = dict(
        name="probe.subject",
        title="Probe subject",
        description="A tool used only to exercise availability.",
        capability="system.info",
        parameters={"type": "object", "properties": {}},
        handler=_noop,
    )
    base.update(kw)
    return Tool(**base)  # type: ignore[arg-type]


# ------------------------------------------------------------- registry layer


def test_a_probe_returning_a_reason_makes_the_tool_unavailable() -> None:
    tool = _tool(available_probe=lambda: "the widget is unplugged")
    assert tool.available is False
    assert tool.unavailable_reason() == "the widget is unplugged"


def test_a_probe_returning_none_leaves_the_tool_available() -> None:
    tool = _tool(available_probe=lambda: None)
    assert tool.available is True
    assert tool.unavailable_reason() is None


def test_no_probe_is_as_if_always_available() -> None:
    # The default: a tool that declares nothing is available (subject to binaries).
    assert _tool().available is True


def test_a_missing_binary_is_reported_ahead_of_the_probe() -> None:
    """A missing program is the more basic problem; the probe's reason must not
    hide it, or the user is told to fix the wrong thing."""
    tool = _tool(
        requires=("this-binary-does-not-exist-anywhere",),
        available_probe=lambda: "and also the widget is unplugged",
    )
    reason = tool.unavailable_reason()
    assert reason is not None
    assert "this-binary-does-not-exist-anywhere" in reason
    assert "widget" not in reason  # the binary wins; the probe is not consulted


def test_a_probe_that_raises_reads_as_unavailable_not_available() -> None:
    """An exploding check must never leave the tool looking runnable (§78)."""
    def boom() -> str | None:
        raise RuntimeError("sensor on fire")

    tool = _tool(available_probe=boom)
    assert tool.available is False
    reason = tool.unavailable_reason()
    assert reason is not None
    assert "sensor on fire" in reason


# --------------------------------------------------------- brightness layer


def _fake_backlight(dirpath: Path, *, writable: bool) -> Path:
    """A directory shaped like /sys/class/backlight/<device>, whose brightness
    file is really writable or really not -- the probe's os.access sees the
    truth, not a flag."""
    dirpath.mkdir(parents=True, exist_ok=True)
    bfile = dirpath / "brightness"
    bfile.write_text("400")
    (dirpath / "max_brightness").write_text("976")
    os.chmod(bfile, 0o644 if writable else 0o444)
    return dirpath


def test_brightness_is_available_when_brightnessctl_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With brightnessctl present, the device's own writability does not matter:
    brightnessctl is the thing that writes it without root."""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/brightnessctl")
    assert system_mod._brightness_change_unavailable() is None


def test_brightness_is_unavailable_when_the_backlight_is_read_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The machine this was written on: a real backlight that will not be
    written and no brightnessctl. get() could read it; set() never works."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    device = _fake_backlight(tmp_path / "intel_backlight", writable=False)
    monkeypatch.setattr(system_mod, "_backlight_devices", lambda: [device])
    reason = system_mod._brightness_change_unavailable()
    assert reason is not None
    assert "brightnessctl" in reason  # names the fix


def test_brightness_is_available_when_the_backlight_is_writable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A udev rule (or root) makes the sysfs file writable; then the tool works
    with no brightnessctl at all."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    device = _fake_backlight(tmp_path / "intel_backlight", writable=True)
    monkeypatch.setattr(system_mod, "_backlight_devices", lambda: [device])
    assert system_mod._brightness_change_unavailable() is None


def test_brightness_on_a_desktop_with_no_backlight_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(system_mod, "_backlight_devices", lambda: [])
    reason = system_mod._brightness_change_unavailable()
    assert reason is not None
    assert "desktop" in reason.lower()


def test_the_registered_brightness_tool_uses_the_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: the shipped tool's availability follows the probe, so
    runtime.execute (which blocks on unavailable_reason) refuses it honestly."""
    tool = registry.get("system.brightness")
    assert tool is not None
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(system_mod, "_backlight_devices", lambda: [])
    assert tool.available is False
    assert tool.unavailable_reason() is not None
