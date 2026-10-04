"""The USB monitor, against a sysfs tree the test builds and changes.

Real insertion and removal need a hand on the hardware, which no test has. What a
test *can* do is build the directory layout the kernel publishes -- copied from
the shapes this machine actually reports, including the ones that look wrong: a
mouse whose device class is 00 and whose real class hides in an interface, a
card reader claiming class ff, three hubs nobody plugged in.

So these tests reproduce the tree and mutate it. That covers everything except
whether ``/sys/bus/usb/devices`` has the layout assumed here -- and the last test
in this file checks that against the live kernel, skipping where there is none.
"""

from __future__ import annotations

import asyncio
import functools
import shutil
from pathlib import Path
from typing import Any, Callable

import pytest

from nexus.db.repos import app_settings
from nexus.monitors import supervisor
from nexus.monitors.base import Debouncer, MonitorUnavailable
from nexus.permissions import capabilities
from nexus.monitors.usb_monitor import (
    SYSFS_USB,
    UsbDevice,
    UsbMonitor,
    read_device,
    scan,
)


def sync(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def write(directory: Path, **attributes: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, value in attributes.items():
        (directory / name).write_text(value + "\n", encoding="utf-8")
    return directory


def device(
    root: Path,
    port: str,
    *,
    vendor_id: str = "1234",
    product_id: str = "5678",
    manufacturer: str = "",
    product: str = "",
    serial: str = "",
    device_class: str = "00",
    interfaces: tuple[str, ...] = ("03",),
    removable: str = "removable",
) -> Path:
    """One device directory plus its interface directories, as sysfs lays them out."""
    entry = write(
        root / port,
        idVendor=vendor_id,
        idProduct=product_id,
        manufacturer=manufacturer,
        product=product,
        serial=serial,
        bDeviceClass=device_class,
        removable=removable,
    )
    for index, interface_class in enumerate(interfaces):
        # Interfaces live *inside* the device directory on the real kernel --
        # /sys/bus/usb/devices/1-1.2/1-1.2:1.0 -- and are additionally exposed at
        # the top level as symlinks. Building only the top-level entry, as an
        # earlier version of this fixture did, gave every device an empty
        # interface list, so a mouse read as an unknown device and four tests
        # failed on a fixture defect rather than a real one.
        name = f"{port}:1.{index}"
        write(entry / name, bInterfaceClass=interface_class)
        link = root / name
        if not link.exists():
            link.symlink_to(entry / name)
    return entry


def unplug(root: Path, port: str) -> None:
    """Remove a device the way the kernel does: the tree and its top-level links."""
    entry = root / port
    for link in sorted(root.iterdir()):
        if link.is_symlink() and link.name.startswith(port + ":"):
            link.unlink()
    shutil.rmtree(entry)


# The shapes measured on this machine, so the fixtures are not invented.
def real_tree(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    # Root hubs -- "usbN" -- and the internal hubs behind them.
    for name in ("usb1", "usb2", "usb3", "usb4"):
        device(root, name, vendor_id="1d6b", product_id="0002",
               device_class="09", interfaces=("09",), removable="unknown")
    device(root, "1-1", vendor_id="8087", product_id="0024",
           device_class="09", interfaces=("09",), removable="fixed")
    device(root, "3-1", vendor_id="8087", product_id="0024",
           device_class="09", interfaces=("09",), removable="fixed")
    # A mouse: device class 00, real class hiding in the interface.
    device(root, "1-1.2", vendor_id="10c4", product_id="8105",
           manufacturer="YSPRINGTECH", product="USB OPTICAL MOUSE",
           device_class="00", interfaces=("03",))
    # A card reader that claims vendor-specific for both.
    device(root, "1-1.4", vendor_id="0bda", product_id="0129",
           manufacturer="Generic", product="USB2.0-CRW",
           serial="20100201396000000", device_class="ff", interfaces=("ff",),
           removable="fixed")
    return root


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return real_tree(tmp_path / "usb")


# ------------------------------------------------------------------- scanning


def test_hubs_are_not_devices(tree: Path):
    """Six of the eight entries on this machine are hubs nobody plugged in.

    Reporting them would mean every startup announces four root hubs, and a real
    insertion into a powered-down hub port would arrive as two events.
    """
    found = scan(tree)
    assert len(found) == 2, sorted(found)
    assert all(not d.is_hub for d in found.values())


def test_interface_directories_are_not_counted_as_devices(tree: Path):
    """``1-1.2:1.0`` is an interface. Counting it doubles the mouse."""
    products = sorted(d.product for d in scan(tree).values())
    assert products == ["USB OPTICAL MOUSE", "USB2.0-CRW"]


def test_an_interface_is_rejected_by_its_name_not_by_luck(tmp_path: Path):
    """The colon check is the rule; the missing ``idVendor`` is a coincidence.

    Interfaces happen not to carry ``idVendor`` today, so a scan with no name
    check would still exclude them -- which means the previous test passes either
    way and proves nothing about the rule. Deciding by name is what keeps the
    scan correct if a kernel ever starts publishing more attributes on interface
    entries, and this is the only shape that can tell the two apart.
    """
    root = tmp_path / "usb"
    device(root, "1-1", vendor_id="aaaa", product_id="bbbb", product="Real device")
    write(root / "1-1:1.9", idVendor="aaaa", idProduct="bbbb",
          bDeviceClass="00", product="Not a device")
    found = scan(root)
    assert [d.product for d in found.values()] == ["Real device"]


def test_a_composite_device_skips_a_placeholder_interface(tmp_path: Path):
    """A vendor-specific interface first, the real one second.

    ``ff`` means "ask the vendor", which tells the user nothing. Returning the
    first interface class regardless would label this a plain device instead of
    the storage it is -- and storage is the class that gets a notice rather than
    a log line, so the mislabel is audible.
    """
    root = tmp_path / "usb"
    device(root, "1-1", vendor_id="0781", product_id="5567", product="Combo",
           device_class="00", interfaces=("ff", "08"))
    found = next(iter(scan(root).values()))
    assert found.effective_class == 0x08
    assert found.kind_suffix == "usb.storage"


def test_a_mouse_reporting_device_class_00_is_still_an_input_device(tree: Path):
    """The measured shape that breaks a monitor trusting bDeviceClass alone."""
    mouse = next(d for d in scan(tree).values() if d.product == "USB OPTICAL MOUSE")
    assert mouse.device_class == 0x00
    assert mouse.effective_class == 0x03
    assert mouse.kind_suffix == "usb.input"


def test_a_device_with_no_vendor_id_is_not_a_device(tmp_path: Path):
    write(tmp_path / "1-1", bDeviceClass="00")
    assert scan(tmp_path) == {}


def test_interfaces_are_read_from_inside_the_device_directory(tmp_path: Path):
    """A device tree path, where the top-level symlinks do not exist.

    ``/sys/bus/usb/devices`` carries interfaces twice -- inside the device
    directory and as sibling symlinks -- so a lookup in the *parent* happens to
    work there and the difference is invisible to every test above. The device
    tree the bus entries point at has only the inner copy:
    ``/sys/devices/pci0000:00/.../usb1/1-1/1-1.2/1-1.2:1.0`` with nothing beside
    it. Anything that resolves a bus entry before reading it lands there, and a
    parent lookup then finds no interfaces at all -- which turns this stick back
    into an unknown device.
    """
    hub = write(tmp_path / "usb1", idVendor="1d6b", idProduct="0002",
                bDeviceClass="09")
    write(hub / "1-0:1.0", bInterfaceClass="09")
    inner = write(hub / "1-1", idVendor="0781", idProduct="5567",
                  product="Cruzer", bDeviceClass="00")
    write(inner / "1-1:1.0", bInterfaceClass="08")

    found = read_device(inner)
    assert found is not None
    assert found.interface_classes == (0x08,)
    assert found.kind_suffix == "usb.storage"


def test_a_missing_tree_scans_empty_rather_than_raising(tmp_path: Path):
    assert scan(tmp_path / "nothing-here") == {}


def test_a_device_that_vanishes_mid_read_is_skipped(tmp_path: Path):
    """Enumerating a tree that changes underneath is this monitor's normal case."""
    entry = tmp_path / "1-1"
    entry.mkdir()
    assert read_device(entry) is None


# ------------------------------------------------------------------- identity


def test_two_identical_devices_are_two_devices(tmp_path: Path):
    """Without the port fallback, unplugging one mouse looks like both leaving."""
    root = tmp_path / "usb"
    device(root, "1-1", vendor_id="aaaa", product_id="bbbb", product="Mouse")
    device(root, "1-2", vendor_id="aaaa", product_id="bbbb", product="Mouse")
    assert len(scan(root)) == 2


def test_a_serial_makes_a_device_the_same_device_in_another_port():
    moved_left = UsbDevice(path="1-1", vendor_id="a", product_id="b", serial="S1")
    moved_right = UsbDevice(path="2-4", vendor_id="a", product_id="b", serial="S1")
    assert moved_left.identity == moved_right.identity

    # Without a serial there is nothing to recognise it by but the port.
    no_serial_left = UsbDevice(path="1-1", vendor_id="a", product_id="b")
    no_serial_right = UsbDevice(path="2-4", vendor_id="a", product_id="b")
    assert no_serial_left.identity != no_serial_right.identity


def test_a_device_with_no_name_is_described_by_its_ids():
    """"Unknown device" tells the user nothing they can look up."""
    nameless = UsbDevice(path="1-1", vendor_id="8087", product_id="0024",
                         interface_classes=(0x08,))
    assert "8087:0024" in nameless.describe()
    assert "storage" in nameless.describe()


def test_a_named_device_is_described_by_its_name():
    named = UsbDevice(path="1-1", vendor_id="10c4", product_id="8105",
                      vendor="YSPRINGTECH", product="USB OPTICAL MOUSE",
                      interface_classes=(0x03,))
    assert named.describe() == "YSPRINGTECH USB OPTICAL MOUSE"


# ------------------------------------------------------------------- sampling


@sync
async def test_the_first_sample_reports_nothing(tree: Path):
    """Devices already attached were not just plugged in.

    Announcing them at startup is a lie told on a schedule, and the kind that
    teaches a user to stop listening to the channel.
    """
    monitor = UsbMonitor(root=tree)
    assert await monitor.sample() == []


@sync
async def test_an_insertion_after_the_baseline_is_reported(tree: Path):
    monitor = UsbMonitor(root=tree)
    await monitor.sample()

    device(tree, "1-1.3", vendor_id="0781", product_id="5567",
           manufacturer="SanDisk", product="Cruzer Blade",
           serial="4C530001", device_class="00", interfaces=("08",))
    observations = await monitor.sample()

    assert len(observations) == 1
    event = observations[0]
    assert event.kind == "usb.storage.added"
    assert "SanDisk Cruzer Blade" in event.summary
    assert event.data["attached"] is True
    assert event.data["class_name"] == "storage device"
    assert event.clears is False


@sync
async def test_a_removal_after_the_baseline_is_reported_and_clears(tree: Path):
    """The removal has to clear the insertion, or the debouncer holds the next one.

    Same dedupe key, ``clears=True``: that is what lets the debouncer forget the
    condition, so plugging the same stick back in fifteen seconds later is still
    an event rather than a suppressed repeat.
    """
    monitor = UsbMonitor(root=tree)
    await monitor.sample()

    unplug(tree, "1-1.2")

    observations = await monitor.sample()
    assert len(observations) == 1
    event = observations[0]
    assert event.kind == "usb.input.removed"
    assert event.clears is True
    assert event.data["attached"] is False


@sync
async def test_a_replug_is_reported_again_because_the_removal_cleared_it(tree: Path):
    """End to end through the real debouncer -- the thing that would swallow it."""
    monitor = UsbMonitor(root=tree)
    debouncer = Debouncer(min_repeat_seconds=900)
    await monitor.sample()

    stick = dict(vendor_id="0781", product_id="5567", product="Cruzer Blade",
                 serial="4C530001", device_class="00", interfaces=("08",))

    device(tree, "1-1.3", **stick)
    first = await monitor.sample()
    assert [debouncer.should_report(o) for o in first] == [True]

    unplug(tree, "1-1.3")
    cleared = await monitor.sample()
    assert [debouncer.should_report(o) for o in cleared] == [True]

    device(tree, "1-1.3", **stick)
    again = await monitor.sample()
    assert [debouncer.should_report(o) for o in again] == [True], (
        "a device plugged back in must be reported, not suppressed as a repeat"
    )


@sync
async def test_nothing_changing_reports_nothing(tree: Path):
    monitor = UsbMonitor(root=tree)
    await monitor.sample()
    assert await monitor.sample() == []
    assert await monitor.sample() == []


@sync
async def test_an_insertion_and_a_removal_in_one_tick_are_both_reported(tree: Path):
    monitor = UsbMonitor(root=tree)
    await monitor.sample()

    device(tree, "2-1", vendor_id="046d", product_id="c52b",
           manufacturer="Logitech", product="Unifying Receiver",
           device_class="00", interfaces=("03",))
    unplug(tree, "1-1.2")

    observations = await monitor.sample()
    kinds = sorted(o.kind for o in observations)
    assert kinds == ["usb.input.added", "usb.input.removed"]


@sync
async def test_storage_is_a_notice_and_a_mouse_is_not(tree: Path):
    """Severity is what decides whether the user hears this or reads it later.

    A stick that appeared has something to *do* about it -- look at what is on it
    -- where a mouse arriving is a fact. If both are ``info`` the scoring layer
    ranks them together, and the one worth a sentence gets logged instead of
    said.
    """
    monitor = UsbMonitor(root=tree)
    await monitor.sample()

    device(tree, "1-1.3", vendor_id="0781", product_id="5567",
           product="Cruzer Blade", device_class="00", interfaces=("08",))
    device(tree, "2-1", vendor_id="046d", product_id="c52b",
           product="Unifying Receiver", device_class="00", interfaces=("03",))

    by_kind = {o.kind: o for o in await monitor.sample()}
    assert sorted(by_kind) == ["usb.input.added", "usb.storage.added"]
    assert by_kind["usb.storage.added"].severity == "notice"
    assert by_kind["usb.input.added"].severity == "info"


@sync
async def test_two_devices_leaving_together_do_not_share_one_dedupe_key(tree: Path):
    """Two removals in one tick, and the debouncer has to see two conditions.

    The key names the *device*, not the monitor. One shared key would mean the
    second unplug is a repeat of the first -- suppressed -- and the fifteen-minute
    window then swallows every further plug and unplug on the machine, because
    they all look like the same ongoing situation.
    """
    monitor = UsbMonitor(root=tree)
    debouncer = Debouncer(min_repeat_seconds=900)
    await monitor.sample()

    device(tree, "1-1.3", vendor_id="0781", product_id="5567", product="Stick",
           serial="AAAA", device_class="00", interfaces=("08",))
    device(tree, "2-1", vendor_id="046d", product_id="c52b", product="Receiver",
           serial="BBBB", device_class="00", interfaces=("03",))
    inserted = await monitor.sample()
    assert len({o.dedupe_key for o in inserted}) == 2
    assert [debouncer.should_report(o) for o in inserted] == [True, True]

    unplug(tree, "1-1.3")
    unplug(tree, "2-1")
    removed = await monitor.sample()
    assert len({o.dedupe_key for o in removed}) == 2, (
        "two devices must be two conditions, or the second unplug is suppressed"
    )
    assert [debouncer.should_report(o) for o in removed] == [True, True]


@sync
async def test_stopping_drops_the_baseline_rather_than_replaying_the_gap(tree: Path):
    """A device plugged in while the monitor was off is not an event happening now.

    Keeping the baseline across a stop would report it with a timestamp saying
    "just now", which is a claim about when something happened that the monitor
    has no basis for.
    """
    monitor = UsbMonitor(root=tree)
    await monitor.sample()
    await monitor.on_stop()

    device(tree, "1-1.3", vendor_id="0781", product_id="5567", product="Stick",
           device_class="00", interfaces=("08",))
    assert await monitor.sample() == []
    # And the new baseline includes it, so the *next* change is still seen.
    unplug(tree, "1-1.3")
    assert len(await monitor.sample()) == 1


# --------------------------------------------------------------- availability


def test_a_kernel_with_no_usb_tree_says_so(tmp_path: Path):
    monitor = UsbMonitor(root=tmp_path / "absent")
    with pytest.raises(MonitorUnavailable) as raised:
        monitor.check_available()
    assert "USB tree" in str(raised.value)


def test_a_present_tree_is_available(tree: Path):
    UsbMonitor(root=tree).check_available()  # must not raise


def test_the_monitor_is_off_until_switched_on(tree: Path):
    assert UsbMonitor(root=tree).enabled() is False


def test_the_switch_is_a_real_setting_and_not_a_blank_one(tree: Path):
    """``enabled()`` returns False for a blank setting name too.

    Which is why the test above passes whether or not this monitor has a switch at
    all: an empty ``enabled_setting`` short-circuits to False in ``Monitor``. A
    monitor with no switch is one the user can never turn on -- the Awareness
    toggle would write ``monitor_usb_enabled`` and nothing would read it -- so the
    name has to be checked against the settings table that stores it, and turning
    it on has to actually turn it on.
    """
    monitor = UsbMonitor(root=tree)
    assert monitor.enabled_setting == "monitor_usb_enabled"
    assert monitor.interval_setting == "monitor_usb_interval_seconds"
    assert monitor.enabled_setting in app_settings.DEFAULTS
    assert app_settings.DEFAULTS[monitor.enabled_setting] is False
    assert monitor.interval_setting in app_settings.DEFAULTS

    app_settings.set(monitor.enabled_setting, True)
    try:
        assert monitor.enabled() is True
    finally:
        # ``reset`` rather than ``set(..., False)``: this removes the row so the
        # default applies again, leaving the settings table as it was found. A
        # stored False is the same value but a different state, and settings are
        # global for the whole session.
        app_settings.reset(monitor.enabled_setting)
    assert monitor.enabled() is False


def test_the_monitor_is_registered_so_it_can_actually_run(tree: Path):
    """A monitor nobody registered is a watcher that silently never runs.

    Everything else in this file exercises the class directly, which is exactly
    the shape that stays green when the supervisor has never heard of it.
    """
    registered = supervisor().get("usb")
    assert isinstance(registered, UsbMonitor)
    assert "usb" in [m["id"] for m in supervisor().status()["monitors"]]


def test_every_registered_monitor_asks_for_a_capability_that_exists():
    """A capability name with no capability behind it is a toggle that only fails.

    ``engine.evaluate`` denies an unknown name outright, so ``check_permission``
    raises ``MonitorUnavailable`` and the supervisor reports the monitor as
    unavailable the first time it is switched on -- with a reason that reads like
    a missing binary. This monitor shipped that way for one draft ("system.read",
    which does not exist), and no test could see it because permission checks only
    happen inside the supervisor's start path.

    Written across every monitor rather than this one: the mistake is a typo in a
    class attribute, and nothing about it is specific to USB.
    """
    for monitor in supervisor().status()["monitors"]:
        capability = monitor["capability"]
        assert capability, f"{monitor['id']} declares no capability"
        assert capabilities.exists(capability), (
            f"{monitor['id']} asks for {capability!r}, which is not a capability "
            "NEXUS has, so it can never start"
        )
        for key in (monitor["enabled_setting"], monitor["interval_setting"]):
            assert key in app_settings.DEFAULTS, (
                f"{monitor['id']} reads {key!r}, which has no default, so the "
                "Awareness screen cannot write it"
            )


# ----------------------------------------------------- against the real kernel


@pytest.mark.skipif(not SYSFS_USB.is_dir(), reason="no USB tree on this kernel")
def test_the_assumed_sysfs_layout_matches_this_kernel():
    """Everything above is a fixture. This is the part that can be wrong anyway.

    If the kernel stopped publishing ``idVendor``, or started naming interfaces
    without a colon, every test in this file would still pass and the monitor
    would report nothing forever. So: read the live tree, and require that it
    contains at least one hub (every machine has a root hub) and that every entry
    the scan accepts has the attributes the code reads.
    """
    entries = [p for p in SYSFS_USB.iterdir() if p.is_dir() and ":" not in p.name]
    assert entries, "a machine with USB support has at least one root hub entry"

    described = [read_device(p) for p in entries]
    real = [d for d in described if d is not None]
    assert real, "no entry had an idVendor -- the attribute names have changed"
    assert any(d.is_hub for d in real), "no root hub found; the class code changed"

    for found in scan().values():
        assert found.vendor_id and found.product_id
        assert ":" not in found.path
        assert found.identity
