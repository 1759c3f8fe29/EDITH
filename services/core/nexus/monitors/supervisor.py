"""The monitor supervisor (task L1; spec §15, §45, §66, §78).

Runs the enabled monitors, each in its own task, and holds the three properties
that make continuous watching safe to ship:

**Crash isolation.** One monitor raising cannot stop the others. Each loop catches
everything, counts consecutive failures, and after a few gives up *loudly* -- a
monitor that has stopped says so in Settings rather than appearing to run while
observing nothing. A watcher that looks alive and sees nothing is exactly the fake
state §78 forbids.

**Reconciliation, not restart.** ``apply_settings`` compares what should be running
against what is, and starts or stops only the difference. Toggling one monitor in
Settings does not disturb the others.

**A truthful indicator.** ``watching`` reports whether anything is observing the
user right now and what, so the UI's screen-access indicator (L5) is driven by
actual running tasks rather than by a flag someone remembered to set.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Iterable

from ..core.events import bus
from ..logging_setup import get_logger
from .base import Debouncer, Monitor, MonitorUnavailable, Observation

log = get_logger("monitors.supervisor")

#: After this many consecutive failures a monitor is stopped rather than looped.
MAX_CONSECUTIVE_ERRORS = 3

#: Monitors that observe the user rather than the machine. Used by ``watching`` so
#: the indicator reflects "something is looking at what I am doing", which is a
#: different question from "a background task is running".
_OBSERVES_USER = {"screen", "window", "clipboard"}

#: Task key for the retention sweep. Contains a colon so it can never collide
#: with a monitor id, and is filtered out of everything the user sees -- it is
#: not a watcher and must not appear as one on the Awareness screen.
_HOUSEKEEPING = "supervisor:housekeeping"

#: How often expired ambient events are swept. Retention is measured in days, so
#: hourly is precise enough and costs one indexed DELETE.
HOUSEKEEPING_INTERVAL = 3600.0


class Supervisor:
    def __init__(self) -> None:
        self._monitors: dict[str, Monitor] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._debouncers: dict[str, Debouncer] = {}
        self._started = False

    # ------------------------------------------------------------- registration

    def register(self, monitor: Monitor) -> None:
        if not monitor.id:
            raise ValueError("a monitor must have an id")
        if monitor.id in self._monitors:
            raise ValueError(f"monitor '{monitor.id}' is already registered")
        self._monitors[monitor.id] = monitor
        self._debouncers[monitor.id] = Debouncer()

    def register_all(self, monitors: Iterable[Monitor]) -> None:
        for monitor in monitors:
            self.register(monitor)

    def get(self, monitor_id: str) -> Monitor | None:
        return self._monitors.get(monitor_id)

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Start every monitor the settings say should run."""
        self._started = True
        await self.apply_settings()
        # Guarded because start() is safe to call again -- a settings change or a
        # reconnect should not leave a second sweep running against the first.
        if not self._is_running(_HOUSEKEEPING):
            self._tasks[_HOUSEKEEPING] = asyncio.create_task(
                self._housekeeping(), name="monitor:housekeeping"
            )

    async def _housekeeping(self) -> None:
        """Enforce ambient retention while the process is alive.

        Retention has to be enforced by something that runs, not only by a cutoff
        written on each row: a machine left on for a fortnight would otherwise
        accumulate a fortnight of observations that are all individually expired.
        This purges immediately on start -- catching whatever expired while NEXUS
        was closed -- and then hourly.
        """
        while True:
            try:
                from ..db.repos import ambient

                ambient.purge()
            except Exception:  # pragma: no cover - pre-migration or DB failure
                log.debug("ambient purge unavailable", exc_info=True)
            await asyncio.sleep(HOUSEKEEPING_INTERVAL)

    async def apply_settings(self) -> dict[str, str]:
        """Reconcile running monitors with the settings. Returns what changed."""
        master = self._master_switch()
        changes: dict[str, str] = {}

        for monitor_id, monitor in self._monitors.items():
            should_run = master and monitor.enabled()
            monitor.state.enabled = should_run
            running = self._is_running(monitor_id)

            if should_run and not running:
                started = await self._start_one(monitor)
                changes[monitor_id] = "started" if started else "unavailable"
            elif not should_run and running:
                await self._stop_one(monitor_id)
                changes[monitor_id] = "stopped"

        if changes:
            bus.publish("monitor.settings_applied", {"changes": changes, "master": master})
        return changes

    async def stop(self) -> None:
        housekeeping = self._tasks.pop(_HOUSEKEEPING, None)
        if housekeeping is not None and not housekeeping.done():
            housekeeping.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await housekeeping

        for monitor_id in list(self._tasks):
            await self._stop_one(monitor_id)
        self._started = False

    def _master_switch(self) -> bool:
        from .base import setting

        return bool(setting("monitor_enabled", False))

    def _is_running(self, monitor_id: str) -> bool:
        task = self._tasks.get(monitor_id)
        return task is not None and not task.done()

    async def _start_one(self, monitor: Monitor) -> bool:
        try:
            monitor.check_permission()
            monitor.check_available()
        except MonitorUnavailable as exc:
            monitor.state.unavailable_reason = str(exc)
            monitor.state.running = False
            log.info("monitor %s unavailable: %s", monitor.id, exc)
            bus.publish(
                "monitor.unavailable",
                {"monitor": monitor.id, "title": monitor.title, "reason": str(exc)},
            )
            return False

        monitor.state.unavailable_reason = None
        monitor.state.last_error = None
        monitor.state.consecutive_errors = 0
        self._debouncers[monitor.id].reset()

        try:
            await monitor.on_start()
        except MonitorUnavailable as exc:
            monitor.state.unavailable_reason = str(exc)
            bus.publish(
                "monitor.unavailable",
                {"monitor": monitor.id, "title": monitor.title, "reason": str(exc)},
            )
            return False

        monitor.state.running = True
        monitor.state.interval_seconds = monitor.interval()
        self._tasks[monitor.id] = asyncio.create_task(
            self._loop(monitor), name=f"monitor:{monitor.id}"
        )
        log.info("monitor %s started (every %.0fs)", monitor.id, monitor.state.interval_seconds)
        bus.publish(
            "monitor.started",
            {
                "monitor": monitor.id,
                "title": monitor.title,
                "interval_seconds": monitor.state.interval_seconds,
                "observes_user": monitor.id in _OBSERVES_USER,
            },
        )
        return True

    async def _stop_one(self, monitor_id: str, *, reason: str = "stopped") -> None:
        task = self._tasks.pop(monitor_id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        monitor = self._monitors.get(monitor_id)
        if monitor is not None:
            monitor.state.running = False
            with contextlib.suppress(Exception):
                await monitor.on_stop()
        log.info("monitor %s %s", monitor_id, reason)
        bus.publish("monitor.stopped", {"monitor": monitor_id, "reason": reason})

    # --------------------------------------------------------------------- loop

    async def _loop(self, monitor: Monitor) -> None:
        """One monitor's life. Never raises; stops itself if it cannot recover."""
        while True:
            interval = monitor.interval()
            monitor.state.interval_seconds = interval
            try:
                observations = await monitor.sample()
            except asyncio.CancelledError:
                raise
            except MonitorUnavailable as exc:
                monitor.state.unavailable_reason = str(exc)
                monitor.state.last_error = str(exc)
                bus.publish(
                    "monitor.unavailable",
                    {"monitor": monitor.id, "title": monitor.title, "reason": str(exc)},
                )
                # Not transient: something the environment cannot provide.
                asyncio.create_task(self._stop_one(monitor.id, reason="unavailable"))
                return
            except Exception as exc:
                monitor.state.consecutive_errors += 1
                monitor.state.last_error = str(exc)
                log.warning(
                    "monitor %s failed (%d in a row): %s",
                    monitor.id,
                    monitor.state.consecutive_errors,
                    exc,
                    exc_info=monitor.state.consecutive_errors == 1,
                )
                bus.publish(
                    "monitor.error",
                    {
                        "monitor": monitor.id,
                        "title": monitor.title,
                        "error": str(exc),
                        "consecutive": monitor.state.consecutive_errors,
                    },
                )
                if monitor.state.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    self._record_activity(
                        monitor,
                        Observation(
                            kind=f"{monitor.id}.stopped",
                            summary=(
                                f"{monitor.title} stopped after failing "
                                f"{monitor.state.consecutive_errors} times: {exc}"
                            ),
                            severity="alert",
                            data={"error": str(exc)},
                        ),
                        level="error",
                    )
                    asyncio.create_task(self._stop_one(monitor.id, reason="repeated failures"))
                    return
                # Back off so a persistent fault does not spin.
                await asyncio.sleep(min(interval * monitor.state.consecutive_errors, 300.0))
                continue

            monitor.state.consecutive_errors = 0
            monitor.state.last_error = None
            monitor.note_sample(len(observations))
            self._emit(monitor, observations)

            await asyncio.sleep(interval)

    def _emit(self, monitor: Monitor, observations: list[Observation]) -> None:
        debouncer = self._debouncers[monitor.id]
        for observation in observations:
            if not debouncer.should_report(observation):
                continue
            bus.publish(
                f"observation.{observation.kind}",
                {"monitor": monitor.id, **observation.to_dict()},
            )
            self._record_ambient(monitor, observation)
            if monitor.record_to_activity and observation.severity != "info":
                self._record_activity(
                    monitor,
                    observation,
                    level="warning" if observation.severity == "alert" else "info",
                )

    def _record_ambient(self, monitor: Monitor, observation: Observation) -> None:
        """Write the observation to the ambient store, if history is switched on.

        The live event bus above is unconditional -- an observation the UI is
        showing right now is not a record, it is the present. This is the record,
        and it is separately refusable: ``ambient_history_enabled`` off means the
        watchers still drive the interface but leave nothing behind afterwards.

        Deliberately not batched. A batch would hold observations in memory until
        the next tick, and a crash between ticks would lose exactly the events
        immediately before it -- the ones most likely to explain the crash.
        """
        try:
            from ..db.repos import ambient, app_settings

            if not app_settings.get("ambient_history_enabled", True):
                return
            ambient.record(
                monitor.id,
                observation.kind,
                observation.summary,
                data=observation.data,
                severity=observation.severity,
                dedupe_key=observation.dedupe_key,
                clears=observation.clears,
                observed_at=observation.at,
            )
        except Exception:  # pragma: no cover - pre-migration or DB failure
            log.debug("could not store observation from %s", monitor.id, exc_info=True)

    def _record_activity(
        self, monitor: Monitor, observation: Observation, *, level: str = "info"
    ) -> None:
        """Write an observation to the Activity log, tolerating no database.

        A monitor must not die because the store is unavailable -- the event bus
        has already told the UI, and losing a log line is a smaller failure than
        losing the watcher.
        """
        try:
            from ..db.repos import activity

            activity.record(
                monitor.activity_kind,
                observation.summary,
                detail={"monitor": monitor.id, **observation.to_dict()},
                level=level,  # type: ignore[arg-type]
            )
        except Exception:  # pragma: no cover - pre-migration or DB failure
            log.debug("could not record observation from %s", monitor.id, exc_info=True)

    # ------------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        return {
            "master_enabled": self._master_switch(),
            "started": self._started,
            "monitors": [m.to_dict() for m in self._monitors.values()],
            "watching": self.watching(),
        }

    def watching(self) -> list[dict[str, str]]:
        """Monitors currently observing the user, for the standing indicator (L5)."""
        return [
            {"monitor": m.id, "title": m.title, "description": m.description}
            for m in self._monitors.values()
            if m.id in _OBSERVES_USER and self._is_running(m.id)
        ]

    def is_watching_user(self) -> bool:
        return bool(self.watching())


_SUPERVISOR: Supervisor | None = None


def supervisor() -> Supervisor:
    """The process-wide supervisor, with every monitor registered."""
    global _SUPERVISOR
    if _SUPERVISOR is None:
        _SUPERVISOR = Supervisor()
        from .bluetooth_monitor import BluetoothMonitor
        from .clipboard_monitor import ClipboardMonitor
        from .screen_monitor import ScreenMonitor
        from .system_monitor import SystemMonitor
        from .usb_monitor import UsbMonitor
        from .window_monitor import WindowMonitor

        _SUPERVISOR.register_all(
            [
                SystemMonitor(),
                WindowMonitor(),
                ScreenMonitor(),
                ClipboardMonitor(),
                UsbMonitor(),
                BluetoothMonitor(),
            ]
        )
    return _SUPERVISOR
