# A6 — BEHAVIOR, RESPONSE & SYSTEM-CONTROL MASTER PROMPT vs the code

Doc: `docs/NEXUS — JARVIS - FRIDAY - EDITH BEHAVIOR, RESPONSE & SYSTEM-CONTROL MASTER PROMPT.md`
(1878 lines, 68 numbered sections). This audit covers the **system-control half**
(§10 tool inventory, §25 computer control, §26 terminal, §27 diagnostics, §52
device control, plus §14/§15/§17/§18 as they constrain control). The
behaviour/response half (§1–§9, §28–§51, §53–§68) is A9's scope.

## This machine (measured, not assumed)

`XDG_SESSION_TYPE=x11`, `DISPLAY=:0.0`, `XDG_CURRENT_DESKTOP=XFCE`,
`loginctl … Type=x11 Active=yes`. Input synthesis and window management are
therefore possible here — on a Wayland session most of the table below would flip
to BLOCKED, and the code checks for that (`input.py:104-126`).

Binary presence (`command -v`, read-only):

| present | absent |
|---|---|
| wpctl, pactl, amixer, xdotool, wmctrl, xrandr, xprop, xwininfo, notify-send, xclip, systemctl, lo
ginctl, gsettings, dbus-send, busctl, upower, nmcli, iw, ip, lsblk, udevadm, setxkbmap, xset, xdg-o
pen, xdg-screensaver, xflock4, **xfce4-screenshooter** | brightnessctl, light, playerctl, wl-copy,
wl-paste, xsel, scrot, grim, import, gnome-screenshot, spectacle, maim, ddcutil, acpi, i3lock, gnom
e-screensaver-command, qdbus, wlr-randr, swaymsg |

`/sys/class/backlight/intel_backlight` exists (current 743 / max 976) but
`brightness` is `root:root -rw-r--r--` → **not writable by this user**
(`os.access(..., W_OK)` → False).

## Section-by-section

### §10 — the tool inventory the doc lists

The doc names 11 groups. What the registry actually holds (live enumeration via
`registry.all_tools()`): **38 tools, all reporting `available=True`** on this
machine, across 12 prefixes: apps, browser, clipboard, files, input, media, net,
screen, system, terminal, window, memory.

| Doc group | Doc items | Verdict | Evidence |
|---|---|---|---|
| APPLICATION | open/close/list/inspect/focus | **IMPLEMENTED** (4 of 5) | `apps.open` (`apps.py:32
5`, `subprocess.Popen(command, start_new_session=True)` from the parsed `.desktop` Exec), `apps.clo
se` (`apps.py:503`, `wmctrl -ic <id>`), `apps.focus` (`apps.py:425`, `wmctrl -ia <id>`), `apps.inst
alled` (`apps.py:221`, parses `*.desktop`), `apps.windows` (`apps.py:252`, `wmctrl -lpx`). No separ
ate `inspect_application` — `apps.windows` + `apps.installed` cover it. wmctrl present. |
| FILE | search/read/create/modify/move/delete/inspect | **IMPLEMENTED** | 7 tools `files.list/read
/write/move/delete/info/search` (`files.py`), no external binary, all path-gated through `policy.re
solve_path` / `path_refusal`. |
| BROWSER | open/navigate/search/inspect/interact | **PARTIAL** | `browser.open` (`browser.py:309`,
 real `xdg-open <url>`, verified by looking for a live browser process at `:246`), `browser.search`
 (real DuckDuckGo HTML parse), `net.fetch`. **navigate/inspect/interact MISSING by design** — no CD
