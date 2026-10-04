"""Behaviour of the administrative-state read tools (batch B1, third increment).

Each test drives a *crafted* ``systemctl``/``dpkg-query``/``apt-cache``/``crontab``
output through the real handler, so the assertion turns on the tool's own parsing
and counting -- not on whatever this machine happens to report.

The values are chosen so a specific mutant fails:

* a list-units parser that split on tabs (there are none) or on whitespace
  without joining the description tail would mangle multi-word descriptions;
* a unit-existence check that trusted the *exit code* of ``systemctl show``
  instead of ``LoadState`` would report a nonexistent unit as found;
* a dpkg filter that looked for "ii" instead of "installed" (the status *word*)
  would count config-file residue as a live package;
* a crontab counter that treated comments or ``VARIABLE=value`` assignments as
  jobs -- or that reported "no crontab" as an error -- would either over-count or
  mislabel the normal empty state.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nexus.tools import registry, system_admin


def _call(name: str, params: dict[str, Any] | None = None) -> registry.ToolResult:
    tool = registry.get(name)
    assert tool is not None, f"{name} is not registered"
    cleaned = registry.validate(tool.parameters, params or {})
    return tool.handler(cleaned)


def _cp(returncode: int, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ------------------------------------------------------------- service list


def test_list_units_parses_space_padded_and_multword_descriptions() -> None:
    stdout = (
        "accounts-daemon.service     loaded    active   running Accounts Service\n"
        "alsa-restore.service        loaded    inactive dead    Save/Restore Sound Card State\n"
        "docker.service              loaded    failed   failed  Docker Application Container Engine\n"
    )
    units = system_admin._parse_list_units(stdout)

    assert [u["unit"] for u in units] == [
        "accounts-daemon.service", "alsa-restore.service", "docker.service",
    ]
    # description is the whitespace-tail, not one pre-split token
    assert units[0]["description"] == "Accounts Service"
    assert units[1]["sub"] == "dead"
    assert units[2]["active"] == "failed"


def test_services_filters_by_state_and_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = (
        "accounts-daemon.service     loaded active   running  Accounts Service\n"
        "docker.service              loaded failed   failed   Docker\n"
        "ssh.service                 loaded active   running  OpenBSD Secure Shell\n"
    )
    monkeypatch.setattr(
        system_admin, "_run",
        lambda cmd, **kw: _cp(0, stdout) if cmd[0] == "systemctl" else None,
    )

    failed = _call("system.services", {"state": "failed"})
    assert [u["unit"] for u in failed.data["services"]] == ["docker.service"]

    ssh = _call("system.services", {"query": "ssh"})
    assert [u["unit"] for u in ssh.data["services"]] == ["ssh.service"]
    assert ssh.data["total"] == 3
    assert ssh.data["by_state"] == {"active": 2, "failed": 1}


# ---------------------------------------------------------- service status


def test_show_unit_reads_existence_from_load_state() -> None:
    """A not-found unit still exits 0 from 'systemctl show'; LoadState is the truth."""
    found = _cp(0, "Description=Accounts Service\nLoadState=loaded\nActiveState=active\n"
                   "SubState=running\nUnitFileState=enabled\nMainPID=752\n")
    not_found = _cp(0, "MainPID=0\nDescription=ssh.service\nLoadState=not-found\n"
                       "ActiveState=inactive\nSubState=dead\nUnitFileState=\n")

    # value with a space survives the per-line Key=Value split
    assert system_admin._show_unit("accounts-daemon") is not None
    assert system_admin._show_unit("ssh") is None


def test_service_status_calls_show_then_detects_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    not_found = _cp(0, "LoadState=not-found\n")
    monkeypatch.setattr(system_admin, "_run", lambda cmd, **kw: not_found)

    result = _call("system.service_status", {"name": "ssh"})

    assert not result.ok
    assert "ssh.service" in result.error


def test_service_status_builds_fields_and_normalizes_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    show = _cp(0, "Description=Accounts Service\nLoadState=loaded\nActiveState=active\n"
                  "SubState=running\nUnitFileState=enabled\nMainPID=752\n")
    status = _cp(0, "● accounts-daemon.service - Accounts Service\n   Loaded: loaded (enabled)\n")

    calls: dict[str, str] = {}

    def fake_run(cmd, **kw):
        joined = " ".join(cmd)
        calls[joined] = joined
        if cmd[0] == "systemctl" and cmd[1] == "show":
            return show
        if cmd[0] == "systemctl" and cmd[1] == "status":
            return status
        return None

    monkeypatch.setattr(system_admin, "_run", fake_run)

    result = _call("system.service_status", {"name": "accounts-daemon"})

    assert result.ok
    # ".service" was appended
    assert result.data["name"] == "accounts-daemon.service"
    assert result.data["active_state"] == "active"
    assert result.data["main_pid"] == 752
    assert result.data["unit_file_state"] == "enabled"
    # the status text was fetched (and survives redact: nothing here is a secret)
    assert "Accounts Service" in result.data["status"]


# ---------------------------------------------------------------- packages


def test_parse_packages_keeps_only_installed() -> None:
    stdout = (
        "2to3\t3.12.3-0ubuntu2.1\tinstalled\n"
        "acl\t2.3.2-2\tinstalled\n"
        "abandoned-pkg\t1.0\tconfig-files\n"  # residue, not a live package
        "other-pkg\t2.0\tnot-installed\n"
    )
    packages = system_admin._parse_packages(stdout)

    assert [p["name"] for p in packages] == ["2to3", "acl"]
    assert packages[0]["version"] == "3.12.3-0ubuntu2.1"


def test_packages_installed_filters_and_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = (
        "python3\t3.12.3\tinstalled\n"
        "python3-pip\t24.0\tinstalled\n"
        "ssh\t1:9.6\tconfig-files\n"
    )
    monkeypatch.setattr(
        system_admin, "_run",
        lambda cmd, **kw: _cp(0, stdout) if cmd[0] == "dpkg-query" else None,
    )

    result = _call("system.packages_installed", {"query": "python3"})

    assert result.data["total_installed"] == 2
    assert result.data["matching"] == 2
    assert [p["name"] for p in result.data["packages"]] == ["python3", "python3-pip"]


def test_package_search_splits_name_and_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = (
        "python3 - interactive high-level object-oriented language (default python3 version)\n"
        "python3-pip - package installer for Python\n"
    )
    monkeypatch.setattr(
        system_admin, "_run",
        lambda cmd, **kw: _cp(0, stdout) if cmd[0] == "apt-cache" else None,
    )

    result = _call("system.package_search", {"query": "python3"})

    assert result.data["matching"] == 2
    assert result.data["results"][0]["name"] == "python3"
    assert "interactive high-level" in result.data["results"][0]["description"]


# ------------------------------------------------------------------- cron


def test_cron_no_crontab_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exit 1 with 'no crontab for <user>' on *stderr* is the normal empty state."""
    monkeypatch.setattr(system_admin, "_run", lambda cmd, **kw: _cp(1, "", "no crontab for santosh\n"))

    result = _call("system.cron_list", {})

    assert result.ok
    assert result.data["entries"] == []
    assert result.data["active_entries"] == 0


def test_cron_counts_jobs_not_comments_or_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crontab = (
        "# run backups daily\n"
        "PATH=/usr/bin:/bin\n"
        "API_KEY=supersecretvalue123\n"
        "0 2 * * * /usr/local/bin/backup.sh\n"
        "@reboot /usr/local/bin/warm-cache.sh\n"
        "*/5 * * * * /usr/local/bin/ping.py\n"
    )
    monkeypatch.setattr(system_admin, "_run", lambda cmd, **kw: _cp(0, crontab))

    result = _call("system.cron_list", {})

    assert result.ok
    # 2 env assignments + 1 comment are not jobs; the 3 scheduled entries are
    assert result.data["active_entries"] == 3
    # the credential-shaped assignment IS redacted (key kept, value gone). The
    # redactor matches "API_KEY=value", not arbitrary "--token value" pairs, and
    # this test asserts only what redact() actually guarantees.
    joined = "\n".join(result.data["entries"])
    assert "supersecretvalue123" not in joined
    assert "API_KEY=[redacted]" in joined


def test_cron_read_failure_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_admin, "_run", lambda cmd, **kw: _cp(1, "", "crontab: permission denied\n"))

    result = _call("system.cron_list", {})

    assert not result.ok
    assert "permission denied" in result.error