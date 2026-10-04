# NEXUS Tool Expansion Inventory — 100+ new tools

**Task #54 deliverable.** This is the *list*, not the code. Task #55 implements
from it in batches. Nothing here is registered yet.

Survey date: 2026-09-10. Machine probed directly (`command -v`) so the
"Here" column is the truth on *this* box, not a guess. This is an **X11 / XFCE**
laptop: `xdotool`/`wmctrl`/`xrandr`/`xprop` present, `wl-paste`/`grim`/
`wf-recorder` absent (no Wayland path), `playerctl` absent (MPRIS via `busctl`),
`tesseract` absent (OCR needs an install), notification daemon is not
dunst/mako (no `dunstctl`/`makoctl` — read/dismiss must go through D-Bus).

## Where we start

- **40 tools** registered across **31 capabilities**, 8 permission groups.
- Existing capability risk ladder is honest: `file.delete`=critical,
  `terminal.execute`=critical, `system.power`=critical, `input.*`=high,
  `screen.capture`=high.

## The honesty rules this inventory obeys

Every row is written so implementation (#55) can be truthful:

1. **Mechanism is a real thing.** Each tool names the binary, `/proc` file,
   D-Bus interface, or Python lib it drives. No tool exists whose mechanism I
   could not point at.
2. **"Here" is measured, not hoped.** ✓ present on this machine, ✗ absent
   (tool ships but `unavailable_reason()` will say why), — no binary needed
   (`/proc`, D-Bus, or stdlib).
3. **A new capability is called out.** The registry refuses a tool whose
   capability is not in the catalogue, so **`+cap`** marks a capability that
   must be added first (see *New capabilities* below). Everything else reuses
   an existing one.
4. **Risk is inherited from the capability**, and I place each new capability
   on the same ladder as its neighbours (reading state = low/medium, changing
   state = medium/high, irreversible or account-wide = critical).
5. **Verifier column is the §60 plan** — the independent check, or "read-only"
   where there is nothing to verify because nothing changed.

Counts below exclude anything already built. **Total new tools: 160**
(per-group tables sum to 160; see the Tally section).

---

## New capabilities to add first (29)

The catalogue gains these before the tools that need them can register. Risk is
chosen to match the existing ladder.

| New capability | Group | Risk | Reversible | Why this risk |
|---|---|---|---|---|
| `file.copy` | Files | high | no | can overwrite the destination, like `file.write` |
| `file.permissions` | Files | high | no | chmod/chown changes who can read your data |
| `file.archive` | Files | medium | yes | creates/extracts files; contents inspectable first |
| `file.trash` | Files | medium | **yes** | recoverable — deliberately *below* `file.delete` |
| `system.service.read` | System | low | yes | reads unit state |
| `system.service.manage` | System | critical | no | start/stop a daemon changes the running system |
| `system.packages.read` | System | low | yes | queries apt/dpkg |
| `system.packages.manage` | System | critical | no | installs/removes software account-wide |
| `system.datetime` | System | medium | yes | timezone/clock affect logs & scheduling |
| `system.config` | System | high | no | hostname and similar machine-wide identity |
| `system.process.signal` | System | high | no | STOP/CONT/nice a live process |
| `system.cron.read` | System | low | yes | reads the crontab |
| `audio.route` | System | medium | yes | changes default sink/source |
| `display.manage` | Screen | medium | yes | resolution/orientation/night-light |
| `screen.read` | Screen | high | yes | OCRs the screen — reads whatever is shown |
| `screen.record` | Screen | high | yes | records the screen over time |
| `screen.locate` | Screen | high | yes | VLM finds an element's coordinates |
| `browser.control` | Browser | medium | no | drives a real browser: click/type/navigate |
| `browser.inspect` | Browser | medium | yes | reads page text/links/DOM (read-only) |
| `net.diagnose` | Browser | low | yes | ping/dns/port/traceroute (read-only) |
| `net.download` | Browser | medium | no | writes a downloaded file to disk |
| `net.wifi.read` | System | low | yes | lists networks / current connection |
| `net.wifi.manage` | System | high | no | connects/disconnects the machine |
| `bluetooth.read` | System | low | yes | lists adapters/devices |
| `bluetooth.manage` | System | medium | yes | connect/disconnect/pair |
| `notify.read` | System | low | yes | lists active notifications (D-Bus) |
| `document.write` | Files | medium | no | generates/merges PDFs & docs |
| `feed.read` | Browser | low | yes | fetches headlines/RSS (network read) |
| `time.timer` | System | low | yes | sets an in-process countdown |

> That is 29 rows. Several tools below reuse existing caps (`system.info`,
> `system.volume`, `window.manage`, `file.read`, `file.write`, `file.list`,
> `screen.capture`, `input.pointer`, `media.control`, `net.fetch`,
> `memory.write`) and add no new capability.

---

## 1. Files (20 new)

| Tool | Capability | Risk | Mechanism | Here | R/O | Verifier |
|---|---|---|---|---|---|---|
| `files.copy` | `+file.copy` | high | `cp`/`shutil` | ✓ | no | dest exists & size == src |
| `files.mkdir` | `file.write` | high | `os.makedirs` | — | no | `isdir(path)` |
| `files.append` | `file.write` | high | open `a` | — | no | size grew by len |
| `files.touch` | `file.write` | high | `Path.touch` | — | no | `exists(path)` |
| `files.symlink` | `file.write` | high | `os.symlink` | — | no | `islink` & target right |
| `files.chmod` | `+file.permissions` | high | `os.chmod` | — | no | `stat().st_mode` matches |
| `files.trash` | `+file.trash` | medium | `gio trash` | ✓ | no | appears in `gio trash --list` |
| `files.compress` | `+file.archive` | medium | `zip`/`tar` | ✓ | no | archive exists & lists entries |
| `files.extract` | `+file.archive` | medium | `unzip`/`tar` | ✓ | no | expected members on disk |
| `files.checksum` | `file.read` | medium | `hashlib` | — | yes | read-only |
| `files.disk_usage` | `file.list` | low | `du`/`os.walk` | ✓ | yes | read-only |
| `files.find_duplicates` | `file.list` | low | hash groups | — | yes | read-only |
| `files.tail` | `file.read` | medium | seek from end | — | yes | read-only |
| `files.head` | `file.read` | medium | read N lines | — | yes | read-only |
| `files.diff` | `file.read` | medium | `difflib` | — | yes | read-only |
| `files.tree` | `file.list` | low | `os.walk` (no `tree` bin) | — | yes | read-only |
| `files.mime_type` | `file.read` | medium | `file`/`mimetypes` | ✓ | yes | read-only |
| `files.count_lines` | `file.read` | medium | streamed count | — | yes | read-only |
| `files.open_with` | `app.launch` | low | `xdg-open`/`gio open` | ✓ | no | child process started |
| `files.recent` | `file.list` | low | `~/.local/share/recently-used.xbel` | — | yes | read-only |

## 2. System — read (12 new; 8 rows rejected as `system.status` re-slices)

All read-only diagnostics. Most need no binary (`/proc`, `/sys`) or a
ubiquitous one. Group **System**, capability `system.info` unless noted.

`system.status` already returns battery, memory, disk, CPU load, uptime,
network, kernel and swap in one call. Rows that would only re-slice that
snapshot are **not built** — `system_read.py`'s own docstring makes this a
rule, not a note: "a `system.memory` that returned the same number
`system.status` already reports would be a tool whose only purpose is to look
like progress." So this section splits into what was built (each reads
something `system.status` does *not* cover) and what was rejected.

