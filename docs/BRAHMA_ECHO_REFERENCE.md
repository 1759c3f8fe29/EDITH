# Brahma Echo (`~/Downloads/Brahma-Echo-main`) as a build reference

Survey done 2026-09-08, code read directly. Brahma Echo is a Windows
voice-first desktop assistant: Gemini 2.5 Flash + OpenRouter, 44 action
modules, Playwright browser automation, pyautogui/pywinauto control, an
Android phone gateway, and a plugin loader. Below: what its six areas
actually are, and what is **MAIN** (worth building in NEXUS) versus what
NEXUS already does better or should deliberately not copy.

## 1. Agent — MAIN: LLM error handler that *edits and retries* the step

Brahma (`agent/`, 1206 lines):
- `planner.py` (360) — Gemini breaks a goal into ≤5 steps, tool list
  verbatim in the prompt, `replan()` on failure. Every step independent
  (no result chaining).
- `executor.py` (433) — lazy per-tool dispatch; on failure calls
  `error_handler.analyze_error()` → LLM classifies → `generate_fix()` →
  retries with modified params.
- `task_queue.py` — TaskStatus/TaskPriority enums, 1 worker, submit/
  cancel/status.
- `error_handler.py` — `ErrorDecision` enum + LLM analysis.

**MAIN to take:** the analyze→fix→retry loop. NEXUS has replan-on-failure
(`agent.py`) but its recovery classification (`recovery.py`) never asks a
model to *propose a corrected step*. One model call on step failure with
the error text and the step's tool schema, gated by the existing step
ceiling, is the whole feature.
**Do NOT take:** `_run_generated_code` (executor.py:25) — Gemini writes
arbitrary Python and it is executed with pip-install rights. This is the
single most dangerous pattern in the codebase and violates NEXUS's
§78/§100 posture and the terminal no-sandbox honesty rule.
**NEXUS already better:** plan-as-data with per-step verified tri-state,
task persistence + interrupted-resume, 7-state task lifecycle vs Brahma's
in-memory queue.

## 2. Automation — MAIN: scheduled actions beyond notify

Brahma: `calendar_scheduler.py` (JSON-file events, NL date/time parsing),
`reminder.py`, `proactive.py` (ProactiveEngine 2.0: 15-min silence gate,
20-min cooldown, time-of-day context rotation), `daily_briefing.py`
(top-headlines briefing), `game_updater.py` (`schedule` +
`shutdown_when_done`), `background_monitor.py`.

**MAIN to take:** the *action* half. NEXUS automations are
schedule/interval/manual × **notify-only** (TRIGGER_KINDS, A10) — so
"every evening 7 baje remind me" works, but "every night 2 AM run the
backup script" does not. Brahma proves the shape: a trigger row whose
action references a registered tool + params. In NEXUS terms: extend
`automations_routes.py` action kinds from `notify` to
`tool:{name}` with the permission engine still gating every firing
(observation ≠ authorization stays true — an automation firing a tool
must surface the same permission prompt as the agent).
**Do NOT take:** ProactiveEngine's "send context snapshot to Gemini and
let it decide what to say" — that is continuous telemetry to an LLM,
which doc2 §2 explicitly forbids. NEXUS's rule-scored proactive engine
(L10) is the compliant design.
**NEXUS already better:** automations survive restart (DB vs JSON file),
proactive gate is deterministic and testable.

## 3. Tools — MAIN: browser automation, headlines, window snapping

Brahma's 44 actions; the ones NEXUS lacks:
- `browser_control.py` (638 lines, **Playwright**): go_to/search/click/
  type/scroll/get_text/press/close, tab list/switch/new, back/forward,
  persistent browser profile, engine picker (chromium/firefox). This is
  the exact A6/A10 gap ("navigate/inspect/interact MISSING by design").
- `daily_briefing.py`: `_get_top_headlines(category, limit)` — the A10
  headline block (15 items, zero implementation in NEXUS).
- `computer_settings.py` (714 lines, 62 defs): volume/brightness
  up/down/mute/set, snap-left/right, show-desktop, switch-window,
  open-task-manager, focus-search, close-app/window.
- `docx_tools`/`ppt_tools`/`pdf_tools`/`office_builder` — document
  generation NEXUS has never claimed.

