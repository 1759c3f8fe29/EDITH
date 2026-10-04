"""Settings key/value store (task B3; spec §42 Settings screen).

Settings are typed at the edges: values are JSON-encoded on the way in and
decoded on the way out, so a boolean toggle round-trips as a boolean rather than
the string "true". ``DEFAULTS`` is the single source of truth for what a setting
means when it has never been written -- a missing row is not an error.

API credentials are deliberately *not* here. They live in the OS keyring via
``nexus.secrets`` so they never enter the database or a settings export (§65).
"""

from __future__ import annotations

from typing import Any

from .. import conn as db
from .base import dumps, loads, now, to_list

# key -> default value. Every key the UI can touch must appear here.
DEFAULTS: dict[str, Any] = {
    # Appearance (§30, §51)
    "theme": "dark",
    "accent": "violet",
    "reduce_motion": False,
    "font_scale": 1.0,
    # Voice (§9, §12). NEXUS is voice-first, so these default on: the microphone is
    # armed when a screen with a composer opens, and an answer is spoken as well as
    # printed. What they do *not* do is bypass the browser's own microphone
    # permission -- getUserMedia asks once per origin and nothing here can answer for
    # the user.
    "voice_enabled": True,
    # Hands-free listening, on: talking to a machine that needs a keypress first is
    # not voice-first. The cost concern that kept this off is answered in the shell
    # rather than by leaving the feature unavailable -- lib/wake.ts measures the
    # room's own noise floor locally and only a segment that crossed it, lasted long
    # enough to be a phrase, and stayed under twelve seconds is uploaded; more than
    # twenty phrases in a minute stops the listener outright and says why, because
    # that is a television and not a conversation. Silence costs nothing. What is
    # still true is that there is no local speech model here, so every phrase that
    # does pass the gate is a paid transcription -- which is why the gate exists and
    # why the switch is on this list rather than compiled in.
    "wake_word_enabled": True,
    # Matched against the first four words of a phrase, case and punctuation ignored.
    # Several words are allowed ("hey nexus"); lib/wake.ts - matchWake() is the rule.
    "wake_word": "nexus",
    # Read by the shell after every completed turn: it POSTs /api/voice/speak and
    # plays what comes back. Off means the answer is printed only.
    "tts_enabled": True,
    # Empty means the routed provider's own default (nexus.providers.speech
    # .default_voice), since the two providers' defaults share nothing. /api/voice
    # /speak falls back to this when the caller names no voice, and drops it when
    # the stored name belongs to a different provider than the one routed to.
    "tts_voice": "",
    # Handed to /api/voice/transcribe so the model is told the language instead of
    # guessing it. A wrong guess transliterates rather than transcribes.
    "input_language": "en",
    # Model routing (§13-§15)
    "router_mode": "auto",  # auto | pinned
    "pinned_provider": "",
    "pinned_model": "",
    "allow_fallback": True,
    # Lets routing learn which model actually serves each job, from the rewards in
    # `model_outcomes` (nexus.learning). Defaults **on**, unlike the monitors,
    # because it observes NEXUS's own provider calls rather than the user -- there
    # is no privacy question to answer -- and because a learner nobody switches on
    # is a feature that does not exist. What keeps it honest is that it can only
    # reorder candidates the router already judged usable, and every reorder is
    # named in the decision's reason, so it can never be a silent substitution.
    "router_learning_enabled": True,
    # The main model. Conversation, planning and tool calling go here first; the
    # per-job keys below only override a single kind of work. Stored as settings
    # rather than constants so the user can change what "main" means without a
    # rebuild. nexus.router reads these.
    #
    # These two must name the same model as ``router.jobs.MAIN_MODEL``, and they
    # spell it out rather than importing it *on purpose*. Deriving it here would
    # invert the layering -- the router consumes settings, so a settings module
    # that imports the router points the dependency backwards -- and the import is
    # cheap enough that the cost would not be what stopped you. What keeps the two
    # honest instead is a test: ``test_router_jobs.py`` fails the moment this
    # literal and that constant disagree. Do not "tidy" this into an import; the
    # duplication is deliberate and it is pinned.
    "main_provider": "nvidia",
    # Re-pointed 2026-10-04 with ``router.jobs.MAIN_MODEL``: the id that stood here
    # (``deepseek-ai/deepseek-v4-flash-0731``) answers 410 Gone, retired by NVIDIA
    # on 2026-08-26. ``test_router_jobs.py`` is what caught the half of the rename
    # that would otherwise have been forgotten -- it asserts this pair and
    # ``MAIN_MODEL`` name the same model, which is the whole reason it exists.
    "main_model": "nvidia/nemotron-3-super-120b-a12b",
    # Per-job overrides, formatted "provider:model" -- e.g.
    # "nvidia:meta/llama-3.2-11b-vision-instruct". Empty means "use the router's
    # default for this job", which is what lets a fresh install work with one key
    # instead of eleven.
    "model_chat": "",
    "model_expert": "",
    "model_reasoning": "",
    "model_coding": "",
    "model_fast": "",
    "model_vision": "",
    "model_ocr": "",
    "model_video": "",
    "model_stt": "",
    "model_tts": "",
    "model_embedding": "",
    "model_rerank": "",
    # How much NEXUS says back (§10, task F10). "quick" is the default because the
    # assistant is voice-first and a spoken paragraph is worse than a spoken
    # sentence; "detailed" is for debugging sessions, "silent" for hands-free use
    # where the user wants the action and not the narration. "expert" is the odd one
    # out: it also routes the turn to the strongest model available, so it costs
    # real latency and is never a default. No mode suppresses a failure or a
    # permission request -- see nexus.ai.persona.MODES.
    "response_mode": "quick",
    # Whether a small fast model gets a second opinion on what a request is, when
    # the keyword rules were not confident (see nexus.ai.intent.refine). On by
    # default: the rules misread the sentences that matter most -- "can you see
    # what's eating my battery" is an inspection, not a screenshot -- and the model
    # is only consulted for the minority of turns the rules already flagged as a
    # guess. Turning it off costs accuracy, never function: the rules' reading
    # always stands as the fallback.
    "intent_model_enabled": True,
    # Whether recall searches by meaning as well as by keyword. On by default:
    # semantic search is what makes memory answer a paraphrase, and turning it off
    # is a deliberate choice to stop calling the embedding endpoint. When it is
    # off, or the endpoint is unreachable, search says so rather than silently
    # returning less (see nexus.ai.embeddings.search).
    "semantic_search_enabled": True,
    # Approved memories retrieved into one turn's context. Small on purpose: the
    # top few relevant memories improve an answer, twenty crowd out the actual
    # conversation and cost tokens that the request itself needs.
    "memory_recall_limit": 6,
    # Whether NEXUS proposes memories from conversation at all (task F5). The
    # proposals still require approval before anything is remembered -- this
    # switch decides whether it suggests, not whether it saves.
    "memory_extract_enabled": True,
    # Whether NEXUS also goes back over past conversation on a timer, picking up
    # exchanges the inline pass missed -- a crash, an unreachable model, a turn that
    # arrived by voice (see nexus.ai.memory_sweep). Behind
    # memory_extract_enabled: this is a second mechanism for the same permission,
    # not a second decision. The sweep proposes; approval is still the user's.
    "memory_sweep_enabled": True,
    "memory_sweep_interval_seconds": 900,
    # The watermark: the last exchange the sweep examined, as "<created_at>|<id>".
    # Written by the sweep, not by the user. Clearing it makes NEXUS re-read the
    # whole history, which costs a model call per exchange and proposes nothing it
    # already has.
    "memory_sweep_cursor": "",
    # Awareness (§39, §47, §54). Every one of these defaults to off, or to the
    # cheapest honest setting: NEXUS watching the screen is a decision the user
    # makes, never a state it arrives in.
    "monitor_enabled": False,
    "monitor_system_enabled": True,
    "monitor_system_interval_seconds": 60,
    "monitor_window_enabled": False,
    "monitor_window_interval_seconds": 5,
    "monitor_screen_enabled": False,
    "monitor_screen_interval_seconds": 300,
    "monitor_screen_describe": False,
    # How much of the image must differ before "the screen changed". Only
    # meaningful when ImageMagick is installed; without it the comparison is an
    # exact byte match and this is ignored (the observation says which was used).
    "monitor_screen_change_percent": 8,
    # Frames kept on disk for comparison and for the timeline. Older ones are
    # deleted as they age out, and all of them when watching stops.
    "monitor_screen_keep_frames": 12,
    "monitor_clipboard_enabled": False,
    "monitor_clipboard_interval_seconds": 3,
    # USB. Reads the kernel's device tree, not the contents of anything, so it is
    # the cheapest watcher here -- but still off by default, because "NEXUS tells
    # me what I plugged in" is a decision the user makes (§39).
    "monitor_usb_enabled": False,
    # Five seconds: plugging something in and hearing about it half a minute later
    # reads as a coincidence rather than a response. The tree is a few dozen small
    # files, so the poll costs nothing measurable.
    "monitor_usb_interval_seconds": 5,
    # Bluetooth. One ``busctl`` call per tick, so it is a subprocess rather than a
    # few file reads -- ten seconds instead of five. Nothing here pairs or connects
    # anything; pairing is a decision with a security consequence and stays in the
    # system's own dialog (§20).
    "monitor_bluetooth_enabled": False,
    "monitor_bluetooth_interval_seconds": 10,
    "monitor_cpu_alert_percent": 90,
    "monitor_memory_alert_percent": 90,
    "monitor_disk_alert_percent": 92,
    "monitor_battery_alert_percent": 15,
    # How long observations survive. Everything the monitors notice is deleted
    # this many days after it happened, whether or not anything read it -- see
    # nexus.db.repos.ambient. 0 means "keep nothing past today".
    "ambient_retention_days": 7,
    # Whether observations are written down at all. Off means the monitors still
    # drive the live UI over the event bus but leave no history behind, which is
    # the right setting for someone who wants awareness without a record.
    "ambient_history_enabled": True,
    "proactive_suggestions_enabled": False,
    "proactive_quiet_hours": "22:00-08:00",
    # Memory (§16-§18)
    "memory_enabled": True,
    "memory_auto_propose": True,
    "memory_require_approval": True,
    "semantic_search_enabled": True,
    # Permissions (§5-§8)
    #
    # "trusted" means NEXUS acts without stopping to ask -- the assistant is
    # voice-first and hands-free, and a spoken request that ends in a dialog nobody
    # is looking at is a request that did not happen. It changes exactly one thing:
    # whether a question is asked. It does not widen what is allowed. A hard limit
    # still refuses (a protected path, a command that wipes a home directory), a
    # capability switched off here is still off, a scope explicitly denied on the
    # Permissions screen is still denied, and every decision is still written to the
    # activity log -- so "did not ask" never means "left no trace". Set it to "ask"
    # to be prompted per capability again.
    "permission_mode": "trusted",
    # The one thing trusted mode would still ask about: an action that is both
    # dangerous and irreversible. Off, because "never ask" was asked for literally and
    # because the floor underneath it is not a prompt: NEXUS refuses outright, and
    # cannot be configured not to, to write system files, read credentials or private
    # keys, or recursively delete a home directory or / -- see
    # nexus.permissions.policy.check_hard_limits. Those are denials, not questions, so
    # turning questions off does not reach them. Set this to true to get one
    # confirmation back on irreversible actions (deleting a file, shutting down)
    # without leaving trusted mode.
    "confirm_destructive": False,
    "session_grants_expire_minutes": 240,
    # Arbitrary shell access. On, because an assistant that controls a computer
    # without a terminal is not controlling the computer -- and this is the switch
    # that says so, rather than every terminal request being refused with a reason
    # the user cannot act on. Destructive commands are still refused outright by
    # nexus.permissions.policy, which is a hard limit and not a prompt.
    "terminal_allowed": True,
    # Agent mode (§20-§23)
    "agent_max_steps": 12,
    "agent_verify_steps": True,
    "agent_pause_on_failure": True,
    # Automations (§31-§34, §47). The engine in nexus.automations reads all three of
    # these, which is why they are here: the module docstring above says every key the
    # UI can touch must appear in DEFAULTS, and /api/settings rejects any key that does
    # not -- so a switch missing from this dict is a switch nothing can flip. The
    # engine's own reads already fall back to these same values, so adding them changes
    # no behaviour; it makes the master switch reachable.
    "automations_enabled": True,
    # Seconds between passes over the due rules. The engine floors this at
    # MIN_TICK_SECONDS (5) regardless of what is stored, because a one-second tick is a
    # database query per second forever for a feature whose finest schedule is 30s.
    "automations_tick_seconds": 20.0,
    # How late a run may be and still fire. A rule due while the machine was asleep is
    # delivered when it wakes, up to this; past it the run is recorded as skipped
    # rather than delivered hours out of context.
    "automations_max_lateness_seconds": 21600,
    # Retention (task B10, §71)
    "retain_activity_days": 90,
    "retain_conversations_days": 0,  # 0 = keep forever
    "retain_tool_invocations_days": 30,
    # Sync (Phase K, §26-§29)
    "sync_enabled": False,
    "sync_interval_seconds": 300,
    # Diagnostics (§66)
    "diagnostics_verbose": False,
}


