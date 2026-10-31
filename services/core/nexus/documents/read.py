"""Reading a document back out as text (spec §5, O15).

The writer turns blocks into bytes. :mod:`verify` proves the bytes are a valid
document. Neither of them can answer the question a person actually asks next --
*what does it say?* -- and answering it is the difference between NEXUS being
able to check its own work and NEXUS having to take its own word for it.

What comes out is **reading order, not structure**. A heading is a line starting
with ``#``, a bullet is a line starting with ``- ``, a table row is its cells
joined by tabs. That is enough to read and to check a document against what was
asked for. It is deliberately *not* a round-trip: feeding this back into
:func:`~nexus.documents.blocks.blocks_from` would re-parse a code block
containing a ``#`` comment as a heading. The tool description says so rather than
letting the model discover it.

The formats are handled by what they actually are:

* DOCX and PPTX text lives in known element names, so it is read straight out of
  the XML.
* XLSX cells are read by cell reference, so the grid stays a grid rather than a
  stream of values with the columns lost.
* PDF text is pulled from the content stream's text-showing operators, which
  means handling the ``FlateDecode`` filter other producers use. A producer that
  subsets fonts with a custom encoding, or draws text as curves, produces no
  extractable text at all -- and this says that, rather than returning "" and
  implying the document was empty.
"""

from __future__ import annotations

import io
import re
import zlib
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .verify import A_NS, KIND_BY_SUFFIX, SS_NS, W_NS

#: Default cap on returned characters. A document is untrusted-size by the time
#: it is read back -- the same reason files.py caps its reads -- and a tool result
#: goes into the audit log and the model's context.
DEFAULT_LIMIT = 20_000
MAX_LIMIT = 200_000


class ReadError(RuntimeError):
    """The document cannot be read, and here is why."""


def _tag(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}"