**MAIN to take, in order:** (1) **Playwright browser tools** — the single
most-repeated gap across every doc audit (browser becomes a tool: read
the page, click, fill forms). Build as new `browser.navigate/inspect/
interact` tools behind the existing medium/high risk classes, keeping
the honest docstrings that today say "cannot read the page afterwards".
(2) **Headline feed + daily brief** — an RSS `feed` monitor (observation
kind `feed.headline`, dedupe by link hash) + a briefing automation
action; closes A10's only wholly-absent block. (3) Window snap
( tile-left/right) — `window.arrange` already computes frame extents, so
snap is two presets away.
**NEXUS already better:** permission classes on every tool (Brahma
actions have zero permission gating — any planned step just runs),
availability probes, verification tri-state, 611-test coverage on the
registry.

## 4. System control — MAIN: per-process tools

Brahma: `system_manager.py` — `get_system_health()`, `kill_process(pid|
name)`; `computer_settings.py` volume/brightness set paths for
Windows/macOS/Linux.

**MAIN to take:** `kill_process`-equivalent and the missing
`system.processes` read tool (A6 gap 2: §27's "Chrome is eating your
RAM" sentence is not constructible today). C1's process monitor closes
the observation half; a `system.processes` tool (ps from /proc, no
shell) closes the diagnosis half. Kill maps to a new `high`-risk
`system.kill_process` tool. Everything else in Brahma's set NEXUS has
verified (volume read-back, lock via LockedHint, clipboard round-trip).

## 5. Deep OS integration — MAIN: notification watching + phone gateway

Brahma: `attention_monitor.py` (683) — Windows toast payload parsing +
UIA window enumeration + auto accept/decline of app dialogs with hint
matching; `brahma_connect/` — a full **phone-as-remote** stack:
WebSocket gateway (`discovery`, `pairing`, `authentication`,
`capability_manager`, `command_router`, `device_manager`) +
`brahma-connect-android` app; `smart_home/` device manager + providers;
`unlock_device.py`, `discord_bot.py`, `claude_code_bridge.py`.

**MAIN to take:** (1) **`brahma_connect`'s gateway shape** — A10 Finding
1 says NEXUS's `devices.py` is dead code with no pairing, no heartbeat,
no routes; Brahma is a working reference for exactly that layer
(discovery → pair → capability handshake → routed commands), portable to
Linux with a WS server + the existing devices tables. (2) A
**notification monitor**: on XFCE, `dbus-monitor
interface=org.freedesktop.Notifications` gives NEXUS an observation kind
(`system.notification`) that today does not exist at all.
**Do NOT take:** auto-accepting dialogs sight-unseen (`_click_best_button`
accepts on title hints) — NEXUS's `apps.close` deliberately refuses to
answer an unsaved-work prompt; keep that.

## 6. Computer control — MAIN: screen-find (VLM → coordinates)

Brahma `computer_control.py` (477): pyautogui wrappers (_type/_click/
_hotkey/_scroll/_drag/_focus_window), clipboard paste-as-type, and
`_screen_find(description)` — screenshot → Gemini VLM → "where is it" →
click coordinates. `screen_processor.py` + `desktop.py` (pywinauto UIA)
round it out.

**MAIN to take:** `screen_find`-equivalent. NEXUS has vision
verification (screenshot → VLM → tri-state, task #7) but the *acting*
half — "find the Send button and click it" — does not exist; `input.*`
only takes literal coordinates. One `screen.locate` tool (VLM returns
bbox, tool returns coordinates + confidence, `input.click` consumes
them) makes the input tools usable without x/y clairvoyance. This was
the "computer control" half of the founding doc's §11/§12 pairing.
**NEXUS already better:** X11/Wayland gating, credential-window guard
(refuses to type at password prompts — Brahma will type into anything),
delivery-only verification honesty, per-action permission.

## Marked priority list (what to build first)

1. **Browser automation via Playwright** — closes the most-cited gap in
   every audit; Brahma `browser_control.py` is the reference.
2. **`system.processes` + `system.kill_process` tools** — smallest build,
   unlocks §27 diagnostics end to end.
3. **Automation actions beyond notify** (tool-firing automations, still
   permission-gated) + **feed/headline monitor + daily brief** — closes
   A10's only wholly-absent block.
4. **`screen.locate` VLM tool** feeding `input.click` — makes computer
   control usable without coordinates.
5. **Device gateway wiring** (pair `devices.py` to real WS routes,
   Brahma `brahma_connect` shape) — or delete it, per A10 Finding 1.
6. **LLM step-fix retry** in the agent loop (skip
   `_run_generated_code`).
7. **Notification monitor** (DBus) — new observation kind, no doc gap but
   cheap awareness win.

Everything else in Brahma Echo is either already built stronger in NEXUS
(permission model, verification, persistence, tests) or is a deliberate
rejection (ungated actions, generated-code execution, LLM-proactive
telemetry, dialog auto-accept).
