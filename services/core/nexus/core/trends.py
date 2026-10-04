"""Anticipation: seeing where a number is going, not just where it is (§37, §52).

Three audits named the same absence in three ways. The founding doc's §37 asks for
a machine that warns *before* the thing happens; `doc-ultraintelligent.md` calls
anticipation the headline superpower and records it as MISSING; and §52's worked
example -- "your battery will be empty in about twenty minutes" -- was not
constructible, because every monitor in NEXUS reported the present tense. A
threshold alert says *memory is at 91%*. It cannot say *memory has been climbing
steadily for ten minutes*, which is the sentence a person would actually want.

This module is the difference between those two sentences, and it is deliberately
ordinary statistics rather than anything that needs a model: an ordinary
least-squares fit over a bounded window of samples, plus the discipline about when
a fit is allowed to speak.

That discipline is the whole file, because a projection is a claim about the future
and the cheap version of this feature is a liar. Four rules:

**It refuses to speak from too few points.** Two samples always fit a line
perfectly, and a line through two points of noise projects confidently to nonsense.
``MIN_SAMPLES`` is the floor.

**It refuses to speak when the fit is bad.** A slope is only meaningful if the
points actually lie near the line. R² is computed and a trend below ``MIN_R2`` is
reported as *no trend* rather than as a weak one -- CPU bouncing between 10% and
90% has a slope, and that slope means nothing.

**It refuses to project past what it has watched.** Extrapolating an hour from a
five-minute window is arithmetic, not evidence. A projection is capped at
``MAX_PROJECTION_RATIO`` times the observed window, and says so.

**It says what it measured.** Every result carries the window length, the sample
count and the R², so a trend the user doubts can be argued with instead of merely
believed.

What this module does *not* do: decide whether a trend is worth mentioning. That is
the proactive engine's job (``core/scoring.py``), and keeping the split means a
trend can be computed, recorded and never spoken -- which is the normal case.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Iterable

#: Fewer points than this and there is no trend, only a line through noise. Three
#: is the smallest number at which R² can disagree with the slope.
MIN_SAMPLES = 4

#: How well the points must fit the line before a slope is allowed to be reported.
#: 0.5 is permissive on purpose: real machine telemetry is noisy, and the cost of
#: staying silent about a genuine climb is higher than the cost of one hedged
#: sentence -- but a value this low is *why* every trend carries its own R² for the
#: reader to weigh.
MIN_R2 = 0.5

#: A projection may not reach further ahead than this multiple of the window it was
#: measured over. Five minutes of data may speak about the next fifteen; it may not
#: speak about the next hour.
MAX_PROJECTION_RATIO = 3.0

#: Samples older than this leave the window. A trend is about the recent past; an
#: hour-old reading says nothing about where memory is going now.
DEFAULT_WINDOW_SECONDS = 900.0

#: Slopes smaller than this (units per minute) are treated as flat. Without it a
#: perfectly steady value produces a tiny nonzero slope from floating-point noise
#: and a projection thousands of minutes out.
FLAT_SLOPE = 0.05


@dataclass(frozen=True)
class Trend:
    """What a series is doing, with the evidence attached."""

    #: Units per minute. Positive is rising.
    slope_per_minute: float
    #: 0..1 goodness of fit. How much the slope should be believed.
    r2: float
    samples: int
    window_seconds: float
    first_value: float
    last_value: float

    @property
    def rising(self) -> bool:
        return self.slope_per_minute > FLAT_SLOPE

    @property
    def falling(self) -> bool:
        return self.slope_per_minute < -FLAT_SLOPE

    @property
    def flat(self) -> bool:
        return not self.rising and not self.falling

    @property
    def trustworthy(self) -> bool:
        """Whether this fit has earned the right to be reported at all."""
        return self.samples >= MIN_SAMPLES and self.r2 >= MIN_R2 and not self.flat

    def minutes_until(self, target: float) -> float | None:
        """Minutes until the value reaches ``target``, or None if it will not.

        None covers every honest refusal, and they are different facts the caller
        does not need to distinguish: the fit is untrustworthy, the series is flat,
        it is moving away from the target, it is already past it, or the answer
        lies further ahead than the window permits anyone to claim.
        """
        if not self.trustworthy:
            return None
        distance = target - self.last_value
        # Already there, or moving the other way.
        if distance == 0 or (distance > 0) != (self.slope_per_minute > 0):
            return None
        minutes = distance / self.slope_per_minute
        if minutes <= 0:
            return None
        horizon = (self.window_seconds / 60.0) * MAX_PROJECTION_RATIO
        return minutes if minutes <= horizon else None

    def to_dict(self) -> dict[str, float | int]:
        return {
            "slope_per_minute": round(self.slope_per_minute, 4),
            "r2": round(self.r2, 3),
            "samples": self.samples,
            "window_seconds": round(self.window_seconds, 1),
            "first_value": self.first_value,
            "last_value": self.last_value,
        }


def fit(samples: Iterable[tuple[float, float]]) -> Trend | None:
    """Least-squares fit over (timestamp_seconds, value). None if not fittable.

    Returns a ``Trend`` whenever the arithmetic is defined -- including untrustworthy
    ones, so a caller that wants to show "watching, no trend yet" can. Whether to
    *act* on it is ``Trend.trustworthy``.
    """
    points = sorted(samples)
    count = len(points)
    if count < 2:
        return None

    span = points[-1][0] - points[0][0]
    if span <= 0:
        # Every sample at the same instant: a vertical line has no slope, and
        # inventing one from identical timestamps would be fabricating a rate.
        return None

    # Fit in minutes so the slope is directly the unit a sentence wants.
    base = points[0][0]
    xs = [(t - base) / 60.0 for t, _ in points]
    ys = [v for _, v in points]

    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:  # pragma: no cover - guarded by the span check above
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = sxy / sxx

    intercept = mean_y - slope * mean_x
    residual = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    total = sum((y - mean_y) ** 2 for y in ys)
    # A series that never moved has no variance to explain. Calling that a perfect
    # fit (1.0) would be arithmetically defensible and practically a lie, since the
    # slope is zero and there is nothing to believe -- but it is flat, so
    # `trustworthy` rejects it either way, and 1.0 is the honest reading of
    # "the line passes through every point".
    r2 = 1.0 if total == 0 else max(0.0, 1.0 - residual / total)

    return Trend(
        slope_per_minute=slope,
        r2=r2,
        samples=count,
        window_seconds=span,
        first_value=ys[0],
        last_value=ys[-1],
    )


class Series:
    """A bounded, time-windowed set of samples for one measurement.

    Deliberately in memory and deliberately lost on restart. The alternative is
    reading the ambient table on every tick to rebuild a window that the monitor
    already has in hand -- and a trend that survives a restart would be asserting
    continuity across a gap in which anything could have happened.
    """

    def __init__(
        self,
        name: str,
        *,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        clock: object | None = None,
    ) -> None:
        self.name = name
        self.window_seconds = window_seconds
        self._samples: Deque[tuple[float, float]] = deque()
        self._clock = clock or time.monotonic

    def add(self, value: float, *, at: float | None = None) -> None:
        moment = at if at is not None else float(self._clock())  # type: ignore[operator]
        self._samples.append((moment, float(value)))
        self._evict(moment)

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def trend(self) -> Trend | None:
        return fit(self._samples)

    def __len__(self) -> int:
        return len(self._samples)

    def clear(self) -> None:
        self._samples.clear()


def describe_percentage(label: str, trend: Trend, *, unit: str = "%") -> str:
    """A sentence about a 0-100 measurement that states its own evidence."""
    direction = "climbing" if trend.rising else "falling"
    minutes = trend.window_seconds / 60.0
    return (
        f"{label} has been {direction} for {minutes:.0f} minutes -- "
        f"{trend.first_value:.0f}{unit} to {trend.last_value:.0f}{unit}, "
        f"about {abs(trend.slope_per_minute):.1f}{unit} per minute"
    )
