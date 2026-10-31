"""Writing and reading documents (spec §5, O15)...

Two tools, and they are deliberately different sizes.

``documents.create`` writes a docx, xlsx, pptx or pdf and then **proves** it:
the verifier reopens the file from disk, parses it as a reader would, counts the
paragraphs, rows and slides that are really in it, and compares those counts
with what the writer claimed. A writer that reports success has told you what it
intended; only the read-back says what happened. If the two disagree the result
is ``ok`` with ``verified=False`` -- the file is on disk, and saying otherwise
would be a lie in the other direction.

``documents.read`` extracts the text of a document so NEXUS can check its own
work later, or read something the user already has. It says plainly what it
cannot read: a PDF whose producer drew its text as curves, or embedded a font
subset with a custom encoding, has no extractable text, and that is reported
rather than returned as an empty document.

The capability is ``file.write`` / ``file.read``, not a new one. A document
*is* a file; a second name for the same power would prompt the user twice for
one consent, which is the opposite of what the closed capability set is for.
Paths therefore go through :func:`~nexus.permissions.policy.resolve_path` exactly
as ``files.py`` does, and the same protected-path floor and scope checks apply.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import settings
from ..documents import KINDS, blocks_from, describe, verify_document
from ..documents import write_docx, write_pdf, write_pptx, write_xlsx
from ..documents.blocks import BlockError
from ..documents.read import DEFAULT_LIMIT, MAX_LIMIT, read_document
from ..logging_setup import get_logger
from ..permissions.policy import resolve_path
from .registry import ToolResult, tool

log = get_logger("tools.documents")

#: Writer per kind. The same table drives the schema's enum, so a kind the tool
#: advertises and a kind it can write cannot drift apart.
_WRITERS = {
    "docx": write_docx,
    "xlsx": write_xlsx,
    "pptx": write_pptx,
    "pdf": write_pdf,
}

_SUFFIX_BY_KIND = {"docx": ".docx", "xlsx": ".xlsx", "pptx": ".pptx", "pdf": ".pdf"}


def _resolve_target(raw: str, kind: str, title: str) -> tuple[Path, str]:
    """Turn what was asked for into an absolute path, and a name to show.

    A bare file name lands in the documents directory rather than in the working
    directory, because "make me a report" means the user's Documents folder and
    not wherever the core process happens to have been started. A path with a
    directory in it is honoured as given.
    """
    text = str(raw).strip()
    directory = settings().documents_dir
    candidate = Path(text).expanduser()

    if not candidate.name or candidate.name in (".", ".."):
        # No usable name was given -- only a folder, or nothing at all. Falling
        # through would write a file called ".docx", which is a hidden file with
        # no name at all. The title is the obvious thing to name it after.
        slug = "".join(
            char if (char.isalnum() or char in " -_") else "-"
            for char in title.lower()
        ).strip().replace(" ", "-")
        candidate = Path((slug or "document") + _SUFFIX_BY_KIND[kind])

    if candidate.is_absolute() or candidate.parent != Path("."):
        target = resolve_path(str(candidate)).path
    else:
        target = resolve_path(str(directory / candidate.name)).path

    # The extension decides the format, so a name without one gets the right one
    # rather than being written as a .docx with a .pdf header in it.
    if target.suffix.lower() != _SUFFIX_BY_KIND[kind]:
        if target.suffix.lower() in _SUFFIX_BY_KIND.values():
            # The caller named a different format's extension. Changing it
            # silently would write a file at a path that was not asked for, so
            # the mismatch is refused instead.
            raise BlockError(
                f"The path ends in {target.suffix} but the kind asked for is "
                f"{kind!r}. Use {_SUFFIX_BY_KIND[kind]} or leave the extension off."
            )
        target = target.with_name(target.name + _SUFFIX_BY_KIND[kind])

    display = resolve_path(str(target)).display
    return target, display


def _verify_written(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    """Reopen the file and check it against what was asked for.

    The comparison uses the *request*, not the writer's own claim. Both are in the
    result and the request wins where they disagree, because the writer's claim is
    the thing under test -- checking a writer against itself would pass a writer
    that dropped half the document and said so.
    """
    if not result.data or not result.data.get("path"):
        return False, "No document path was recorded, so there is nothing to verify."
    path = Path(result.data["path"])
    if not path.exists():
        return False, f"{path} does not exist, so nothing was written."
    expected = {**result.data.get("claimed", {}), **result.data.get("requested", {})}
    ok, detail = verify_document(path, expected)
    return ok, detail


@tool(
    "documents.create",
    title="Create a document",
    description=(
        "Write a Word (.docx), Excel (.xlsx), PowerPoint (.pptx) or PDF document. "
        "Give the content as a list of blocks: {type: 'heading', level, text}, "
        "{type: 'paragraph', text}, {type: 'bullets', items}, "
        "{type: 'numbered', items}, {type: 'table', headers, rows}, "
        "{type: 'code', text}, or {type: 'page_break'}. Plain text with blank-line "
        "paragraphs, '- ' bullets and '# ' headings also works. A bare file name "
        "like 'report.pdf' is saved in the NEXUS documents folder; give a full "
        "path to save somewhere else. The file is reopened and checked afterwards."
    ),
    capability="file.write",
    idempotent=False,
    verifier=_verify_written,
    timeout_s=60.0,
    parameters={
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": sorted(_WRITERS),
                "description": "Which format to write.",
            },
            "path": {
                "type": "string",
                "description": (
                    "Where to save it. A name with no folder goes in the NEXUS "
                    "documents folder; a full path is used as given."
                ),
            },
            "title": {
                "type": "string",
                "description": "The document's title, stored in its properties.",
            },
            "content": {
                "description": (
                    "The document body: a list of blocks, or plain text. "
                    "Markdown-ish plain text is accepted and split into "
                    "paragraphs, bullets and headings."
                ),
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "object"}},
                    {"type": "object"},
                ],
            },
        },
        "required": ["kind", "path", "content"],
    },
)
def documents_create(params: dict[str, Any]) -> ToolResult:
    kind = str(params.get("kind") or "").strip().lower()
    if kind not in _WRITERS:
        return ToolResult(
            ok=False,
            error=f"There is no document kind called {kind!r}. "
                  f"The kinds are: {', '.join(sorted(_WRITERS))}.",
        )

    title = str(params.get("title") or "").strip() or "Untitled"

    try:
        blocks = blocks_from(params.get("content"))
    except BlockError as exc:
        return ToolResult(ok=False, error=str(exc))
    if not blocks:
        return ToolResult(ok=False, error="The document would be empty.")

    try:
        target, display = _resolve_target(str(params.get("path") or ""), kind, title)
    except BlockError as exc:
        return ToolResult(ok=False, error=str(exc))

    if target.is_dir():
        return ToolResult(ok=False, error=f"{display} is a folder, not a file.")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return ToolResult(ok=False, error=f"Could not create {target.parent}: {exc}")

    try:
        data, claimed = _WRITERS[kind](title, blocks)
    except Exception as exc:  # noqa: BLE001 -- a writer crash is a failed write
        log.warning("document writer failed", extra={"kind": kind, "error": str(exc)})
        return ToolResult(ok=False, error=f"The {kind.upper()} could not be written: {exc}")

    requested = describe(blocks)

    # Written to a sibling temp file and renamed, exactly as files.write does, so
    # an interrupted write cannot leave a half-written document in the user's
    # folder that looks like a real one.
    tmp = target.with_name(f".{target.name}.nexus-tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(target)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return ToolResult(ok=False, error=f"Could not write {display}: {exc}")

    size = target.stat().st_size
    lost = int(claimed.get("characters_lost") or 0)
    summary = f"Wrote {display} — {size:,} bytes."
    if lost:
        # §78: say what was lost rather than shipping a document with silent
        # question marks where the user's characters used to be.
        summary += (
            f" {lost} character(s) could not be represented in this format's "
            "alphabet and were written as '?'."
        )

    return ToolResult(
        ok=True,
        data={
            "path": str(target),
            "display": display,
            "kind": kind,
            "size_bytes": size,
            # What the writer says it wrote, and what was asked for. The verifier
            # checks the file against the second and both are reported, so a
            # disagreement between them is visible rather than hidden by the
            # verification having passed.
            "claimed": claimed,
            "requested": requested,
            "characters_lost": lost,
        },
        summary=summary,
    )


@tool(
    "documents.read",
    title="Read a document",
    description=(
        "Read the text of a .docx, .xlsx, .pptx or .pdf file. Headings come back "
        "as lines starting with '#', list items as lines starting with '- ', and "
        "spreadsheet or table rows as tab-separated cells. This is reading order, "
        "not a byte-exact copy: use it to check what a document says, not to "
        "reproduce its exact formatting."
    ),
    capability="file.read",
    read_only=True,
    timeout_s=30.0,
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The document to read."},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_LIMIT,
                "default": DEFAULT_LIMIT,
                "description": "Most characters to return.",
            },
        },
        "required": ["path"],
    },
)
def documents_read(params: dict[str, Any]) -> ToolResult:
    resolved = resolve_path(str(params["path"]))
    target = resolved.path

    if not target.exists():
        return ToolResult(ok=False, error=f"{resolved.display} does not exist.")
    if target.is_dir():
        return ToolResult(
            ok=False,
            error=f"{resolved.display} is a folder. Use files.list to see what is in it.",
        )

    try:
        limit = int(params.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT

    try:
        text, stats = read_document(target, limit=limit)
    except Exception as exc:  # noqa: BLE001 -- an unreadable file is a failed read
        return ToolResult(ok=False, error=f"{resolved.display} could not be read: {exc}")

    if not text.strip():
        return ToolResult(
            ok=False,
            error=(
                f"{resolved.display} opened but contains no text. It may be an "
                "empty document, or one whose content is images."
            ),
        )

    shape = ", ".join(
        f"{key}={value}" for key, value in stats.items()
        if key not in ("format", "characters", "truncated") and not isinstance(value, list)
    )
    summary = f"Read {resolved.display} — {len(text):,} characters"
    if shape:
        summary += f" ({shape})"
    if stats.get("truncated"):
        summary += ". The text was cut off at the limit; raise it to read more."

    return ToolResult(
        ok=True,
        data={
            "path": str(target),
            "display": resolved.display,
            "text": text,
            "characters": len(text),
            "truncated": bool(stats.get("truncated")),
            **stats,
        },
        summary=summary,
    )


@tool(
    "documents.formats",
    title="List document formats",
    description=(
        "List the document formats NEXUS can write and read, and where written "
        "files go by default. Use it when unsure whether a format is supported."
    ),
    capability="file.read",
    read_only=True,
    parameters={"type": "object", "properties": {}},
)
def documents_formats(params: dict[str, Any]) -> ToolResult:
    directory = settings().documents_dir
    return ToolResult(
        ok=True,
        data={
            "kinds": list(KINDS),
            "formats": sorted(_WRITERS),
            "default_directory": str(directory),
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        },
        summary=(
            f"NEXUS writes and reads {', '.join(sorted(_WRITERS))}. "
            f"Files made with a bare name go to {directory}."
        ),
    )
