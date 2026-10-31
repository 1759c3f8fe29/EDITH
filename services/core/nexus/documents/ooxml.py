"""Writing DOCX, XLSX and PPTX with nothing but the standard library.

These three formats are zip archives of XML parts. That is the whole reason this
module exists without a dependency: ``zipfile`` plus ``xml.sax.saxutils`` is
enough to produce files Word, Excel, PowerPoint and LibreOffice open.

Three things are load-bearing and easy to get subtly wrong, so they are worth
naming:

* **The relationships graph.** A part that exists in the zip but is not reachable
  from ``_rels/.rels`` -- or from the part that references it -- is not part of
  the document as far as any reader is concerned. The file will open and the
  content will be missing, which looks like a writer bug and is actually a graph
  bug. Every part written here has a rel that points at it.
* **Content types.** ``[Content_Types].xml`` must declare the main part with an
  ``Override`` and enough ``Default`` extensions to cover the rest. Get an
  override wrong and the reader refuses the file outright; ``python-docx`` and
  LibreOffice disagree about how forgiving to be, which is why the check in
  :mod:`verify` insists on the parts a reader would demand.
* **Word's list numbering.** Bullets and numbers are not text in DOCX; they are a
  ``numPr`` on the paragraph pointing into a ``numbering.xml`` part. Writing a
  literal "• " prefix instead produces something that looks right in a
  screenshot and is not a list in any editor.

The writers return a small dict of counts. Those counts are what the document
*claims* to contain -- :func:`nexus.documents.verify.verify_document` reopens the
file and counts again, which is the only thing that makes the claim evidence.
"""

from __future__ import annotations

import time
import zipfile
from io import BytesIO
from typing import Any
from xml.sax.saxutils import escape as _xml_escape
from xml.sax.saxutils import quoteattr

from .blocks import Block

# ---------------------------------------------------------------- namespaces

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CORE_REL = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"
SS_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

_DOCX_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_XLSX_MAIN = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
_PPTX_MAIN = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"


