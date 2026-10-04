"""Tests for the world model (C2): observation.* -> world_state().

The module under test is the real one -- real bus, real bounded deques -- and
observations are published by hand exactly as the supervisor publishes them
(``observation.<kind>`` with the observation's own dict in the payload), which
is how test_proactive_engine drives the engine it tests. No monitor samples
anything, so nothing in here touches the real machine.

The failures this file exists to catch are the dishonesty ones: a domain
filled with a default nobody observed, a stale fact silently dropped, a
subscriber crash taking the bus with it, a window title reaching the agent's
system prompt. None of those show up as an error -- they show up as a
plausible-looking answer, which is why each test asserts against the
*shape* the model would read.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from nexus.ai import context as ctx_mod
from nexus.core import world as world_mod
from nexus.core.events import bus
from nexus.monitors.base import Observation


def sync(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Run one coroutine test to completion, then assert nothing leaked."""
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = asyncio.run(_run_with_world(fn, *args, **kwargs))
        assert world_mod.running() is False  # nothing leaked into the next test
        return result

    return wrapper


async def _run_with_world(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """One fresh world model per test, stopped and cleared afterwards."""
    world_mod.reset()
    assert world_mod.start() is True
    # The subscriber registers only on its first loop turn; publishing before
    # that turn publishes into the void. Wait for the subscription to be live.
    while bus.subscriber_count == 0:
        await asyncio.sleep(0)
    try:
        return await fn(*args, **kwargs)
    finally:
        await world_mod.stop()
        world_mod.reset()


async def drain(n: int = 20) -> None:
    """Give the subscriber's loop turns to consume what was published.

    ``bus.publish`` offers onto subscriber queues synchronously, but the
    subscriber's ``async for`` needs loop turns to record each event.
    """
    for _ in range(n):
        await asyncio.sleep(0)


def publish(kind: str, *, data: dict[str, Any] | None = None, at: str | None = None,
            **kwargs: Any) -> None:
    """The supervisor's publish shape, verbatim (supervisor.py ``_emit``)."""
    observation = Observation(
        kind=kind,
        summary=kwargs.pop("summary", f"{kind} observed"),
        data=data or {},
        **kwargs,
    )
    if at is not None:
        observation.at = at
    bus.publish(f"observation.{kind}", {"monitor": "test", **observation.to_dict()})


def iso(moment: datetime) -> str:
    return moment.isoformat()


# ------------------------------------------------------------------- mechanics


@sync
async def test_start_is_idempotent() -> None:
    """A second start() must not leave a second subscriber racing the first."""
    assert world_mod.start() is False  # already running from the harness


@sync
async def test_stop_is_clean_when_never_started() -> None:
    """stop() on a dead model is a no-op, not an error."""
    await world_mod.stop()
    await world_mod.stop()  # second call: task already None
    assert world_mod.running() is False


# -------------------------------------------------------------- honest facts


@sync
async def test_state_reflects_published_observations() -> None:
    """A real observation appears in world_state() with its own timestamps."""
    moment = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)
    publish("system.battery", data={"percent": 42, "charging": False, "status": "Discharging"},
            at=iso(moment))
    await drain()
    state = world_mod.world_state()
    battery = state["domains"]["battery"]
    assert battery is not None
    assert battery["data"]["percent"] == 42
    assert battery["data"]["charging"] is False
    assert battery["observed_at"] == iso(moment)      # the observation's own stamp
    assert battery["stale"] is False
    assert state["observed"] == 1
    assert state["kinds"] == ["system.battery"]


