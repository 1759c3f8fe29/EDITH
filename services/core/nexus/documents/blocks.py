"""The document model, shared by every writer in this package.

One shape in, four formats out. The alternative -- letting each writer invent its
own input -- would mean the model has to learn a different way to ask for a
heading depending on which file it wanted, which is the sort of detail that
belongs in code rather than in a prompt.

A document is a list of blocks. Each block is a small dict:

    {"type": "heading",  "level": 1, "text": "Quarterly summary"}
    {"type": "paragraph", "text": "..."}
    {"type": "bullets",  "items": ["...", "..."]}
    {"type": "table",    "headers": ["A", "B"], "rows": [["1", "2"]]}
    {"type": "page_break"}

``blocks_from`` normalises anything reasonable into that list, because a model
asked for "a report" will produce whichever of these shapes it finds natural and
a normaliser that rejected the near-misses would make the tool feel broken for no
real reason. A bare string becomes paragraphs; a block missing its ``type`` is
inferred from the keys it does have.

This module is deliberately free of both XML and PDF: it is the vocabulary, and
it stays testable without writing a file.
"""

from __future__ import annotations

from typing import Any

#: The block types every writer must handle. Kept as a tuple so the schema
#: advertised to the model, the validator here, and the writers cannot drift:
#: a type added to one and not the others would be silently dropped.
KINDS = ("heading", "paragraph", "bullets", "numbered", "table", "page_break", "code")

MAX_BLOCKS = 2000
MAX_TEXT = 20_000


class BlockError(ValueError):
    """The content cannot be turned into a document, and here is why."""


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(_text(v) for v in value)
    return str(value)


def _infer_kind(item: dict[str, Any]) -> str:
    """Work out the type of a block that did not say.

    The order matters: ``rows`` is checked before ``items`` because a table given
    as ``{"items": [...]}`` is far more likely to be a mislabelled table than a
    bullet list, and guessing "bullets" would silently flatten a real table into
    one long line.
    """
    if "rows" in item:
        return "table"
    if "headers" in item:
        return "table"
    if "items" in item:
        return "bullets"
    if "text" in item:
        return "paragraph"
    return ""


def _normalise_block(raw: Any) -> Block | None:
    if isinstance(raw, str):
        text = raw.strip()
        return Block("paragraph", text=text) if text else None

    if not isinstance(raw, dict):
        raise BlockError(
            f"A block must be text or an object, but this one is {type(raw).__name__}."
        )

    kind = str(raw.get("type") or raw.get("kind") or "").strip().lower()
    if not kind:
        kind = _infer_kind(raw)
    if kind in ("bullet", "bullets", "list"):
        kind = "bullets"
    elif kind in ("numbered", "number", "ordered"):
        kind = "numbered"
    elif kind in ("para", "paragraph", "text", "body"):
        kind = "paragraph"
    elif kind in ("h1", "h2", "h3", "heading", "title", "subtitle"):
        if kind in ("h1", "h2", "h3"):
            raw = {**raw, "level": int(kind[1])}
        kind = "heading"
    elif kind in ("table", "grid"):
        kind = "table"
    elif kind in ("page_break", "pagebreak", "break"):
        return Block("page_break")
    elif kind in ("code", "pre", "monospace"):
        kind = "code"

    if kind not in KINDS:
        raise BlockError(
            f"There is no block type called {kind!r}. The types are: {', '.join(KINDS)}."
        )

    if kind == "page_break":
        return Block("page_break")

    if kind == "heading":
        try:
            level = int(raw.get("level", 1))
        except (TypeError, ValueError):
            level = 1
        level = 1 if level < 1 else (6 if level > 6 else level)  # noqa: SIM108
        return Block("heading", text=_text(raw.get("text"))[:MAX_TEXT], level=level)

    if kind in ("bullets", "numbered"):
        items = raw.get("items")
        if isinstance(items, str):
            items = [line for line in items.splitlines() if line.strip()]
        if not isinstance(items, (list, tuple)):
            raise BlockError(f"A {kind} block needs a list of items.")
        return Block(kind, items=[_text(i) for i in items][:MAX_BLOCKS])

    if kind == "table":
        headers = raw.get("headers") or raw.get("columns") or []
        rows = raw.get("rows") or raw.get("data") or []
        if not isinstance(headers, (list, tuple)):
            raise BlockError("A table's headers must be a list.")
        if not isinstance(rows, (list, tuple)):
            raise BlockError("A table's rows must be a list of rows.")
        width = len(headers)
        cleaned: list[list[str]] = []
        for row in rows:
            cells = [_text(c) for c in (row if isinstance(row, (list, tuple)) else [row])]
            if width and len(cells) > width:
                # Ragged rows are returned to width rather than padded: a table
                # with a half-empty column reads as a mistake, and silently
                # dropping the overflow would hide a malformed request.
                cells = cells[:width]
            cleaned.append(cells)
        return Block("table", headers=[_text(h) for h in headers], rows=cleaned)

    if kind == "code":
        return Block("code", text=_text(raw.get("text"))[:MAX_TEXT])

    # Everything else is prose. "paragraph" is the deliberate default rather than
    # another branch of the elif chain: an unrecognised-but-mapped kind that fell
    # through to `code` here once turned every paragraph in every format
    # monospaced, and the loose counting downstream hid it.
    return Block("paragraph", text=_text(raw.get("text"))[:MAX_TEXT])


