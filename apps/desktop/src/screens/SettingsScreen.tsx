/**
 * SETTINGS - the switches that are real, and an honest account of the rest (§42, §56).
 *
 * `GET /api/settings` returns every key in `app_settings.DEFAULTS`. Rendering all of
 * them as controls would be a §56 violation, because some of those keys are still read
 * by nothing: `sync_enabled`, `session_grants_expire_minutes` and a handful more are
 * stored faithfully and consulted by no code path in this build. A toggle for one of
 * them would move a switch, save it, and change nothing - which is worse than an
 * absent switch, because the user would believe the machine's behaviour had changed.
 *
 * So this screen splits the list. A setting appears as a live control only when
 * something in the core actually reads it; every other stored key is listed, by name,
 * under what it is waiting for. That list shrinks as features land - and it has:
 * `wake_word`, `confirm_destructive`, `agent_max_steps` and the rest of the voice keys
 * were in it until the code that reads them was written, and each moved to a control on
 * the day it acquired a reader. The `reads` line on every row is that evidence, and it
 * is why a row cannot be added here without naming the function that consults the key.
 *
 * Three more shapes come from the route rather than from taste:
 *
 * - **`monitor_*` and `ambient_*` are refused by `PATCH /api/settings`** with a 400
 *   naming the route that does accept them, because writing them here would leave a
 *   toggle on with no monitor running. They are therefore not on this screen at all -
 *   AWARENESS owns them and starts the monitors when it writes.
 * - **Model and routing keys are not here either.** `model_*`, `main_model`,
 *   `router_mode` and `allow_fallback` are writable through this route, but MODELS is
 *   where they mean something - it can show the chain a pin displaces. Two screens
 *   writing one key is how they come to disagree.
 * - **Only changed keys are sent.** The route rejects the whole body if any key is
 *   unknown, so patching the full settings object would fail the moment the core
 *   dropped a key this build still remembers.
 */

import { useEffect, useRef, useState } from "react";

import {
  ActionResult,
  Button,
  ConfirmDialog,
  DataRow,
  ErrorState,
  LoadingState,
  Panel,
  Section,
  Select,
  StatusPill,
  Tabs,
  TextInput,
  Toggle,
  clockTime,
  useAccent,
  ACCENTS,
} from "../components/primitives";
import { api, type RetentionReport } from "../lib/api";
import { useAction, useAsync } from "../lib/useAsync";
import { PENDING_SCREENS } from "../state/screens";

type TabValue = "behaviour" | "voice" | "memory" | "machine" | "data" | "gaps" | "personality";

const TABS: Array<{ value: TabValue; label: string }> = [
  { value: "behaviour", label: "How it answers" },
  { value: "voice", label: "Voice" },
  { value: "memory", label: "Memory" },
  { value: "machine", label: "This machine" },
  { value: "data", label: "Data on disk" },
  { value: "gaps", label: "Stored but inert" },
  { value: "personality", label: "Personality" },
];

/**
 * One live setting.
 *
 * `reads` names the code that consults the key. It is not decoration: it is the
 * evidence that this control does something, and a row cannot be added to this file
 * without someone finding the reader first.
 */
type Row = {
  key: string;
  label: string;
  help: string;
  reads: string;
  control:
    | { kind: "toggle" }
    | { kind: "choice"; options: Array<{ value: string; label: string }> }
    | { kind: "number"; min: number; max: number; unit: string }
    | { kind: "text"; placeholder: string; maxLength: number };
};

const BEHAVIOUR: Row[] = [
  {
    key: "response_mode",
    label: "Response mode",
    help:
      "How much NEXUS says back. Expert is the odd one out: it also routes the turn to the strongest model configured, so it costs real latency and is never a default. No mode hides a failure or skips a permission request.",
    reads: "ai/persona.py - current_mode(), and MODE_JOB routes 'expert' to the expert job",
    control: {
      kind: "choice",
      options: [
        { value: "quick", label: "Quick - one or two sentences" },
        { value: "detailed", label: "Detailed - what it checked and found" },
        { value: "silent", label: "Silent - one line, no narration" },
        { value: "expert", label: "Expert - strongest model, slower" },
      ],
    },
  },
  {
    key: "intent_model_enabled",
    label: "Ask a fast model what a request means",
    help:
      "Only consulted for the turns the keyword rules already flagged as a guess - 'can you see what is eating my battery' is an inspection, not a screenshot. Turning it off costs accuracy, never function: the rules' reading always stands.",
    reads: "ai/intent.py - refine()",
    control: { kind: "toggle" },
  },
];

/**
 * Voice (§9, §12).
 *
 * Every one of these was in the "stored but inert" list until the routes and the shell
 * were written to read them. They are controls now for the same reason they were text
 * then: a switch is only offered when something consults it.
 */