P, no Playwright; each docstring says so (`browser.py:283` "NEXUS cannot read the page afterwards o
r interact with it"). Same finding as A10. |
| TERMINAL | execute/inspect/cancel | **PARTIAL** | `terminal.run` is real: `bash -c` in its own pr
ocess group, both pipes read with a `selectors` loop, output bounded at 120 000 chars, truncation r
eported, exit code verbatim (`terminal.py:46,253,325-330`). `cancel()`/`running()` exist at `termin
al.py:333-352` — **but see Finding 1: they have zero callers and zero tests.** |
| SYSTEM | cpu/memory/network/processes/inspect/disk | **PARTIAL** | `system.status` (`system.py:19
4`) reads `/proc/meminfo`, `/proc/uptime`, `/proc/loadavg`, `/proc/cpuinfo`, `/sys/class/power_supp
ly/BAT*`, `shutil.disk_usage`, `/sys/class/net/*/operstate` + `/proc/net/route` — all real file rea
ds, no shell. **`get_running_processes` MISSING**: no per-process tool anywhere (`grep process tool
s/*.py` → only `_process_running` as a launch verifier at `apps.py:193`). §27 asks for "active proc
esses" in the slowness diagnosis; that input does not exist. |
| SCREEN | capture/inspect/inspect_region | **PARTIAL** | `screen.capture` (`screen.py:170`): backe
nd table at `:38`, tries each present binary in order, verifies the file exists, is non-empty, carr
ies a real PNG/JPEG signature, and reads width/height out of the IHDR (`_verify_capture` at `:116`)
. On this machine only **xfce4-screenshooter** matches — and its flags in the table (`-f -s {path}`
 / `-w -s {path}`) are correct per `--help`. `inspect_region` MISSING (no crop/region tool). |
| DEVICE | list/inspect/connect/disconnect/invoke_capability | **MISSING as tools** | Zero `device.
*` tools in the registry. `db/repos/devices.py` exists but is uncalled dead code (A10 Finding 1); B
luetooth/USB are *monitors* (observation), not device-control tools. §52's ESP32/phone coordination
: not started. |
| VOICE | listen/transcribe/speak/stop_speaking | **not tools** (implemented elsewhere) | Voice liv
es in routes + `voice_stream.py`, not the tool registry. Functionally present (STT/TTS real, per he
aring audit) except spoken barge-in. Not a control gap, a placement difference. |
| MEMORY | search/save/update/delete | **IMPLEMENTED** | `memory.remember/recall/forget/delete` (`m
emory.py`), backed by the real memories repo. |
| TASK | create/update/cancel/inspect | **not tools** | Tasks are routes + the agent loop, not regi
stry tools. The model reaches them through the agent, not a tool call. |
| AUTOMATION | create/update/pause/resume/delete/inspect | **not tools** | Same: `automations_route
s.py` + engine, no `automation.*` tool. So the model cannot itself create an automation the way §53
 implies ("NEXUS should create a real automation") — a user creates it in the UI. **Genuine gap 3.*
* |

### §25 — computer control must be real

| Capability | Verdict | Evidence + binary |
|---|---|---|
| opening applications | IMPLEMENTED | `apps.py:351` real `Popen`; `_verify_launch` (`:274`) polls
up to 6 s for a **new window in `wmctrl -lpx`** or a live `pgrep -f <binary>`, and returns `False`
with "may have exited immediately" when neither appears. This is the §14 "never say VS Code is open
" rule enforced in code. |
| closing applications | IMPLEMENTED | `wmctrl -ic` (polite close). `_verify_close` (`:443`) polls
5 s for the window id to leave the list and, when it does not, says the app is probably asking abou
t unsaved work — explicitly **not** answering that prompt. |
| switching applications | IMPLEMENTED | `apps.focus` → `wmctrl -ia`; verifier re-reads the active
window. |
| searching / reading / creating / editing files | IMPLEMENTED | `files.*`, real filesystem calls.
|
| operating approved browsers | PARTIAL | open/search/fetch only (above). |
| running approved commands | IMPLEMENTED | `terminal.run` + `policy.terminal_refusal` (`permission
s/policy.py:305`) rejecting catastrophic shapes *before* the permission prompt. |
| inspecting system state | PARTIAL | `system.status` real; **no process list**. |
| interacting with approved devices | MISSING | no device tools. |
| volume | IMPLEMENTED | `pactl set-sink-volume @DEFAULT_SINK@ N%` / `set-sink-mute`, with `amixer
-D pulse sset Master` fallback (`system.py:363-373`); reads back current state first at `:250`. pac
tl present. |
| brightness | **BLOCKED, and the code says so correctly** | Prefers `brightnessctl -q set N%` (`sy
stem.py:497`) — **brightnessctl absent**. Falls back to writing `/sys/class/backlight/intel_backlig
ht/brightness` (`:513`), which is root-owned here, so the `OSError` branch fires with the honest me
ssage naming brightnessctl as the fix. `get` still works (sysfs read, `verified=True`). Note the do
c-level honesty at `system.py:15-18`: xrandr gamma is *not* a backlight change, and `_verify_bright
ness` (`:426`) refuses to claim verification for a gamma path. There is in fact **no xrandr write p
ath at all** — only that comment — so software dimming is not silently substituted. |
| power (lock/sleep/restart/shutdown/logout) | IMPLEMENTED | `_POWER_COMMANDS` (`system.py:534`) tr
ies `loginctl lock-session` → `xdg-screensaver lock` → `xflock4` (first two + xflock4 present); `sy
stemctl suspend/reboot/poweroff` with `loginctl` fallback. Lock is **verified for real** via `login
ctl show-session … -p LockedHint` (`:621`); sleep/restart/shutdown deliberately return `verified=No
ne` with "what happens next is out of NEXUS's hands" (`:639-645`) — the right answer, not a fake su
ccess. |
| clipboard | IMPLEMENTED | Readers/writers split into two tables (`system.py:656-666`) precisely b
ecause wl-paste and wl-copy are separate binaries; xclip present, so both directions work. `clipboa
rd.set` verifies by **reading the clipboard back and comparing** (`_verify_clipboard_set`, `:715`).
 |
| notifications | IMPLEMENTED | `notify-send -a NEXUS -u <urgency> <title> [message]` (`system.py:7
91`); notify-send present. |
| windows / workspaces | IMPLEMENTED | `window.arrange` (`windows.py:323`): `wmctrl -i -r … -b add,
maximized_vert,maximized_horz`, `xdotool windowminimize`, `wmctrl -i -a` for restore, plus move/res
ize with a genuine off-screen guard that computes the **frame** extents from `xprop _NET_FRAME_EXTE
NTS` and refuses a move that would park the window past the edge (`:380-389`). All four required bi
naries present. |
| media keys | IMPLEMENTED (via the fallback path) | `media.control`/`media.status`: playerctl **ab
sent**, so the module speaks **MPRIS over `busctl --user`** directly (`media.py:92-114`, method tab
le at `:82`) — busctl present. Right now `busctl --user list | grep -i mpris` returns nothing, so t
he honest answer is "no players found"; that is a real reading, not a failure. `test_media_mpris.py
` covers this path. |
| mouse / keyboard | IMPLEMENTED (gated) | `input.type/key/click/scroll/pointer_position` all throu
gh `xdotool` (`input.py:129`). Three environment refusals up front (`:108`): missing xdotool, Wayla
nd session, no DISPLAY. A **credential-window guard** reads both the title and `xprop WM_CLASS` (`:
200`) and refuses to type at anything that looks like a password prompt, naming which channel match
ed. `_verify_dispatch` (`:241`) confirms *delivery only* and says outright that whether the app act
ed cannot be observed. |

Doc's three prohibitions — "never simulate control visually", "never move a fake
cursor", "never display fake progress": **no violation found.** Every control tool
ends in a real subprocess or a real file write, and the fakes sweep (B8) found no
canned success envelopes.

### §26 — terminal control

Doc asks: determine the command, validate, check permission, assess destructive
impact, execute only what is required, confirm consequential commands, never
execute merely because the model generated it.

All five present. `terminal.execute` is **critical** risk with
`requires_second_confirmation=True` (verified live from `capabilities.py`), so the
model cannot run a command without an explicit human accept. `policy.terminal_refusal`
blocks the catastrophic shapes pre-prompt. `terminal.py:1-22` states plainly that
there is **no sandbox** — no container, no seccomp, no restricted user — which is
the §78 posture the doc's own "never claim a safety property you do not have" rule
demands. Output truncation is reported, not hidden; non-zero exit is a failure.

### §27 — diagnostics before advice

`system.status` gives CPU load, RAM, disk, battery, uptime, network. The doc's
worked example is *"Your RAM usage is high, with Chrome using most of it"* — that
sentence is **not constructible today**: there is no per-process memory reading
anywhere. Same gap as the master-prompt audit's per-process item and the C1 build
task.

### §17/§18/§19 — permission model and confirmation fatigue

Live risk table matches the doc's intent closely:

- **critical + second confirmation**: `file.delete`, `system.power`, `terminal.execute`
- **high**: `input.type`, `input.key`, `input.pointer`, `screen.capture`, `file.write`, `file.move`
, `memory.delete`
- **medium**: `file.read`, `clipboard.read`, `net.fetch`, `browser.search`, `app.close`, `window.li
st`
- **low** (the doc's "low-risk, just do it" list): `app.launch`, `app.focus`, `app.list`, `file.lis
t`, `system.info`, `system.volume`, `system.brightness`, `window.manage`, `media.control`, `notify.
send`, `clipboard.write`, `browser.open`, `memory.write`

The doc's low-risk examples — opening an app, checking CPU, searching files,
reading network status, inspecting apps — map onto `app.launch` / `system.info` /
`file.list` / `app.list`, all `low`. `file.read` sits at medium (stricter than the
doc's "reading an authorized file" example), which is a deliberate tightening, not
a defect.

## Genuine gaps

1. **`terminal.cancel` is unreachable.** `running()` and `cancel()`/`cancel_all()`
   exist at `terminal.py:333-352` and publish a real `terminal.cancelled` event,
   but grep across `services/core` finds **no caller and no route** — the only
   cancellation that reaches a running command is
   `engine.cancel_all_for_task()` (`permissions/engine.py:513`, called from
   `agent.py:940`), which cancels *permission requests*, not the subprocess. There
   is no `/api/terminal/cancel`, no `run_id` in any route
   (`grep run_id api/` hits only automations), and no test. §26's "cancel_command"
   and §22's interruptibility are therefore **code-present, product-absent** — a
   long-running command started by the agent cannot be stopped by the user except
   by its own timeout. Same shape as the `devices.py` finding: honest code that can
   never run.
2. **No per-process tool** (`get_running_processes`, §10 SYSTEM; §27's "active
   processes"). This is the input §27's own worked example needs. C1 (process
   monitor) closes the observation half; a `system.processes` *tool* would be
   needed for the diagnosis half.
3. **No automation tool** (§10 AUTOMATION, six items; §53 "NEXUS should create a
   real automation"). Automations are reachable only through the UI/routes — the
   model cannot create, pause, resume or inspect one as a tool call, so
   *"Every evening at 7, remind me to study"* cannot be executed end to end by the
   assistant.
4. **No device tools** (§10 DEVICE, five items; §52). Nothing in the registry;
   `devices.py` uncalled.
5. **No region/screen-inspect tool** (§10 SCREEN `inspect_region`). Whole screen or
   whole window only.
6. **Browser navigate/inspect/interact absent** (§10 BROWSER) — by design and
   documented per tool, but still a doc↔code divergence.
7. **Brightness *write* is blocked on this machine**, not implemented: brightnessctl
   is absent and the sysfs node is root-owned. The tool reports this correctly, but
   the capability shows `available=True` in the registry because
   `system.brightness` declares **no `requires`** at all (`system.py:442-456`) —
   unlike every sibling. So the model is offered a brightness tool that will fail on
   the write path. Declaring `requires=("brightnessctl",)` would be wrong too (the
   sysfs path is real where writable); the honest fix is an availability probe that
   checks writability, the way `_backlight_devices()` already can.

## Test coverage of the control surface

Grepping `services/core/tests/` for each tool name — **13 of 20 control tools have
no test at all**: `system.volume`, `system.brightness`, `clipboard.get`,
`clipboard.set`, `system.notify`, `apps.open`, `apps.close`, `apps.focus`,
`files.delete`, `browser.open`, `net.fetch` have zero mentions. Covered:
`window.arrange` (`test_window_manage_x11.py` + `x11_harness.py`), `input.*`
(`test_input_synthesis_x11.py`), `media.control` (`test_media_mpris.py`),
`terminal.run` (only via `test_secret_boundary.py`), `screen.capture` /
`system.power` / `input.type` (only via `test_permission_trust.py`, i.e. their
*permission* behaviour, not their control behaviour). The 611-test suite is strong
on voice, scoring, monitors, permissions and the agent loop, and thin exactly where
the machine gets moved.

## Verdict

The system-control half of this doc is **substantially real**. Every capability
that claims to move the machine does so through a genuine subprocess with real
arguments, and the verification discipline is stronger than the doc asks for: app
launch polls for a real window or process, clipboard writes are read back,
lock state is confirmed from `LockedHint`, screenshots are validated down to the
PNG header, and the paths that *cannot* be verified (shutdown, keystroke
acted-upon, gamma dimming) return `verified=None` with a reason instead of a
confident lie. **Zero FAKE findings** — no tool returns success without doing the
thing.

What is missing is reach, not honesty: three doc tool groups (device, automation,
task) are not in the registry at all, per-process inspection does not exist, and
the one control tool that could be stopped mid-flight cannot be, because
`terminal.cancel` has no caller. One availability declaration is wrong in the
optimistic direction (`system.brightness`), which is the only place in this audit
where the registry over-promises.


A6 landed. The most consequential new finding: terminal.cancel is unreachable code.

terminal.py:333  def running()      → zero callers
terminal.py:337  def cancel(run_id) → zero callers, publishes a real
terminal.py:347  def cancel_all()     terminal.cancelled event nobody triggers

No /api/terminal/* route, no run_id anywhere in api/ except automations, no test. engine.cancel_all_for_task() (permissions/engine.py:513) cancels permission requests, not the subprocess — so a long command the agent starts can only be stopped by its own timeout. Same shape as devices.py: honest code that can never run. That's now two unreachable subsystems.

The control surface itself is real. Every tool that claims to move the machine ends in a genuine subprocess with real arguments, and verification is stronger than the doc asks — app launch polls wmctrl for a new window or pgrep for a live process before claiming success, clipboard.set reads the clipboard back and compares, lock is confirmed from loginctl … -p LockedHint, screenshots are validated down to the PNG IHDR. Zero FAKE findings.

Measured environment facts that change verdicts:

- x11 session on XFCE, so xdotool/wmctrl input and window control genuinely work here.
- playerctl absent → media control runs on the busctl --user MPRIS path, which is present and real. Not a degraded fallback; MPRIS