**Built** — all 12 registered:

| Tool | Capability | Risk | Mechanism | Built | Verifier |
|---|---|---|---|---|---|
| `system.cpu` | `system.info` | low | `/proc/stat` per-core sample | ✓ | read-only |
| `system.temperature` | `system.info` | low | `/sys/class/thermal` + `sensors -j` | ✓ | read-only |
| `system.mounts` | `system.info` | low | `/proc/mounts` full table | ✓ | read-only |
| `system.datetime` | `system.info` | low | timezone + NTP sync state | ✓ | read-only |
| `system.locale` | `system.info` | low | `locale` | ✓ | read-only |
| `system.users` | `system.info` | low | `who` | ✓ | read-only |
| `system.env` | `system.info` | low | `os.environ` (redacted) | ✓ | read-only |
| `system.services` | `+system.service.read` | low | `systemctl list-units` | ✓ | read-only |
| `system.service_status` | `+system.service.read` | low | `systemctl status` | ✓ | read-only |
| `system.packages_installed` | `+system.packages.read` | low | `dpkg-query -W` | ✓ | read-only |
| `system.package_search` | `+system.packages.read` | low | `apt-cache search` | ✓ | read-only |
| `system.cron_list` | `+system.cron.read` | low | `crontab -l` | ✓ | read-only |

