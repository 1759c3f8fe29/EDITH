"""Drive every registered tool against this real machine and report what happens.

Not a test suite -- an instrument. It exists because the clipboard write bug was
found by running tools for real rather than by reading them, and the lesson from
that was general: a tool can pass its own unit tests and still be broken on the
machine it is for.

Classification, applied per tool:

* **read** -- no side effect worth worrying about; always safe to call.
* **mutate** -- changes something. Called only when ``--mutate`` is passed, and
  each one is chosen because its effect is verifiable and reversible.
* **skip** -- would seize the user's keyboard, close their windows, delete files,
  change power state, or run a command. Never invoked from here.

The verdict for each call distinguishes the outcomes a lazy harness would merge:

* ``OK``       -- ok=True, and independently verified where a verifier exists.
* ``UNVERIFIED`` -- ok=True but verified=None. Not a failure: it is the tool
                    honestly saying it could not confirm the effect.
* ``REFUSED``  -- ok=False with a reason. Often the *correct* answer on a machine
                  missing a binary, and the reason is printed so it can be judged.
* ``RAISED``   -- an exception escaped the handler. Always a bug.

Usage:
    python tools_sweep.py              # read-only tools
    python tools_sweep.py --mutate     # plus the reversible mutating ones
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from typing import Any

sys.path.insert(0, "/home/santosh/EDITH/services/core")

from nexus.tools import registry  # noqa: E402
import nexus.tools  # noqa: E402,F401  -- registers everything

#: Tools safe to call for real, with parameters that do something meaningful.
READ_ONLY: dict[str, dict[str, Any]] = {
    "system.status": {},
    "system.cpu": {},
    "system.memory": {},
    "system.disk": {},
    "system.temperature": {},
    "system.uptime": {},
    "system.datetime": {},
    "system.locale": {},
    "system.mounts": {},
    "system.users": {},
    "system.services": {"limit": 5},
    "system.processes": {"limit": 5},
    "system.process_tree": {"pid": 1},
    "system.env": {"limit": 5},
    "system.cron_list": {},
    "system.packages_installed": {"limit": 5},
    "system.package_search": {"query": "xclip", "limit": 3},
    "clipboard.get": {},
    "window.workspaces": {},
    "files.list": {"path": "~"},
    "files.info": {"path": "~/.bashrc"},
    "files.search": {"query": "nexus", "path": "~/.config", "limit": 3},
    "memory.recall": {"query": "test"},
    "automation.list": {},
    "net.fetch": {"url": "https://example.com"},
    "screen.capture": {"target": "screen"},
    "media.status": {},
    "system.volume": {},
    "system.brightness": {},
    "system.network": {},
    "system.battery": {},
    "system.gpu": {},
    "process.list": {},
    "apps.installed": {"limit": 5},
    "apps.windows": {},
    "browser.search": {"query": "nexus ai"},
    "system.process_by_port": {"port": 22},
}

#: Real calls with a real effect, each chosen because it is verifiable and can be
#: undone. Run only with --mutate.
MUTATING: dict[str, dict[str, Any]] = {
    "clipboard.set": {"text": "nexus sweep probe"},
    "system.notify": {"title": "NEXUS sweep", "message": "Real-world tool check"},
    "automation.create": {
        "name": "sweep probe",
        "trigger_kind": "manual",
        "message": "sweep",
    },
    "files.write": {"path": "/tmp/nexus_sweep_probe.txt", "content": "sweep"},
    "process.suspend_process": {"pid": 0},  # deliberately invalid: tests the guard
    "system.nice": {"pid": 0, "priority": 10},  # deliberately invalid: tests the guard
}

#: Never called from here, with the reason. Printed so the gaps are visible rather
#: than silently absent.
NEVER: dict[str, str] = {
    "input.type": "would seize the user's keyboard",
    "input.key": "would send real keystrokes to whatever has focus",
    "input.click": "would move the user's pointer and click",
    "input.scroll": "would scroll whatever window is under the pointer",
    "input.pointer_position": "moves the pointer as a side effect of reading it",
    "apps.close": "would close one of the user's windows",
    "apps.open": "would launch a real application",
    "apps.focus": "would steal focus from what the user is doing",
    "apps.quit": "would close a real application",
    "terminal.run": "would execute a command",
    "terminal.cancel": "would kill a running command",
    "files.delete": "would delete a real file",
    "files.move": "would move a real file",
    "system.kill_process": "would kill a real process",
    "system.power": "would lock, suspend, reboot or shut down the machine",
    "window.arrange": "would rearrange the user's windows",
    "screen.look": "sends the screen to a vision model (run separately by choice)",
    "screen.locate": "sends the screen to a vision model",
    "system.resume_process": "would resume a suspended process",
    "system.service_control": "would change a system service",
}


def call(name: str, params: dict[str, Any]) -> tuple[str, str, str]:
    """Run one tool. Returns (verdict, detail, extra)."""
    tool = registry.get(name)
    if tool is None:
        return "MISSING", "no such tool is registered", ""
    try:
        result = tool.handler(params)
    except Exception as exc:  # noqa: BLE001 - that is the point of the sweep
        tb = traceback.format_exc().strip().splitlines()[-3:]
        return "RAISED", f"{type(exc).__name__}: {exc}", " / ".join(tb)

    blob = ""
    if result.data is not None:
        try:
            blob = json.dumps(result.data, default=str)
        except Exception:  # noqa: BLE001
            blob = str(result.data)
    blob = blob[:150]

    if not result.ok:
        return "REFUSED", (result.error or "refused with no reason given")[:160], blob
    if result.verified is True:
        return "OK", (result.verification or result.summary or "")[:120], blob
    if result.verified is False:
        return "FAILED-VERIFY", (result.verification or "")[:140], blob
    return "UNVERIFIED", (result.summary or "")[:120], blob


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutate", action="store_true", help="also run mutating tools")
    parser.add_argument("--only", default="", help="comma-separated tool names")
    args = parser.parse_args()

    plan = dict(READ_ONLY)
    if args.mutate:
        plan.update(MUTATING)
    if args.only:
        wanted = {n.strip() for n in args.only.split(",") if n.strip()}
        plan = {k: v for k, v in plan.items() if k in wanted}

    counts: dict[str, int] = {}
    findings: list[tuple[str, str, str, str]] = []

    for name in sorted(plan):
        verdict, detail, extra = call(name, plan[name])
        counts[verdict] = counts.get(verdict, 0) + 1
        print(f"{verdict:<14} {name:<28} {detail}")
        if verdict in ("RAISED", "FAILED-VERIFY", "MISSING"):
            findings.append((verdict, name, detail, extra))

    print()
    registered = {t.name for t in registry.all_tools()}
    never = sorted(registered - {t.name for t in registry.all_tools()} | (set(NEVER) & registered))
    print(f"exercised {len(plan)} tools: {counts}")
    print(f"not exercised by design: {len(never)}")

    if findings:
        print("\n=== FINDINGS (need a look) ===")
        for verdict, name, detail, extra in findings:
            print(f"{verdict} {name}: {detail}")
            if extra:
                print(f"    {extra}")
        return 1
    print("\nno exceptions escaped any handler")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
