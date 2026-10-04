"""Clipboard monitor (task L6; spec §37, §66, §78, §81).

Notices when the clipboard changes, so "save what I just copied" and "what did I
copy earlier" have real answers.

The clipboard is where passwords live. A password manager's copy button, an SSH
key on the way into a config file, a recovery code -- all of it passes through the
same buffer as the URL someone wanted to keep. So this monitor's most important
behaviour is the one where it *does not* record something:

**Secrets are classified, never kept.** Content matching a credential shape --
private key blocks, API tokens, JWTs, high-entropy strings from a password
manager -- produces an observation that says "a credential was copied" with a
length and a category, and no content. Not truncated content. None.

**Everything else is capped, not logged.** Ordinary text is kept up to a limit,
because a 4 MB paste of a CSV in a history buffer is a memory leak wearing a
feature's clothes. Content never reaches the log file at any level.

Unlike the screen and window monitors this one works on Wayland too, because
``wl-paste`` exists and reads the same buffer as ``xclip`` does on X11.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..logging_setup import get_logger
from .base import Monitor, MonitorUnavailable, Observation

log = get_logger("monitors.clipboard")

#: Longest text kept in the history buffer. Beyond this the entry records the
#: length and the first line only.
MAX_KEPT_CHARS = 4_000

#: How many clipboard entries stay in memory.
HISTORY_SIZE = 50

#: Readers in preference order: (binary, argv, works_on_wayland)
_READERS: tuple[tuple[str, list[str], bool], ...] = (
    ("wl-paste", ["wl-paste", "--no-newline"], True),
    ("xclip", ["xclip", "-selection", "clipboard", "-o"], False),
    ("xsel", ["xsel", "--clipboard", "--output"], False),
)

#: Credential shapes. Each is (category, pattern). Order matters only for the
#: label the user sees; any single match is enough to withhold the content.
_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("a certificate", re.compile(r"-----BEGIN CERTIFICATE-----")),
    ("an AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("an OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("a Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("an NVIDIA API key", re.compile(r"\bnvapi-[A-Za-z0-9_\-]{20,}\b")),
    ("a 32-character API key", re.compile(r"\b[A-Za-z0-9]{32}\b(?=\s*$)")),
    ("a JSON web token", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.")),
    ("a connection string with a password", re.compile(r"[a-z][a-z0-9+.\-]*://[^\s:@/]+:[^\s@/]+@")),
    ("an environment variable holding a secret", re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|token|credential)\b\s*[:=]\s*\S+"
    )),
)

#: A string this long with this much entropy and no spaces is a generated secret,
#: not prose. Below it, false positives (a git SHA, a UUID) are too likely.
_ENTROPY_MIN_LENGTH = 24
_ENTROPY_BITS_PER_CHAR = 3.5


@dataclass
class ClipboardEntry:
    """One thing that was copied."""

    at: str
    kind: str                    # "text", "url", "path", "code", "credential"
    length: int
    lines: int
    #: Empty for credentials, and for oversized content beyond the cap.
    content: str = ""
    preview: str = ""
    withheld_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "kind": self.kind,
            "length": self.length,
            "lines": self.lines,
            "content": self.content,
            "preview": self.preview,
            "withheld_reason": self.withheld_reason,
        }


def _entropy(text: str) -> float:
    """Shannon entropy in bits per character."""
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    total = len(text)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def looks_like_credential(text: str) -> str:
    """Return a human category if this looks like a secret, else "".

    Deliberately biased towards false positives. Mislabelling a git SHA as a
    credential costs the user a slightly vaguer history entry; the opposite
    mistake writes their password into a database (§81).
    """
    sample = text.strip()
    if not sample:
        return ""

    for category, pattern in _CREDENTIAL_PATTERNS:
        if pattern.search(sample):
            return category

    # A single unbroken high-entropy token: the shape of a generated password.
    if (
        len(sample) >= _ENTROPY_MIN_LENGTH
        and len(sample) <= 200
        and not any(c.isspace() for c in sample)
        and _entropy(sample) >= _ENTROPY_BITS_PER_CHAR
        and not sample.startswith(("http://", "https://", "/", "~/", "file://"))
        and "." not in sample.rstrip(".")[-6:]  # not a hostname or a filename
    ):
        return "something that looks like a password or token"

    return ""


def _classify(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith(("http://", "https://", "www.")) and len(stripped.split()) == 1:
        return "url"
    if stripped.startswith(("/", "~/", "./")) and len(stripped.split()) == 1:
        return "path"
    if "\n" in stripped and re.search(r"[{};]|\bdef \b|\bfunction\b|\bclass \b|^\s{2,}\S", stripped, re.M):
        return "code"
    return "text"


def _read_clipboard(argv: list[str]) -> tuple[str | None, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()
        # An empty clipboard is a successful read of nothing on some tools and a
        # non-zero exit on others; both mean "nothing copied", not a failure.
        if "empty" in detail.lower() or not detail:
            return "", ""
        return None, detail
    return proc.stdout, ""


class ClipboardMonitor(Monitor):
    id = "clipboard"
    title = "Clipboard"
    description = (
        "Notices what you copy so NEXUS can use it or bring it back later. "
        "Anything that looks like a password or key is counted, never stored."
    )
    enabled_setting = "monitor_clipboard_enabled"
    interval_setting = "monitor_clipboard_interval_seconds"
    default_interval = 3.0
    capability = "clipboard.read"
    activity_kind = "clipboard"
    # Copying happens constantly; the Activity log is not the place for it.
    record_to_activity = False

    def __init__(self) -> None:
        super().__init__()
        self._argv: list[str] = []
        self._binary = ""
        self._last_hash: str | None = None
        self._history: deque[ClipboardEntry] = deque(maxlen=HISTORY_SIZE)
        self._credentials_skipped = 0

    # -------------------------------------------------------------- availability

    def check_available(self) -> None:
        for binary, argv, _wayland in _READERS:
            if shutil.which(binary):
                self._binary, self._argv = binary, argv
                return
        raise MonitorUnavailable(
            "No clipboard reader is installed, so NEXUS cannot see what you copy. "
            "Installing 'xclip' would enable this (on Wayland, 'wl-clipboard')."
        )

    async def on_start(self) -> None:
        # Prime with the current contents so the first tick does not report
        # whatever happened to be on the clipboard before watching began as
        # something the user just copied.
        text, _ = await asyncio.to_thread(_read_clipboard, self._argv)
        if text is not None:
            self._last_hash = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()

    # -------------------------------------------------------------------- sample

    async def sample(self) -> list[Observation]:
        text, error = await asyncio.to_thread(_read_clipboard, self._argv)
        if text is None:
            raise RuntimeError(f"{self._binary} could not read the clipboard: {error}")

        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        if digest == self._last_hash:
            return []
        self._last_hash = digest

        if not text.strip():
            return []

        entry = self._entry(text)
        self._history.append(entry)

        if entry.kind == "credential":
            self._credentials_skipped += 1
            return [
                Observation(
                    kind="clipboard.credential",
                    summary=(
                        f"You copied {entry.withheld_reason} "
                        f"({entry.length} characters). NEXUS did not keep it."
                    ),
                    data={
                        "kind": "credential",
                        "category": entry.withheld_reason,
                        "length": entry.length,
                        "stored": False,
                    },
                    severity="notice",
                    dedupe_key=f"credential:{digest[:16]}",
                )
            ]

        return [
            Observation(
                kind="clipboard.copy",
                summary=f"Copied {entry.kind}: {entry.preview}",
                data=entry.to_dict(),
                dedupe_key=f"clip:{digest[:16]}",
            )
        ]

    # ------------------------------------------------------------------- entries

    def _entry(self, text: str) -> ClipboardEntry:
        now = datetime.now(timezone.utc).isoformat()
        length = len(text)
        lines = text.count("\n") + 1

        category = looks_like_credential(text)
        if category:
            # No content, and no preview either: the first eight characters of a
            # password are still eight characters of a password.
            return ClipboardEntry(
                at=now,
                kind="credential",
                length=length,
                lines=lines,
                withheld_reason=category,
            )

        kind = _classify(text)
        first_line = text.strip().splitlines()[0] if text.strip() else ""
        preview = first_line[:120] + ("..." if len(first_line) > 120 else "")

        if length > MAX_KEPT_CHARS:
            return ClipboardEntry(
                at=now,
                kind=kind,
                length=length,
                lines=lines,
                preview=preview,
                withheld_reason=(
                    f"kept the first line only: {length:,} characters is over the "
                    f"{MAX_KEPT_CHARS:,} NEXUS holds in clipboard history"
                ),
            )

        return ClipboardEntry(
            at=now, kind=kind, length=length, lines=lines, content=text, preview=preview
        )

    # ------------------------------------------------------------------- queries

    def history(self, limit: int = 20, *, include_content: bool = False) -> list[dict[str, Any]]:
        """Recent clipboard entries, newest first.

        ``include_content`` is False by default so a UI list, a log line or a
        model prompt gets metadata unless the caller has a reason to ask for the
        text itself.
        """
        out: list[dict[str, Any]] = []
        for entry in reversed(self._history):
            data = entry.to_dict()
            if not include_content:
                data["content"] = ""
            out.append(data)
            if len(out) >= limit:
                break
        return out

    def latest(self, *, include_content: bool = True) -> dict[str, Any] | None:
        if not self._history:
            return None
        data = self._history[-1].to_dict()
        if not include_content:
            data["content"] = ""
        return data

    def snapshot(self) -> dict[str, Any]:
        return {
            "reader": self._binary or None,
            "entries": len(self._history),
            "credentials_skipped": self._credentials_skipped,
            "max_kept_chars": MAX_KEPT_CHARS,
            "history_size": HISTORY_SIZE,
        }
