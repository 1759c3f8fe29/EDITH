"""Structured logging with secret redaction and a diagnostics ring buffer.

Task A9. Spec §65 forbids credentials appearing in logs or diagnostics, and
§112 wants structured internal diagnostics while user-facing messaging stays
understandable. Redaction is enforced by a logging filter rather than by
remembering to scrub at each call site -- the call sites will forget.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections import deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Deque

REDACTED = "[redacted]"

# Values registered here are scrubbed from every log record. The secrets layer
# registers each credential it loads, so a key cannot reach a log even if some
# call site passes it by mistake.
_secret_values: set[str] = set()
_secret_lock = Lock()

# Shape-based fallback for credentials that were never registered -- e.g. a key
# pasted into a prompt, or one returned in a provider error body.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bnvapi-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*", re.IGNORECASE),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[=:]\s*[\"']?([^\s\"',}]{8,})"),
)


def register_secret(value: str | None) -> None:
    """Mark a value as never-loggable."""
    if value and len(value) >= 8:
        with _secret_lock:
            _secret_values.add(value)


def forget_secret(value: str | None) -> None:
    if value:
        with _secret_lock:
            _secret_values.discard(value)


def redact(text: str) -> str:
    """Remove known and shape-matched credentials from a string."""
    if not text:
        return text
    with _secret_lock:
        known = tuple(_secret_values)
    for secret in known:
        if secret in text:
            text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


#: How deep :func:`redact_data` will walk before it gives up and stringifies.
#: Far deeper than any tool result NEXUS builds; it exists so a malformed or
#: self-referential payload cannot turn a scrub into a stack overflow.
_MAX_REDACT_DEPTH = 12


def redact_data(value: Any, _depth: int = 0) -> Any:
    """Scrub credentials from a nested payload, preserving its shape.

    :func:`redact` takes one string. A tool result is a nested structure whose
    *leaves* are the dangerous part: ``terminal.run`` returns the command it ran plus
    the whole of stdout and stderr, and ``files.read`` returns the file's contents. So
    "reading" a ``.env`` and "running ``env``" both produce a payload that is mostly
    credential, and that payload is what gets handed to the model.

    Walking the structure rather than scrubbing the serialised JSON is deliberate, and
    not merely tidier. Inside JSON a newline is the two characters backslash-n, while
    the ``KEY=value`` pattern in ``_PATTERNS`` ends its value run at whitespace -- so
    against JSON text that run does not stop at the end of the line, and one
    ``API_KEY=`` near the top swallows everything after it as part of the secret. The
    output would still be safe and would also be useless. Scrubbing each leaf while it
    still contains real newlines keeps every match as narrow as the line it was found
    on.

    Dictionary **keys** are deliberately left alone. They are field and variable names
    rather than values, and two distinct keys can redact to the same string -- which
    would silently merge two entries into one, losing data inside a function whose
    whole job is to lose only the parts that are secret.

    Tuples come back as lists. Both consumers serialise to JSON, where the two are the
    same thing, so the alternative is preserving a distinction nothing downstream can
    observe.
    """
    if isinstance(value, str):
        return redact(value)
    if _depth >= _MAX_REDACT_DEPTH:
        return redact(str(value))
    if isinstance(value, dict):
        return {key: redact_data(item, _depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_data(item, _depth + 1) for item in value]
    return value


class RedactionFilter(logging.Filter):
    """Scrub credentials from the message, its args, and any exception text."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._clean(v) for k, v in record.args.items()}
            else:
                record.args = tuple(self._clean(a) for a in record.args)
        for key, value in list(record.__dict__.items()):
            if key not in _RESERVED and isinstance(value, str):
                record.__dict__[key] = redact(value)
        return True

    @staticmethod
    def _clean(value: Any) -> Any:
        return redact(value) if isinstance(value, str) else value


_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class DiagnosticsBuffer(logging.Handler):
    """Keeps the most recent records in memory for the Advanced settings pane.

    Bounded on purpose (§69): diagnostics must not become a memory leak in a
    long-running desktop process.
    """

    def __init__(self, capacity: int = 2000) -> None:
        super().__init__()
        self.records: Deque[dict[str, Any]] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(_as_dict(record))
        except Exception:  # a logging handler must never raise into the app
            pass

    def dump(self, limit: int = 200, min_level: int = logging.DEBUG) -> list[dict[str, Any]]:
        items = [r for r in self.records if r["levelno"] >= min_level]
        return items[-limit:]

    def clear(self) -> None:
        self.records.clear()


def _as_dict(record: logging.LogRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
        "level": record.levelname,
        "levelno": record.levelno,
        "logger": record.name,
        "message": record.getMessage(),
    }
    if record.exc_info:
        payload["error"] = redact(logging.Formatter().formatException(record.exc_info))
    for key, value in record.__dict__.items():
        if key not in _RESERVED and not key.startswith("_"):
            payload[key] = value
    return payload


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(_as_dict(record), default=str)


class HumanFormatter(logging.Formatter):
    """Readable single-line output for development."""

    def format(self, record: logging.LogRecord) -> str:
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        }
        tail = " ".join(f"{k}={v}" for k, v in extras.items())
        stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        line = f"{stamp} {record.levelname:<7} {record.name:<22} {record.getMessage()}"
        if tail:
            line = f"{line}  {tail}"
        if record.exc_info:
            line += "\n" + redact(self.formatException(record.exc_info))
        return line


diagnostics = DiagnosticsBuffer()


def setup_logging(level: str = "INFO", human: bool = True) -> DiagnosticsBuffer:
    """Install the root handlers. Idempotent."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)

    redaction = RedactionFilter()

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(HumanFormatter() if human else JsonFormatter())
    stream.addFilter(redaction)
    root.addHandler(stream)

    diagnostics.addFilter(redaction)
    root.addHandler(diagnostics)

    # uvicorn installs its own noisy handlers; route them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    return diagnostics


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"nexus.{name}")
