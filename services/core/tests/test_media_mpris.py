"""Playback control over D-Bus, without playerctl installed.

``playerctl`` is a package; MPRIS is a bus interface. NEXUS speaks the interface, so
media control works on a machine where nobody installed the client — which is what
these tests fix in place. The JSON here is not invented: it was captured from
``busctl --user get-property`` against a real Rhythmbox playing a real file, quirks
included (an artist arrives as an *array*, a length in microseconds, and Rhythmbox
refuses ``Position`` outright while stopped).

Only ``_busctl`` is stubbed — the process boundary. Everything above it, including
which method each action maps to, is the real code path.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from nexus.tools import media

RHYTHMBOX = "org.mpris.MediaPlayer2.rhythmbox"

LIST_NAMES = json.dumps({"type": "as", "data": [[
    "org.freedesktop.DBus", "org.freedesktop.Notifications", ":1.7",
    "org.mpris.MediaPlayer2.rhythmbox", "org.pulseaudio.Server",
]]})

METADATA = json.dumps({"type": "a{sv}", "data": {
    "mpris:trackid": {"type": "s", "data": "/org/mpris/MediaPlayer2/Track/12"},
    "xesam:url": {"type": "s", "data": "file:///tmp/probe.wav"},
    "xesam:title": {"type": "s", "data": "probe.wav"},
    "xesam:artist": {"type": "as", "data": ["Aretha Franklin", "The Band"]},
    "xesam:album": {"type": "s", "data": "Unknown"},
    "mpris:length": {"type": "x", "data": 60_000_000},
}})

EMPTY_METADATA = json.dumps({"type": "a{sv}", "data": {}})


class Bus:
    """Stands in for the ``busctl`` process. Answers keyed by property name."""

    def __init__(self, properties: dict[str, str], *, names: str = LIST_NAMES,
                 call_code: int = 0, call_error: str = "") -> None:
        self.properties = properties
        self.names = names
        self.call_code = call_code
        self.call_error = call_error
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], timeout: float = 8.0) -> tuple[int, str, str]:
        self.calls.append(args)
        if args[0] == "call" and args[1] == "org.freedesktop.DBus":
            return 0, self.names, ""
        if args[0] == "call":
            return self.call_code, "", self.call_error
        prop = args[-1]
        if prop in self.properties:
            return 0, self.properties[prop], ""
        # A player that will not answer exits non-zero with its reason on stderr —
        # exactly how Rhythmbox refuses Position. Not an error to report upward.
        return 1, "", f"Failed to get property {prop}: not available"


PLAYING = {
    "PlaybackStatus": json.dumps({"type": "s", "data": "Playing"}),
    "Metadata": METADATA,
    "Identity": json.dumps({"type": "s", "data": "Rhythmbox"}),
    "Position": json.dumps({"type": "x", "data": 3_000_000}),
}


@pytest.fixture
def bus(monkeypatch: pytest.MonkeyPatch):
    """Install a stub bus and force the busctl backend, as on a machine without playerctl."""

    def install(stub: Bus) -> Bus:
        monkeypatch.setattr(media, "_busctl", stub)
        monkeypatch.setattr(media, "backend", lambda: "busctl")
        return stub

    return install


def test_the_tools_are_available_with_only_busctl_installed() -> None:
    from nexus.tools import registry

    # The declared requirement is an alternatives group, so "playerctl is missing" is
    # not the same fact as "there is no way to control playback".
    for name in ("media.control", "media.status"):
        tool = registry.get(name)
        assert "playerctl|busctl" in tool.requires, tool.requires


def test_a_playing_player_is_read_over_dbus(bus) -> None:
    bus(Bus(PLAYING))
    state = media._now_playing()

    assert state is not None
    assert state["status"] == "playing"
    assert state["title"] == "probe.wav"
    # An MPRIS artist is an array. Two artists read as two, and one does not read
    # as ['Artist'].
    assert state["artist"] == "Aretha Franklin, The Band"
    assert state["player"] == "Rhythmbox", "the player's own Identity, not its bus name"
    # Microseconds in the protocol, seconds for a person.
    assert state["position_seconds"] == 3.0
    assert state["length_seconds"] == 60.0


def test_only_mpris_names_count_as_players(bus) -> None:
    bus(Bus(PLAYING))
    assert media._players() == ["rhythmbox"], "the bus is full of names that are not players"


def test_a_property_the_player_refuses_is_not_an_error(bus) -> None:
    without_position = {k: v for k, v in PLAYING.items() if k != "Position"}
    bus(Bus(without_position))
    state = media._now_playing()

    assert state is not None, "Rhythmbox refuses Position while it plays; that is not a failure"
    assert "position_seconds" not in state
    assert state["status"] == "playing"


def test_missing_metadata_still_reports_the_status(bus) -> None:
    bus(Bus({**PLAYING, "Metadata": EMPTY_METADATA}))
    state = media._now_playing()

    assert state == {"status": "playing", "artist": "", "title": "", "album": "",
                     "player": "Rhythmbox", "position_seconds": 3.0}


@pytest.mark.parametrize("action,method", [
    ("play", "Play"), ("pause", "Pause"), ("toggle", "PlayPause"),
    ("stop", "Stop"), ("next", "Next"), ("previous", "Previous"),
])
def test_each_action_calls_the_mpris_method_it_claims(bus, action: str, method: str) -> None:
    stub = bus(Bus(PLAYING))
    code, error = media._command("rhythmbox", action)

    assert (code, error) == (0, "")
    sent = [args for args in stub.calls if args[0] == "call" and args[1] == RHYTHMBOX]
    assert [args[-1] for args in sent] == [method]
    assert sent[0][2] == media.MPRIS_PATH
    assert sent[0][3] == media.MPRIS_PLAYER_IFACE


def test_with_no_player_named_the_one_that_is_playing_is_used(bus) -> None:
    stub = bus(Bus(PLAYING))
    media._command("", "pause")

    sent = [args for args in stub.calls if args[0] == "call" and args[1] == RHYTHMBOX]
    assert sent and sent[0][-1] == "Pause"


def test_the_player_s_own_refusal_is_passed_up_not_swallowed(bus) -> None:
    bus(Bus(PLAYING, call_code=1, call_error="Call failed: Current playing source is NULL"))
    code, error = media._command("rhythmbox", "play")

    assert code == 1
    assert "Current playing source is NULL" in error, \
        "the player's own words are the only honest error here"


def test_nothing_running_is_an_answer_not_a_failure(bus) -> None:
    bus(Bus({}, names=json.dumps({"type": "as", "data": [["org.freedesktop.DBus"]]})))
    from nexus.tools import registry
    result = registry.get("media.status").handler({})

    assert result.ok, "no player running is a true answer to 'what is playing'"
    assert result.data["players"] == []
    assert "Nothing is playing" in result.summary


def test_a_named_player_that_is_not_running_is_refused_with_the_list(bus) -> None:
    bus(Bus(PLAYING))
    from nexus.tools import registry
    result = registry.get("media.control").handler({"action": "play", "player": "spotify"})

    assert not result.ok
    assert "spotify" in result.error and "rhythmbox" in result.error, \
        "naming what is running turns a refusal into something the user can act on"


def test_the_verifier_believes_the_player_not_the_command(bus) -> None:
    # The command succeeded and the player says it is stopped. The player wins.
    bus(Bus({**PLAYING, "PlaybackStatus": json.dumps({"type": "s", "data": "Stopped"})}))
    from nexus.tools.registry import ToolResult

    ok, text = media._verify_media(
        {}, ToolResult(ok=True, data={"action": "pause", "player": "rhythmbox"}, summary="Paused"))

    assert ok is False
    assert "stopped" in text and "paused" in text


def test_unwrap_leaves_plain_values_alone() -> None:
    assert media._unwrap({"type": "s", "data": "x"}) == "x"
    assert media._unwrap({"type": "as", "data": ["a", "b"]}) == ["a", "b"]
    # A dict that merely *has* those keys among others is data, not a variant.
    payload: dict[str, Any] = {"type": "s", "data": "x", "extra": 1}
    assert media._unwrap(payload) == payload
