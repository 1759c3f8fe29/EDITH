"""Screen watch (task L4; spec §39, §60, §65, §78).

Takes a screenshot on an interval, compares it with the previous one, and reports
only when the screen actually changed. This is the capability that makes "look at
what I am doing" possible, and it is the most invasive thing in NEXUS, so three
things are true of it by construction:

**It is off by default and says when it is on.** ``monitor_screen_enabled``
defaults to False, and while this monitor runs the supervisor reports it through
``watching()``, which drives the standing indicator (L5). There is no way for the
screen to be watched without the indicator showing it.

**Change detection is honest about which kind it is.** With ImageMagick present,
frames are compared perceptually: downscaled to 16x16 greyscale and differenced,
so a blinking cursor or an anti-aliasing wobble is not a "change". Without it, the
only comparison available is an exact byte hash, where a ticking clock counts as
the screen changing -- so the observation says ``detection: "exact-bytes"`` and
the summary says so too, rather than letting the user believe in a smarter
comparison than the machine can do (§78).

**Frames are pruned.** A 5-minute watch over a working day is ~100 screenshots of
the user's screen. Only the most recent few are kept; the rest are deleted as they
age out, and the count is a setting.

Describing a frame with a vision model is a second, separate switch
(``monitor_screen_describe``). When it is off, this monitor records that the screen
changed and nothing about what was on it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import settings
from ..logging_setup import get_logger
from ..tools.screen import _available_backends, _png_size
from .base import Monitor, MonitorUnavailable, Observation, setting

log = get_logger("monitors.screen")

#: Size the comparison thumbnail is reduced to. 16x16 greyscale is 256 numbers:
#: enough to tell "a different window is open" from "the caret blinked", and
#: cheap enough to run every few seconds.
THUMB = 16

#: Frames wider than this are downscaled before being described, so a 4K
#: screenshot does not become a 9 MB base64 payload.
DESCRIBE_MAX_WIDTH = 1280

#: Above this, a frame is not sent to a vision model at all -- an oversized
#: request that fails slowly is worse than saying it was skipped.
DESCRIBE_MAX_BYTES = 4_000_000

_MAGICK: list[str] | None = None


def _magick() -> list[str] | None:
    """The ImageMagick invocation available here, if any.

    ImageMagick 7 renamed ``convert`` to ``magick``; both are still shipped by
    different distributions, so both are tried.
    """
    global _MAGICK
    if _MAGICK is not None:
        return _MAGICK or None
    for binary, prefix in (("magick", ["magick"]), ("convert", ["convert"])):
        if shutil.which(binary):
            _MAGICK = prefix
            return _MAGICK
    _MAGICK = []
    return None


def _thumbnail(path: Path) -> list[int] | None:
    """Downscale to a 16x16 greyscale vector via ImageMagick, or None."""
    magick = _magick()
    if magick is None:
        return None
    command = magick + [
        str(path),
        "-resize", f"{THUMB}x{THUMB}!",
        "-colorspace", "Gray",
        "-depth", "8",
        "pgm:-",
    ]
    try:
        proc = subprocess.run(command, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("thumbnail failed: %s", exc)
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return _parse_pgm(proc.stdout)


def _parse_pgm(raw: bytes) -> list[int] | None:
    """Parse binary (P5) or ASCII (P2) PGM into a flat list of grey values."""
    if not raw.startswith(b"P5") and not raw.startswith(b"P2"):
        return None
    binary = raw.startswith(b"P5")

    # Header fields are whitespace-separated and may be interrupted by comments.
    fields: list[bytes] = []
    index = 2
    while len(fields) < 3 and index < len(raw):
        while index < len(raw) and raw[index : index + 1].isspace():
            index += 1
        if raw[index : index + 1] == b"#":
            while index < len(raw) and raw[index : index + 1] != b"\n":
                index += 1
            continue
        start = index
        while index < len(raw) and not raw[index : index + 1].isspace():
            index += 1
        fields.append(raw[start:index])
    if len(fields) < 3:
        return None

    try:
        width, height = int(fields[0]), int(fields[1])
    except ValueError:
        return None
    expected = width * height
    if expected <= 0:
        return None

    if binary:
        index += 1  # the single whitespace byte after maxval
        values = list(raw[index : index + expected])
        return values if len(values) == expected else None

    try:
        values = [int(v) for v in raw[index:].split()]
    except ValueError:
        return None
    return values[:expected] if len(values) >= expected else None


def _difference(a: list[int], b: list[int]) -> float:
    """Mean absolute difference between two thumbnails, as a percentage."""
    if not a or not b or len(a) != len(b):
        return 100.0
    total = sum(abs(x - y) for x, y in zip(a, b))
    return total / (len(a) * 255.0) * 100.0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _capture(path: Path) -> tuple[bool, str]:
    """Capture the full screen to ``path``. Returns (ok, detail)."""
    attempts: list[str] = []
    for binary, full_args, _window_args, _wayland in _available_backends():
        command = [binary] + [a.format(path=str(path)) for a in full_args]
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            attempts.append(f"{binary}: {exc}")
            continue
        if proc.returncode == 0 and path.exists() and path.stat().st_size > 0:
            return True, binary
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        attempts.append(f"{binary}: {detail[-1] if detail else f'exited {proc.returncode}'}")
        if path.exists() and path.stat().st_size == 0:
            path.unlink(missing_ok=True)
    return False, "; ".join(attempts) or "no screenshot backend produced an image"


class ScreenMonitor(Monitor):
    id = "screen"
    title = "Screen watch"
    description = (
        "Takes a screenshot on an interval and notices when the screen changes. "
        "Whatever is on screen is captured, including anything private."
    )
    enabled_setting = "monitor_screen_enabled"
    interval_setting = "monitor_screen_interval_seconds"
    default_interval = 300.0
    capability = "screen.capture"
    activity_kind = "screen"

    def __init__(self) -> None:
        super().__init__()
        self._previous_thumb: list[int] | None = None
        self._previous_hash: str | None = None
        self._previous_path: Path | None = None
        self._frames: list[Path] = []
        self._perceptual = False
        self._describe_failed_reason: str | None = None

    # -------------------------------------------------------------- availability

    def check_available(self) -> None:
        if not _available_backends():
            raise MonitorUnavailable(
                "No screenshot program is installed, so NEXUS cannot see the screen. "
                "Installing 'scrot' would enable this (on Wayland, 'grim')."
            )

    async def on_start(self) -> None:
        self._perceptual = _magick() is not None
        self._previous_thumb = None
        self._previous_hash = None
        self._previous_path = None
        self._frames = sorted(self._directory().glob("frame-*.png"))
        if not self._perceptual:
            log.info(
                "screen watch is using exact-byte comparison: ImageMagick is not "
                "installed, so any pixel change counts as a change"
            )

    async def on_stop(self) -> None:
        # Watching stopped, so the frames it kept for comparison have no further
        # purpose. Leaving a directory of the user's screen behind after they
        # switched watching off would be the opposite of what they asked for.
        self._prune(keep=0)
        self._previous_thumb = None
        self._previous_hash = None
        self._previous_path = None

    # -------------------------------------------------------------------- sample

    async def sample(self) -> list[Observation]:
        directory = self._directory()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = directory / f"frame-{stamp}.png"

        ok, detail = await asyncio.to_thread(_capture, path)
        if not ok:
            # Not transient: a missing backend or a Wayland session will fail the
            # same way next tick.
            raise MonitorUnavailable(f"The screen could not be captured. {detail}")

        thumb = await asyncio.to_thread(_thumbnail, path) if self._perceptual else None
        digest = None if thumb is not None else await asyncio.to_thread(_sha256, path)

        changed, amount, method = self._compare(thumb, digest)

        self._previous_thumb = thumb
        self._previous_hash = digest
        previous_path = self._previous_path
        self._previous_path = path
        self._frames.append(path)
        self._prune(keep=int(setting("monitor_screen_keep_frames", 12)))

        if not changed:
            # Nothing happened. The frame stays only as the next comparison base.
            return []

        dimensions = _png_size(path)
        data: dict[str, Any] = {
            "frame_path": str(path),
            "previous_frame_path": str(previous_path) if previous_path else None,
            "backend": detail,
            "change_percent": round(amount, 2) if amount is not None else None,
            "detection": method,
            "width": dimensions[0] if dimensions else None,
            "height": dimensions[1] if dimensions else None,
            "bytes": path.stat().st_size,
        }

        summary = "The screen changed"
        if method == "perceptual" and amount is not None:
            summary = f"The screen changed ({amount:.0f}% of the image differs)"
        elif method == "exact-bytes":
            summary = (
                "The screen changed -- compared byte-for-byte, so even a clock "
                "tick counts (install ImageMagick for a perceptual comparison)"
            )

        if bool(setting("monitor_screen_describe", False)):
            description, why_not = await self._describe(path)
            if description:
                data["description"] = description
                summary = f"On screen: {description}"
            elif why_not:
                data["description_unavailable"] = why_not

        return [
            Observation(
                kind="screen.change",
                summary=summary,
                data=data,
                severity="info",
                # Every change is its own event: unlike a threshold, "the screen
                # changed" is not an ongoing condition to suppress.
                dedupe_key="",
            )
        ]

    # ------------------------------------------------------------------ compare

    def _compare(
        self, thumb: list[int] | None, digest: str | None
    ) -> tuple[bool, float | None, str]:
        threshold = float(setting("monitor_screen_change_percent", 8))

        if thumb is not None:
            if self._previous_thumb is None:
                # The first frame has nothing to compare against. Reporting it as
                # a change would announce "the screen changed" the moment
                # watching started, which is not something that happened.
                return False, None, "perceptual"
            amount = _difference(self._previous_thumb, thumb)
            return amount >= threshold, amount, "perceptual"

        if digest is not None:
            if self._previous_hash is None:
                return False, None, "exact-bytes"
            return digest != self._previous_hash, None, "exact-bytes"

        return False, None, "unavailable"

    # ----------------------------------------------------------------- describe

    async def _describe(self, path: Path) -> tuple[str, str]:
        """Ask the vision model what is on the frame. Returns (description, why_not)."""
        from ..providers.base import Attachment, ChatRequest, Message, ProviderError
        from ..router import router

        prepared, why_not = await asyncio.to_thread(self._prepare_for_vision, path)
        if prepared is None:
            return "", why_not

        decision = None
        try:
            decision = router().resolve("vision", needs_vision=True)
        except ProviderError as exc:
            # No vision model configured or reachable. The frame is still recorded
            # as "the screen changed"; what was on it stays unknown, and the
            # observation says why rather than leaving an empty description.
            return "", exc.message

        request = ChatRequest(
            model=decision.model_id,
            system=(
                "You describe screenshots for a personal assistant's activity log. "
                "One sentence, factual, no speculation about intent. Name the "
                "application and what is on screen. If you cannot tell, say so."
            ),
            messages=[
                Message(
                    role="user",
                    content="What is on this screen right now?",
                    attachments=[
                        Attachment(
                            kind="image",
                            media_type="image/png",
                            data=prepared,
                            source_path=str(path),
                        )
                    ],
                )
            ],
            max_output_tokens=120,
        )

        chunks: list[str] = []
        try:
            async for event in router().stream(request, job="vision", decision=decision):
                if event.type == "text":
                    chunks.append(event.text)
        except ProviderError as exc:
            return "", exc.message
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - unexpected transport failure
            return "", str(exc)

        return " ".join("".join(chunks).split()), ""

    def _prepare_for_vision(self, path: Path) -> tuple[str | None, str]:
        """Base64 of a frame small enough to send. Runs off the event loop."""
        candidate = path
        magick = _magick()
        if magick is not None:
            reduced = path.with_name(path.stem + "-small.png")
            command = magick + [
                str(path), "-resize", f"{DESCRIBE_MAX_WIDTH}x>", str(reduced)
            ]
            try:
                proc = subprocess.run(command, capture_output=True, timeout=20)
                if proc.returncode == 0 and reduced.exists() and reduced.stat().st_size > 0:
                    candidate = reduced
            except (OSError, subprocess.SubprocessError) as exc:
                log.debug("could not downscale frame: %s", exc)

        try:
            size = candidate.stat().st_size
        except OSError as exc:
            return None, f"The frame could not be read: {exc}"

        if size > DESCRIBE_MAX_BYTES:
            return None, (
                f"The screenshot is {size // 1_000_000} MB, larger than the "
                f"{DESCRIBE_MAX_BYTES // 1_000_000} MB NEXUS will send to a vision "
                "model, so this frame was recorded but not described."
            )

        try:
            payload = base64.b64encode(candidate.read_bytes()).decode("ascii")
        except OSError as exc:
            return None, f"The frame could not be read: {exc}"
        finally:
            if candidate is not path:
                candidate.unlink(missing_ok=True)
        return payload, ""

    # -------------------------------------------------------------------- frames

    def _directory(self) -> Path:
        directory = settings().screenshot_dir / "watch"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _prune(self, *, keep: int) -> None:
        """Delete frames beyond the retention count, oldest first."""
        keep = max(0, keep)
        while len(self._frames) > keep:
            stale = self._frames.pop(0)
            # Never delete the frame the next comparison needs.
            if stale == self._previous_path and keep > 0:
                self._frames.insert(0, stale)
                break
            stale.unlink(missing_ok=True)

    # ------------------------------------------------------------------- queries

    def frames(self) -> list[dict[str, Any]]:
        """The frames currently on disk, newest first, for the timeline (L16)."""
        out: list[dict[str, Any]] = []
        for path in sorted(self._frames, reverse=True):
            try:
                stat = path.stat()
            except OSError:
                continue
            out.append(
                {
                    "path": str(path),
                    "bytes": stat.st_size,
                    "taken_at": datetime.fromtimestamp(stat.st_mtime).isoformat(
                        timespec="seconds"
                    ),
                }
            )
        return out

    def detection_method(self) -> str:
        """Which comparison this machine can actually do, for the UI to state."""
        return "perceptual" if (self._perceptual or _magick() is not None) else "exact-bytes"

    def snapshot(self) -> dict[str, Any]:
        return {
            "detection": self.detection_method(),
            "describe_enabled": bool(setting("monitor_screen_describe", False)),
            "change_threshold_percent": float(setting("monitor_screen_change_percent", 8)),
            "frames_kept": int(setting("monitor_screen_keep_frames", 12)),
            "frames_on_disk": len(self._frames),
            "directory": str(settings().screenshot_dir / "watch"),
            "last_frame": str(self._previous_path) if self._previous_path else None,
        }
