"""Notifications, parsed from bytes a real desktop actually produced (O11).

Two things are being pinned, and the second is the one that matters.

**The parser reads the real wire format.** The sample below was captured on this
machine by running ``dbus-monitor --session "interface='org.freedesktop.Notifications'"``
while ``notify-send -a "NEXUS probe" -u normal "Format probe" "Capturing the dbus
signal shape"`` fired, then pasted in verbatim. That matters because the argument
order is the contract: a notification is a *method call*, not a signal, and its
body is the fourth string argument. A parser written from the documentation rather
than from bytes is a parser that has never been wrong yet.

**A notification body is untrusted text that is often a secret.** One-time codes
and password-reset links arrive this way constantly. The §56 test asserts that a
body which looks like an API key is recorded as *a category* and that the key itself
appears nowhere in the observation -- not in the summary, not in the data. A secret
that reaches the ambient store outlives the code it was.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus.monitors.notification_monitor import NotificationMonitor, parse_notify

#: Verbatim from this machine, 2026-10-04. Do not tidy: the indentation (three
#: leading spaces, as dbus-monitor emits) and the ``variant`` wrappers are the
#: format, and a "cleaned up" fixture would stop testing the real thing.
CAPTURED = '''signal time=1791166237.340651 sender=org.freedesktop.DBus -> destination=:1.121 serial=2 path=/org/freedesktop/DBus; interface=org.freedesktop.DBus; member=NameAcquired
   string ":1.121"
method call time=1791166238.847160 sender=:1.122 -> destination=:1.35 serial=7 path=/org/freedesktop/Notifications; interface=org.freedesktop.Notifications; member=GetServerInformation
method call time=1791166238.849089 sender=:1.122 -> destination=:1.35 serial=9 path=/org/freedesktop/Notifications; interface=org.freedesktop.Notifications; member=Notify
   string "NEXUS probe"
   uint32 0
   string ""
   string "Format probe"
   string "Capturing the dbus signal shape"
   array [
   ]
   array [
      dict entry(
         string "urgency"
         variant             byte 1
      )
      dict entry(
         string "sender-pid"
         variant             int64 5990
      )
   ]
   int32 -1
'''


def notify_lines(*, app: str, summary: str, body: str, urgency: str = "1") -> list[str]:
    """A minimal Notify call in the same shape, for cases the capture does not cover."""
    return [
        "method call time=1.0 sender=:1.5 -> destination=:1.6 serial=2 "
        "path=/org/freedesktop/Notifications; "
        "interface=org.freedesktop.Notifications; member=Notify\n",
        f'   string "{app}"\n',
        "   uint32 0\n",
        '   string ""\n',
        f'   string "{summary}"\n',
        f'   string "{body}"\n',
        "   array [\n",
        "   ]\n",
        "   array [\n",
        "      dict entry(\n",
        '         string "urgency"\n',
        f"         variant             byte {urgency}\n",
        "      )\n",
        "   ]\n",
        "   int32 -1\n",
    ]


# ------------------------------------------------------------------ the parser


def test_the_real_captured_notification_parses_completely() -> None:
    """Every field, from the bytes a desktop on this machine sent."""
    found = parse_notify(CAPTURED.splitlines(keepends=True))
    assert len(found) == 1, "the capture contains exactly one Notify call"
    item = found[0]
    assert item["app"] == "NEXUS probe"
    assert item["summary"] == "Format probe"
    assert item["body"] == "Capturing the dbus signal shape"
    # byte 1 is "normal" in the Desktop Notification Specification.
    assert item["urgency"] == "normal"


def test_get_server_information_is_not_mistaken_for_a_notification() -> None:
    """The same interface carries calls that are not notifications.

    The stub is deliberately *complete* -- it ends in the ``int32`` that commits a
    notification -- because an earlier version of this test stopped short of it.
    That version passed whether or not the parser distinguished the two methods, so
    it proved nothing; a loose ``interface`` match survived it.
    """
    lines = [
        "method call time=1.0 sender=:1.5 -> destination=:1.6 serial=1 "
        "path=/org/freedesktop/Notifications; "
        "interface=org.freedesktop.Notifications; member=GetServerInformation\n",
        '   string "xfce4-notifyd"\n',
        '   string "xfce"\n',
        '   string "1.0"\n',
        '   string "1.2"\n',
        "   int32 -1\n",
    ]
    assert parse_notify(lines) == []


def test_several_notifications_in_one_read_are_all_kept() -> None:
    lines = notify_lines(app="A", summary="first", body="one")
    lines += notify_lines(app="B", summary="second", body="two", urgency="2")
    found = parse_notify(lines)
    assert [f["summary"] for f in found] == ["first", "second"]
    assert found[1]["urgency"] == "critical"


def test_urgency_values_map_to_the_specifications_words() -> None:
    for byte, word in (("0", "low"), ("1", "normal"), ("2", "critical")):
        found = parse_notify(notify_lines(app="A", summary="s", body="b", urgency=byte))
        assert found[0]["urgency"] == word


def test_a_half_written_call_is_not_reported_as_arrived() -> None:
    """Only the trailing int32 ends a notification; a truncated read keeps nothing."""
    lines = notify_lines(app="A", summary="cut off", body="b")[:-1]  # drop int32
    assert parse_notify(lines) == []


def test_escapes_in_a_body_survive() -> None:
    """dbus-monitor escapes quotes and newlines the way C does."""
    lines = notify_lines(app="A", summary="s", body='say \\"hi\\"')
    assert parse_notify(lines)[0]["body"] == 'say "hi"'


# ----------------------------------------------------------- the observations


def monitor() -> NotificationMonitor:
    return NotificationMonitor()


def test_an_ordinary_notification_becomes_an_observation() -> None:
    out = monitor()._observations(
        [{"app": "Slack", "summary": "Ravi: are we still on?", "body": "ping",
          "urgency": "normal"}]
    )
    assert len(out) == 1
    seen = out[0]
    assert seen.kind == "system.notification"
    assert seen.severity == "notice"
    assert seen.data["app"] == "Slack"
    assert seen.data["body"] == "ping"
    assert "Slack" in seen.summary and "Ravi" in seen.summary


def test_a_critical_notification_is_an_alert() -> None:
    out = monitor()._observations(
        [{"app": "upower", "summary": "Battery critical", "body": "", "urgency": "critical"}]
    )
    assert out[0].severity == "alert"


def test_a_notification_with_no_text_at_all_is_skipped() -> None:
    """Some applications send empty notifications as progress ticks."""
    assert monitor()._observations(
        [{"app": "thing", "summary": "   ", "body": "", "urgency": "low"}]
    ) == []


def test_a_secret_in_the_body_is_never_stored() -> None:
    """The §56 boundary, asserted on a shape that really arrives this way."""
    secret = "nvapi-DEADBEEF1234567890abcdefghijklmnopqrstuvwxyz"
    out = monitor()._observations(
        [{"app": "npm", "summary": "Your token", "body": f"token: {secret}",
          "urgency": "normal"}]
    )
    assert len(out) == 1
    seen = out[0]
    # Recorded as a category...
    assert seen.data.get("body_withheld") is True
    assert seen.data.get("sensitive_category")
    # ...and the value appears nowhere in the observation.
    blob = f"{seen.summary}{seen.data}"
    assert secret not in blob, "the secret reached the observation"
    assert "DEADBEEF" not in blob


def test_an_ordinary_body_is_kept_because_withholding_everything_would_be_useless(
) -> None:
    """A redactor that fires on every message is the same as no notification monitor."""
    out = monitor()._observations(
        [{"app": "git", "summary": "Build finished", "body": "took 42 seconds on main",
          "urgency": "normal"}]
    )
    assert out[0].data.get("body") == "took 42 seconds on main"
    assert "body_withheld" not in out[0].data


def test_a_very_long_body_is_bounded() -> None:
    out = monitor()._observations(
        [{"app": "A", "summary": "s", "body": "x" * 5_000, "urgency": "low"}]
    )
    assert len(str(out[0].data["body"])) <= 400


# ------------------------------------------------------------------ the posture


def test_it_is_off_until_the_user_turns_it_on() -> None:
    """Notifications are among the most personal things on a desktop."""
    from nexus.db.repos import app_settings

    assert app_settings.DEFAULTS["monitor_notifications_enabled"] is False


def test_it_declares_itself_as_observing_the_user() -> None:
    """The standing indicator (L5) must show this, or reading is invisible."""
    from nexus.monitors.supervisor import _OBSERVES_USER

    assert "notifications" in _OBSERVES_USER


def test_it_refuses_clearly_when_there_is_no_session_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running under a system service is a real state; the reason must name it."""
    from nexus.monitors.base import MonitorUnavailable

    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    with pytest.raises(MonitorUnavailable) as caught:
        monitor().check_available()
    assert "session bus" in str(caught.value)


def test_it_is_registered_with_the_supervisor() -> None:
    from nexus.monitors.supervisor import supervisor

    ids = [m.id for m in supervisor()._monitors.values()]
    assert "notifications" in ids


def test_the_capability_it_needs_exists_and_is_gated() -> None:
    """A monitor on an undeclared capability would be unpromptable and invisible."""
    from nexus.permissions import capabilities

    by_name = {c.name: c for c in capabilities.CAPABILITIES}
    assert "notifications.read" in by_name
    order = ["low", "medium", "high", "critical"]
    # Reading other applications' messages is not a low-risk act.
    assert order.index(by_name["notifications.read"].risk) >= order.index("medium")
