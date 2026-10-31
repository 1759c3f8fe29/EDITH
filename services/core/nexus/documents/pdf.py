"""Writing a PDF with nothing but the standard library.

A PDF is a header, a set of numbered objects, a cross-reference table that says
where each object starts in bytes, and a trailer. There is no compression and no
container format in the way -- which is why this is tractable without reportlab,
and why the one thing that must be exactly right is the byte offsets. An ``xref``
that is off by one byte produces a file that some readers open and others refuse,
with nothing in the file to say which byte was wrong.

Text uses the base-14 fonts (Helvetica, Helvetica-Bold, Courier), which every
reader has and therefore none is embedded. Two consequences worth stating rather
than discovering:

* **Widths are measured, not guessed.** Wrapping needs to know how wide a string
  is, and for a base-14 font that means the metrics Adobe published in the AFM
  files -- a table, not a formula. An average-character-width estimate wraps well
  for one paragraph and badly for a line of "MMMMM" or a column of numbers, so
  the real tables are here.
* **A single-byte font cannot hold every character.** With WinAnsiEncoding the
  representable set is Windows-1252. A character outside it is replaced with "?"
  and **counted**; the count is returned so the caller can say what was lost
  instead of shipping a document with silent question marks in it (§78).
"""

from __future__ import annotations

import time
from typing import Any

from .blocks import Block

PAGE_W = 595.0   # A4 at 72 dpi
PAGE_H = 842.0
MARGIN = 60.0
CONTENT_W = PAGE_W - 2 * MARGIN

FONT_REGULAR = "F1"
FONT_BOLD = "F2"
FONT_MONO = "F3"

# Adobe's own advance widths for Helvetica and Helvetica-Bold, characters 32-126,
# taken from the published AFM files. Everything else is handled by fallback.
_HELVETICA = (
    "278,278,355,556,556,889,667,191,333,333,389,584,278,333,278,278,"
    "556,556,556,556,556,556,556,556,556,556,"
    "278,278,584,584,584,556,1015,"
    "667,667,722,722,667,611,778,722,278,500,667,556,833,722,778,667,778,722,667,611,722,667,944,667,667,611,"
    "278,278,278,469,556,333,"
    "556,556,500,556,556,278,556,556,222,222,500,222,833,556,556,556,556,333,500,278,556,500,722,500,500,500,"
    "334,260,334,584"
)
_HELVETICA_BOLD = (
    "278,333,474,556,556,889,722,238,333,333,389,584,278,333,278,278,"
    "556,556,556,556,556,556,556,556,556,556,"
    "333,333,584,584,584,611,975,"
    "722,722,722,722,667,611,778,722,278,556,722,611,833,722,778,667,778,722,667,611,722,667,944,667,667,611,"
    "333,278,333,584,556,333,"
    "556,611,556,611,556,333,611,611,278,278,556,278,889,611,611,611,611,389,556,333,611,556,778,556,556,500,"
    "389,280,389,584"
)

_MONO_ADVANCE = 600  # Courier is monospaced


def _widths(spec: str) -> dict[int, int]:
    return {code: int(w) for code, w in zip(range(32, 127), spec.split(","))}


_W_REGULAR = _widths(_HELVETICA)
_W_BOLD = _widths(_HELVETICA_BOLD)


def _char_width(char: str, font: str) -> int:
    """Advance width in 1/1000 em for one character of a proportional font.

    A character outside printable ASCII is either a WinAnsi letter (which will be
    drawn) or one that becomes "?" -- and in neither case do we have a metric for
    it. Both take the width of "?", which is at least the width of the glyph that
    ends up on the page.
    """
    table = _W_BOLD if font == FONT_BOLD else _W_REGULAR
    code = ord(char)
    if 32 <= code <= 126:
        return table.get(code, 556)
    return table.get(ord("?"), 556)


def text_width(text: str, font: str, size: float) -> float:
    """Width of a string in points at the given size."""
    if font == FONT_MONO:
        return len(text) * _MONO_ADVANCE * size / 1000.0
    return sum(_char_width(c, font) for c in text) * size / 1000.0


def wrap_text(text: str, font: str, size: float, width: float) -> list[str]:
    """Break text into lines that fit, without splitting a word unnecessarily.

    A word wider than the whole line is broken by character rather than allowed to
    overflow: an unbreakable URL in a narrow table column would otherwise draw
    straight through the column beside it.
    """
    if not text:
        return [""]
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if not current or text_width(candidate, font, size) <= width:
                current = candidate
                continue
            lines.append(current)
            current = word
        if not current:
            continue
        while text_width(current, font, size) > width and len(current) > 1:
            cut = len(current)
            while cut > 1 and text_width(current[:cut], font, size) > width:
                cut -= 1
            lines.append(current[:cut])
            current = current[cut:]
        if current:
            lines.append(current)
    return lines or [""]


