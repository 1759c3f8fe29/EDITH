"""Window management exercised against a nested X server (task L12; §22, §30, §60).

L12 has said the same thing for a fortnight: *"Move and resize read-back is no longer
blocked -- `xdotool` and `xwininfo` are both present -- but has not been run, because
it would rearrange the windows of the session doing the work."* That objection is real
and it is not one patience solves, any more than L11's was. What solves it is the same
thing: a display NEXUS owns, built by ``x11_harness``, where a window can be moved,
resized, maximised and minimised without touching anything the user is looking at.

Every assertion here reads the X server directly rather than the ``after`` dict the
tool returns. That is the whole point of the file: ``window.arrange`` verifies itself by
reading geometry back (§60), and a test that checked that verification by reading the
same field the verifier populated would be asking the tool whether it agreed with
itself. Here the tool says what it did, and the X server is asked separately whether it
happened.

This file's first run found a real defect, which is the argument for the approach:
``_geometry`` read positions through ``xdotool getwindowgeometry``, which adds the
window's offset *inside its frame* to the translated origin -- so under metacity every
successful move came back unverified, with the tool reporting "x is 146 rather than
120" about a window sitting exactly where it had been asked to go. ``wmctrl -lG``
performs the same arithmetic and agrees with it, which is how the wrong reading looked
independently confirmed. The same run showed that Tk's ``winfo_id()`` is the *inner*
window -- one id below the one the window manager actually manages -- so reading
``_NET_WM_STATE`` from it returns "not found" for everything, indistinguishable from a
window manager that ignored every request. Both halves of the read-back now use the
window the WM manages, and the position from ``xwininfo`` minus the frame extents.

What the module docstring in ``tools/windows.py`` promises is that a window manager may
refuse or adjust a request, and that "the command exited 0" is therefore not evidence.
These tests are that claim's only real check: metacity is a real window manager with
real frame borders and its own opinions about geometry.
"""
from __future__ import annotations

from typing import Callable

import pytest

from x11_harness import SCREEN, Nested, Probe, send, skip_reason, wait

# `nested` and `probe` come from the harness and are resolved by pytest by name.
from x11_harness import nested, probe  # noqa: F401

#: wmctrl on top of the harness's own needs: `window.arrange` drives states and
#: workspaces through it, and `_find_window` resolves every target from its listing.
_SKIP = skip_reason("wmctrl")
pytestmark = pytest.mark.skipif(bool(_SKIP), reason=_SKIP)

TITLE = "NEXUS input probe"


def arrange(**params: object) -> object:
    return send("window.arrange", params)


def managed(nested: Nested, title: str = TITLE) -> str:
    """The id of the window the *window manager* manages for ``title``.

    Tk wraps its toplevels, so ``Probe.window_id()`` names a window one level below
    the one metacity frames. All state lives on the wrapper; reading it from the inner
    window returns nothing.
    """
    return nested.managed_id(title)


# ------------------------------------------------------------ geometry, read back


def test_a_move_puts_the_window_where_the_x_server_says_it_is(
    nested: Nested, probe: Callable[..., Probe]
) -> None:
    """The claim is about the window's real position, so the X server is asked.

    ``window.arrange`` reports ``after.geometry`` and its verifier compares that with
    the request. Reading that same dict back would test nothing; this reads
    ``xwininfo`` independently, in a second process, after the fact -- the origin
    without the frame offset ``xdotool getwindowgeometry`` would have added.
    """
    window = probe()
    result = arrange(action="move", window=TITLE, x=120, y=90)

    assert result.ok, result.error
    assert result.verified is True, result.verification

    left, _r, top, _b = nested.frame_extents(managed(nested))
    assert nested.absolute_position(managed(nested)) == (120 + left, 90 + top)