const VOICE: Row[] = [
  {
    key: "voice_enabled",
    label: "Treat spoken words as commands",
    help:
      "On, what you say after pressing the microphone is sent as an instruction: a person across the room cannot proofread a text box. Off, it is dictation - the transcript lands in the box for you to read and press Enter. The transcript stays on screen either way, so a mishearing is always attributable. Neither setting answers the browser's own microphone permission.",
    reads: "shell/CommandBar.tsx - transcribe()",
    control: { kind: "toggle" },
  },
  {
    key: "tts_enabled",
    label: "Speak answers out loud",
    help:
      "Every completed answer is synthesised by /api/voice/speak and played in this window. Only completed ones: a failed turn's text is a fragment, and an interrupted one was stopped on purpose. An answer longer than one synthesis request is cut at a sentence and the cut is said out loud rather than hidden.",
    reads: "state/voice.tsx - the auto-speak effect",
    control: { kind: "toggle" },
  },
  {
    key: "wake_word_enabled",
    label: "Listen for a wake word",
    help:
      "Holds the microphone open and measures loudness in this process. Only a phrase that crossed the room's own noise floor is uploaded to be transcribed, and only one beginning with the wake word is acted on - the rest is discarded and shown as heard-but-ignored. It disarms itself while NEXUS is speaking, and stops itself entirely if more than 20 phrases arrive in a minute, because that is a television rather than a conversation. There is no local speech model in this build, so each phrase that does pass the gate is a paid transcription.",
    reads: "lib/useWake.ts, via shell/CommandBar.tsx",
    control: { kind: "toggle" },
  },
  {
    key: "wake_word",
    label: "The wake word",
    help:
      "Matched against the first four words of a phrase, ignoring case and punctuation, so \"Um, nexus, open Firefox\" works and \"I told Sam that nexus can do this\" does not. Several words are allowed - \"hey nexus\" is a valid setting.",
    reads: "lib/wake.ts - matchWake()",
    control: { kind: "text", placeholder: "nexus", maxLength: 40 },
  },
  {
    key: "input_language",
    label: "Language you speak",
    help:
      "Handed to the transcription model rather than left for it to guess, because a wrong guess transliterates instead of transcribing. NVIDIA Parakeet accepts arbitrary BCP-47 codes. Mistral Voxtral uses its own set. Empty leaves the model to detect it.",
    reads: "api/routes/voice_routes.py - transcribe()",
    control: {
      kind: "choice",
      options: [
        { value: "", label: "Auto-detect" },
        { value: "en", label: "English" },
        { value: "hi", label: "Hindi (hi)" },
        { value: "hi-IN", label: "Hindi - India (hi-IN)" },
        { value: "es", label: "Spanish (es)" },
        { value: "fr", label: "French (fr)" },
        { value: "de", label: "German (de)" },
        { value: "zh", label: "Chinese (zh)" },
        { value: "ja", label: "Japanese (ja)" },
        { value: "ko", label: "Korean (ko)" },
        { value: "pt", label: "Portuguese (pt)" },
        { value: "ar", label: "Arabic (ar)" },
        { value: "it", label: "Italian (it)" },
        { value: "vi", label: "Vietnamese (vi)" },
      ],
    },
  },
  {
    key: "output_language",
    label: "Language NEXUS speaks in",
    help:
      "Handed to the synthesis model rather than left for it to guess, because a wrong guess synthesises in the wrong language. NVIDIA Magpie TTS supports 12 languages. Mistral Voxtral voice carries language. Empty leaves the model to use its default.",
    reads: "api/routes/voice_routes.py - speak()",
    control: {
      kind: "choice",
      options: [
        { value: "", label: "Provider default" },
        { value: "en-US", label: "English - US (en-US)" },
        { value: "hi-IN", label: "Hindi - India (hi-IN)" },
        { value: "es-US", label: "Spanish - US (es-US)" },
        { value: "fr-FR", label: "French - France (fr-FR)" },
        { value: "de-DE", label: "German - Germany (de-DE)" },
        { value: "zh-CN", label: "Chinese - China (zh-CN)" },
        { value: "ja-JP", label: "Japanese - Japan (ja-JP)" },
        { value: "ko-KR", label: "Korean - Korea (ko-KR)" },
        { value: "pt-BR", label: "Portuguese - Brazil (pt-BR)" },
        { value: "ar-AR", label: "Arabic - Arabic (ar-AR)" },
        { value: "it-IT", label: "Italian - Italy (it-IT)" },
        { value: "vi-VN", label: "Vietnamese - Vietnam (vi-VN)" },
      ],
    },
  },
  {
    key: "tts_voice",
    label: "Voice used for speech",
    help:
      "The provider's voice name, used whenever a caller does not name one. Empty means the provider default (en_paul_neutral for Mistral, Magpie-Multilingual for NVIDIA). VOICE lists what a synthesis actually came back as, so a name that does not exist is visible rather than silently substituted.",
    reads: "api/routes/voice_routes.py - speak()",
    control: { kind: "text", placeholder: "en_paul_neutral", maxLength: 60 },
  },
];

const MEMORY: Row[] = [
  {
    key: "memory_enabled",
    label: "Memory",
    help:
      "The master switch. With this off the permission engine refuses every memory.* capability outright, so NEXUS cannot recall or remember even if it tries.",
    reads: "permissions/engine.py - _disabled_reason(), and ai/context.py",
    control: { kind: "toggle" },
  },
  {
    key: "learning_enabled",
    label: "Continuous learning",
    help:
      "Whether NEXUS observes your corrections, repeated choices, and stated facts to adapt without being retrained. The processor runs on a timer and checks this setting on every tick. Learned records are visible in the Memory panel and can be deleted at any time.",
    reads: "tools/learning.py - LearningProcessor._loop()",
    control: { kind: "toggle" },
  },
  {
    key: "memory_extract_enabled",
    label: "Propose memories from conversation",
    help:
      "Whether NEXUS suggests things worth keeping as you talk. Proposals still need approval before anything is saved - this decides whether it suggests, not whether it saves.",
    reads: "ai/memory_extract.py, ai/memory_sweep.py",
    control: { kind: "toggle" },
  },
  {
    key: "memory_sweep_enabled",
    label: "Also re-read past conversation on a timer",
    help:
      "Picks up exchanges the inline pass missed - a crash, an unreachable model, a turn that arrived by voice. Behind the switch above: this is a second mechanism for the same permission, not a second decision.",
    reads: "ai/memory_sweep.py",
    control: { kind: "toggle" },
  },
  {
    key: "memory_sweep_interval_seconds",
    label: "How often the sweep runs",
    help: "Each pass costs one fast-model call per unexamined exchange, so short intervals cost real money on a busy day.",
    reads: "ai/memory_sweep.py",
    control: { kind: "number", min: 60, max: 86_400, unit: "seconds" },
  },
  {
    key: "memory_recall_limit",
    label: "Memories pulled into one turn",
    help:
      "Small on purpose. The top few relevant memories improve an answer; twenty crowd out the conversation itself and spend tokens the request needs.",
    reads: "ai/context.py",
    control: { kind: "number", min: 1, max: 50, unit: "memories" },
  },
  {
    key: "semantic_search_enabled",
    label: "Search memory by meaning",
    help:
      "What lets recall answer a paraphrase. With it off, or the embedding endpoint unreachable, search says so rather than quietly returning less.",
    reads: "ai/embeddings.py - search()",
    control: { kind: "toggle" },
  },
];