@sync
async def test_unobserved_domains_are_none() -> None:
    """Publish nothing about a domain -> that domain is None, never a guess.

    The load-bearing assertion is not that the key exists (it always does) but
    that its value is None: a battery shape with a plausible percent in it
    would be exactly the invented-data failure §60 forbids.
    """
    publish("window.focus", data={"to": {"app": "firefox", "title": "docs"}})
    await drain()
    state = world_mod.world_state()
    assert state["domains"]["battery"] is None
    assert state["domains"]["cpu"] is None
    assert state["domains"]["memory"] is None
    assert state["domains"]["usb"] is None
    assert state["domains"]["bluetooth"] is None
    assert state["domains"]["clipboard"] is None
    assert state["domains"]["process"] is None          # C1 not landed: unknown
    assert state["domains"]["window"] is not None        # the one that was published


@sync
async def test_process_domain_is_none_until_c1_lands() -> None:
    """No process monitor exists yet, so process.* facts are absent, not invented."""
    state = world_mod.world_state()
    assert state["domains"]["process"] is None
    assert "process" not in state["kinds"]
    # And a defensive subscription: if C1 does publish, the fact flows in.
    publish("process.churn", data={"started": 3, "exited": 1})
    await drain()
    assert world_mod.world_state()["domains"]["process"]["data"]["started"] == 3


@sync
async def test_latest_fact_wins_per_domain() -> None:
    """A newer observation of the same kind replaces the old one's view."""
    publish("system.battery", data={"percent": 80}, at=iso(datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)))
    publish("system.battery", data={"percent": 30}, at=iso(datetime(2026, 9, 9, 12, 5, tzinfo=timezone.utc)))
    await drain()
    assert world_mod.world_state()["domains"]["battery"]["data"]["percent"] == 30


@sync
async def test_newest_wins_across_kinds_in_one_domain() -> None:
    """usb.input.added and usb.storage.removed both feed usb; the later one is current."""
    publish("usb.input.added", data={"product": "mouse"}, at=iso(datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)))
    publish("usb.storage.removed", data={"product": "stick"}, at=iso(datetime(2026, 9, 9, 12, 2, tzinfo=timezone.utc)))
    await drain()
    usb = world_mod.world_state()["domains"]["usb"]
    assert usb["kind"] == "usb.storage.removed"
    assert usb["data"]["product"] == "stick"


# ------------------------------------------------------------------ staleness


@sync
async def test_stale_fact_is_marked_not_dropped() -> None:
    """An old observation is returned with stale=True -- the model learns the
    fact is old rather than being given nothing and guessing."""
    old = datetime.now(timezone.utc) - timedelta(seconds=world_mod.STALE_AFTER_SECONDS + 600)
    publish("system.network", data={"has_default_route": True}, at=iso(old))
    await drain()
    state = world_mod.world_state()
    network = state["domains"]["network"]
    assert network is not None          # still there, not dropped
    assert network["stale"] is True     # and marked
    assert network["data"]["has_default_route"] is True


@sync
async def test_fresh_fact_is_not_marked_stale() -> None:
    """A fact inside the window reads fresh -- staleness must not stick."""
    publish("system.network", data={"has_default_route": True})
    await drain()
    assert world_mod.world_state()["domains"]["network"]["stale"] is False


@sync
async def test_stale_boundary_is_exclusive() -> None:
    """A fact exactly at the TTL is fresh; one second past it is stale.

    Written against the literal comparison (``>``), not by reading the module's
    own constant back -- a test that asserts ``x == STALE_AFTER_SECONDS`` cannot
    fail when someone changes the constant.
    """
    boundary = datetime.now(timezone.utc) - timedelta(seconds=world_mod.STALE_AFTER_SECONDS)
    just_inside = boundary + timedelta(seconds=1)
    just_outside = boundary - timedelta(seconds=1)
    publish("system.memory", data={"percent": 55.0}, at=iso(just_inside))
    publish("system.cpu", data={"percent": 12.0}, at=iso(just_outside))
    await drain()
    state = world_mod.world_state()
    assert state["domains"]["memory"]["stale"] is False
    assert state["domains"]["cpu"]["stale"] is True


