"""Everything NEXUS can do to this computer.

Importing this package registers every tool. Nothing else in the codebase should
import the individual tool modules -- ``import nexus.tools`` is the single point
where the catalogue becomes complete, so there is no way to end up running with a
half-registered tool set.

The registry raises at import time on a tool that declares an unknown capability
or a malformed schema, so a mistake here fails the service at startup rather than
in the middle of a user's task.
"""

from __future__ import annotations

from . import (  # noqa: F401
    apps,
    automations,
    browser,
    files,
    input,
    media,
    memory,
    process_read,
    process_signal,
    screen,
    screen_read,
    system,
    system_admin,
    system_read,
    terminal,
    windows,
)
from .registry import (  # noqa: F401
    SchemaError,
    Tool,
    ToolResult,
    all_tools,
    available_tools,
    by_capability,
    catalogue,
    get,
    schemas,
)
from .runtime import ToolNotFound, execute, preview  # noqa: F401

__all__ = [
    "SchemaError",
    "Tool",
    "ToolNotFound",
    "ToolResult",
    "all_tools",
    "available_tools",
    "by_capability",
    "catalogue",
    "execute",
    "get",
    "preview",
    "schemas",
]