def _open_zip(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ReadError(f"This is not a readable zip archive ({exc}).") from None


def _text_of(element: ElementTree.Element, namespace: str) -> str:
    """The text under an element, in order.

    Runs are concatenated rather than the first one taken, because Word splits a
    paragraph into a run per formatting change -- reading only the first would
    drop everything after the first bolded word. Breaks and tabs are emitted as
    the characters they stand for: a code block stored as one paragraph with
    ``<w:br/>`` between its lines has no other record of where those lines end,
    so ignoring the break silently glues the whole block into one line.
    """
    return "".join(_text_pieces(element, namespace))


def _text_pieces(element: ElementTree.Element, namespace: str) -> list[str]:
    pieces: list[str] = []
    for node in element.iter():
        if node.tag == _tag(namespace, "t"):
            pieces.append(node.text or "")
        elif node.tag in (_tag(namespace, "br"), _tag(namespace, "cr")):
            pieces.append("\n")
        elif node.tag == _tag(namespace, "tab"):
            pieces.append("\t")
    return pieces


# --------------------------------------------------------------------- DOCX


def _read_docx(data: bytes) -> tuple[str, dict[str, Any]]:
    with _open_zip(data) as archive:
        try:
            source = archive.read("word/document.xml")
        except KeyError:
            raise ReadError("This .docx has no word/document.xml.") from None
        root = ElementTree.fromstring(source)
        body = root.find(_tag(W_NS, "body"))
        if body is None:
            raise ReadError("word/document.xml has no <w:body>.")

        lines: list[str] = []
        counts = {"paragraphs": 0, "headings": 0, "list_items": 0, "tables": 0}

        def emit_paragraph(paragraph: ElementTree.Element, *, prefix_indent: str = "") -> None:
            content = _text_of(paragraph, W_NS).strip()
            if not content:
                return
            props = paragraph.find(_tag(W_NS, "pPr"))
            style = None
            numbered = False
            if props is not None:
                node = props.find(_tag(W_NS, "pStyle"))
                style = (node.get(_tag(W_NS, "val")) or "").lower() if node is not None else None
                numbered = props.find(_tag(W_NS, "numPr")) is not None
            if style and style.startswith("heading"):
                level = style[len("heading"):].strip() or "1"
                lines.append(f"{'#' * (int(level) if level.isdigit() else 1)} {content}")
                counts["headings"] += 1
            elif style == "code":
                # Word stores the line breaks inside the runs, so the whole block
                # arrives as one paragraph with newlines already in it.
                lines.extend(content.splitlines())
                counts["paragraphs"] += 1
            elif numbered:
                lines.append(f"{prefix_indent}- {content}")
                counts["list_items"] += 1
            else:
                lines.append(f"{prefix_indent}{content}")
                counts["paragraphs"] += 1

        def walk(parent: ElementTree.Element) -> None:
            for child in parent:
                if child.tag == _tag(W_NS, "tbl"):
                    counts["tables"] += 1
                    for row in child.findall(_tag(W_NS, "tr")):
                        cells = [
                            _text_of(cell, W_NS).strip().replace("\n", " ")
                            for cell in row.findall(_tag(W_NS, "tc"))
                        ]
                        while cells and not cells[-1]:
                            cells.pop()
                        if cells:
                            lines.append("\t".join(cells))
                elif child.tag == _tag(W_NS, "p"):
                    emit_paragraph(child)
                else:
                    walk(child)

        walk(body)
        counts["characters"] = sum(len(line) for line in lines)
    return "\n".join(lines), counts


# --------------------------------------------------------------------- XLSX


def _read_xlsx(data: bytes) -> tuple[str, dict[str, Any]]:
    cell_re = re.compile(r"([A-Z]+)(\d+)")
    with _open_zip(data) as archive:
        sheet_names = sorted(
            n for n in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)
        )
        if not sheet_names:
            raise ReadError("This .xlsx contains no worksheets.")
        # A shared-string table is how every producer other than this one stores
        # text, and a reader that ignored it would return a spreadsheet of bare
        # numbers with every label missing.
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = [_text_of(node, SS_NS) for node in root.findall(_tag(SS_NS, "si"))]

        lines: list[str] = []
        counts = {"sheets": 0, "rows": 0, "cells": 0}
        for name in sheet_names:
            counts["sheets"] += 1
            root = ElementTree.fromstring(archive.read(name))
            for row in root.iter(_tag(SS_NS, "row")):
                values: dict[int, str] = {}
                for cell in row.findall(_tag(SS_NS, "c")):
                    match = cell_re.fullmatch(cell.get("r") or "")
                    column = 0
                    if match:
                        for letter in match.group(1):
                            column = column * 26 + (ord(letter) - 64)
                    kind = cell.get("t")
                    if kind == "inlineStr":
                        value = _text_of(cell, SS_NS)
                    elif kind == "s":
                        node = cell.find(_tag(SS_NS, "v"))
                        index = int(node.text) if node is not None and node.text else -1
                        value = shared[index] if 0 <= index < len(shared) else ""
                    else:
                        formula = cell.find(_tag(SS_NS, "f"))
                        node = cell.find(_tag(SS_NS, "v"))
                        if node is not None and node.text:
                            value = node.text
                        elif formula is not None:
                            # Our own writer stores a formula with no cached
                            # result, so the file has the formula and not the
                            # number. Showing "=A1+B1" is truthful; showing a
                            # blank would read as an empty cell.
                            value = f"={formula.text or ''}"
                        else:
                            value = ""
                    if column:
                        values[column] = value
                    counts["cells"] += 1
                if values:
                    width = max(values)
                    lines.append(
                        "\t".join(values.get(c, "") for c in range(1, width + 1)).rstrip()
                    )
                    counts["rows"] += 1
                elif row.findall(_tag(SS_NS, "c")):
                    lines.append("")
    return "\n".join(lines), counts


# --------------------------------------------------------------------- PPTX


