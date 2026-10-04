"""Break usb_monitor.py on purpose and confirm a test notices.

The fixture in this module's tests is a directory tree the test builds, which
means a test can be green because the fixture agrees with the code rather than
because the code is right. That already happened once here: the first fixture put
interface directories in the wrong place and four tests failed honestly. These
mutations are the other half -- proof that the tests can fail when the *code* is
what moves.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TARGET = Path("nexus/monitors/usb_monitor.py")
ORIGINAL = TARGET.read_text(encoding="utf-8")

#: Files other than the monitor that carry a behaviour these tests defend. A
#: monitor that works perfectly and was never registered, or whose settings have
#: no defaults, is a watcher that cannot run -- and that failure lives in
#: supervisor.py and app_settings.py, not here.
SUPERVISOR = Path("nexus/monitors/supervisor.py")
SETTINGS = Path("nexus/db/repos/app_settings.py")

MUTATIONS: list[tuple[str, str, str] | tuple[str, str, str, Path]] = [
    (
        "hubs are reported as devices",
        "        if device is None or device.is_hub:",
        "        if device is None:",
    ),
    (
        "interface directories count as devices",
        '        if ":" in entry.name or not entry.is_dir():',
        "        if not entry.is_dir():",
    ),
    (
        "device class is trusted even when it is 00",
        "        if self.device_class not in (0x00, 0xFF) or not self.interface_classes:",
        "        if True:",
    ),
    (
        "the interface fallback accepts a placeholder class",
        "            if candidate not in (0x00, 0xFF):",
        "            if True:",
    ),
    (
        "identity ignores the serial",
        "        if self.serial:\n            return f\"{base}:{self.serial}\"",
        "        if False:\n            return base",
    ),
    (
        "identity ignores the port for unserialised devices",
        '        return f"{base}@{self.path}"',
        "        return base",
    ),
    (
        "the first sample reports every attached device",
        "        if previous is None:",
        "        if False:",
    ),
    (
        "removals are not reported",
        "        for identity in sorted(set(previous) - set(current)):",
        "        for identity in sorted(()):",
    ),
    (
        "insertions are not reported",
        "        for identity in sorted(set(current) - set(previous)):",
        "        for identity in sorted(()):",
    ),
    (
        "a removal does not clear the insertion",
        "            clears=not attached,",
        "            clears=False,",
    ),
    (
        "every device shares one dedupe key",
        '            dedupe_key=f"usb:{device.identity}",',
        '            dedupe_key="usb",',
    ),
    (
        "stopping keeps the baseline and replays the gap",
        "        self._known = None\n\n    async def sample",
        "        pass\n\n    async def sample",
    ),
    (
        "a device with no vendor id is still a device",
        "    if not vendor_id or not product_id:\n        return None",
        "    if False:\n        return None",
    ),
    (
        "storage is not distinguished from any other device",
        "KIND_FOR_CLASS: dict[int, str] = {",
        # ``{} and {...}`` short-circuits to the empty dict. The first version of
        # this mutation wrote ``{} or {`` -- which evaluates to the *original*
        # dict, so it changed nothing and "survived" every possible test. A
        # mutation that does not mutate reads exactly like an untested behaviour.
        "KIND_FOR_CLASS: dict[int, str] = {} and {",
    ),
    (
        "an unnamed device is described as nothing useful",
        '        if self.vendor_id and self.product_id:\n            return f"{label} {self.vendor_id}:{self.product_id}"',
        "        if False:\n            return label",
    ),
    (
        "a named device is described by its class instead",
        "        if name:\n            return name",
        "        if False:\n            return name",
    ),
    (
        "a missing usb tree is reported as available",
        "        if not self.root.is_dir():",
        "        if False:",
    ),
    (
        "the monitor turns itself on",
        '    enabled_setting = "monitor_usb_enabled"',
        '    enabled_setting = ""',
    ),
    (
        "interfaces are looked for outside the device directory",
        'for interface in sorted(directory.glob(f"{directory.name}:*")):',
        'for interface in sorted(directory.parent.glob(f"{directory.name}:*")):',
    ),
    (
        "a storage insertion is only info",
        '            severity="notice" if storage else "info",',
        '            severity="info",',
    ),
    (
        "the monitor is never registered with the supervisor",
        "                UsbMonitor(),\n",
        "",
        SUPERVISOR,
    ),
    (
        "the enabled setting has no default, so nothing can write it",
        '    "monitor_usb_enabled": False,',
        '    "monitor_usb_enabled_typo": False,',
        SETTINGS,
    ),
    (
        "the monitor asks for a capability that does not exist",
        '    capability = "system.info"',
        '    capability = "system.read"',
    ),
]


def run() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_usb_monitor.py", "-q",
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
            # A replacement that changes nothing "survives" everything and reads
            # exactly like an untested behaviour. It happened once here: `{} or {`
            # evaluates to the original dict.
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
