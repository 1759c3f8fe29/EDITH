"""The Bluetooth monitor, against recorded ``busctl`` output and a built rfkill tree.

Two things make this module testable without a paired headset. The BlueZ reply is
JSON from a subprocess, so a test hands it recorded bytes -- and the bytes here are
in the exact encoding ``busctl --system --json=short`` produced on this machine
(``{"type": "a{oa{sa{sv}}}", "data": [{...}]}``, with every property wrapped in its
own ``{"type": ..., "data": ...}``), including the reply from this laptop, whose
radio is soft-blocked and therefore has no adapter under ``/org/bluez`` at all.
The rfkill switches are files, so a test builds the directory.

What no test here can check is whether ``busctl`` still speaks that dialect. The
last test does, against the live bus, and skips where there is none.
"""

from __future__ import annotations

import asyncio
import functools
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

from nexus.db.repos import app_settings
from nexus.monitors import supervisor
from nexus.monitors import bluetooth_monitor
from nexus.monitors.base import Debouncer, MonitorUnavailable
from nexus.permissions import capabilities
from nexus.permissions.engine import engine
from nexus.monitors.bluetooth_monitor import (
    ADAPTER_INTERFACE,
    BLUEZ_SERVICE,
    DEVICE_INTERFACE,
    ICON_ROLES,
    MAJOR_CLASS_ROLES,
    SYSFS_RFKILL,
    BluetoothDevice,
    BluetoothMonitor,
    RadioState,
    parse_managed_objects,
    read_radios,
    radios_blocked,
    scan,
)


