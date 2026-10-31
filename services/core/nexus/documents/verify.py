"""Proving a document is what it claims to be, by opening it again (spec §60).

A writer that reports success has told you what it intended. This module reopens
the file from disk, parses it, and counts what is really in it -- which is the
only way any claim about a written file becomes evidence rather than an echo.

Four things get checked, and they are not the same thing:

* **The parts are all there.** A DOCX missing ``word/document.xml`` is still a
  valid zip, and ``zipfile`` will open it happily. Every part a reader would
  demand is listed per format and required.
* **Every part parses.** A part that is present but not well-formed XML is worse
  than a missing one, because it looks fine until a real application refuses the
  whole file. Parsing each part is what catches it.
* **The relationships resolve.** A part with no rel pointing at it is not part of
  the document as far as any reader is concerned: the file opens and the content
  is simply absent. So the rels are parsed and their targets checked to exist.
* **The content count matches.** Paragraphs, rows, slides and pages are counted
  from the markup and compared with what the writer said it wrote. This is the
  check that fails when a writer silently drops half a document.

For PDF the shape is different because the file is not a container: the check is
that the ``xref`` table's byte offsets each land exactly on the object they claim
to point at. That is the one thing about a hand-written PDF that is easy to get
subtly wrong and impossible to notice by looking.

**The negative case is part of the design.** A verifier that only ever sees files
its own writer produced cannot fail, and a check that cannot fail proves nothing.
:func:`verify_document` is therefore exercised against deliberately truncated and
corrupted files, and is expected to reject them.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
SS_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

#: What a reader will insist on before it will open the file at all.
REQUIRED_PARTS: dict[str, tuple[str, ...]] = {
    "docx": (
        "[Content_Types].xml",
        "_rels/.rels",
        "word/document.xml",
        "word/_rels/document.xml.rels",
        "word/styles.xml",
    ),
    "xlsx": (
        "[Content_Types].xml",
        "_rels/.rels",
        "xl/workbook.xml",
        "xl/_rels/workbook.xml.rels",
        "xl/worksheets/sheet1.xml",
    ),
    "pptx": (
        "[Content_Types].xml",
        "_rels/.rels",
        "ppt/presentation.xml",
        "ppt/_rels/presentation.xml.rels",
        "ppt/slideMasters/slideMaster1.xml",
        "ppt/slideLayouts/slideLayout1.xml",
        "ppt/theme/theme1.xml",
    ),
}

KIND_BY_SUFFIX = {".docx": "docx", ".xlsx": "xlsx", ".pptx": "pptx", ".pdf": "pdf"}


class VerifyError(RuntimeError):
    """The file is not what it claims to be, and here is exactly why."""


def _tag(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}"


def _read_zip(data: bytes) -> zipfile.ZipFile:
    """Open the archive and prove every part parses before trusting any of it."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise VerifyError(f"This is not a readable zip archive ({exc}).") from None
    names = set(archive.namelist())
    bad = archive.testzip()
    if bad is not None:
        raise VerifyError(f"The archive is corrupt: {bad} failed its checksum.")
    if not names:
        raise VerifyError("The archive is empty.")
    return archive


def _parse_all_xml(archive: zipfile.ZipFile) -> list[str]:
    """Parse every .xml and .rels part. Returns the names that parsed."""
    parsed: list[str] = []
    for name in archive.namelist():
        if not (name.endswith(".xml") or name.endswith(".rels")):
            continue
        try:
            ElementTree.fromstring(archive.read(name))
        except ElementTree.ParseError as exc:
            raise VerifyError(f"The part {name} is not well-formed XML: {exc}.") from None
        parsed.append(name)
    if not parsed:
        raise VerifyError("The archive contains no XML parts at all.")
    return parsed


def _check_required(kind: str, names: set[str]) -> None:
    missing = [p for p in REQUIRED_PARTS[kind] if p not in names]
    if missing:
        raise VerifyError(
            f"A {kind.upper()} reader would refuse this file: it is missing "
            f"{', '.join(missing)}."
        )


def _check_relationships(archive: zipfile.ZipFile) -> None:
    """Every rel target must exist inside the archive.

    This is the failure that looks like a writer bug and is not: the content is
    in the zip and nothing points at it, so the document opens empty.
    """
    for name in archive.namelist():
        if not name.endswith(".rels"):
            continue
        base = name.rsplit("_rels/", 1)[0]
        root = ElementTree.fromstring(archive.read(name))
        for rel in root:
            target = rel.get("Target") or ""
            if rel.get("TargetMode") == "External" or target.startswith(("http:", "https:")):
                continue
            # "../theme/theme1.xml" from "ppt/slideMasters/..." resolves inside
            # the archive; the rel path is relative to the part's own folder.
            resolved = (base + target).replace("//", "/")
            parts: list[str] = []
            for segment in resolved.split("/"):
                if segment == "..":
                    if parts:
                        parts.pop()
                elif segment not in ("", "."):
                    parts.append(segment)
            if "/".join(parts) not in set(archive.namelist()):
                raise VerifyError(
                    f"{name} points at {target!r}, which is not in the file. That content "
                    "would be missing when the document is opened."
                )


