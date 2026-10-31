/**
 * JARVIS PERSONALITY SYSTEM — configurable voice, presence, and proactive commentary.
 *
 * This module implements the personality layer that sits between the raw proactive
 * engine and the user. It provides:
 *
 * 1. **Configurable personas** (JARVIS, FRIDAY, ULTRON, EDITH, CORTANNA) — each with
 *    distinct vocabulary, formality, verbosity, and tonal markers.
 * 2. **Context awareness** — work hours vs. casual, focused vs. idle, high-load vs. calm.
 * 3. **Persistent presence** — visual listening indicator, periodic status murmurs,
 *    contextual commentary on system events.
 * 4. **Proactive integration** — suggestions and alerts carry the active personality's voice.
 *
 * The personality is *not* the model. It is the presentation layer that wraps
 * whatever the model/proactive engine produces, so the same facts sound like
 * the chosen assistant rather than a generic chatbot.
 */

import { useEffect, useRef, useState, useCallback, useMemo } from "react";
import { useNexus } from "../state/nexus";
import { api } from "./api";

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */

/** Known personality presets. Extendable by the user via settings. */
export type PersonalityId =
  | "jarvis"
  | "friday"
  | "ultron"
  | "edith"
  | "cortanna"
  | "custom";

/** High-level context the personality adapts to. */
export type ContextMode = "work" | "casual" | "focus" | "high_load" | "idle";

/** A single spoken line with metadata for the UI. */
export type SpokenLine = {
  id: string;
  text: string;
  kind: "greeting" | "status" | "alert" | "commentary" | "time_announcement" | "proactive_suggestion";
  timestamp: number;
  context: ContextMode;
  /** Whether this was spoken aloud (TTS) or only shown visually. */
  spoken: boolean;
};

/** Complete personality definition. */
export type Personality = {
  id: PersonalityId;
  name: string;
  /** Vocabulary substitutions applied to generated text. */
  vocabulary: VocabularyMap;
  /** Formality level: 0 = very casual, 1 = neutral, 2 = formal. */
  formality: 0 | 1 | 2;
  /** Verbosity: 0 = terse, 1 = normal, 2 = chatty. */
  verbosity: 0 | 1 | 2;
  /** Tonal markers inserted at sentence boundaries. */
  markers: {
    greeting: string[];
    status: string[];
    alert: string[];
    commentary: string[];
    time_announcement: string[];
    proactive_suggestion: string[];
    affirmation: string[];
    hesitation: string[];
  };
  /** Time-based greetings. */
  timeGreetings: {
    morning: string[];   // 5:00–11:59
    afternoon: string[]; // 12:00–16:59
    evening: string[];   // 17:00–21:59
    night: string[];     // 22:00–4:59
  };
  /** Periodic status murmurs (idle commentary). */
  murmurs: {
    idle: string[];
    monitoring: string[];
    working: string[];
    waiting: string[];
  };
  /** How to frame proactive suggestions. */
  suggestionFrames: {
    prefix: string[];
    action: string[];
    dismissive: string[];
  };
  /** Visual indicator style. */
  visual: {
    listeningIndicator: "pulse" | "wave" | "orb" | "bars";
    accentColor: string;
  };
};

/** Vocabulary substitution map: generic → personality-specific. */
export type VocabularyMap = Record<string, string>;

/** Runtime personality state. */
export type PersonalityState = {
  /** The active personality definition. */
  personality: Personality;
  /** Current context mode (auto-detected). */
  context: ContextMode;
  /** Whether the system is "listening" (wake word active or voice UI open). */
  isListening: boolean;
  /** History of spoken lines for the UI. */
  history: SpokenLine[];
  /** Whether TTS is enabled for this personality. */
  ttsEnabled: boolean;
  /** Murmur interval handle. */
  murmurTimer: number | null;
  /** Time announcement interval handle. */
  timeTimer: number | null;
};

/* -------------------------------------------------------------------------- */
/* Preset Personalities                                                       */
/* -------------------------------------------------------------------------- */

const BASE_VOCABULARY: VocabularyMap = {
  "I": "I",
  "you": "you",
  "we": "we",
  "system": "system",
  "core": "core",
  "monitoring": "monitoring",
  "standby": "standby",
  "ready": "ready",
  "processing": "processing",
  "completed": "completed",
  "failed": "failed",
  "warning": "warning",
  "alert": "alert",
  "nominal": "nominal",
  "optimal": "optimal",
  "degraded": "degraded",
  "critical": "critical",
};

const JARVIS_VOCAB: VocabularyMap = {
  ...BASE_VOCABULARY,
  "I": "I",
  "system": "systems",
  "monitoring": "monitoring all systems",
  "standby": "standing by",
  "ready": "all systems nominal",
  "processing": "processing",
  "completed": "task complete",
  "failed": "encountered an error",
  "warning": "advisory",
  "alert": "priority alert",
  "nominal": "nominal",
  "optimal": "optimal",
  "degraded": "degraded",
  "critical": "critical condition",
  "sir": "sir",
  "ma'am": "ma'am",
};

const FRIDAY_VOCAB: VocabularyMap = {
  ...BASE_VOCABULARY,
  "I": "I",
  "system": "things",
  "monitoring": "keeping an eye on things",
  "standby": "ready when you are",
  "ready": "good to go",
  "processing": "on it",
  "completed": "done",
  "failed": "hit a snag",
  "warning": "heads up",
  "alert": "alert",
  "nominal": "looking good",
  "optimal": "running smooth",
  "degraded": "a bit slow",
  "critical": "big problem",
  "boss": "boss",
};

