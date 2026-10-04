"""Anticipation: the maths, and the four refusals that keep it honest.

A projection is a claim about something that has not happened, which makes this the
one module in NEXUS where being *confidently wrong* is the whole risk. The cheap
version of this feature -- fit a line, divide, announce a number -- is a liar, and
it is a liar in a way the user cannot check.

So most of these tests are about silence. They assert that a trend declines to
speak from two points, declines to speak when the points do not lie near the line,
declines to project further ahead than it has watched, and declines to call a flat
series a slow climb. The ones that assert it *does* speak are the easy half.

The monitor tests then check that the discipline survives being wired up: that a
climb still well below its threshold says so with a time attached, that the same
climb does not repeat every tick, that a battery on AC does not fit a line across
the moment it was plugged in, and that a restart drops the window instead of
asserting continuity across a gap in which anything could have happened.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable

import pytest

from nexus.core import trends
from nexus.core.trends import Series, fit
from nexus.monitors import system_monitor
from nexus.monitors.system_monitor import SystemMonitor

from test_system_monitor import readings, samples  # the existing payload builders


def sync(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def line(*values: float, step: float = 60.0) -> list[tuple[float, float]]:
    """Samples one minute apart, so a slope reads directly in units per minute."""
    return [(index * step, value) for index, value in enumerate(values)]


# ----------------------------------------------------------------- the maths


def test_a_steady_climb_measures_its_own_rate() -> None:
    """Ten per minute, fitted back out of the samples."""
    trend = fit(line(10, 20, 30, 40, 50))
    assert trend is not None
    assert trend.slope_per_minute == pytest.approx(10.0)
    assert trend.r2 == pytest.approx(1.0)
    assert trend.rising and not trend.falling and not trend.flat
    assert trend.trustworthy
    assert trend.samples == 5
    assert trend.window_seconds == pytest.approx(240.0)


def test_a_fall_is_negative_and_known_to_be_falling() -> None:
    trend = fit(line(90, 80, 70, 60))
    assert trend is not None
    assert trend.slope_per_minute == pytest.approx(-10.0)
    assert trend.falling and not trend.rising


def test_too_few_points_is_not_a_trend() -> None:
    """Two points always fit perfectly, which is exactly why they prove nothing."""
    trend = fit(line(10, 90))
    assert trend is not None
    assert trend.r2 == pytest.approx(1.0)      # the fit is flawless...
    assert trend.samples < trends.MIN_SAMPLES
    assert not trend.trustworthy               # ...and still says nothing
    assert trend.minutes_until(100) is None
    assert fit(line(10)) is None
    assert fit([]) is None


def test_noise_has_a_slope_and_must_not_be_believed() -> None:
    """CPU bouncing 10-90 fits a line whose slope means nothing."""
    trend = fit(line(10, 90, 15, 85, 20, 80, 12))
    assert trend is not None
    assert trend.r2 < trends.MIN_R2
    assert not trend.trustworthy
    assert trend.minutes_until(100) is None


def test_a_flat_series_is_not_a_slow_climb() -> None:
    """Floating-point noise on a steady value must not project thousands of minutes."""
    trend = fit(line(50.0, 50.0, 50.0, 50.0, 50.0))
    assert trend is not None
    assert trend.flat
    assert not trend.trustworthy
    assert trend.minutes_until(90) is None


def test_identical_timestamps_do_not_produce_a_rate() -> None:
    """A vertical line has no slope; inventing one would fabricate a rate."""
    assert fit([(100.0, 10.0), (100.0, 50.0), (100.0, 90.0)]) is None


# ------------------------------------------------------------- the projection


def test_a_projection_names_the_minutes_to_the_target() -> None:
    trend = fit(line(60, 65, 70, 75, 80))   # 5%/min
    assert trend is not None
    assert trend.minutes_until(90) == pytest.approx(2.0)


def test_it_refuses_to_project_further_than_it_watched() -> None:
    """Four minutes of data may not speak about the next hour."""
    trend = fit(line(10.0, 10.5, 11.0, 11.5, 12.0))   # 0.5%/min over 4 min
    assert trend is not None
    assert trend.trustworthy
    # 90 is 156 minutes away; the window is 4 minutes, so the horizon is 12.
    assert trend.minutes_until(90) is None
    # Something inside the horizon is still answered.
    assert trend.minutes_until(13) == pytest.approx(2.0)


def test_it_will_not_project_backwards_or_to_where_it_already_is() -> None:
    rising = fit(line(60, 70, 80, 90))
    assert rising is not None
    assert rising.minutes_until(50) is None      # moving away from the target
    assert rising.minutes_until(90) is None      # already there


# ---------------------------------------------------------------- the window


def test_a_series_forgets_samples_older_than_its_window() -> None:
    series = Series("cpu", window_seconds=300.0)
    for minute in range(12):
        series.add(float(minute), at=minute * 60.0)
    # Twelve minutes added, five minutes kept.
    assert len(series) <= 6
    trend = series.trend()
    assert trend is not None
    assert trend.window_seconds <= 300.0


def test_the_sentence_states_its_own_evidence() -> None:
    trend = fit(line(70, 75, 80, 85))
    assert trend is not None
    sentence = trends.describe_percentage("Memory", trend)
    assert "climbing" in sentence
    assert "70%" in sentence and "85%" in sentence
    assert "per minute" in sentence


# ------------------------------------------------------- wired into the monitor


def monitor_at(clock: list[float]) -> SystemMonitor:
    """A monitor whose trend clock the test drives.

    Both clocks have to be replaced, which is worth spelling out because getting it
    half right is silent: ``Series`` captures ``time.monotonic`` at construction, so
    patching ``system_monitor.time`` moves the *cooldown* clock and leaves the
    *sample* clock real -- every sample then lands within microseconds of the last,
    the fitted span is ~0, and `fit` correctly refuses to produce a rate. The
    symptom is a trend that never fires, which reads exactly like a broken feature.
    """
    monitor = SystemMonitor()
    for name in list(monitor._series):
        monitor._series[name] = Series(name, clock=lambda: clock[0])
    system_monitor.time = type(
        "FakeTime", (), {"monotonic": staticmethod(lambda: clock[0])}
    )()  # type: ignore[assignment]
    return monitor


@pytest.fixture(autouse=True)
def restore_time():
    import time as real_time

    yield
    system_monitor.time = real_time


@sync
async def test_a_climb_below_threshold_is_anticipated_with_a_time() -> None:
    """The point of the whole feature: warned before the alert, not after."""
    clock = [0.0]
    monitor = monitor_at(clock)
    await monitor.on_start()

    # Memory climbing 2%/min from 72 to 80, threshold 90: below the line, above
    # the "worth anticipating" floor, and a trustworthy fit.
    found: list[Any] = []
    for index, value in enumerate((72.0, 74.0, 76.0, 78.0, 80.0)):
        clock[0] = index * 60.0
        found.extend(monitor._trends(readings(cpu=None, memory=value)))

    trend = [o for o in found if o.kind == "system.trend"]
    assert trend, "a steady climb below the threshold produced no anticipation"
    seen = trend[0]
    assert seen.data["series"] == "memory"
    assert seen.severity == "notice"
    assert seen.data["minutes_until_threshold"] is not None
    assert "minutes" in seen.summary


@sync
async def test_the_same_trend_is_not_repeated_every_tick() -> None:
    """A climb lasts many ticks; the sentence must not arrive on each of them."""
    clock = [0.0]
    monitor = monitor_at(clock)
    await monitor.on_start()

    found: list[Any] = []
    for index in range(10):
        clock[0] = index * 60.0
        value = 72.0 + index
        found.extend(monitor._trends(readings(cpu=None, memory=value)))

    trend = [o for o in found if o.kind == "system.trend"]
    assert len(trend) == 1, f"ten ticks of one climb gave {len(trend)} trends"


@sync
async def test_a_value_already_past_its_threshold_is_the_alerts_business() -> None:
    """No prediction once the thing predicted has happened."""
    clock = [0.0]
    monitor = monitor_at(clock)
    await monitor.on_start()

    found: list[Any] = []
    for index, value in enumerate((91.0, 92.0, 93.0, 94.0, 95.0)):
        clock[0] = index * 60.0
        found.extend(monitor._trends(readings(cpu=None, memory=value)))

    assert [o for o in found if o.kind == "system.trend"] == []


@sync
async def test_a_low_climb_is_noise_with_a_slope() -> None:
    """Rising steadily from 20% is not worth pre-empting."""
    clock = [0.0]
    monitor = monitor_at(clock)
    await monitor.on_start()

    found: list[Any] = []
    for index, value in enumerate((20.0, 22.0, 24.0, 26.0, 28.0)):
        clock[0] = index * 60.0
        found.extend(monitor._trends(readings(cpu=None, memory=value)))

    assert [o for o in found if o.kind == "system.trend"] == []


@sync
async def test_a_draining_battery_reports_how_long_it_has() -> None:
    """The sentence the specs single out (§52), with the rate it was measured at."""
    clock = [0.0]
    monitor = monitor_at(clock)
    await monitor.on_start()

    found: list[Any] = []
    for index, level in enumerate((60.0, 55.0, 50.0, 45.0, 40.0)):
        clock[0] = index * 60.0
        battery = {"percent": level, "status": "Discharging", "time_remaining": None}
        found.extend(monitor._trends(readings(cpu=None, battery=battery)))

    trend = [o for o in found if o.data.get("series") == "battery"]
    assert trend, "a steady discharge produced no projection"
    seen = trend[0]
    assert seen.data["minutes_until_floor"] is not None
    assert "minutes" in seen.summary
    assert "a minute" in seen.summary


@sync
async def test_plugging_in_discards_the_discharge_window() -> None:
    """A charge/discharge cycle must not be fitted across the plug event."""
    clock = [0.0]
    monitor = monitor_at(clock)
    await monitor.on_start()

    for index, level in enumerate((60.0, 55.0, 50.0)):
        clock[0] = index * 60.0
        monitor._trends(readings(cpu=None,
                                battery={"percent": level, "status": "Discharging"}))

    clock[0] = 240.0
    monitor._trends(readings(cpu=None, battery={"percent": 51.0, "status": "Charging"}))
    assert len(monitor._series["battery"]) == 0


@sync
async def test_a_restart_drops_the_window_rather_than_spanning_the_gap() -> None:
    clock = [0.0]
    monitor = monitor_at(clock)
    await monitor.on_start()
    for index, value in enumerate((70.0, 75.0, 80.0)):
        monitor._series["memory"].add(value, at=index * 60.0)
    assert len(monitor._series["memory"]) == 3

    await monitor.on_start()
    assert len(monitor._series["memory"]) == 0


def test_a_trend_is_scored_quieter_than_the_alert_it_predicts() -> None:
    """Inverting these would train the user to distrust the louder of the two."""
    from nexus.core import scoring

    trend = scoring.RULES["system.trend"]
    memory = scoring.RULES["system.memory"]
    assert trend.urgency < memory.urgency
    # Early is the whole point: there is something to do about it beforehand.
    assert trend.actionability > memory.actionability
    # The detector is a line fit, not an observation, and says so.
    assert trend.confidence < 1.0
    assert trend.memorable is False