def _count_docx(archive: zipfile.ZipFile) -> dict[str, Any]:
    root = ElementTree.fromstring(archive.read("word/document.xml"))
    body = root.find(_tag(W_NS, "body"))
    if body is None:
        raise VerifyError("word/document.xml has no <w:body>.")

    counters = {"paragraphs": 0, "list_items": 0, "headings": 0, "code_blocks": 0,
                "tables": 0, "characters": 0}

    def walk(parent: ElementTree.Element) -> None:
        for child in parent:
            if child.tag == _tag(W_NS, "tbl"):
                # A table is one thing. Every cell holds a paragraph because Word
                # requires one, and counting those as body paragraphs makes a
                # three-row table report a dozen paragraphs that are not there.
                # The subtree is counted as a table and not descended into.
                counters["tables"] += 1
                continue
            if child.tag == _tag(W_NS, "p"):
                texts = child.findall(f".//{_tag(W_NS, 't')}")
                content = "".join(t.text or "" for t in texts).strip()
                if not content:
                    continue
                counters["characters"] += len(content)
                props = child.find(_tag(W_NS, "pPr"))
                style = None
                numbered = False
                if props is not None:
                    node = props.find(_tag(W_NS, "pStyle"))
                    if node is not None:
                        style = node.get(_tag(W_NS, "val"))
                    numbered = props.find(_tag(W_NS, "numPr")) is not None
                if style and style.lower().startswith("heading"):
                    counters["headings"] += 1
                elif style and style.lower() == "code":
                    # The writer puts every line of a code block in one
                    # Code-styled paragraph, so this counts blocks, which is what
                    # the claim counts. Counting it as prose instead is how a
                    # two-line snippet first made the verifier disagree with a
                    # writer that had written exactly what it said it had.
                    counters["code_blocks"] += 1
                elif numbered:
                    counters["list_items"] += 1
                else:
                    counters["paragraphs"] += 1
                continue
            walk(child)

    walk(body)
    return {**counters, "parts": len(archive.namelist())}


def _count_xlsx(archive: zipfile.ZipFile) -> dict[str, Any]:
    root = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    rows = 0
    cells = 0
    filled = 0
    for element in root.iter():
        if element.tag == _tag(SS_NS, "row"):
            rows += 1
            if list(element):
                filled += 1
        elif element.tag == _tag(SS_NS, "c"):
            cells += 1
    return {
        "rows": rows,
        "filled_rows": filled,
        "cells": cells,
        "parts": len(archive.namelist()),
    }


