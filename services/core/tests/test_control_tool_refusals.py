"""Control tools that move the machine, tested where they refuse (audit item O4).

The system-control audit found 13 of 20 control tools with no test at all, and made
the argument for why that matters: "the happy path is the binary's" is not a reason
the *guard* code can go unchecked, and every one of these tools carries guards -- a
missing binary, a malformed argument, an out-of-scope path, a URL scheme it must
refuse -- that a live call on a working desktop never reaches.

The clipboard tests here earned their place the hard way. Writing the clipboard was
broken on this machine in a way no happy-path test would have caught: ``xclip``
forks a child that keeps holding the selection, that child inherited the capture
pipes, and ``communicate()`` waited for an EOF that never came -- a 5-second stall
on a write measured at 8 ms, followed by the tool reporting "none of wl-copy, xclip
or xsel is installed" about a program that was installed and had already copied the
text. The regression test asserts the call site passes no capture pipes, because
that is the fix; a timing assertion would be flaky and would not name the cause.
"""

from __future__ import annotations

from typing import Any

import pytest

def test_clipboard_write_does_not_wait_for_a_forked_holder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug: a 5-second stall on a write that takes 8 milliseconds.

    ``xclip`` and ``wl-copy`` fork a child that keeps holding the selection -- that
    is how a clipboard outlives the program that set it. Attaching stdout/stderr
    pipes let that child inherit the write ends, so ``communicate()`` waited for an
    EOF that never came, hit the timeout, and the loop moved on. The tool then told
    the user "none of wl-copy, xclip or xsel is installed" on a machine where xclip
    was installed **and had already copied the text**.

    Asserts the call site passes no capture pipes, which is the actual fix; a timing
    assertion would be flaky and would not name the cause.
    """
    from nexus.tools import system

    seen: dict[str, Any] = {}

    class FakeProc:
        returncode = 0

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(system.subprocess, "run", fake_run)
    monkeypatch.setattr(system, "_clipboard_writers", lambda: [("xclip", ["xclip", "-in"])])
    monkeypatch.setattr(system, "_read_clipboard_now", lambda: ("hello", ""))

    result = system.clipboard_set({"text": "hello"})

    assert result.ok, result.error
    assert "capture_output" not in seen["kwargs"], (
        "a capture pipe lets the forked selection holder inherit it, and the write "
        "then blocks until the timeout"
    )
    assert "stdout" not in seen["kwargs"] and "stderr" not in seen["kwargs"]
    assert seen["kwargs"]["input"] == "hello"


def test_clipboard_write_reports_honestly_when_nothing_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no writer at all, naming the missing program IS the right message."""
    from nexus.tools import system

    monkeypatch.setattr(system, "_clipboard_writers", lambda: [])
    monkeypatch.setattr(system, "_read_clipboard_now", lambda: (None, ""))

    result = system.clipboard_set({"text": "hello"})
    assert not result.ok
    assert "xclip" in (result.error or ""), "the fix must be named"


def test_clipboard_write_admits_when_the_text_did_not_stick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer that returned 0 but left the clipboard different is not success."""
    from nexus.tools import system

    class FakeProc:
        returncode = 0

    monkeypatch.setattr(system.subprocess, "run", lambda cmd, **kw: FakeProc())
    monkeypatch.setattr(system, "_clipboard_writers", lambda: [("xclip", ["xclip", "-in"])])
    # The clipboard ends up holding something else entirely.
    monkeypatch.setattr(system, "_read_clipboard_now", lambda: ("something else", ""))

    result = system.clipboard_set({"text": "hello"})
    assert not result.ok
    assert "did not take" in (result.error or "")
