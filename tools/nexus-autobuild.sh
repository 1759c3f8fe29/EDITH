#!/usr/bin/env bash
# NEXUS build supervisor - restart the build session when it stops.
#
# Why this exists: a build session ends for reasons that have nothing to do with the
# work - a context limit, a closed terminal, a dropped network, a crash. This watches
# for that and starts the session again, continuing the same conversation.
#
#   tools/nexus-autobuild.sh            # supervise, in a terminal, in the foreground
#   tools/nexus-autobuild.sh --status   # what it has done so far
#   tools/nexus-autobuild.sh --stop     # ask a running supervisor to exit
#
# It deliberately does NOT relax the permission gate. The restarted session asks for
# approval exactly as it would if you had started it yourself, which is why this must
# run in a terminal you can see rather than as a detached service. Handing an
# unattended loop the authority to edit and run anything without asking is a decision
# only you can make -- so it is one line, in your hands, and empty by default:
#
#   AUTOBUILD_CLAUDE_FLAGS='--permission-mode acceptEdits' tools/nexus-autobuild.sh
#
# Whatever you put there is passed to `claude` verbatim and written to the log, so the
# supervisor's own record says what authority each session was started with. If you
# put a full bypass there, know that you are the approval gate you just removed: this
# loop edits files and runs commands on this machine with no one watching.
#
# It also never runs two sessions at once: each cycle checks for a live `claude`
# process first, so the worst case is a supervisor that waits.

set -uo pipefail

ROOT="/home/santosh/EDITH"
TOOLS="$ROOT/tools"
PROMPT_FILE="$TOOLS/autobuild-prompt.md"
LOG_DIR="$ROOT/.autobuild"
LOG="$LOG_DIR/supervisor.log"
SESSION_LOG="$LOG_DIR/session.log"
STOP_FILE="$TOOLS/.autobuild-stop"
LOCK="$LOG_DIR/supervisor.lock"
STATE="$LOG_DIR/state.tsv"

#: Wait between cycles while a session is alive. Long enough that checking costs
#: nothing, short enough to pick the work up promptly once it ends.
POLL_SECONDS=30
#: A session that dies in under a minute is failing for a reason a faster retry will
#: not fix, so those get a backoff and a cap instead of a tight loop.
FAST_FAIL_SECONDS=60
BACKOFF_SECONDS=120
MAX_FAST_FAILS=5

mkdir -p "$LOG_DIR"

log() { printf '%s  %s\n' "$(date -Is)" "$*" | tee -a "$LOG"; }

case "${1:-}" in
  --stop)
    : > "$STOP_FILE"
    echo "Stop requested. A running supervisor exits after its current cycle."
    exit 0
    ;;
  --status)
    echo "supervisor log: $LOG"
    echo "session log:    $SESSION_LOG"
    if [[ -f "$STATE" ]]; then
      echo
      printf '%-26s %-9s %-6s %s\n' STARTED OUTCOME CODE SECONDS
      tail -n 20 "$STATE" | while IFS=$'\t' read -r started outcome code secs; do
        printf '%-26s %-9s %-6s %s\n' "$started" "$outcome" "$code" "$secs"
      done
    else
      echo "(no sessions recorded yet)"
    fi
    if [[ -f "$STOP_FILE" ]]; then
      echo
      echo "STOPPED: $STOP_FILE exists. Delete it to allow restarts."
    fi
    exit 0
    ;;
esac

command -v claude >/dev/null 2>&1 || { log "FATAL: no 'claude' on PATH."; exit 127; }
[[ -f "$PROMPT_FILE" ]] || { log "FATAL: missing $PROMPT_FILE"; exit 66; }

# Credentials come from the environment, or from an env file you control. The file is
# sourced and never printed. Missing credentials are fatal rather than survivable:
# restarting into the same 401 forever is worse than stopping and saying so.
if [[ -z "${ANTHROPIC_AUTH_TOKEN:-}${ANTHROPIC_API_KEY:-}" && -f "$TOOLS/.autobuild-env" ]]; then
  # shellcheck disable=SC1090
  set -a; source "$TOOLS/.autobuild-env"; set +a
fi
if [[ -z "${ANTHROPIC_AUTH_TOKEN:-}${ANTHROPIC_API_KEY:-}" ]]; then
  log "FATAL: no ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY in the environment."
  log "       Put the exports in $TOOLS/.autobuild-env (chmod 600) and start again."
  exit 78
fi

# One supervisor at a time.
exec 9>"$LOCK"
if ! flock -n 9; then
  log "Another supervisor already holds $LOCK. Exiting."
  exit 0
fi

rm -f "$STOP_FILE"
log "Supervisor up. root=$ROOT poll=${POLL_SECONDS}s"

fast_fails=0
cycle=0

while true; do
  if [[ -f "$STOP_FILE" ]]; then
    log "Stop file present. Exiting."
    exit 0
  fi

  # Already running? Then the build is not stopped and there is nothing to do. The
  # pattern matches the CLI's own process, not this script.
  if pgrep -f '(^|/)claude( |$)' >/dev/null 2>&1; then
    sleep "$POLL_SECONDS"
    continue
  fi

  cycle=$((cycle + 1))
  started_at="$(date -Is)"
  started_epoch="$(date +%s)"
  log "cycle $cycle: nothing running - starting a session"
  printf '\n===== cycle %s  %s =====\n' "$cycle" "$started_at" >> "$SESSION_LOG"

  cd "$ROOT" || exit 70
  # `-c` continues the most recent conversation in this directory, so a restart keeps
  # the context it had. With no prior conversation, `claude` starts a fresh one from
  # the same briefing - which is the fallback wanted.
  #
  # Extra flags are read into an array rather than interpolated as a string, because
  # `$VAR` unquoted would split on whitespace inside a quoted flag value and quoted
  # would pass the whole thing as one argument. Empty by default, and logged either
  # way so the record says what authority the session ran with.
  read -r -a extra_flags <<< "${AUTOBUILD_CLAUDE_FLAGS:-}"
  if (( ${#extra_flags[@]} )); then
    log "cycle $cycle: extra flags: ${extra_flags[*]}"
  fi
  claude -c "${extra_flags[@]}" "$(cat "$PROMPT_FILE")" 2>&1 | tee -a "$SESSION_LOG"
  code=${PIPESTATUS[0]}

  elapsed=$(( $(date +%s) - started_epoch ))
  if (( code == 0 )); then outcome=exited; else outcome=failed; fi
  printf '%s\t%s\t%s\t%s\n' "$started_at" "$outcome" "$code" "$elapsed" >> "$STATE"
  log "cycle $cycle: $outcome code=$code after ${elapsed}s"

  if (( elapsed < FAST_FAIL_SECONDS )); then
    fast_fails=$((fast_fails + 1))
    if (( fast_fails >= MAX_FAST_FAILS )); then
      log "FATAL: $fast_fails sessions in a row died in under ${FAST_FAIL_SECONDS}s."
      log "       Restarting will not fix that - read $SESSION_LOG."
      exit 75
    fi
    log "backing off ${BACKOFF_SECONDS}s (fast failure $fast_fails/$MAX_FAST_FAILS)"
    sleep "$BACKOFF_SECONDS"
  else
    fast_fails=0
    sleep 5
  fi
done