def _read_pptx(data: bytes) -> tuple[str, dict[str, Any]]:
    with _open_zip(data) as archive:
        slides = sorted(
            (n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
            key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)),
        )
        if not slides:
            raise ReadError("This .pptx contains no slides.")

        lines: list[str] = []
        titles: list[str] = []
        for index, name in enumerate(slides, start=1):
            root = ElementTree.fromstring(archive.read(name))
            # Every paragraph is one <a:p>; taking all <a:t> at once would run
            # the title and the first bullet together on one line.
            paragraphs: list[tuple[str, bool]] = []
            for paragraph in root.iter(_tag(A_NS, "p")):
                content = _text_of(paragraph, A_NS).strip()
                if not content:
                    continue
                props = paragraph.find(_tag(A_NS, "pPr"))
                # A real list item, according to the file. Inferring it from a
                # leading "•" in the text would read a literal bullet someone
                # actually typed as a list, which is the mistake this writer used
                # to make in the other direction.
                bullet = props is not None and props.find(_tag(A_NS, "buChar")) is not None
                paragraphs.append((content, bullet))
            if not paragraphs:
                continue
            titles.append(paragraphs[0][0])
            lines.append(f"# Slide {index}: {paragraphs[0][0]}")
            lines.extend(
                f"- {text}" if bullet else text for text, bullet in paragraphs[1:]
            )
        counts = {"slides": len(titles), "slide_titles": titles}
    return "\n".join(lines), counts


# ---------------------------------------------------------------------- PDF


def _pdf_strings(content: bytes) -> list[str]:
    """Text drawn by the page, in the order the operators draw it.

    This walks the operators rather than searching for text between parentheses,
    because parentheses are also used for page numbers, author names and
    anything else the producer felt like storing -- and returning those mixed
    into the body text is worse than returning nothing.

    Pieces drawn at the same baseline are joined, because a single visual line is
    usually several ``Tj`` calls: this writer draws a bullet marker at the margin
    and the item text twenty-two points to its right, and a reader that emitted
    one line per operator would return every list item as a stray "-" followed by
    its own text on the next line.
    """
    # A "show text" argument is either (a literal) or <a hex string>, and the TJ
    # form passes an array of them with kerning numbers in between.
    token = re.compile(rb"\((?:[^()\\]|\\.)*\)|<[0-9A-Fa-f\s]*>|\[|\]|[-\d.]+|[A-Za-z'\"*]+")
    tokens = [m.group(0) for m in token.finditer(content)]

    drawn: list[tuple[float | None, str]] = []
    pending: list[str] = []
    baseline: float | None = None

    def flush() -> None:
        nonlocal pending
        if pending:
            drawn.append((baseline, "".join(pending)))
            pending = []

    for index, raw in enumerate(tokens):
        if raw == b"Tm":
            # "a b c d e f Tm" -- f is the y of the baseline this text sits on.
            numbers = tokens[max(0, index - 6):index]
            try:
                baseline = float(numbers[-1])
            except (IndexError, ValueError):
                baseline = None
            flush()
        elif raw in (b"Tj", b"TJ", b"'", b'"'):
            flush()
        elif raw in (b"Td", b"TD", b"T*"):
            # A relative move to a new line finishes the line being drawn.
            flush()
            baseline = None
        elif raw.startswith(b"("):
            pending.append(_pdf_unescape(raw[1:-1]))
        elif raw.startswith(b"<") and len(raw) > 2:
            # A hex string in a simple font is one byte per glyph, in the font's
            # encoding. With WinAnsiEncoding that is cp1252.
            digits = re.sub(rb"\s", b"", raw[1:-1])
            if len(digits) % 2:
                digits += b"0"
            pending.append(
                bytes.fromhex(digits.decode("ascii", "ignore")).decode("cp1252", "replace")
            )
    flush()

    # Joined here rather than at draw time, because whether two pieces share a
    # line is only known once the second one has been seen.
    lines: list[str] = []
    previous: float | None = None
    for position, text in drawn:
        if lines and position is not None and previous == position:
            lines[-1] = f"{lines[-1]} {text}".strip()
        else:
            lines.append(text)
        previous = position
    return lines


