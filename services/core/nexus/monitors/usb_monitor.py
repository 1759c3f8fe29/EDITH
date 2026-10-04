"""What got plugged in (spec doc2 §7, §20; MASTER §35).

Reads ``/sys/bus/usb/devices`` directly rather than shelling out to ``lsusb``.
Two reasons, both practical: ``usbutils`` is not installed everywhere, and a
monitor that depends on a binary it does not need has an availability failure it
did not have to have. The kernel already publishes everything wanted here as
files.

**A first sample reports nothing.** The devices already attached when NEXUS
starts were not just plugged in, and announcing them would be a lie told at every
startup -- the kind that trains a user to ignore the channel. So the first look
establishes the baseline and returns no observations, and only differences from
it are events.

**Hubs are not events.** ``bDeviceClass 09`` is a hub, and a machine has several
internal ones -- this laptop shows three that nobody ever touched. Worse, a real
insertion into a hub port can make the *hub* appear in a scan if it was powered
down, so reporting hubs turns one plug into two events. They are filtered out and
counted, not reported.

**Identity is vendor, product, serial and port.** A device is recognised by
``idVendor:idProduct`` plus its serial where it has one; the sysfs path is the
port, and it is carried separately because "the same stick in the other socket"
is a different physical act than "a different stick". Without the serial, two
identical mice would be one device and unplugging either would look like both
leaving.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..logging_setup import get_logger
from .base import Monitor, MonitorUnavailable, Observation

log = get_logger("monitors.usb")

#: Where the kernel publishes the USB tree. A parameter so a test can point at a
#: directory it built, which is the only way to exercise insertion and removal
#: without asking a human to handle hardware.
SYSFS_USB = Path("/sys/bus/usb/devices")

#: USB device/interface class codes, as the ones worth naming. Straight from the
#: USB-IF class list; the rest fall through to "device", which is honest -- a
#: made-up guess at what an unknown class does would be worse than not saying.
CLASS_NAMES: dict[int, str] = {
    0x01: "audio device",
    0x02: "communications device",
    0x03: "input device",
    0x05: "physical interface device",
    0x06: "imaging device",
    0x07: "printer",
    0x08: "storage device",
    0x09: "hub",
    0x0A: "audio/video device",
    0x0B: "smart card reader",
    0x0D: "content security device",
    0x0E: "camera",
    0x0F: "personal healthcare device",
    0xDC: "diagnostic device",
    0xE0: "wireless adapter",
    0xEF: "device",
    0xFE: "device",
    0xFF: "device",
}

#: Hub. Internal plumbing, never a user-visible event.
HUB_CLASS = 0x09

#: Classes that get their own observation kind, because the scoring layer treats
#: them differently: a storage device that appeared has something to *do* about
#: it (look at what is on it), where a mouse does not.
KIND_FOR_CLASS: dict[int, str] = {
    0x08: "usb.storage",
    0x0B: "usb.storage",
    0x03: "usb.input",
    0x01: "usb.audio",
    0x0E: "usb.camera",
    0x0F: "usb.camera",
    0xE0: "usb.network",
}


def _read(path: Path) -> str:
    """One sysfs attribute, or "" -- a device unplugged mid-scan is normal.

    Enumerating a tree that is changing under you is the expected case for this
    monitor, so a vanished file is not an error worth propagating. It is why every
    read here degrades instead of raising.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _read_hex(path: Path) -> int | None:
    raw = _read(path)
    if not raw:
        return None
    try:
        return int(raw, 16)
    except ValueError:
        return None


@dataclass(frozen=True)
class UsbDevice:
    """One device on the bus, as the kernel describes it."""

    path: str                    # "1-1.2" -- the port, not the device
    vendor_id: str = ""
    product_id: str = ""
    vendor: str = ""             # "YSPRINGTECH"
    product: str = ""            # "USB OPTICAL MOUSE"
    serial: str = ""
    device_class: int = 0
    interface_classes: tuple[int, ...] = ()
    removable: str = ""          # "removable", "fixed", "unknown"

    @property
    def identity(self) -> str:
        """Stable across ports, distinct between two identical models.

        The serial is what separates two of the same mouse. Devices without one
        fall back to including the port, because the alternative -- treating them
        as one device -- means unplugging either looks like both leaving.
        """
        base = f"{self.vendor_id}:{self.product_id}"
        if self.serial:
            return f"{base}:{self.serial}"
        return f"{base}@{self.path}"

    @property
    def effective_class(self) -> int:
        """The class that describes what this device *is*.

        ``bDeviceClass`` is 0 on most real peripherals -- it means "ask the
        interfaces". This laptop's mouse reports device class 00 and interface
        class 03, so a monitor that trusted the device class alone would call
        every mouse and keyboard and webcam an unknown device.
        """
        if self.device_class not in (0x00, 0xFF) or not self.interface_classes:
            return self.device_class
        for candidate in self.interface_classes:
            if candidate not in (0x00, 0xFF):
                return candidate
        return self.device_class

    @property
    def is_hub(self) -> bool:
        return self.device_class == HUB_CLASS or self.effective_class == HUB_CLASS

    @property
    def kind_suffix(self) -> str:
        return KIND_FOR_CLASS.get(self.effective_class, "usb.device")

    def describe(self) -> str:
        """A name a person would recognise, falling back down to the ids.

        Ends at ``vendor_id:product_id`` rather than at "unknown device", because
        the numbers are something the user can actually look up.
        """
        name = " ".join(part for part in (self.vendor, self.product) if part).strip()
        if name:
            return name
        label = CLASS_NAMES.get(self.effective_class, "device")
        if self.vendor_id and self.product_id:
            return f"{label} {self.vendor_id}:{self.product_id}"
        return label

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "port": self.path,
            "vendor_id": self.vendor_id,
            "product_id": self.product_id,
            "vendor": self.vendor,
            "product": self.product,
            "serial_present": bool(self.serial),
            "class": f"{self.effective_class:02x}",
            "class_name": CLASS_NAMES.get(self.effective_class, "device"),
            "removable": self.removable,
        }