def test_a_resize_changes_the_size_and_leaves_the_position(
    nested: Nested, probe: Callable[..., Probe]
) -> None:
    window = probe()
    before = nested.absolute_position(managed(nested))
    result = arrange(action="resize", window=TITLE, width=420, height=260)

    assert result.ok, result.error
    assert result.verified is True, result.verification

    # The window's own idea of its size, which is the application's state rather than
    # either of the tools' arithmetic.
    wait(lambda: nested.size(managed(nested)) == (420, 260),
         "the window to report the new size")
    # A resize that also moved the window would be a different action than the one
    # asked for, and the user would have to hunt for the window afterwards.
    assert nested.absolute_position(managed(nested)) == before


def test_centre_puts_the_window_in_the_middle_of_this_screen(
    nested: Nested, probe: Callable[..., Probe]
) -> None:
    """Centred means the *frame* is centred on the display, not the client area.

    The frame is what the user sees. Centring the client area instead leaves a
    decorated window visibly high and left, by half its own decoration -- and the
    tool's read-back would confirm it as centred, because the reader agrees with the
    centring arithmetic.
    """
    window = probe()
    assert arrange(action="resize", window=TITLE, width=400, height=240).ok
    result = arrange(action="center", window=TITLE)

    assert result.ok, result.error
    left, right, top, bottom = nested.frame_extents(managed(nested))
    x, y = nested.absolute_position(managed(nested))
    width, height = nested.size(managed(nested))
    # The visible box is the origin the WM placed plus the decorations on each side.
    visible = (
        x - left, y - top, width + left + right, height + top + bottom,
    )
    # metacity is not exact about centring (it reserves a 1px strut and rounds), so
    # the claim pinned is "the visible box is centred to within the decoration width"
    # rather than to the pixel.
    assert abs((visible[0] + visible[2] // 2) - SCREEN[0] // 2) <= max(left, right) + 2
    assert abs((visible[1] + visible[3] // 2) - SCREEN[1] // 2) <= max(top, bottom) + 2


def test_the_window_is_still_reachable_after_being_arranged(
    nested: Nested, probe: Callable[..., Probe]
) -> None:
    """Every arrangement leaves the window somewhere the user could click on it.

    §22's point about window management is that it is for the user's benefit. A move
    that parks a window past the edge of the screen is technically successful and
    practically a loss. The tool refuses off-screen coordinates (tested below); this
    is the other half -- the coordinates it accepts leave the whole window on screen,
    where "whole window" means the frame, not just its corner.

    The last coordinate is the exact maximum that fits the probe's frame, chosen from
    the live window size rather than a constant: metacity clamps a move that would
    take part of a window off screen (a window manager may adjust a request), so a
    test that asked for more would be measuring the clamp rather than the move.
    """
    window = probe()
    mid = managed(nested)
    left, _r, top, _b = nested.frame_extents(mid)
    width, height = nested.size(mid)
    max_x, max_y = SCREEN[0] - width - left, SCREEN[1] - height - top
    for x, y in ((0, 0), (max_x, max_y), (max_x // 2, max_y // 2)):
        assert arrange(action="move", window=TITLE, x=x, y=y).ok, (x, y)
        px, py = nested.absolute_position(managed(nested))
        # The frame's corner is what the user could click on.
        assert 0 <= px - left < SCREEN[0], (px, py, left)
        assert 0 <= py - top < SCREEN[1], (px, py, top)


def test_a_move_that_would_take_part_of_the_window_off_screen_is_refused(
    nested: Nested, probe: Callable[..., Probe]
) -> None:
    """On the screen but half off it: the corner is reachable, the window is not.

    Found by this suite's own first run. `(680, 480)` passes the corner-only bounds
    check on an 800x600 screen, and metacity rescues it by clamping -- so the move
    came back honestly *unverified* ("the window manager applied something
    different") about a window nobody asked to move to (240,343). A window manager
    that honored the request blindly would have moved it and let the verifier
    *confirm* a window the user could not click on. The size check makes both cases a
    refusal before anything moves.
    """
    window = probe()
    before = nested.absolute_position(managed(nested))
    result = arrange(action="move", window=TITLE, x=SCREEN[0] - 100, y=100)

    assert result.ok is False
    assert f"{SCREEN[0]}x{SCREEN[1]}" in (result.error or ""), result.error
    assert nested.absolute_position(managed(nested)) == before


# --------------------------------------------------------------- states, read back


def test_maximise_is_confirmed_from_the_window_managers_own_state(
    nested: Nested, probe: Callable[..., Probe]
) -> None:
    """``_NET_WM_STATE`` is what the window manager publishes; nothing else counts."""
    window = probe()
    result = arrange(action="maximize", window=TITLE)

    assert result.ok, result.error
    assert result.verified is True, result.verification
    wait(lambda: "_NET_WM_STATE_MAXIMIZED_HORZ" in nested.wm_state(managed(nested)),
         "the window manager to publish the maximised state")
    assert "_NET_WM_STATE_MAXIMIZED_VERT" in nested.wm_state(managed(nested))


def test_unmaximise_takes_the_state_away_again(
    nested: Nested, probe: Callable[..., Probe]
) -> None:
    window = probe()
    assert arrange(action="maximize", window=TITLE).ok
    wait(lambda: "_NET_WM_STATE_MAXIMIZED_HORZ" in nested.wm_state(managed(nested)),
         "the maximised state to appear before it is removed")

    result = arrange(action="unmaximize", window=TITLE)
    assert result.ok, result.error
    wait(lambda: "MAXIMIZED" not in nested.wm_state(managed(nested)),
         "the maximised state to be removed")


def test_minimise_hides_the_window_and_restore_brings_it_back(
    nested: Nested, probe: Callable[..., Probe]
) -> None:
    """Two actions, because a minimise nobody can undo is a way to lose a window."""
    window = probe()
    result = arrange(action="minimize", window=TITLE)

    assert result.ok, result.error
    wait(lambda: "_NET_WM_STATE_HIDDEN" in nested.wm_state(managed(nested)),
         "the window to report itself hidden")

    back = arrange(action="restore", window=TITLE)
    assert back.ok, back.error
    wait(lambda: "_NET_WM_STATE_HIDDEN" not in nested.wm_state(managed(nested)),
         "the window to come back")


def test_fullscreen_and_back(nested: Nested, probe: Callable[..., Probe]) -> None:
    window = probe()
    assert arrange(action="fullscreen", window=TITLE).ok
    wait(lambda: "_NET_WM_STATE_FULLSCREEN" in nested.wm_state(managed(nested)),
         "the window to go fullscreen")
    assert nested.size(managed(nested)) == SCREEN

    assert arrange(action="unfullscreen", window=TITLE).ok
    wait(lambda: "_NET_WM_STATE_FULLSCREEN" not in nested.wm_state(managed(nested)),
         "the window to leave fullscreen")


# ------------------------------------------------------------- naming the target


def test_no_window_named_means_the_focused_one(
    nested: Nested, probe: Callable[..., Probe]
) -> None:
    """The convenience path still has to resolve to a specific window, and say which."""
    window = probe()
    result = arrange(action="move", x=200, y=140)

    assert result.ok, result.error
    assert result.data["title"] == TITLE
    left, _r, top, _b = nested.frame_extents(managed(nested))
    assert nested.absolute_position(managed(nested)) == (200 + left, 140 + top)


def test_an_ambiguous_name_is_refused_and_lists_the_candidates(
    probe: Callable[..., Probe],
) -> None:
    """Two matches is a question, not a coin toss.

    Guessing here moves the wrong window, and the user's own arrangement is not the
    tool's to rearrange on a guess. The refusal has to name what it found, or the user
    cannot answer it.
    """
    probe(title="Report draft")
    probe(title="Report final")
    result = arrange(action="maximize", window="Report")

    assert result.ok is False
    assert "2 windows match" in (result.error or ""), result.error
    assert "Report draft" in (result.error or "")
    assert "Report final" in (result.error or "")


def test_an_exact_title_beats_a_partial_one(
    nested: Nested, probe: Callable[..., Probe]
) -> None:
    """"Report" is ambiguous; "Report draft" is not, even though both are prefixes."""
    draft = probe(title="Report draft")
    probe(title="Report draft extended")
    result = arrange(action="move", window="Report draft", x=60, y=60)

    assert result.ok, result.error
    assert result.data["title"] == "Report draft"
    left, _r, top, _b = nested.frame_extents(nested.managed_id("Report draft"))
    assert nested.absolute_position(nested.managed_id("Report draft")) == (
        60 + left, 60 + top,
    )


def test_a_window_that_is_not_open_is_refused_by_name(
    probe: Callable[..., Probe],
) -> None:
    probe()
    result = arrange(action="maximize", window="Photoshop")

    assert result.ok is False
    assert "Photoshop" in (result.error or "")


# ------------------------------------------------------ refusals before the effect


def test_a_move_off_the_screen_is_refused_and_nothing_moves(
    nested: Nested, probe: Callable[..., Probe]
) -> None:
    """A window parked past the edge is unreachable, so this is a refusal not a clamp."""
    window = probe()
    before = nested.absolute_position(managed(nested))
    result = arrange(action="move", window=TITLE, x=SCREEN[0] + 500, y=10)

    assert result.ok is False
    assert f"{SCREEN[0]}x{SCREEN[1]}" in (result.error or ""), result.error
    assert nested.absolute_position(managed(nested)) == before


def test_a_move_without_coordinates_is_refused(probe: Callable[..., Probe]) -> None:
    probe()
    result = arrange(action="move", window=TITLE, x=100)

    assert result.ok is False
    assert "both x and y" in (result.error or "")


def test_a_resize_below_the_schema_minimum_never_reaches_the_tool(
    nested: Nested, probe: Callable[..., Probe]
) -> None:
    """The runtime validates before it executes, so a 1x1 window is not constructible."""
    window = probe()
    before = nested.absolute_position(managed(nested))
    result = arrange(action="resize", window=TITLE, width=10, height=10)

    assert result.ok is False
    assert "width" in (result.error or "").lower()
    assert nested.absolute_position(managed(nested)) == before


def test_an_unknown_action_is_refused_with_the_list_of_real_ones(
    probe: Callable[..., Probe],
) -> None:
    probe()
    result = arrange(action="explode", window=TITLE)

    assert result.ok is False
    assert "maximize" in (result.error or ""), result.error


# ------------------------------------------------------------------- workspaces


def test_workspaces_describes_the_display_it_is_on(probe: Callable[..., Probe]) -> None:
    """Read from wmctrl on the nested display, not from the session's desktop."""
    probe()
    result = send("window.workspaces", {})

    assert result.ok, result.error
    names = result.data["workspaces"]
    assert names, "a window manager with no workspaces at all is not a real reading"
    assert 0 <= result.data["current"] < len(names)
    # Each entry has to identify a workspace, because the point of the listing is that
    # the user can name one back. A list of blanks passes a length check and is useless.
    assert all(str(name).strip() for name in names), names


def test_a_workspace_that_does_not_exist_is_refused_with_the_real_count(
    probe: Callable[..., Probe],
) -> None:
    """The refusal has to carry the true range, or it is just a "no"."""
    probe()
    count = len(send("window.workspaces", {}).data["workspaces"])
    result = arrange(action="workspace", window=TITLE, workspace=count + 5)

    assert result.ok is False
    assert str(count) in (result.error or ""), result.error


def test_moving_to_another_workspace_is_confirmed_by_wmctrl(
    nested: Nested, probe: Callable[..., Probe]
) -> None:
    """Skipped rather than faked when the nested display has only one workspace."""
    probe()
    count = len(send("window.workspaces", {}).data["workspaces"])
    if count < 2:
        pytest.skip(f"the nested window manager offers {count} workspace(s)")

    result = arrange(action="workspace", window=TITLE, workspace=1)
    assert result.ok, result.error
    assert result.verified is True, result.verification
    wait(lambda: nested.workspace_of(TITLE) == 1, "wmctrl to list the window on 1")