def _esc(value: Any) -> str:
    """Text for an XML text node. Control characters are dropped, not escaped.

    XML 1.0 has no representation for most control characters, so a stray \\x0b
    from a text field makes the part unparseable rather than merely ugly. Dropping
    them silently is the lesser evil; the alternative is a file that will not open.
    """
    text = str(value)
    cleaned = "".join(c for c in text if c >= " " or c in "\t\n\r")
    return _xml_escape(cleaned)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _zip(parts: dict[str, str | bytes]) -> bytes:
    """Build the archive. Parts are written in a fixed order for reproducibility."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(parts):
            payload = parts[name]
            data = payload.encode("utf-8") if isinstance(payload, str) else payload
            info = zipfile.ZipInfo(name, date_time=time.localtime(time.time())[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def _core_properties(title: str, kind: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:dcterms="http://purl.org/dc/terms/"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{_esc(title)}</dc:title>"
        "<dc:creator>NEXUS</dc:creator>"
        "<cp:lastModifiedBy>NEXUS</cp:lastModifiedBy>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{_now()}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{_now()}</dcterms:modified>'
        f"<dc:description>Written by NEXUS as {kind}.</dc:description>"
        "</cp:coreProperties>"
    )


# =============================================================== DOCX


def _docx_run(text: str, *, bold: bool = False, mono: bool = False, color: str | None = None) -> str:
    props = ""
    if bold:
        props += "<w:b/>"
    if mono:
        props += '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/>'
    if color:
        props += f'<w:color w:val="{color}"/>'
    rpr = f"<w:rPr>{props}</w:rPr>" if props else ""
    # xml:space="preserve" is required whenever the text has a leading or trailing
    # space, which prose routinely does. Without it Word collapses the space and
    # the file differs from what was asked for.
    return f'<w:r>{rpr}<w:t xml:space="preserve">{_esc(text)}</w:t></w:r>'


def _docx_para(
    runs: str,
    *,
    style: str | None = None,
    num_id: int | None = None,
    spacing_after: int | None = None,
    keep_next: bool = False,
) -> str:
    props = ""
    if style:
        props += f'<w:pStyle w:val="{style}"/>'
    if keep_next:
        props += "<w:keepNext/>"
    if num_id is not None:
        props += f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{num_id}"/></w:numPr>'
    if spacing_after is not None:
        props += f'<w:spacing w:after="{spacing_after}"/>'
    ppr = f"<w:pPr>{props}</w:pPr>" if props else ""
    return f"<w:p>{ppr}{runs}</w:p>"


def _docx_table(block: Block) -> str:
    grid = ""
    if block.headers:
        columns = len(block.headers)
        grid = (
            "<w:tblGrid>"
            + "".join(f'<w:gridCol w:w="{int(9000 / max(columns, 1))}"/>' for _ in range(columns))
            + "</w:tblGrid>"
        )

    def cell(text: str, *, header: bool = False) -> str:
        shading = '<w:shd w:val="clear" w:fill="EFEFEF"/>' if header else ""
        # A table cell must contain at least one paragraph; Word treats an empty
        # w:tc as corrupt, which is why this is not conditional on `text`.
        return (
            "<w:tc><w:tcPr>"
            f'<w:tcW w:w="0" w:type="auto"/>{shading}'
            '<w:tcBorders>'
            '<w:top w:val="single" w:sz="4" w:color="BFBFBF"/>'
            '<w:left w:val="single" w:sz="4" w:color="BFBFBF"/>'
            '<w:bottom w:val="single" w:sz="4" w:color="BFBFBF"/>'
            '<w:right w:val="single" w:sz="4" w:color="BFBFBF"/>'
            "</w:tcBorders>"
            "</w:tcPr>"
            + _docx_para(_docx_run(text, bold=header) if text else "")
            + "</w:tc>"
        )

    rows = ""
    if block.headers:
        rows += "<w:tr>" + "".join(cell(h, header=True) for h in block.headers) + "</w:tr>"
    for row in block.rows:
        cells = "".join(cell(c) for c in row)
        # A row with fewer cells than the grid is legal but renders raggedly;
        # pad so the table keeps its shape.
        missing = max(0, len(block.headers) - len(row))
        rows += "<w:tr>" + cells + ("<w:tc><w:p/></w:tc>" * missing) + "</w:tr>"

    return (
        "<w:tbl><w:tblPr>"
        '<w:tblStyle w:val="TableGrid"/>'
        '<w:tblW w:w="0" w:type="auto"/>'
        '<w:tblLayout w:type="autofit"/>'
        "</w:tblPr>"
        + grid
        + rows
        + "</w:tbl>"
    )


def _docx_body(blocks: list[Block]) -> str:
    out: list[str] = []
    heading_styles = {1: "Heading1", 2: "Heading2", 3: "Heading3",
                      4: "Heading4", 5: "Heading5", 6: "Heading6"}
    for block in blocks:
        if block.kind == "heading":
            out.append(
                _docx_para(
                    _docx_run(block.text),
                    style=heading_styles.get(block.level, "Heading1"),
                    keep_next=True,
                )
            )
        elif block.kind == "paragraph":
            out.append(_docx_para(_docx_run(block.text), spacing_after=120))
        elif block.kind == "code":
            # One paragraph, one run per line, with a real line break between
            # them. The break is an empty run carrying <w:br/> because w:br is
            # run-level content and is not legal as a direct child of w:p. One
            # paragraph per block (rather than one per line) is what makes a code
            # block countable: two adjacent code paragraphs are indistinguishable
            # from two separate blocks.
            line_runs = "<w:r><w:br/></w:r>".join(
                _docx_run(line, mono=True) for line in (block.text.splitlines() or [""])
            )
            out.append(_docx_para(line_runs, style="Code"))
        elif block.kind == "bullets":
            out.extend(
                _docx_para(_docx_run(item), num_id=1, spacing_after=0) for item in block.items
            )
        elif block.kind == "numbered":
            out.extend(
                _docx_para(_docx_run(item), num_id=2, spacing_after=0) for item in block.items
            )
        elif block.kind == "table":
            out.append(_docx_table(block))
            # Word requires a paragraph between a table and whatever follows it;
            # a table as the last body element makes some readers unhappy.
            out.append(_docx_para(""))
        elif block.kind == "page_break":
            out.append(
                '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'
            )
    # The body must end with a paragraph carrying the section properties.
    out.append(
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" '
        'w:header="709" w:footer="709" w:gutter="0"/></w:sectPr>'
    )
    return "".join(out)


def _docx_styles() -> str:
    def para_style(style_id: str, name: str, *, size: int, bold: bool, outline: int,
                   spacing_before: int, spacing_after: int, color: str) -> str:
        return (
            f'<w:style w:type="paragraph" w:styleId="{style_id}">'
            f'<w:name w:val="{name}"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>'
            f'<w:uiPriority w:val="{outline}"/><w:qFormat/>'
            f'<w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="{spacing_before}" '
            f'w:after="{spacing_after}"/><w:outlineLvl w:val="{outline}"/></w:pPr>'
            f'<w:rPr><w:b/><w:color w:val="{color}"/><w:sz w:val="{size}"/>'
            f'<w:szCs w:val="{size}"/></w:rPr></w:style>'
        )

    heading_sizes = {1: (36, 240, 120), 2: (30, 200, 100), 3: (26, 160, 80),
                     4: (24, 140, 60), 5: (22, 120, 60), 6: (22, 120, 60)}
    headings = "".join(
        para_style(
            f"Heading{level}", f"heading {level}",
            size=size, bold=True, outline=level - 1,
            spacing_before=before, spacing_after=after, color="1F3864",
        )
        for level, (size, before, after) in heading_sizes.items()
    )

    # Code is its own paragraph style rather than just mono runs inside a Normal
    # paragraph. That is what lets a reader -- and the verifier -- tell a code
    # block from prose without guessing from the font, and it is why one code
    # block is exactly one paragraph however many lines it holds.
    code_style = (
        '<w:style w:type="paragraph" w:styleId="Code">'
        '<w:name w:val="Code"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>'
        '<w:qFormat/>'
        '<w:pPr><w:keepLines/><w:spacing w:before="0" w:after="120"/>'
        '<w:shd w:val="clear" w:fill="F2F2F2"/></w:pPr>'
        '<w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/>'
        '<w:sz w:val="18"/><w:szCs w:val="18"/><w:color w:val="333333"/></w:rPr>'
        "</w:style>"
    )

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:styles xmlns:w="{W_NS}">'
        '<w:docDefaults><w:rPrDefault><w:rPr>'
        '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>'
        '<w:sz w:val="22"/><w:szCs w:val="22"/>'
        "</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>"
        '<w:spacing w:after="120" w:line="276" w:lineRule="auto"/>'
        "</w:pPr></w:pPrDefault></w:docDefaults>"
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        '<w:name w:val="Normal"/><w:qFormat/></w:style>'
        + headings
        + code_style
        + '<w:style w:type="table" w:styleId="TableGrid">'
        '<w:name w:val="Table Grid"/><w:tblPr>'
        '<w:tblBorders>'
        '<w:top w:val="single" w:sz="4" w:color="BFBFBF"/>'
        '<w:left w:val="single" w:sz="4" w:color="BFBFBF"/>'
        '<w:bottom w:val="single" w:sz="4" w:color="BFBFBF"/>'
        '<w:right w:val="single" w:sz="4" w:color="BFBFBF"/>'
        '<w:insideH w:val="single" w:sz="4" w:color="BFBFBF"/>'
        '<w:insideV w:val="single" w:sz="4" w:color="BFBFBF"/>'
        "</w:tblBorders></w:tblPr></w:style>"
        "</w:styles>"
    )


def _docx_numbering() -> str:
    def abstract(num_id: int, fmt: str, text: str) -> str:
        fonts = (
            '<w:rPr><w:rFonts w:ascii="Symbol" w:hAnsi="Symbol" w:hint="default"/></w:rPr>'
            if fmt == "bullet" else ""
        )
        return (
            f'<w:abstractNum w:abstractNumId="{num_id}">'
            '<w:multiLevelType w:val="hybridMultilevel"/>'
            '<w:lvl w:ilvl="0"><w:start w:val="1"/>'
            f'<w:numFmt w:val="{fmt}"/><w:lvlText w:val="{text}"/>'
            '<w:lvlJc w:val="left"/>'
            '<w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr>'
            f"{fonts}</w:lvl></w:abstractNum>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:numbering xmlns:w="{W_NS}">'
        + abstract(0, "bullet", "•")
        + abstract(1, "decimal", "%1.")
        + '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
        + '<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>'
        "</w:numbering>"
    )


def write_docx(title: str, blocks: list[Block]) -> tuple[bytes, dict[str, Any]]:
    """A .docx whose content is the blocks. Returns (bytes, claimed counts)."""
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W_NS}"'
        ' xmlns:r="' + OFFICE_REL + '"'
        ' xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
        f"<w:body>{_docx_body(blocks)}</w:body></w:document>"
    )

    parts: dict[str, str | bytes] = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Types xmlns="{CT_NS}">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/word/document.xml" ContentType="{_DOCX_MAIN}"/>'
            '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
            '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{REL_NS}">'
            f'<Relationship Id="rId1" Type="{OFFICE_REL}/officeDocument" Target="word/document.xml"/>'
            f'<Relationship Id="rId2" Type="{CORE_REL}" Target="docProps/core.xml"/>'
            "</Relationships>"
        ),
        "docProps/core.xml": _core_properties(title, "a Word document"),
        "word/document.xml": document,
        "word/styles.xml": _docx_styles(),
        "word/numbering.xml": _docx_numbering(),
        "word/_rels/document.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{REL_NS}">'
            f'<Relationship Id="rId1" Type="{OFFICE_REL}/styles" Target="styles.xml"/>'
            f'<Relationship Id="rId2" Type="{OFFICE_REL}/numbering" Target="numbering.xml"/>'
            "</Relationships>"
        ),
    }

    claimed = {
        "format": "docx",
        "paragraphs": sum(1 for b in blocks if b.kind == "paragraph"),
        "code_blocks": sum(1 for b in blocks if b.kind == "code"),
        "headings": sum(1 for b in blocks if b.kind == "heading"),
        "list_items": sum(len(b.items) for b in blocks if b.kind in ("bullets", "numbered")),
        "tables": sum(1 for b in blocks if b.kind == "table"),
        "table_rows": sum(len(b.rows) for b in blocks if b.kind == "table"),
        "parts": len(parts),
    }
    return _zip(parts), claimed


# =============================================================== XLSX


def _col_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _looks_numeric(text: str) -> bool:
    """Whether a cell should be written as a number rather than as text.

    Deliberately strict. ``007``, ``1,234`` and ``2024-01-01`` are all strings a
    person means literally, and writing them as numbers would silently change
    them -- an account number losing its leading zeros is the classic version of
    this bug and it is unrecoverable once the file is saved.
    """
    stripped = text.strip()
    if not stripped or stripped.startswith("="):
        return False
    if stripped.startswith("-"):
        stripped = stripped[1:]
    if stripped.isdigit():
        # A leading zero means the value is an identifier, not a quantity.
        return stripped == "0" or not stripped.startswith("0")
    if stripped.count(".") == 1:
        whole, _, fraction = stripped.partition(".")
        return whole.isdigit() and (whole == "0" or not whole.startswith("0")) and fraction.isdigit()
    return False


def _xlsx_cell(ref: str, value: str) -> str:
    if value.startswith("="):
        # A formula is stored without a cached result. Excel and LibreOffice both
        # recalculate on open. Writing a made-up <v> would be a wrong number
        # sitting in the file that the application has no reason to recompute --
        # which is worse than a blank it will fill in.
        return f'<c r="{ref}"><f>{_esc(value[1:])}</f></c>'
    if _looks_numeric(value):
        return f'<c r="{ref}"><v>{_esc(value.strip())}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{_esc(value)}</t></is></c>'


def _xlsx_sheet(blocks: list[Block]) -> tuple[str, dict[str, Any]]:
    """One worksheet. Tables and lists become rows; everything else becomes text rows."""
    grid: list[list[str]] = []
    for block in blocks:
        if block.kind == "table":
            if block.headers:
                grid.append(list(block.headers))
            grid.extend(list(row) for row in block.rows)
        elif block.kind in ("bullets", "numbered"):
            for item in block.items:
                grid.append([item])
        elif block.kind == "heading":
            grid.append([block.text])
        elif block.kind == "paragraph":
            grid.append([block.text])
        elif block.kind == "code":
            grid.extend([line] if line else [""] for line in block.text.splitlines())
        elif block.kind == "page_break":
            grid.append([])

    rows_xml = []
    for row_index, row in enumerate(grid, start=1):
        cells = "".join(
            _xlsx_cell(f"{_col_letter(col)}{row_index}", value)
            for col, value in enumerate(row)
        )
        rows_xml.append(f'<row r="{row_index}">{cells}</row>')

    width = max((len(r) for r in grid), default=0)
    dimension = f"A1:{_col_letter(max(width - 1, 0))}{max(len(grid), 1)}"
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{SS_NS}">'
        f'<dimension ref="{dimension}"/>'
        f"<sheetData>{''.join(rows_xml)}</sheetData>"
        "</worksheet>"
    )
    filled = sum(1 for row in grid if any(c.strip() for c in row))
    return sheet, {"rows": len(grid), "filled_rows": filled, "columns": width}


def write_xlsx(title: str, blocks: list[Block]) -> tuple[bytes, dict[str, Any]]:
    sheet, stats = _xlsx_sheet(blocks)
    parts: dict[str, str | bytes] = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Types xmlns="{CT_NS}">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/xl/workbook.xml" ContentType="{_XLSX_MAIN}"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{REL_NS}">'
            f'<Relationship Id="rId1" Type="{OFFICE_REL}/officeDocument" Target="xl/workbook.xml"/>'
            f'<Relationship Id="rId2" Type="{CORE_REL}" Target="docProps/core.xml"/>'
            "</Relationships>"
        ),
        "docProps/core.xml": _core_properties(title, "a spreadsheet"),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{SS_NS}" xmlns:r="{OFFICE_REL}">'
            f"<sheets><sheet name={quoteattr(title[:31] or 'Sheet1')} sheetId=\"1\" r:id=\"rId1\"/></sheets>"
            "</workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{REL_NS}">'
            f'<Relationship Id="rId1" Type="{OFFICE_REL}/worksheet" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{OFFICE_REL}/styles" Target="styles.xml"/>'
            "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": sheet,
        "xl/styles.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<styleSheet xmlns="{SS_NS}">'
            '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
            "</styleSheet>"
        ),
    }
    claimed = {"format": "xlsx", "parts": len(parts), **stats}
    return _zip(parts), claimed


# =============================================================== PPTX

_SLIDE_W = 9144000  # 10 inches, in EMU
_SLIDE_H = 6858000  # 7.5 inches


def _pptx_theme() -> str:
    """A theme with every list filled to the length the spec requires.

    Word and PowerPoint reject a theme whose ``fmtScheme`` has fewer than three
    fill, line, effect and background styles. They are not decorative here -- they
    are the shape of the part -- so they are repeated rather than trimmed.
    """
    solid = '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    line = (
        '<a:ln w="6350" cap="flat" cmpd="sng" algn="ctr">'
        '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '<a:prstDash val="solid"/></a:ln>'
    )
    accents = ["0F6FC5", "2E9BE6", "1F3864", "63B3ED", "8AB4D8", "C9D9E8"]
    clr = (
        '<a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>'
        '<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>'
        '<a:dk2><a:srgbClr val="1F1F1F"/></a:dk2>'
        '<a:lt2><a:srgbClr val="EEEEEE"/></a:lt2>'
        + "".join(f'<a:accent{i}><a:srgbClr val="{c}"/></a:accent{i}>'
                  for i, c in enumerate(accents, start=1))
        + '<a:hlink><a:srgbClr val="0563C1"/></a:hlink>'
        '<a:folHlink><a:srgbClr val="954F72"/></a:folHlink>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<a:theme xmlns:a="{A_NS}" name="NEXUS"><a:themeElements>'
        f'<a:clrScheme name="NEXUS">{clr}</a:clrScheme>'
        '<a:fontScheme name="NEXUS">'
        '<a:majorFont><a:latin typeface="Calibri Light"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>'
        '<a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont>'
        "</a:fontScheme>"
        "<a:fmtScheme name=\"NEXUS\">"
        f"<a:fillStyleLst>{solid}{solid}{solid}</a:fillStyleLst>"
        f"<a:lnStyleLst>{line}{line}{line}</a:lnStyleLst>"
        "<a:effectStyleLst>"
        '<a:effectStyle><a:effectLst/></a:effectStyle>'
        '<a:effectStyle><a:effectLst/></a:effectStyle>'
        '<a:effectStyle><a:effectLst/></a:effectStyle>'
        "</a:effectStyleLst>"
        f"<a:bgFillStyleLst>{solid}{solid}{solid}</a:bgFillStyleLst>"
        "</a:fmtScheme></a:themeElements></a:theme>"
    )


def _pptx_textbox(shape_id: int, name: str, x: int, y: int, cx: int, cy: int,
                  paragraphs: list[tuple[str, bool]], *, size: int, bold: bool, color: str) -> str:
    """A plain text box holding (text, is_bullet) paragraphs.

    Not a placeholder. A placeholder inherits its geometry from the slide layout,
    which means a blank layout inherits nothing useful and the text lands on top
    of itself -- and a layout that does define them is a great deal more XML for
    no benefit here. Explicit coordinates are both simpler and more predictable.

    Bullets are real ``<a:buChar>`` formatting, not a "•  " typed into the text.
    A typed bullet is not a bullet: PowerPoint has no idea the line is a list
    item, selecting the text copies the glyph along with it, and changing the
    list's indent or marker style does nothing. The glyph in the file and the
    glyph on the slide look identical, which is exactly what makes it worth
    getting right rather than looking right.
    """
    def paragraph(text: str, bullet: bool) -> str:
        props = '<a:pPr algn="l" marL="285750" indent="-285750" lvl="0">'
        props += '<a:buChar char="•"/>' if bullet else "<a:buNone/>"
        props += "</a:pPr>"
        return (
            f"<a:p>{props}"
            f'<a:r><a:rPr lang="en-US" sz="{size}" b="{1 if bold else 0}" dirty="0">'
            f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'
            f'<a:latin typeface="Calibri"/></a:rPr><a:t>{_esc(text)}</a:t></a:r></a:p>'
        )

    body = "".join(
        paragraph(text, bullet) for text, bullet in (paragraphs or [("", False)])
    )
    return (
        "<p:sp><p:nvSpPr>"
        f'<p:cNvPr id="{shape_id}" name={quoteattr(name)}/>'
        '<p:cNvSpPr txBox="1"><a:spLocks noGrp="1"/></p:cNvSpPr>'
        "<p:nvPr/></p:nvSpPr>"
        "<p:spPr>"
        f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/>'
        "</p:spPr>"
        f'<p:txBody><a:bodyPr wrap="square" lIns="91440" tIns="45720" rIns="91440" bIns="45720">'
        "<a:normAutofit/></a:bodyPr><a:lstStyle/>"
        f"{body}</p:txBody></p:sp>"
    )


def _pptx_sp_tree(inner: str) -> str:
    return (
        "<p:spTree>"
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
        '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        f"{inner}</p:spTree>"
    )


def _pptx_slide(title: str, items: list[tuple[str, bool]]) -> str:
    """One slide: a title, then the body paragraphs under it."""
    shapes: list[str] = []
    shapes.append(
        _pptx_textbox(
            2, "Title", 457200, 457200, _SLIDE_W - 914400, 900000,
            [(title, False)], size=3200, bold=True, color="1F3864",
        )
    )
    if items:
        # Long bullet lists are split across slides by the caller; this box shows
        # what it is given, so the body height is generous but finite.
        shapes.append(
            _pptx_textbox(
                3, "Body", 457200, 1500000, _SLIDE_W - 914400, _SLIDE_H - 1950000,
                items, size=1800, bold=False, color="262626",
            )
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:sld xmlns:a="{A_NS}" xmlns:r="{OFFICE_REL}" xmlns:p="{P_NS}">'
        f"<p:cSld>{_pptx_sp_tree(''.join(shapes))}</p:cSld>"
        "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>"
    )


def write_pptx(title: str, blocks: list[Block]) -> tuple[bytes, dict[str, Any]]:
    """A deck: one slide per heading, with the following content as its body."""
    # A slide title needs a heading; if the document has no headings at all the
    # next best thing is the document title, and the whole body becomes slide one.
    slides: list[tuple[str, list[tuple[str, bool]]]] = []
    current_title: str | None = None
    current_items: list[tuple[str, bool]] = []

    for block in blocks:
        if block.kind == "heading":
            if current_title is not None:
                slides.append((current_title, current_items))
            current_title = block.text
            current_items = []
        elif block.kind in ("bullets", "numbered"):
            # Only a real list gets a real bullet. A table row and a paragraph are
            # lines of their own, and marking them as list items would tell
            # PowerPoint something untrue about the document.
            current_items.extend((item, True) for item in block.items)
        elif block.kind == "table":
            if block.headers:
                current_items.append((" | ".join(block.headers), False))
            current_items.extend((" | ".join(row), False) for row in block.rows)
        elif block.kind == "paragraph":
            current_items.append((block.text, False))
        elif block.kind == "code":
            current_items.extend((line, False) for line in block.text.splitlines())
    if current_title is not None:
        slides.append((current_title, current_items))
    if not slides:
        slides = [(title or "Document", [(b.text, False) for b in blocks if b.text])]

    # Split anything that would not fit: a slide with sixty bullets is a wall of
    # text that no reader can use, so the overflow becomes its own slide with the
    # same title continued.
    MAX_PER_SLIDE = 12
    expanded: list[tuple[str, list[tuple[str, bool]]]] = []
    for slide_title, items in slides:
        if len(items) <= MAX_PER_SLIDE:
            expanded.append((slide_title, items))
            continue
        chunks = [items[i:i + MAX_PER_SLIDE] for i in range(0, len(items), MAX_PER_SLIDE)]
        for index, chunk in enumerate(chunks):
            expanded.append((slide_title if index == 0 else f"{slide_title} (cont.)", chunk))
    slides = expanded

    parts: dict[str, str | bytes] = {}
    overrides = "".join(
        f'<Override PartName="/ppt/slides/slide{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for i in range(1, len(slides) + 1)
    )
    parts["[Content_Types].xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Types xmlns="{CT_NS}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/ppt/presentation.xml" ContentType="{_PPTX_MAIN}"/>'
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        + overrides
        + "</Types>"
    )
    parts["_rels/.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{REL_NS}">'
        f'<Relationship Id="rId1" Type="{OFFICE_REL}/officeDocument" Target="ppt/presentation.xml"/>'
        f'<Relationship Id="rId2" Type="{CORE_REL}" Target="docProps/core.xml"/>'
        "</Relationships>"
    )
    parts["docProps/core.xml"] = _core_properties(title, "a presentation")

    # rId1 is the master; slides start at rId2 and are one per slide in order.
    slide_rels = "".join(
        f'<Relationship Id="rId{i + 2}" Type="{OFFICE_REL}/slide" Target="slides/slide{i}.xml"/>'
        for i in range(1, len(slides) + 1)
    )
    parts["ppt/_rels/presentation.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{REL_NS}">'
        f'<Relationship Id="rId1" Type="{OFFICE_REL}/slideMaster" Target="slideMasters/slideMaster1.xml"/>'
        + slide_rels
        + "</Relationships>"
    )
    slide_ids = "".join(
        f'<p:sldId id="{255 + i}" r:id="rId{i + 1}"/>' for i in range(1, len(slides) + 1)
    )
    parts["ppt/presentation.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:presentation xmlns:a="{A_NS}" xmlns:r="{OFFICE_REL}" xmlns:p="{P_NS}" saveSubsetFonts="1">'
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        f"<p:sldIdLst>{slide_ids}</p:sldIdLst>"
        f'<p:sldSz cx="{_SLIDE_W}" cy="{_SLIDE_H}" type="screen4x3"/>'
        '<p:notesSz cx="6858000" cy="9144000"/>'
        "</p:presentation>"
    )
    parts["ppt/slideMasters/slideMaster1.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:sldMaster xmlns:a="{A_NS}" xmlns:r="{OFFICE_REL}" xmlns:p="{P_NS}">'
        f"<p:cSld>{_pptx_sp_tree('')}</p:cSld>"
        '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" '
        'accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" '
        'hlink="hlink" folHlink="folHlink"/>'
        '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
        "</p:sldMaster>"
    )
    parts["ppt/slideMasters/_rels/slideMaster1.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{REL_NS}">'
        f'<Relationship Id="rId1" Type="{OFFICE_REL}/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
        f'<Relationship Id="rId2" Type="{OFFICE_REL}/theme" Target="../theme/theme1.xml"/>'
        "</Relationships>"
    )
    parts["ppt/slideLayouts/slideLayout1.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:sldLayout xmlns:a="{A_NS}" xmlns:r="{OFFICE_REL}" xmlns:p="{P_NS}" type="blank" preserve="1">'
        f'<p:cSld name="Blank">{_pptx_sp_tree("")}</p:cSld>'
        "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>"
    )
    parts["ppt/slideLayouts/_rels/slideLayout1.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{REL_NS}">'
        f'<Relationship Id="rId1" Type="{OFFICE_REL}/slideMaster" Target="../slideMasters/slideMaster1.xml"/>'
        "</Relationships>"
    )
    parts["ppt/theme/theme1.xml"] = _pptx_theme()

    for index, (slide_title, items) in enumerate(slides, start=1):
        parts[f"ppt/slides/slide{index}.xml"] = _pptx_slide(slide_title, items)
        parts[f"ppt/slides/_rels/slide{index}.xml.rels"] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{REL_NS}">'
            f'<Relationship Id="rId1" Type="{OFFICE_REL}/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
            "</Relationships>"
        )

    claimed = {
        "format": "pptx",
        "slides": len(slides),
        "slide_titles": [s[0] for s in slides],
        "body_items": sum(len(s[1]) for s in slides),
        "parts": len(parts),
    }
    return _zip(parts), claimed