def get(key: str, default: Any = None) -> Any:
    row = db.query_one("SELECT value FROM settings WHERE key = ?", (key,))
    if row is None:
        return DEFAULTS.get(key, default)
    return loads(row["value"], DEFAULTS.get(key, default))


def get_all() -> dict[str, Any]:
    """Defaults overlaid with anything explicitly stored."""
    merged = dict(DEFAULTS)
    for row in to_list(db.query("SELECT key, value FROM settings")):
        merged[row["key"]] = loads(row["value"], DEFAULTS.get(row["key"]))
    return merged


def set(key: str, value: Any) -> Any:
    """Persist one setting. Returns the stored value on success.

    Raises StorageError on failure so the caller can report that the setting did
    not save rather than showing a toggle that silently reverts (§62).
    """
    with db.transaction() as c:
        rev = db.next_rev(c)
        c.execute(
            """
            INSERT INTO settings (key, value, updated_at, rev)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at,
                rev = excluded.rev
            """,
            (key, dumps(value), now(), rev),
        )
    return value


def set_many(values: dict[str, Any]) -> dict[str, Any]:
    stamp = now()
    with db.transaction() as c:
        for key, value in values.items():
            c.execute(
                """
                INSERT INTO settings (key, value, updated_at, rev)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at,
                    rev = excluded.rev
                """,
                (key, dumps(value), stamp, db.next_rev(c)),
            )
    return values


def reset(key: str) -> Any:
    """Remove the override so the default applies again."""
    with db.transaction() as c:
        c.execute("DELETE FROM settings WHERE key = ?", (key,))
    return DEFAULTS.get(key)


def unknown_keys() -> list[str]:
    """Stored keys with no default -- a leftover from a removed feature.

    ``DEFAULTS.keys()`` rather than ``set(DEFAULTS)``: this module defines its own
    ``set``, which shadows the builtin, so the obvious spelling raises a TypeError
    on every call.
    """
    stored = {row["key"] for row in db.query("SELECT key FROM settings")}
    return sorted(stored - DEFAULTS.keys())
