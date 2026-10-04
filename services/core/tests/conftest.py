"""Test harness for the NEXUS core.

Two rules hold everywhere in here.

**Never the real database.** ``NEXUS_DB_PATH`` is redirected to a temporary file
*before* ``nexus`` is imported, because :func:`nexus.config.settings` is cached on
first use and anything reading it afterwards -- including ``create_app``, which calls
``db.configure()`` with no arguments -- would otherwise open the user's real
``~/.local/share/nexus/nexus.db``. A test suite that can delete someone's memories is
not a test suite.

**Stub the model and the machine, nothing else.** The planner (a network call) and
tool execution (which touches this computer) are the only things replaced. The store,
the schema, the status vocabulary, the permission engine and the event bus are the
real ones, so a test can still fail because the schema disagrees with the code -- which
is most of what these tests are for.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

CORE = Path(__file__).resolve().parents[1]
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

_TMP = Path(tempfile.mkdtemp(prefix="nexus-tests-"))
os.environ["NEXUS_DB_PATH"] = str(_TMP / "test.db")

import pytest

from nexus.db import conn as db
from nexus.db import migrate
from nexus.db.repos import app_settings
from nexus.tools.registry import ToolResult


@pytest.fixture(scope="session", autouse=True)
def database() -> Path:
    """One migrated temporary database for the whole session."""
    path = db.configure(_TMP / "test.db")
    migrate.migrate()
    assert path.parent.is_dir()
    return path


@pytest.fixture
def tmp_dir() -> Path:
    """A directory tests may name in tool params. Nothing is written to it by stubs."""
    return _TMP


AGENT_SETTINGS = ("agent_max_steps", "agent_verify_steps", "agent_pause_on_failure")


@pytest.fixture(autouse=True)
def agent_settings_restored():
    """Settings are global; a test that changes one must not change it for the next."""
    before = {key: app_settings.get(key) for key in AGENT_SETTINGS}
    yield
    for key, value in before.items():
        if value is not None:
            app_settings.set(key, value)


# --------------------------------------------------------------------- stubs


class Planner:
    """Stands in for the reasoning model. Returns queued answers in order.

    A planning call and a thinking step both go through ``agent._one_shot``, and this
    tells them apart the way the model would: the planner's system prompt carries the
    JSON contract. So a test can assert on what the planner was *asked*, which is how
    the re-plan tests prove the failure reason was actually handed back.
    """

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.calls: list[str] = []
        self.thoughts: list[str] = []

    async def __call__(self, job: Any, system: str, user: str, *, max_tokens: int) -> str:
        if "Return ONLY a JSON object" in system:
            self.calls.append(user)
            return self.answers.pop(0) if self.answers else '{"steps": []}'
        self.thoughts.append(user)
        return "Nothing in those results looks wrong."


class Runner:
    """Stands in for ``runtime.execute``. ``results`` is keyed by tool name."""

    def __init__(self, results: dict[str, ToolResult] | None = None) -> None:
        self.results = results or {}
        self.calls: list[str] = []

    async def __call__(self, name: str, params: dict[str, Any] | None = None, **kw: Any) -> ToolResult:
        self.calls.append(name)
        return self.results.get(name, ToolResult(ok=True, summary=f"{name} done"))


def plan_json(steps: list[dict[str, Any]]) -> str:
    return json.dumps({"steps": steps})


# Tool results with the shapes that matter: no verifier, a verifier that agrees, a
# verifier that contradicts a successful tool, and an honest failure with a reason.
STATUS = ToolResult(ok=True, summary="Up 1h", verified=None)
OK_VERIFIED = ToolResult(ok=True, summary="wrote it", verified=True, verification="the file is there")
CONTRADICTED = ToolResult(
    ok=True, summary="wrote it", verified=False, verification="the file is not there"
)
BROKEN = ToolResult(ok=False, error="the disk is full", summary="could not write it")