def _pdf_unescape(body: bytes) -> str:
    result = bytearray()
    escaped = False
    index = 0
    while index < len(body):
        byte = body[index]
        if escaped:
            mapping = {0x6E: 0x0A, 0x72: 0x0D, 0x74: 0x09, 0x62: 0x08, 0x66: 0x0C}
            if byte in mapping:
                result.append(mapping[byte])
            elif 0x30 <= byte <= 0x37:
                # \ddd is an octal character code, up to three digits.
                digits = bytes([byte])
                while (
                    len(digits) < 3
                    and index + 1 < len(body)
                    and 0x30 <= body[index + 1] <= 0x37
                ):
                    index += 1
                    digits += bytes([body[index]])
                result.append(int(digits, 8) & 0xFF)
            else:
                result.append(byte)
            escaped = False
        elif byte == 0x5C:
            escaped = True
        else:
            result.append(byte)
        index += 1
    return bytes(result).decode("cp1252", "replace")


def _read_pdf(data: bytes) -> tuple[str, dict[str, Any]]:
    if not data.startswith(b"%PDF-"):
        raise ReadError("This file does not begin with a PDF header.")

    lines: list[str] = []
    pages = 0
    undecodable = 0
    for match in re.finditer(rb"stream\r?\n", data):
        start = match.end()
        end = data.find(b"endstream", start)
        if end == -1:
            continue
        body = data[start:end]
        # This object's dictionary runs from the end of the previous object to
        # the "stream" keyword, which is how the /Filter is found without parsing
        # the whole object graph.
        previous = data.rfind(b"endobj", 0, match.start())
        header = data[(previous + 6) if previous != -1 else 0:match.start()]
        if b"FlateDecode" in header or b"/Fl" in header:
            try:
                body = zlib.decompress(body)
            except zlib.error:
                # A stream that claims Flate and is not is a corrupt file, not a
                # file with no text. Counted so the caller can tell the difference.
                undecodable += 1
                continue
        elif b"/Filter" in header:
            # JPEG, LZW, RunLength: none of them carry text. Skipping is not a
            # failure -- it means this page's text is not extractable here.
            continue
        text = _pdf_strings(body)
        if not text:
            continue
        pages += 1
        lines.extend(line for line in text if line.strip())

    if not lines:
        raise ReadError(
            "No text could be extracted from this PDF. It may draw its text as "
            "images or use an embedded font subset with a custom encoding. "
            "NEXUS can read PDFs it wrote itself."
        )
    return "\n".join(lines), {"pages_with_text": pages, "undecodable_streams": undecodable}


# ------------------------------------------------------------------- public

_READERS = {"docx": _read_docx, "xlsx": _read_xlsx, "pptx": _read_pptx, "pdf": _read_pdf}


def read_document(path: str | Path, limit: int = DEFAULT_LIMIT) -> tuple[str, dict[str, Any]]:
    """Extract the text of a document. Raises :class:`ReadError` with the reason."""
    target = Path(path)
    if not target.is_file():
        raise ReadError(f"{target} does not exist.")
    kind = KIND_BY_SUFFIX.get(target.suffix.lower())
    if kind is None:
        raise ReadError(
            f"{target.suffix or 'That file'} is not a format NEXUS reads. "
            f"It reads {', '.join(sorted(_READERS))}."
        )
    reader = _READERS[kind]
    text, stats = reader(target.read_bytes())

    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    truncated = len(text) > limit
    if truncated:
        text = text[:limit]
    return text, {
        "format": kind,
        "characters": len(text),
        "truncated": truncated,
        **stats,
    }
