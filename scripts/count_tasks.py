#!/usr/bin/env python3
"""Count the task markers in docs/TASKS.md, and fail loudly rather than quietly.

The status paragraph in TASKS.md says its counts are "by script, not estimated". That
script did not exist on disk until now -- it was retyped each time -- and on 2026-09-03
that cost something: one entry (`A5`) had its glyph glued to its blocker (`**A5 ⛔A2**`),
so every ad-hoc pattern anchored on ``**ID glyph**`` skipped it, and the header's entry
count had been one short for as long as the typo lived. A count is only as honest as its
ability to notice what it could not read, so this reports unmatched lines as an error
instead of leaving them out of the total.

    python3 scripts/count_tasks.py [--check]

``--check`` exits non-zero when the header's own numbers disagree with the glyphs, which
is the drift TASKS.md has recorded three times: a marker flipped without the total being
recounted.
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "docs" / "TASKS.md"

#: A task entry. The ID and the optional glyph must both sit inside the bold span --
#: anything else is a malformed entry, not an unmarked one.
ENTRY = re.compile(r"^- \*\*([A-N]\d+[a-z]?)(?: (\S))?\*\*")
#: A line that opens like an entry but does not parse as one. This is the check that
#: would have caught `**A5 ⛔A2**`; without it a malformed entry is simply invisible.
SUSPECT = re.compile(r"^- \*\*([A-N]\d+[a-z]?)\S")

GLYPHS = {"✔": "done", "◐": "partial", "⚑": "waiting on you", "⛔": "blocked"}


def main() -> int:
    lines = DOC.read_text(encoding="utf-8").splitlines()
    ids: list[str] = []
    glyphs: Counter[str | None] = Counter()
    blocked: list[str] = []
    malformed: list[tuple[int, str]] = []

    for number, line in enumerate(lines, 1):
        match = ENTRY.match(line)
        if match is None:
            if SUSPECT.match(line):
                malformed.append((number, line[:80]))
            continue
        ids.append(match.group(1))
        glyphs[match.group(2)] += 1
        if match.group(2) in ("⚑", "⛔"):
            blocked.append(f"{match.group(1)} {match.group(2)}")

    marked = sum(count for glyph, count in glyphs.items() if glyph)
    print(f"entries      {len(ids)}")
    for glyph, label in GLYPHS.items():
        print(f"  {glyph} {label:<14} {glyphs[glyph]}")
    print(f"  unmarked        {glyphs[None]}")
    print(f"marked       {marked}")
    print(f"blocked      {', '.join(blocked)}")

    problems: list[str] = []
    duplicates = sorted(i for i, n in Counter(ids).items() if n > 1)
    if duplicates:
        problems.append(f"duplicate ids: {', '.join(duplicates)}")
    for number, text in malformed:
        problems.append(f"line {number} opens like an entry but does not parse: {text}")

    if "--check" in sys.argv:
        header = "\n".join(lines[:12])
        claimed = re.search(
            r"\*\*(\d+)`?\*?\*? ?of the \*\*(\d+)\*\* task entries", header
        ) or re.search(r"(\d+) of the \*\*(\d+)\*\* task entries", header)
        if claimed is None:
            problems.append("could not find the header's own counts to compare against")
        else:
            if int(claimed.group(1)) != marked:
                problems.append(
                    f"header says {claimed.group(1)} marked, glyphs say {marked}"
                )
            if int(claimed.group(2)) != len(ids):
                problems.append(
                    f"header says {claimed.group(2)} entries, glyphs say {len(ids)}"
                )
        for glyph in ("✔", "◐"):
            found = re.search(rf"\*\*(\d+) `{glyph}`\*\*", header)
            if found and int(found.group(1)) != glyphs[glyph]:
                problems.append(
                    f"header says {found.group(1)} {glyph}, glyphs say {glyphs[glyph]}"
                )

    for problem in problems:
        print(f"DRIFT: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