const MACHINE: Row[] = [
  {
    key: "permission_mode",
    label: "When NEXUS needs permission",
    help:
      "Trusted is standing consent to the questions that would have been asked - it changes whether you are asked, and nothing about what is allowed. Every refusal still refuses: a protected path, a destructive command, a capability switched off here, a scope you explicitly denied. Every decision is still written to the audit log, which is what makes trusted reviewable afterwards instead of invisible. Ask is the opposite trade: nothing acts on this machine until you answer.",
    reads: "permissions/engine.py - evaluate(), checked last of all",
    control: {
      kind: "choice",
      options: [
        { value: "trusted", label: "Trusted - act, and log every decision" },
        { value: "ask", label: "Ask - stop and ask each time" },
      ],
    },
  },
  {
    key: "confirm_destructive",
    label: "Still ask before something irreversible",
    help:
      "The one exception to trusted mode. Something judged destructive and irreversible - by consequence, not by wording - is asked about anyway while this is on. Standing consent to opening a browser is not standing consent to deleting a directory. With trusted off this changes nothing, because everything is being asked already.",
    reads: "permissions/engine.py - the trusted branch, via _is_destructive()",
    control: { kind: "toggle" },
  },
  {
    key: "terminal_allowed",
    label: "Allow terminal commands at all",
    help:
      "The hard gate rather than a preference: with it off the permission engine denies terminal.execute before it is ever asked about, so no prompt appears and no command runs - and no permission mode can override that. With it on, a command still passes the hard limits, which refuse a destructive shell line outright.",
    reads: "permissions/engine.py - _disabled_reason()",
    control: { kind: "toggle" },
  },
  {
    key: "agent_max_steps",
    label: "Steps an agent run may take",
    help:
      "The ceiling on one task run. Reached, the run stops and says it reached the ceiling rather than reporting success - an unbounded loop that keeps acting on a machine is the failure this number exists to prevent.",
    reads: "ai/agent.py - run()",
    control: { kind: "number", min: 1, max: 60, unit: "steps" },
  },
  {
    key: "agent_verify_steps",
    label: "Verify each step before moving on",
    help:
      "After a step acts, its result is checked against what the step said it would do, and a step that did not achieve it is marked failed instead of done. Off is faster and lets a run report progress it did not make.",
    reads: "ai/agent.py - _flag('agent_verify_steps')",
    control: { kind: "toggle" },
  },
  {
    key: "agent_pause_on_failure",
    label: "Stop the run at the first failed step",
    help:
      "On, a failed step ends the run with what failed and why, which is the safe default when steps build on each other. Off, the run continues past it - useful for a list of independent steps, and misleading for anything else.",
    reads: "ai/agent.py - _flag('agent_pause_on_failure')",
    control: { kind: "toggle" },
  },
];

const PERSONALITY: Row[] = [
  {
    key: "personality_active_id",
    label: "Active personality",
    help:
      "Which persona NEXUS speaks as. Each has distinct vocabulary, formality, and mannerisms. JARVIS is formal and precise. FRIDAY is casual and efficient. ULTRON is cold and logical. EDITH is tactical and secure. CORTANNA is warm and helpful. Custom lets you build your own.",
    reads: "lib/personality.ts - PERSONALITIES registry, usePersonality hook",
    control: {
      kind: "choice",
      options: [
        { value: "jarvis", label: "JARVIS - Formal, precise, British butler" },
        { value: "friday", label: "FRIDAY - Casual, efficient, modern" },
        { value: "ultron", label: "ULTRON - Cold, logical, superior" },
        { value: "edith", label: "EDITH - Tactical, secure, mission-focused" },
        { value: "cortanna", label: "CORTANNA - Warm, helpful, conversational" },
        { value: "custom", label: "Custom - Build your own" },
      ],
    },
  },
  {
    key: "personality_tts_enabled",
    label: "Speak personality lines aloud",
    help:
      "When on, greetings, status murmurs, time announcements, and proactive suggestions are spoken via TTS. When off, they only appear visually in the history. The proactive toast and ambient alerts respect this setting.",
    reads: "lib/personality.ts - usePersonality hook, speak() function",
    control: { kind: "toggle" },
  },
  {
    key: "personality_murmur_interval_ms",
    label: "Murmur interval",
    help:
      "How often NEXUS speaks a contextual status murmur during idle periods. Shorter intervals feel more present; longer intervals are less intrusive. Minimum 30 seconds. Set to 0 to disable murmurs entirely.",
    reads: "lib/personality.ts - usePersonality murmur timer",
    control: { kind: "number", min: 0, max: 3600000, unit: "milliseconds" },
  },
  {
    key: "personality_time_announce_enabled",
    label: "Announce the time periodically",
    help:
      "When on, NEXUS announces the time at the configured interval. Uses the personality's time announcement vocabulary and markers.",
    reads: "lib/personality.ts - usePersonality timeTimer",
    control: { kind: "toggle" },
  },
  {
    key: "personality_time_announce_interval_ms",
    label: "Time announcement interval",
    help:
      "How often to announce the time. Default is hourly. Minimum 5 minutes.",
    reads: "lib/personality.ts - usePersonality timeTimer",
    control: { kind: "number", min: 300000, max: 86400000, unit: "milliseconds" },
  },
];

const RETENTION: Row[] = [
  {
    key: "retain_activity_days",
    label: "Keep activity for",
    help: "0 means keep forever, and is skipped by the sweep entirely.",
    reads: "db/maintenance.py - retention_sweep()",
    control: { kind: "number", min: 0, max: 3_650, unit: "days" },
  },
  {
    key: "retain_tool_invocations_days",
    label: "Keep tool invocations for",
    help: "The record of what NEXUS actually did to this machine. 0 keeps it forever.",
    reads: "db/maintenance.py - retention_sweep()",
    control: { kind: "number", min: 0, max: 3_650, unit: "days" },
  },
  {
    key: "retain_conversations_days",
    label: "Keep conversations for",
    help:
      "0 keeps them forever, which is the default. A sweep never deletes an archived conversation, nor one a saved memory points at.",
    reads: "db/maintenance.py - retention_sweep()",
    control: { kind: "number", min: 0, max: 3_650, unit: "days" },
  },
];