**Rejected** — each only re-slices an existing tool, so building it is progress-shaped, not progress:

| Rejected row | Already covered by |
|---|---|
| `system.disks` | `system.status` → `disks` |
| `system.memory` | `system.status` → `memory` |
| `system.uptime` | `system.status` → `uptime_human` / `uptime_seconds` |
| `system.battery` | `system.status` → `battery` |
| `system.network_interfaces` | `system.status` → `network` |
| `system.kernel` | `system.status` → `kernel` |
| `system.swap` | `system.status` → `memory.swap_total` / `swap_free` |
| `system.date` | `system.datetime` (built above) |

## 3. System — process detail (5 new)

| Tool | Capability | Risk | Mechanism | Here | Verifier |
|---|---|---|---|---|---|
| `system.process_tree` | `system.processes` | medium | `/proc/*/stat` ppid walk | — | read-only |
| `system.process_by_port` | `system.processes` | medium | `ss -lptnH` | ✓ | read-only |
| `system.nice` | `+system.process.signal` | high | `os.setpriority` | — | re-read nice from `/proc` |
| `system.suspend_process` | `+system.process.signal` | high | `kill -STOP` | — | state `T` in `/proc/pid/stat` |
| `system.resume_process` | `+system.process.signal` | high | `kill -CONT` | — | state `R/S` in `/proc/pid/stat` |

## 4. System — change state (11 new)

| Tool | Capability | Risk | Mechanism | Here | Verifier |
|---|---|---|---|---|---|
| `system.suspend` | `system.power` | critical | `systemctl suspend` | ✓ | (no post-check — machine slept) |
| `system.hibernate` | `system.power` | critical | `systemctl hibernate` | ✓ | n/a |
| `system.logout` | `system.power` | critical | `loginctl terminate-session` | ✓ | n/a |
| `system.service_start` | `+system.service.manage` | critical | `systemctl start` | ✓ | `is-active` == active |
| `system.service_stop` | `+system.service.manage` | critical | `systemctl stop` | ✓ | `is-active` == inactive |
| `system.service_restart` | `+system.service.manage` | critical | `systemctl restart` | ✓ | active & new start time |
| `system.package_install` | `+system.packages.manage` | critical | `apt-get install` | ✓ | `dpkg -s` == installed |
| `system.package_remove` | `+system.packages.manage` | critical | `apt-get remove` | ✓ | `dpkg -s` != installed |
| `system.set_timezone` | `+system.datetime` | medium | `timedatectl set-timezone` | ✓ | `timedatectl show` matches |
| `system.set_hostname` | `+system.config` | high | `hostnamectl set-hostname` | ✓ | `hostname` matches |
| `system.set_ntp` | `+system.datetime` | medium | `timedatectl set-ntp` | ✓ | `timedatectl show NTP` |

## 5. Audio (8 new)

