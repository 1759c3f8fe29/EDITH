"""Behaviour of the additive process-read tools (batch B1, second increment).

The flat ``system.processes`` lister cannot show ancestry or map a port to its
owner; these two tools exist only because they answer questions the lister does
not. Each test drives a *crafted* snapshot or ``ss`` output through the real
handler, so the assertion turns on the tool's own parsing and tree/root logic --
not on what this machine happens to report.

The values are chosen so a specific mutant fails:

* the orphan process (ppid 555, which is not alive) is its own tree root, so a
  mutant whose root rule is only ``ppid == 0`` drops it;
* one ``ss`` line has *no* owner and one owner name contains ``(`` and a comma,
  so a mutant that splits owners on punctuation or drops ownerless sockets fails;
* an empty port result is ``ok`` with an honest summary, so a mutant that turns
  "nothing here" into an error fails.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nexus.tools import process_read, registry


def _call(name: str, params: dict[str, Any] | None = None) -> registry.ToolResult:
    tool = registry.get(name)
    assert tool is not None, f"{name} is not registered"
    cleaned = registry.validate(tool.parameters, params or {})
    return tool.handler(cleaned)


def _snap() -> dict[int, dict[str, Any]]:
    """A synthetic process table: two real roots, three descendants, one orphan."""
    return {
        1: {"pid": 1, "name": "systemd", "state": "S", "ppid": 0},
        2: {"pid": 2, "name": "kthreadd", "state": "S", "ppid": 0},
        10: {"pid": 10, "name": "bash", "state": "S", "ppid": 1},
        11: {"pid": 11, "name": "sleep", "state": "R", "ppid": 10},
        999: {"pid": 999, "name": "orphan", "state": "Z", "ppid": 555},
    }


# ------------------------------------------------------ process hierarchy


def test_tree_roots_include_orphaned_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process whose parent is gone is its own root, not invisible."""
    monkeypatch.setattr(process_read, "_process_snapshot", _snap)

    result = _call("system.process_tree", {})

    assert result.ok
    roots = result.data["tree"]
    assert sorted(n["pid"] for n in roots) == [1, 2, 999]
    assert result.data["total_processes"] == 5
    assert result.data["nodes_shown"] == 5
    # the child links are assembled, not just listed
    one = next(n for n in roots if n["pid"] == 1)
    assert [c["pid"] for c in one["children"]] == [10]
    assert [c["pid"] for c in one["children"][0]["children"]] == [11]


def test_tree_pid_scopes_to_the_subtree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given a pid, only that process's descendants come back."""
    monkeypatch.setattr(process_read, "_process_snapshot", _snap)

    result = _call("system.process_tree", {"pid": 10})

    assert result.ok
    assert result.data["total_processes"] == 5  # still counted, just not drawn
    assert result.data["nodes_shown"] == 2
    tree = result.data["tree"]
    assert len(tree) == 1
    assert tree[0]["pid"] == 10
    assert [c["pid"] for c in tree[0]["children"]] == [11]


