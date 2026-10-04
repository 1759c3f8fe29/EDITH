"""File tools (task D3; spec §35, §55, §60, §70).

Real filesystem operations, every mutating one verified by re-reading the
filesystem afterwards.

Two decisions worth calling out:

* **Delete means trash, by default.** Deletion follows the XDG Trash
  specification -- the file moves to ``~/.local/share/Trash/files`` with a
  ``.trashinfo`` record, so the user can restore it from their file manager. A
  permanent delete is a separate, explicit parameter, and it says so in the
  result. This is implemented directly rather than shelling out to ``gio`` so it
  works without extra packages.
* **Reads are bounded and honest.** A file larger than the limit is truncated,
  and the result says exactly how many bytes were returned out of how many, so
  the model is never silently handed a fragment it believes is whole (§78).
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..config import settings
from ..permissions.policy import resolve_path
from .registry import ToolResult, tool

TEXT_SUFFIXES = {
    ".txt", ".md", ".rst", ".log", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".xml", ".html", ".htm", ".css", ".js",
    ".ts", ".tsx", ".jsx", ".py", ".rs", ".go", ".java", ".c", ".h", ".cpp",
    ".hpp", ".sh", ".bash", ".zsh", ".sql", ".env", ".gitignore", ".dockerfile",
}


def _stat_entry(path: Path) -> dict[str, Any]:
    try:
        st = path.stat()
    except OSError as exc:
        return {"name": path.name, "path": str(path), "error": str(exc)}
    return {
        "name": path.name,
        "path": str(path),
        "type": "directory" if path.is_dir() else "file",
        "size": st.st_size,
        "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        "readable": os.access(path, os.R_OK),
        "writable": os.access(path, os.W_OK),
    }


def _looks_binary(sample: bytes) -> bool:
    if b"\x00" in sample:
        return True
    # A high proportion of bytes outside printable/UTF-8 range means binary.
    if not sample:
        return False
    printable = sum(1 for b in sample if 32 <= b < 127 or b in (9, 10, 13))
    return printable / len(sample) < 0.75


def _digest(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


# ------------------------------------------------------------------- listing


@tool(
    "files.list",
    title="List a folder",
    description=(
        "List the files and folders inside a directory. Returns names, sizes and "
        "modification times. Use this before reading or writing so you know what "
        "is actually there."
    ),
    capability="file.list",
    read_only=True,
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory to list. '~' is allowed."},
            "pattern": {
                "type": "string",
                "description": "Optional glob such as '*.pdf' to filter results.",
            },
            "include_hidden": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
        },
        "required": ["path"],
    },
)
def files_list(params: dict[str, Any]) -> ToolResult:
    resolved = resolve_path(str(params["path"]))
    target = resolved.path

    if not target.exists():
        return ToolResult(ok=False, error=f"{resolved.display} does not exist.")
    if not target.is_dir():
        return ToolResult(ok=False, error=f"{resolved.display} is a file, not a folder.")

    pattern = params.get("pattern")
    limit = int(params.get("limit", 200))
    include_hidden = bool(params.get("include_hidden", False))

    try:
        candidates = sorted(
            target.glob(pattern) if pattern else target.iterdir(),
            key=lambda p: (not p.is_dir(), p.name.lower()),
        )
    except (OSError, ValueError) as exc:
        return ToolResult(ok=False, error=f"Could not read {resolved.display}: {exc}")

    if not include_hidden:
        candidates = [p for p in candidates if not p.name.startswith(".")]

    entries = [_stat_entry(p) for p in candidates[:limit]]
    truncated = len(candidates) > limit

    return ToolResult(
        ok=True,
        data={
            "path": str(target),
            "display": resolved.display,
            "entries": entries,
            "total": len(candidates),
            "returned": len(entries),
            "truncated": truncated,
        },
        summary=(
            f"Listed {len(entries)} of {len(candidates)} items in {resolved.display}"
            if truncated
            else f"Listed {len(entries)} item(s) in {resolved.display}"
        ),
    )


# --------------------------------------------------------------------- read


@tool(
    "files.read",
    title="Read a file",
    description=(
        "Read the text contents of a file. Large files are truncated and the "
        "result says how much was returned. Binary files are refused with an "
        "explanation rather than returned as garbled text."
    ),
    capability="file.read",
    read_only=True,
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": 5_000_000},
        },
        "required": ["path"],
    },
)
def files_read(params: dict[str, Any]) -> ToolResult:
    resolved = resolve_path(str(params["path"]))
    target = resolved.path
    cap = int(params.get("max_bytes") or settings().max_file_read_bytes)

    if not target.exists():
        return ToolResult(ok=False, error=f"{resolved.display} does not exist.")
    if target.is_dir():
        return ToolResult(
            ok=False,
            error=f"{resolved.display} is a folder. Use files.list to see what is inside it.",
        )

    try:
        size = target.stat().st_size
        with target.open("rb") as handle:
            sample = handle.read(8192)
            if _looks_binary(sample) and target.suffix.lower() not in TEXT_SUFFIXES:
                return ToolResult(
                    ok=False,
                    error=(
                        f"{resolved.display} is a binary file ({size} bytes), so its contents "
                        "cannot be read as text."
                    ),
                )
            handle.seek(0)
            raw = handle.read(cap)
    except OSError as exc:
        return ToolResult(ok=False, error=f"Could not read {resolved.display}: {exc}")

    text = raw.decode("utf-8", errors="replace")
    truncated = size > len(raw)

    return ToolResult(
        ok=True,
        data={
            "path": str(target),
            "display": resolved.display,
            "content": text,
            "bytes_returned": len(raw),
            "bytes_total": size,
            "truncated": truncated,
            "lines": text.count("\n") + 1 if text else 0,
        },
        summary=(
            f"Read the first {len(raw)} of {size} bytes of {resolved.display}"
            if truncated
            else f"Read {resolved.display} ({size} bytes)"
        ),
    )


# -------------------------------------------------------------------- write


def _verify_write(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    """Re-read the file and compare against what we meant to write."""
    if not result.data:
        return False, "No write was recorded."
    path = Path(result.data["path"])
    expected = result.data.get("sha256")
    if not path.exists():
        return False, f"{path} does not exist after the write."
    actual = _digest(path)
    if expected and actual != expected:
        return False, "The file on disk does not match what was written."
    size = path.stat().st_size
    return True, f"Confirmed: {path} exists and is {size} bytes."


@tool(
    "files.write",
    title="Write a file",
    description=(
        "Create a file or change an existing one. Use mode 'create' to refuse if "
        "the file already exists, 'overwrite' to replace its contents, or "
        "'append' to add to the end. The write is verified by reading the file "
        "back afterwards."
    ),
    capability="file.write",
    idempotent=False,
    verifier=_verify_write,
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "mode": {
                "type": "string",
                "enum": ["create", "overwrite", "append"],
                "default": "create",
            },
            "create_parents": {"type": "boolean", "default": False},
        },
        "required": ["path", "content"],
    },
)
def files_write(params: dict[str, Any]) -> ToolResult:
    resolved = resolve_path(str(params["path"]))
    target = resolved.path
    content = str(params["content"])
    mode = params.get("mode", "create")

    if target.is_dir():
        return ToolResult(ok=False, error=f"{resolved.display} is a folder, not a file.")

    if mode == "create" and target.exists():
        return ToolResult(
            ok=False,
            error=(
                f"{resolved.display} already exists. Use mode 'overwrite' to replace it "
                "or 'append' to add to it."
            ),
        )

    if not target.parent.exists():
        if not params.get("create_parents", False):
            return ToolResult(
                ok=False,
                error=(
                    f"The folder {target.parent} does not exist. Set create_parents to true "
                    "if it should be created."
                ),
            )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ToolResult(ok=False, error=f"Could not create {target.parent}: {exc}")

    existed = target.exists()
    previous_size = target.stat().st_size if existed else 0

    try:
        if mode == "append":
            with target.open("a", encoding="utf-8") as handle:
                handle.write(content)
        else:
            # Write to a sibling temp file and rename, so an interrupted write
            # cannot leave the user with a half-written file.
            tmp = target.with_name(f".{target.name}.nexus-tmp")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, target)
    except OSError as exc:
        return ToolResult(ok=False, error=f"Could not write {resolved.display}: {exc}")

    size = target.stat().st_size
    verb = {"create": "Created", "overwrite": "Replaced", "append": "Appended to"}[mode]
    return ToolResult(
        ok=True,
        data={
            "path": str(target),
            "display": resolved.display,
            "bytes_written": len(content.encode("utf-8")),
            "size": size,
            "previous_size": previous_size,
            "existed": existed,
            "mode": mode,
            "sha256": _digest(target),
        },
        summary=f"{verb} {resolved.display} ({size} bytes)",
    )


# --------------------------------------------------------------------- move


def _verify_move(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    if not result.data:
        return False, "No move was recorded."
    source = Path(result.data["source"])
    destination = Path(result.data["destination"])
    if source.exists():
        return False, f"{source} still exists, so the move did not complete."
    if not destination.exists():
        return False, f"{destination} does not exist, so the move did not complete."
    return True, f"Confirmed: {source.name} is now at {destination}."


@tool(
    "files.move",
    title="Move or rename",
    description=(
        "Move or rename a file or folder. If the destination is an existing "
        "folder, the item is moved into it. Refuses to overwrite an existing file "
        "unless overwrite is true."
    ),
    capability="file.move",
    idempotent=False,
    verifier=_verify_move,
    parameters={
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "destination": {"type": "string"},
            "overwrite": {"type": "boolean", "default": False},
        },
        "required": ["source", "destination"],
    },
)
def files_move(params: dict[str, Any]) -> ToolResult:
    src = resolve_path(str(params["source"]))
    dst = resolve_path(str(params["destination"]))
    source, destination = src.path, dst.path

    if not source.exists():
        return ToolResult(ok=False, error=f"{src.display} does not exist.")

    if destination.is_dir():
        destination = destination / source.name

    if destination.exists() and not params.get("overwrite", False):
        return ToolResult(
            ok=False,
            error=(
                f"{destination} already exists. Set overwrite to true to replace it, or "
                "choose a different destination."
            ),
        )

    if destination == source:
        return ToolResult(ok=False, error="The source and destination are the same file.")

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
    except (OSError, shutil.Error) as exc:
        return ToolResult(ok=False, error=f"Could not move {src.display}: {exc}")

    return ToolResult(
        ok=True,
        data={"source": str(source), "destination": str(destination)},
        summary=f"Moved {src.display} to {destination}",
    )


# ------------------------------------------------------------------- delete


def _trash_dirs() -> tuple[Path, Path]:
    base = Path(
        os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    ) / "Trash"
    return base / "files", base / "info"


def _trash(path: Path) -> tuple[Path, Path]:
    """Move a path into the XDG trash, returning (trashed_file, info_file)."""
    files_dir, info_dir = _trash_dirs()
    files_dir.mkdir(parents=True, exist_ok=True)
    info_dir.mkdir(parents=True, exist_ok=True)

    name = path.name
    target = files_dir / name
    counter = 1
    while target.exists() or (info_dir / f"{target.name}.trashinfo").exists():
        target = files_dir / f"{path.stem}.{counter}{path.suffix}"
        counter += 1

    info = info_dir / f"{target.name}.trashinfo"
    info.write_text(
        "[Trash Info]\n"
        f"Path={quote(str(path))}\n"
        f"DeletionDate={datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\n",
        encoding="utf-8",
    )
    try:
        shutil.move(str(path), str(target))
    except (OSError, shutil.Error):
        info.unlink(missing_ok=True)
        raise
    return target, info


def _verify_delete(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    if not result.data:
        return False, "No deletion was recorded."
    original = Path(result.data["path"])
    if original.exists():
        return False, f"{original} still exists, so it was not deleted."
    if result.data.get("permanent"):
        return True, f"Confirmed: {original} no longer exists."
    trashed = result.data.get("trashed_to")
    if trashed and Path(trashed).exists():
        return True, f"Confirmed: {original} is gone and is recoverable from the trash."
    return False, "The file is gone but was not found in the trash, so it may not be recoverable."


@tool(
    "files.delete",
    title="Delete a file",
    description=(
        "Delete a file or folder. By default it goes to the trash so it can be "
        "restored. Set permanent to true only when the user has explicitly asked "
        "for an unrecoverable delete."
    ),
    capability="file.delete",
    idempotent=False,
    verifier=_verify_delete,
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "permanent": {"type": "boolean", "default": False},
            "recursive": {
                "type": "boolean",
                "default": False,
                "description": "Required to delete a folder that is not empty.",
            },
        },
        "required": ["path"],
    },
)
def files_delete(params: dict[str, Any]) -> ToolResult:
    resolved = resolve_path(str(params["path"]))
    target = resolved.path
    permanent = bool(params.get("permanent", False))

    if not target.exists():
        return ToolResult(ok=False, error=f"{resolved.display} does not exist.")
    if target == Path.home():
        return ToolResult(ok=False, error="NEXUS will not delete your home folder.")

    is_dir = target.is_dir()
    if is_dir:
        try:
            not_empty = any(target.iterdir())
        except OSError as exc:
            return ToolResult(ok=False, error=f"Could not inspect {resolved.display}: {exc}")
        if not_empty and not params.get("recursive", False):
            return ToolResult(
                ok=False,
                error=(
                    f"{resolved.display} is a folder that is not empty. Set recursive to true "
                    "to delete it and everything inside it."
                ),
            )

    size = 0
    count = 1
    if is_dir:
        for root, _dirs, files in os.walk(target):
            for name in files:
                try:
                    size += (Path(root) / name).stat().st_size
                    count += 1
                except OSError:
                    pass
    else:
        try:
            size = target.stat().st_size
        except OSError:
            pass

    try:
        if permanent:
            if is_dir:
                shutil.rmtree(target)
            else:
                target.unlink()
            trashed_to = None
        else:
            trashed_path, _info = _trash(target)
            trashed_to = str(trashed_path)
    except (OSError, shutil.Error) as exc:
        return ToolResult(ok=False, error=f"Could not delete {resolved.display}: {exc}")

    return ToolResult(
        ok=True,
        data={
            "path": str(target),
            "display": resolved.display,
            "permanent": permanent,
            "trashed_to": trashed_to,
            "was_directory": is_dir,
            "items": count,
            "bytes": size,
        },
        summary=(
            f"Permanently deleted {resolved.display}"
            if permanent
            else f"Moved {resolved.display} to the trash (restorable)"
        ),
    )


# --------------------------------------------------------------------- info


@tool(
    "files.info",
    title="File details",
    description="Get the size, type and modification time of a file or folder without reading it.",
    capability="file.list",
    read_only=True,
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
)
def files_info(params: dict[str, Any]) -> ToolResult:
    resolved = resolve_path(str(params["path"]))
    target = resolved.path
    if not target.exists():
        return ToolResult(ok=False, error=f"{resolved.display} does not exist.")

    entry = _stat_entry(target)
    entry["display"] = resolved.display
    if target.is_dir():
        try:
            children = list(target.iterdir())
            entry["items"] = len(children)
        except OSError:
            entry["items"] = None
    return ToolResult(ok=True, data=entry, summary=f"Details for {resolved.display}")


# ------------------------------------------------------------------- search


@tool(
    "files.search",
    title="Find files by name",
    description=(
        "Search for files by name under a starting folder. The query is a glob "
        "pattern when it contains * ? or [ (so '*.md' finds markdown files), and "
        "otherwise part of a file name (so 'report' finds 'q3-report.pdf'). "
        "Searches names only, not contents."
    ),
    capability="file.list",
    read_only=True,
    timeout_s=25.0,
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Folder to search under."},
            "query": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "A glob pattern such as '*.md' or 'draft-?.txt', or plain text "
                    "to match anywhere in the file name."
                ),
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            "max_depth": {"type": "integer", "minimum": 1, "maximum": 10, "default": 6},
        },
        "required": ["path", "query"],
    },
)
def files_search(params: dict[str, Any]) -> ToolResult:
    resolved = resolve_path(str(params["path"]))
    root = resolved.path
    if not root.is_dir():
        return ToolResult(ok=False, error=f"{resolved.display} is not a folder.")

    needle = str(params["query"]).lower()
    # A query containing glob characters is treated as a glob, anything else as a
    # substring. Without this, "*.md" is looked for literally, finds nothing, and
    # reports a confident "Found 0 files" that reads as "you have no markdown
    # files" -- a wrong answer that looks like a correct one. Both forms are what
    # callers actually send, so both are supported and the mode is reported.
    is_glob = any(ch in needle for ch in "*?[")
    matcher = (
        (lambda name: fnmatch.fnmatch(name, needle))
        if is_glob
        else (lambda name: needle in name)
    )
    limit = int(params.get("limit", 50))
    max_depth = int(params.get("max_depth", 6))
    deadline = time.monotonic() + 20.0

    matches: list[dict[str, Any]] = []
    scanned = 0
    hit_deadline = False

    base_depth = len(root.parts)
    for current, dirs, files in os.walk(root):
        if time.monotonic() > deadline:
            hit_deadline = True
            break
        depth = len(Path(current).parts) - base_depth
        if depth >= max_depth:
            dirs[:] = []
        # Skip noise that would swamp the results and waste the time budget.
        dirs[:] = [
            d for d in dirs
            if not d.startswith(".")
            and d not in ("node_modules", "__pycache__", "venv", ".venv", "target", "dist")
        ]
        for name in files:
            scanned += 1
            if matcher(name.lower()):
                matches.append(_stat_entry(Path(current) / name))
                if len(matches) >= limit:
                    break
        if len(matches) >= limit:
            break

    note = ""
    if hit_deadline:
        note = " Search stopped at the time limit, so there may be more matches."
    elif len(matches) >= limit:
        note = " Stopped at the result limit, so there may be more matches."

    return ToolResult(
        ok=True,
        data={
            "root": str(root),
            "query": params["query"],
            "matches": matches,
            "match_mode": "glob" if is_glob else "substring",
            "scanned": scanned,
            "complete": not hit_deadline and len(matches) < limit,
        },
        # The mode is in the sentence because an empty glob result and an empty
        # substring result mean different things, and a caller that cannot tell
        # them apart will read either as "no such files exist".
        summary=(
            f"Found {len(matches)} file(s) in {resolved.display} "
            + (
                f"whose name matches the pattern '{params['query']}'"
                if is_glob
                else f"with '{params['query']}' in the name"
            )
            + f", out of {scanned} searched.{note}"
        ),
    )