@sync
async def test_unparseable_timestamp_falls_back_to_received_at() -> None:
    """A fact whose observed_at cannot be parsed is judged by when it arrived.

    Dropped would mean losing a real observation; guessed would mean inventing
    a time. The received time is the honest fallback.
    """
    publish("system.disk", data={"mountpoint": "/", "percent": 71.0}, at="not-a-timestamp")
    await drain()
    disk = world_mod.world_state()["domains"]["disk"]
    assert disk is not None
    # The fact survives, judged against received_at, which is now.
    assert disk["stale"] is False
    assert disk["observed_at"] == "not-a-timestamp"  # verbatim, not corrected


def test_the_exact_ttl_is_fresh_and_one_microsecond_past_it_is_stale() -> None:
    """The boundary itself, pinned on the helper rather than through the bus.

    ``_is_stale`` is called with a ``now`` that has already moved on by the
    time a published fact is read, so a fact *exactly* at the TTL is not
    constructible through ``publish`` -- which is why
    ``test_stale_boundary_is_exclusive`` cannot tell ``>`` from ``>=``. This
    calls the judgement directly, where the boundary is reachable.
    """
    now = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)
    # 300.0 written out once: if the constant is retuned, this line says so
    # instead of the test silently re-deriving whatever the new value is.
    assert world_mod.STALE_AFTER_SECONDS == 300.0
    exactly = now - timedelta(seconds=world_mod.STALE_AFTER_SECONDS)
    assert world_mod._is_stale(exactly, now) is False
    assert world_mod._is_stale(exactly - timedelta(microseconds=1), now) is True


@sync
async def test_a_naive_timestamp_is_not_guessed_into_a_timezone() -> None:
    """A timestamp with no zone is unusable, not silently read as UTC.

    Reading a naive wall-clock time as UTC is off by the machine's offset --
    up to half a day -- which is how a stale fact reads fresh (or the reverse).
    The observation still happened, so it is judged by when it arrived. The
    naive time here is years old: were it coerced to UTC it would be stale, so
    ``stale is False`` is the proof no zone was invented, on any machine's
    offset.
    """
    ancient_but_zoneless = "2020-01-01T00:00:00"
    publish("system.disk", data={"mountpoint": "/", "percent": 12.0},
            at=ancient_but_zoneless)
    await drain()
    disk = world_mod.world_state()["domains"]["disk"]
    assert disk is not None
    assert disk["stale"] is False
    assert disk["observed_at"] == ancient_but_zoneless  # kept as the monitor said it


# --------------------------------------------------------------- boundedness


@sync
async def test_only_latest_n_kept_per_kind() -> None:
    """Fifty observations of one kind -> the deque holds KEEP_PER_KIND, not fifty.

    The bound is asserted against a literal 8 (and against len()) rather than
    reading KEEP_PER_KIND back, so a change to the constant cannot quietly
    make this test un-failable.
    """
    for i in range(50):
        publish("system.cpu", data={"percent": float(i)}, at=iso(datetime(2026, 9, 9, 12, i, tzinfo=timezone.utc)))
    await drain()
    history = world_mod._state.facts["system.cpu"]
    assert len(history) == 8
    # The newest is index 0 and the eight survivors are the last eight.
    assert [f["data"]["percent"] for f in history] == [49.0, 48.0, 47.0, 46.0, 45.0, 44.0, 43.0, 42.0]
    # The counter still saw all fifty.
    assert world_mod.world_state()["observed"] == 50


@sync
async def test_kind_count_is_capped() -> None:
    """A producer of unlimited novel kinds cannot grow the dict forever.

    Beyond MAX_KINDS a new kind is counted as unknown rather than silently
    extending memory.
    """
    for i in range(world_mod.MAX_KINDS + 10):
        bus.publish(f"observation.synthetic.{i}",
                    {"kind": f"synthetic.{i}", "summary": "synthetic", "data": {}})
    await drain()
    assert len(world_mod._state.facts) == world_mod.MAX_KINDS
    assert world_mod.world_state()["unknown_kinds"] == 10
    # The capped kinds never become a named domain either.
    assert world_mod.world_state()["domains"]["battery"] is None