def test_tree_max_depth_stops_short_of_leaf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Depth is honoured, and a stopped branch says so instead of silently dropping."""
    monkeypatch.setattr(process_read, "_process_snapshot", _snap)

    result = _call("system.process_tree", {"pid": 1, "max_depth": 1})

    assert result.ok
    top = result.data["tree"][0]
    child = top["children"][0]
    assert child["pid"] == 10
    assert child.get("children_truncated") is True
    assert "children" not in child
    assert result.data["nodes_shown"] == 2
    # the fact that *something* was cut by depth is disclosed at the top too
    assert result.data["depth_truncated"] is True


def test_tree_include_cmdline_adds_command_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_read, "_process_snapshot", _snap)
    monkeypatch.setattr(process_read, "_cmdline", lambda pid: f"cmd:{pid}")

    result = _call("system.process_tree", {"pid": 10, "include_cmdline": True})

    assert result.data["tree"][0]["cmdline"] == "cmd:10"
    assert result.data["tree"][0]["children"][0]["cmdline"] == "cmd:11"


def test_tree_unknown_pid_is_an_honest_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_read, "_process_snapshot", _snap)

    result = _call("system.process_tree", {"pid": 4242})

    assert not result.ok
    assert "4242" in result.error


def test_tree_empty_snapshot_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_read, "_process_snapshot", lambda: {})

    result = _call("system.process_tree", {})

    assert not result.ok


# --------------------------------------------------------- port attribution


def test_split_host_port_handles_ipv6_and_wildcard() -> None:
    assert process_read._split_host_port("[::1]:8080") == ("::1", "8080")
    assert process_read._split_host_port("*:5353") == ("*", "5353")
    assert process_read._split_host_port("127.0.0.1:51649") == ("127.0.0.1", "51649")


def test_listening_sockets_parse_hidden_and_punctuated_owners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner names may contain '(' and commas, and a socket may have no owner."""
    stdout = (
        'LISTEN 0 511 127.0.0.1:51649 0.0.0.0:* users:(("code",pid=7102,fd=36))\n'
        'LISTEN 0 128 0.0.0.0:80 0.0.0.0:*\n'
        'LISTEN 0 511 [::1]:8080 [::]:* '
        'users:(("omniroute (v16.",pid=9574,fd=21),("other,svc",pid=5,fd=3))\n'
    )
    monkeypatch.setattr(
        process_read, "_run", lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=stdout)
    )

    socks = process_read._listening_sockets("tcp")

    assert len(socks) == 3
    by_port = {s["port"]: s for s in socks}
    assert by_port[51649]["owners"] == [{"name": "code", "pid": 7102}]
    assert by_port[51649]["owner_visible"] is True
    # held by something, but ss needs root to name it -- that is not "free".
    assert by_port[80]["owners"] == []
    assert by_port[80]["owner_visible"] is False
    # names with parentheses and commas survive un-mangled.
    assert by_port[8080]["owners"] == [
        {"name": "omniroute (v16.", "pid": 9574},
        {"name": "other,svc", "pid": 5},
    ]


def test_listening_sockets_none_when_ss_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_read, "_run", lambda *_a, **_k: None)
    assert process_read._listening_sockets("tcp") == []

    monkeypatch.setattr(
        process_read, "_run", lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="")
    )
    assert process_read._listening_sockets("udp") == []


def test_process_by_port_filters_to_the_requested_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sockets = [
        {"protocol": "tcp", "port": 80, "address": "0.0.0.0",
         "owners": [{"name": "nginx", "pid": 1}], "owner_visible": True},
        {"protocol": "tcp", "port": 8080, "address": "0.0.0.0",
         "owners": [{"name": "app", "pid": 2}], "owner_visible": True},
    ]
    # The default is "both": the tool fetches tcp then udp and merges. A fake that
    # handed the same list to both would double-count the tcp sockets, so mirror
    # the real function's per-protocol split here.
    monkeypatch.setattr(
        process_read, "_listening_sockets", lambda p: sockets if p == "tcp" else []
    )
    monkeypatch.setattr(process_read, "_proc_row", lambda _pid: None)

    result = _call("system.process_by_port", {"port": 8080})

    assert result.ok
    assert result.data["count"] == 1
    assert result.data["sockets"][0]["port"] == 8080


def test_process_by_port_nothing_listening_isnt_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_read, "_listening_sockets", lambda _p: [])

    result = _call("system.process_by_port", {"port": 999})

    assert result.ok
    assert result.data["port"] == 999
    assert result.data["sockets"] == []
    assert "Nothing is listening on port 999" in result.summary


def test_process_by_port_enriches_owners_with_cmdline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sockets = [
        {"protocol": "tcp", "port": 8080, "address": "0.0.0.0",
         "owners": [{"name": "app", "pid": 7102}], "owner_visible": True},
    ]
    monkeypatch.setattr(
        process_read, "_listening_sockets", lambda p: sockets if p == "tcp" else []
    )
    monkeypatch.setattr(process_read, "_proc_row", lambda pid: {"pid": pid})
    monkeypatch.setattr(process_read, "_cmdline", lambda _pid: "curl -x http://redacted")

    result = _call("system.process_by_port", {"port": 8080})

    assert result.data["count"] == 1
    assert result.data["sockets"][0]["owners"][0]["cmdline"] == "curl -x http://redacted"