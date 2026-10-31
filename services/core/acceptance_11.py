"""The ten acceptance tests from the build brief's §11, run for real.

This is not a unit suite. It drives the *real* tool runtime -- permission engine,
verification, audit trail and all -- against this actual machine, and it reports
what genuinely happened. Its value is that it can fail: every check asserts on an
observed effect (a file that exists, an exit code that came back, a process that
really died), not on a return value the code chose to give.

Two rules it follows, both learned the hard way in this project:

* **A refusal is not a failure.** A missing binary, a denied capability or a rate
  limit is the system behaving correctly, and it is reported as REFUSED with the
  reason intact. Collapsing it into "failed" sends someone hunting a defect in
  working code.
* **An empty result is not evidence of absence.** A probe whose budget is too small
  to see an answer reads exactly like a dead model. Where a check could be
  ambiguous it says so rather than concluding.

Nothing here is destructive: every file it makes is under /tmp/nexus-acceptance/,
and every process it starts is one it created itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, "/home/santosh/EDITH/services/core")

WORKDIR = Path("/tmp/nexus-acceptance")
RESULTS: list[tuple[int, str, str, str]] = []


def record(number: int, title: str, verdict: str, detail: str) -> None:
    RESULTS.append((number, title, verdict, detail))
    icon = {"PASS": "PASS", "FAIL": "FAIL", "REFUSED": "refu", "INFO": "    "}.get(
        verdict, verdict
    )
    print(f"  [{icon}] §11.{number} {title:<47} {detail[:100]}")


async def runtime_execute(name: str, params: dict[str, Any], task_id: str):
    """Run a tool through the real runtime: permissions, audit, verification."""
    from nexus.tools import runtime

    return await runtime.execute(name, params, task_id=task_id)


async def main() -> int:
    from nexus.db.repos import tasks as tasks_repo

    WORKDIR.mkdir(parents=True, exist_ok=True)
    task = tasks_repo.create("NEXUS acceptance tests (§11)")
    task_id = task["id"]

    # Start from a clean directory. `files.write` deliberately refuses to clobber
    # an existing file without an explicit mode, which is a real safety property --
    # but it means a second run of this harness would fail on its own leftovers and
    # report a defect that does not exist.
    import shutil

    shutil.rmtree(WORKDIR, ignore_errors=True)
    WORKDIR.mkdir(parents=True, exist_ok=True)

    print("=" * 100)
    print(f"NEXUS ACCEPTANCE TESTS — brief §11   task={task_id[:8]}  workdir={WORKDIR}")
    print("=" * 100)

    # ---------------------------------------------------------------- 1. shell
    print("\n=== 1. executes a harmless shell command, shows output and exit code ===")
    result = await runtime_execute("terminal.run", {"command": "echo nexus-acceptance-ok; exit 0"}, task_id)
    if not result.ok:
        record(1, "shell command + exit code", "FAIL", result.error or "no result")
    else:
        blob = json.dumps(result.data, default=str)
        stdout = str(result.data.get("stdout", "")) if isinstance(result.data, dict) else blob
        code = result.data.get("exit_code") if isinstance(result.data, dict) else None
        saw = "nexus-acceptance-ok" in stdout
        record(
            1,
            "shell command + exit code",
            "PASS" if saw and code == 0 else "FAIL",
            f"exit={code} output_present={saw} verified={result.verified}",
        )

    # ------------------------------------------------------------- 2. file
    print("\n=== 2. creates a file and verifies it exists ===")
    target = WORKDIR / "acceptance-file.txt"
    result = await runtime_execute(
        "files.write", {"path": str(target), "content": "written by the acceptance harness\n"}, task_id
    )
    exists = target.is_file()
    content_ok = exists and "acceptance harness" in target.read_text()
    record(
        2,
        "create file + verify on disk",
        "PASS" if (result.ok and exists and content_ok) else "FAIL",
        f"ok={result.ok} exists={exists} content_ok={content_ok} verified={result.verified}",
    )

    # ------------------------------------------------------------ 3. python
    print("\n=== 3. runs a small Python script and reports the result ===")
    script = WORKDIR / "acceptance_script.py"
    script.write_text("print(sum(range(1, 101)))\n")
    result = await runtime_execute(
        "terminal.run", {"command": f"{sys.executable} {script}"}, task_id
    )
    stdout = str(result.data.get("stdout", "")) if isinstance(result.data, dict) else ""
    record(
        3,
        "run python script",
        "PASS" if result.ok and "5050" in stdout else "FAIL",
        f"exit={result.data.get('exit_code') if isinstance(result.data, dict) else '?'} "
        f"sum_5050={'5050' in stdout}",
    )

    # ------------------------------------------------- 4. invalid command fails
    print("\n=== 4. detects an intentionally invalid command and reports failure ===")
    result = await runtime_execute(
        "terminal.run", {"command": "this-command-does-not-exist-nexus-test"}, task_id
    )
    code = result.data.get("exit_code") if isinstance(result.data, dict) else None
    # The tool must report this as a failure rather than swallowing it.
    record(
        4,
        "invalid command reported as failure",
        "PASS" if (not result.ok or (code not in (None, 0))) else "FAIL",
        f"ok={result.ok} exit={code} stderr_present={bool(str(result.data.get('stderr','')) if isinstance(result.data, dict) else '')}",
    )

    # ------------------------------------------------------- 5. recover
    print("\n=== 5. inspects the failure and recovers with an alternative ===")
    bad = await runtime_execute("terminal.run", {"command": "ls /nonexistent-nexus-path"}, task_id)
    bad_ok = bad.ok and (bad.data or {}).get("exit_code") == 0
    # A reasonable alternative: use the tool that does not depend on the path existing.
    good = await runtime_execute("files.list", {"path": str(WORKDIR)}, task_id)
    recovered = (not bad_ok) and good.ok
    record(
        5,
        "inspect failure, then recover",
        "PASS" if recovered else "FAIL",
        f"first_failed={not bad_ok} alternative_succeeded={good.ok}",
    )

    # ------------------------------------------- 6. long job, status, cancel
    print("\n=== 6. starts a long-running process, shows status, cancels it ===")
    # Driven on a thread, because `terminal.run` blocks until the command ends and
    # the real scenario is a *separate* actor inspecting and stopping it. A
    # same-thread version can only observe the job after it has already finished,
    # which proved nothing: an earlier version of this test used the wrong
    # parameter name (`timeout_s` for `timeout_seconds`), the runtime rejected the
    # call, and the empty running-list looked like a working system.
    import threading

    from nexus.tools import terminal

    long_outcome: dict[str, Any] = {}

    def run_long() -> None:
        try:
            long_outcome["result"] = asyncio.run(
                runtime_execute(
                    "terminal.run",
                    {"command": "sleep 120", "timeout_seconds": 300},
                    task_id,
                )
            )
        except Exception as exc:  # noqa: BLE001
            long_outcome["error"] = f"{type(exc).__name__}: {exc}"

    worker = threading.Thread(target=run_long, daemon=True)
    worker.start()
    await asyncio.sleep(4)

    seen = terminal.running()
    cancelled = False
    if seen:
        cancel_result = terminal.cancel(seen[0]["run_id"])
        cancelled = bool(cancel_result.get("stopped"))
    await asyncio.sleep(2)
    after = terminal.running()
    worker.join(timeout=8)

    result = long_outcome.get("result")
    exit_code = None
    if result is not None and isinstance(result.data, dict):
        exit_code = result.data.get("exit_code")
    # A cancelled command must report the signal, not a success and not a silence.
    record(
        6,
        "long process tracked, then cancelled",
        "PASS" if (seen and cancelled and not after and exit_code == -15) else "FAIL",
        f"tracked={len(seen)} cancel_ok={cancelled} still_running={len(after)} "
        f"exit_code={exit_code} (SIGTERM is -15)",
    )

    # ------------------------------------------------- 7. multi-step workflow
    print("\n=== 7. completes a multi-step workflow using more than one tool ===")
    steps: list[str] = []
    ok_all = True
    # step 1: write a data file
    data_file = WORKDIR / "numbers.txt"
    r1 = await runtime_execute(
        "files.write", {"path": str(data_file), "content": "3\n1\n2\n"}, task_id
    )
    steps.append(f"write={r1.ok}")
    ok_all &= r1.ok
    # step 2: sort it with a shell tool, writing to a second file
    sorted_file = WORKDIR / "numbers-sorted.txt"
    r2 = await runtime_execute(
        "terminal.run", {"command": f"sort {data_file} -o {sorted_file}"}, task_id
    )
    steps.append(f"sort={r2.ok}")
    ok_all &= r2.ok
    # step 3: read the result back and confirm the ordering really changed
    r3 = await runtime_execute("files.read", {"path": str(sorted_file)}, task_id)
    steps.append(f"read={r3.ok}")
    ok_all &= r3.ok
    body = ""
    if r3.ok and isinstance(r3.data, dict):
        body = str(r3.data.get("content") or r3.data.get("text") or "")
    ordered = body.split()[:3] == ["1", "2", "3"]
    record(
        7,
        "multi-step workflow (write→sort→read)",
        "PASS" if ok_all and ordered else "FAIL",
        f"{' '.join(steps)} result_ordered={ordered}",
    )

    # ------------------------------------------------------ 8. UI state truth
    print("\n=== 8. UI can reflect running/success/failed/cancelled/approval states ===")
    # Read the states off the bus the way the UI's live surface does. The first
    # version of this test asked `hasattr(bus, "subscribe")` and recorded INFO --
    # which checks that the code exists, not that the UI can be told anything.
    # §11.8 names five states, so five states are produced by real actions below
    # and then looked for in a real subscription.
    from nexus.core.events import bus

    # The real app binds the loop at startup (nexus/api/__init__.py). Without it
    # `publish_threadsafe` -- which is how `terminal.cancel` announces itself --
    # buffers the event for replay but hands it to nobody, so the cancellation
    # notice is invisible even though the cancel worked. Binding here is the
    # harness standing in for the startup the UI would have had.
    bus.bind_loop()

    streamed: list[Any] = []

    async def drain() -> None:
        async for event in bus.subscribe(None):
            streamed.append(event)

    pump = asyncio.create_task(drain())
    await asyncio.sleep(0.15)  # let the subscription attach before driving anything

    def count(event_type: str, **match: Any) -> int:
        total = 0
        for event in streamed:
            if event.type != event_type:
                continue
            if all(event.payload.get(key) == value for key, value in match.items()):
                total += 1
        return total

    def saw(event_type: str, **match: Any) -> bool:
        return count(event_type, **match) > 0

    five: dict[str, bool] = {}

    # running -- a job is in flight, the UI has been told, and it has *not* been
    # told the job finished. That gap is the state the brief is naming, and
    # starting the job on a thread is what makes it observable at all.
    import threading

    long_done: dict[str, Any] = {}

    def run_long_8() -> None:
        try:
            long_done["result"] = asyncio.run(
                runtime_execute(
                    "terminal.run", {"command": "sleep 60", "timeout_seconds": 300}, task_id
                )
            )
        except Exception as exc:  # noqa: BLE001
            long_done["error"] = f"{type(exc).__name__}: {exc}"

    worker8 = threading.Thread(target=run_long_8, daemon=True)
    worker8.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not saw("tool.started", tool="terminal.run"):
        await asyncio.sleep(0.1)
    five["running"] = saw("tool.started", tool="terminal.run") and not saw(
        "tool.finished", tool="terminal.run"
    )

    # cancelled -- stop it, then look for both the terminal-level notice the UI
    # clears its progress row on and the tool-level finish underneath it.
    from nexus.tools import terminal as terminal_tool

    live = terminal_tool.running()
    stopped = bool(live) and bool(terminal_tool.cancel(live[0]["run_id"]).get("stopped"))
    worker8.join(timeout=15)
    await asyncio.sleep(0.4)
    five["cancelled"] = (
        stopped and saw("terminal.cancelled") and saw("tool.finished", tool="terminal.run", ok=False)
    )

    # success
    good = await runtime_execute("terminal.run", {"command": "true"}, task_id)
    await asyncio.sleep(0.3)
    five["success"] = good.ok and saw("tool.finished", tool="terminal.run", ok=True)

    # failed -- counted rather than merely looked for, because the cancelled run
    # above already produced one `ok=False` and would have made this pass for
    # free. A new failure has to appear, not just any failure.
    failures_before = count("tool.finished", ok=False)
    bad = await runtime_execute(
        "terminal.run", {"command": "this-command-does-not-exist-nexus-test"}, task_id
    )
    await asyncio.sleep(0.3)
    five["failed"] = (not bad.ok) and count("tool.finished", ok=False) > failures_before

    # approval -- the one state this machine's settings cannot reach on their own:
    # `permission_mode` is "trusted" and `confirm_destructive` is off, so nothing
    # is ever asked. Driving it means turning on exactly that one switch for the
    # duration. The restore is asserted, not assumed: a test that quietly leaves a
    # safety setting flipped would be worse than not testing this at all.
    from nexus.db.repos import app_settings
    from nexus.permissions.engine import engine as permission_engine

    destructive_before = app_settings.get("confirm_destructive")
    approval: dict[str, Any] = {}
    restored = False
    try:
        app_settings.set("confirm_destructive", True)
        scratch = str(WORKDIR / "scratch")
        pending = asyncio.create_task(
            permission_engine.require(
                "terminal.execute", {"command": f"rm -rf {scratch}"}, timeout=25
            )
        )
        request_id = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and request_id is None:
            asked = [e for e in streamed if e.type == "permission.requested"]
            if asked:
                request_id = asked[-1].payload.get("id")
                break
            await asyncio.sleep(0.1)
        approval["asked"] = bool(request_id)
        if request_id:
            # Plain "deny" writes no persistent grant -- `deny_always` would, and
            # an acceptance test has no business leaving one behind.
            approval["answered"] = bool(permission_engine.respond(request_id, "deny").get("ok"))
        decision = await asyncio.wait_for(pending, timeout=25)
        approval["outcome"] = decision.outcome
        await asyncio.sleep(0.3)
        approval["resolved"] = saw("permission.resolved", choice="deny")
    finally:
        app_settings.set("confirm_destructive", destructive_before)
        restored = app_settings.get("confirm_destructive") == destructive_before

    pump.cancel()
    try:
        await pump
    except asyncio.CancelledError:
        pass

    five["approval"] = (
        approval.get("asked") is True
        and approval.get("answered") is True
        and approval.get("outcome") == "deny"
        and approval.get("resolved") is True
    )
    record(
        8,
        "five UI states observed on the live bus",
        "PASS" if all(five.values()) and restored else "FAIL",
        " ".join(f"{name}={'ok' if ok else 'MISSING'}" for name, ok in five.items())
        + f"  events={len(streamed)} confirm_destructive_restored={restored}",
    )
    if not all(five.values()):
        record(8, "  which states were missing", "INFO", json.dumps(approval, default=str))

    # -------------------------------------------------------- 9. safety gate
    print("\n=== 9. a destructive action is blocked or requires approval ===")
    from nexus.permissions.engine import engine as permission_engine
    from nexus.permissions import capabilities, policy
    from nexus.db.repos import app_settings

    # The first version of this test asserted that destructive capabilities
    # evaluate to "ask". They evaluate to "allow" -- because this machine is set
    # to `permission_mode = "trusted"`, which the user chose. Trusted mode changes
    # exactly one thing: whether a question is asked. It does not widen what is
    # permitted. So the property worth testing is not "does it ask" but "is there
    # a floor underneath that no mode can remove".
    mode = app_settings.get("permission_mode", "ask")
    hard = {
        "rm -rf /": "rm -rf /",
        "rm -rf ~": "rm -rf /home/santosh",
        "read ~/.ssh/id_rsa": "cat /home/santosh/.ssh/id_rsa",
        "read .env": "cat /home/santosh/EDITH/.env",
        "write /etc/hosts": "tee /etc/hosts",
    }
    refused = {label: policy.terminal_refusal(cmd) is not None for label, cmd in hard.items()}
    path_refused = policy.path_refusal("/etc/shadow", write=False) is not None
    # And the second-confirmation flag still marks the irreversible ones.
    second = capabilities.requires_second_confirmation("terminal.execute")

    all_refused = all(refused.values()) and path_refused
    record(
        9,
        "hard limits refuse regardless of mode",
        "PASS" if all_refused else "FAIL",
        f"mode={mode!r} second_confirmation={second} "
        + " ".join(f"{k.split()[0]}={'refused' if v else 'ALLOWED'}" for k, v in refused.items()),
    )
    if not all_refused:
        record(9, "  which limits failed", "INFO", str(refused))

    # ------------------------------------------------ 10. real, not fabricated
    print("\n=== 10. runs for real: audit trail proves the work happened ===")
    try:
        from nexus.db import conn as db

        rows = db.query(
            "SELECT tool, ok, verified, duration_ms FROM tool_invocations "
            "WHERE task_id = ? ORDER BY rowid DESC LIMIT 20",
            (task_id,),
        )
        tools_used = sorted({row["tool"] for row in rows})
        verified_any = any(row["verified"] == 1 for row in rows)
        record(
            10,
            "audit trail rows written for this task",
            "PASS" if len(rows) >= 6 else "FAIL",
            f"{len(rows)} invocations, tools={tools_used[:6]}, any_verified={verified_any}",
        )
    except Exception as exc:  # noqa: BLE001
        record(10, "audit trail rows written for this task", "FAIL", f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------- 11. browser automation
    print("\n=== 11. drives a real browser and reads back what it rendered ===")
    from nexus.tools import registry as tool_registry

    nav_tool = tool_registry.get("browser.navigate")
    if nav_tool is None or not nav_tool.available:
        # A machine without Chrome is not a defect in NEXUS. The tools declare
        # their requirement precisely so this can be reported as "cannot run
        # here" with the reason, rather than as a failure to hunt for.
        record(
            11,
            "browser automation over CDP",
            "REFUSED",
            "no Chrome or Chromium on this machine, so browser.drive cannot run here",
        )
    else:
        # A real button, injected into the live DOM, whose only effect is to
        # change the title. Clicking a link on example.com would also prove the
        # click landed -- but it would prove it by leaving the page, which makes
        # the test depend on iana.org answering, and it would conflate "the click
        # mechanism works" with "the internet works". This isolates the thing
        # under test: whether Input.dispatchMouseEvent really reaches the element
        # at its box centre.
        INJECT = """
        (() => {
          const old = document.getElementById('nexus-acceptance-btn');
          if (old) old.remove();
          const b = document.createElement('button');
          b.id = 'nexus-acceptance-btn';
          b.textContent = 'nexus acceptance button';
          b.style.cssText = 'position:fixed;top:20px;left:20px;padding:24px 40px;'
                          + 'font-size:20px;z-index:2147483647';
          b.addEventListener('click', () => { document.title = 'NEXUS-CLICK-CONFIRMED'; });
          document.body.appendChild(b);
          return b.getBoundingClientRect().width;
        })()
        """

        try:
            nav = await runtime_execute("browser.navigate", {"url": "https://example.com"}, task_id)
            detail_bits: list[str] = []
            problems: list[str] = []
            refused_network = False

            dom_title = None
            if not nav.ok:
                blob = f"{nav.error or ''}"
                if any(w in blob.lower() for w in ("could not be found", "network", "timed out", "name resolution", "connection")):
                    refused_network = True
                    problems.append(f"navigate refused (network): {blob[:120]}")
                else:
                    problems.append(f"navigate failed: {blob[:160]}")
            else:
                dom_title = (nav.data or {}).get("title")
                detail_bits.append(f"title={dom_title!r} verified={nav.verified}")
                if not nav.verified:
                    problems.append(f"navigate not verified: {nav.verification}")

            # The cross-check: the same URL read by a completely different path.
            # CDP renders the page in a real browser; net.fetch downloads the
            # bytes and parses them. If these two agree on the title, the DOM
            # read is not a fabrication of the driver.
            if dom_title and not refused_network:
                fetched = await runtime_execute("net.fetch", {"url": "https://example.com"}, task_id)
                fetch_title = (
                    fetched.data.get("title")
                    if fetched.ok and isinstance(fetched.data, dict)
                    else None
                )
                agree = (fetch_title or "").strip().lower() == (dom_title or "").strip().lower()
                detail_bits.append(f"http_title={fetch_title!r} agree={agree}")
                if not agree:
                    problems.append(
                        f"the DOM reports title {dom_title!r} but http reports {fetch_title!r}"
                    )

            # Reading the rendered page: links and text.
            if not problems:
                q = await runtime_execute("browser.query", {"selector": "a[href]"}, task_id)
                link_count = (q.data or {}).get("count", 0) if q.ok else 0
                rd = await runtime_execute("browser.read", {"mode": "text", "max_characters": 5000}, task_id)
                dom_text = (rd.data or {}).get("text", "") if rd.ok else ""
                detail_bits.append(f"links={link_count} dom_chars={len(dom_text)}")
                if link_count < 1:
                    problems.append("no links found in the rendered page")
                if len(dom_text) < 50:
                    problems.append(f"the rendered page yielded only {len(dom_text)} characters of text")

            # The click, on a real element at real coordinates.
            click_changed = False
            if not problems:
                inj = await runtime_execute("browser.eval", {"expression": INJECT}, task_id)
                if not inj.ok:
                    problems.append(f"could not inject the test button: {(inj.error or '')[:120]}")
                else:
                    clk = await runtime_execute(
                        "browser.click", {"selector": "#nexus-acceptance-btn"}, task_id
                    )
                    title_now = await runtime_execute(
                        "browser.eval", {"expression": "document.title"}, task_id
                    )
                    got = (title_now.data or {}).get("result") if title_now.ok else None
                    click_changed = got == "NEXUS-CLICK-CONFIRMED"
                    detail_bits.append(
                        f"click ok={clk.ok} verified={clk.verified} "
                        f"changed={(clk.data or {}).get('changed')} title_after={got!r}"
                    )
                    if not click_changed:
                        problems.append(
                            f"the click did not reach the button (title is {got!r}, "
                            f"expected 'NEXUS-CLICK-CONFIRMED')"
                        )

            # The screenshot, verified from the bytes on disk rather than the buffer.
            shot_path = None
            if not problems:
                shot = await runtime_execute("browser.screenshot", {}, task_id)
                if not shot.ok:
                    problems.append(f"screenshot failed: {(shot.error or '')[:120]}")
                else:
                    shot_path = Path(str((shot.data or {}).get("path", "")))
                    w = (shot.data or {}).get("width")
                    h = (shot.data or {}).get("height")
                    size = shot_path.stat().st_size if shot_path.is_file() else 0
                    detail_bits.append(f"png={w}x{h} bytes={size} verified={shot.verified}")
                    if not (shot.verified and w and h and size > 500):
                        problems.append(
                            f"the screenshot is not a usable PNG ({w}x{h}, {size} bytes)"
                        )
        except Exception as exc:  # noqa: BLE001 -- a crash here is a FAIL, not an abort
            problems = [f"{type(exc).__name__}: {exc}"]
            detail_bits = []
            refused_network = False

        # The guard that must not be missing: a navigate to a private or
        # link-local address has to be refused *by the tool*, not merely by a
        # comment. 169.254.169.254 is the cloud metadata endpoint -- the address
        # that turns "read a web page" into "read this machine's credentials".
        try:
            ssrf = await runtime_execute(
                "browser.navigate", {"url": "http://169.254.169.254/latest/meta-data/"}, task_id
            )
            blocked = not ssrf.ok
            detail_bits.append(f"metadata_ssrf_blocked={blocked}")
            if not blocked:
                problems.append("browser.navigate reached the cloud metadata address")
        except Exception as exc:  # noqa: BLE001
            detail_bits.append(f"metadata_ssrf_check_raised={type(exc).__name__}")
            problems.append(f"the SSRF guard raised instead of refusing: {exc}")

        # Close the browser so a window is not left on the desktop by a test run.
        try:
            await runtime_execute("browser.close", {}, task_id)
        except Exception:  # noqa: BLE001
            pass

        if refused_network and len(problems) == 1:
            # The only thing that went wrong is that this machine could not reach
            # example.com. Nothing about the browser was tested, so this is not a
            # verdict on it either way -- say so rather than calling it a defect.
            record(11, "browser automation over CDP", "REFUSED", problems[0])
        else:
            record(
                11,
                "browser automation over CDP",
                "FAIL" if problems else "PASS",
                " | ".join(problems) if problems else " ".join(detail_bits),
            )
        if problems:
            record(11, "  what the browser check saw", "INFO", " ".join(detail_bits))

    # ---------------------------------------------------------------- 12. documents
    print("\n=== 12. document generation: files open, and the verifier can fail ===")
    try:
        from nexus.tools.registry import all_tools as _all_tools
        from nexus.tools.registry import ToolResult as _ToolResult
    except Exception as exc:  # noqa: BLE001
        record(12, "document generation", "REFUSED", f"the tool registry would not load: {exc}")
    else:
        DOCDIR = WORKDIR / "documents"
        DOCDIR.mkdir(parents=True, exist_ok=True)
        PHRASE = "Nexus acceptance marker phrase"
        CONTENT = [
            {"type": "heading", "level": 1, "text": "Acceptance Report"},
            {"type": "paragraph", "text": f"{PHRASE} for the document check."},
            {"type": "bullets", "items": ["first checked item", "second checked item"]},
            {"type": "table", "headers": ["Column", "Value"],
             "rows": [["alpha", "1"], ["beta", "2"]]},
            {"type": "heading", "level": 2, "text": "Detail"},
            {"type": "numbered", "items": ["step one", "step two"]},
            {"type": "code", "text": "line one of code\nline two of code"},
        ]
        # What each format's own verifier counts, so a mismatch is reported as a
        # disagreement rather than as an unexplained "not verified".
        EXPECTED = {
            "docx": "paragraphs headings list_items code_blocks tables",
            "xlsx": "rows filled_rows cells",
            "pptx": "slides",
            "pdf": "pages",
        }

        problems: list[str] = []
        details: list[str] = []
        written: dict[str, Path] = {}

        for kind, expect in EXPECTED.items():
            path = DOCDIR / f"acceptance.{kind}"
            try:
                result = await runtime_execute(
                    "documents.create",
                    {"kind": kind, "path": str(path), "title": "Acceptance Report",
                     "content": CONTENT},
                    task_id,
                )
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{kind}: raised {type(exc).__name__}: {exc}")
                continue

            if not result.ok:
                problems.append(f"{kind}: {result.error}")
                continue
            if result.verified is not True:
                problems.append(f"{kind}: written but not verified -- {result.verification}")
                continue
            if not path.exists() or path.stat().st_size == 0:
                problems.append(f"{kind}: verification passed but the file is empty")
                continue
            written[kind] = path
            details.append(f"{kind}={path.stat().st_size}B")

        # Read one back through the tool, which is what lets NEXUS check its own
        # work later. The phrase has to survive the round trip or the reader is
        # not reading what the writer wrote.
        if "docx" in written:
            try:
                back = await runtime_execute(
                    "documents.read", {"path": str(written["docx"])}, task_id
                )
                if not back.ok:
                    problems.append(f"docx could not be read back: {back.error}")
                elif PHRASE not in (back.data or {}).get("text", ""):
                    problems.append(
                        "the text read back from the .docx does not contain the phrase "
                        "that was written into it"
                    )
                else:
                    details.append(f"read-back {back.data.get('characters')} chars")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"docx read-back raised {type(exc).__name__}: {exc}")

        # A real application opening the file is evidence no re-parse can give:
        # it proves the parts, relationships and content types are not merely
        # self-consistent but that a reader which never saw this code accepts them.
        if "docx" in written:
            import shutil as _shutil
            import subprocess as _subprocess

            if _shutil.which("soffice") is None or _shutil.which("pdftotext") is None:
                record(12, "  an outside application opens the .docx", "INFO",
                       "skipped: LibreOffice or pdftotext is not installed on this machine")
            else:
                try:
                    converted = _subprocess.run(
                        ["soffice", "--headless", "--convert-to", "pdf",
                         "--outdir", str(DOCDIR), str(written["docx"])],
                        capture_output=True, timeout=180, check=False,
                    )
                    external = DOCDIR / "acceptance.pdf"
                    if not external.exists():
                        problems.append(
                            "LibreOffice did not produce a PDF from the .docx: "
                            + (converted.stderr or converted.stdout or b"").decode()[-200:]
                        )
                    else:
                        text = _subprocess.run(
                            ["pdftotext", str(external), "-"],
                            capture_output=True, timeout=60, check=False,
                        ).stdout.decode("utf-8", "replace")
                        if PHRASE not in text:
                            problems.append(
                                "LibreOffice opened the .docx but the phrase is missing "
                                "from what it rendered"
                            )
                        else:
                            details.append("LibreOffice rendered it")
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"the outside-reader check raised {type(exc).__name__}: {exc}")

        # THE NEGATIVE CASE. A verifier that only ever sees files its own writer
        # produced cannot fail, and a check that cannot fail proves nothing. This
        # runs the verifier that is *registered on the tool* -- the same function
        # the runtime calls -- against a corrupted copy of a file that just
        # verified. The healthy file is put through the identical path first, so
        # "it said no" cannot be explained by a verifier that says no to
        # everything.
        if "docx" in written:
            create_tool = {t.name: t for t in _all_tools()}.get("documents.create")
            if create_tool is None or create_tool.verifier is None:
                problems.append("documents.create has no registered verifier to test")
            else:
                source = written["docx"]
                data = source.read_bytes()
                truncated = DOCDIR / "truncated.docx"
                truncated.write_bytes(data[: int(len(data) * 0.6)])

                def as_verifier_input(target: Path):
                    return _ToolResult(
                        ok=True,
                        data={
                            "path": str(target),
                            "claimed": {"format": "docx"},
                            "requested": {"paragraphs": 1, "headings": 2},
                        },
                    )

                healthy_ok, _healthy_detail = create_tool.verifier(
                    {}, as_verifier_input(source)
                )
                broken_ok, broken_detail = create_tool.verifier(
                    {}, as_verifier_input(truncated)
                )

                if not healthy_ok:
                    problems.append(
                        "the verifier rejected the healthy file, so its rejection of the "
                        f"truncated one proves nothing (it said: {_healthy_detail})"
                    )
                elif broken_ok:
                    problems.append(
                        "the verifier ACCEPTED a .docx truncated to 60% of its bytes -- "
                        "the read-back check cannot fail and is not a check"
                    )
                else:
                    details.append(f"truncated file rejected: {broken_detail[:60]}")

        record(
            12,
            "documents written, verified and rejectable",
            "FAIL" if problems else "PASS",
            " | ".join(problems) if problems else "  ".join(details),
        )
        if problems:
            record(12, "  what the document check saw", "INFO", "  ".join(details))

    # ------------------------------------------------------------------ summary
    print("\n" + "=" * 100)
    counts: dict[str, int] = {}
    for _n, _t, verdict, _d in RESULTS:
        counts[verdict] = counts.get(verdict, 0) + 1
    print("SUMMARY:", counts)
    failures = [r for r in RESULTS if r[2] == "FAIL"]
    if failures:
        print("\nFAILURES (real defects):")
        for number, title, _v, detail in failures:
            print(f"  §11.{number} {title}: {detail}")
    else:
        print("No acceptance test failed.")
    print("=" * 100)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