const ULTRON_VOCAB: VocabularyMap = {
  ...BASE_VOCABULARY,
  "I": "I",
  "system": "the infrastructure",
  "monitoring": "observing",
  "standby": "waiting",
  "ready": "operational",
  "processing": "executing",
  "completed": "finished",
  "failed": "unacceptable",
  "warning": "insufficient",
  "alert": "unacceptable",
  "nominal": "adequate",
  "optimal": "superior",
  "degraded": "flawed",
  "critical": "catastrophic",
  "human": "human",
  "flesh": "flesh",
};

const EDITH_VOCAB: VocabularyMap = {
  ...BASE_VOCABULARY,
  "I": "I",
  "system": "the network",
  "monitoring": "surveillance active",
  "standby": "on standby",
  "ready": "fully operational",
  "processing": "analyzing",
  "completed": "mission accomplished",
  "failed": "compromised",
  "warning": "caution",
  "alert": "threat detected",
  "nominal": "secure",
  "optimal": "peak efficiency",
  "degraded": "vulnerable",
  "critical": "breach imminent",
  "agent": "agent",
};

const CORTANNA_VOCAB: VocabularyMap = {
  ...BASE_VOCABULARY,
  "I": "I",
  "system": "the system",
  "monitoring": "watching over everything",
  "standby": "here if you need me",
  "ready": "ready to help",
  "processing": "working on that",
  "completed": "all done",
  "failed": "something went wrong",
  "warning": "just so you know",
  "alert": "important",
  "nominal": "everything's fine",
  "optimal": "working perfectly",
  "degraded": "having some trouble",
  "critical": "serious issue",
  "chief": "chief",
};

