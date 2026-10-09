"""Continuous awareness (tasks L1-L3; spec §15, §35, §36, §44, §66).

Importing this package is the single point where every monitor becomes known.
``supervisor()`` returns the process-wide supervisor with all of them registered,
so no caller has to remember the list -- a monitor that exists but was never
registered would be a watcher that silently never runs, which is the same failure
as a watcher that runs and sees nothing (§78).

Two rules hold across everything here:

**Off until switched on.** ``monitor_enabled`` is the master switch and defaults
to False, and each monitor has its own default-False setting underneath it. NEXUS
never arrives watching, and nothing in this package can turn itself on.

**Observation is not authorization.** A monitor reports facts. It never acts on
them, and it never routes them to a model on its own. What happens next is the
proactive engine's decision (L10) and ultimately the user's (§31).
"""

from __future__ import annotations

from typing import Any

from .base import (  # noqa: F401
    Debouncer,
    Monitor,
    MonitorState,
    MonitorUnavailable,
    Observation,
    Severity,
    setting,
)
from .bluetooth_monitor import BluetoothDevice, BluetoothMonitor, RadioState  # noqa: F401
from .clipboard_monitor import ClipboardEntry, ClipboardMonitor, looks_like_credential  # noqa: F401
from .notification_monitor import NotificationMonitor, parse_notify  # noqa: F401
from .screen_monitor import ScreenMonitor  # noqa: F401
from .supervisor import MAX_CONSECUTIVE_ERRORS, Supervisor, supervisor  # noqa: F401
from .system_monitor import SystemMonitor  # noqa: F401
from .usb_monitor import UsbDevice, UsbMonitor  # noqa: F401
from .window_monitor import FocusSession, WindowMonitor  # noqa: F401

__all__ = [
    "MAX_CONSECUTIVE_ERRORS",
    "BluetoothDevice",
    "BluetoothMonitor",
    "ClipboardEntry",
    "ClipboardMonitor",
    "Debouncer",
    "FocusSession",
    "Monitor",
    "MonitorState",
    "MonitorUnavailable",
    "Observation",
    "RadioState",
    "ScreenMonitor",
    "Severity",
    "Supervisor",
    "SystemMonitor",
    "UsbDevice",
    "UsbMonitor",
    "WindowMonitor",
    "looks_like_credential",
    "NotificationMonitor",
    "parse_notify",
    "setting",
    "status",
    "supervisor",
]


def status() -> dict[str, Any]:
    """What the Awareness settings screen shows, without starting anything."""
    return supervisor().status()
