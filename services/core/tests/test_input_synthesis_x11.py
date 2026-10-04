"""Input synthesis exercised against a nested X server (task L11; §12, §38, §60, §65).

Why this file was so long in coming. ``input.*`` sends real keystrokes to whatever
window has focus, so a test of it on the live session would seize the user's keyboard
and type into whatever they happened to be looking at. The task entry for L11 said as
much and stayed unfinished: *"it needs a deliberate test against a scratch window,
with a human watching."* That is not a gap patience closes -- it needs a **different
display**.

``Xephyr`` is one: a nested X server with its own root window, pointer, keyboard and
XTEST extension, driven through the same ``xdotool`` calls, with no effect on anything
outside it. Inside it, verification stops being about exit codes. The probe application
in ``fixtures/x11_probe_app.py`` writes what it actually received to a JSON file, so a
passing test here means *the text is in the field* -- not that xdotool returned 0. The
tools themselves can only verify delivery and say so (§60); these tests can go the one
step further that the tools honestly cannot.

It found a real bug on the first run, which is the argument for the whole approach:
``xdotool getwindowclassname`` does not exist in the version Debian and Ubuntu ship,
and its stderr was being stored as the window class -- disabling the class channel of
the credential guard while every call still reported success. Two tests below exist
specifically so that cannot come back.

Skipped, with a reason, when the display cannot be built. A skip that says why is the
point; a test that quietly passes without running is how L11 got here.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable

import pytest

from nexus.tools import input as input_tool
from x11_harness import SCREEN, Nested, Probe, send, skip_reason, wait

# The display, the window manager and the probe window all come from x11_harness, which
# `test_window_manage_x11.py` shares. `nested` and `probe` are imported as fixtures --
# pytest resolves them by name in this module -- and re-exported so nothing here looks
# like an unused import.
from x11_harness import nested, probe  # noqa: F401

_SKIP = skip_reason()
pytestmark = pytest.mark.skipif(bool(_SKIP), reason=_SKIP)


# ------------------------------------------------------- the effect, not the exit code


def test_typed_text_arrives_in_the_field(probe: Callable[..., Probe]) -> None:
    """The text is in the entry, read from the application's own state.

    This is the assertion the tools cannot make about themselves. ``input.type``
    confirms delivery and says plainly that it cannot see whether the application
    acted (§60); from in here, it can be seen.
    """
    window = probe()
    result = send("input.type", {"text": "hello from nexus"})

    assert result.ok, result.error
    assert result.data["window_title"] == "NEXUS input probe"
    wait(lambda: window.state()["entry"] == "hello from nexus",
          "the typed text to appear in the entry")


def test_typing_reports_the_window_it_went_to(probe: Callable[..., Probe]) -> None:
    """Which window received the keystrokes is a fact in the result, not an assumption.

    The docstring's first promise is "no blind typing". The window id must belong to
    the probe -- a plausible-looking id for the wrong window is the failure this
    reports against.
    """
    window = probe()
    result = send("input.type", {"text": "x"})

    assert result.ok, result.error
    # Tk's winfo_id is the client window; xdotool reports the frame the WM reparented
    # it into. Same window, two ids, so the relationship is what can be asserted.
    assert int(result.data["window_id"]) >= window.state()["window_id"]
    assert result.data["still_focused"] is True
    assert result.verified is True
    assert "delivery only" in result.verification


def test_a_shortcut_reaches_the_application(probe: Callable[..., Probe]) -> None:
    """input.key with a modifier arrives as that combination, not as literal letters."""
    window = probe()
    assert send("input.key", {"keys": "ctrl+c"}).ok
    wait(lambda: "ctrl+c" in window.state()["keys"], "ctrl+c to be received")
    assert window.state()["entry"] == "", "a shortcut must not type its own letters"


def test_repeat_presses_arrive_as_separate_keys(probe: Callable[..., Probe]) -> None:
    """`repeat` sends n keystrokes, and the application counts n of them.

    A loop that sends once and reports `repeat: 3` passes any test that only reads
    the result, which is why the count comes from inside the application.
    """
    window = probe()
    result = send("input.key", {"keys": "Escape", "repeat": 3})

    assert result.ok, result.error
    assert result.data["repeat"] == 3
    wait(lambda: window.state()["keys"].count("Escape") == 3, "three Escape presses")


def test_press_enter_is_a_second_keystroke(probe: Callable[..., Probe]) -> None:
    """`press_enter` types the text *and* sends Return, both observably."""
    window = probe()
    result = send("input.type", {"text": "submit me", "press_enter": True})

    assert result.ok, result.error
    assert result.data["press_enter"] is True
    wait(lambda: window.state()["entry"] == "submit me", "the text")
    wait(lambda: "Return" in window.state()["keys"], "the Return that followed it")


# --------------------------------------------------------------------- the pointer


def test_a_click_presses_the_button_it_was_aimed_at(probe: Callable[..., Probe]) -> None:
    """CLICK -> CONFIRM UI CHANGE, which is what §12 asks for instead of assuming."""
    window = probe()
    x, y = window.centre("confirm")
    result = send("input.click", {"x": x, "y": y})

    assert result.ok, result.error
    assert result.data["moved_to"] == {"x": x, "y": y}
    wait(lambda: window.state()["clicks"] == 1, "the button to register the click")
    assert window.state()["label"] == "clicked 1"


def test_a_double_click_is_two_presses(probe: Callable[..., Probe]) -> None:
    window = probe()
    x, y = window.centre("confirm")
    assert send("input.click", {"x": x, "y": y, "double": True}).ok
    wait(lambda: window.state()["clicks"] == 2, "both presses of the double click")


def test_clicking_the_same_spot_twice_does_not_hang(probe: Callable[..., Probe]) -> None:
    """Agent mode clicks the same button twice constantly; that used to cost 10s a go.

    ``xdotool mousemove --sync`` waits for the pointer to move *away from* where it
    started, so asking it to go somewhere it already is never returns: the tool sat
    there until its own timeout and the click came back as a pointer failure that had
    not happened. Measured before the fix: first move 0.01s, second move to the same
    coordinates 12.0s and killed. So this test asserts both halves -- the second click
    lands, and it lands promptly. The duration bound is loose on purpose; the bug was
    an order of magnitude out, not a few milliseconds.
    """
    window = probe()
    x, y = window.centre("confirm")

    first = send("input.click", {"x": x, "y": y})
    assert first.ok, first.error
    wait(lambda: window.state()["clicks"] == 1, "the first click")

    second = send("input.click", {"x": x, "y": y})
    assert second.ok, second.error
    assert second.data["moved_to"] == {"x": x, "y": y}
    wait(lambda: window.state()["clicks"] == 2, "the second click at the same spot")
    assert second.duration_ms < 5_000, (
        f"the second click took {second.duration_ms}ms; a no-op pointer move is hanging"
    )


def test_a_click_off_the_screen_is_refused_before_the_pointer_moves(
    probe: Callable[..., Probe],
) -> None:
    """The bounds check runs before the move, so nothing is clicked at all."""
    window = probe()
    result = send("input.click", {"x": SCREEN[0] + 40, "y": 10})

    assert result.ok is False
    assert f"{SCREEN[0]}x{SCREEN[1]}" in (result.error or "")
    assert window.state()["clicks"] == 0


def test_pointer_position_reads_the_display_it_is_on(
    nested: Nested, probe: Callable[..., Probe]
) -> None:
    """What the tool reports is what the display says, asked independently.

    This asserted equality with the click's target coordinates until it failed once in
    four runs while passing alone -- and a test that is right three times out of four
    is not evidence of anything. The claim worth pinning is narrower than the one that
    flaked: `input.pointer_position` reports *this* display's pointer and *this*
    display's size, so it is checked against a separate `getmouselocation` read of the
    same instant, plus the button's own rectangle rather than its exact centre. That
    the click lands where it is aimed is already proved by effect, in the app's own
    click counter, by `test_a_click_presses_the_button_it_was_aimed_at`.
    """
    window = probe()
    x, y = window.centre("confirm")
    assert send("input.click", {"x": x, "y": y}).ok

    result = send("input.pointer_position", {})
    assert result.ok, result.error
    assert (result.data["screen_width"], result.data["screen_height"]) == SCREEN
    assert result.verified is True

    code, out = nested.run(["xdotool", "getmouselocation", "--shell"])
    assert code == 0, out
    truth = dict(
        line.split("=", 1) for line in out.splitlines() if "=" in line
    )
    assert (result.data["x"], result.data["y"]) == (int(truth["X"]), int(truth["Y"]))

    box = window.state()["confirm"]
    assert box["x"] <= result.data["x"] <= box["x"] + box["width"]
    assert box["y"] <= result.data["y"] <= box["y"] + box["height"]


# ------------------------------------------------------------------ the guards hold


def test_the_reported_window_class_is_a_class_not_an_error_message(
    probe: Callable[..., Probe],
) -> None:
    """The bug this file was written to find, and the reason it cannot come back.

    ``xdotool getwindowclassname`` does not exist in 3.20160805 (Debian's and
    Ubuntu's). ``_xdotool`` returns its stderr as the value, and the caller discarded
    the ok flag -- so every result carried *"xdotool: Unknown command:
    getwindowclassname"* in the field the credential guard matches on. It read as
    data, which is precisely what §48 forbids.
    """
    probe()
    result = send("input.type", {"text": "x"})

    assert result.ok, result.error
    assert result.data["window_class"] == "Tk"
    assert "xdotool" not in result.data["window_class"].lower()
    assert "class_caveat" not in result.data, "the class was read, so there is no caveat"


def test_a_credential_title_is_refused_and_nothing_is_typed(
    probe: Callable[..., Probe],
) -> None:
    """A refusal that still typed would be worse than no guard at all."""
    window = probe(title="Unlock your keyring")
    result = send("input.type", {"text": "hunter2"})

    assert result.ok is False
    assert "its title" in (result.error or "")
    assert window.state()["entry"] == "", "the refusal must happen before any keystroke"


def test_a_password_manager_known_only_by_its_class_is_refused(
    probe: Callable[..., Probe],
) -> None:
    """Title "Vault", class "Bitwarden" -- the case the dead channel let through.

    The module docstring promises refusal near "a password manager". A real one is
    often identifiable only by its window class, so this is not a corner case; it is
    the promise. Before the fix this test typed the password into the manager and
    reported success.
    """
    window = probe(title="Vault", wm_class="Bitwarden")
    result = send("input.type", {"text": "hunter2"})

    assert result.ok is False
    assert "window class" in (result.error or ""), result.error
    assert "Bitwarden" in (result.error or "")
    assert window.state()["entry"] == ""


def test_a_click_into_a_credential_window_is_refused_too(
    probe: Callable[..., Probe],
) -> None:
    """The guard is on the focus read, so the pointer is covered by it as well."""
    window = probe(title="Vault", wm_class="Bitwarden")
    x, y = window.centre("confirm")
    result = send("input.click", {"x": x, "y": y})

    assert result.ok is False
    assert window.state()["clicks"] == 0


def test_a_missing_xprop_is_said_out_loud_rather_than_narrowing_the_guard(
    probe: Callable[..., Probe], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without xprop the class channel is gone -- and the result has to admit it.

    This is the lesson of the bug generalised. The guard degrading is survivable; the
    guard degrading *silently*, while every result still reads like a full check ran,
    is the thing that let a password manager through for weeks. So the caveat travels
    in the data and into the verification sentence the user is shown.
    """
    window = probe()
    real = shutil.which
    monkeypatch.setattr(
        input_tool.shutil, "which", lambda name: None if name == "xprop" else real(name)
    )
    result = send("input.type", {"text": "x"})

    assert result.ok, result.error
    assert result.data["window_class"] == ""
    assert "xprop" in result.data["class_caveat"]
    assert "not checked" in result.data["class_caveat"]
    assert "xprop" in (result.verification or ""), "the caveat must reach the user"
    wait(lambda: window.state()["entry"] == "x", "the keystroke, which is still sent")


def test_text_over_the_limit_is_refused_before_a_single_keystroke(
    probe: Callable[..., Probe],
) -> None:
    """The length check is a refusal, not a truncation.

    Only the application's own state can tell those apart: a truncating implementation
    returns the same error and leaves 5,000 characters in the field.
    """
    window = probe()
    result = send("input.type", {"text": "x" * (input_tool.MAX_TYPE_LENGTH + 1)})

    assert result.ok is False
    assert "clipboard" in (result.error or "")
    assert window.state()["entry"] == ""


def test_an_unknown_key_name_never_reaches_the_keyboard(
    probe: Callable[..., Probe],
) -> None:
    """The key allowlist is checked before xdotool sees the string."""
    window = probe()
    result = send("input.key", {"keys": "ctrl+exec"})

    assert result.ok is False
    assert "not a key NEXUS recognises" in (result.error or "")
    assert window.state()["keys"] == []
    assert window.state()["entry"] == ""