def sync(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


# --------------------------------------------------------------- recorded shapes


def variant(signature: str, value: Any) -> dict[str, Any]:
    """One property, in ``busctl --json=short``'s variant encoding."""
    return {"type": signature, "data": value}


def reply(objects: dict[str, Any]) -> str:
    """A ``GetManagedObjects`` reply, wrapped the way busctl wraps it."""
    return json.dumps({"type": "a{oa{sa{sv}}}", "data": [objects]})


#: This laptop, verbatim: the radio is soft-blocked, so BlueZ publishes the daemon
#: node and nothing else. No adapter, no devices -- which is the shape that would
#: read as "no Bluetooth hardware" if rfkill were not consulted.
BLOCKED_LAPTOP = reply(
    {
        "/org/bluez": {
            "org.freedesktop.DBus.Introspectable": {},
            "org.bluez.AgentManager1": {},
            "org.bluez.ProfileManager1": {},
            "org.bluez.HealthManager1": {},
        }
    }
)


def adapter(path: str = "/org/bluez/hci0", *, powered: bool = True) -> dict[str, Any]:
    return {
        path: {
            ADAPTER_INTERFACE: {
                "Address": variant("s", "00:11:22:33:44:55"),
                "Name": variant("s", "nexus-laptop"),
                "Powered": variant("b", powered),
                "Discoverable": variant("b", False),
            }
        }
    }


def remote(
    address: str,
    *,
    name: str = "",
    alias: str | None = None,
    icon: str = "",
    device_class: int | None = None,
    connected: bool = True,
    paired: bool = True,
    trusted: bool = False,
    battery: int | None = None,
    adapter_path: str = "/org/bluez/hci0",
) -> dict[str, Any]:
    """One ``org.bluez.Device1`` object, keyed by the path BlueZ derives."""
    path = f"{adapter_path}/dev_" + address.replace(":", "_")
    properties: dict[str, Any] = {
        "Address": variant("s", address),
        "Alias": variant("s", alias if alias is not None else (name or address)),
        "Paired": variant("b", paired),
        "Trusted": variant("b", trusted),
        "Connected": variant("b", connected),
        "Adapter": variant("o", adapter_path),
    }
    if name:
        properties["Name"] = variant("s", name)
    if icon:
        properties["Icon"] = variant("s", icon)
    if device_class is not None:
        properties["Class"] = variant("u", device_class)
    interfaces: dict[str, Any] = {DEVICE_INTERFACE: properties}
    if battery is not None:
        interfaces["org.bluez.Battery1"] = {"Percentage": variant("y", battery)}
    return {path: interfaces}


def bus(*objects: dict[str, Any], powered: bool = True) -> str:
    merged: dict[str, Any] = {"/org/bluez": {"org.bluez.AgentManager1": {}}}
    merged.update(adapter(powered=powered))
    for entry in objects:
        merged.update(entry)
    return reply(merged)


def runner(payload: str | None) -> Callable[..., str | None]:
    """Stands in for ``busctl``. Records the argv it was asked to run."""

    calls: list[list[str]] = []

    def run(command: list[str], timeout: float = 5.0) -> str | None:
        calls.append(command)
        return payload

    run.calls = calls  # type: ignore[attr-defined]
    return run


# ------------------------------------------------------------------ rfkill trees


def radio_tree(tmp_path: Path, *, soft: str = "0", hard: str = "0",
               name: str = "ideapad_bluetooth") -> Path:
    """The rfkill layout measured on this machine, including the wlan switches.

    Three switches, only one of them Bluetooth: a monitor that read the first
    entry, or any entry, would report the state of the wireless card instead.
    """
    root = tmp_path / "rfkill"
    write(root / "rfkill0", type="wlan", name="ideapad_wlan", soft="0", hard="0")
    write(root / "rfkill1", type="bluetooth", name=name, soft=soft, hard=hard)
    write(root / "rfkill2", type="wlan", name="phy0", soft="0", hard="0")
    return root


def write(directory: Path, **attributes: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for key, value in attributes.items():
        (directory / key).write_text(value + "\n", encoding="utf-8")
    return directory


def flip(root: Path, *, soft: str, hard: str = "0") -> None:
    write(root / "rfkill1", soft=soft, hard=hard)


# ------------------------------------------------------------------- the radio


def test_only_the_bluetooth_switch_is_read(tmp_path: Path):
    """Three switches, two of them wlan. Reading any of them would be wrong.

    ``ideapad_wlan`` sits at ``rfkill0`` on this machine, before the Bluetooth
    one, so a monitor that took the first entry would report the wireless card's
    state -- and on this laptop wlan is on while Bluetooth is off, which means the
    wrong answer is also the reassuring one.
    """
    radios = read_radios(radio_tree(tmp_path, soft="1"))
    assert [r.name for r in radios] == ["ideapad_bluetooth"]
    assert radios[0].blocked is True


def test_a_soft_block_and_a_hard_block_are_described_differently(tmp_path: Path):
    """"Turn Bluetooth on" is useless advice when a hardware switch is off."""
    soft = read_radios(radio_tree(tmp_path, soft="1"))[0]
    hard = read_radios(radio_tree(tmp_path, soft="0", hard="1"))[0]
    assert "switched off" in soft.describe()
    assert "hardware switch" in hard.describe()
    assert soft.blocked and hard.blocked


def test_a_machine_with_no_rfkill_directory_reads_empty(tmp_path: Path):
    assert read_radios(tmp_path / "absent") == []


def test_an_unblocked_radio_is_not_blocked(tmp_path: Path):
    radio = read_radios(radio_tree(tmp_path))[0]
    assert radio.blocked is False
    assert radio.describe() == "the Bluetooth radio is on"


def test_one_working_dongle_means_bluetooth_is_not_off(tmp_path: Path):
    """A laptop with a blocked built-in radio and a working USB dongle.

    ``any`` would call that machine's Bluetooth switched off while a headset was
    connected through the dongle -- a statement the machine's own behaviour
    contradicts, and one the user cannot act on because the toggle they would
    reach for is already on.
    """
    root = tmp_path / "rfkill"
    write(root / "rfkill0", type="bluetooth", name="ideapad_bluetooth", soft="1", hard="0")
    write(root / "rfkill1", type="bluetooth", name="hci1", soft="0", hard="0")
    radios = read_radios(root)
    assert [r.blocked for r in radios] == [True, False]
    assert radios_blocked(radios) is False

    write(root / "rfkill1", soft="1")
    assert radios_blocked(read_radios(root)) is True


def test_no_switches_at_all_is_not_a_blocked_radio(tmp_path: Path):
    """``all(())`` is True, which would make an absent switch mean "off"."""
    assert radios_blocked([]) is False
    assert radios_blocked(read_radios(tmp_path / "absent")) is False


# ------------------------------------------------------------------- the parser


def test_this_laptops_reply_has_no_adapter_but_is_not_a_parse_failure():
    """The recorded shape from a machine whose radio is off.

    An empty snapshot here is the right answer, and it is the same answer a parse
    failure would give -- so the two are told apart on ``powered``/``known`` being
    empty *and* the daemon node having been read at all. What matters downstream is
    that ``has_adapter`` is False, because that is what the availability check
    reads.
    """
    found = parse_managed_objects(BLOCKED_LAPTOP)
    assert found.has_adapter is False
    assert found.connected == {}
    assert found.powered is False


def test_output_that_cannot_be_parsed_is_not_an_empty_bus():
    """A monitor that raises on malformed output stops watching for good.

    ``ok=False`` rather than an empty snapshot, and the distinction is the whole
    point: an empty snapshot is a claim that nothing is connected, and diffing
    against it announces a disconnect for every device on the machine. "We could
    not read the bus" is a third state.
    """
    for payload in ("", "not json", "[]", "{}", '{"data": []}', '{"data": [null]}',
                    '{"data": "hci0"}'):
        found = parse_managed_objects(payload)
        assert found.ok is False, payload
        assert found.has_adapter is False, payload
        assert found.connected == {}, payload


def test_a_reply_with_nonsense_inside_it_is_read_as_far_as_it_goes():
    """Parseable JSON whose objects are the wrong shape is not a failed read.

    These are the shapes that would raise from inside the loop -- an interface map
    that is a number, a property bag that is a number, a class that is not one --
    and every one of them has to be skipped rather than propagated. A monitor that
    raises here is a monitor the supervisor stops after three ticks.
    """
    payloads = [
        '{"data": [{"/org/bluez": 7}]}',
        '{"data": [{"/org/bluez/hci0/dev_X": {"org.bluez.Device1": 5}}]}',
        json.dumps(
            {
                "data": [
                    {
                        "/org/bluez/hci0/dev_Y": {
                            DEVICE_INTERFACE: {
                                "Address": variant("s", "AA:BB:CC:DD:EE:FF"),
                                "Class": variant("s", "not a number"),
                                "Connected": variant("b", True),
                            }
                        }
                    }
                ]
            }
        ),
    ]
    for payload in payloads:
        found = parse_managed_objects(payload)
        assert found.ok is True, payload
        assert found.has_adapter is False, payload
    # The last one is a real device with an unreadable class: it is still tracked,
    # with the class treated as absent rather than the device dropped.
    last = parse_managed_objects(payloads[-1])
    assert list(last.connected) == ["AA:BB:CC:DD:EE:FF"]
    assert last.connected["AA:BB:CC:DD:EE:FF"].role == "device"


def test_a_connected_headset_is_audio_by_its_icon():
    found = parse_managed_objects(
        bus(remote("AA:BB:CC:DD:EE:FF", name="WH-1000XM4",
                   icon="audio-headphones", battery=70))
    )
    device = found.connected["AA:BB:CC:DD:EE:FF"]
    assert device.role == "audio"
    assert device.kind_suffix == "bluetooth.audio"
    assert device.battery_percent == 70
    assert device.describe() == "WH-1000XM4"


def test_a_device_with_no_icon_falls_back_to_its_class_nibble():
    """BlueZ omits ``Icon`` for devices it does not recognise.

    The class of device is a 24-bit field and the major class is bits 8-12, so
    0x240418 -- an audio/video major with the headphones minor -- has to be shifted
    and masked, not compared. Reading the wrong bits would make every device the
    same role, which is the one thing this property exists to distinguish.

    The value matters: 0x240404 (a headset) has 4 in its *low* five bits as well,
    so an unshifted read would land on "audio" by coincidence and this test would
    pass against a mask that ignores the shift entirely. 0x240418's low bits are
    0x18, which is not a major class at all.
    """
    found = parse_managed_objects(
        bus(remote("11:22:33:44:55:66", name="Speaker", device_class=0x240418))
    )
    device = found.connected["11:22:33:44:55:66"]
    assert device.icon == ""
    assert device.role == "audio"
    assert device.kind_suffix == "bluetooth.audio"


def test_a_keyboard_with_no_icon_is_input_by_its_class():
    """0x002540 is the class a Bluetooth keyboard advertises. Bits 8-12 are 0x05.

    The same shift from the other side: the low bits here are 0x00, which is not
    a major class, so a mask that forgot to shift would call a keyboard a plain
    device -- and §20's "a keyboard that just connected can type" would silently
    stop applying to any keyboard BlueZ gave no icon.
    """
    found = parse_managed_objects(
        bus(remote("99:88:77:66:55:44", name="Keychron K2", device_class=0x002540))
    )
    device = found.connected["99:88:77:66:55:44"]
    assert device.icon == ""
    assert device.role == "input"
    assert device.kind_suffix == "bluetooth.input"


def test_a_keyboard_is_input_and_gets_its_own_kind():
    """A keyboard that just connected can type, which is worth its own kind (§20)."""
    found = parse_managed_objects(
        bus(remote("99:88:77:66:55:44", name="Keychron K2", icon="input-keyboard"))
    )
    device = found.connected["99:88:77:66:55:44"]
    assert device.role == "input"
    assert device.kind_suffix == "bluetooth.input"


def test_an_unrecognised_icon_and_class_stay_a_plain_device():
    found = parse_managed_objects(
        bus(remote("AA:AA:AA:AA:AA:AA", name="Thing", icon="something-new",
                   device_class=0x7C0000))
    )
    device = found.connected["AA:AA:AA:AA:AA:AA"]
    assert device.role == "device"
    assert device.kind_suffix == "bluetooth.device"


def test_a_paired_but_absent_device_is_known_and_not_connected():
    """A phone in another room is the normal state, not an event.

    Reporting paired-but-absent devices would make every startup announce a list
    of things that are not here, which is the same failure as announcing the
    devices that *are* -- a channel the user learns to ignore.
    """
    found = parse_managed_objects(
        bus(
            remote("AA:BB:CC:DD:EE:FF", name="Headset", icon="audio-headset"),
            remote("22:22:22:22:22:22", name="Phone", icon="phone", connected=False),
        )
    )
    assert sorted(found.connected) == ["AA:BB:CC:DD:EE:FF"]
    assert sorted(found.known) == ["22:22:22:22:22:22", "AA:BB:CC:DD:EE:FF"]


def test_a_nameless_device_is_described_by_its_address():
    """"Unknown device" is not something the user can match against their phone."""
    found = parse_managed_objects(bus(remote("0C:0C:0C:0C:0C:0C", icon="input-mouse")))
    device = found.connected["0C:0C:0C:0C:0C:0C"]
    assert device.name == ""
    assert device.describe() == "input 0C:0C:0C:0C:0C:0C"


def test_the_address_is_the_identity_not_the_object_path():
    """The same headset on a second controller must be the same device.

    BlueZ derives the object path from the adapter *and* the address, so a machine
    with two controllers -- a built-in one and a USB dongle -- gives one headset two
    paths. Keying on the path would report a reconnection through the dongle as a
    different device arriving while the first was still connected.
    """
    on_hci0 = parse_managed_objects(bus(remote("AA:BB:CC:DD:EE:FF", name="Headset")))
    on_hci1 = parse_managed_objects(
        reply(
            {
                **adapter("/org/bluez/hci1"),
                **remote("AA:BB:CC:DD:EE:FF", name="Headset", adapter_path="/org/bluez/hci1"),
            }
        )
    )
    assert list(on_hci0.connected) == list(on_hci1.connected) == ["AA:BB:CC:DD:EE:FF"]
    left = on_hci0.connected["AA:BB:CC:DD:EE:FF"]
    right = on_hci1.connected["AA:BB:CC:DD:EE:FF"]
    assert left.path != right.path
    assert left.identity == right.identity


def test_a_device_with_no_address_is_still_tracked_separately():
    """Two anonymous devices must not collapse into one entry."""
    first = BluetoothDevice(path="/org/bluez/hci0/dev_one")
    second = BluetoothDevice(path="/org/bluez/hci0/dev_two")
    assert first.identity != second.identity


def test_no_battery_interface_means_unknown_and_never_zero():
    """A headset at 0% would be off. None is the honest answer."""
    found = parse_managed_objects(bus(remote("AA:BB:CC:DD:EE:FF", name="Mouse",
                                             icon="input-mouse")))
    assert found.connected["AA:BB:CC:DD:EE:FF"].battery_percent is None


def test_an_unreadable_battery_percentage_is_unknown_and_never_zero():
    """The interface is there and its value is not a number.

    Separate from the test above because it exercises the other exit: that one
    never enters the battery branch at all, this one enters it and fails to read
    what it finds. Both have to give None, and a ``0`` from either would be a
    battery reading NEXUS would go on to say out loud -- "your headset is at 0%"
    about a headset that never reported a level.
    """
    for value in (variant("s", "unknown"), variant("s", ""), {"type": "y"}):
        payload = json.dumps(
            {
                "data": [
                    {
                        "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF": {
                            DEVICE_INTERFACE: {
                                "Address": variant("s", "AA:BB:CC:DD:EE:FF"),
                                "Name": variant("s", "Headset"),
                                "Icon": variant("s", "audio-headphones"),
                                "Connected": variant("b", True),
                            },
                            "org.bluez.Battery1": {"Percentage": value},
                        }
                    }
                ]
            }
        )
        found = parse_managed_objects(payload)
        device = found.connected["AA:BB:CC:DD:EE:FF"]
        assert device.battery_percent is None, value
        assert device.to_dict()["battery_percent"] is None, value


def test_an_unpowered_adapter_is_still_an_adapter():
    """"Present but off" is a different answer from "not here at all".

    The availability check refuses only the second: an adapter the user can power
    on is exactly the thing worth watching for.
    """
    found = parse_managed_objects(bus(powered=False))
    assert found.has_adapter is True
    assert found.powered is False


def test_an_alias_that_is_only_the_address_is_not_a_name():
    """BlueZ writes the address, dashes and all, as the alias of a nameless device.

    Taking it would set ``name`` to a string that is not a name: ``describe()``
    would then print an address where the caller asked for what the user calls the
    thing, and ``to_dict()["name"]`` would claim a name the device does not have.
    """
    found = parse_managed_objects(
        bus(remote("0C:0C:0C:0C:0C:0C", alias="0C-0C-0C-0C-0C-0C", icon="input-mouse"))
    )
    device = found.connected["0C:0C:0C:0C:0C:0C"]
    assert device.name == ""
    assert device.to_dict()["name"] == ""
    assert device.describe() == "input 0C:0C:0C:0C:0C:0C"


def test_a_user_given_alias_wins_over_the_advertised_name():
    """Renaming a device in the system's Bluetooth panel sets ``Alias``.

    ``Name`` is what the device advertises about itself and it does not change; a
    user who renamed their headset "Desk headphones" should hear that name back,
    not the model number.
    """
    found = parse_managed_objects(
        bus(remote("AA:BB:CC:DD:EE:FF", name="WH-1000XM4", alias="Desk headphones",
                   icon="audio-headphones"))
    )
    assert found.connected["AA:BB:CC:DD:EE:FF"].describe() == "Desk headphones"


# ------------------------------------------------------------------- sampling


@sync
async def test_the_first_sample_reports_nothing(tmp_path: Path):
    """Headphones already connected were not just connected."""
    monitor = BluetoothMonitor(
        runner=runner(bus(remote("AA:BB:CC:DD:EE:FF", name="Headset",
                                 icon="audio-headphones"))),
        rfkill=radio_tree(tmp_path),
    )
    assert await monitor.sample() == []


@sync
async def test_a_connection_after_the_baseline_is_reported(tmp_path: Path):
    empty = bus()
    with_headset = bus(remote("AA:BB:CC:DD:EE:FF", name="WH-1000XM4",
                              icon="audio-headphones", battery=80))
    payloads = [empty, with_headset]

    def run(command: list[str], timeout: float = 5.0) -> str | None:
        return payloads.pop(0) if payloads else with_headset

    monitor = BluetoothMonitor(runner=run, rfkill=radio_tree(tmp_path))
    assert await monitor.sample() == []

    observations = await monitor.sample()
    assert len(observations) == 1
    event = observations[0]
    assert event.kind == "bluetooth.audio.connected"
    assert "WH-1000XM4 connected" == event.summary
    assert event.severity == "notice"
    assert event.data["connected"] is True
    assert event.data["battery_percent"] == 80
    assert event.clears is False


@sync
async def test_a_disconnection_is_reported_and_clears(tmp_path: Path):
    """The disconnect has to clear the connect, or the debouncer holds the next one."""
    with_headset = bus(remote("AA:BB:CC:DD:EE:FF", name="Headset",
                              icon="audio-headphones"))
    payloads = [with_headset, bus()]

    def run(command: list[str], timeout: float = 5.0) -> str | None:
        return payloads.pop(0) if payloads else bus()

    monitor = BluetoothMonitor(runner=run, rfkill=radio_tree(tmp_path))
    await monitor.sample()

    observations = await monitor.sample()
    assert len(observations) == 1
    event = observations[0]
    assert event.kind == "bluetooth.audio.disconnected"
    assert event.clears is True
    assert event.data["connected"] is False


@sync
async def test_a_reconnect_is_reported_again_because_the_disconnect_cleared_it(tmp_path: Path):
    """End to end through the real debouncer -- the thing that would swallow it.

    A headset that drops and comes back within the fifteen-minute window is the
    single most common Bluetooth event there is. Without ``clears`` on the
    disconnect, the second connect looks like a repeat of the first and the user
    hears nothing when their audio returns.
    """
    headset = remote("AA:BB:CC:DD:EE:FF", name="Headset", icon="audio-headphones")
    # Starts *empty*: the first sample is a baseline that reports nothing, so a
    # headset present from the start would never reach the debouncer -- and its
    # disconnect would then have nothing to clear, which is a different (correct)
    # behaviour that would mask the one under test.
    payloads = [bus(), bus(headset), bus(), bus(headset)]

    def run(command: list[str], timeout: float = 5.0) -> str | None:
        return payloads.pop(0) if payloads else bus(headset)

    monitor = BluetoothMonitor(runner=run, rfkill=radio_tree(tmp_path))
    debouncer = Debouncer(min_repeat_seconds=900)
    await monitor.sample()

    arrived = await monitor.sample()
    assert [debouncer.should_report(o) for o in arrived] == [True]

    dropped = await monitor.sample()
    assert [o.kind for o in dropped] == ["bluetooth.audio.disconnected"]
    assert [debouncer.should_report(o) for o in dropped] == [True]

    returned = await monitor.sample()
    assert [o.kind for o in returned] == ["bluetooth.audio.connected"]
    assert [debouncer.should_report(o) for o in returned] == [True], (
        "a headset that came back must be reported, not suppressed as a repeat"
    )


@sync
async def test_two_devices_do_not_share_one_dedupe_key(tmp_path: Path):
    """One key per device, or the second disconnect is a suppressed repeat."""
    headset = remote("AA:BB:CC:DD:EE:FF", name="Headset", icon="audio-headphones")
    mouse = remote("11:11:11:11:11:11", name="Mouse", icon="input-mouse")
    payloads = [bus(), bus(headset, mouse), bus()]

    def run(command: list[str], timeout: float = 5.0) -> str | None:
        return payloads.pop(0) if payloads else bus()

    monitor = BluetoothMonitor(runner=run, rfkill=radio_tree(tmp_path))
    debouncer = Debouncer(min_repeat_seconds=900)
    await monitor.sample()

    arrived = await monitor.sample()
    assert len({o.dedupe_key for o in arrived}) == 2
    assert [debouncer.should_report(o) for o in arrived] == [True, True]

    left = await monitor.sample()
    assert len({o.dedupe_key for o in left}) == 2, (
        "two devices must be two conditions, or the second disconnect is suppressed"
    )
    assert [debouncer.should_report(o) for o in left] == [True, True]


@sync
async def test_a_headset_and_a_mouse_get_different_kinds_and_severities(tmp_path: Path):
    """Audio and input are louder than a plain device, and for different reasons.

    Audio because it drops mid-call; input because a keyboard or mouse that just
    connected can act on this machine (§20). A generic device -- a phone syncing --
    is a fact for the record.
    """
    payloads = [
        bus(),
        bus(
            remote("AA:BB:CC:DD:EE:FF", name="Headset", icon="audio-headphones"),
            remote("11:11:11:11:11:11", name="Mouse", icon="input-mouse"),
            remote("22:22:22:22:22:22", name="Phone", icon="phone"),
        ),
    ]

    def run(command: list[str], timeout: float = 5.0) -> str | None:
        return payloads.pop(0) if payloads else payloads[-1]

    monitor = BluetoothMonitor(runner=run, rfkill=radio_tree(tmp_path))
    await monitor.sample()

    by_kind = {o.kind: o for o in await monitor.sample()}
    assert sorted(by_kind) == [
        "bluetooth.audio.connected",
        "bluetooth.device.connected",
        "bluetooth.input.connected",
    ]
    assert by_kind["bluetooth.audio.connected"].severity == "notice"
    assert by_kind["bluetooth.input.connected"].severity == "notice"
    assert by_kind["bluetooth.device.connected"].severity == "info"


@sync
async def test_nothing_changing_reports_nothing(tmp_path: Path):
    payload = bus(remote("AA:BB:CC:DD:EE:FF", name="Headset", icon="audio-headphones"))
    monitor = BluetoothMonitor(runner=runner(payload), rfkill=radio_tree(tmp_path))
    await monitor.sample()
    assert await monitor.sample() == []
    assert await monitor.sample() == []


@sync
async def test_a_paired_device_appearing_in_the_list_is_not_a_connection(tmp_path: Path):
    """BlueZ learns about devices by scanning. Being seen is not being connected.

    Without this, walking past someone's speaker adds an object to
    ``GetManagedObjects`` and NEXUS announces it as having connected to this
    machine -- which is both wrong and alarming.
    """
    payloads = [
        bus(),
        bus(remote("33:33:33:33:33:33", name="Someone's speaker",
                   icon="audio-speakers", connected=False, paired=False)),
    ]

    def run(command: list[str], timeout: float = 5.0) -> str | None:
        return payloads.pop(0) if payloads else payloads[-1]

    monitor = BluetoothMonitor(runner=run, rfkill=radio_tree(tmp_path))
    await monitor.sample()
    assert await monitor.sample() == []


@sync
async def test_busctl_failing_is_not_a_wave_of_disconnections(tmp_path: Path):
    """The daemon restarting must not read as every device leaving at once.

    ``busctl`` returns non-zero while BlueZ is down, and a monitor that treated
    "no reply" as "nothing is connected" would announce a disconnect for every
    device and then a connect for every one of them a few seconds later.
    """
    headset = remote("AA:BB:CC:DD:EE:FF", name="Headset", icon="audio-headphones")
    replies: list[str | None] = [bus(headset), None, bus(headset)]

    def run(command: list[str], timeout: float = 5.0) -> str | None:
        return replies.pop(0) if replies else bus(headset)

    monitor = BluetoothMonitor(runner=run, rfkill=radio_tree(tmp_path))
    await monitor.sample()

    assert await monitor.sample() == [], "a failed call is not a disconnection"
    assert await monitor.sample() == [], "and the device was never forgotten"


@sync
async def test_stopping_drops_the_baseline_rather_than_replaying_the_gap(tmp_path: Path):
    """A headset connected while the monitor was off did not connect just now."""
    headset = remote("AA:BB:CC:DD:EE:FF", name="Headset", icon="audio-headphones")
    payloads = [bus(), bus(headset), bus(headset), bus()]

    def run(command: list[str], timeout: float = 5.0) -> str | None:
        return payloads.pop(0) if payloads else bus()

    monitor = BluetoothMonitor(runner=run, rfkill=radio_tree(tmp_path))
    await monitor.sample()
    await monitor.on_stop()

    assert await monitor.sample() == []
    # The new baseline includes it, so the *next* change is still seen.
    assert [o.kind for o in await monitor.sample()] == []
    assert [o.kind for o in await monitor.sample()] == ["bluetooth.audio.disconnected"]


# --------------------------------------------------------------- radio events


@sync
async def test_the_radio_being_switched_off_is_one_event_not_one_per_tick(tmp_path: Path):
    """A blocked radio is a standing condition; saying so every ten seconds is noise."""
    radios = radio_tree(tmp_path)
    monitor = BluetoothMonitor(runner=runner(bus()), rfkill=radios)
    await monitor.sample()

    flip(radios, soft="1")
    observations = await monitor.sample()
    assert [o.kind for o in observations] == ["bluetooth.radio"]
    assert observations[0].data["blocked"] is True
    assert "switched off" in observations[0].summary.lower()
    assert observations[0].clears is False

    assert await monitor.sample() == []
    assert await monitor.sample() == []


@sync
async def test_the_radio_coming_back_clears_the_condition(tmp_path: Path):
    """Otherwise the debouncer suppresses the next switch-off for fifteen minutes."""
    radios = radio_tree(tmp_path, soft="1")
    monitor = BluetoothMonitor(runner=runner(bus()), rfkill=radios)
    debouncer = Debouncer(min_repeat_seconds=900)
    await monitor.sample()

    flip(radios, soft="0")
    back = await monitor.sample()
    assert [o.kind for o in back] == ["bluetooth.radio"]
    assert back[0].clears is True
    assert back[0].data["blocked"] is False
    assert [debouncer.should_report(o) for o in back] == [False], (
        "nothing reported the block, so its clear has nothing to clear"
    )

    flip(radios, soft="1")
    off_again = await monitor.sample()
    assert [debouncer.should_report(o) for o in off_again] == [True]
    flip(radios, soft="0")
    on_again = await monitor.sample()
    assert [debouncer.should_report(o) for o in on_again] == [True]
    flip(radios, soft="1")
    assert [debouncer.should_report(o) for o in await monitor.sample()] == [True], (
        "a radio switched off again must be reported, not held as a repeat"
    )


@sync
async def test_a_machine_with_no_rfkill_reports_no_radio_events(tmp_path: Path):
    """Absent switches are not an unblocked radio.

    A tree that does not exist gives an empty list, and treating that as "not
    blocked" would emit a spurious "Bluetooth is back on" the first time a machine
    without rfkill sampled twice.
    """
    monitor = BluetoothMonitor(runner=runner(bus()), rfkill=tmp_path / "absent")
    assert await monitor.sample() == []
    assert await monitor.sample() == []
    assert await monitor.sample() == []


@sync
async def test_a_tick_that_could_not_read_rfkill_does_not_forget_the_block(tmp_path: Path):
    """One unreadable tick must not turn into a switch-off that never happened.

    ``read_radios`` returns an empty list both for "this machine has no Bluetooth
    switch" and for "sysfs did not answer this time" -- they are indistinguishable
    from here. So the remembered state is only ever written from a read that found
    switches. Writing it unconditionally means a single failed read sets it to
    False, and the next successful read of a radio that has been off the whole time
    is reported as the user having just switched it off.
    """
    radios = radio_tree(tmp_path, soft="1")
    monitor = BluetoothMonitor(runner=runner(bus()), rfkill=radios)
    await monitor.sample()  # baseline: blocked, and it says nothing about it

    moved = tmp_path / "gone"
    radios.rename(moved)
    assert await monitor.sample() == [], "an unreadable tick is not an event"

    moved.rename(radios)
    assert await monitor.sample() == [], (
        "the radio was off the whole time, so nothing was switched off"
    )

    # And the state it kept is the real one: turning the radio on is still an event.
    flip(radios, soft="0")
    assert [o.kind for o in await monitor.sample()] == ["bluetooth.radio"]


@sync
async def test_a_blocked_radio_does_not_stop_the_device_diff(tmp_path: Path):
    """Both things can happen in one tick, and both are reported.

    Returning early on a blocked radio would mean the devices that dropped
    *because* of it were never reported -- and their conditions would still be
    marked active in the debouncer, so their eventual reconnect would be
    suppressed as a repeat.
    """
    headset = remote("AA:BB:CC:DD:EE:FF", name="Headset", icon="audio-headphones")
    radios = radio_tree(tmp_path)
    payloads = [bus(headset), bus()]

    def run(command: list[str], timeout: float = 5.0) -> str | None:
        return payloads.pop(0) if payloads else bus()

    monitor = BluetoothMonitor(runner=run, rfkill=radios)
    await monitor.sample()

    flip(radios, soft="1")
    kinds = sorted(o.kind for o in await monitor.sample())
    assert kinds == ["bluetooth.audio.disconnected", "bluetooth.radio"]


# --------------------------------------------------------------- availability


def test_a_machine_with_no_adapter_and_no_switch_says_so(tmp_path: Path):
    monitor = BluetoothMonitor(runner=runner(BLOCKED_LAPTOP), rfkill=tmp_path / "absent")
    with pytest.raises(MonitorUnavailable) as raised:
        monitor.check_available()
    assert "no Bluetooth adapter" in str(raised.value)


def test_a_blocked_radio_is_available_because_the_block_is_the_news(tmp_path: Path):
    """This laptop, exactly: rfkill says bluetooth, BlueZ shows no adapter.

    Refusing here would be the worst possible reading of the situation -- the
    monitor would stop precisely when there is something to say, and the moment the
    user switches the radio back on would be the moment nothing is watching.
    """
    monitor = BluetoothMonitor(
        runner=runner(BLOCKED_LAPTOP), rfkill=radio_tree(tmp_path, soft="1")
    )
    monitor.check_available()  # must not raise


def test_an_adapter_with_no_rfkill_entry_is_available(tmp_path: Path):
    """A USB dongle on a desktop: BlueZ sees it, the kernel lists no switch."""
    monitor = BluetoothMonitor(runner=runner(bus()), rfkill=tmp_path / "absent")
    monitor.check_available()  # must not raise


def test_the_monitor_is_off_until_switched_on(tmp_path: Path):
    assert BluetoothMonitor(runner=runner(bus()), rfkill=radio_tree(tmp_path)).enabled() is False


def test_the_switch_is_a_real_setting_and_not_a_blank_one(tmp_path: Path):
    """``enabled()`` returns False for a blank setting name too.

    So the test above passes whether or not this monitor has a switch at all. A
    monitor with no switch is one the user can never turn on: the Awareness toggle
    would write ``monitor_bluetooth_enabled`` and nothing would read it.
    """
    monitor = BluetoothMonitor(runner=runner(bus()), rfkill=radio_tree(tmp_path))
    assert monitor.enabled_setting == "monitor_bluetooth_enabled"
    assert monitor.interval_setting == "monitor_bluetooth_interval_seconds"
    assert app_settings.DEFAULTS[monitor.enabled_setting] is False
    assert monitor.interval_setting in app_settings.DEFAULTS

    app_settings.set(monitor.enabled_setting, True)
    try:
        assert monitor.enabled() is True
    finally:
        app_settings.reset(monitor.enabled_setting)
    assert monitor.enabled() is False


def test_the_monitor_is_registered_so_it_can_actually_run():
    """A monitor nobody registered is a watcher that silently never runs."""
    assert isinstance(supervisor().get("bluetooth"), BluetoothMonitor)
    assert "bluetooth" in [m["id"] for m in supervisor().status()["monitors"]]


def test_the_capability_it_asks_for_is_one_NEXUS_actually_has(tmp_path: Path):
    """A capability name with no capability behind it is a toggle that only fails.

    ``engine.evaluate`` denies an unknown name outright, so ``check_permission``
    raises and the supervisor marks the monitor unavailable the first time anyone
    switches it on -- with a reason that reads like a missing binary. The USB
    monitor shipped exactly this for one draft ("system.read", which does not
    exist).

    Asserting the name is in the registry is not enough on its own: the failure is
    a typo in a class attribute, and what it breaks is the *start path*. So this
    runs the real check too, which is the thing the supervisor calls.
    """
    monitor = BluetoothMonitor(runner=runner(bus()), rfkill=radio_tree(tmp_path))
    assert capabilities.exists(monitor.capability), (
        f"{monitor.capability!r} is not a capability NEXUS has, so this monitor "
        "reports itself unavailable the moment it is switched on"
    )
    monitor.check_permission()  # must not raise
    assert engine.evaluate(monitor.capability).outcome != "deny"


def test_nothing_here_can_pair_connect_or_trust_a_device():
    """§20: a paired device can act as a keyboard, so pairing is the user's.

    Read as source rather than exercised, because the property is the *absence* of
    a call -- there is no behaviour to invoke and no test that can observe a method
    that was never written. A grep is the only shape that fails when someone adds
    one.
    """
    source = Path(bluetooth_monitor.__file__).read_text(encoding="utf-8")
    for method in ("Pair", "Connect", "RemoveDevice", "SetProperty", "Disconnect"):
        assert f'"{method}"' not in source, (
            f"this monitor calls {method}; pairing and connecting belong to the "
            "user in the system's own dialog, not to a background watcher"
        )
    # ``busctl call`` is the only bus verb used, and only on the object manager.
    assert source.count('"call"') == 1


# ----------------------------------------------------- against the real system


@pytest.mark.skipif(not shutil.which("busctl"), reason="no busctl on this machine")
def test_busctl_still_speaks_the_dialect_this_module_parses():
    """Everything above is recorded bytes. This is the part that can go stale.

    If ``--json=short`` changed its wrapping, or BlueZ moved its object manager,
    every test in this file would still pass and the monitor would report nothing
    forever. So: make the real call, and require that it either fails cleanly (no
    BlueZ on the bus) or parses into a snapshot whose shape this module understands.
    """
    proc = subprocess.run(
        ["busctl", "--system", "--json=short", "call", BLUEZ_SERVICE, "/",
         "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"],
        capture_output=True, text=True, timeout=15,
    )
    if proc.returncode != 0:
        pytest.skip(f"BlueZ is not answering on this machine: {proc.stderr.strip()[:80]}")

    parsed = json.loads(proc.stdout)
    assert parsed["type"].startswith("a{oa{sa{sv}}}"), (
        f"busctl's reply signature changed to {parsed['type']!r}"
    )
    found = parse_managed_objects(proc.stdout)
    assert found.ok, "the live reply did not parse, so the encoding has changed"
    # ``/org/bluez`` itself is always in the reply when the daemon is up, so a
    # snapshot that saw nothing at all means the parse walked the wrong level.
    assert "/org/bluez" in parsed["data"][0]
    for device in found.known.values():
        assert device.identity
        assert device.role in {*ICON_ROLES.values(), *MAJOR_CLASS_ROLES.values(), "device"}


@pytest.mark.skipif(not SYSFS_RFKILL.is_dir(), reason="no rfkill on this kernel")
def test_the_rfkill_layout_matches_this_kernel():
    """The attribute names are read as files; a rename would be silent."""
    entries = [p for p in SYSFS_RFKILL.iterdir() if p.is_dir() or p.is_symlink()]
    assert entries, "a kernel with rfkill has at least one switch"
    for entry in entries:
        for attribute in ("type", "name", "soft", "hard"):
            assert (entry / attribute).is_file(), (
                f"{entry.name} has no {attribute}; the rfkill attribute names changed"
            )
    for radio in read_radios():
        assert radio.name
        assert radio.describe()