/**
 * Keys the core stores and nothing reads - with what each one is waiting for.
 *
 * This list is the reason the rest of the screen can be trusted. Every entry was
 * checked by grepping the core for the key name outside `app_settings.py`; each one
 * came back with no reader. They are shown as text, never as controls (§56).
 */
const INERT: Array<{ keys: string[]; waiting: string }> = [
  {
    keys: ["memory_auto_propose", "memory_require_approval"],
    waiting:
      "Nothing: they are duplicates. The code reads memory_extract_enabled, and approval is always required. Changing these would move a switch the core never looks at.",
  },
  {
    keys: ["session_grants_expire_minutes"],
    waiting:
      "A grant expiry clock. Session grants currently last until the core restarts, and nothing times them out.",
  },
  {
    keys: ["sync_enabled", "sync_interval_seconds"],
    waiting: "The Firestore sync layer. SQLite is the source of truth; nothing pushes to a backup yet.",
  },
  {
    keys: ["proactive_suggestions_enabled", "proactive_quiet_hours"],
    waiting: "Proactive suggestions. Screen changes offer to describe what's on screen; quiet hours hold and release later.",
  },
  {
    keys: ["diagnostics_verbose"],
    waiting: "A verbosity switch in the logger. /api/diagnostics already returns its records regardless.",
  },
  {
    keys: ["theme", "reduce_motion", "font_scale"],
    waiting:
      "Appearance beyond the accent. This build renders one dark theme, honours no motion preference of its own, and does not scale type.",
  },
  {
    keys: ["accent"],
    waiting:
      "Nothing - the accent is real, but it lives in this browser's local storage, which is what the shell reads. The database copy is a leftover.",
  },
];

/** Model and awareness keys, named so their absence reads as deliberate. */
const ELSEWHERE: Array<{ label: string; keys: string; where: string }> = [
  {
    label: "Awareness",
    keys: "monitor_*, ambient_*",
    where:
      "AWARENESS. PATCH /api/settings refuses them with a 400 naming the other route, because that one also starts and stops the monitors - writing them here would leave a switch on with nothing watching.",
  },
  {
    label: "Models and routing",
    keys: "main_model, model_*, router_mode, allow_fallback, pinned_*",
    where:
      "MODELS, which can show the chain a pin displaces and call the real resolver to prove it. One key written from two screens is how two screens come to disagree.",
  },
  {
    label: "Personality",
    keys: "personality_*",
    where:
      "PERSONALITY tab. All personality keys are managed here - the custom personality definition is edited via the Personality screen component (not yet built), and the active ID switches the live persona.",
  },
];

export function SettingsScreen() {
  const load = useAsync(() => api.settings.get(), []);
  const [tab, setTab] = useState<TabValue>("behaviour");
  const [draft, setDraft] = useState<Record<string, unknown>>({});

  const settings = load.data?.settings ?? {};
  const defaults = load.data?.defaults ?? {};

  // A fresh read replaces the baseline, so any edit still in the draft would now be
  // compared against different stored values. Dropping it is the honest move: keeping
  // it would let a save write a value the user typed against a state that has changed.
  const seen = useRef<Record<string, unknown> | null>(null);
  useEffect(() => {
    if (load.data && seen.current !== load.data.settings) {
      seen.current = load.data.settings;
      setDraft({});
    }
  }, [load.data]);

  const dirty = Object.keys(draft).filter((key) => !same(draft[key], settings[key]));

  const save = useAction(async () => {
    if (dirty.length === 0) throw new Error("Nothing has been changed.");
    // Only the changed keys. The route rejects the whole body if one key is unknown,
    // so sending everything would fail the day the core drops a key this build knows.
    const body: Record<string, unknown> = {};
    for (const key of dirty) body[key] = draft[key];
    await api.settings.patch(body);
    setDraft({});
    load.reload();
    return `Saved ${dirty.length} setting${dirty.length === 1 ? "" : "s"}.`;
  });

  if (load.state === "loading") {
    return (
      <Panel title="SETTINGS" className="min-h-0">
        <LoadingState what="settings" />
      </Panel>
    );
  }

  if (!load.data) {
    return (
      <Panel title="SETTINGS" className="min-h-0">
        <div className="p-2">
          <ErrorState
            what={load.error ?? "Settings did not load."}
            did="No controls are shown. Rendering defaults would show switches that do not reflect what is stored."
            next={load.offline ? "Start the core, then retry." : load.detail || "Retry."}
            offline={load.offline}
            onRetry={load.reload}
          />
        </div>
      </Panel>
    );
  }

  const value = (key: string): unknown => (key in draft ? draft[key] : settings[key]);
  const change = (key: string, next: unknown) => setDraft((prev) => ({ ...prev, [key]: next }));
  const rows =
    tab === "behaviour"
      ? BEHAVIOUR
      : tab === "voice"
        ? VOICE
        : tab === "memory"
          ? MEMORY
          : tab === "machine"
            ? MACHINE
            : tab === "personality"
              ? PERSONALITY
              : [];

  return (
    <div className="flex h-full min-h-0 flex-col p-2">
      <Panel
        title="SETTINGS"
        meta={dirty.length > 0 ? `${dirty.length} unsaved` : "saved"}
        className="min-h-0"
        bodyClassName="flex min-h-0 flex-1 flex-col"
        actions={
          <Button
            size="sm"
            tone="accent"
            icon="check"
            onClick={save.run}
            busy={save.busy}
            disabled={dirty.length === 0}
            reason="Nothing has been changed."
          >
            Save
          </Button>
        }
      >
        <div className="border-b border-line px-2">
          <Tabs<TabValue> tabs={TABS} value={tab} onChange={setTab} />
        </div>

        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-2">
          <ActionResult done={save.done} failed={save.failed} onClear={save.clear} />

          {rows.length > 0 && (
            <div className="space-y-2">
              {rows.map((row) => (
                <SettingControl
                  key={row.key}
                  row={row}
                  value={value(row.key)}
                  fallback={defaults[row.key]}
                  onChange={(next) => change(row.key, next)}
                  onReset={load.reload}
                />
              ))}
            </div>
          )}

          {tab === "behaviour" && <Appearance />}
          {tab === "machine" && <Elsewhere />}
          {tab === "memory" && <SweepCursor value={settings["memory_sweep_cursor"]} onReset={load.reload} />}
          {tab === "data" && (
            <>
              {RETENTION.map((row) => (
                <SettingControl
                  key={row.key}
                  row={row}
                  value={value(row.key)}
                  fallback={defaults[row.key]}
                  onChange={(next) => change(row.key, next)}
                  onReset={load.reload}
                />
              ))}
              <Storage />
            </>
          )}
          {tab === "gaps" && <Gaps orphans={load.data.orphans} settings={settings} />}
          {tab === "personality" && <PersonalitySettings />}
        </div>
      </Panel>
    </div>
  );
}