@sync
async def test_kindless_event_is_counted_not_stored() -> None:
    """An observation with no kind has no domain to file under; it is counted."""
    bus.publish("observation.", {"summary": "anonymous"})
    await drain()
    state = world_mod.world_state()
    assert state["domains"]["battery"] is None
    assert state["kinds"] == []
    assert state["unknown_kinds"] == 1


# --------------------------------------------------------------- containment


@sync
async def test_subscriber_crash_marks_degraded_and_bus_survives() -> None:
    """A handler that raises must not kill the bus or the task -- but must
    mark the whole state degraded, because a snapshot that stopped updating
    while looking current is the §78 fake."""
    original = world_mod._accept
    calls = {"n": 0}

    def exploding(kind: str, payload: dict[str, Any]) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:  # let the first through so the state has a fact
            raise RuntimeError("subscriber exploded")
        original(kind, payload)

    world_mod._accept = exploding  # type: ignore[assignment]
    try:
        publish("system.battery", data={"percent": 90})
        await drain()
        publish("system.battery", data={"percent": 10})
        await drain()
        # The bus delivered both; a collector downstream is still receiving.
        collected: list[str] = []

        async def collect() -> None:
            agen = bus.subscribe(topics=("observation",))
            try:
                async for event in agen:
                    collected.append(event.type)
            finally:
                await agen.aclose()

        task = asyncio.create_task(collect())
        while bus.subscriber_count < 2:
            await asyncio.sleep(0)
        publish("window.focus", data={"to": {"app": "vim"}})
        await drain()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert "observation.window.focus" in collected  # bus unaffected

        state = world_mod.world_state()
        assert state["degraded"] is True
        assert "subscriber exploded" in (state["error"] or "")
        # The subscriber is still running -- one bad event did not kill it.
        assert world_mod.running() is True
        # The first fact (recorded before the crash) survives; the crashed one
        # never landed.
        assert state["domains"]["battery"]["data"]["percent"] == 90
    finally:
        world_mod._accept = original  # type: ignore[assignment]


@sync
async def test_degraded_is_not_silently_cleared() -> None:
    """A state that went degraded stays degraded until reset -- it did not
    fix itself, and the model must keep being told the snapshot is unreliable."""
    original = world_mod._accept
    world_mod._accept = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[assignment,misc]
    try:
        publish("system.battery", data={"percent": 1})
        await drain()
        assert world_mod.world_state()["degraded"] is True
    finally:
        world_mod._accept = original  # type: ignore[assignment]
    # Successful observations afterwards do not un-degrade the state.
    publish("system.battery", data={"percent": 2})
    await drain()
    state = world_mod.world_state()
    assert state["degraded"] is True
    # And the status() surface agrees with world_state().
    assert world_mod.status()["degraded"] is True


# ---------------------------------------------------------------- redaction


@sync
async def test_window_title_is_redacted_for_the_agent() -> None:
    """The agent-facing view redacts window titles; the raw state keeps them.

    A window title is content -- a document name, a URL, a message subject.
    The model's system prompt gets the fact that focus changed, not the title.
    """
    publish("window.title", data={
        "app": "firefox", "title": "Bank statement - account 1234.pdf",
        "previous_title": "Inbox (23) - work mail",
    })
    await drain()
    raw = world_mod.world_state()
    redacted = world_mod.redacted_state()
    assert raw["domains"]["window"]["data"]["title"] == "Bank statement - account 1234.pdf"
    assert redacted["domains"]["window"]["data"]["title"] == "[redacted]"
    assert redacted["domains"]["window"]["data"]["previous_title"] == "[redacted]"
    assert redacted["domains"]["window"]["data"]["app"] == "firefox"  # machine fact, kept