def _escape_bytes(text: str) -> tuple[bytes, int]:
    """PDF string bytes, plus how many characters could not be represented."""
    out = bytearray()
    lost = 0
    for char in text:
        try:
            encoded = char.encode("cp1252")
        except UnicodeEncodeError:
            lost += 1
            encoded = b"?"
        for byte in encoded:
            # ( ) and \ must be escaped. Every other byte, high ones included, is
            # written literally: the font declares WinAnsiEncoding, so byte 0xE9
            # is 'é' and escaping it would be wrong.
            if byte in (0x28, 0x29, 0x5C):
                out.append(0x5C)
            out.append(byte)
    return bytes(out), lost


class _Page:
    """One page's drawing operations, plus the vertical cursor that fills them.

    ``y`` is the baseline for the next piece of text, counted from the bottom of
    the page the way PDF counts. Drawing and moving are separate calls so a table
    can place every cell itself instead of inheriting wherever the last line left
    the cursor.
    """

    def __init__(self) -> None:
        self.ops: list[bytes] = []
        self.y = PAGE_H - MARGIN

    def room(self, needed: float) -> bool:
        return self.y - needed >= MARGIN

    def advance(self, amount: float) -> None:
        self.y -= amount

    def draw(self, x: float, text: str, font: str, size: float, *,
             indent: float = 0.0, colour: tuple[float, float, float] = (0.1, 0.1, 0.1)) -> int:
        payload, lost = _escape_bytes(text)
        r, g, b = colour
        prefix = (
            f"BT {r:.3f} {g:.3f} {b:.3f} rg /{font} {size:.2f} Tf "
            f"1 0 0 1 {x + indent:.2f} {self.y:.2f} Tm ("
        ).encode("ascii")
        self.ops.append(prefix + payload + b") Tj ET")
        return lost

    def rule(self, x1: float, y: float, x2: float, *, grey: float = 0.78) -> None:
        self.ops.append(
            f"{grey:.3f} {grey:.3f} {grey:.3f} RG 0.6 w "
            f"{x1:.2f} {y:.2f} m {x2:.2f} {y:.2f} l S".encode("ascii")
        )

    def stream(self) -> bytes:
        return b"\n".join(self.ops)


