"""Tool registry (task D1; spec §35, §59, §60).

Every action NEXUS can take on this computer is a registered tool. A tool
declares:

* the **capability** it needs -- the registry refuses to register a tool whose
  capability is not in the catalogue, so no action can exist outside the
  permission system;
* a **JSON Schema** for its parameters -- validated before execution and also
  handed to the model as the function-calling schema, so there is exactly one
  definition of a tool's shape;
* the external **binaries** it requires -- checked at registration time, so an
  unavailable tool reports *why* instead of failing mid-task (§78, §100);
* an optional **verifier** -- a function that independently confirms the action
  actually happened (§60): the difference between "the command returned 0" and
  "the file is really there".

The parameter validator is deliberately implemented here rather than pulled from
a library: it needs to produce error messages a user can act on, and the schema
subset a tool needs is small.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..logging_setup import get_logger
from ..permissions import capabilities

log = get_logger("tools.registry")


class SchemaError(ValueError):
    """Parameters did not match the tool's schema."""


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str | None = None
    # Verification is tri-state on purpose: True (checked and confirmed),
    # False (checked and NOT confirmed), None (no verifier for this tool).
    # Reporting None as True would be the exact dishonesty §60 forbids.
    verified: bool | None = None
    verification: str = ""
    duration_ms: int | None = None
    # A short, user-facing sentence describing what actually happened.
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "data": self.data,
            "error": self.error,
            "verified": self.verified,
            "verification": self.verification,
            "duration_ms": self.duration_ms,
            "summary": self.summary,
        }