/** JSON values, compared without pretending numbers and strings are interchangeable. */
function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

/**
 * One control, with its default and the code that reads it.
 *
 * Reset is a route (`POST /api/settings/{key}/reset`), not a client-side write of a
 * remembered default: it deletes the stored row so the core's own default applies
 * again. Those are different states - a stored value that happens to equal the default
 * still counts as an override the day the default changes.
 */
function SettingControl({
  row,
  value,
  fallback,
  onChange,
  onReset,
}: {
  row: Row;
  value: unknown;
  fallback: unknown;
  onChange: (next: unknown) => void;
  onReset: () => void;
}) {
  const reset = useAction(async () => {
    const result = await api.settings.reset(row.key);
    onReset();
    return `${row.label} is back to ${describe(result.value)}.`;
  });

  const overridden = !same(value, fallback);

  return (
    <div className="chamfer-sm border border-line bg-base px-2.5 py-2">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-xs text-ink">{row.label}</span>
            {overridden && (
              <StatusPill state="pending" size="sm">
                CHANGED FROM DEFAULT
              </StatusPill>
            )}
          </div>
          <p className="mt-1 text-[11px] leading-relaxed text-ink-dim">{row.help}</p>
        </div>
        {row.control.kind === "toggle" && (
          <Toggle
            on={Boolean(value)}
            onChange={(next) => onChange(next)}
            label={row.label}
          />
        )}
      </div>

      {row.control.kind === "choice" && (
        <div className="mt-2">
          {/* The heading above is the visible label, so the accessible one is hidden
              rather than repeated - a select with no label at all is the thing to
              avoid, not a duplicated one. */}
          <label htmlFor={`setting-${row.key}`} className="sr-only">
            {row.label}
          </label>
          <Select<string>
            id={`setting-${row.key}`}
            value={String(value ?? "")}
            onChange={onChange}
            options={row.control.options}
          />
        </div>
      )}

      {row.control.kind === "text" && (
        <div className="mt-2">
          <label htmlFor={`setting-${row.key}`} className="sr-only">
            {row.label}
          </label>
          <TextInput
            id={`setting-${row.key}`}
            value={typeof value === "string" ? value : ""}
            placeholder={row.control.placeholder}
            onChange={(next) => onChange(next.slice(0, textLimit(row)))}
          />
        </div>
      )}

      {row.control.kind === "number" && (
        <NumberControl
          id={`setting-${row.key}`}
          value={value}
          min={row.control.min}
          max={row.control.max}
          unit={row.control.unit}
          onChange={onChange}
        />
      )}

      <div className="mt-2 flex flex-wrap items-center gap-2 border-t border-line/60 pt-1.5">
        <span className="font-mono text-[10px] text-ink-faint">
          default {describe(fallback)} · read by {row.reads}
        </span>
        {overridden && (
          <Button size="sm" tone="ghost" icon="refresh" onClick={reset.run} busy={reset.busy}>
            Reset
          </Button>
        )}
      </div>
      <ActionResult done={reset.done} failed={reset.failed} onClear={reset.clear} />
    </div>
  );
}

/** The row's own ceiling, so a paste cannot write a value longer than the field says. */
function textLimit(row: Row): number {
  return row.control.kind === "text" ? row.control.maxLength : 0;
}

/**
 * A number field that refuses to hand up a value the route would reject.
 *
 * The text is local so a half-typed "1" on the way to "120" is not written as 1. A
 * value outside the range is shown as invalid and *not* written at all, which is why
 * an out-of-range field cannot be saved by pressing Save.
 */
function NumberControl({
  id,
  value,
  min,
  max,
  unit,
  onChange,
}: {
  id: string;
  value: unknown;
  min: number;
  max: number;
  unit: string;
  onChange: (next: number) => void;
}) {
  const [text, setText] = useState(String(typeof value === "number" ? value : (value ?? "")));
  const parsed = Number(text);
  const valid = text.trim() !== "" && Number.isFinite(parsed) && parsed >= min && parsed <= max;

  return (
    <div className="mt-2">
      <div className="flex items-center gap-2">
        <div className="w-32">
          <TextInput
            id={id}
            type="number"
            value={text}
            invalid={!valid}
            onChange={(next) => {
              setText(next);
              const n = Number(next);
              if (next.trim() !== "" && Number.isFinite(n) && n >= min && n <= max) onChange(n);
            }}
          />
        </div>
        <span className="font-mono text-[10px] text-ink-faint">{unit}</span>
      </div>
      {!valid && (
        <p className="mt-1 text-[11px] text-danger">
          Between {min.toLocaleString()} and {max.toLocaleString()}. Until it is, this
          value is not staged for saving.
        </p>
      )}
    </div>
  );
}

/** Values as the core would print them, so "default" is never a guess. */
function describe(value: unknown): string {
  if (value === null || value === undefined) return "nothing";
  if (typeof value === "boolean") return value ? "on" : "off";
  if (value === "") return "empty";
  return String(value);
}

/**
 * The accent, which is real, and where it lives.
 *
 * `useAccent` writes `nexus.accent` in this browser's local storage and sets
 * `data-accent` on `<html>`, which is where the stylesheet reads the palette. That is
 * the whole mechanism - so this control changes something immediately, and it does not
 * travel to another machine. Saying so is better than a control that looks synced.
 */
