"""Mutation tests for the world model (C2): every honesty rule pinned.

The mutation pattern is the one established by mutate_usb_monitor.py: apply a
single, *semantically meaningful* change to the module under test, run the
world tests, and expect them to fail. A mutation that survives means a rule
this module exists to enforce is not actually enforced.

Every mutation below targets a rule, not a line -- "unobserved domains are
filled with defaults", "stale facts are dropped", "redaction only covers the
top level", "the subscriber dies on the first bad event". Each is the
plausible-looking shortcut a reviewer would not blink at, which is exactly
why each needs a test that blinks.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).parent
TARGET = TESTS.parent / "nexus" / "core" / "world.py"
CONTEXT = TESTS.parent / "nexus" / "ai" / "context.py"

#: The semantically distinct honesty rules, each as one mutation. A mutation
#: is (name, old, new[, file]) -- ``old`` must match the source exactly once.
MUTATIONS: list[tuple[str, str, str] | tuple[str, str, str, Path]] = [
    # ---------------------------------------------------------- honest empties
    (
        "unobserved domains are guessed at",
        "        latest = None\n        best_key = \"\"",
        "        latest = {\"summary\": \"unknown\", \"data\": {}, \"observed_at\": None,\n"
        "                  \"received_at\": None, \"severity\": None, \"clears\": None,\n"
        "                  \"kind\": None, \"stale\": False}\n        best_key = \"\"",
    ),
    (
        "the empty world claims something was seen",
        "        lines.append(\"- No monitor has reported any state yet.\")",
        "        lines.append(\"- All systems nominal.\")",
        CONTEXT,
    ),
    # --------------------------------------------------------------- staleness
    (
        "stale facts are dropped instead of marked",
        "    return (now - observed).total_seconds() > STALE_AFTER_SECONDS",
        "    return False",
    ),
    (
        "the staleness boundary is inclusive",
        "    return (now - observed).total_seconds() > STALE_AFTER_SECONDS",
        "    return (now - observed).total_seconds() >= STALE_AFTER_SECONDS",
    ),
    (
        "a naive timestamp is guessed as UTC",
        "    if parsed.tzinfo is None:\n        return None\n    return parsed",
        "    if parsed.tzinfo is None:\n        parsed = parsed.replace(tzinfo=timezone.utc)\n    return parsed",
    ),
    # ------------------------------------------------------------ boundedness
    (
        "the per-kind history is unbounded",
        "        history = deque(maxlen=KEEP_PER_KIND)",
        "        history = deque()",
    ),
    (
        "the kind count is not capped",
        "        if len(_state.facts) >= MAX_KINDS:",
        "        if False:",
    ),
    # ------------------------------------------------------------ containment
    (
        "a handler crash kills the subscriber",
        "                _remember_failure(exc)\n    finally:",
        "                _remember_failure(exc)\n                raise\n    finally:",
    ),
    (
        "a crash does not mark the state degraded",
        "    _state.degraded = True\n    _state.error = f\"{type(exc).__name__}: {exc}\"",
        "    pass",
    ),
    (
        "the handshake event is treated as an observation",
        "            if not event.type.startswith(\"observation.\"):\n                continue",
        "            if False:\n                continue",
    ),
    # --------------------------------------------------------------- redaction
    (
        "titles are passed through to the agent",
        "        elif key in _SENSITIVE_FIELDS and item not in (None, \"\"):\n            out[key] = \"[redacted]\"",
        "        elif key in _SENSITIVE_FIELDS and False:\n            out[key] = \"[redacted]\"",
    ),
    (
        "clipboard content is redacted rather than dropped",
        "        if key in _DROPPED_FIELDS:\n            continue",
        "        if False:\n            continue",
    ),
    (
        "the title-leaking summary is passed through",
        "    (\"window\", \".title\", \"the focused window's title changed (details withheld)\"),",
        "    (\"window\", \".title-never-matches\", \"the focused window's title changed (details withheld)\"),",
    ),
    (
        "the clipboard-preview summary is passed through",
        "    (\"clipboard\", \".copy\", \"something was copied (content withheld)\"),",
        "    (\"clipboard\", \".copy-never-matches\", \"something was copied (content withheld)\"),",
    ),
    (
        "device-name summaries are passed through",
        "    (\"usb\", \"\", \"a USB device was attached or removed\"),",
        "    (\"usb\", \"never-matches\", \"a USB device was attached or removed\"),",
    ),
    # ------------------------------------------------------------- the block
    (
        "stale facts lose their caveat in the agent's view",
        '        caveat = " (stale reading)" if fact.get("stale") else ""',
        '        caveat = ""',
        CONTEXT,
    ),
    (
        "the agent is not told what is unknown",
        "    trailer = (\n        \"This is what the ambient monitors observed. Anything not listed is \"\n        \"unknown, not known-to-be-fine -- check with a tool before stating it.\"\n    )",
        "    trailer = (\n        \"This is what the ambient monitors observed.\"\n    )",
        CONTEXT,
    ),
    (
        "a degraded world model reads as a complete one",
        "    if state.get(\"degraded\"):",
        "    if False:",
        CONTEXT,
    ),
]


def run() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_world_state.py", "-q",
         "--no-header", "-x", "-p", "no:randomly"],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout[-400:]


def main() -> int:
    survivors: list[str] = []
    sources = {TARGET: TARGET.read_text(encoding="utf-8"),
               CONTEXT: CONTEXT.read_text(encoding="utf-8")}
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
