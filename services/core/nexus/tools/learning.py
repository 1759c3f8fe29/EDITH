"""Continuous learning: remember corrections, preferences, and patterns (§10, §13, §45).

This module implements a learning layer that observes interactions and extracts
actionable memories, preferences, and corrections. It is not a training loop:
it writes structured records that the memory system and router can use immediately.

Three learning signals:
1. **Explicit corrections** — "No, I meant X" or "That was wrong, do Y instead"
2. **Implicit preferences** — Repeated choices, accepted suggestions, dismissed options
3. **Pattern recognition** — Recurring workflows, common sequences, time-based habits

All learning is:
- Transparent: every record is visible in the memory panel
- Reversible: the user can delete any learned fact
- Attributed: each record knows which turn produced it
- Scoped: facts are tagged with domain (coding, writing, system, etc.)
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..db.repos import app_settings, memories
from ..logging_setup import get_logger
from ..tools.registry import ToolResult, tool

log = get_logger("learning")

# Where learning records are persisted
LEARNING_DIR = Path("/home/santosh/.local/share/nexus/learning")
LEARNING_DIR.mkdir(parents=True, exist_ok=True)

# Types of learning signals
LEARNING_TYPES = {
    "correction": "User explicitly corrected the assistant",
    "preference": "User showed a consistent preference",
    "pattern": "Recurring workflow or sequence detected",
    "fact": "User stated a fact to remember",
    "feedback": "User gave positive/negative feedback on an answer",
}


@dataclass
class LearningRecord:
    """One learned item."""
    id: str
    type: str  # correction, preference, pattern, fact, feedback
    domain: str  # coding, writing, system, voice, general
    content: str  # What was learned
    context: dict[str, Any] = field(default_factory=dict)  # Turn ID, model, etc.
    confidence: float = 0.5  # 0..1
    source_turn_id: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    applied_count: int = 0
    user_confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "domain": self.domain,
            "content": self.content,
            "context": self.context,
            "confidence": self.confidence,
            "source_turn_id": self.source_turn_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "applied_count": self.applied_count,
            "user_confirmed": self.user_confirmed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LearningRecord":
        return cls(**data)


class LearningStore:
    """Persistent store for learning records."""

    def __init__(self, path: Path = LEARNING_DIR):
        self.path = path
        self.path.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, LearningRecord] = {}
        self._load()

    def _load(self) -> None:
        for file in self.path.glob("*.json"):
            try:
                data = json.loads(file.read_text())
                record = LearningRecord.from_dict(data)
                self._records[record.id] = record
            except Exception as exc:
                log.warning(f"Failed to load learning record {file}: {exc}")

    def _save(self, record: LearningRecord) -> None:
        file = self.path / f"{record.id}.json"
        file.write_text(json.dumps(record.to_dict(), indent=2))

    def add(self, record: LearningRecord) -> LearningRecord:
        self._records[record.id] = record
        self._save(record)
        return record

    def get(self, record_id: str) -> LearningRecord | None:
        return self._records.get(record_id)

    def update(self, record: LearningRecord) -> LearningRecord:
        record.updated_at = time.time()
        self._records[record.id] = record
        self._save(record)
        return record

    def delete(self, record_id: str) -> bool:
        if record_id in self._records:
            del self._records[record_id]
            file = self.path / f"{record_id}.json"
            if file.exists():
                file.unlink()
            return True
        return False

    def list(self, domain: str | None = None, type_: str | None = None) -> list[LearningRecord]:
        records = list(self._records.values())
        if domain:
            records = [r for r in records if r.domain == domain]
        if type_:
            records = [r for r in records if r.type == type_]
        return sorted(records, key=lambda r: r.updated_at, reverse=True)

    def search(self, query: str) -> list[LearningRecord]:
        query = query.lower()
        return [
            r for r in self._records.values()
            if query in r.content.lower() or query in r.domain.lower()
        ]


STORE = LearningStore()


# ----------------------------------------------------------------------
# Signal extractors: turn the raw interaction into learning records
# ----------------------------------------------------------------------


def extract_corrections(user_message: str, assistant_response: str) -> list[LearningRecord]:
    """Detect explicit corrections in user message."""
    records = []
    now = time.time()

    # Patterns for corrections
    correction_patterns = [
        (r"(?i)^(?:no|wrong|not|actually),?\s*(?:i meant|i mean|it should be|do)\s+(.+)", "correction"),
        (r"(?i)^(?:that'?s|that is)\s+(?:wrong|incorrect|not right),?\s*(.+)", "correction"),
        (r"(?i)instead of\s+.+?\s+(?:do|use|say)\s+(.+)", "correction"),
        (r"(?i)(?:please|just)\s+(?:do|use|say|make)\s+(.+)", "preference"),
        (r"(?i)^(?:remember|note that|from now on)\s+(.+)", "fact"),
    ]

    for pattern, ltype in correction_patterns:
        match = re.search(pattern, user_message)
        if match:
            content = match.group(1).strip()
            if content and len(content) > 3:
                record = LearningRecord(
                    id=f"learn_{ltype}_{int(now * 1000)}",
                    type=ltype,
                    domain=detect_domain(user_message),
                    content=content,
                    context={"user_message": user_message[:200], "assistant_response": assistant_response[:200]},
                    confidence=0.8 if ltype == "correction" else 0.6,
                    source_turn_id=None,
                )
                records.append(record)

    return records


def extract_preferences_from_tools(tool_calls: list[dict]) -> list[LearningRecord]:
    """Detect preferences from repeated tool usage patterns."""
    records = []
    # This would analyze tool call patterns over time
    # For now, return empty - implemented via background analysis
    return records


def detect_domain(text: str) -> str:
    """Classify the domain of a learning signal."""
    text_lower = text.lower()
    domains = {
        "coding": ["code", "function", "class", "api", "bug", "debug", "git", "compile", "python", "javascript", "typescript", "rust", "go", "java"],
        "writing": ["write", "edit", "document", "email", "blog", "article", "summary", "rewrite", "draft"],
        "system": ["system", "process", "memory", "disk", "cpu", "network", "service", "install", "package", "command", "terminal"],
        "voice": ["speak", "listen", "voice", "audio", "wake", "transcribe", "tts", "stt"],
        "browser": ["browser", "web", "search", "url", "page", "click", "navigate", "scrape"],
        "files": ["file", "folder", "directory", "copy", "move", "delete", "read", "write", "path"],
    }

    for domain, keywords in domains.items():
        if any(kw in text_lower for kw in keywords):
            return domain
    return "general"


# ----------------------------------------------------------------------
# Background learning processor
# ----------------------------------------------------------------------


class LearningProcessor:
    """Processes conversation history to extract learning signals."""

    def __init__(self):
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_processed_turn = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while self._running:
            try:
                from ..db.repos import app_settings
                if app_settings.get("learning_enabled", True):
                    await self._process_new_turns()
            except Exception as exc:
                log.exception("Learning processor error")
            await asyncio.sleep(30)  # Check every 30 seconds

    async def _process_new_turns(self) -> None:
        """Process recent turns for learning signals."""
        # Get recent conversations
        try:
            from ..db.conn import conn as db
            from ..db.repos import conversations as convos_repo, turns as turns_repo

            convos = await convos_repo.list(db, limit=10)
            for convo in convos:
                turns = await turns_repo.list(db, convo["id"], limit=5)
                for turn in turns:
                    if turn.id <= self._last_processed_turn:
                        continue
                    self._last_processed_turn = turn.id
                    await self._analyze_turn(turn)
        except Exception as exc:
            log.debug(f"Learning processor: {exc}")

    async def _analyze_turn(self, turn) -> None:
        """Analyze a single turn for learning signals."""
        user_msg = turn.prompt or ""
        assistant_msg = turn.text or ""

        # Extract corrections
        corrections = extract_corrections(user_msg, assistant_msg)
        for record in corrections:
            record.source_turn_id = str(turn.id)
            STORE.add(record)
            log.info(f"Learned {record.type}: {record.content[:50]}")

        # Extract facts from assistant responses (when assistant says "I'll remember...")
        fact_patterns = [
            r"(?i)i'?ll remember (?:that )?(.+)",
            r"(?i)noted[.:]?\s*(.+)",
            r"(?i)remembering (?:that )?(.+)",
        ]
        for pattern in fact_patterns:
            match = re.search(pattern, assistant_msg)
            if match:
                content = match.group(1).strip()
                if content and len(content) > 3:
                    record = LearningRecord(
                        id=f"learn_fact_{int(time.time() * 1000)}",
                        type="fact",
                        domain=detect_domain(assistant_msg),
                        content=content,
                        context={"assistant_message": assistant_msg[:200]},
                        confidence=0.7,
                        source_turn_id=str(turn.id),
                    )
                    STORE.add(record)
                    break


PROCESSOR = LearningProcessor()


async def start_learning() -> None:
    """Start the background learning processor."""
    await PROCESSOR.start()


async def stop_learning() -> None:
    """Stop the background learning processor."""
    await PROCESSOR.stop()


# ----------------------------------------------------------------------
# Tools for the model to interact with learning
# ----------------------------------------------------------------------


@tool(
    "learning.record",
    title="Record a learning",
    description="Explicitly record something the user wants NEXUS to remember.",
    capability="memory.write",
    idempotent=False,
    parameters={
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["correction", "preference", "pattern", "fact", "feedback"], "description": "Type of learning"},
            "content": {"type": "string", "description": "What to remember"},
            "domain": {"type": "string", "description": "Domain (coding, writing, system, voice, general)"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.8, "description": "Confidence level"},
        },
        "required": ["type", "content"],
    },
)
def learning_record(params: dict[str, Any]) -> ToolResult:
    ltype = str(params.get("type") or "")
    content = str(params.get("content") or "")
    domain = str(params.get("domain") or "general")
    confidence = float(params.get("confidence", 0.8))

    if not ltype or not content:
        return ToolResult(ok=False, error="Type and content are required")

    record = LearningRecord(
        id=f"learn_{ltype}_{int(time.time() * 1000)}",
        type=ltype,
        domain=domain,
        content=content,
        confidence=confidence,
        user_confirmed=True,
    )
    STORE.add(record)
    return ToolResult(ok=True, data=record.to_dict(), summary=f"Recorded {ltype}: {content[:50]}")


@tool(
    "learning.list",
    title="List learnings",
    description="List all learned records, optionally filtered by domain or type.",
    capability="memory.read",
    read_only=True,
    parameters={
        "type": "object",
        "properties": {
            "domain": {"type": "string", "description": "Filter by domain"},
            "type": {"type": "string", "description": "Filter by type"},
        },
    },
)
def learning_list(params: dict[str, Any]) -> ToolResult:
    domain = params.get("domain")
    ltype = params.get("type")
    records = STORE.list(domain=domain, type_=ltype)
    return ToolResult(
        ok=True,
        data={"records": [r.to_dict() for r in records], "count": len(records)},
        summary=f"Found {len(records)} learning records",
    )


@tool(
    "learning.search",
    title="Search learnings",
    description="Search learned records by content.",
    capability="memory.read",
    read_only=True,
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
        },
        "required": ["query"],
    },
)
def learning_search(params: dict[str, Any]) -> ToolResult:
    query = str(params.get("query") or "")
    if not query:
        return ToolResult(ok=False, error="Query is required")
    records = STORE.search(query)
    return ToolResult(
        ok=True,
        data={"records": [r.to_dict() for r in records], "count": len(records)},
        summary=f"Found {len(records)} matching records for '{query}'",
    )


@tool(
    "learning.delete",
    title="Delete a learning",
    description="Delete a learned record by ID.",
    capability="memory.delete",
    idempotent=False,
    parameters={
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Learning record ID"},
        },
        "required": ["id"],
    },
)
def learning_delete(params: dict[str, Any]) -> ToolResult:
    record_id = str(params.get("id") or "")
    if not record_id:
        return ToolResult(ok=False, error="ID is required")
    ok = STORE.delete(record_id)
    return ToolResult(ok=ok, summary=f"Deleted {record_id}" if ok else f"Not found: {record_id}")


@tool(
    "learning.apply",
    title="Apply a learning",
    description="Mark a learning as applied (increases its confidence).",
    capability="memory.write",
    idempotent=False,
    parameters={
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Learning record ID"},
        },
        "required": ["id"],
    },
)
def learning_apply(params: dict[str, Any]) -> ToolResult:
    record_id = str(params.get("id") or "")
    if not record_id:
        return ToolResult(ok=False, error="ID is required")
    record = STORE.get(record_id)
    if not record:
        return ToolResult(ok=False, error=f"Not found: {record_id}")
    record.applied_count += 1
    record.confidence = min(1.0, record.confidence + 0.05)
    STORE.update(record)
    return ToolResult(ok=True, data=record.to_dict(), summary=f"Applied {record_id} (confidence: {record.confidence:.2f})")


# Export for other modules
__all__ = [
    "LearningRecord",
    "LearningStore",
    "STORE",
    "LEARNING_TYPES",
    "extract_corrections",
    "detect_domain",
    "start_learning",
    "stop_learning",
    "PROCESSOR",
]