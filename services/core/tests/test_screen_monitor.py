"""Screen watch's change detection, driven over a loop of synthetic frames.

The monitor's whole value is the comparison: it takes a screenshot every few
minutes and must stay silent unless the screen *actually* changed. That path has
only ever been reasoned about -- L4 carried `◐` on exactly this -- because
exercising it looked like it needed a real screen changing on an interval.

It does not. `sample()` reads the world through three module-level functions
(`_capture`, `_thumbnail`, `_sha256`), so pointing those at frames the test
chooses drives the real `_compare`, the real prune, and the real observation
payload. What is stubbed is the camera; what is tested is the judgement.

Pinned here:

- the first frame is never a change (announcing "the screen changed" the moment
  watching starts would report something that did not happen);
- identical frames are silent, and a difference below
  `monitor_screen_change_percent` is silent too;
- a difference at or above it reports exactly one observation, carrying the
  measured percentage and `detection: "perceptual"`;
- without ImageMagick the comparison degrades to exact bytes and *says so* in
  the summary, rather than implying a smarter comparison than it did (§78);
- frames on disk stay bounded by `monitor_screen_keep_frames`, and the frame the
  next comparison needs is never the one pruned.
"""

from __future__ import annotations

import asyncio
import functools
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator

import pytest

from nexus.monitors import screen_monitor
from nexus.monitors.screen_monitor import ScreenMonitor, _difference, _parse_pgm


def sync(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def png(width: int = 320, height: int = 200, *, fill: int = 0) -> bytes:
    """A real, minimal PNG so `_png_size` reads genuine dimensions from IHDR."""
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    # One filter byte + one row of greyscale per scanline; `fill` distinguishes
    # frames by content so the exact-bytes path has something to differ on.
    raw = b"".join(b"\x00" + bytes([fill]) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def flat(value: int) -> list[int]:
    """A 16x16 thumbnail of one grey value -- the shape `_thumbnail` returns."""
    return [value] * (screen_monitor.THUMB * screen_monitor.THUMB)


@pytest.fixture
def watched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Any]]:
    """A monitor whose captures and thumbnails the test supplies."""
    options: dict[str, Any] = {
        "monitor_screen_change_percent": 8,
        "monitor_screen_keep_frames": 12,
        "monitor_screen_describe": False,
    }
    monkeypatch.setattr(
        screen_monitor, "setting", lambda key, default: options.get(key, default)
    )
    monkeypatch.setattr(
        screen_monitor, "settings", lambda: SimpleNamespace(screenshot_dir=tmp_path)
    )

    def build(
        *,
        thumbs: list[list[int] | None] | None = None,
        fills: list[int] | None = None,
        perceptual: bool = True,
        **overrides: Any,
    ) -> tuple[ScreenMonitor, dict[str, Any]]:
        options.update(overrides)
        monitor = ScreenMonitor()
        monitor._perceptual = perceptual
        monitor._frames = []
        monitor._previous_thumb = None
        monitor._previous_hash = None
        monitor._previous_path = None

        state = {"tick": 0}
        bodies = fills if fills is not None else []

        def capture(path: Path) -> tuple[bool, str]:
            index = state["tick"]
            fill = bodies[index] if index < len(bodies) else index
            path.write_bytes(png(fill=fill))
            return True, "stub-shooter"

        def thumbnail(path: Path) -> list[int] | None:
            index = state["tick"]
            state["tick"] += 1
            if thumbs is None:
                return None
            return thumbs[index] if index < len(thumbs) else thumbs[-1]

        def digest(path: Path) -> str:
            state["tick"] += 1
            import hashlib

            return hashlib.sha256(path.read_bytes()).hexdigest()

        monkeypatch.setattr(screen_monitor, "_capture", capture)
        monkeypatch.setattr(screen_monitor, "_thumbnail", thumbnail)
        monkeypatch.setattr(screen_monitor, "_sha256", digest)
        return monitor, state

    yield build


async def ticks(monitor: ScreenMonitor, count: int) -> list[Any]:
    out: list[Any] = []
    for _ in range(count):
        out.extend(await monitor.sample())
    return out


# ------------------------------------------------------------------ perceptual


@sync
async def test_the_first_frame_is_never_a_change(watched: Any) -> None:
    """Watching starting is not the screen changing."""
    monitor, _ = watched(thumbs=[flat(10)])
    assert await monitor.sample() == []


@sync
async def test_identical_frames_say_nothing(watched: Any) -> None:
    monitor, _ = watched(thumbs=[flat(10), flat(10), flat(10)])
    assert await ticks(monitor, 3) == []


@sync
async def test_a_change_past_the_threshold_reports_once_with_its_measurement(
    watched: Any,
) -> None:
    """Grey 10 -> grey 200 differs by ~74%, far past the 8% threshold."""
    monitor, _ = watched(thumbs=[flat(10), flat(200)])
    observations = await ticks(monitor, 2)

    assert len(observations) == 1
    seen = observations[0]
    assert seen.kind == "screen.change"
    assert seen.severity == "info"
    # Every change is its own event, so there is deliberately no dedupe key.
    assert seen.dedupe_key == ""
    assert seen.data["detection"] == "perceptual"
    assert seen.data["change_percent"] == pytest.approx(
        _difference(flat(10), flat(200)), abs=0.01
    )
    assert seen.data["width"] == 320 and seen.data["height"] == 200
    assert seen.data["previous_frame_path"] is not None
    assert "% of the image differs" in seen.summary