function Appearance() {
  const { accent, setAccent } = useAccent();
  return (
    <Section title="Accent" meta="this browser only">
      <div className="flex flex-wrap items-center gap-2 px-3 py-2">
        {ACCENTS.map((option) => (
          <button
            key={option}
            type="button"
            onClick={() => setAccent(option)}
            aria-pressed={option === accent}
            className={`chamfer-sm border px-2 py-1 font-mono text-[10px] uppercase tracking-wider ${
              option === accent
                ? "border-accent bg-accent/15 text-accent"
                : "border-line text-ink-dim hover:border-line-bright hover:text-ink-soft"
            }`}
          >
            {option}
          </button>
        ))}
      </div>
      <p className="px-3 pb-2 text-[11px] leading-relaxed text-ink-dim">
        Applied the moment you press it, and kept in this browser rather than in the
        database - the core has an `accent` setting, but nothing reads it.
      </p>
    </Section>
  );
}

/** The keys this screen deliberately does not own. */
function Elsewhere() {
  return (
    <Section title="Settings that live on another screen">
      <div className="flex flex-col">
        {ELSEWHERE.map((entry) => (
          <div key={entry.label} className="border-b border-line/60 px-3 py-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs text-ink">{entry.label}</span>
              <span className="font-mono text-[10px] text-ink-faint">{entry.keys}</span>
            </div>
            <p className="mt-1 text-[11px] leading-relaxed text-ink-dim">{entry.where}</p>
          </div>
        ))}
      </div>
    </Section>
  );
}

/**
 * The sweep's watermark, which the user does not type.
 *
 * It is `"<created_at>|<id>"` of the last exchange the memory sweep examined, written
 * by the sweep. The only sensible action is to clear it, and the consequence is worth
 * stating before the fact: NEXUS re-reads the entire history at one model call per
 * exchange, and proposes nothing it already has.
 */
function SweepCursor({ value, onReset }: { value: unknown; onReset: () => void }) {
  const [confirm, setConfirm] = useState(false);
  const clear = useAction(async () => {
    await api.settings.reset("memory_sweep_cursor");
    setConfirm(false);
    onReset();
    return "Cleared. The next sweep starts from the beginning of the history.";
  });

  const cursor = typeof value === "string" ? value : "";

  return (
    <Section title="Where the sweep has read up to">
      <div className="flex flex-col">
        <DataRow
          label="Watermark"
          value={cursor || "nothing examined yet"}
          state={cursor ? "idle" : "pending"}
          title="The sweep writes this itself: the created_at and id of the last exchange it looked at."
        />
      </div>
      <div className="px-3 py-2">
        <Button size="sm" tone="ghost" icon="refresh" onClick={() => setConfirm(true)} disabled={!cursor} reason="There is no watermark to clear.">
          Make it re-read everything
        </Button>
      </div>
      <ActionResult done={clear.done} failed={clear.failed} onClear={clear.clear} />
      <ConfirmDialog
        open={confirm}
        title="Clear the sweep's watermark"
        consequence="The next sweep re-examines every exchange in the history, which costs one fast-model call per exchange. It proposes nothing it has already proposed, so the cost is time and quota rather than duplicates."
        confirmLabel="Clear it"
        onConfirm={clear.run}
        onCancel={() => setConfirm(false)}
        busy={clear.busy}
        failed={clear.failed}
      />
    </Section>
  );
}

/**
 * What is on disk (§66, §70, §71).
 *
 * Four real operations, and the order matters. A retention sweep deletes rows, so the
 * dry run is the one that happens by default here as well as on the route: the numbers
 * shown in the confirmation are measured, not estimated. `integrity_ok` is a real
 * `PRAGMA integrity_check`, which is why it is shown even when it passes - "the query
 * worked" is not the same claim.
 */