def _count_pptx(archive: zipfile.ZipFile) -> dict[str, Any]:
    names = archive.namelist()
    slides = sorted(n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
    if not slides:
        raise VerifyError("The presentation contains no slides.")
    titles: list[str] = []
    for name in slides:
        root = ElementTree.fromstring(archive.read(name))
        texts = [
            (element.text or "").strip()
            for element in root.iter(_tag(A_NS, "t"))
        ]
        titles.append(next((t for t in texts if t), ""))
    # The presentation must actually reference the slides it contains.
    presentation = ElementTree.fromstring(archive.read("ppt/presentation.xml"))
    listed = len(list(presentation.iter(_tag(P_NS, "sldId"))))
    if listed != len(slides):
        raise VerifyError(
            f"The presentation lists {listed} slide(s) but the file contains {len(slides)}."
        )
    return {
        "slides": len(slides),
        "slide_titles": titles,
        "parts": len(names),
    }


def _verify_pdf(data: bytes) -> tuple[dict[str, Any], str]:
    """Parse the xref and prove every offset lands on the object it names."""
    if not data.startswith(b"%PDF-"):
        raise VerifyError("The file does not begin with a PDF header.")
    if b"%%EOF" not in data[-1024:]:
        raise VerifyError("The file has no %%EOF marker, so it was truncated.")

    marker = data.rfind(b"startxref")
    if marker == -1:
        raise VerifyError("There is no startxref, so the cross-reference table cannot be found.")
    tail = data[marker + len("startxref"):].split(b"%%EOF")[0].strip()
    first_line = tail.split(b"\n")[0].strip()
    try:
        xref_at = int(first_line)
    except ValueError:
        raise VerifyError(f"The startxref offset {first_line!r} is not a number.") from None
    if xref_at <= 0 or xref_at >= len(data):
        raise VerifyError(f"The startxref offset {xref_at} is outside the file.")

    if not data[xref_at:].startswith(b"xref"):
        raise VerifyError(
            f"The startxref offset {xref_at} does not point at an xref table; "
            f"it points at {data[xref_at:xref_at + 20]!r}."
        )

    header = data[xref_at:].split(b"\n", 2)
    counts = header[1].split()
    if len(counts) != 2:
        raise VerifyError("The xref subsection header is malformed.")
    start, total = int(counts[0]), int(counts[1])
    if start != 0:
        raise VerifyError(f"The xref table starts at object {start}, not 0.")

    entries = data[xref_at + len(header[0]) + len(header[1]) + 2:].split(b"\n")
    checked = 0
    for number in range(1, total):
        # Entry 0 is the free head of the list, so object N's entry is at index N.
        # Reading index N-1 instead makes object 1 look free and every subsequent
        # check compare the wrong object against the wrong offset -- which is what
        # happened the first time this ran.
        if number >= len(entries):
            raise VerifyError(f"The xref table ends before entry {number}.")
        entry = entries[number]
        fields = entry.split()
        if len(fields) < 3:
            raise VerifyError(f"xref entry {number} is malformed: {entry!r}.")
        if fields[2] != b"n":
            raise VerifyError(f"Object {number} is marked free, but the document references it.")
        offset = int(fields[0])
        if offset <= 0 or offset >= len(data):
            raise VerifyError(f"Object {number} claims to start at byte {offset}, outside the file.")
        expected = f"{number} 0 obj".encode("ascii")
        if not data[offset:].startswith(expected):
            raise VerifyError(
                f"The offset for object {number} points at {data[offset:offset + 20]!r}, "
                f"not at {expected!r}. The cross-reference table is wrong and readers "
                "will either refuse the file or show the wrong content."
            )
        checked += 1

    # The page tree has to agree with the pages that are actually in the file.
    pages_present = len(re.findall(rb"/Type\s*/Page[^s]", data))
    count_match = re.search(rb"/Count\s+(\d+)", data)
    listed = int(count_match.group(1)) if count_match else None
    if listed is None:
        raise VerifyError("The page tree declares no /Count.")
    if pages_present != listed:
        raise VerifyError(
            f"The page tree says there are {listed} page(s) but the file contains "
            f"{pages_present}."
        )

    return {"objects": checked, "pages": pages_present}, (
        f"{checked} objects at correct byte offsets, {pages_present} page(s) present"
    )


def verify_document(path: str | Path, claimed: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Reopen a written document and check it against what was claimed.

    Returns ``(ok, detail)``. Never raises for a bad document -- a refusal is a
    result, and the caller wants the reason, not an exception.
    """
    target = Path(path)
    if not target.is_file():
        return False, f"{target} does not exist."
    size = target.stat().st_size
    if size == 0:
        return False, f"{target} is empty."

    kind = KIND_BY_SUFFIX.get(target.suffix.lower())
    if kind is None:
        return False, f"{target.suffix} is not a document format NEXUS writes."

    data = target.read_bytes()
    try:
        if kind == "pdf":
            counts, summary = _verify_pdf(data)
        else:
            archive = _read_zip(data)
            with archive:
                names = set(archive.namelist())
                _check_required(kind, names)
                _parse_all_xml(archive)
                _check_relationships(archive)
                if kind == "docx":
                    counts = _count_docx(archive)
                elif kind == "xlsx":
                    counts = _count_xlsx(archive)
                else:
                    counts = _count_pptx(archive)
            summary = ", ".join(f"{k}={v}" for k, v in counts.items() if not isinstance(v, list))
    except VerifyError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 -- a crash while verifying is a failed verification
        return False, f"The file could not be verified ({type(exc).__name__}: {exc})."

    # Compare against what the writer said, where the two are directly comparable.
    # `parts` is not compared: a writer counts the parts it wrote, a reader counts
    # the parts it found, and those are the same number for a reason that is worth
    # asserting rather than assuming.
    if claimed:
        mismatches: list[str] = []
        for key, actual in counts.items():
            if key in ("slide_titles", "parts", "characters"):
                continue
            if key in claimed and claimed[key] != actual:
                mismatches.append(f"{key}: wrote {claimed[key]}, found {actual}")
        if mismatches:
            return False, "The document does not match what was written: " + "; ".join(mismatches)

    return True, f"{kind.upper()} verified: {summary}, {size:,} bytes"


def verify_bytes(data: bytes, kind: str) -> tuple[bool, str]:
    """Verify a document held in memory, for the negative tests."""
    try:
        if kind == "pdf":
            _, summary = _verify_pdf(data)
        else:
            archive = _read_zip(data)
            with archive:
                names = set(archive.namelist())
                _check_required(kind, names)
                _parse_all_xml(archive)
                _check_relationships(archive)
            summary = f"{len(names)} parts parsed"
    except VerifyError as exc:
        return False, str(exc)
    return True, summary
