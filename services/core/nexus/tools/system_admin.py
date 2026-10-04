"""Administrative-state reads (tool batch B1; spec §38, §60).

The *read* half of the system-administration surface: what services exist, what
each one's status is, what packages are installed, and what jobs this account
has scheduled. Each is a read of state the machine already exposes through its
own tooling (``systemctl``, ``dpkg-query``, ``apt-cache``, ``crontab``) -- these
tools parse that output into a shape the model can reason over, nothing more.

The *write* half (start/stop/enable/install/remove) is deliberately not here; it
lands later, under its own higher-risk capabilities.

Three new capabilities were added for these (§5 closed set):

* ``system.service.read`` and ``system.packages.read`` are ``implicit`` -- they
  answer questions about the *machine* ("which services run", "is this package
  installed"), the same exemption ``system.info`` and ``app.list`` have.
* ``system.cron.read`` is **not** implicit: a crontab is the account's *own*
  scheduled automation, and its entries can embed credentials. It asks.

All are read-only, so none has a verifier (§60): there is nothing to confirm.
The ``/proc``-style discipline from :mod:`nexus.tools.system` is reused via
``_run`` rather than reimplementing process execution here.
"""

from __future__ import annotations

from typing import Any

from ..logging_setup import get_logger, redact
from .registry import ToolResult, tool
from .system import _run

log = get_logger("tools.system_admin")


def _show_unit(name: str) -> dict[str, str] | None:
    """One ``systemctl show`` unit as a plain dict, or None if it does not exist.

    ``systemctl show`` exits 0 even for a unit that is not found (it reports
    ``LoadState=not-found``), so existence is read from that field, never from the
    return code. Values after ``=`` may contain spaces ("Accounts Service"), so
    each line is split on the first ``=`` rather than on whitespace.
    """
    proc = _run(
        [
            "systemctl", "show", name,
            "-p", "Description", "-p", "LoadState", "-p", "ActiveState",
            "-p", "SubState", "-p", "UnitFileState", "-p", "MainPID",
        ],
        timeout=6.0,
    )
    if proc is None or proc.returncode != 0:
        return None
    fields: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        if key:
            fields[key] = value
    if fields.get("LoadState") == "not-found":
        return None
    return fields


def _parse_list_units(stdout: str) -> list[dict[str, str]]:
    """Rows from ``systemctl list-units --plain`` (space-padded, not tabbed)."""
    units: list[dict[str, str]] = []
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        units.append(
            {
                "unit": parts[0],
                "load": parts[1],
                "active": parts[2],
                "sub": parts[3],
                "description": " ".join(parts[4:]).strip(),
            }
        )
    return units


def _parse_packages(stdout: str) -> list[dict[str, str]]:
    """Installed packages from ``dpkg-query -W`` (tab-separated)."""
    packages: list[dict[str, str]] = []
    for line in stdout.splitlines():
        package, _, rest = line.partition("\t")
        version, _, status = rest.partition("\t")
        # ``db:Status-Status`` is the third word of the status field; "installed" is
        # the only value that means the package is actually here. "config-files" and
        # "not-installed" refer to residue, not a live package.
        if status == "installed":
            packages.append({"name": package, "version": version})
    return packages


# ------------------------------------------------------------------ services


