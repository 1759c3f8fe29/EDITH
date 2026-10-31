"""Drive NEXUS for real: live providers, live speech, live tools, live machine.

Nothing here is stubbed. Every check makes an actual network call to a real
provider, runs an actual tool against this machine, or reads an actual row the
system just wrote. The point is to find the failures that unit tests cannot see --
the ones that only exist because the wire, the binary or the display is real.

Each section prints what it did and what came back, and the summary separates
"worked" from "refused with a reason" (often correct) from "broke" (never correct).
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import traceback
from typing import Any

sys.path.insert(0, "/home/santosh/EDITH/services/core")

RESULTS: list[tuple[str, str, str]] = []


def record(section: str, verdict: str, detail: str) -> None:
    RESULTS.append((section, verdict, detail))
    icon = {"OK": "OK  ", "REFUSED": "refu", "BROKE": "BROKE", "INFO": "    ",
            "EMPTY": "empt"}[verdict]
    print(f"  [{icon}] {section:<44} {detail[:110]}")


# ---------------------------------------------------------------- providers


async def test_providers() -> None:
    print("\n=== 1. PROVIDERS (live calls) ===")
    from nexus import providers
    from nexus.providers.base import ChatRequest, Message

    async def ask(pid: str, model: str, label: str) -> None:
        provider = providers.get(pid)
        if provider is None:
            record(label, "BROKE", f"no provider registered as {pid}")
            return
        request = ChatRequest(
            model=model,
            messages=[Message(role="user", content="Reply with exactly: alive")],
            max_output_tokens=400,
        )
        chunks: list[str] = []
        started = time.monotonic()
        try:
            async for event in provider.stream(request):
                if event.type == "text":
                    chunks.append(event.text)
            text = "".join(chunks).strip()
            ms = int((time.monotonic() - started) * 1000)
            if text:
                record(label, "OK", f"{ms}ms  {text[:40]!r}")
            else:
                # An empty reply is only a bug if the model is not a reasoner.
                # These models emit `reasoning_content` first, so a small budget
                # can be spent entirely on thinking -- which is how GLM 5.3 was
                # wrongly written off as dead. Reported as its own verdict rather
                # than as BROKE, because the two need different responses.
                record(label, "EMPTY", f"{ms}ms but no text (reasoning-only budget?)")
        except Exception as exc:  # noqa: BLE001 - that is the point
            kind = getattr(exc, "kind", "")
            # A rate limit or a missing key is the provider saying no, not the
            # code breaking. Counting it as BROKE sends someone hunting a bug in
            # a system that is behaving correctly -- the misdiagnosis this whole
            # file exists to avoid, and one it made on its first run.
            if kind in ("rate_limited", "missing_credential", "authentication_failed"):
                record(label, "REFUSED", f"{kind}: {str(exc)[:78]}")
            else:
                record(label, "BROKE", f"{type(exc).__name__}: {str(exc)[:80]}")

    await ask("nvidia", "nvidia/nemotron-3-super-120b-a12b", "nvidia chat (nemotron super)")
    await ask("nvidia", "z-ai/glm-5.3-flash", "nvidia GLM 5.3 Flash")
    await ask("groq", "openai/gpt-oss-120b", "groq gpt-oss-120b")
    await ask("openrouter", "nvidia/nemotron-3-super-120b-a12b:free", "openrouter free nemotron")
    await ask("mistral", "mistral-small-latest", "mistral small")


# ------------------------------------------------------------------ speech


async def test_speech() -> None:
    print("\n=== 2. SPEECH (synthesise then transcribe the real audio) ===")
    from nexus.router import router
    from nexus.providers import speech

    tts = router().resolve("tts")
    stt = router().resolve("stt")
    record("routing: tts head", "INFO", tts.key)
    record("routing: stt head", "INFO", stt.key)

    sentence = "Nexus is online and listening to you right now."
    try:
        started = time.monotonic()
        audio = await speech.synthesize(
            sentence, provider_id=tts.provider_id, model=tts.model_id, voice=None
        )
        body = audio.to_dict()
        record(
            "text-to-speech",
            "OK",
            f"{int((time.monotonic()-started)*1000)}ms  {body.get('bytes')} bytes",
        )
    except Exception as exc:  # noqa: BLE001
        record("text-to-speech", "BROKE", f"{type(exc).__name__}: {str(exc)[:80]}")
        return

    try:
        data = open(body["path"], "rb").read()
        started = time.monotonic()
        heard = await speech.transcribe(
            data, provider_id=stt.provider_id, model=stt.model_id, language="en"
        )
        ms = int((time.monotonic() - started) * 1000)
        match = "VERBATIM" if heard.text.strip().lower() == sentence.lower() else "DIFFERENT"
        record("speech-to-text", "OK", f"{ms}ms  {match}  {heard.text[:48]!r}")
    except Exception as exc:  # noqa: BLE001
        record("speech-to-text", "BROKE", f"{type(exc).__name__}: {str(exc)[:80]}")

    # The door must refuse a hearing-only provider for speech it cannot do.
    try:
        await speech.synthesize("x", provider_id="groq", model="whisper-large-v3-turbo")
        record("door refusal (groq cannot speak)", "BROKE", "it did not refuse")
    except speech.CannotSpeak:
        record("door refusal (groq cannot speak)", "OK", "refused with a sentence")


# ------------------------------------------------------------------ vision


async def test_vision() -> None:
    print("\n=== 3. VISION (look at this machine's actual screen) ===")
    from nexus.tools import registry

    tool = registry.get("screen.look")
    started = time.monotonic()
    try:
        result = await asyncio.to_thread(
            tool.handler, {"question": "Which application is in front? One short sentence."}
        )
        ms = int((time.monotonic() - started) * 1000)
        if not result.ok:
            record("screen.look", "REFUSED", (result.error or "")[:90])
        elif result.data.get("description_available"):
            record("screen.look", "OK", f"{ms}ms  {result.data['description'][:70]!r}")
        else:
            record("screen.look", "REFUSED", str(result.data.get("reason"))[:80])
    except Exception as exc:  # noqa: BLE001
        record("screen.look", "BROKE", f"{type(exc).__name__}: {str(exc)[:80]}")


# ------------------------------------------------------------------- tools


def test_tools() -> None:
    print("\n=== 4. TOOLS (run against the real machine) ===")
    from nexus.tools import registry

    calls: list[tuple[str, dict[str, Any]]] = [
        ("system.status", {}),
        ("system.processes", {"limit": 3}),
        ("system.volume", {}),
        ("system.temperature", {}),
        ("window.workspaces", {}),
        ("apps.windows", {}),
        ("clipboard.set", {"text": "nexus live test"}),
        ("clipboard.get", {}),
        ("automation.list", {}),
        ("files.list", {"path": "~"}),
        ("system.brightness", {}),
    ]
    for name, params in calls:
        tool = registry.get(name)
        if tool is None:
            record(name, "BROKE", "not registered")
            continue
        try:
            result = tool.handler(params)
        except Exception as exc:  # noqa: BLE001
            record(name, "BROKE", f"{type(exc).__name__}: {str(exc)[:80]}")
            continue
        if not result.ok:
            record(name, "REFUSED", (result.error or "")[:90])
            continue
        blob = json.dumps(result.data, default=str)
        verified = {True: "verified", False: "FAILED-VERIFY", None: "unverified"}[
            result.verified
        ]
        record(name, "OK", f"{verified}  {blob[:80]}")


# ----------------------------------------------------------------- learning


def test_learning() -> None:
    print("\n=== 5. LEARNING (did real outcomes get recorded?) ===")
    try:
        from nexus.db.repos import model_outcomes
        from nexus.learning import outcomes as learning

        record("learning enabled", "INFO", str(learning.enabled()))
        rows = model_outcomes.recent(limit=8)
        if not rows:
            record("outcome rows", "REFUSED", "none recorded yet")
        else:
            jobs = {}
            for row in rows:
                jobs.setdefault(row["job"], []).append(row)
            for job, group in jobs.items():
                best = group[0]
                record(
                    f"  outcomes: {job}",
                    "OK",
                    f"{len(group)} recent, latest {best['key'][:34]} "
                    f"reward={round(best['reward'], 2)} ok={bool(best['ok'])}",
                )

        stats = model_outcomes.stats("vision")
        if stats:
            for key, data in list(stats.items())[:3]:
                record(
                    "  vision model record",
                    "OK",
                    f"{key[:34]} calls={data['calls']} "
                    f"mean_reward={round(data['mean_reward'] or 0, 2)}",
                )
    except Exception as exc:  # noqa: BLE001
        record("learning layer", "BROKE", f"{type(exc).__name__}: {str(exc)[:90]}")


# -------------------------------------------------------------- world state


def test_world() -> None:
    print("\n=== 6. WORLD STATE (the fused model the agent reasons over) ===")
    try:
        from nexus.core import world

        state = world.world_state()
        domains = state.get("domains", {})
        known = [name for name, value in domains.items() if value]
        if known:
            record("domains populated", "OK", ", ".join(known))
        else:
            # Not a failure. Every watcher defaults OFF, so a fresh process has
            # observed nothing -- and an empty world state that says so is the
            # correct, truthful answer rather than a broken one. Recording it as
            # REFUSED with the reason keeps that distinction visible, because a
            # bare "none yet" reads like something failed to start.
            record(
                "domains populated",
                "REFUSED",
                "none -- every monitor is off by default (monitor_enabled=False), "
                "so nothing has been observed yet",
            )
        for name in known[:4]:
            entry = domains[name]
            record(
                f"  {name}",
                "OK",
                f"stale={entry.get('stale')} observed={str(entry.get('observed_at'))[:19]}",
            )
    except Exception as exc:  # noqa: BLE001
        record("world state", "BROKE", f"{type(exc).__name__}: {str(exc)[:90]}")


async def main() -> None:
    print("=" * 96)
    print("NEXUS LIVE DRIVE -- real providers, real audio, real screen, real machine")
    print("=" * 96)
    for step in (test_providers, test_speech, test_vision):
        try:
            await step()
        except Exception:  # noqa: BLE001
            print(traceback.format_exc())
    for step in (test_tools, test_learning, test_world):
        try:
            step()
        except Exception:  # noqa: BLE001
            print(traceback.format_exc())

    print("\n" + "=" * 96)
    counts: dict[str, int] = {}
    for _section, verdict, _detail in RESULTS:
        counts[verdict] = counts.get(verdict, 0) + 1
    print("SUMMARY:", counts)
    broke = [r for r in RESULTS if r[1] == "BROKE"]
    if broke:
        print("\nBROKEN (needs fixing):")
        for section, _v, detail in broke:
            print(f"  - {section}: {detail}")
    print("=" * 96)


if __name__ == "__main__":
    asyncio.run(main())