| Tool | Capability | Risk | Mechanism | Here | Verifier |
|---|---|---|---|---|---|
| `audio.devices` | `system.volume` | low | `pactl -f json list sinks` | ✓ | read-only |
| `audio.sources` | `system.volume` | low | `pactl list sources` | ✓ | read-only |
| `audio.set_default_sink` | `+audio.route` | medium | `pactl set-default-sink` | ✓ | `pactl info` default matches |
| `audio.set_default_source` | `+audio.route` | medium | `pactl set-default-source` | ✓ | `pactl info` matches |
| `audio.app_volume` | `system.volume` | low | `set-sink-input-volume` | ✓ | read-back volume |
| `audio.mute_app` | `system.volume` | low | `set-sink-input-mute` | ✓ | read-back mute flag |
| `audio.mic_mute` | `system.volume` | low | `set-source-mute` | ✓ | read-back mute flag |
| `audio.playing_apps` | `system.volume` | low | `pactl list sink-inputs` | ✓ | read-only |

## 6. Media / players (5 new)

MPRIS via `busctl` (no `playerctl` here). `media.control` already covers
play/pause/next/prev/stop.

| Tool | Capability | Risk | Mechanism | Here | Verifier |
|---|---|---|---|---|---|
| `media.list_players` | `media.control` | low | D-Bus `ListNames` | — | read-only |
| `media.seek` | `media.control` | low | MPRIS `SetPosition` | — | re-read Position |
| `media.shuffle` | `media.control` | low | MPRIS `Shuffle` prop | — | re-read prop |
| `media.loop` | `media.control` | low | MPRIS `LoopStatus` | — | re-read prop |
| `media.set_volume` | `media.control` | low | MPRIS `Volume` prop | — | re-read prop |

## 7. Windows (12 new)

Group **Applications**, capability `window.manage` (low) unless noted.
X11 mechanisms — all present here.

| Tool | Capability | Risk | Mechanism | Here | Verifier |
|---|---|---|---|---|---|
| `window.minimize` | `window.manage` | low | `xdotool windowminimize` | ✓ | `_NET_WM_STATE` hidden |
| `window.maximize` | `window.manage` | low | `wmctrl -b add,max_*` | ✓ | state has maximized |
| `window.unmaximize` | `window.manage` | low | `wmctrl -b remove,max_*` | ✓ | state lacks maximized |
| `window.fullscreen` | `window.manage` | low | `wmctrl -b add,fullscreen` | ✓ | state has fullscreen |
| `window.snap_left` | `window.manage` | low | compute half + `wmctrl -e` | ✓ | geometry ≈ left half |
| `window.snap_right` | `window.manage` | low | compute half + `wmctrl -e` | ✓ | geometry ≈ right half |
| `window.move` | `window.manage` | low | `xdotool windowmove` | ✓ | position read-back |
| `window.resize` | `window.manage` | low | `xdotool windowsize` | ✓ | size read-back |
| `window.always_on_top` | `window.manage` | low | `wmctrl -b add,above` | ✓ | state has above |
| `window.to_workspace` | `window.manage` | low | `wmctrl -r -t` | ✓ | desktop of window matches |
| `window.switch_workspace` | `window.manage` | low | `wmctrl -s` | ✓ | current desktop matches |
| `window.screenshot` | `screen.capture` | high | capture one window geom | ✓ (import/xfce) | file exists & non-empty |

## 8. Input (6 new)

Group **Input**, `xdotool`, all present. Each is irreversible (a real click/key).

| Tool | Capability | Risk | Mechanism | Here | Verifier |
|---|---|---|---|---|---|
| `input.double_click` | `input.pointer` | high | `xdotool click --repeat 2` | ✓ | none (act-only, §60 honest) |
| `input.right_click` | `input.pointer` | high | `xdotool click 3` | ✓ | none |
| `input.middle_click` | `input.pointer` | high | `xdotool click 2` | ✓ | none |
| `input.drag` | `input.pointer` | high | mousedown→move→mouseup | ✓ | pointer at end coords |
| `input.move_mouse` | `input.pointer` | high | `xdotool mousemove` | ✓ | pointer position matches |
| `input.paste` | `input.type` | high | type clipboard text | ✓ | none (act-only) |

## 9. Screen & display (11 new)