def blocks_from(content: Any) -> list[Block]:
    """Turn whatever was asked for into a list of blocks, or refuse clearly."""
    if content is None:
        raise BlockError("There is no content to put in the document.")

    if isinstance(content, str):
        # A bare string is the common case: the model wrote prose, not structure.
        # Blank-line-separated chunks become paragraphs, a line beginning with
        # "- " starts a bullet list, and a line of "# " starts a heading. This is
        # enough Markdown to be useful without pretending to be a Markdown parser.
        blocks: list[Block] = []
        paragraph: list[str] = []
        bullets: list[str] = []

        def flush() -> None:
            if paragraph:
                blocks.append(Block("paragraph", text=" ".join(paragraph).strip()))
                paragraph.clear()
            if bullets:
                blocks.append(Block("bullets", items=list(bullets)))
                bullets.clear()

        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                flush()
                continue
            if stripped.startswith(("- ", "* ")):
                if paragraph:
                    blocks.append(Block("paragraph", text=" ".join(paragraph).strip()))
                    paragraph.clear()
                bullets.append(stripped[2:].strip())
                continue
            if stripped.startswith("#"):
                flush()
                hashes = len(stripped) - len(stripped.lstrip("#"))
                blocks.append(
                    Block("heading", text=stripped.lstrip("#").strip(), level=min(hashes, 6))
                )
                continue
            if bullets:
                blocks.append(Block("bullets", items=list(bullets)))
                bullets.clear()
            paragraph.append(stripped)
        flush()
        if not blocks:
            raise BlockError("The content was empty once whitespace was removed.")
        return blocks

    if isinstance(content, dict):
        content = [content]

    if not isinstance(content, (list, tuple)):
        raise BlockError(
            f"Content must be text or a list of blocks, but this is {type(content).__name__}."
        )

    out: list[Block] = []
    for raw in content:
        block = _normalise_block(raw)
        if block is not None:
            out.append(block)
    if not out:
        raise BlockError("The content was empty once whitespace was removed.")
    if len(out) > MAX_BLOCKS:
        raise BlockError(
            f"That document has {len(out)} blocks; the most NEXUS will write is {MAX_BLOCKS}."
        )
    return out


def describe(blocks: list[Block]) -> dict[str, int]:
    """Count what is actually in the document, for the verifier to check against.

    Paragraphs and code blocks are counted separately on purpose. Summing them
    into one "text blocks" figure once hid a bug that made every paragraph render
    monospaced in all four formats: the total was right, so nothing looked wrong.
    A count that cannot distinguish two different things cannot be used to check
    that either of them is correct.
    """
    return {
        "blocks": len(blocks),
        "headings": sum(1 for b in blocks if b.kind == "heading"),
        "paragraphs": sum(1 for b in blocks if b.kind == "paragraph"),
        "code_blocks": sum(1 for b in blocks if b.kind == "code"),
        "lists": sum(1 for b in blocks if b.kind in ("bullets", "numbered")),
        "list_items": sum(len(b.items) for b in blocks if b.kind in ("bullets", "numbered")),
        "tables": sum(1 for b in blocks if b.kind == "table"),
        "table_rows": sum(len(b.rows) + (1 if b.headers else 0) for b in blocks if b.kind == "table"),
        "words": sum(len(b.all_text().split()) for b in blocks),
    }


class Block:
    """One piece of a document. Immutable, because nothing edits a document.

    Frozen on purpose: the writers receive blocks and return bytes, and a writer
    that mutated its input would make a second write with the same blocks produce
    a different file.
    """

    __slots__ = ("kind", "text", "level", "items", "headers", "rows")

    def __init__(
        self,
        kind: str,
        *,
        text: str = "",
        level: int = 1,
        items: list[str] | None = None,
        headers: list[str] | None = None,
        rows: list[list[str]] | None = None,
    ) -> None:
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "level", level)
        object.__setattr__(self, "items", tuple(items or ()))
        object.__setattr__(self, "headers", tuple(headers or ()))
        object.__setattr__(self, "rows", tuple(tuple(r) for r in (rows or ())))

    def __setattr__(self, name: str, value: Any) -> None:  # pragma: no cover - guard
        raise AttributeError("Blocks are immutable once built.")

    def all_text(self) -> str:
        parts = [self.text, *self.items, *self.headers]
        for row in self.rows:
            parts.extend(row)
        return " ".join(p for p in parts if p)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Block({self.kind!r}, text={self.text[:30]!r}, items={len(self.items)})"