def _interface_classes(directory: Path) -> tuple[int, ...]:
    """Interface classes under one device, in a stable order.

    Interfaces live in sibling directories named ``<device>:<config>.<n>``, which
    is also why the scan below rejects any name containing a colon: those entries
    are interfaces, and counting them as devices would multiply every peripheral
    by its interface count.
    """
    found: list[int] = []
    for interface in sorted(directory.glob(f"{directory.name}:*")):
        value = _read_hex(interface / "bInterfaceClass")
        if value is not None:
            found.append(value)
    return tuple(found)


def read_device(directory: Path) -> UsbDevice | None:
    """Describe one device directory, or None if it is not a device.

    A directory with no ``idVendor`` is either an interface or a device that left
    between the listing and the read; neither is something to report.
    """
    vendor_id = _read(directory / "idVendor")
    product_id = _read(directory / "idProduct")
    if not vendor_id or not product_id:
        return None
    device_class = _read_hex(directory / "bDeviceClass")
    return UsbDevice(
        path=directory.name,
        vendor_id=vendor_id,
        product_id=product_id,
        vendor=_read(directory / "manufacturer"),
        product=_read(directory / "product"),
        serial=_read(directory / "serial"),
        device_class=device_class if device_class is not None else 0,
        interface_classes=_interface_classes(directory),
        removable=_read(directory / "removable"),
    )


def scan(root: Path | None = None) -> dict[str, UsbDevice]:
    """Every non-hub device on the bus, keyed by identity."""
    base = root if root is not None else SYSFS_USB
    devices: dict[str, UsbDevice] = {}
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return devices
    for entry in entries:
        # ":" marks an interface; "usbN" is a root hub. Both are plumbing.
        if ":" in entry.name or not entry.is_dir():
            continue
        device = read_device(entry)
        if device is None or device.is_hub:
            continue
        devices[device.identity] = device
    return devices


class UsbMonitor(Monitor):
    id = "usb"
    title = "USB devices"
    description = (
        "Notices devices being plugged in and unplugged, reading the kernel's USB "
        "tree directly. Reports what appeared, not what was already there."
    )
    enabled_setting = "monitor_usb_enabled"
    interval_setting = "monitor_usb_interval_seconds"
    default_interval = 5.0
    # ``system.info`` -- hardware status, the same capability the system monitor
    # reads battery and disk under. An earlier draft named "system.read", which is
    # not a capability NEXUS has: ``engine.evaluate`` denies an unknown name, so
    # ``check_permission`` raised and the monitor reported "unavailable" the moment
    # anyone switched it on. A toggle that can only fail is worse than no toggle.
    capability = "system.info"
    activity_kind = "device"
    record_to_activity = True

    def __init__(self, root: Path | None = None) -> None:
        super().__init__()
        self._root = root
        #: None until the first sample. The distinction between "no devices" and
        #: "have not looked yet" is what makes the first-sample rule work: an
        #: empty dict would make every device on the machine an insertion.
        self._known: dict[str, UsbDevice] | None = None

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else SYSFS_USB

    def check_available(self) -> None:
        if not self.root.is_dir():
            raise MonitorUnavailable(
                f"{self.root} does not exist, so this kernel is not publishing a USB "
                "tree to read."
            )

    async def on_stop(self) -> None:
        # Dropping the baseline is deliberate. Devices can come and go while the
        # monitor is off, and replaying that gap on restart would report an
        # insertion for something plugged in an hour ago -- with a timestamp
        # saying now. Starting from a fresh baseline reports nothing it cannot
        # stand behind.
        self._known = None

    async def sample(self) -> list[Observation]:
        current = scan(self.root)
        previous = self._known
        self._known = current

        if previous is None:
            log.debug("usb baseline: %d device(s)", len(current))
            return []

        observations: list[Observation] = []
        for identity in sorted(set(current) - set(previous)):
            observations.append(self._event(current[identity], attached=True))
        for identity in sorted(set(previous) - set(current)):
            observations.append(self._event(previous[identity], attached=False))
        return observations

    def _event(self, device: UsbDevice, *, attached: bool) -> Observation:
        verb = "connected" if attached else "disconnected"
        # Storage is the one class worth a notice on sight: something appeared
        # with files on it, and the user may be about to ask about them.
        storage = device.kind_suffix == "usb.storage"
        return Observation(
            kind=f"{device.kind_suffix}.{'added' if attached else 'removed'}",
            summary=f"{device.describe()} {verb}",
            data={"attached": attached, **device.to_dict()},
            severity="notice" if storage else "info",
            # Keyed on the device, not the event, so an unplug clears the plug.
            dedupe_key=f"usb:{device.identity}",
            # A removal ends the condition the insertion started. Saying so is
            # what lets the debouncer forget it instead of suppressing the next
            # insertion of the same device for fifteen minutes.
            clears=not attached,
        )