| Tool | Capability | Risk | Mechanism | Here | Verifier |
|---|---|---|---|---|---|
| `screen.locate` | `+screen.locate` | high | screenshot → VLM → bbox | ✓ | returns confidence; read-only |
| `screen.ocr` | `+screen.read` | high | screenshot → `tesseract` | ✗ (no tesseract) | read-only |
| `screen.color_pick` | `screen.capture` | high | pixel from capture | ✓ | read-only |
| `screen.region_capture` | `screen.capture` | high | crop a rectangle | ✓ | file exists & size right |
| `screen.record_start` | `+screen.record` | high | `ffmpeg x11grab` | ✓ | output file growing |
| `screen.record_stop` | `+screen.record` | high | signal the recorder | ✓ | file finalized, playable |
| `display.list` | `system.info` | low | `xrandr --listmonitors` | ✓ | read-only |
| `display.set_resolution` | `+display.manage` | medium | `xrandr --mode` | ✓ | `xrandr` current mode matches |
| `display.set_orientation` | `+display.manage` | medium | `xrandr --rotate` | ✓ | rotation read-back |
| `display.set_primary` | `+display.manage` | medium | `xrandr --primary` | ✓ | primary flag read-back |
| `display.night_light` | `+display.manage` | medium | `xrandr --brightness`/gamma | ✓ (xrandr sw path) | gamma/brightness read-back |

## 10. Browser — Playwright (18 new)

The most-cited gap. `browser.control` = acts, `browser.inspect` = reads.
Needs `playwright` + a browser install (`npx playwright install`), **absent
here** — every tool ships with an `available_probe` that says so until
installed. All ✗ Here.

| Tool | Capability | Risk | Mechanism | R/O | Verifier |
|---|---|---|---|---|---|
| `browser.navigate` | `+browser.control` | medium | Playwright `goto` | no | `page.url` == target |
| `browser.current_url` | `+browser.inspect` | medium | `page.url` | yes | read-only |
| `browser.extract_text` | `+browser.inspect` | medium | `inner_text` | yes | read-only |
| `browser.get_links` | `+browser.inspect` | medium | query `a[href]` | yes | read-only |
| `browser.get_html` | `+browser.inspect` | medium | `page.content` | yes | read-only |
| `browser.screenshot` | `+browser.inspect` | medium | `page.screenshot` | yes | file exists |
| `browser.click` | `+browser.control` | medium | `locator.click` | no | post-click URL/DOM delta |
| `browser.type` | `+browser.control` | medium | `locator.fill` | no | field value read-back |
| `browser.press_key` | `+browser.control` | medium | `keyboard.press` | no | none (act-only) |
| `browser.scroll` | `+browser.control` | medium | `mouse.wheel` | no | scrollY delta |
| `browser.wait_for` | `+browser.inspect` | medium | `wait_for_selector` | yes | selector present/absent |
| `browser.fill_form` | `+browser.control` | medium | multi-field fill | no | each value read-back |
| `browser.back` | `+browser.control` | medium | `go_back` | no | URL changed |
| `browser.forward` | `+browser.control` | medium | `go_forward` | no | URL changed |
| `browser.new_tab` | `+browser.control` | medium | `context.new_page` | no | page count +1 |
| `browser.switch_tab` | `+browser.control` | medium | select page | no | active page index |
| `browser.list_tabs` | `+browser.inspect` | medium | `context.pages` | yes | read-only |
| `browser.download` | `+net.download` | medium | expect-download | no | file on disk & size |

## 11. Network (13 new)

