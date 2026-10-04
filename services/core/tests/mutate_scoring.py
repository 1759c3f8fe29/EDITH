"""Break scoring.py on purpose, one edit at a time, and confirm a test notices.

A green suite proves the tests run. It does not prove they can fail. Every
mutation below removes a behaviour the tests claim to defend; a surviving mutant
is a test that is watching nothing.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TARGET = Path("nexus/core/scoring.py")
ORIGINAL = TARGET.read_text(encoding="utf-8")

MUTATIONS = [
    (
        "confidence stops multiplying",
        "return self.importance * self.confidence * self.relevance",
        "return self.importance * self.relevance",
    ),
    (
        "relevance stops multiplying",
        "return self.importance * self.confidence * self.relevance",
        "return self.importance * self.confidence",
    ),
    (
        "a monitor may raise its own confidence",
        "        if lowered < confidence:\n            confidence = lowered",
        "        if True:\n            confidence = lowered",
    ),
    (
        "rule lookup matches a bare prefix, not a segment",
        'if kind == prefix or kind.startswith(prefix + "."):',
        "if kind.startswith(prefix):",
    ),
    (
        "shortest rule prefix wins instead of longest",
        "if len(prefix) > best_length:",
        "if len(prefix) < best_length or best is None:",
    ),
    (
        "attention no longer holds speech",
        'if disposition == "notify" and not attention.interruptible():',
        "if False:",
    ),
    (
        "notification volume no longer raises the bar",
        'if disposition == "notify" and attention.recent_notifications >= 5 and weight < 0.6:',
        "if False:",
    ),
    (
        "speaking is not a reason to stay quiet",
        "return not self.speaking and self.activity not in (\"call\", \"watching\")",
        'return self.activity not in ("call", "watching")',
    ),
    (
        "a clear keeps its actionability",
        '        actionability = 0.0\n        reasons.append("condition cleared")',
        '        reasons.append("condition cleared")',
    ),
    (
        "a clear keeps its urgency",
        "        urgency = min(urgency, 0.25)",
        "        urgency = max(urgency, 0.25)",
    ),
    (
        "critical events no longer bypass attention",
        "critical = result.urgency >= CRITICAL_URGENCY and weight >= 0.5",
        "critical = False",
    ),
    (
        "off-topic events are not discounted",
        "OFF_TOPIC_RELEVANCE = 0.8",
        "OFF_TOPIC_RELEVANCE = 1.0",
    ),
    (
        "being busy no longer dulls background noise",
        "        relevance = MIN_RELEVANCE",
        "        relevance = 1.0",
    ),
    (
        "battery ignores the charger",
        "    if plugged is True:",
        "    if False:",
    ),
    (
        "battery urgency is flat",
        "        return 0.95, 0.95, reasons",
        "        return 0.45, 0.25, reasons",
    ),
    (
        "a nearly full disk reads as merely full",
        "        return 0.95, 0.90, reasons",
        "        return 0.78, 0.50, reasons",
    ),
    (
        "severity stops moving the score",
        'scale = SEVERITY_SCALE.get(observation.severity, 1.0)',
        "scale = 1.0",
    ),
    (
        "the rate gate drops what it suppresses",
        "            held.count += 1",
        "            held.count += 0",
    ),
    (
        "the rate gate has no cooldown",
        "if last is not None and not critical and moment - last < self.cooldown_seconds:",
        "if False:",
    ),
    (
        "critical events wait for the cooldown like everything else",
        "        critical = judgement.score.urgency >= CRITICAL_URGENCY",
        "        critical = False",
    ),
    (
        "the gate rate-limits quiet dispositions too",
        'if rank(judgement.disposition) < rank("notify"):\n            return None',
        "if False:\n            return None",
    ),
    (
        "held conditions are never released on their own",
        "        out: list[Aggregate] = []",
        "        return []\n        out: list[Aggregate] = []",
    ),
    (
        "prioritise ignores urgency",
        "            -j.score.urgency,",
        "",
    ),
    (
        "an unkeyed event gets no rate limit key",
        "return judgement.dedupe_key or judgement.kind",
        'return judgement.dedupe_key or ""',
    ),
]


def run() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_event_scoring.py", "-q",
         "--no-header", "-x", "-p", "no:randomly"],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout[-400:]


def main() -> int:
    survivors: list[str] = []
    for name, old, new in MUTATIONS:
        if ORIGINAL.count(old) != 1:
            print(f"SKIP  {name}: anchor matched {ORIGINAL.count(old)} times")
            survivors.append(f"{name} (anchor did not match exactly once)")
            continue
        TARGET.write_text(ORIGINAL.replace(old, new, 1), encoding="utf-8")
        code, tail = run()
        TARGET.write_text(ORIGINAL, encoding="utf-8")
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