Handler = Callable[[dict[str, Any]], ToolResult]
Verifier = Callable[[dict[str, Any], ToolResult], tuple[bool, str]]
#: A runtime availability check for the things a binary list cannot express.
#: Returns None when the tool can actually run, or a short user-facing reason
#: when it cannot. See ``Tool.available_probe``.
AvailabilityProbe = Callable[[], "str | None"]


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    capability: str
    parameters: dict[str, Any]
    handler: Handler
    verifier: Verifier | None = None
    timeout_s: float | None = None
    #: External programs this tool needs. Entries are ANDed: every one must be
    #: satisfied. An entry may name alternatives with ``|`` -- ``"xclip|xsel"`` is
    #: satisfied by either -- because several tools here drive whichever of a set of
    #: interchangeable backends the machine happens to have, and an AND-only list
    #: cannot express that. Without the OR form those tools declared nothing at all
    #: and were counted as available on a machine where none of their backends
    #: existed: the startup probe over-reported, and the model was handed a tool
    #: whose only possible outcome was an error.
    requires: tuple[str, ...] = ()
    #: A runtime availability check for what a binary list cannot express: a
    #: writable device file, a reachable socket, a permission the current user
    #: happens to hold. ``requires`` answers "is the program installed?"; this
    #: answers "will the thing it needs to touch actually let it?" -- the case
    #: ``system.brightness`` lives in, where a laptop has a real, *readable*
    #: backlight whose ``brightness`` file is not writable and no ``brightnessctl``
    #: to do it without root, so the tool would otherwise be offered to the model
    #: as available and fail the moment it tried to change anything. Returns None
    #: when the tool can run, or a short reason when it cannot. Kept separate from
    #: ``requires`` because a missing binary is a different, more basic problem
    #: than an unwritable device, and the two are reported in that order.
    available_probe: AvailabilityProbe | None = None
    # True when running it twice has the same effect as running it once. Agent
    # mode uses this to decide whether a retry is safe (§23).
    idempotent: bool = True
    # Some tools are read-only regardless of their capability's risk; the UI uses
    # this to avoid decorating a harmless action with a scary confirmation.
    read_only: bool = False
    aliases: tuple[str, ...] = field(default_factory=tuple)

    #: Voice announcement when this tool starts executing.
    #: If not provided, a default is generated from the tool's title.
    #: Can include {param_name} placeholders that are filled from cleaned params.
    voice_start: str | None = None
    #: Voice announcement when this tool times out (optional).
    #: If not provided, a default is generated from the tool's title.
    voice_timeout: str | None = None
    #: Voice announcement when this tool fails with an error (optional).
    #: If not provided, a default is generated from the tool's title.
    voice_error: str | None = None

    @property
    def risk(self) -> str:
        return capabilities.get(self.capability).risk

    def missing_binaries(self) -> list[str]:
        """Unsatisfied requirements, each as it was declared.

        An alternatives group is returned whole (``"wl-paste|xclip|xsel"``) rather
        than as three separate names, because "none of these three is installed" is
        one problem with one fix and listing them separately would read as three.
        """
        return [
            requirement
            for requirement in self.requires
            if not any(shutil.which(option) for option in requirement.split("|") if option)
        ]

    def _probe_reason(self) -> str | None:
        """The availability probe's verdict, or None when there is no probe.

        A probe that raises is treated as unavailable, not available: an
        uncertain check must never read as a working tool (§78). The exception
        is logged and turned into an honest reason rather than swallowed.
        """
        if self.available_probe is None:
            return None
        try:
            return self.available_probe()
        except Exception as exc:  # noqa: BLE001 -- a probe bug hides the tool, loudly
            log.exception("availability probe for %s failed", self.name)
            return f"NEXUS could not confirm this tool can run on this computer ({exc})."

    @property
    def available(self) -> bool:
        return not self.missing_binaries() and self._probe_reason() is None

    def unavailable_reason(self) -> str | None:
        missing = self.missing_binaries()
        if not missing:
            # No missing binary; the probe is the only remaining thing that can
            # make it unavailable. A missing binary is reported first (below)
            # because it is the more basic fix.
            return self._probe_reason()

        def phrase(requirement: str) -> str:
            options = [o for o in requirement.split("|") if o]
            if len(options) == 1:
                return f"'{options[0]}'"
            return "none of " + ", ".join(f"'{o}'" for o in options)

        if len(missing) == 1:
            only = missing[0]
            if "|" in only:
                return (
                    f"{phrase(only)} is installed on this computer, and this tool needs "
                    "one of them."
                )
            return f"{phrase(only)} is not installed on this computer."
        return "These programs are not installed: " + ", ".join(
            phrase(m) for m in missing
        ) + "."

    def to_schema(self) -> dict[str, Any]:
        """The function-calling schema handed to a model provider (§59)."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    def to_dict(self) -> dict[str, Any]:
        cap = capabilities.get(self.capability)
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "capability": self.capability,
            "group": cap.group,
            "risk": cap.risk,
            "reversible": cap.reversible,
            "read_only": self.read_only,
            "idempotent": self.idempotent,
            "verified_by_default": self.verifier is not None,
            "parameters": self.parameters,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason(),
            "requires": list(self.requires),
        }


# --------------------------------------------------------------- registration

_TOOLS: dict[str, Tool] = {}
_ALIASES: dict[str, str] = {}


def register(tool: Tool) -> Tool:
    if tool.name in _TOOLS:
        raise ValueError(f"tool {tool.name!r} is already registered")
    if not capabilities.exists(tool.capability):
        # A tool with no capability would be an action outside the permission
        # engine. That must be impossible, not merely discouraged (§5).
        raise ValueError(
            f"tool {tool.name!r} declares unknown capability {tool.capability!r}"
        )
    _validate_schema_shape(tool.name, tool.parameters)
    _TOOLS[tool.name] = tool
    for alias in tool.aliases:
        _ALIASES[alias] = tool.name
    return tool


def tool(
    name: str,
    *,
    title: str,
    description: str,
    capability: str,
    parameters: dict[str, Any],
    verifier: Verifier | None = None,
    timeout_s: float | None = None,
    requires: Iterable[str] = (),
    available_probe: AvailabilityProbe | None = None,
    idempotent: bool = True,
    read_only: bool = False,
    aliases: Iterable[str] = (),
    voice_start: str | None = None,
    voice_timeout: str | None = None,
    voice_error: str | None = None,
) -> Callable[[Handler], Handler]:
    """Decorator form: keeps a tool's declaration next to its implementation."""

    def decorate(handler: Handler) -> Handler:
        register(
            Tool(
                name=name,
                title=title,
                description=description,
                capability=capability,
                parameters=parameters,
                handler=handler,
                verifier=verifier,
                timeout_s=timeout_s,
                requires=tuple(requires),
                available_probe=available_probe,
                idempotent=idempotent,
                read_only=read_only,
                aliases=tuple(aliases),
                voice_start=voice_start,
                voice_timeout=voice_timeout,
                voice_error=voice_error,
            )
        )
        return handler

    return decorate


def get(name: str) -> Tool | None:
    if name in _TOOLS:
        return _TOOLS[name]
    resolved = _ALIASES.get(name)
    return _TOOLS.get(resolved) if resolved else None


def all_tools() -> list[Tool]:
    return sorted(_TOOLS.values(), key=lambda t: t.name)


def available_tools() -> list[Tool]:
    return [t for t in all_tools() if t.available]


def schemas(*, only_available: bool = True) -> list[dict[str, Any]]:
    """Function-calling schemas for the model.

    Unavailable tools are withheld by default: offering the model a tool that
    cannot run produces a plan that must fail (§78).
    """
    source = available_tools() if only_available else all_tools()
    return [t.to_schema() for t in source]


