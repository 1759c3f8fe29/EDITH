"""What a monitor is (task L1; spec §15, §45, §66).

A monitor is a small thing that looks at the computer on an interval and reports
what changed. The supervisor runs them; this file defines the contract they share
and the two rules every one of them obeys.

**Off until switched on.** ``Monitor.enabled()`` reads a setting that defaults to
False. NEXUS never arrives watching, and a monitor cannot opt itself in.

**An observation is a fact, not a conclusion.** A monitor reports that CPU hit
94%, that the active window became Firefox, that the screen changed. Deciding
whether any of that is worth mentioning is the proactive engine's job (L10), and
deciding whether to act on it is the user's (§31). Keeping that boundary here is
what stops an assistant that watches from becoming an assistant that meddles.

Observations carry a ``dedupe_key`` because the most common failure mode of a
monitor is not missing an event -- it is reporting the same one sixty times.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from ..logging_setup import get_logger

log = get_logger("monitors")

Severity = Literal["info", "notice", "alert"]


@dataclass
class Observation:
    """One thing a monitor noticed."""

    kind: str                      # "system.cpu", "window.focus", "screen.change"
    summary: str                   # one plain-English line
    data: dict[str, Any] = field(default_factory=dict)
    severity: Severity = "info"
    #: Identity of the *condition*, not the moment. Two observations with the same
    #: key describe the same ongoing situation, so the supervisor reports the
    #: first and suppresses the rest until it clears.
    dedupe_key: str = ""
    #: True when this observation says a previously-reported condition ended.
    clears: bool = False
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "data": self.data,
            "severity": self.severity,
            "dedupe_key": self.dedupe_key,
            "clears": self.clears,
            "at": self.at,
        }


class MonitorUnavailable(Exception):
    """The monitor cannot run here, and the reason is worth showing the user.

    Distinct from a transient error: a missing binary or a Wayland session will
    not fix itself on the next tick, so the supervisor stops the monitor and
    reports why instead of retrying forever (§78).
    """


@dataclass
class MonitorState:
    """What the Settings screen shows about one monitor."""

    id: str
    title: str
    enabled: bool
    running: bool
    interval_seconds: float
    samples: int = 0
    observations: int = 0
    last_run_at: str | None = None
    last_observation_at: str | None = None
    last_error: str | None = None
    consecutive_errors: int = 0
    unavailable_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "enabled": self.enabled,
            "running": self.running,
            "interval_seconds": self.interval_seconds,
            "samples": self.samples,
            "observations": self.observations,
            "last_run_at": self.last_run_at,
            "last_observation_at": self.last_observation_at,
            "last_error": self.last_error,
            "consecutive_errors": self.consecutive_errors,
            "unavailable_reason": self.unavailable_reason,
        }


def setting(key: str, default: Any) -> Any:
    """Read one setting, tolerating a database that does not exist yet.

    Monitors start during service startup, which can happen before or after the
    first migration depending on how the service was launched. A monitor that
    crashes the process because a settings table is missing would be a worse
    failure than one that uses its default, so this degrades instead.
    """
    try:
        from ..db.repos import app_settings

        return app_settings.get(key, default)
    except Exception:  # pragma: no cover - pre-migration
        log.debug("setting %s unavailable, using default", key, exc_info=True)
        return default


class Monitor(ABC):
    """One continuously-running observer."""

    #: Stable identifier, used in settings, events and the UI.
    id: str = ""
    title: str = ""
    description: str = ""
    #: Setting that turns this monitor on. Must default to False for anything
    #: that reads content -- screen, clipboard, window titles.
    enabled_setting: str = ""
    interval_setting: str = ""
    default_interval: float = 60.0
    #: Permission capability this monitor needs, checked once at start.
    capability: str = ""
    #: Activity kind its observations are recorded under.
    activity_kind: str = "system"
    #: When True, a single observation is also written to the Activity log. Noisy
    #: monitors set this False and rely on the ambient stream instead.
    record_to_activity: bool = True

    def __init__(self) -> None:
        self.state = MonitorState(
            id=self.id,
            title=self.title,
            enabled=False,
            running=False,
            interval_seconds=self.default_interval,
        )

    # ------------------------------------------------------------- configuration

    def enabled(self) -> bool:
        if not self.enabled_setting:
            return False
        return bool(setting(self.enabled_setting, False))

    def interval(self) -> float:
        if not self.interval_setting:
            return self.default_interval
        try:
            value = float(setting(self.interval_setting, self.default_interval))
        except (TypeError, ValueError):
            return self.default_interval
        # A one-second screen capture loop would be a performance problem the user
        # did not ask for; the floor is a guard, not a preference.
        return max(1.0, value)

    # ------------------------------------------------------------------ lifecycle

    def check_available(self) -> None:
        """Raise ``MonitorUnavailable`` if this monitor cannot work here.

        Called once before the loop starts. The default assumes availability;
        anything depending on an external binary must override it.
        """

    def check_permission(self) -> None:
        """Raise ``MonitorUnavailable`` if the capability is switched off.

        A background loop cannot prompt, so the rule is asymmetric on purpose. A
        capability the user *denied*, or turned off in Settings, stops the monitor
        and says so. A capability that has simply never come up does not: turning
        a watcher on in Settings is itself the consent for the one thing it does,
        and prompting again for what was just enabled would be theatre.

        The distinction matters because the alternative -- a monitor that starts
        anyway on a denied capability -- would make a permission decision
        meaningless as soon as it was made in the background (§31).
        """
        if not self.capability:
            return
        try:
            from ..permissions.engine import engine

            decision = engine.evaluate(self.capability)
        except Exception:  # pragma: no cover - engine unavailable pre-startup
            log.debug("permission check for %s unavailable", self.id, exc_info=True)
            return

        if decision.outcome == "deny":
            raise MonitorUnavailable(
                f"{decision.reason} {self.title} cannot run until that changes."
            )

    async def on_start(self) -> None:
        """Hook for setting up state that survives across samples."""

    async def on_stop(self) -> None:
        """Hook for releasing anything ``on_start`` acquired."""

    @abstractmethod
    async def sample(self) -> list[Observation]:
        """Look once, and return what changed.

        Returning an empty list is the normal case and means nothing happened --
        which is a real answer, not a failure. Raising means the look itself went
        wrong.
        """

    # ------------------------------------------------------------------ metrics

    def note_sample(self, observations: int) -> None:
        self.state.samples += 1
        self.state.last_run_at = datetime.now(timezone.utc).isoformat()
        if observations:
            self.state.observations += observations
            self.state.last_observation_at = self.state.last_run_at

    def to_dict(self) -> dict[str, Any]:
        data = self.state.to_dict()
        data.update(
            {
                "description": self.description,
                "enabled_setting": self.enabled_setting,
                "interval_setting": self.interval_setting,
                "capability": self.capability,
            }
        )
        return data


class Debouncer:
    """Suppresses repeats of an ongoing condition.

    A CPU sitting at 92% for an hour is one situation. Without this, a 60-second
    monitor reports it sixty times, and the notification that mattered is buried
    among fifty-nine that did not (§82).
    """

    def __init__(self, min_repeat_seconds: float = 900.0) -> None:
        self._active: dict[str, float] = {}
        self.min_repeat_seconds = min_repeat_seconds

    def should_report(self, observation: Observation) -> bool:
        key = observation.dedupe_key
        if not key:
            return True

        now = time.monotonic()
        if observation.clears:
            # A clear is only interesting if the condition was actually reported.
            return self._active.pop(key, None) is not None

        first_seen = self._active.get(key)
        if first_seen is None:
            self._active[key] = now
            return True
        if now - first_seen >= self.min_repeat_seconds:
            self._active[key] = now
            return True
        return False

    def active_keys(self) -> list[str]:
        return sorted(self._active)

    def reset(self) -> None:
        self._active.clear()