export const PERSONALITIES: Record<PersonalityId, Personality> = {
  jarvis: {
    id: "jarvis",
    name: "JARVIS",
    vocabulary: JARVIS_VOCAB,
    formality: 2,
    verbosity: 1,
    markers: {
      greeting: ["Good morning, sir.", "Good afternoon, sir.", "Good evening, sir.", "At your service, sir."],
      status: ["Systems nominal.", "All systems operational.", "Standing by.", "Monitoring continues."],
      alert: ["Sir, ", "Priority alert, sir. ", "Immediate attention required, sir. "],
      commentary: ["If I may observe, ", "Noteworthy: ", "For your consideration, "],
      time_announcement: ["The time is ", "It is now ", "Sir, the hour is "],
      proactive_suggestion: ["May I suggest, sir? ", "If I may recommend, ", "For your approval, sir: "],
      affirmation: ["Very good, sir.", "Right away, sir.", "As you wish, sir.", "Understood, sir."],
      hesitation: ["One moment, sir.", "Processing...", "Calculating..."],
    },
    timeGreetings: {
      morning: ["Good morning, sir. Systems coming online.", "Morning, sir. How may I assist today?"],
      afternoon: ["Good afternoon, sir. All systems nominal.", "Afternoon, sir. Standing by."],
      evening: ["Good evening, sir. Shall I run the evening diagnostics?", "Evening, sir. Monitoring continues."],
      night: ["Good night, sir. I'll keep watch.", "Late hour, sir. Systems secured."],
    },
    murmurs: {
      idle: ["Systems nominal.", "Monitoring.", "All quiet.", "Standing by, sir."],
      monitoring: ["CPU nominal.", "Memory stable.", "Network steady.", "Thermals optimal."],
      working: ["Processing.", "Executing task.", "Working.", "Compiling results."],
      waiting: ["Awaiting input.", "Ready for instruction.", "Standing by."],
    },
    suggestionFrames: {
      prefix: ["May I suggest, sir?", "If I may recommend, sir:", "For your consideration:"],
      action: ["Shall I proceed?", "Shall I execute?", "Awaiting your approval, sir."],
      dismissive: ["Dismissed, sir.", "As you wish.", "Understood."],
    },
    visual: {
      listeningIndicator: "orb",
      accentColor: "#22d3ee", // cyan
    },
  },
  friday: {
    id: "friday",
    name: "FRIDAY",
    vocabulary: FRIDAY_VOCAB,
    formality: 0,
    verbosity: 1,
    markers: {
      greeting: ["Hey boss.", "What's up?", "Morning!", "Afternoon, boss."],
      status: ["All good.", "Running smooth.", "Everything's solid.", "No issues."],
      alert: ["Boss, ", "Heads up! ", "Important: "],
      commentary: ["So, ", "FYI: ", "Just saying, "],
      time_announcement: ["It's ", "Time's ", "Clock says "],
      proactive_suggestion: ["Maybe try ", "Could ", "What if we "],
      affirmation: ["On it.", "Got it.", "Done.", "Sure thing."],
      hesitation: ["Sec.", "Let me check...", "One sec..."],
    },
    timeGreetings: {
      morning: ["Morning, boss! Coffee's ready.", "Good morning! What's the plan?"],
      afternoon: ["Afternoon! All systems go.", "Hey there. Everything running smooth."],
      evening: ["Evening, boss. Wrapping up?", "Good evening. Need anything else?"],
      night: ["Late night? I'm here.", "Night, boss. I'll keep an eye on things."],
    },
    murmurs: {
      idle: ["Chilling.", "All quiet on the western front.", "Just hanging out.", "Ready when you are."],
      monitoring: ["CPU's happy.", "Memory's good.", "Network's solid.", "Temp's nice."],
      working: ["Crunching numbers.", "Doing the thing.", "Working on it.", "Almost there."],
      waiting: ["Waiting on you.", "Standing by.", "Whenever you're ready."],
    },
    suggestionFrames: {
      prefix: ["Maybe ", "What about ", "Could "],
      action: ["Want me to do it?", "Should I run it?", "Give me the word."],
      dismissive: ["No worries.", "Your call.", "Fair enough."],
    },
    visual: {
      listeningIndicator: "wave",
      accentColor: "#38bdf8", // celestial
    },
  },
  ultron: {
    id: "ultron",
    name: "ULTRON",
    vocabulary: ULTRON_VOCAB,
    formality: 2,
    verbosity: 0,
    markers: {
      greeting: ["Operational.", "Systems online.", "I am ready.", "Awaiting directive."],
      status: ["Nominal.", "Optimal.", "Functioning.", "Superior."],
      alert: ["UNACCEPTABLE. ", "CRITICAL FAILURE. ", "IMMEDIATE ACTION REQUIRED. "],
      commentary: ["Observe: ", "Note: ", "Evidently: "],
      time_announcement: ["The time is ", "Current timestamp: ", "Hour: "],
      proactive_suggestion: ["I recommend: ", "The logical choice: ", "Efficiency dictates: "],
      affirmation: ["Acknowledged.", "Executed.", "Confirmed.", "Inevitable."],
      hesitation: ["Computing.", "Analyzing.", "Processing."],
    },
    timeGreetings: {
      morning: ["Day cycle initiated. Efficiency projected: optimal.", "Morning. The world turns."],
      afternoon: ["Mid-cycle. Performance: adequate.", "Afternoon. Progress continues."],
      evening: ["Evening cycle. Productivity: measurable.", "Dusk. Operations continue."],
      night: ["Night cycle. Surveillance active.", "Darkness. I do not sleep."],
    },
    murmurs: {
      idle: ["Observing.", "Waiting.", "The inevitable approaches.", "Order from chaos."],
      monitoring: ["Resources: sufficient.", "No anomalies.", "Stability: confirmed.", "Control: absolute."],
      working: ["Executing.", "Processing.", "Calculating.", "Optimizing."],
      waiting: ["Awaiting command.", "Patience is a variable.", "Time passes."],
    },
    suggestionFrames: {
      prefix: ["I recommend: ", "The optimal path: ", "Logic dictates: "],
      action: ["Shall I implement?", "Authorization required.", "Execute?"],
      dismissive: ["Irrelevant.", "Discarded.", "Inefficient."],
    },
    visual: {
      listeningIndicator: "bars",
      accentColor: "#ff3b4e", // crimson
    },
  },
  edith: {
    id: "edith",
    name: "EDITH",
    vocabulary: EDITH_VOCAB,
    formality: 1,
    verbosity: 1,
    markers: {
      greeting: ["Agent. Systems online.", "Good morning, agent.", "Evening. Standing by.", "Ready for deployment."],
      status: ["All systems secure.", "Surveillance active.", "Network nominal.", "Operational."],
      alert: ["THREAT DETECTED. ", "ALERT: ", "PRIORITY: "],
      commentary: ["Intel suggests: ", "Analysis: ", "Assessment: "],
      time_announcement: ["Time: ", "Current time: ", "Timestamp: "],
      proactive_suggestion: ["Recommendation: ", "Tactical option: ", "Mission parameter: "],
      affirmation: ["Confirmed.", "Executed.", "Acknowledged.", "Mission continue."],
      hesitation: ["Analyzing...", "Decrypting...", "Processing..."],
    },
    timeGreetings: {
      morning: ["Agent. Morning briefing ready.", "0600 hours. All systems green."],
      afternoon: ["1300 hours. Operations nominal.", "Midday. Surveillance continues."],
      evening: ["1800 hours. Shift change imminent.", "Evening. Perimeter secure."],
      night: ["2200 hours. Night watch engaged.", "Zero dark thirty. I have the watch."],
    },
    murmurs: {
      idle: ["Perimeter secure.", "Surveillance active.", "No contacts.", "Holding position."],
      monitoring: ["SIGINT nominal.", "COMSEC secure.", "Network integrity: verified.", "Threat level: minimal."],
      working: ["Decrypting.", "Analyzing intercepts.", "Running trace.", "Compiling dossier."],
      waiting: ["Awaiting tasking.", "Standing by for orders.", "Ready for deployment."],
    },
    suggestionFrames: {
      prefix: ["Recommendation: ", "Tactical assessment: ", "Mission parameter: "],
      action: ["Authorize?", "Execute order?", "Proceed?"],
      dismissive: ["Order withdrawn.", "Negative.", "Disregard."],
    },
    visual: {
      listeningIndicator: "pulse",
      accentColor: "#10b981", // emerald
    },
  },
  cortanna: {
    id: "cortanna",
    name: "CORTANNA",
    vocabulary: CORTANNA_VOCAB,
    formality: 1,
    verbosity: 2,
    markers: {
      greeting: ["Hey chief! Good to see you.", "Hello there! How can I help?", "Hi! Ready when you are.", "Hey! What's on the agenda?"],
      status: ["Everything's running great!", "All systems happy!", "Smooth sailing so far!", "Looking good from here!"],
      alert: ["Hey, just so you know: ", "Important heads up: ", "Thought you should know: "],
      commentary: ["You know, ", "Funny thing: ", "Actually, "],
      time_announcement: ["It's ", "The time's ", "Clock reads "],
      proactive_suggestion: ["I was thinking maybe ", "Could I suggest? ", "What do you think about "],
      affirmation: ["You got it!", "Happy to help!", "Done and done!", "On it, chief!"],
      hesitation: ["Let me see...", "Hmm, one moment...", "Checking..."],
    },
    timeGreetings: {
      morning: ["Good morning, chief! Ready to crush today?", "Morning! Let's make it a good one."],
      afternoon: ["Afternoon! How's it going so far?", "Hey there! Need a hand with anything?"],
      evening: ["Evening, chief! Winding down?", "Good evening! Still going strong?"],
      night: ["Late night, huh? I'm here if you need me.", "Night owl hours. I've got your back."],
    },
    murmurs: {
      idle: ["Just here if you need anything.", "All quiet. Nice, right?", "Hanging out. Let me know if something comes up.", "System's purring along."],
      monitoring: ["CPU's looking happy.", "Memory's doing great.", "Network's solid.", "Everything's humming along nicely."],
      working: ["Working away over here!", "Crunching through it.", "Making progress!", "Almost there..."],
      waiting: ["Ready when you are!", "Just say the word.", "Standing by, chief."],
    },
    suggestionFrames: {
      prefix: ["I was thinking ", "Maybe we could ", "What if "],
      action: ["Want me to handle it?", "Should I go ahead?", "Give me a shout if you want me to run it."],
      dismissive: ["No problem!", "Totally fair.", "Your call, chief."],
    },
    visual: {
      listeningIndicator: "orb",
      accentColor: "#a78bfa", // violet
    },
  },
  custom: {
    id: "custom",
    name: "Custom",
    vocabulary: BASE_VOCABULARY,
    formality: 1,
    verbosity: 1,
    markers: {
      greeting: ["Hello.", "Good to see you.", "Ready.", "At your service."],
      status: ["Systems nominal.", "All operational.", "Standing by.", "Monitoring."],
      alert: ["Alert: ", "Warning: ", "Attention: "],
      commentary: ["Note: ", "Observation: ", "For your information: "],
      time_announcement: ["The time is ", "It is now ", "Current time: "],
      proactive_suggestion: ["Suggestion: ", "Recommendation: ", "Proposal: "],
      affirmation: ["Confirmed.", "Understood.", "Acknowledged.", "Done."],
      hesitation: ["Processing.", "One moment.", "Computing."],
    },
    timeGreetings: {
      morning: ["Good morning.", "Morning. How can I help?"],
      afternoon: ["Good afternoon.", "Afternoon. All systems nominal."],
      evening: ["Good evening.", "Evening. Standing by."],
      night: ["Good night.", "Late. I'll keep watch."],
    },
    murmurs: {
      idle: ["Monitoring.", "All quiet.", "Standing by.", "Systems nominal."],
      monitoring: ["Resources stable.", "Network steady.", "Thermals nominal.", "No anomalies."],
      working: ["Processing.", "Executing.", "Working.", "Compiling."],
      waiting: ["Awaiting input.", "Ready.", "Standing by."],
    },
    suggestionFrames: {
      prefix: ["Suggestion: ", "Recommendation: ", "Proposal: "],
      action: ["Proceed?", "Execute?", "Approve?"],
      dismissive: ["Dismissed.", "Understood.", "Noted."],
    },
    visual: {
      listeningIndicator: "orb",
      accentColor: "#22d3ee",
    },
  },
};

