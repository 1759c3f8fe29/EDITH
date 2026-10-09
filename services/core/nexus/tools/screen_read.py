"""Looking at the screen and saying what is on it (§11, §12, §25).

``screen.capture`` saves a file and returns a path. That is not the same capability
as *seeing*, and the distinction is the whole reason this module exists: a file path
is useful to a model that can read images, and useless to every part of NEXUS that
cannot. The founding doc asks for a machine that can be asked "what is on my screen
right now?" and answer in words, and the audits recorded that half as missing -- the
screen monitor notices that the pixels changed and never knows what they became.

So this is the reading half: capture, hand the frame to the vision model, and return
one sentence about what is there. Four properties are deliberate.

**It returns a description, never an action.** Seeing what is on screen and *acting*
on it are separated everywhere in this codebase (H9), and this tool stays firmly on
the observation side. It does not click, type, or move anything; a caller that wants
a click asks ``screen.locate`` for coordinates and then calls ``input.click``, which
carries its own permission prompt. Interpretation is never authorization.

**The screenshot is a real file the user can open.** The description is a
convenience; the frame is the evidence. Every answer names the path it was read
from, so a claim about what was on screen can be checked against the picture rather
than trusted.

**A frame too large to send is refused in words.** The screenshot from a 4K display
is several megabytes; the vision endpoint has a limit and an oversized request fails
slowly. Above the cap the tool says the frame was saved but not sent, and why,
rather than reporting a description it did not obtain.

**No vision model means no description, stated plainly.** If the vision chain is
unreachable, the tool reports that this machine cannot currently read images and
still returns the path -- the same tri-state honesty the rest of the tool layer uses.
A tool that returned an empty description as though it were a blank screen would be
the exact fake §78 forbids.
"""

from __future__ import annotations

import asyncio
import base64
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from .registry import ToolResult, tool
from .screen import _available_backends, _capture_dir, _image_kind, _png_size

log = get_logger("tools.screen_read")

#: Frames wider than this are downscaled before being described, so a 4K screenshot
#: does not become a multi-megabyte base64 payload.
MAX_WIDTH = 1280

#: Above this, the frame is not sent at all. An oversized request that fails slowly
#: is worse than saying it was skipped -- the user waits either way, and only one of
#: those tells them why.
MAX_BYTES = 4_000_000

#: How long to wait for the capture subprocess.
CAPTURE_TIMEOUT = 25.0


def _backends() -> list[tuple[str, list[str], list[str] | None, bool]]:
    return _available_backends()


def _capture(target: str) -> tuple[Path | None, str]:
    """Run the first working screenshot backend. Returns (path, detail)."""
    directory = _capture_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = directory / f"look-{stamp}.png"

    attempts: list[str] = []
    for binary, full_args, window_args, _wayland in _backends():
        args = window_args if (target != "screen" and window_args) else full_args
        if args is None:
            continue
        command = [binary] + [a.format(path=str(path)) for a in args]
        try:
            proc = subprocess.run(
                command, capture_output=True, text=True, timeout=CAPTURE_TIMEOUT
            )
        except Exception as exc:  # noqa: BLE001 - any spawn failure is the same here
            attempts.append(f"{binary}: {exc}")
            continue
        if proc.returncode == 0 and path.exists() and path.stat().st_size > 0:
            return path, binary
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        attempts.append(
            f"{binary}: {detail[-1] if detail else f'exited {proc.returncode}'}"
        )
        if path.exists() and path.stat().st_size == 0:
            path.unlink(missing_ok=True)

    return None, "; ".join(attempts) or "no screenshot program produced an image"


def _prepare(path: Path) -> tuple[str | None, str, Path]:
    """Base64 of a frame small enough to send. Returns (payload, why_not, frame).

    Downscaling needs ImageMagick, which this module does not depend on: without it
    the original is sent when it is small enough, and refused with a reason when it
    is not.
    """
    candidate = path
    magick = shutil.which("magick") or shutil.which("convert")
    if magick:
        reduced = path.with_name(path.stem + "-small.png")
        try:
            proc = subprocess.run(
                [magick, str(path), "-resize", f"{MAX_WIDTH}x>", str(reduced)],
                capture_output=True,
                timeout=20,
            )
            if proc.returncode == 0 and reduced.exists() and reduced.stat().st_size > 0:
                candidate = reduced
        except Exception:  # noqa: BLE001 - falling back to the original is correct
            log.debug("could not downscale frame", exc_info=True)

    try:
        size = candidate.stat().st_size
    except OSError as exc:
        return None, f"The frame could not be read: {exc}", path

    if size > MAX_BYTES:
        if candidate is not path:
            candidate.unlink(missing_ok=True)
        return (
            None,
            (
                f"The screenshot is {size // 1_000_000} MB, larger than the "
                f"{MAX_BYTES // 1_000_000} MB NEXUS will send to a vision model, so it "
                "was saved but not read."
            ),
            path,
        )

    try:
        payload = base64.b64encode(candidate.read_bytes()).decode("ascii")
    except OSError as exc:
        return None, f"The frame could not be read: {exc}", path
    finally:
        if candidate is not path:
            candidate.unlink(missing_ok=True)
    return payload, "", path