@sync
async def test_device_names_are_redacted_for_the_agent() -> None:
    """USB and Bluetooth device names are user data in the agent's view."""
    publish("bluetooth.audio.connected", data={
        "name": "Santosh's AirPods", "address": "AA:BB:CC:DD:EE:FF", "role": "audio",
    })
    await drain()
    redacted = world_mod.redacted_state()
    bluetooth = redacted["domains"]["bluetooth"]["data"]
    assert bluetooth["name"] == "[redacted]"
    assert bluetooth["address"] == "AA:BB:CC:DD:EE:FF"  # a machine fact, kept


@sync
async def test_clipboard_content_never_reaches_the_agent() -> None:
    """Clipboard presence only -- content and preview are dropped, not redacted.

    The clipboard monitor made this decision for its own observations; the
    world model re-implementing a softer version here would open a second
    path around it.
    """
    publish("clipboard.copy", data={
        "kind": "text", "content": "my password is hunter2", "preview": "my password i…",
        "length": 19, "lines": 1,
    })
    await drain()
    data = world_mod.redacted_state()["domains"]["clipboard"]["data"]
    assert "content" not in data
    assert "preview" not in data
    assert data["length"] == 19   # shape survives: the user can ask about it


# --------------------------------------------------------------- context block


@sync
async def test_world_block_enters_the_system_context() -> None:
    """_world_block renders observed domains and omits unobserved ones."""
    publish("system.battery", summary="Battery is at 42% and discharging",
            data={"percent": 42, "charging": False})
    await drain()
    block = ctx_mod._world_block()
    assert block is not None
    assert block.kind == "world"
    assert "- Battery" in block.body
    assert "Battery is at 42% and discharging" in block.body
    # Unobserved domains are omitted, not filled in.
    assert "- CPU:" not in block.body
    assert "- Focused window" not in block.body
    # And the honesty trailer is present.
    assert "unknown, not known-to-be-fine" in block.body


@sync
async def test_world_block_reports_staleness() -> None:
    """A stale fact is rendered with its caveat -- the model reads it as old."""
    old = datetime.now(timezone.utc) - timedelta(seconds=world_mod.STALE_AFTER_SECONDS * 4)
    publish("system.battery", data={"percent": 42}, at=iso(old))
    await drain()
    block = ctx_mod._world_block()
    assert block is not None
    assert "- Battery (stale reading):" in block.body


@sync
async def test_world_block_when_nothing_observed() -> None:
    """An empty world model produces an honest 'nothing observed' block,
    not a block full of defaults."""
    block = ctx_mod._world_block()
    assert block is not None
    assert "No monitor has reported any state yet" in block.body
    assert "- Battery" not in block.body


@sync
async def test_world_block_when_degraded() -> None:
    """A degraded world model says so in the agent's view, before it is trusted."""
    world_mod._state.degraded = True
    world_mod._state.error = "RuntimeError: boom"
    try:
        block = ctx_mod._world_block()
        assert block is not None
        assert "subscriber failed" in block.body
        assert "boom" in block.body
    finally:
        world_mod._state.degraded = False
        world_mod._state.error = ""


@sync
async def test_world_block_never_leaks_a_title() -> None:
    """End to end: title observed -> block rendered -> no title in the block.

    The title travels two ways from the monitor -- in ``data.title`` and
    inside the summary string ("firefox: Secret project roadmap") -- and a
    redaction that only scrubbed the data field would leave it sitting in
    the one line the model definitely reads. Both paths are pinned here.
    """
    publish("window.title", summary="firefox: Secret project roadmap",
            data={"app": "firefox", "title": "Secret project roadmap"})
    await drain()
    block = ctx_mod._world_block()
    assert block is not None
    assert "Secret project roadmap" not in block.body
    assert "details withheld" in block.body  # the redirect, not the raw summary