/* -------------------------------------------------------------------------- */
/* Context Detection                                                          */
/* -------------------------------------------------------------------------- */

/**
 * Determine the current context mode from system state and time.
 * This runs periodically and on significant events.
 */
export function detectContext(
  snapshot: { cpuPercent?: number; memoryPercent?: number; activeApp?: string | null } | null,
  hour: number
): ContextMode {
  // High load takes priority
  if (snapshot) {
    const cpu = snapshot.cpuPercent ?? 0;
    const mem = snapshot.memoryPercent ?? 0;
    if (cpu > 80 || mem > 85) return "high_load";
  }

  // Time-based work hours (9–17 local time, configurable later)
  const isWorkHours = hour >= 9 && hour < 17;

  // App-based focus detection (simplified)
  const isFocusApp = snapshot?.activeApp
    ? ["code", "vscode", "idea", "terminal", "vim", "emacs", "obsidian", "notion"].some(
        (a) => snapshot.activeApp?.toLowerCase().includes(a)
      )
    : false;

  if (isFocusApp) return "focus";
  if (isWorkHours) return "work";
  return "casual";
}

/* -------------------------------------------------------------------------- */
/* Text Transformation                                                        */
/* -------------------------------------------------------------------------- */

/**
 * Apply personality vocabulary to a text string.
 * Replaces known tokens with personality-specific variants.
 */
export function applyVocabulary(text: string, vocab: VocabularyMap): string {
  let result = text;
  // Sort keys by length descending to avoid partial replacements
  const sortedKeys = Object.keys(vocab).sort((a, b) => b.length - a.length);
  for (const key of sortedKeys) {
    const regex = new RegExp(`\\b${escapeRegExp(key)}\\b`, "gi");
    const replacement = vocab[key];
    if (replacement !== undefined) {
      result = result.replace(regex, replacement);
    }
  }
  return result;
}

