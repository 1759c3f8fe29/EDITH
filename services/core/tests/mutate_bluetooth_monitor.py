"""Break bluetooth_monitor.py on purpose and confirm a test notices.

Every fixture in this module's tests is recorded bytes -- a ``busctl`` reply
captured from this machine and an rfkill tree the test builds. That is what makes
it testable without a paired headset, and it is also what makes a green suite
weaker evidence than usual: a test can pass because the fixture agrees with the
code rather than because the code is right. These mutations are the other half of
the argument.

Two guards, both learned from the USB run:

* the anchor must match **exactly once**, so a mutation cannot silently apply to
  the wrong line or to nothing;
* the mutant must **differ** from the original. ``{} or {...}`` evaluates to the
  original dict, so that "mutation" survived every possible test while changing
  nothing -- and a mutation that does not mutate reads exactly like an untested
  behaviour.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TARGET = Path("nexus/monitors/bluetooth_monitor.py")

#: Files other than the monitor that carry a behaviour these tests defend. A
#: monitor that parses perfectly and was never registered, or whose settings have
#: no defaults, is a watcher that cannot run -- and that failure lives in
#: supervisor.py and app_settings.py, not here.
SUPERVISOR = Path("nexus/monitors/supervisor.py")
SETTINGS = Path("nexus/db/repos/app_settings.py")

MUTATIONS: list[tuple[str, str, str] | tuple[str, str, str, Path]] = [
    # -- the radio ---------------------------------------------------------
    (
        "every rfkill switch is read, not only the Bluetooth ones",
        '        if _attribute(entry / "type") != "bluetooth":\n            continue',
        "        if False:\n            continue",
    ),
    (
        "a soft block and a hard block read the same",
        "        if self.hard_blocked:\n            return "
        '"the Bluetooth radio is blocked by a hardware switch"',
        '        if False:\n            return "the Bluetooth radio is blocked by a hardware switch"',
    ),
    (
        "a hard block alone is not counted as blocked",
        "        return self.soft_blocked or self.hard_blocked",
        "        return self.soft_blocked",
    ),
    (
        "a missing rfkill directory raises instead of reading empty",
        "    except OSError:\n        return radios",
        "    except ZeroDivisionError:\n        return radios",
    ),
    (
        "one blocked radio of several means Bluetooth is off",
        "    return bool(states) and all(radio.blocked for radio in states)",
        "    return bool(states) and any(radio.blocked for radio in states)",
    ),
    (
        "a machine with no switches reports its radio as blocked",
        "    return bool(states) and all(radio.blocked for radio in states)",
        "    return all(radio.blocked for radio in states)",
    ),
    # -- the parser --------------------------------------------------------
    (
        "malformed json is an empty bus rather than an unknown one",
        "    except (json.JSONDecodeError, TypeError):\n        snapshot.ok = False",
        "    except (json.JSONDecodeError, TypeError):\n        snapshot.ok = True",
    ),
    (
        "a reply of the wrong shape is an empty bus rather than an unknown one",
        "    if not isinstance(objects, dict):\n        snapshot.ok = False",
        "    if not isinstance(objects, dict):\n        snapshot.ok = True",
    ),
    (
        "a failed busctl call is an empty bus rather than an unknown one",
        "        return Snapshot(ok=False)",
        "        return Snapshot(ok=True)",
    ),
    (
        "the variant wrapper is not unwrapped",
        '    if isinstance(value, dict) and "data" in value and "type" in value:\n'
        '        return value["data"]',
        '    if False:\n        return value["data"]',
    ),
    (
        "paired-but-absent devices are reported as connected",
        "        if device.connected:\n            snapshot.connected[device.identity] = device",
        "        if True:\n            snapshot.connected[device.identity] = device",
    ),
    (
        "an interface map of the wrong type raises instead of being skipped",
        "        if not isinstance(interfaces, dict):\n            continue",
        "        if False:\n            continue",
    ),
    (
        "a property bag of the wrong type raises instead of being skipped",
        "        if not isinstance(properties, dict):\n            continue",
        "        if False:\n            continue",
    ),
    (
        "an unreadable class drops the device instead of being treated as absent",
        "    except (TypeError, ValueError):\n        device_class = 0",
        "    except ZeroDivisionError:\n        device_class = 0",
    ),
    # -- identity and role -------------------------------------------------
    (
        "the object path is the identity, so one headset on two adapters is two devices",
        "        return self.address or self.path",
        "        return self.path",
    ),
    (
        "a device with no address collides with every other such device",
        "        return self.address or self.path",
        "        return self.address",
    ),
    (
        "the major class nibble is read from the wrong bits",
        "        major = (self.device_class >> 8) & 0x1F",
        "        major = self.device_class & 0x1F",
    ),
    (
        "the class is trusted over the icon BlueZ already worked out",
        "        role = ICON_ROLES.get(self.icon)\n        if role:\n            return role",
        "        role = ICON_ROLES.get(self.icon)\n        if False:\n            return role",
    ),
    (
        "audio and input do not get their own observation kinds",
        "KIND_FOR_ROLE: dict[str, str] = {",
        # ``{} and {...}`` short-circuits to the empty dict. ``{} or {...}`` would
        # evaluate to the original and change nothing at all.
        "KIND_FOR_ROLE: dict[str, str] = {} and {",
    ),
    (
        "the address BlueZ writes into Alias is accepted as a name",
        "    if alias and _is_address(alias, address):\n        alias = \"\"",
        '    if False:\n        alias = ""',
    ),
    (
        "the alias is only compared with the punctuation it does not use",
        '    flatten = str.maketrans("", "", ":-_")',
        '    flatten = str.maketrans("", "", ":")',
    ),
    (
        "the advertised name wins over the name the user chose",
        '    return alias or str(_variant(properties.get("Name")) or "")',
        '    return str(_variant(properties.get("Name")) or "") or alias',
    ),
    (
        "a device with no name is described as nothing useful",
        '        if self.address:\n            return f"{self.role} {self.address}"',
        "        if False:\n            return self.role",
    ),
    (
        "an absent battery reads as zero percent",
        "    except (TypeError, ValueError):\n        return None",
        "    except (TypeError, ValueError):\n        return 0",
    ),
    (
        "a device with no battery service reports zero percent",
        "    if not isinstance(battery, dict):\n        return None",
        "    if not isinstance(battery, dict):\n        return 0",
    ),
    # -- sampling ----------------------------------------------------------
    (
        "the first sample announces everything already connected",
        "        if previous is None:",
        "        if False:",
    ),
    (
        "a bus that did not answer is diffed anyway",
        "        if not snapshot.ok:",
        "        if False:",
    ),
    (
        "connections are not reported",
        "        for identity in sorted(set(snapshot.connected) - set(previous)):",
        "        for identity in sorted(()):",
    ),
    (
        "disconnections are not reported",
        "        for identity in sorted(set(previous) - set(snapshot.connected)):",
        "        for identity in sorted(()):",
    ),
    (
        "a disconnect does not clear its own connect",
        "            dedupe_key=f\"bluetooth:{device.identity}\",\n            clears=not connected,",
        '            dedupe_key=f"bluetooth:{device.identity}",\n            clears=False,',
    ),
    (
        "every device shares one dedupe key",
        '            dedupe_key=f"bluetooth:{device.identity}",',
        '            dedupe_key="bluetooth",',
    ),
    (
        "the event payload reports a disconnected device as connected",
        '            data={**device.to_dict(), "connected": connected},',
        '            data={"connected": connected, **device.to_dict()},',
    ),
    (
        "audio and input are as quiet as anything else",
        '            severity="notice" if loud else "info",',
        '            severity="info",',
    ),
    (
        "input is treated as an ordinary device",
        '        loud = device.role in ("audio", "input")',
        '        loud = device.role == "audio"',
    ),
    (
        "stopping keeps the baseline and replays the gap",
        "        self._known = None\n        self._radio_blocked = None",
        "        self._radio_blocked = None",
    ),
    # -- radio events ------------------------------------------------------
    (
        "the radio being off is reported on every tick",
        "        if radios and self._radio_blocked is not None and blocked != self._radio_blocked:",
        "        if radios and self._radio_blocked is not None:",
    ),
    (
        "the first sample announces the radio state it found",
        "        if radios and self._radio_blocked is not None and blocked != self._radio_blocked:",
        "        if radios and blocked != self._radio_blocked:",
    ),
    (
        "the radio coming back does not clear the block",
        "            clears=not blocked,",
        "            clears=False,",
    ),
    (
        "a machine with no rfkill has its radio state remembered anyway",
        "        if radios:\n            self._radio_blocked = blocked",
        "        self._radio_blocked = blocked",
    ),
    (
        "a blocked radio stops the device diff for that tick",
        "        snapshot = scan(self._runner)\n        if not snapshot.ok:",
        "        if blocked:\n            return observations\n        snapshot = scan(self._runner)\n        if not snapshot.ok:",
    ),
    # -- availability and wiring -------------------------------------------
    (
        "a machine with no adapter and no switch is reported as watchable",
        '        raise MonitorUnavailable(\n            "This machine has no Bluetooth adapter',
        '        return None\n        raise MonitorUnavailable(\n            "This machine has no Bluetooth adapter',
    ),
    (
        "a blocked radio makes the monitor unavailable, so nothing watches for it coming back",
        "        radios = read_radios(self.rfkill_root)\n        if radios:\n            return",
        "        radios = read_radios(self.rfkill_root)\n        if radios and not radios_blocked(radios):\n            return",
    ),
    (
        "the monitor turns itself on",
        '    enabled_setting = "monitor_bluetooth_enabled"',
        '    enabled_setting = ""',
    ),
    (
        "the monitor asks for a capability that does not exist",
        '    capability = "system.info"',
        '    capability = "system.read"',
    ),
    (
        "the monitor is never registered with the supervisor",
        "                BluetoothMonitor(),\n",
        "",
        SUPERVISOR,
    ),
    (
        "the enabled setting has no default, so nothing can write it",
        '    "monitor_bluetooth_enabled": False,',
        '    "monitor_bluetooth_enabled_typo": False,',
        SETTINGS,
    ),
    (
        "the interval setting has no default",
        '    "monitor_bluetooth_interval_seconds": 10,',
        '    "monitor_bluetooth_interval_seconds_typo": 10,',
        SETTINGS,
    ),
]


def run() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_bluetooth_monitor.py", "-q",
         "--no-header", "-x", "-p", "no:randomly"],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout[-400:]


def main() -> int:
    survivors: list[str] = []
    sources = {path: path.read_text(encoding="utf-8") for path in (TARGET, SUPERVISOR, SETTINGS)}
    for mutation in MUTATIONS:
        name, old, new = mutation[0], mutation[1], mutation[2]
        path = mutation[3] if len(mutation) > 3 else TARGET  # type: ignore[misc]
        original = sources[path]
        if original.count(old) != 1:
            print(f"SKIP  {name}: anchor matched {original.count(old)} times")
            survivors.append(f"{name} (anchor did not match exactly once)")
            continue
        mutant = original.replace(old, new, 1)
        if mutant == original:
            print(f"SKIP  {name}: the replacement is identical to the original")
            survivors.append(f"{name} (the mutation changed nothing)")
            continue
        path.write_text(mutant, encoding="utf-8")
        code, tail = run()
        path.write_text(original, encoding="utf-8")
        if code == 0:
            print(f"SURVIVED  {name}")
            survivors.append(name)
        else:
            first = [l for l in tail.splitlines() if l.startswith("FAILED")]
            print(f"killed    {name}  <- {first[0] if first else 'failed'}")
    print()
    if survivors:
        print(f"{len(survivors)} of {len(MUTATIONS)} survived:")
        for s in survivors:
            print(f"  - {s}")
        return 1
    print(f"all {len(MUTATIONS)} mutations killed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
