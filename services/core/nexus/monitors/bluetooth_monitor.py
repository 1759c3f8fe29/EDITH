"""What paired, connected and went away (spec doc2 §7, §20; MASTER §35).

Reads BlueZ over the system bus with ``busctl``, and the kernel's radio switches
from ``/sys/class/rfkill``. Two sources because they answer different questions
and only one of them is always available.

**BlueZ says what is connected. rfkill says why it isn't.** On this laptop the
adapter is soft-blocked, so ``bluetoothctl show`` prints "No default controller
available" and ``GetManagedObjects`` returns a single ``/org/bluez`` node with no
adapter under it. A monitor that only spoke to BlueZ would report that as "no
Bluetooth hardware", which is wrong and unactionable. ``/sys/class/rfkill``
carries ``ideapad_bluetooth`` with ``soft=1``, which is the actual answer: the
radio is off, and the user can turn it on. So the unavailable reason distinguishes
"no adapter on this machine" from "the radio is switched off".

**A disconnect is only an event if something connected first.** Like the USB
monitor, the first sample is a baseline and reports nothing: headphones already
connected when NEXUS starts were not just connected. Everything after is a
difference.

**Audio is not the same as a mouse.** A device the user is listening or talking
through dropping mid-call is a different event from a keyboard pairing, and the
scoring layer has a separate rule for it (``bluetooth.audio``). The class comes
from BlueZ's ``Icon`` and ``Class`` properties, which is the only thing available
without a connection to the device itself.

**Nothing here pairs, connects or trusts anything.** This module has no write
path. Pairing is a decision with a security consequence -- a paired device can
act as a keyboard -- and it belongs to the user in the system's own dialog, not to
a background watcher.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..logging_setup import get_logger
from .base import Monitor, MonitorUnavailable, Observation

log = get_logger("monitors.bluetooth")

#: Where the kernel publishes radio kill switches. A parameter so a test can point
#: at a directory it built -- the alternative is asking a human to flip an rfkill
#: switch mid-test, which no test can do.
SYSFS_RFKILL = Path("/sys/class/rfkill")

#: The bus name and object manager path BlueZ publishes. Constants rather than
#: literals because the tests stub the transport, not the names.
BLUEZ_SERVICE = "org.bluez"
BLUEZ_ROOT = "/"

#: BlueZ interface names. ``Adapter1`` is the controller, ``Device1`` is a remote
#: device it knows about -- paired, connected, or merely seen.
ADAPTER_INTERFACE = "org.bluez.Adapter1"
DEVICE_INTERFACE = "org.bluez.Device1"

#: ``Icon`` values BlueZ uses, mapped to what the device is for. Taken from the
#: freedesktop icon-naming spec that BlueZ follows; anything unlisted stays
#: "device", which is honest -- guessing at an unknown icon name would be worse.
ICON_ROLES: dict[str, str] = {
    "audio-card": "audio",
    "audio-headphones": "audio",
    "audio-headset": "audio",
    "audio-speakers": "audio",
    "camera-photo": "camera",
    "camera-video": "camera",
    "computer": "computer",
    "input-gaming": "input",
    "input-keyboard": "input",
    "input-mouse": "input",
    "input-tablet": "input",
    "modem": "network",
    "network-wireless": "network",
    "phone": "phone",
    "printer": "printer",
    "scanner": "scanner",
    "multimedia-player": "audio",
    "video-display": "display",
}

#: The Bluetooth "major device class" nibble, used when ``Icon`` is absent. The
#: class of device is a 24-bit field; bits 8-12 are the major class. Only the
#: majors that change what NEXUS says are named.
MAJOR_CLASS_ROLES: dict[int, str] = {
    0x01: "computer",
    0x02: "phone",
    0x03: "network",
    0x04: "audio",
    0x05: "input",
    0x06: "imaging",
    0x07: "wearable",
    0x08: "toy",
    0x09: "health",
}

#: Roles that get their own observation kind. Audio is separated because losing a
#: headset mid-call is the one Bluetooth event with any urgency; input is
#: separated because a keyboard connecting is a security-relevant fact (§20).
KIND_FOR_ROLE: dict[str, str] = {
    "audio": "bluetooth.audio",
    "input": "bluetooth.input",
}


def _run(command: list[str], timeout: float = 5.0) -> str | None:
    """One command's stdout, or None if it failed in any way.

    ``busctl`` returns non-zero when the service is not on the bus, which is the
    normal state on a machine whose Bluetooth daemon is not running. That is a
    fact to report, not an exception to propagate.
    """
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


@dataclass(frozen=True)
class RadioState:
    """One rfkill switch, as the kernel reports it."""

    name: str            # "ideapad_bluetooth"
    soft_blocked: bool   # the user or software turned it off
    hard_blocked: bool   # a physical switch or firmware turned it off

    @property
    def blocked(self) -> bool:
        return self.soft_blocked or self.hard_blocked

    def describe(self) -> str:
        """Why the radio is off, in the words that tell the user what to do.

        The distinction is not pedantry: a soft block is a setting the user can
        change, and a hard block is a physical switch or a firmware setting that
        software cannot touch. Telling someone to "turn Bluetooth on" when a
        hardware switch is off would send them to a toggle that will not move.
        """
        if self.hard_blocked:
            return "the Bluetooth radio is blocked by a hardware switch"
        if self.soft_blocked:
            return "the Bluetooth radio is switched off"
        return "the Bluetooth radio is on"


def read_radios(root: Path | None = None) -> list[RadioState]:
    """Every Bluetooth rfkill switch on this machine, or [] if there are none."""
    base = root if root is not None else SYSFS_RFKILL
    radios: list[RadioState] = []
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return radios
    for entry in entries:
        if _attribute(entry / "type") != "bluetooth":
            continue
        radios.append(
            RadioState(
                name=_attribute(entry / "name") or entry.name,
                soft_blocked=_attribute(entry / "soft") == "1",
                hard_blocked=_attribute(entry / "hard") == "1",
            )
        )
    return radios


def _attribute(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def radios_blocked(radios: Iterable[RadioState]) -> bool:
    """Is Bluetooth off on this machine?

    ``all``, not ``any``, and the difference is a claim about the whole machine. A
    laptop whose built-in radio is soft-blocked while a USB dongle works is *not*
    off -- announcing "the Bluetooth radio is switched off" while a headset is
    connected through the dongle would be a statement the machine's own behaviour
    contradicts.

    ``bool(radios)`` first because ``all(())`` is True: a machine with no rfkill
    switches at all would otherwise report Bluetooth as blocked, which is the
    opposite of what an absent switch means.
    """
    states = list(radios)
    return bool(states) and all(radio.blocked for radio in states)


@dataclass(frozen=True)
class BluetoothDevice:
    """One remote device BlueZ knows about."""

    path: str                       # "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
    address: str = ""               # "AA:BB:CC:DD:EE:FF"
    name: str = ""                  # "WH-1000XM4"
    icon: str = ""                  # "audio-headphones"
    device_class: int = 0           # the 24-bit class of device
    paired: bool = False
    trusted: bool = False
    connected: bool = False
    battery_percent: int | None = None

    @property
    def identity(self) -> str:
        """The address, which is the one identifier a Bluetooth device keeps.

        Not the object path: BlueZ derives it from the address *and* the adapter,
        so the same headset on a second controller is a different path. Falling
        back to the path when there is no address means a device without one is
        still tracked rather than colliding with every other such device.
        """
        return self.address or self.path

    @property
    def role(self) -> str:
        """What the device is for: "audio", "input", or "device".

        ``Icon`` first because BlueZ has already done this interpretation and does
        it better -- it reads the class of device *and* the service UUIDs. The
        class nibble is the fallback for devices BlueZ gave no icon.
        """
        role = ICON_ROLES.get(self.icon)
        if role:
            return role
        major = (self.device_class >> 8) & 0x1F
        return MAJOR_CLASS_ROLES.get(major, "device")

    @property
    def kind_suffix(self) -> str:
        return KIND_FOR_ROLE.get(self.role, "bluetooth.device")

    def describe(self) -> str:
        """A name a person would recognise, falling back to the address.

        The address is ugly but it is a fact the user can match against what their
        phone shows. "Unknown device" is not.
        """
        if self.name:
            return self.name
        if self.address:
            return f"{self.role} {self.address}"
        return self.role

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "address": self.address,
            "name": self.name,
            "role": self.role,
            "icon": self.icon,
            "class": f"{self.device_class:06x}",
            "paired": self.paired,
            "trusted": self.trusted,
            "connected": self.connected,
            "battery_percent": self.battery_percent,
            "object_path": self.path,
        }


@dataclass
class Snapshot:
    """Everything one look at the bus found."""

    adapters: list[str] = field(default_factory=list)
    #: Only the devices that are *connected*. Paired-but-absent devices are the
    #: normal state of a phone that is in another room, and reporting them would
    #: make every startup announce a list of things that are not here.
    connected: dict[str, BluetoothDevice] = field(default_factory=dict)
    known: dict[str, BluetoothDevice] = field(default_factory=dict)
    powered: bool = False
    #: Whether the bus actually answered. False means "we do not know", which is a
    #: third state and not the same as "nothing is connected": ``busctl`` returns
    #: non-zero while BlueZ is restarting, and treating that as an empty bus would
    #: announce a disconnect for every device and a reconnect for each of them a
    #: few seconds later. Every read of ``connected`` for diffing has to check this
    #: first.
    ok: bool = True

    @property
    def has_adapter(self) -> bool:
        return bool(self.adapters)


def _variant(value: Any) -> Any:
    """Unwrap one ``busctl --json=short`` variant.

    The encoding is ``{"type": "s", "data": "hci0"}`` for a property and a bare
    value inside containers. Written as one function because the alternative is
    every caller remembering which of the two it is looking at, and a missed
    unwrap yields a dict where a string was expected -- which reads as "the
    property is missing" rather than as a bug.
    """
    if isinstance(value, dict) and "data" in value and "type" in value:
        return value["data"]
    return value


def parse_managed_objects(payload: str) -> Snapshot:
    """Turn one ``GetManagedObjects`` reply into a snapshot.

    Separate from the call so a test can hand it the exact bytes ``busctl``
    produced on a machine with a headset connected -- which is the only way to
    exercise the connected path on a laptop whose radio is off.

    Malformed output is ``ok=False`` rather than an empty bus, for the same reason
    a failed call is: the two are indistinguishable from here, and only one of them
    can be safely diffed against.
    """
    snapshot = Snapshot()
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        snapshot.ok = False
        return snapshot

    objects = parsed.get("data") if isinstance(parsed, dict) else None
    if isinstance(objects, list) and objects:
        objects = objects[0]
    if not isinstance(objects, dict):
        snapshot.ok = False
        return snapshot

    for path, interfaces in sorted(objects.items()):
        if not isinstance(interfaces, dict):
            continue
        if ADAPTER_INTERFACE in interfaces:
            snapshot.adapters.append(path)
            properties = interfaces[ADAPTER_INTERFACE]
            if isinstance(properties, dict) and bool(_variant(properties.get("Powered"))):
                snapshot.powered = True
        if DEVICE_INTERFACE not in interfaces:
            continue
        properties = interfaces[DEVICE_INTERFACE]
        if not isinstance(properties, dict):
            continue
        device = _device_from(path, properties, interfaces)
        snapshot.known[device.identity] = device
        if device.connected:
            snapshot.connected[device.identity] = device
    return snapshot


def _device_from(path: str, properties: dict[str, Any], interfaces: dict[str, Any]) -> BluetoothDevice:
    raw_class = _variant(properties.get("Class"))
    try:
        device_class = int(raw_class) if raw_class is not None else 0
    except (TypeError, ValueError):
        device_class = 0
    address = str(_variant(properties.get("Address")) or "")
    return BluetoothDevice(
        path=path,
        address=address,
        name=_name(properties, address),
        icon=str(_variant(properties.get("Icon")) or ""),
        device_class=device_class,
        paired=bool(_variant(properties.get("Paired"))),
        trusted=bool(_variant(properties.get("Trusted"))),
        connected=bool(_variant(properties.get("Connected"))),
        battery_percent=_battery_percent(interfaces),
    )


def _name(properties: dict[str, Any], address: str) -> str:
    """The device's name, or "" when it has none.

    ``Alias`` is preferred over ``Name`` because it is the name the *user* gave the
    device, and BlueZ falls back to ``Name`` itself when no alias is set. But it
    falls back further: a device with neither gets its address with the colons
    turned into dashes. Accepting that would set ``name`` to a string that is not a
    name, and then ``describe()`` prints the address where the caller expected the
    thing the user calls it -- and ``to_dict()["name"]`` claims a name exists.

    So an alias that is only the address, in either punctuation, is discarded.
    """
    alias = str(_variant(properties.get("Alias")) or "")
    if alias and _is_address(alias, address):
        alias = ""
    return alias or str(_variant(properties.get("Name")) or "")


def _is_address(value: str, address: str) -> bool:
    """Is ``value`` just this device's address written out?

    Compared with the separators removed because BlueZ writes the address with
    dashes in an alias and with colons in ``Address``, and a comparison that missed
    that would let exactly the case this guards against through.
    """
    if not address:
        return False
    flatten = str.maketrans("", "", ":-_")
    return value.translate(flatten).lower() == address.translate(flatten).lower()


def _battery_percent(interfaces: dict[str, Any]) -> int | None:
    """The device's own battery level, where BlueZ publishes one.

    ``org.bluez.Battery1`` appears only for devices that report it over the
    battery service, which in practice means headsets and some mice. None means
    "not reported", never 0 -- a headset at 0% would be off.
    """
    battery = interfaces.get("org.bluez.Battery1")
    if not isinstance(battery, dict):
        return None
    raw = _variant(battery.get("Percentage"))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def query(runner: Any = None) -> str | None:
    """Ask BlueZ for everything it knows. Returns raw JSON, or None.

    ``busctl`` rather than a D-Bus library: nothing in ``requirements.txt`` speaks
    D-Bus, systemd ships ``busctl`` on every machine that has BlueZ running under
    it, and adding a dependency to read four properties would be a worse trade
    than one subprocess every few seconds.
    """
    run = runner if runner is not None else _run
    return run(
        [
            "busctl", "--system", "--json=short", "call",
            BLUEZ_SERVICE, BLUEZ_ROOT,
            "org.freedesktop.DBus.ObjectManager", "GetManagedObjects",
        ]
    )


def scan(runner: Any = None) -> Snapshot:
    payload = query(runner)
    if payload is None:
        # ``ok=False``: the daemon did not answer, which is not the same fact as
        # "nothing is connected". See Snapshot.ok.
        return Snapshot(ok=False)
    return parse_managed_objects(payload)


class BluetoothMonitor(Monitor):
    id = "bluetooth"
    title = "Bluetooth devices"
    description = (
        "Notices Bluetooth devices connecting and disconnecting, and says when the "
        "radio is switched off. Never pairs or connects anything itself."
    )
    enabled_setting = "monitor_bluetooth_enabled"
    interval_setting = "monitor_bluetooth_interval_seconds"
    default_interval = 10.0
    capability = "system.info"
    activity_kind = "device"
    record_to_activity = True

    def __init__(self, runner: Any = None, rfkill: Path | None = None) -> None:
        super().__init__()
        #: Injected so tests drive the parser with recorded ``busctl`` output
        #: rather than needing a paired headset on the machine running them.
        self._runner = runner
        self._rfkill = rfkill
        #: None until the first sample, which is what makes "connected at startup"
        #: different from "just connected". An empty dict would announce every
        #: already-connected device as an arrival.
        self._known: dict[str, BluetoothDevice] | None = None
        #: The last radio state reported, so the switch being flipped is one event
        #: rather than one per tick.
        self._radio_blocked: bool | None = None

    @property
    def rfkill_root(self) -> Path:
        return self._rfkill if self._rfkill is not None else SYSFS_RFKILL

    def check_available(self) -> None:
        """Refuse only when there is genuinely nothing to watch.

        A blocked radio is *not* unavailable: the switch being off is a fact this
        monitor exists to report, and stopping here would mean the one moment the
        user turns Bluetooth back on is the moment nothing is watching.
        """
        if not shutil.which("busctl"):
            raise MonitorUnavailable(
                "busctl is not installed, so NEXUS cannot read the Bluetooth "
                "daemon. Installing systemd's tools would fix it."
            )
        radios = read_radios(self.rfkill_root)
        if radios:
            return
        if scan(self._runner).has_adapter:
            return
        raise MonitorUnavailable(
            "This machine has no Bluetooth adapter that the kernel or BlueZ can "
            "see, so there is nothing to watch."
        )

    async def on_stop(self) -> None:
        # Same reasoning as the USB monitor: a headset connected while the monitor
        # was off did not connect just now, and replaying it with a current
        # timestamp would be a claim this monitor cannot stand behind.
        self._known = None
        self._radio_blocked = None

    async def sample(self) -> list[Observation]:
        observations: list[Observation] = []
        radios = read_radios(self.rfkill_root)
        blocked = radios_blocked(radios)

        if radios and self._radio_blocked is not None and blocked != self._radio_blocked:
            observations.append(self._radio_event(radios, blocked=blocked))
        if radios:
            self._radio_blocked = blocked

        snapshot = scan(self._runner)
        if not snapshot.ok:
            # The bus did not answer. Keeping the previous set is the whole point
            # of Snapshot.ok: BlueZ restarting would otherwise produce a
            # disconnect for every device followed by a reconnect for each, which
            # is a burst of events describing something that never happened.
            log.debug("bluetooth: no usable reply from the bus, keeping %d device(s)",
                      len(self._known or ()))
            return observations

        previous = self._known
        self._known = dict(snapshot.connected)

        if previous is None:
            log.debug(
                "bluetooth baseline: %d adapter(s), %d connected",
                len(snapshot.adapters),
                len(snapshot.connected),
            )
            return observations

        for identity in sorted(set(snapshot.connected) - set(previous)):
            observations.append(self._event(snapshot.connected[identity], connected=True))
        for identity in sorted(set(previous) - set(snapshot.connected)):
            observations.append(self._event(previous[identity], connected=False))
        return observations

    def snapshot(self) -> dict[str, Any]:
        """Current state without waiting for a tick, for the UI and for F4."""
        found = scan(self._runner)
        radios = read_radios(self.rfkill_root)
        return {
            "adapters": found.adapters,
            "powered": found.powered,
            "blocked": radios_blocked(radios),
            "radios": [
                {"name": r.name, "soft_blocked": r.soft_blocked, "hard_blocked": r.hard_blocked}
                for r in radios
            ],
            "connected": [d.to_dict() for d in found.connected.values()],
            "known": [d.to_dict() for d in found.known.values()],
        }

    # ------------------------------------------------------------------- events

    def _radio_event(self, radios: Iterable[RadioState], *, blocked: bool) -> Observation:
        states = list(radios)
        reason = next((r.describe() for r in states if r.blocked), "the Bluetooth radio is on")
        return Observation(
            kind="bluetooth.radio",
            summary=reason[0].upper() + reason[1:] if blocked else "Bluetooth is back on",
            data={
                "blocked": blocked,
                "radios": [
                    {"name": r.name, "soft_blocked": r.soft_blocked, "hard_blocked": r.hard_blocked}
                    for r in states
                ],
            },
            severity="notice",
            dedupe_key="bluetooth:radio",
            # The radio coming back ends the condition its going away started, so
            # the debouncer forgets it and the *next* switch-off is an event again
            # rather than a repeat suppressed for fifteen minutes.
            clears=not blocked,
        )

    def _event(self, device: BluetoothDevice, *, connected: bool) -> Observation:
        verb = "connected" if connected else "disconnected"
        # Audio dropping is the one Bluetooth event with any urgency: it happens
        # mid-call or mid-track, and the user already knows something is wrong and
        # wants to know what. Input is a notice for a different reason -- a
        # keyboard that just connected can type (§20).
        loud = device.role in ("audio", "input")
        return Observation(
            kind=f"{device.kind_suffix}.{'connected' if connected else 'disconnected'}",
            summary=f"{device.describe()} {verb}",
            # The spread comes *first* and the flag last, deliberately. A
            # disconnect reports the device as it was last seen -- which is
            # connected -- so ``to_dict()`` carries ``connected: True``, and
            # spreading it over an explicit ``connected: False`` produced a
            # disconnect event whose payload said the device was connected. The
            # event's own view of what just happened has to win.
            data={**device.to_dict(), "connected": connected},
            severity="notice" if loud else "info",
            # Keyed on the device so a disconnect clears its own connect.
            dedupe_key=f"bluetooth:{device.identity}",
            clears=not connected,
        )