@tool(
    "system.services",
    title="List background services",
    description=(
        "List system services, with whether each is loaded, active, and its current "
        "state. Use a query to filter by service name; use the state filter to see "
        "only running, failed, or inactive services."
    ),
    capability="system.service.read",
    read_only=True,
    timeout_s=10.0,
    requires=["systemctl"],
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "maxLength": 100,
                "description": "Only services whose name contains this.",
            },
            "state": {
                "type": "string",
                "enum": ["all", "active", "inactive", "failed"],
                "default": "all",
                "description": "Show only services in this state.",
            },
        },
    },
)
def system_services(params: dict[str, Any]) -> ToolResult:
    query = (params.get("query") or "").lower()
    state = str(params.get("state") or "all")

    proc = _run(
        ["systemctl", "list-units", "--type=service", "--all",
         "--no-pager", "--no-legend", "--plain"],
        timeout=10.0,
    )
    if proc is None or proc.returncode != 0:
        return ToolResult(
            ok=False,
            error="systemctl could not list services on this computer.",
            summary="No service information available",
        )

    all_units = _parse_list_units(proc.stdout)
    if not all_units:
        return ToolResult(
            ok=False,
            error="systemctl returned no services to list.",
            summary="No services found",
        )

    counts: dict[str, int] = {}
    for u in all_units:
        counts[u["active"]] = counts.get(u["active"], 0) + 1

    shown = [
        u for u in all_units
        if (state == "all" or u["active"] == state)
        and (not query or query in u["unit"].lower())
    ]
    shown.sort(key=lambda u: u["unit"])

    summary_parts = [f"{len(all_units)} service(s)"]
    summary_parts += [f"{n} {s}" for s, n in sorted(counts.items(), key=lambda kv: -kv[1])]
    return ToolResult(
        ok=True,
        data={
            "services": shown,
            "total": len(all_units),
            "shown": len(shown),
            "by_state": counts,
        },
        summary=", ".join(summary_parts),
    )


@tool(
    "system.service_status",
    title="Read one service's status",
    description=(
        "Report the full status of one system service: whether it is enabled, its "
        "active state, main process, and (optionally) its most recent log lines."
    ),
    capability="system.service.read",
    read_only=True,
    timeout_s=10.0,
    requires=["systemctl"],
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
                "description": "The service name, e.g. \"ssh\" or \"accounts-daemon.service\".",
            },
            "lines": {
                "type": "integer",
                "minimum": 0,
                "maximum": 50,
                "default": 0,
                "description": "How many recent log lines to include (0 = header only).",
            },
        },
        "required": ["name"],
    },
)
def system_service_status(params: dict[str, Any]) -> ToolResult:
    name = str(params["name"]).strip()
    if "." not in name:
        name += ".service"

    fields = _show_unit(name)
    if fields is None:
        return ToolResult(
            ok=False,
            error=f"There is no unit called '{name}' on this computer.",
            summary=f"{name} not found",
        )

    lines = int(params.get("lines", 0) or 0)
    status_text = None
    proc = _run(
        ["systemctl", "status", name, "--no-pager", f"--lines={lines}"],
        timeout=10.0,
    )
    if proc is not None and proc.returncode == 0:
        # The cgroup/ExecStart lines can carry command lines; the same redactor the
        # logs use is applied before anything reaches a model.
        status_text = redact(proc.stdout.strip()) or None

    active = fields.get("ActiveState", "unknown")
    return ToolResult(
        ok=True,
        data={
            "name": name,
            "description": fields.get("Description"),
            "load_state": fields.get("LoadState"),
            "active_state": active,
            "sub_state": fields.get("SubState"),
            "unit_file_state": fields.get("UnitFileState") or None,
            "main_pid": int(fields["MainPID"]) if fields.get("MainPID", "").isdigit() else None,
            "status": status_text,
        },
        summary=f"{name}: {active}"
                + ("" if fields.get("SubState") == "dead" else f" ({fields.get('SubState')})"),
    )


# ----------------------------------------------------------------- packages


@tool(
    "system.packages_installed",
    title="List installed packages",
    description=(
        "List the software packages installed on this computer. Use a query to find "
        "a specific package; omit it to see an overall count and a sample."
    ),
    capability="system.packages.read",
    read_only=True,
    timeout_s=15.0,
    requires=["dpkg-query"],
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "maxLength": 100,
                "description": "Only packages whose name contains this.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 2000,
                "default": 200,
                "description": "Maximum number of packages to return.",
            },
        },
    },
)
def system_packages_installed(params: dict[str, Any]) -> ToolResult:
    query = (params.get("query") or "").lower()
    limit = int(params.get("limit", 200))

    proc = _run(
        ["dpkg-query", "-W", "-f=${Package}\t${Version}\t${db:Status-Status}\n"],
        timeout=15.0,
    )
    if proc is None or proc.returncode != 0:
        return ToolResult(
            ok=False,
            error="dpkg-query could not read the package database.",
            summary="No package information available",
        )

    installed = _parse_packages(proc.stdout)
    matches = [p for p in installed if not query or query in p["name"].lower()]
    matches.sort(key=lambda p: p["name"])

    truncated = len(matches) > limit
    shown = matches[:limit]
    return ToolResult(
        ok=True,
        data={
            "packages": shown,
            "total_installed": len(installed),
            "matching": len(matches),
            "truncated": truncated,
        },
        summary=f"{len(matches)} package(s) match"
                + (f"; {len(shown)} shown of {len(installed)} installed" if query else
                   f"; {len(installed)} installed total"),
    )