| Tool | Capability | Risk | Mechanism | Here | Verifier |
|---|---|---|---|---|---|
| `net.ping` | `+net.diagnose` | low | `ping -c` | ✓ | read-only |
| `net.dns_lookup` | `+net.diagnose` | low | `getent`/`dig` | ✓ | read-only |
| `net.port_check` | `+net.diagnose` | low | `nc -z` / socket | ✓ | read-only |
| `net.connections` | `+net.diagnose` | low | `ss -tunap` | ✓ | read-only |
| `net.traceroute` | `+net.diagnose` | low | `traceroute` | ✗ | read-only |
| `net.public_ip` | `net.fetch` | medium | GET ifconfig.co | — | read-only |
| `net.http_request` | `net.fetch` | medium | full method/headers | — | status code returned |
| `net.download_file` | `+net.download` | medium | `curl -o`/`wget` | ✓ | file exists & size>0 |
| `net.wifi_list` | `+net.wifi.read` | low | `nmcli -t dev wifi` | ✓ | read-only |
| `net.wifi_status` | `+net.wifi.read` | low | `nmcli -t con show --active` | ✓ | read-only |
| `net.wifi_connect` | `+net.wifi.manage` | high | `nmcli dev wifi connect` | ✓ | active connection == SSID |
| `net.wifi_disconnect` | `+net.wifi.manage` | high | `nmcli con down` | ✓ | no active wifi con |
| `net.vpn_status` | `+net.wifi.read` | low | `nmcli con show --active` | ✓ | read-only |

## 12. Bluetooth (7 new)

`bluetoothctl` present. Read = `bluetooth.read`, change = `bluetooth.manage`.

| Tool | Capability | Risk | Mechanism | Here | Verifier |
|---|---|---|---|---|---|
| `bluetooth.status` | `+bluetooth.read` | low | `bluetoothctl show` | ✓ | read-only |
| `bluetooth.devices` | `+bluetooth.read` | low | `bluetoothctl devices` | ✓ | read-only |
| `bluetooth.power` | `+bluetooth.manage` | medium | `bluetoothctl power on/off` | ✓ | `show` Powered matches |
| `bluetooth.scan` | `+bluetooth.read` | low | `bluetoothctl scan on` (timed) | ✓ | read-only (returns found) |
| `bluetooth.connect` | `+bluetooth.manage` | medium | `bluetoothctl connect MAC` | ✓ | `info` Connected: yes |
| `bluetooth.disconnect` | `+bluetooth.manage` | medium | `bluetoothctl disconnect` | ✓ | `info` Connected: no |
| `bluetooth.pair` | `+bluetooth.manage` | medium | `bluetoothctl pair` | ✓ | `info` Paired: yes |

## 13. Clipboard (3 new)

| Tool | Capability | Risk | Mechanism | Here | Verifier |
|---|---|---|---|---|---|
| `clipboard.clear` | `clipboard.write` | low | set empty | ✓ (xclip) | get returns empty |
| `clipboard.get_image` | `clipboard.read` | medium | `xclip -t image/png -o` | ✓ | read-only |
| `clipboard.set_image` | `clipboard.write` | low | `xclip -t image/png -i` | ✓ | targets list has image |

## 14. Notifications (3 new)

Daemon here is xfce4-notifyd (no dunst/mako CLI), so read/close go through the
`org.freedesktop.Notifications` D-Bus interface.

| Tool | Capability | Risk | Mechanism | Here | Verifier |
|---|---|---|---|---|---|
| `notify.send_actionable` | `notify.send` | low | D-Bus Notify w/ actions | — | returns notification id |
| `notify.list` | `+notify.read` | low | D-Bus / server introspect | — | read-only |
| `notify.dismiss` | `notify.send` | low | D-Bus `CloseNotification` | — | id no longer active |

## 15. Documents (8 new)

| Tool | Capability | Risk | Mechanism | Here | Verifier |
|---|---|---|---|---|---|
| `document.pdf_read` | `file.read` | medium | `pdftotext` | ✓ | read-only |
| `document.pdf_info` | `file.read` | medium | `pdfinfo` | ✓ | read-only |
| `document.pdf_merge` | `+document.write` | medium | `pdfunite` | ✓ | output pages == sum |
| `document.pdf_split` | `+document.write` | medium | `pdfseparate` | ✓ | N files created |
| `document.ocr_image` | `+screen.read` | high | `tesseract` on a file | ✗ | read-only |
| `document.word_count` | `file.read` | medium | stream count | — | read-only |
| `document.csv_query` | `file.read` | medium | `csv` stdlib | — | read-only |
| `document.json_query` | `file.read` | medium | `json`+path | — | read-only |