def catalogue() -> list[dict[str, Any]]:
    return [t.to_dict() for t in all_tools()]


def by_capability() -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for t in all_tools():
        grouped.setdefault(t.capability, []).append(t.name)
    return grouped


def reset_for_tests() -> None:
    _TOOLS.clear()
    _ALIASES.clear()


# ------------------------------------------------------------ schema handling

_SUPPORTED_TYPES = {"object", "string", "number", "integer", "boolean", "array"}


def _validate_schema_shape(tool_name: str, schema: dict[str, Any]) -> None:
    """Reject a malformed schema at import time rather than at call time."""
    if schema.get("type") != "object":
        raise ValueError(f"tool {tool_name!r}: parameters schema must be type 'object'")
    props = schema.get("properties")
    if not isinstance(props, dict):
        raise ValueError(f"tool {tool_name!r}: parameters schema needs 'properties'")
    for key, spec in props.items():
        declared = spec.get("type")
        if declared and declared not in _SUPPORTED_TYPES:
            raise ValueError(
                f"tool {tool_name!r}: property {key!r} has unsupported type {declared!r}"
            )
    for required in schema.get("required", []):
        if required not in props:
            raise ValueError(
                f"tool {tool_name!r}: required property {required!r} is not declared"
            )


def validate(schema: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Validate and coerce parameters. Returns the cleaned dict.

    Coercion is limited to what a JSON-emitting model plausibly gets wrong -- a
    number arriving as "12" -- and never invents a value that was not supplied.
    """
    if not isinstance(params, dict):
        raise SchemaError("parameters must be a JSON object")

    props: dict[str, Any] = schema.get("properties", {})
    required: list[str] = list(schema.get("required", []))
    allow_extra = schema.get("additionalProperties", False)

    missing = [key for key in required if params.get(key) in (None, "")]
    if missing:
        raise SchemaError(
            f"missing required parameter{'s' if len(missing) > 1 else ''}: {', '.join(missing)}"
        )

    if not allow_extra:
        extra = sorted(set(params) - set(props))
        if extra:
            raise SchemaError(f"unexpected parameter(s): {', '.join(extra)}")

    cleaned: dict[str, Any] = {}
    for key, spec in props.items():
        if key not in params or params[key] is None:
            if "default" in spec:
                cleaned[key] = spec["default"]
            continue
        cleaned[key] = _coerce(key, spec, params[key])

    return cleaned


def _coerce(key: str, spec: dict[str, Any], value: Any) -> Any:
    declared = spec.get("type")

    if declared == "string":
        if not isinstance(value, str):
            value = str(value)
        if (min_len := spec.get("minLength")) is not None and len(value) < min_len:
            raise SchemaError(f"{key} must be at least {min_len} character(s)")
        if (max_len := spec.get("maxLength")) is not None and len(value) > max_len:
            raise SchemaError(f"{key} must be at most {max_len} characters")
        if (pattern := spec.get("pattern")) and not re.search(pattern, value):
            raise SchemaError(f"{key} is not in the expected format")

    elif declared in ("number", "integer"):
        try:
            value = int(value) if declared == "integer" else float(value)
        except (TypeError, ValueError):
            raise SchemaError(f"{key} must be a {declared}") from None
        if (minimum := spec.get("minimum")) is not None and value < minimum:
            raise SchemaError(f"{key} must be at least {minimum}")
        if (maximum := spec.get("maximum")) is not None and value > maximum:
            raise SchemaError(f"{key} must be at most {maximum}")

    elif declared == "boolean":
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "yes", "1"):
                value = True
            elif lowered in ("false", "no", "0"):
                value = False
            else:
                raise SchemaError(f"{key} must be true or false")
        elif not isinstance(value, bool):
            value = bool(value)

    elif declared == "array":
        if not isinstance(value, list):
            raise SchemaError(f"{key} must be a list")
        item_spec = spec.get("items")
        if isinstance(item_spec, dict):
            value = [_coerce(f"{key}[{i}]", item_spec, v) for i, v in enumerate(value)]
        if (min_items := spec.get("minItems")) is not None and len(value) < min_items:
            raise SchemaError(f"{key} needs at least {min_items} item(s)")

    elif declared == "object" and not isinstance(value, dict):
        raise SchemaError(f"{key} must be an object")

    if (choices := spec.get("enum")) is not None and value not in choices:
        raise SchemaError(f"{key} must be one of: {', '.join(map(str, choices))}")

    return value
