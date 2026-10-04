#!/usr/bin/env bash
# nexus-watchdog.sh — keep the Claude Code session alive.
#
# Runs the session and watches it. When it stops, the watchdog reports what
# happened and decides whether to relaunch:
#
#   exit 0   -> finished cleanly      -> "done", and the watchdog STOPS.
#   exit 130 -> Ctrl-C by the user    -> interrupted on purpose, STOPS.
#   anything else (crash / kill / terminal killed the process)
#            -> stopped unexpectedly  -> relaunch, so a crash is a short pause
#                                        rather than "the assistant is gone".
#
# That last rule is the whole point, and the "only on an unexpected exit" rule is
# the safety valve that stops it becoming a loop: a session that genuinely
# *finished* never spins back up on its own.
#
# A crash-loop is bounded too. If the session dies with a non-zero exit three
# times in a row within RAPID_CRASH_WINDOW seconds, the watchdog gives up and
# says so, instead of relaunching a broken session forever.
#
# What "unexpected" does NOT catch: if the *whole terminal* closes, a foreground
# watchdog dies with it before it can relaunch. To survive that, run the watcher
# detached — under tmux or nohup — see the Usage note at the bottom.
#
# Usage:
#   ./nexus-watchdog.sh            # resume the most recent session (claude --continue)
#   ./nexus-watchdog.sh <id>       # resume a specific session (claude --resume <id>)
#
#   # survive a closed terminal / logout:
#   tmux new -s nexus-watchdog './nexus-watchdog.sh'
#   # or
#   nohup ./nexus-watchdog.sh >> /tmp/nexus-watchdog.log 2>&1 &
#
# Environment:
#   CLAUDE_BIN      path to the claude binary    (default: claude)
#   MAX_RESTARTS    crash-loop cap               (default: 3)
#   WATCHDOG_LOG    where events are written     (default: /tmp/nexus-watchdog.log)
set -u

CLAUDE_BIN="${CLAUDE_BIN:-claude}"
MAX_RESTARTS="${MAX_RESTARTS:-3}"
WATCHDOG_LOG="${WATCHDOG_LOG:-/tmp/nexus-watchdog.log}"
#: A crash counts as "rapid" if the session lived less than this long, in seconds.
RAPID_CRASH_WINDOW=30
#: 128 + 2: the exit code the shell fabricates for a process killed by SIGINT.
SIGINT_EXIT=130

mkdir -p -- "$(dirname -- "$WATCHDOG_LOG")"

if ! command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
  printf 'nexus-watchdog: "%s" is not on PATH. Set CLAUDE_BIN.\n' "$CLAUDE_BIN" >&2
  exit 2
fi

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a -- "$WATCHDOG_LOG"
}

notify() {
  local title="$1" msg="$2" urgency="$3"
  if command -v notify-send >/dev/null 2>&1; then
    notify-send -u "$urgency" "$title" "$msg" 2>/dev/null || true
  fi
}

# What to relaunch. A specific session id beats the default "most recent one".
launch_cmd=("$CLAUDE_BIN")
if [ "$#" -ge 1 ]; then
  launch_cmd+=(--resume "$1")
else
  launch_cmd+=(--continue)
fi

log "watching: ${launch_cmd[*]}"
log "launching session…"

consecutive_rapid=0
while :; do
  started=$SECONDS
  "${launch_cmd[@]}"
  code=$?

  if [ "$code" -eq 0 ]; then
    log "Claude Code finished cleanly — staying stopped."
    notify "NEXUS Watchdog" "Claude Code finished. Watchdog standing by — session stopped." normal
    exit 0
  fi

  if [ "$code" -eq "$SIGINT_EXIT" ]; then
    log "interrupted (SIGINT) — the user stopped it, so staying stopped."
    notify "NEXUS Watchdog" "Session interrupted by you. Staying stopped." normal
    exit 0
  fi

  # Any other non-zero exit is unexpected: a crash, a kill, a dropped terminal.
  elapsed=$((SECONDS - started))
  log "session stopped unexpectedly (exit $code) after ${elapsed}s."

  if [ "$elapsed" -lt "$RAPID_CRASH_WINDOW" ]; then
    consecutive_rapid=$((consecutive_rapid + 1))
  else
    consecutive_rapid=1
  fi

  if [ "$consecutive_rapid" -ge "$MAX_RESTARTS" ]; then
    log "stopped after $MAX_RESTARTS rapid exits — not relaunching a broken session."
    notify "NEXUS Watchdog" "Giving up after $MAX_RESTARTS rapid exits. Check $WATCHDOG_LOG" critical
    exit 1
  fi

  notify "NEXUS Watchdog" "Session stopped (exit $code). Restarting…" critical
  log "relaunching session…"
  sleep 2
done