@sync
async def test_clipboard_preview_never_leaks_through_the_summary() -> None:
    """The clipboard monitor's summary embeds the copied text's preview."""
    publish("clipboard.copy", summary="Copied text: my password is hunte…",
            data={"kind": "text", "content": "my password is hunter2",
                  "preview": "my password is hunte…", "length": 19})
    await drain()
    block = ctx_mod._world_block()
    assert block is not None
    assert "hunte" not in block.body
    assert "content withheld" in block.body


@sync
async def test_device_name_never_leaks_through_the_summary() -> None:
    """usb/bluetooth summaries embed device names; the agent's view must not."""
    publish("bluetooth.audio.connected", summary="Santosh's AirPods connected",
            data={"name": "Santosh's AirPods", "role": "audio"})
    publish("usb.storage.added", summary="SanDisk Ultra 128GB connected",
            data={"product": "SanDisk Ultra 128GB"})
    await drain()
    block = ctx_mod._world_block()
    assert block is not None
    assert "Santosh's AirPods" not in block.body
    assert "SanDisk" not in block.body


@sync
async def test_machine_facts_survive_redaction() -> None:
    """Redaction blinds user content, not machine facts: a system.cpu summary
    about percentages passes through verbatim, and so does a window.focus
    summary that names apps but no documents."""
    publish("system.cpu", summary="CPU is at 5%", data={"percent": 5.0})
    publish("window.focus", summary="Switched to firefox after 2m in vim",
            data={"to": {"app": "firefox", "title": "untitled"}})
    publish("bluetooth.radio", summary="Bluetooth is back on", data={"blocked": False})
    await drain()
    block = ctx_mod._world_block()
    assert block is not None
    assert "CPU is at 5%" in block.body
    assert "Bluetooth is back on" in block.body
    # window.focus names apps (machine facts) but its data carries a title.
    # The summary survives; the title field does not.
    assert "Switched to firefox" in block.body
    data = world_mod.redacted_state()["domains"]["window"]["data"]
    assert data["to"]["title"] == "[redacted]"


@sync
async def test_assemble_includes_world_block() -> None:
    """The block reaches the assembled context a model actually receives."""
    world_mod._accept("system.cpu", {"kind": "system.cpu", "summary": "CPU is at 5%",
                                      "data": {"percent": 5.0},
                                      "at": iso(datetime.now(timezone.utc))})
    ctx = await ctx_mod.assemble(
        "how is the machine doing?", max_context_tokens=32_000, include_memories=False,
    )
    worlds = [b for b in ctx.blocks if b.kind == "world"]
    assert len(worlds) == 1
    assert "- CPU" in worlds[0].body
    # And it renders into the system prompt block a model would read.
    assert "Current machine state (observed)" in ctx.system_block()


@sync
async def test_assemble_sheds_world_before_memories() -> None:
    """world (40) sheds before memories (70) in the _fit priority order.

    A tiny budget forces the shed. What matters is which block goes: the one
    that changes the answer (memories) stays longer than the one that only
    saves a tool call (world) -- and ambient (30) goes before world does.
    """
    # Fill five domains with verbose summaries so the world block alone is
    # larger than the smallest budget the assembler will ever use (the
    # MIN_CONTEXT_TOKENS floor): ~1600 chars of summary is ~450 tokens per
    # domain, well past what 4_000 tokens of window can hold alongside it.
    for i, kind in enumerate(
        ("system.cpu", "system.memory", "system.disk", "system.battery", "system.network")
    ):
        world_mod._accept(kind, {"kind": kind, "summary": f"fact {i} " + "x" * 1_600,
                                  "data": {"note": "y"},
                                  "at": iso(datetime.now(timezone.utc))})
    ctx = await ctx_mod.assemble(
        "a" * 200, max_context_tokens=4_000, include_memories=False,
    )
    kinds = [b.kind for b in ctx.blocks]
    assert "world" not in kinds           # informational: gone first
    assert "environment" in kinds         # non-negotiable: stays
    assert ctx.dropped != []              # and the shed is recorded, not silent