@tool(
    "system.package_search",
    title="Search for available packages",
    description=(
        "Search the software repositories for a package by name and read its "
        "one-line description."
    ),
    capability="system.packages.read",
    read_only=True,
    timeout_s=15.0,
    requires=["apt-cache"],
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
                "description": "The package name (or fragment) to search for.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "default": 50,
                "description": "Maximum number of results to return.",
            },
        },
        "required": ["query"],
    },
)
def system_package_search(params: dict[str, Any]) -> ToolResult:
    query = str(params["query"]).strip()
    limit = int(params.get("limit", 50))

    # Passed as a list element, never through a shell, so the query cannot be
    # interpreted by a shell even if it is hostile punctuation.
    proc = _run(["apt-cache", "search", "--names-only", query], timeout=15.0)
    if proc is None:
        return ToolResult(
            ok=False,
            error="apt-cache is not available, so NEXUS cannot search packages.",
            summary="Package search unavailable",
        )
    if proc.returncode != 0:
        return ToolResult(
            ok=False,
            error=f"Package search failed: {redact(proc.stderr.strip()) or 'unknown error'}",
            summary="Package search failed",
        )

    results: list[dict[str, str]] = []
    for line in proc.stdout.splitlines():
        name, _, description = line.partition(" - ")
        if name:
            results.append({"name": name.strip(), "description": description.strip()})

    truncated = len(results) > limit
    shown = results[:limit]
    return ToolResult(
        ok=True,
        data={"results": shown, "matching": len(results), "truncated": truncated},
        summary=f"{len(results)} package(s) match '{query}'",
    )


# ---------------------------------------------------------------------- cron


@tool(
    "system.cron_list",
    title="Read scheduled jobs",
    description=(
        "List this account's scheduled jobs (crontab), and how many active entries "
        "there are. Credential-shaped values in the entries are redacted."
    ),
    capability="system.cron.read",
    read_only=True,
    timeout_s=6.0,
    requires=["crontab"],
    parameters={"type": "object", "properties": {}},
)
def system_cron_list(_params: dict[str, Any]) -> ToolResult:
    import getpass

    proc = _run(["crontab", "-l"], timeout=6.0)
    if proc is None:
        return ToolResult(
            ok=False,
            error="crontab is not available, so NEXUS could not read scheduled jobs.",
            summary="Cron unavailable",
        )

    # exit 1 with "no crontab for <user>" is the *normal* state of an account with
    # no scheduled jobs -- it is not an error, and reporting it as one would teach
    # the model to treat "nothing scheduled" as a failure. The message goes to
    # stderr, not stdout, so both streams are checked.
    if proc.returncode != 0:
        if "no crontab for" in (proc.stdout + proc.stderr).lower():
            return ToolResult(
                ok=True,
                data={"entries": [], "active_entries": 0, "raw": None},
                summary=f"No crontab for {getpass.getuser()}",
            )
        return ToolResult(
            ok=False,
            error=f"Could not read the crontab: {redact(proc.stderr.strip()) or 'unknown error'}",
            summary="Could not read crontab",
        )

    raw = proc.stdout.strip()
    lines = [redact(line) for line in raw.splitlines() if line.strip()]

    # An active entry is a non-comment line that is not an environment assignment
    # (VARIABLE=value). ``@reboot`` and ``@daily`` short-hands count as entries too,
    # since they are a real scheduled job.
    active = 0
    for line in lines:
        s = line.lstrip()
        if not s or s.startswith("#"):
            continue
        if "=" in s and s.split("=", 1)[0].isidentifier():
            continue  # VARIABLE=... environment assignment, not a job
        active += 1

    return ToolResult(
        ok=True,
        data={"entries": lines, "active_entries": active, "raw": None},
        summary=f"{len(lines)} line(s), {active} scheduled job(s)",
    )