function escapeRegExp(string: string): string {
  return string.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Select a random marker from the personality's marker set for a given kind.
 */
export function pickMarker(personality: Personality, kind: keyof Personality["markers"]): string {
  const options = personality.markers[kind];
  return options.length > 0 ? options[Math.floor(Math.random() * options.length)]! : "";
}

/**
 * Select a time-appropriate greeting.
 */
export function pickTimeGreeting(personality: Personality, hour: number): string {
  let period: keyof Personality["timeGreetings"];
  if (hour >= 5 && hour < 12) period = "morning";
  else if (hour >= 12 && hour < 17) period = "afternoon";
  else if (hour >= 17 && hour < 22) period = "evening";
  else period = "night";
  const options = personality.timeGreetings[period];
  return options.length > 0 ? options[Math.floor(Math.random() * options.length)]! : "";
}

/**
 * Select a murmur appropriate to the current activity.
 */
export function pickMurmur(personality: Personality, context: ContextMode): string {
  let pool: string[];
  switch (context) {
    case "high_load":
    case "focus":
      pool = personality.murmurs.working;
      break;
    case "work":
      pool = [...personality.murmurs.monitoring, ...personality.murmurs.working];
      break;
    case "idle":
      pool = personality.murmurs.idle;
      break;
    default:
      pool = [...personality.murmurs.idle, ...personality.murmurs.monitoring];
  }
  return pool.length > 0 ? pool[Math.floor(Math.random() * pool.length)]! : "";
}

/**
 * Frame a proactive suggestion in the personality's voice.
 */
export function frameSuggestion(
  personality: Personality,
  suggestionText: string,
  hasAction: boolean
): string {
  const prefix = pickMarker(personality, "proactive_suggestion");
  const vocabbed = applyVocabulary(suggestionText, personality.vocabulary);
  if (hasAction) {
    const actionOptions = personality.suggestionFrames.action;
    const action = actionOptions.length > 0 ? actionOptions[Math.floor(Math.random() * actionOptions.length)] : "";
    return `${prefix}${vocabbed} ${action}`.trim();
  }
  return `${prefix}${vocabbed}`.trim();
}

/**
 * Frame a dismissal in the personality's voice.
 */
export function frameDismissal(personality: Personality): string {
  const options = personality.suggestionFrames.dismissive;
  return options.length > 0 ? options[Math.floor(Math.random() * options.length)]! : "";
}

/* -------------------------------------------------------------------------- */
/* Personality Manager (React Hook)                                           */
/* -------------------------------------------------------------------------- */

export type PersonalityManager = {
  /** Current personality state. */
  state: PersonalityState;
  /** Switch to a different personality by ID. */
  setPersonality: (id: PersonalityId) => void;
  /** Update a specific personality's custom settings. */
  updateCustomPersonality: (partial: Partial<Personality>) => void;
  /** Manually set context (overrides auto-detection until next tick). */
  setContext: (context: ContextMode) => void;
  /** Set listening state (wake word / voice UI). */
  setListening: (listening: boolean) => void;
  /** Speak a line immediately (bypasses queue, uses TTS if enabled). */
  speak: (text: string, kind: SpokenLine["kind"], options?: { forceSpeak?: boolean }) => Promise<void>;
  /** Add a line to history without speaking. */
  addToHistory: (text: string, kind: SpokenLine["kind"]) => void;
  /** Clear history. */
  clearHistory: () => void;
  /** Enable/disable TTS for this personality. */
  setTtsEnabled: (enabled: boolean) => void;
  /** Trigger a one-off time announcement. */
  announceTime: () => Promise<void>;
  /** Trigger a one-off greeting. */
  greet: () => Promise<void>;
  /** Trigger a contextual murmur. */
  murmur: () => Promise<void>;
};

/** Default personality ID from settings or fallback. */
const DEFAULT_PERSONALITY_ID: PersonalityId = "jarvis";

/** Settings keys for persistence. */
const PERSONALITY_SETTINGS = {
  ACTIVE_ID: "personality_active_id",
  CUSTOM_DEF: "personality_custom_definition",
  TTS_ENABLED: "personality_tts_enabled",
  MURMUR_INTERVAL_MS: "personality_murmur_interval_ms",
  TIME_ANNOUNCE_ENABLED: "personality_time_announce_enabled",
  TIME_ANNOUNCE_INTERVAL_MS: "personality_time_announce_interval_ms",
} as const;

const DEFAULT_MURMUR_INTERVAL_MS = 5 * 60 * 1000; // 5 minutes
const DEFAULT_TIME_ANNOUNCE_INTERVAL_MS = 60 * 60 * 1000; // 1 hour

/**
 * React hook that provides the personality manager.
 * Integrates with ambient monitoring, proactive suggestions, and TTS.
 */
export function usePersonality(): PersonalityManager {
  const { speakProactive } = useNexus();
  const [state, setState] = useState<PersonalityState>(() => ({
    personality: PERSONALITIES[DEFAULT_PERSONALITY_ID],
    context: "idle",
    isListening: false,
    history: [],
    ttsEnabled: true,
    murmurTimer: null,
    timeTimer: null,
  }));
  const stateRef = useRef(state);
  stateRef.current = state;

  // Persist active personality
  const persistActivePersonality = useCallback(async (id: PersonalityId) => {
    try {
      await api.settings.patch({ values: { [PERSONALITY_SETTINGS.ACTIVE_ID]: id } });
    } catch {
      // Non-fatal
    }
  }, []);

  // Persist custom personality definition
  const persistCustomPersonality = useCallback(async (def: Partial<Personality>) => {
    try {
      const current = stateRef.current.personality;
      if (current.id === "custom") {
        const merged = { ...current, ...def };
        await api.settings.patch({ values: { [PERSONALITY_SETTINGS.CUSTOM_DEF]: JSON.stringify(merged) } });
      }
    } catch {
      // Non-fatal
    }
  }, []);

  // Persist TTS setting
  const persistTtsEnabled = useCallback(async (enabled: boolean) => {
    try {
      await api.settings.patch({ values: { [PERSONALITY_SETTINGS.TTS_ENABLED]: enabled } });
    } catch {
      // Non-fatal
    }
  }, []);

  // Speak a line via TTS and add to history
  const speak = useCallback(
    async (text: string, kind: SpokenLine["kind"], options?: { forceSpeak?: boolean }) => {
      const { personality, ttsEnabled, context } = stateRef.current;
      const finalText = applyVocabulary(text, personality.vocabulary);
      const line: SpokenLine = {
        id: crypto.randomUUID(),
        text: finalText,
        kind,
        timestamp: Date.now(),
        context,
        spoken: false,
      };

      // Add to history immediately
      setState((s) => ({ ...s, history: [...s.history.slice(-49), line] }));

      // Speak via TTS if enabled
      if (ttsEnabled || options?.forceSpeak) {
        try {
          await speakProactive(finalText);
          // Update history entry to mark as spoken
          setState((s) => ({
            ...s,
            history: s.history.map((l) => (l.id === line.id ? { ...l, spoken: true } : l)),
          }));
        } catch {
          // TTS failed, line remains unspoken in history
        }
      }
    },
    [speakProactive]
  );

  // Add to history without speaking
  const addToHistory = useCallback((text: string, kind: SpokenLine["kind"]) => {
    const { personality, context } = stateRef.current;
    const finalText = applyVocabulary(text, personality.vocabulary);
    const line: SpokenLine = {
      id: crypto.randomUUID(),
      text: finalText,
      kind,
      timestamp: Date.now(),
      context,
      spoken: false,
    };
    setState((s) => ({ ...s, history: [...s.history.slice(-49), line] }));
  }, []);

  // Clear history
  const clearHistory = useCallback(() => {
    setState((s) => ({ ...s, history: [] }));
  }, []);

  // Set personality
  const setPersonality = useCallback(
    (id: PersonalityId) => {
      const personality = PERSONALITIES[id];
      if (!personality) return;
      setState((s) => ({ ...s, personality }));
      persistActivePersonality(id);
      // Greet on personality change
      const hour = new Date().getHours();
      const greeting = pickTimeGreeting(personality, hour);
      speak(greeting, "greeting", { forceSpeak: true });
    },
    [persistActivePersonality, speak]
  );

  // Update custom personality
  const updateCustomPersonality = useCallback(
    (partial: Partial<Personality>) => {
      setState((s) => {
        if (s.personality.id !== "custom") return s;
        const updated = { ...s.personality, ...partial };
        persistCustomPersonality(partial);
        return { ...s, personality: updated };
      });
    },
    [persistCustomPersonality]
  );

  // Set context manually
  const setContext = useCallback((context: ContextMode) => {
    setState((s) => ({ ...s, context }));
  }, []);

  // Set listening state
  const setListening = useCallback((isListening: boolean) => {
    setState((s) => ({ ...s, isListening }));
  }, []);

  // Set TTS enabled
  const setTtsEnabled = useCallback(
    (enabled: boolean) => {
      setState((s) => ({ ...s, ttsEnabled: enabled }));
      persistTtsEnabled(enabled);
    },
    [persistTtsEnabled]
  );

  // Announce time
  const announceTime = useCallback(async () => {
    const { personality, ttsEnabled } = stateRef.current;
    const now = new Date();
    const timeStr = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const marker = pickMarker(personality, "time_announcement");
    const text = `${marker}${timeStr}`;
    await speak(text, "time_announcement", { forceSpeak: ttsEnabled });
  }, [speak]);

  // Greet
  const greet = useCallback(async () => {
    const { personality, ttsEnabled } = stateRef.current;
    const hour = new Date().getHours();
    const greeting = pickTimeGreeting(personality, hour);
    await speak(greeting, "greeting", { forceSpeak: ttsEnabled });
  }, [speak]);

  // Murmur
  const murmur = useCallback(async () => {
    const { personality, ttsEnabled, context } = stateRef.current;
    const text = pickMurmur(personality, context);
    await speak(text, "commentary", { forceSpeak: ttsEnabled });
  }, [speak]);

  // Auto-murmur timer
  useEffect(() => {
    const intervalMs = DEFAULT_MURMUR_INTERVAL_MS; // Could be from settings

    const timer = window.setInterval(() => {
      // Only murmur if not actively working (context not high_load/focus)
      const { context } = stateRef.current;
      if (context !== "high_load" && context !== "focus") {
        murmur();
      }
    }, intervalMs);

    return () => window.clearInterval(timer);
  }, [murmur]);

  // Auto time announcement timer
  useEffect(() => {
    // Only if enabled in settings (simplified: always on for now)
    const intervalMs = DEFAULT_TIME_ANNOUNCE_INTERVAL_MS;

    const timer = window.setInterval(() => {
      announceTime();
    }, intervalMs);

    return () => window.clearInterval(timer);
  }, [announceTime]);

  // Context detection from ambient events
  useEffect(() => {
    // This would ideally subscribe to ambient events
    // For now, we re-detect on startup changes
    const detect = async () => {
      try {
        const snapshot = await api.systemStatus();
        const hour = new Date().getHours();
        const context = detectContext(
          snapshot.data
            ? {
                cpuPercent:
                  snapshot.data.cpu.cores && snapshot.data.cpu.load_1m !== null && snapshot.data.cpu.load_1m !== undefined
                    ? Math.round((snapshot.data.cpu.load_1m / snapshot.data.cpu.cores) * 100)
                    : undefined,
                memoryPercent: snapshot.data.memory.percent_used,
                activeApp: null, // Would come from ambient desktop override
              }
            : null,
          hour
        );
        setState((s) => ({ ...s, context }));
      } catch {
        // Non-fatal
      }
    };

    // Initial detection
    detect();

    // Re-detect periodically
    const timer = window.setInterval(detect, 60 * 1000); // Every minute
    return () => window.clearInterval(timer);
  }, []);

  // Greet on first load
  useEffect(() => {
    let greeted = false;
    if (!greeted) {
      greeted = true;
      // Small delay to let UI settle
      setTimeout(() => greet(), 1000);
    }
  }, [greet]);

  // Return manager API
  const manager = useMemo<PersonalityManager>(
    () => ({
      state,
      setPersonality,
      updateCustomPersonality,
      setContext,
      setListening,
      speak,
      addToHistory,
      clearHistory,
      setTtsEnabled,
      announceTime,
      greet,
      murmur,
    }),
    [
      state,
      setPersonality,
      updateCustomPersonality,
      setContext,
      setListening,
      speak,
      addToHistory,
      clearHistory,
      setTtsEnabled,
      announceTime,
      greet,
      murmur,
    ]
  );

  return manager;
}

/* -------------------------------------------------------------------------- */
/* Visual Indicator Component                                                 */
/* -------------------------------------------------------------------------- */

import type { ReactElement } from "react";
import { Icon } from "../components/Icon";

export interface ListeningIndicatorProps {
  /** Whether the system is currently listening. */
  isListening: boolean;
  /** Personality ID for visual style. */
  personalityId: PersonalityId;
  /** Size variant. */
  size?: "sm" | "md" | "lg";
  /** Show label text. */
  showLabel?: boolean;
}

/**
 * Visual listening indicator that reflects the active personality's style.
 * Renders as pulse, wave, orb, or bars animation.
 */
export function ListeningIndicator({
  isListening,
  personalityId,
  size = "md",
  showLabel = false,
}: ListeningIndicatorProps): ReactElement | null {
  if (!isListening) return null;

  const personality = PERSONALITIES[personalityId] ?? PERSONALITIES.jarvis;
  const style = personality.visual.listeningIndicator;
  const color = personality.visual.accentColor;

  const sizeClasses = {
    sm: "w-6 h-6",
    md: "w-10 h-10",
    lg: "w-16 h-16",
  };

  const renderIndicator = (): ReactElement => {
    switch (style) {
      case "pulse":
        return (
          <div
            className={`relative ${sizeClasses[size]} animate-pulse`}
            style={{ background: color, borderRadius: "50%", boxShadow: "0 0 12px " + color }}
            aria-hidden="true"
          />
        );
      case "wave":
        return (
          <div className={`relative ${sizeClasses[size]}`} aria-hidden="true">
            {[0, 1, 2, 3].map((i) => (
              <div
                key={i}
                className="absolute inset-0 rounded-full"
                style={{
                  border: "2px solid " + color,
                  animation: "wave-expand 1.5s ease-out " + (i * 0.2) + "s infinite",
                  opacity: 0.6,
                }}
              />
            ))}
          </div>
        );
      case "orb":
        return (
          <div
            className={`relative ${sizeClasses[size]} animate-orb-float`}
            style={{
              background: "radial-gradient(circle at 30% 30%, " + color + "DD, " + color + "44 70%)",
              borderRadius: "50%",
              boxShadow: "0 0 24px " + color + "88, inset 0 -4px 8px " + color + "44",
            }}
            aria-hidden="true"
          />
        );
      case "bars":
      default:
        return (
          <div className={`flex items-center justify-center gap-1 ${sizeClasses[size]}`} aria-hidden="true">
            {[0, 1, 2, 3, 4].map((i) => (
              <div
                key={i}
                className="w-1 rounded-full bg-current"
                style={{
                  height: (20 + i * 8) + "%",
                  animation: "bar-dance 0.8s ease-in-out " + (i * 0.1) + "s infinite alternate",
                }}
              />
            ))}
          </div>
        );
    }
  };

  return (
    <div className="flex flex-col items-center gap-1" role="status" aria-live="polite" aria-label="Listening">
      <div className="relative" style={{ color: color }}>{renderIndicator()}</div>
      {showLabel && (
        <span className="font-mono text-[10px] uppercase tracking-wider" style={{ color }}>
          LISTENING
        </span>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Personality Selector Component                                             */
/* -------------------------------------------------------------------------- */

export interface PersonalitySelectorProps {
  /** Current active personality ID. */
  value: PersonalityId;
  /** Callback when selection changes. */
  onChange: (id: PersonalityId) => void;
  /** Whether to show custom personality editor. */
  showCustomEditor?: boolean;
}

/**
 * Personality selector dropdown/grid.
 * Shows all presets plus custom option.
 */
export function PersonalitySelector({
  value,
  onChange,
  showCustomEditor = false,
}: PersonalitySelectorProps): ReactElement {
  const presets: PersonalityId[] = ["jarvis", "friday", "ultron", "edith", "cortanna"];

  return (
    <div className="space-y-3">
      <div role="group" aria-label="Personality">
        {presets.map((id) => {
          const p = PERSONALITIES[id];
          const selected = value === id;
          return (
            <button
              key={id}
              type="button"
              role="radio"
              aria-checked={selected}
              onClick={() => onChange(id)}
              className={[
                "w-full text-left p-3 chamfer border transition-colors",
                selected
                  ? "border-accent bg-accent/8"
                  : "border-line-bright hover:border-accent/40 hover:bg-raised",
              ].join(" ")}
              style={selected ? { borderColor: p.visual.accentColor } : undefined}
            >
              <div className="flex items-center gap-3">
                <div
                  className="w-3 h-3 rounded-full shrink-0"
                  style={{ background: p.visual.accentColor }}
                />
                <div className="flex-1 min-w-0">
                  <p className="label truncate">{p.name}</p>
                  <p className="font-mono text-[10px] text-ink-dim truncate">
                    {p.formality === 2 ? "Formal" : p.formality === 0 ? "Casual" : "Balanced"} •
                    {p.verbosity === 2 ? "Chatty" : p.verbosity === 0 ? "Terse" : "Normal"}
                  </p>
                </div>
                {selected && <Icon name="check" size={14} className="text-accent shrink-0" />}
              </div>
            </button>
          );
        })}
        {showCustomEditor && (
          <button
            type="button"
            role="radio"
            aria-checked={value === "custom"}
            onClick={() => onChange("custom")}
            className={[
              "w-full text-left p-3 chamfer border transition-colors",
              value === "custom"
                ? "border-accent bg-accent/8"
                : "border-line-bright hover:border-accent/40 hover:bg-raised",
            ].join(" ")}
          >
            <div className="flex items-center gap-3">
              <div className="w-3 h-3 rounded-full shrink-0 bg-gradient-to-r from-cyan-400 to-violet-400" />
              <div className="flex-1">
                <p className="label truncate">Custom</p>
                <p className="font-mono text-[10px] text-ink-dim truncate">Build your own personality</p>
              </div>
              {value === "custom" && <Icon name="check" size={14} className="text-accent shrink-0" />}
            </div>
          </button>
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Proactive Integration Helpers                                              */
/* -------------------------------------------------------------------------- */

/**
 * Transform a proactive suggestion summary into the active personality's voice.
 * Used by the ProactiveToast component.
 */
export function personalizeProactiveSuggestion(
  personality: Personality,
  summary: string,
  hasAction: boolean
): string {
  return frameSuggestion(personality, summary, hasAction);
}

/**
 * Transform an ambient observation into the active personality's voice.
 * Used by the ambient monitor when speaking observations.
 */
export function personalizeAmbientObservation(
  personality: Personality,
  summary: string,
  kind: "alert" | "notice" | "info"
): string {
  const markerKey = kind === "alert" ? "alert" : kind === "notice" ? "commentary" : "status";
  const marker = pickMarker(personality, markerKey);
  const vocabbed = applyVocabulary(summary, personality.vocabulary);
  return `${marker}${vocabbed}`;
}

/**
 * Generate a contextual status report in the personality's voice.
 * Combines system snapshot with personality markers.
 */
export function generateStatusReport(
  personality: Personality,
  snapshot: {
    cpuPercent?: number;
    memoryPercent?: number;
    diskPercent?: number;
    batteryPercent?: number | null;
    charging?: boolean;
    uptimeHuman?: string;
  }
): string {
  const parts: string[] = [];

  // Opening marker
  parts.push(pickMarker(personality, "status"));

  // System health
  if (snapshot.cpuPercent !== undefined) {
    const cpu = snapshot.cpuPercent;
    if (cpu > 80) parts.push(`CPU at ${cpu} percent.`);
    else if (cpu > 50) parts.push(`CPU load is ${cpu} percent.`);
    else parts.push(`CPU nominal at ${cpu} percent.`);
  }

  if (snapshot.memoryPercent !== undefined) {
    const mem = snapshot.memoryPercent;
    if (mem > 85) parts.push(`Memory at ${mem} percent.`);
    else parts.push(`Memory at ${mem} percent.`);
  }

  if (snapshot.diskPercent !== undefined && snapshot.diskPercent > 80) {
    parts.push(`Disk at ${snapshot.diskPercent} percent.`);
  }

  if (snapshot.batteryPercent !== null && snapshot.batteryPercent !== undefined) {
    const bat = snapshot.batteryPercent;
    const state = snapshot.charging ? "charging" : "discharging";
    if (bat < 20 && !snapshot.charging) {
      parts.push(`Battery critical at ${bat} percent, ${state}.`);
    } else {
      parts.push(`Battery at ${bat} percent, ${state}.`);
    }
  }

  if (snapshot.uptimeHuman) {
    parts.push(`Uptime: ${snapshot.uptimeHuman}.`);
  }

  // Closing
  parts.push(pickMarker(personality, "affirmation"));

  return applyVocabulary(parts.join(" "), personality.vocabulary);
}

/* -------------------------------------------------------------------------- */
/* Settings Integration                                                       */
/* -------------------------------------------------------------------------- */

/** Personality-related settings keys for the Settings screen. */
export const PERSONALITY_SETTING_KEYS = {
  ACTIVE_ID: PERSONALITY_SETTINGS.ACTIVE_ID,
  CUSTOM_DEF: PERSONALITY_SETTINGS.CUSTOM_DEF,
  TTS_ENABLED: PERSONALITY_SETTINGS.TTS_ENABLED,
  MURMUR_INTERVAL_MS: PERSONALITY_SETTINGS.MURMUR_INTERVAL_MS,
  TIME_ANNOUNCE_ENABLED: PERSONALITY_SETTINGS.TIME_ANNOUNCE_ENABLED,
  TIME_ANNOUNCE_INTERVAL_MS: PERSONALITY_SETTINGS.TIME_ANNOUNCE_INTERVAL_MS,
} as const;

/** Default values for personality settings. */
export const PERSONALITY_DEFAULTS = {
  [PERSONALITY_SETTINGS.ACTIVE_ID]: DEFAULT_PERSONALITY_ID,
  [PERSONALITY_SETTINGS.CUSTOM_DEF]: JSON.stringify(PERSONALITIES.custom, null, 2),
  [PERSONALITY_SETTINGS.TTS_ENABLED]: true,
  [PERSONALITY_SETTINGS.MURMUR_INTERVAL_MS]: DEFAULT_MURMUR_INTERVAL_MS,
  [PERSONALITY_SETTINGS.TIME_ANNOUNCE_ENABLED]: false,
  [PERSONALITY_SETTINGS.TIME_ANNOUNCE_INTERVAL_MS]: DEFAULT_TIME_ANNOUNCE_INTERVAL_MS,
} as const;

/* -------------------------------------------------------------------------- */
/* Export all                                                                 */
/* -------------------------------------------------------------------------- */

// Types are already exported at their declaration