def write_pdf(title: str, blocks: list[Block]) -> tuple[bytes, dict[str, Any]]:
    """Render the blocks to a PDF. Returns (bytes, claimed counts)."""
    pages: list[_Page] = [_Page()]
    page = pages[0]
    lost_characters = 0

    BODY = 10.5
    BODY_LEAD = 15.0
    CODE = 9.5
    CODE_LEAD = 12.0
    CELL = 9.5
    CELL_LEAD = 11.5
    HEADING = {
        1: (21.0, 18.0, 9.0), 2: (16.0, 15.0, 7.0), 3: (13.5, 13.0, 6.0),
        4: (12.0, 11.0, 5.0), 5: (11.0, 10.0, 4.0), 6: (11.0, 10.0, 4.0),
    }

    def ensure(needed: float) -> None:
        """Start a new page when the next thing would not fit on this one."""
        nonlocal page
        if not page.room(needed):
            page = _Page()
            pages.append(page)

    for block in blocks:
        if block.kind == "heading":
            size, before, after = HEADING.get(block.level, HEADING[1])
            ensure(before + size * 1.6 + after)
            page.advance(before)
            for line in wrap_text(block.text, FONT_BOLD, size, CONTENT_W):
                ensure(size * 1.6)
                lost_characters += page.draw(MARGIN, line, FONT_BOLD, size,
                                             colour=(0.12, 0.22, 0.39))
                page.advance(size * 1.5)
            page.advance(after)

        elif block.kind == "paragraph":
            for line in wrap_text(block.text, FONT_REGULAR, BODY, CONTENT_W):
                ensure(BODY_LEAD)
                lost_characters += page.draw(MARGIN, line, FONT_REGULAR, BODY)
                page.advance(BODY_LEAD)
            page.advance(6.0)

        elif block.kind == "code":
            for line in block.text.splitlines() or [""]:
                for wrapped in wrap_text(line, FONT_MONO, CODE, CONTENT_W - 12):
                    ensure(CODE_LEAD)
                    lost_characters += page.draw(MARGIN, wrapped, FONT_MONO, CODE,
                                                 indent=12.0, colour=(0.20, 0.22, 0.26))
                    page.advance(CODE_LEAD)
            page.advance(6.0)

        elif block.kind in ("bullets", "numbered"):
            for index, item in enumerate(block.items, start=1):
                marker = "•" if block.kind == "bullets" else f"{index}."
                for offset, line in enumerate(wrap_text(item, FONT_REGULAR, BODY, CONTENT_W - 22)):
                    ensure(BODY_LEAD)
                    if offset == 0:
                        lost_characters += page.draw(MARGIN, marker, FONT_REGULAR, BODY)
                    lost_characters += page.draw(MARGIN, line, FONT_REGULAR, BODY, indent=22.0)
                    page.advance(BODY_LEAD)
            page.advance(4.0)

        elif block.kind == "table":
            columns = max(
                len(block.headers),
                max((len(r) for r in block.rows), default=0),
                1,
            )
            col_w = CONTENT_W / columns

            def draw_row(cells: list[str], *, bold: bool) -> None:
                nonlocal lost_characters
                font = FONT_BOLD if bold else FONT_REGULAR
                wrapped = [wrap_text(str(c), font, CELL, col_w - 10) for c in cells]
                height = max((len(w) for w in wrapped), default=1) * CELL_LEAD + 4
                ensure(height)
                top = page.y
                for column, lines in enumerate(wrapped):
                    # Each column restarts at the row top. Without this a short
                    # cell in column one would shift column two up the page.
                    for row_index, line in enumerate(lines):
                        page.y = top - row_index * CELL_LEAD
                        lost_characters += page.draw(
                            MARGIN + column * col_w, line, font, CELL, indent=5.0
                        )
                page.y = top - height
                page.rule(MARGIN, page.y + 2, PAGE_W - MARGIN)

            if block.headers:
                draw_row(list(block.headers), bold=True)
            for row in block.rows:
                draw_row([str(c) for c in row], bold=False)
            page.advance(8.0)

        elif block.kind == "page_break":
            page = _Page()
            pages.append(page)

    # ---------------------------------------------------------------- assemble
    #
    # Object 1 (catalog), 2 (pages) and 3 (info) are reserved up front because the
    # page objects refer to their parent by number while their own bodies are
    # still being built. A reserved slot that is never filled would be written as
    # an empty object, which is not a legal PDF object, so exactly three are
    # reserved and all three are replaced below.
    objects: list[bytes] = [b"", b"", b""]

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)  # PDF object numbers are 1-based

    font_regular = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    font_bold = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
    )
    font_mono = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>"
    )
    resources = (
        f"<< /Font << /{FONT_REGULAR} {font_regular} 0 R /{FONT_BOLD} {font_bold} 0 R "
        f"/{FONT_MONO} {font_mono} 0 R >> >>"
    )

    page_numbers: list[int] = []
    for one in pages:
        content = one.stream()
        content_number = add(
            b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n"
            + content + b"\nendstream"
        )
        page_numbers.append(
            add(
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W:.0f} {PAGE_H:.0f}] "
                f"/Resources {resources} /Contents {content_number} 0 R >>".encode("ascii")
            )
        )

    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{n} 0 R" for n in page_numbers)
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_numbers)} >>".encode("ascii")
    stamp = time.strftime("D:%Y%m%d%H%M%S", time.localtime())
    objects[2] = (
        f"<< /Title ({_escape_bytes(title)[0].decode('latin-1')}) "
        f"/Producer (NEXUS) /Creator (NEXUS) "
        f"/CreationDate ({stamp}) /ModDate ({stamp}) >>"
    ).encode("latin-1")

    buffer = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(buffer))
        buffer += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    xref_at = len(buffer)
    count = len(objects) + 1
    buffer += f"xref\n0 {count}\n".encode("ascii")
    buffer += b"0000000000 65535 f \n"
    for offset in offsets:
        buffer += f"{offset:010d} 00000 n \n".encode("ascii")
    buffer += (
        f"trailer\n<< /Size {count} /Root 1 0 R /Info 3 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode("ascii")

    claimed = {
        "format": "pdf",
        "pages": len(pages),
        "headings": sum(1 for b in blocks if b.kind == "heading"),
        "paragraphs": sum(1 for b in blocks if b.kind == "paragraph"),
        "code_blocks": sum(1 for b in blocks if b.kind == "code"),
        "list_items": sum(len(b.items) for b in blocks if b.kind in ("bullets", "numbered")),
        "tables": sum(1 for b in blocks if b.kind == "table"),
        "table_rows": sum(len(b.rows) for b in blocks if b.kind == "table"),
        "characters_lost": lost_characters,
    }
    return bytes(buffer), claimed
