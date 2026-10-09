"""Looking at the screen and saying what is on it (§11, §25, H9).

``screen.capture`` returns a path; ``screen.look`` returns *a sentence*. The gap
between those two is what the audits recorded as missing, so these tests are mostly
about the four ways this tool must refuse to invent that sentence:

* the screenshot backend writes something that is not an image,
* the frame is too large to send to a vision model,
* the vision chain is unreachable,
* the model itself returns nothing.

In every one of those the tool still returns the frame's path -- because the picture
is the evidence and the description is a convenience -- and states in words what it
could not do. A tool that answered an unreachable vision model with an empty
description would be reporting a blank screen, which is the fake §78 exists to
forbid.

The separation-from-action test is the other half. This tool must never be a way to
click something: seeing is observation, and acting on what you see is a different
call that carries its own permission prompt (H9).
"""

from __future__ import annotations

import asyncio
import functools
import struct
import zlib
from pathlib import Path
from typing import Any, Callable

import pytest

from nexus.tools import screen_read


def sync(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def png(width: int = 320, height: int = 200) -> bytes:
    """A real, minimal PNG so `_png_size` and the signature check both pass."""
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes([7]) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def screen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A screen whose capture, size and vision the test supplies."""
    state: dict[str, Any] = {
        "writes": png(),
        "backends": [("stub-shooter", ["-f", "{path}"], None, False)],
        "answer": ("PyCharm, with a Python file open.", ""),
        "big": False,
        "calls": [],
    }
    monkeypatch.setattr(screen_read, "_backends", lambda: state["backends"])

    def capture(target: str) -> tuple[Path | None, str]:
        state["calls"].append(("capture", target))
        path = tmp_path / f"frame-{len(state['calls'])}.png"
        path.write_bytes(state["writes"])
        return path, "stub-shooter"

    async def describe(payload: str, path: Path, question: str) -> tuple[str, str]:
        state["calls"].append(("describe", question))
        if state["big"]:
            raise AssertionError("a frame over the cap must never reach the vision model")
        return state["answer"]

    monkeypatch.setattr(screen_read, "_capture", capture)
    monkeypatch.setattr(screen_read, "_describe", describe)
    # The real preparation is what enforces the size cap, so it is not stubbed --
    # only the filesystem location it writes a reduced copy to is.
    monkeypatch.setattr(screen_read, "_capture_dir", lambda: tmp_path)
    return state


# ------------------------------------------------------------------- it works


@sync
async def test_a_normal_look_returns_a_sentence_and_the_frame(screen: Any) -> None:
    result = await screen_read._screen_look({"question": "what is this?"})
    assert result.ok
    assert result.data["description"] == "PyCharm, with a Python file open."
    assert result.data["description_available"] is True
    # The picture is the evidence, so its path is always returned.
    assert result.data["path"]
    assert Path(result.data["path"]).exists()
    assert result.data["width"] == 320 and result.data["height"] == 200
    assert result.summary == "PyCharm, with a Python file open."


@sync
async def test_the_question_reaches_the_model_and_is_echoed_back(screen: Any) -> None:
    result = await screen_read._screen_look({"question": "what does the dialog say?"})
    assert ("describe", "what does the dialog say?") in screen["calls"]
    assert result.data["question"] == "what does the dialog say?"


@sync
async def test_an_empty_question_is_allowed_and_recorded_as_none(screen: Any) -> None:
    result = await screen_read._screen_look({})
    assert result.ok
    assert result.data["question"] is None
    assert ("describe", "") in screen["calls"]


# ---------------------------------------------------------------- it refuses


@sync
async def test_no_screenshot_program_is_a_refusal_that_names_the_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(screen_read, "_backends", lambda: [])
    result = await screen_read._screen_look({})
    assert not result.ok
    assert "scrot" in (result.error or "") or "grim" in (result.error or "")


@sync
async def test_a_backend_that_wrote_a_non_image_is_refused_not_described(
    screen: Any,
) -> None:
    """A text file reported as a screenshot has not succeeded."""
    screen["writes"] = b"this is not an image at all"
    result = await screen_read._screen_look({})
    assert not result.ok
    assert "not a PNG" in (result.error or "")
    # And it did not waste a vision call on it.
    assert not any(call[0] == "describe" for call in screen["calls"])


@sync
async def test_a_frame_too_large_to_send_is_saved_but_not_read(screen: Any) -> None:
    """An oversized request that fails slowly is worse than saying it was skipped."""
    screen["writes"] = png(width=8_000, height=8_000)  # compresses small, so pad it
    # PNG of one repeated row compresses to nothing; pad the file past the cap with a
    # real trailing chunk so the size check, not the signature check, is what fires.
    screen["writes"] = screen["writes"] + b"\x00" * (screen_read.MAX_BYTES + 1024)
    result = await screen_read._screen_look({})
    assert result.ok, "the frame was taken, so this is not a failure"
    assert result.data["description_available"] is False
    assert "larger than" in result.data["reason"]
    assert Path(result.data["path"]).exists()
    assert not any(call[0] == "describe" for call in screen["calls"])


@sync
async def test_an_unreachable_vision_model_returns_the_frame_and_says_why(
    screen: Any,
) -> None:
    """No vision model is not a blank screen, and must not read like one."""
    screen["answer"] = ("", "No vision model is configured.")
    result = await screen_read._screen_look({})
    assert result.ok
    assert result.data["description_available"] is False
    assert result.data["reason"] == "No vision model is configured."
    assert "no vision model" in result.summary.lower()
    assert result.data["path"]


@sync
async def test_a_vision_model_that_returns_nothing_says_exactly_that(
    screen: Any,
) -> None:
    screen["answer"] = ("", "")
    result = await screen_read._screen_look({})
    assert result.ok
    assert result.data["description_available"] is False
    assert "returned nothing" in result.data["reason"]


@sync
async def test_a_capture_failure_is_a_refusal_with_the_backends_reasons(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        screen_read, "_backends", lambda: [("stub-shooter", ["-f", "{path}"], None, False)]
    )
    monkeypatch.setattr(
        screen_read, "_capture", lambda target: (None, "stub-shooter: no display")
    )
    result = await screen_read._screen_look({})
    assert not result.ok
    assert "no display" in (result.error or "")


# ------------------------------------------------------------- posture (H9)


def test_looking_is_not_acting() -> None:
    """Interpretation is never authorization: this tool must not click or type."""
    from nexus.tools import registry

    tool = registry.get("screen.look")
    assert tool is not None
    # It carries the *reading* capability, not any input capability.
    assert tool.capability == "screen.capture"
    schema = tool.parameters["properties"]
    for forbidden in ("click", "type", "key", "coordinate", "x", "y"):
        assert forbidden not in schema, f"screen.look grew an acting parameter: {forbidden}"
    assert "input.click" in tool.description, (
        "the description must point at the acting tool so the model does not "
        "assume looking is doing"
    )


def test_the_description_says_it_only_looks() -> None:
    from nexus.tools import registry

    tool = registry.get("screen.look")
    assert tool is not None
    lowered = tool.description.lower()
    assert "only looks" in lowered or "only looks:" in lowered


# ------------------------------------------- the vision path, not a stub of it


def test_describe_reports_an_unreachable_router_rather_than_a_blank_screen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The branch the fixture stubs away is the one that matters most.

    ``_describe`` is replaced wholesale in the tests above, which is right for
    testing the tool's handling of its result -- and it means a mutation *inside*
    ``_describe`` survives them. This exercises the real function against a router
    that cannot resolve a vision model, so the "." that turns "no model" into a
    silent empty description is caught here rather than shipped.
    """
    from nexus.providers.base import ProviderError
    import nexus.router as router_mod  # the *module*; `from ... import router`
    # would bind the accessor function of the same name and the patch would fail

    # ``resolve`` is called with keywords only -- ``resolve("vision", needs_vision=True)``
    # at the call site, so the stub accepts them the same way; a positional-only
    # signature raises TypeError instead of testing anything.
    def explode(*args: Any, **kwargs: Any) -> Any:
        raise ProviderError("No vision model is configured.", provider="nvidia")

    # ``namespace.router`` is the accessor *function* (``def router()``), so the
    # patch replaces it with one that raises rather than with an object that has a
    # ``resolve`` -- getting this wrong is an AttributeError, not a silent pass.
    monkeypatch.setattr(router_mod, "router", explode)
    frame = tmp_path / "frame.png"
    frame.write_bytes(png())

    description, why_not = asyncio.run(
        screen_read._describe("ZmFrZQ==", frame, "what is on screen?")
    )
    assert description == "", "an unreachable model must not produce a sentence"
    assert why_not == "No vision model is configured."