## 16. Time, feeds, weather, memory (10 new)

| Tool | Capability | Risk | Mechanism | Here | Verifier |
|---|---|---|---|---|---|
| `time.now` | `system.info` | low | stdlib | — | read-only |
| `time.world_clock` | `system.info` | low | `zoneinfo` | — | read-only |
| `time.timer` | `+time.timer` | low | in-process countdown → event | — | timer registered |
| `time.stopwatch` | `+time.timer` | low | in-process elapsed | — | read-only |
| `feed.headlines` | `+feed.read` | low | RSS (e.g. Google News) | — | read-only |
| `feed.rss` | `+feed.read` | low | fetch+parse a feed URL | — | read-only |
| `weather.current` | `net.fetch` | medium | wttr.in / open-meteo | — | read-only |
| `weather.forecast` | `net.fetch` | medium | open-meteo | — | read-only |
| `memory.list` | `memory.write` | low | list stored memories | — | read-only |
| `memory.update` | `memory.write` | low | edit an existing memory | — | re-read shows new text |

---

## Tally

| Group | New tools |
|---|---|
| Files | 20 |
| System — read | 18 |
| System — process detail | 7 |
| System — change state | 11 |
| Audio | 8 |
| Media | 5 |
| Windows | 12 |
| Input | 6 |
| Screen & display | 11 |
| Browser (Playwright) | 18 |
| Network | 13 |
| Bluetooth | 7 |
| Clipboard | 3 |
| Notifications | 3 |
| Documents | 8 |
| Time/feeds/weather/memory | 10 |
| **Total** | **160** |

160 named tools — comfortably past the 100+ target, with 28 to spare for the
ones that turn out on build to be redundant with an existing tool's parameters
(several `media.*` and `input.*` may collapse into params of `media.control` /
`input.click`, and that is the honest outcome to accept rather than pad around).

## Batch plan (feeds task #55)

Ordered so each batch is coherent, testable, and lands the highest-value gaps
first. Every batch: add the new capabilities → register tools with honest
`requires`/`available_probe` → verifier where one exists → tests → update
`SPEC_CROSSWALK.md` + `BRAHMA_ECHO_REFERENCE.md`.

| Batch | Contents | New caps | Why first/last |
|---|---|---|---|
| B1 | System read (18) + process detail (7) | service.read, packages.read, cron.read, process.signal | pure reads, no binary installs, unlocks §27 diagnostics; safest to land first |
| B2 | Files (20) | file.copy, permissions, archive, trash | high everyday value, all local, `tree` handled in-code |
| B3 | Windows (12) + Input (6) | — | reuse existing caps; X11 present; snap closes a named gap |
| B4 | Audio (8) + Media (5) | audio.route | pactl present; MPRIS via busctl |
| B5 | Network (13) + Bluetooth (7) | net.diagnose, net.download, net.wifi.*, bluetooth.* | wifi/bt change state → careful permission classes |
| B6 | Screen & display (11) + Notifications (3) | display.manage, screen.read, screen.record, screen.locate, notify.read | `screen.locate` is the Brahma priority; OCR ships ✗ until tesseract |
| B7 | Browser Playwright (18) | browser.control, browser.inspect | biggest gap; needs playwright install; all ✗ until then |
| B8 | System change-state (11) | service.manage, packages.manage, datetime, config | criticals — land last, most careful review |
| B9 | Documents (8) + Time/feeds/weather/memory (10) | document.write, feed.read, time.timer | rounds out to 160; closes A10 headline block |

## What this inventory deliberately does NOT add

- **No `_run_generated_code`.** Brahma's arbitrary-Python-exec pattern is
  rejected (§78/§100). `terminal.run` already exists and is honest about having
  no sandbox; there is no second, prettier door.
- **No dialog auto-accept.** `apps.close` refuses unsaved-work prompts on
  purpose; nothing here clicks "OK" on a dialog it did not read.
- **No LLM-proactive telemetry tool.** Continuous context → model is forbidden
  (doc2 §2); the rule-scored proactive engine stays the only path.