async def _describe(payload: str, path: Path, question: str) -> tuple[str, str]:
    """Ask the vision model about a frame. Returns (description, why_not)."""
    from ..providers.base import Attachment, ChatRequest, Message, ProviderError
    from ..router import router

    try:
        decision = router().resolve("vision", needs_vision=True)
    except ProviderError as exc:
        return "", exc.message

    request = ChatRequest(
        model=decision.model_id,
        system=(
            "You describe what is on a person's screen for their own assistant. Be "
            "concrete and factual: name the application, the document or page, and "
            "anything a person would notice. Two sentences at most. Do not speculate "
            "about intent, and do not invent detail you cannot see -- if the image is "
            "unclear, say so."
        ),
        messages=[
            Message(
                role="user",
                content=question
                or "What is on this screen right now? Name the application and what is in front of me.",
                attachments=[
                    Attachment(
                        kind="image",
                        media_type="image/png",
                        data=payload,
                        source_path=str(path),
                    )
                ],
            )
        ],
        max_output_tokens=200,
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
    except Exception as exc:  # noqa: BLE001 - unexpected transport failure
        return "", str(exc)

    return " ".join("".join(chunks).split()), ""


@tool(
    "screen.look",
    title="Look at the screen",
    description=(
        "Take a screenshot and read it with the vision model, returning a sentence "
        "about what is actually on screen. Use this to answer 'what am I looking at', "
        "'what does this error say', or 'is that dialog still open'. It only looks: to "
        "click or type, use screen.locate for coordinates and then input.click, which "
        "asks for permission separately. The screenshot is saved and its path is "
        "returned, so you can refer to the same frame again."
    ),
    capability="screen.capture",
    idempotent=False,
    timeout_s=90.0,
    parameters={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "What to ask about the screen, e.g. 'what does the error dialog "
                    "say?'. Leave empty for a general description."
                ),
            },
            "target": {
                "type": "string",
                "description": (
                    "'screen' for everything, or 'window' for just the active window."
                ),
                "default": "screen",
            },
        },
    },
)
def screen_look(params: dict[str, Any]) -> ToolResult:
    """Synchronous wrapper: the tool runtime calls handlers without awaiting."""
    return asyncio.run(_screen_look(params))


async def _screen_look(params: dict[str, Any]) -> ToolResult:
    target = str(params.get("target") or "screen").strip() or "screen"
    question = str(params.get("question") or "").strip()

    if not _backends():
        return ToolResult(
            ok=False,
            error=(
                "NEXUS cannot see the screen: no screenshot program is installed. "
                "Installing 'scrot' would enable it (on Wayland, 'grim')."
            ),
        )

    path, detail = await asyncio.to_thread(_capture, target)
    if path is None:
        return ToolResult(
            ok=False, error=f"The screen could not be captured. {detail}"
        )

    size = _png_size(path)
    kind = _image_kind(path)
    if kind is None:
        # A backend that wrote something that is not an image has not succeeded, and
        # saying so beats sending a text file to a vision model.
        path.unlink(missing_ok=True)
        return ToolResult(
            ok=False,
            error=(
                f"{detail} reported success but wrote a file that is not a PNG or "
                "JPEG, so it was discarded rather than described."
            ),
        )

    payload, why_not, frame = await asyncio.to_thread(_prepare, path)
    base_data: dict[str, Any] = {
        "path": str(frame),
        "backend": detail,
        "width": size[0] if size else None,
        "height": size[1] if size else None,
        "bytes": frame.stat().st_size if frame.exists() else None,
    }

    if payload is None:
        # Saved but not readable here. The frame is still returned, and the reason is
        # stated instead of an empty description standing in for a blank screen.
        return ToolResult(
            ok=True,
            data={**base_data, "description_available": False, "reason": why_not},
            summary=f"Took a screenshot, but could not read it: {why_not}",
        )

    description, vision_problem = await _describe(payload, frame, question)
    if not description:
        return ToolResult(
            ok=True,
            data={
                **base_data,
                "description_available": False,
                "reason": vision_problem or "The vision model returned nothing.",
            },
            summary=(
                "Took a screenshot but no vision model is reachable, so NEXUS "
                f"cannot say what is on it. The image is at {frame}."
            ),
        )

    return ToolResult(
        ok=True,
        data={
            **base_data,
            "description": description,
            "description_available": True,
            "question": question or None,
        },
        summary=description,
    )


__all__ = ["screen_look"]
