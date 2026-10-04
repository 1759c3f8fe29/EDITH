"""Process reads that the flat lister cannot answer (tool batch B1; spec §38, §60).

``system.processes`` already lists every process and will sort by CPU or memory
and cut to a limit -- so a ``system.top_cpu`` or ``system.top_memory`` would be
that same tool wearing a different name, and is deliberately **not** added here
(§78: a tool whose only job is to look like new capability is not capability).

What the flat list genuinely cannot do is answer two shape-of-the-system
questions:

* ``system.process_tree`` -- who launched whom. The list gives each process a
  ``ppid`` but never assembles the hierarchy, so "what did this daemon spawn" or
  "what is the ancestry of pid 9574" has no single-call answer.
* ``system.process_by_port`` -- which program owns a listening port. Nothing in
  the registry maps ``:8080`` to the process holding it; this reads that mapping
  from ``ss``.

Both are reads and carry the existing ``system.processes`` capability -- the same
one the lister uses, for the same reason: a command line names the files, sites
and accounts being worked on, so this is not implicit machine state. Neither
changes anything, so neither has a verifier (§60): there is nothing to confirm.

The ``/proc`` parsing is imported from :mod:`nexus.tools.system` rather than
reimplemented, so the tree and the list cannot disagree about what a process is.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .registry import ToolResult, tool
from .system import (
    _STATE_WORDS,
    _cmdline,
    _process_snapshot,
    _proc_row,
    _run,
)

# ss prints socket owners as: users:(("name",pid=1234,fd=5),("other",pid=6,fd=7))
# The name is double-quoted and may itself contain commas, parentheses and spaces
# ("omniroute (v16." is a real example on this machine), so the pid cannot be
# found by splitting on punctuation -- only the quoted-name-then-pid shape is
# unambiguous. A socket owned by another user prints no users:(...) at all.
_SS_OWNER = re.compile(r'\("([^"]*)",pid=(\d+),fd=\d+\)')


# --------------------------------------------------------------- process tree


def _split_host_port(local: str) -> tuple[str, str]:
    """Split ss's "address:port" where the address may be IPv6 or a '*' wildcard.

    ss writes IPv6 locals as ``[::1]:8080`` and wildcards as ``*:5353``; the port
    is always after the *last* colon, never a colon inside an IPv6 address.
    """
    host, _, port = local.rpartition(":")
    return host.strip("[]"), port


@tool(
    "system.process_tree",
    title="Show the process hierarchy",
    description=(
        "Show which processes launched which others, as a tree. Give a pid to root "
        "the tree at that process and see only its descendants; omit it to see the "
        "whole hierarchy from the top."
    ),
    capability="system.processes",
    read_only=True,
    timeout_s=10.0,
    parameters={
        "type": "object",
        "properties": {
            "pid": {
                "type": "integer",
                "minimum": 1,
                "description": "Root the tree at this process. Omit for the whole system.",
            },
            "max_depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": 8,
                "description": "How many generations deep to expand.",
            },
            "include_cmdline": {
                "type": "boolean",
                "default": False,
                "description": "Include each process's full command line (more revealing, larger result).",
            },
        },
    },
)
def system_process_tree(params: dict[str, Any]) -> ToolResult:
    root_pid = params.get("pid")
    max_depth = int(params.get("max_depth", 8))
    include_cmdline = bool(params.get("include_cmdline", False))

    snap = _process_snapshot()
    if not snap:
        return ToolResult(
            ok=False,
            error="Could not read /proc, so NEXUS cannot build the process tree.",
            summary="No process information available",
        )

    children: dict[int, list[int]] = defaultdict(list)
    for pid, row in snap.items():
        children[row["ppid"]].append(pid)
    for kids in children.values():
        kids.sort()

    if root_pid is not None and root_pid not in snap:
        return ToolResult(
            ok=False,
            error=f"No process with pid {root_pid} is running, so there is no tree to show.",
            summary=f"pid {root_pid} not found",
        )

    # A whole-system tree can be hundreds of nodes; cap what is emitted so the
    # result stays legible and small enough to reason over. Two different caps can
    # bite, and both are reported rather than left for the reader to notice:
    #   * NODE_BUDGET -- the absolute number of nodes in the result;
    #   * max_depth  -- how many generations deep to expand before marking a
    #                   branch ``children_truncated`` and stopping.
    NODE_BUDGET = 800
    emitted = 0
    truncated = {"hit": False}
    depth_truncated = {"hit": False}

    def build(pid: int, depth: int) -> dict[str, Any] | None:
        nonlocal emitted
        if emitted >= NODE_BUDGET:
            truncated["hit"] = True
            return None
        row = snap.get(pid)
        if row is None:
            return None
        emitted += 1
        node: dict[str, Any] = {
            "pid": pid,
            "name": row["name"],
            "state": _STATE_WORDS.get(row["state"], row["state"]),
            "ppid": row["ppid"],
        }
        if include_cmdline:
            node["cmdline"] = _cmdline(pid)
        kids = children.get(pid, [])
        node["child_count"] = len(kids)
        if kids:
            if depth >= max_depth:
                node["children_truncated"] = True
                depth_truncated["hit"] = True
            else:
                built = [build(k, depth + 1) for k in kids]
                node["children"] = [c for c in built if c is not None]
        return node

    if root_pid is not None:
        forest = [build(root_pid, 0)]
        scope = f"pid {root_pid}"
    else:
        # Real roots: ppid 0 (the kernel's ancestor of init) or a ppid that is not
        # itself a live process. On a normal system this is pid 1 and pid 2.
        roots = sorted(
            pid for pid, row in snap.items() if row["ppid"] == 0 or row["ppid"] not in snap
        )
        forest = [build(r, 0) for r in roots]
        scope = "whole system"

    forest = [n for n in forest if n is not None]
    data = {
        "tree": forest,
        "total_processes": len(snap),
        "nodes_shown": emitted,
        "truncated": truncated["hit"],
        "depth_truncated": depth_truncated["hit"],
        "scope": scope,
    }
    note = "truncated" if truncated["hit"] or depth_truncated["hit"] else None
    summary = f"Process tree for {scope}: {emitted} of {len(snap)} processes shown"
    if note:
        summary += f" ({note})"
    return ToolResult(ok=True, data=data, summary=summary)


# ------------------------------------------------------------ process by port


def _listening_sockets(protocol: str) -> list[dict[str, Any]]:
    """Listening sockets for one protocol, parsed from ``ss -lpnH``.

    Owners with no visible pid (a socket owned by another user, which needs root
    to attribute) come back with an empty owner list rather than being dropped, so
    "port 80 is held by something you can't see" stays a truthful answer.
    """
    flag = "-t" if protocol == "tcp" else "-u"
    proc = _run(["ss", "-lpnH", flag])
    if proc is None or proc.returncode != 0:
        return []
    sockets: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        host, port = _split_host_port(parts[3])
        if not port.isdigit():
            continue
        owners = [
            {"name": name, "pid": int(pid)} for name, pid in _SS_OWNER.findall(line)
        ]
        sockets.append(
            {
                "protocol": protocol,
                "port": int(port),
                "address": host,
                "owners": owners,
                "owner_visible": bool(owners),
            }
        )
    return sockets


@tool(
    "system.process_by_port",
    title="Find what is using a port",
    description=(
        "Find which program is listening on a network port. Give a port number to "
        "look up just that one, or omit it to list everything currently listening."
    ),
    capability="system.processes",
    read_only=True,
    timeout_s=8.0,
    requires=["ss"],
    parameters={
        "type": "object",
        "properties": {
            "port": {
                "type": "integer",
                "minimum": 1,
                "maximum": 65535,
                "description": "The port to look up. Omit to list all listening ports.",
            },
            "protocol": {
                "type": "string",
                "enum": ["tcp", "udp", "both"],
                "default": "both",
                "description": "Which protocol(s) to include.",
            },
        },
    },
)
def system_process_by_port(params: dict[str, Any]) -> ToolResult:
    port = params.get("port")
    protocol = str(params.get("protocol", "both") or "both")

    protos = ("tcp", "udp") if protocol == "both" else (protocol,)
    sockets: list[dict[str, Any]] = []
    for proto in protos:
        sockets.extend(_listening_sockets(proto))

    if port is not None:
        sockets = [s for s in sockets if s["port"] == int(port)]

    # Enrich each visible owner with its (redacted) command line, reading each pid
    # once even when it owns several sockets.
    cmd_cache: dict[int, str] = {}
    for sock in sockets:
        for owner in sock["owners"]:
            pid = owner["pid"]
            if pid not in cmd_cache:
                row = _proc_row(pid)
                cmd_cache[pid] = _cmdline(pid) if row is not None else ""
            owner["cmdline"] = cmd_cache[pid]

    sockets.sort(key=lambda s: (s["port"], s["protocol"]))

    if port is not None and not sockets:
        # A truthful, useful answer -- not a failure. Nothing holds the port.
        return ToolResult(
            ok=True,
            data={"port": int(port), "sockets": []},
            summary=f"Nothing is listening on port {port}.",
        )

    data: dict[str, Any] = {"sockets": sockets, "count": len(sockets)}
    if port is not None:
        data["port"] = int(port)
        named = [o["name"] for s in sockets for o in s["owners"]]
        if named:
            summary = f"Port {port} is held by {', '.join(sorted(set(named)))}."
        else:
            summary = f"Port {port} is in use, but its owner is not visible without root."
    else:
        summary = f"{len(sockets)} listening socket(s) found."
    return ToolResult(ok=True, data=data, summary=summary)