@sync
async def test_a_difference_below_the_threshold_is_not_a_change(watched: Any) -> None:
    """Grey 10 -> grey 12 is under 1% -- a caret blink, not a new screen."""
    monitor, _ = watched(thumbs=[flat(10), flat(12)])
    assert _difference(flat(10), flat(12)) < 8
    assert await ticks(monitor, 2) == []


@sync
async def test_the_threshold_setting_is_what_decides(watched: Any) -> None:
    """The same pair of frames flips verdict when the setting moves."""
    pair = [flat(10), flat(12)]
    amount = _difference(*pair)

    quiet, _ = watched(thumbs=pair, monitor_screen_change_percent=8)
    assert await ticks(quiet, 2) == []

    noisy, _ = watched(thumbs=pair, monitor_screen_change_percent=amount / 2)
    assert len(await ticks(noisy, 2)) == 1


# ----------------------------------------------------------------- exact bytes


@sync
async def test_without_imagemagick_the_summary_admits_byte_comparison(
    watched: Any,
) -> None:
    """The degraded path must not imply a perceptual comparison it did not do."""
    monitor, _ = watched(thumbs=None, fills=[1, 2], perceptual=False)
    observations = await ticks(monitor, 2)

    assert len(observations) == 1
    seen = observations[0]
    assert seen.data["detection"] == "exact-bytes"
    # No percentage is claimed, because none was measured.
    assert seen.data["change_percent"] is None
    assert "byte-for-byte" in seen.summary
    assert "ImageMagick" in seen.summary


@sync
async def test_exact_bytes_is_silent_when_the_bytes_repeat(watched: Any) -> None:
    monitor, _ = watched(thumbs=None, fills=[7, 7, 7], perceptual=False)
    assert await ticks(monitor, 3) == []


# ------------------------------------------------------------------- retention


@sync
async def test_frames_on_disk_stay_bounded_by_the_retention_setting(
    watched: Any, tmp_path: Path
) -> None:
    """Ten ticks keeping three leaves three frames, not ten screenshots."""
    monitor, _ = watched(
        thumbs=[flat(v) for v in (10, 200, 10, 200, 10, 200, 10, 200, 10, 200)],
        monitor_screen_keep_frames=3,
    )
    await ticks(monitor, 10)

    on_disk = sorted((tmp_path / "watch").glob("frame-*.png"))
    assert len(on_disk) == 3, f"retention of 3 left {len(on_disk)} frames"
    assert len(monitor._frames) == 3
    # The frame the next comparison is based on must still exist.
    assert monitor._previous_path is not None and monitor._previous_path.exists()


@sync
async def test_stopping_removes_the_frames_it_kept(watched: Any, tmp_path: Path) -> None:
    """Watching off must not leave a directory of the user's screen behind."""
    monitor, _ = watched(thumbs=[flat(10), flat(200), flat(10)])
    await ticks(monitor, 3)
    assert sorted((tmp_path / "watch").glob("frame-*.png"))

    await monitor.on_stop()
    assert sorted((tmp_path / "watch").glob("frame-*.png")) == []
    assert monitor._previous_thumb is None
    assert monitor._previous_path is None


# ------------------------------------------------- the comparison's own maths


def test_difference_is_zero_for_equal_and_full_for_opposite() -> None:
    assert _difference(flat(0), flat(0)) == 0.0
    assert _difference(flat(0), flat(255)) == pytest.approx(100.0)
    # Mismatched or empty vectors cannot be compared, so they count as fully
    # different rather than silently equal -- a wrong "no change" would make the
    # monitor go quiet forever.
    assert _difference([], flat(0)) == 100.0
    assert _difference([1, 2], [1, 2, 3]) == 100.0


def test_parse_pgm_reads_both_encodings_imagemagick_emits() -> None:
    binary = b"P5\n2 2\n255\n" + bytes([1, 2, 3, 4])
    assert _parse_pgm(binary) == [1, 2, 3, 4]

    ascii_form = b"P2\n2 2\n255\n1 2 3 4\n"
    assert _parse_pgm(ascii_form) == [1, 2, 3, 4]

    # A comment between header fields is legal PGM and must not derail parsing.
    commented = b"P5\n# made by magick\n2 2\n255\n" + bytes([9, 9, 9, 9])
    assert _parse_pgm(commented) == [9, 9, 9, 9]

    assert _parse_pgm(b"not a pgm at all") is None
    # Truncated data must be rejected, not padded: a short read that became a
    # thumbnail would compare against garbage.
    assert _parse_pgm(b"P5\n2 2\n255\n" + bytes([1, 2])) is None