function Storage() {
  const stats = useAsync(() => api.storage.stats(), []);
  const [plan, setPlan] = useState<RetentionReport | null>(null);
  const [confirmSweep, setConfirmSweep] = useState(false);

  const preview = useAction(async () => {
    const report = await api.storage.retentionSweep(true);
    setPlan(report);
    const total = Object.values(report.removed).reduce((sum, n) => sum + n, 0);
    return total === 0
      ? "Nothing is old enough to delete under the current retention settings."
      : `${total} rows would be deleted. Nothing was.`;
  });

  const sweep = useAction(async () => {
    const report = await api.storage.retentionSweep(false);
    setPlan(report);
    setConfirmSweep(false);
    stats.reload();
    const total = Object.values(report.removed).reduce((sum, n) => sum + n, 0);
    return `${total} row${total === 1 ? "" : "s"} deleted.`;
  });

  const vacuum = useAction(async () => {
    const report = await api.storage.vacuum();
    stats.reload();
    return report.reclaimed > 0
      ? `${(report.reclaimed / 1_000_000).toFixed(1)} MB reclaimed.`
      : "Nothing to reclaim - the file was already compact.";
  });

  const exportAll = useAction(async () => {
    const payload = await api.storage.export();
    const total = Object.values(payload.counts).reduce((sum, n) => sum + n, 0);
    // The browser saves it. Where it lands is the user's choice, not ours (§70).
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `nexus-export-${new Date().toISOString().slice(0, 10)}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
    return `${total.toLocaleString()} rows exported. Credentials are excluded: ${payload.excluded.join(", ") || "nothing"}.`;
  });

  const planned = plan ? Object.values(plan.removed).reduce((sum, n) => sum + n, 0) : null;

  return (
    <Section
      title="The database"
      meta={stats.data ? `${(stats.data.bytes / 1_000_000).toFixed(1)} MB` : undefined}
    >
      {stats.state === "loading" ? (
        <LoadingState what="the database" />
      ) : !stats.data ? (
        <div className="p-2">
          <ErrorState
            what={stats.error ?? "The storage report did not load."}
            did="No sizes or counts are shown."
            next={stats.offline ? "Start the core, then retry." : stats.detail || "Retry."}
            offline={stats.offline}
            onRetry={stats.reload}
            compact
          />
        </div>
      ) : (
        <>
          <div className="flex flex-col">
            <DataRow label="File" value={stats.data.path} />
            <DataRow label="Size" value={`${(stats.data.bytes / 1_000_000).toFixed(1)} MB`} />
            <DataRow
              label="Schema version"
              value={stats.data.schema_version === null ? "not reported" : String(stats.data.schema_version)}
            />
            <DataRow
              label="Integrity check"
              value={stats.data.integrity_ok ? "passed" : stats.data.integrity_detail || "failed"}
              state={stats.data.integrity_ok ? "ok" : "failed"}
              title="A real PRAGMA integrity_check, not an inference from a successful query."
            />
          </div>

          <div className="border-t border-line/60">
            {Object.entries(stats.data.counts)
              .filter(([, count]) => count > 0)
              .sort((a, b) => b[1] - a[1])
              .map(([table, count]) => (
                <DataRow key={table} label={table} value={count.toLocaleString()} />
              ))}
          </div>
        </>
      )}

      <div className="flex flex-wrap items-center gap-2 border-t border-line/60 px-3 py-2">
        <Button size="sm" tone="ghost" icon="external" onClick={exportAll.run} busy={exportAll.busy}>
          Export everything
        </Button>
        <Button size="sm" tone="ghost" icon="search" onClick={preview.run} busy={preview.busy}>
          What would retention delete?
        </Button>
        <Button
          size="sm"
          tone="danger"
          icon="trash"
          onClick={() => setConfirmSweep(true)}
          disabled={planned === null || planned === 0}
          reason={planned === null ? "Check what would be deleted first." : "Nothing is old enough to delete."}
        >
          Run the sweep
        </Button>
        <Button size="sm" tone="ghost" icon="database" onClick={vacuum.run} busy={vacuum.busy}>
          Compact the file
        </Button>
      </div>

      {plan && (
        <div className="border-t border-line/60 px-3 py-2">
          <span className="label">{plan.dry_run ? "What a sweep would remove" : "What the sweep removed"}</span>
          <div className="mt-1 flex flex-col">
            {Object.entries(plan.removed).map(([table, count]) => (
              <DataRow
                key={table}
                label={table}
                value={`${count} row${count === 1 ? "" : "s"}`}
                state={count > 0 ? "pending" : "idle"}
                title={`Retention for ${table}: ${plan.plan[table] ?? 0} days. 0 means keep forever, and is skipped.`}
              />
            ))}
          </div>
          <p className="mt-1 font-mono text-[10px] text-ink-faint">
            measured at {clockTime(new Date().toISOString())} · 0 days means keep forever
          </p>
        </div>
      )}

      <ActionResult done={exportAll.done} failed={exportAll.failed} onClear={exportAll.clear} />
      <ActionResult done={preview.done} failed={preview.failed} onClear={preview.clear} />
      <ActionResult done={sweep.done} failed={sweep.failed} onClear={sweep.clear} />
      <ActionResult done={vacuum.done} failed={vacuum.failed} onClear={vacuum.clear} />

      <ConfirmDialog
        open={confirmSweep}
        title="Delete everything past its retention window"
        consequence={`${planned ?? 0} rows go, permanently, in the tables listed above. Archived conversations and any conversation a saved memory points at are never touched. This cannot be undone.`}
        confirmLabel="Delete them"
        onConfirm={sweep.run}
        onCancel={() => setConfirmSweep(false)}
        busy={sweep.busy}
        failed={sweep.failed}
      />
    </Section>
  );
}

/**
 * The honest inventory: stored keys nothing reads, keys nothing recognises, and the
 * §63 screens that do not exist.
 *
 * All three are here for the same reason. A settings screen that showed only working
 * controls would be truthful but incomplete - the user would have no way to tell
 * "NEXUS cannot do this yet" from "NEXUS can do this and I have not found the switch".
 * Naming the gap answers that.
 */
function Gaps({
  orphans,
  settings,
}: {
  orphans: string[];
  settings: Record<string, unknown>;
}) {
  return (
    <>
      <Section title="Stored, but nothing reads it yet" meta={`${INERT.reduce((n, e) => n + e.keys.length, 0)} keys`}>
        <p className="border-b border-line/60 px-3 py-1.5 text-[11px] leading-relaxed text-ink-dim">
          Each of these has a row in the settings table and a default, and no code path
          in this build consults it. They are listed rather than shown as switches: a
          switch that saves and changes nothing is a lie the user cannot detect (§56).
        </p>
        {INERT.map((entry) => (
          <div key={entry.keys.join(",")} className="border-b border-line/60 px-3 py-2">
            <div className="flex flex-wrap items-center gap-1.5">
              {entry.keys.map((key) => (
                <span
                  key={key}
                  className="chamfer-sm border border-line bg-raised px-1.5 py-0.5 font-mono text-[10px] text-ink-soft"
                  title={`currently ${describe(settings[key])}`}
                >
                  {key}
                </span>
              ))}
            </div>
            <p className="mt-1 text-[11px] leading-relaxed text-ink-dim">
              <span className="text-ink-faint">Waiting on: </span>
              {entry.waiting}
            </p>
          </div>
        ))}
      </Section>

      <Section title="Screens §63 names that do not exist" meta={`${PENDING_SCREENS.length}`}>
        <p className="border-b border-line/60 px-3 py-1.5 text-[11px] leading-relaxed text-ink-dim">
          These are absent from the navigation on purpose. A named screen that opens a
          blank panel reads as broken software; an absent name reads as software that has
          not been built yet, which is the true one.
        </p>
        {PENDING_SCREENS.map((screen) => (
          <div key={screen.label} className="border-b border-line/60 px-3 py-2">
            <span className="font-mono text-[10px] uppercase tracking-wider text-ink-soft">
              {screen.label}
            </span>
            <p className="mt-1 text-[11px] leading-relaxed text-ink-dim">{screen.needs}</p>
          </div>
        ))}
      </Section>

      <Section title="Orphaned keys" meta={`${orphans.length}`}>
        {orphans.length === 0 ? (
          <p className="px-3 py-2 text-[11px] leading-relaxed text-ink-soft">
            None. Every stored key still has a default behind it, which means nothing is
            left over from a removed feature.
          </p>
        ) : (
          <>
            <p className="border-b border-line/60 px-3 py-1.5 text-[11px] leading-relaxed text-warn">
              Stored with no default behind them - leftovers from a removed or renamed
              feature. Nothing reads them, and this route cannot write them either: a
              PATCH naming one is rejected with a 400.
            </p>
            {orphans.map((key) => (
              <DataRow key={key} label={key} value={describe(settings[key])} state="pending" />
            ))}
          </>
        )}
      </Section>
    </>
  );
}

/**
 * Personality configuration panel.
 *
 * Shows the active personality, lets the user switch, and displays the current
 * configuration. The custom personality editor is not yet built - selecting
 * "custom" activates the built-in custom preset which can be configured via
 * the raw JSON setting.
 */
function PersonalitySettings() {
  const { accent } = useAccent();
  return (
    <Section title="Personality Settings" meta="how NEXUS speaks and behaves">
      <div className="space-y-4 p-3">
        <div className="chamfer border border-line bg-base p-3">
          <div className="flex items-center gap-3 mb-3">
            <div
              className="w-8 h-8 rounded-full flex items-center justify-center font-mono text-[10px] uppercase tracking-wider text-ink"
              style={{ background: `var(--accent)`, color: "var(--accent-ink)" }}
            >
              AI
            </div>
            <div>
              <p className="label">NEXUS Personality</p>
              <p className="font-mono text-[10px] text-ink-dim">
                Choose how NEXUS presents itself - vocabulary, formality, verbosity, and tone
              </p>
            </div>
          </div>

          <div className="space-y-2 text-[11px] leading-relaxed text-ink-dim">
            <p>
              <span className="text-ink-faint">Active:</span> The personality setting controls every spoken
              line, proactive suggestion framing, ambient murmur, and visual indicator style.
              Switching takes effect immediately - greetings, status reports, and time
              announcements all use the new voice.
            </p>
            <p>
              <span className="text-ink-faint">Murmurs:</span> Periodic status updates during idle time
              ("Systems nominal", "Monitoring"). Interval configurable above. Disabled during
              high-load or focus contexts.
            </p>
            <p>
              <span className="text-ink-faint">Time announcements:</span> Periodic time announcements
              ("The time is 3:45 PM"). Optional, off by default.
            </p>
            <p>
              <span className="text-ink-faint">TTS:</span> When enabled, all personality lines are spoken
              aloud via the configured TTS voice. When disabled, they appear only in the visual
              history and proactive toasts.
            </p>
          </div>
        </div>

        <div className="chamfer border border-line bg-base p-3">
          <p className="label mb-2">Personality Comparison</p>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {[
              { id: "jarvis", name: "JARVIS", style: "Formal, precise, British butler", color: "#22d3ee" },
              { id: "friday", name: "FRIDAY", style: "Casual, efficient, modern", color: "#38bdf8" },
              { id: "ultron", name: "ULTRON", style: "Cold, logical, superior", color: "#ff3b4e" },
              { id: "edith", name: "EDITH", style: "Tactical, secure, mission-focused", color: "#10b981" },
              { id: "cortanna", name: "CORTANNA", style: "Warm, helpful, conversational", color: "#a78bfa" },
            ].map((p) => (
              <button
                key={p.id}
                type="button"
                className={[
                  "chamfer-sm border p-3 text-left transition-colors",
                  "border-line-bright hover:border-accent/40 hover:bg-raised",
                ].join(" ")}
                style={{ borderLeft: `3px solid ${p.color}` }}
              >
                <div className="flex items-center gap-2 mb-1">
                  <div className="w-2.5 h-2.5 rounded-full" style={{ background: p.color }} />
                  <span className="label">{p.name}</span>
                </div>
                <p className="font-mono text-[10px] text-ink-dim">{p.style}</p>
              </button>
            ))}
            <button
              type="button"
              className={[
                "chamfer-sm border p-3 text-left transition-colors",
                "border-line-bright hover:border-accent/40 hover:bg-raised",
              ].join(" ")}
              style={{ borderLeft: `3px solid ${accent}` }}
            >
              <div className="flex items-center gap-2 mb-1">
                <div className="w-2.5 h-2.5 rounded-full bg-gradient-to-r from-cyan-400 to-violet-400" />
                <span className="label">CUSTOM</span>
              </div>
              <p className="font-mono text-[10px] text-ink-dim">Build your own vocabulary, markers, and style</p>
            </button>
          </div>
        </div>

        <div className="chamfer border border-line bg-base p-3">
          <p className="label mb-2">Visual Indicator Preview</p>
          <p className="font-mono text-[10px] text-ink-dim mb-3">
            The listening indicator style varies by personality:
          </p>
          <div className="flex flex-wrap items-center gap-4">
            <div className="flex items-center gap-2">
              <span className="font-mono text-[10px] text-ink-dim">Pulse:</span>
              <div className="w-8 h-8 rounded-full animate-pulse" style={{ background: "var(--accent)", boxShadow: "0 0 12px var(--accent)" }} />
            </div>
            <div className="flex items-center gap-2">
              <span className="font-mono text-[10px] text-ink-dim">Wave:</span>
              <div className="flex gap-1" style={{ color: "var(--accent)" }}>
                {[0, 1, 2, 3].map((i) => (
                  <div
                    key={i}
                    className="w-1.5 h-8 rounded-full animate-pulse"
                    style={{ animationDelay: `${i * 100}ms`, opacity: 0.6 }}
                  />
                ))}
              </div>
            </div>
            <div className="flex items-center gap-2">
              <span className="font-mono text-[10px] text-ink-dim">Orb:</span>
              <div className="w-8 h-8 rounded-full animate-orb-float" style={{
                background: "radial-gradient(circle at 30% 30%, var(--accent)DD, var(--accent)44 70%)",
                boxShadow: "0 0 24px var(--accent)88, inset 0 -4px 8px var(--accent)44",
              }} />
            </div>
            <div className="flex items-center gap-2">
              <span className="font-mono text-[10px] text-ink-dim">Bars:</span>
              <div className="flex items-end gap-1 h-8" style={{ color: "var(--accent)" }}>
                {[0, 1, 2, 3, 4].map((i) => (
                  <div
                    key={i}
                    className="w-1.5 rounded-full animate-bar-dance"
                    style={{
                      height: `${20 + i * 16}%`,
                      animationDelay: `${i * 100}ms`,
                    }}
                  />
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>
    </Section>
  );
}
