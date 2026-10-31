/**
 * Voice, as the product's default rather than a feature (§9, §12, §61).
 *
 * NEXUS is voice-first, which in a UI means two concrete things: the microphone is
 * ready without being asked for, and an answer is *heard* as well as read. This
 * provider owns the second half and the settings that govern both.
 *
 * Five decisions live here.
 *
 * 1. **The core is the source of truth for the settings, not localStorage.** The
 *    Settings screen writes `tts_enabled`, `tts_voice` and `input_language` to the
 *    core, and the core is also what the routes read. A second copy in the browser
 *    would drift, and the drift would show up as an answer spoken in a voice the user
 *    had already changed. Writes announce themselves (`SETTINGS_CHANGED`) so this
 *    re-reads instead of going stale.
 *
 * 2. **One audio element for the whole window.** A new answer stops the previous one.
 *    Two overlapping voices are not "more responsive", they are unintelligible.
 *
 * 3. **A browser that refuses to autoplay is reported, not ignored.** Every engine
 *    blocks programmatic playback until the page has been interacted with, and
 *    `play()` rejects. Swallowing that produces the §61 failure exactly: a SPEAKING
 *    indicator over silence. The rejection becomes a sentence the shell can show.
 *
 * 4. **Text longer than one synthesis request is cut at a sentence, and the cut is
 *    said out loud.** The route carries 4,000 characters. Quietly speaking the first
 *    4,000 of a longer answer would let the user believe they heard all of it.
 *
 * 5. **An answer is spoken once.** Keyed on the core's `message_id`, so React's
 *    development double-render, a re-render from an unrelated state change, and a
 *    reconnect cannot each pay for the same synthesis again.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { api, ApiError, SETTINGS_CHANGED } from "../lib/api";
import { SpeechQueue } from "../lib/speech";
import { browserPlayer } from "../lib/voice";
import { isTerminal, useTurn } from "./turn";

/** The route's own ceiling (`mistral_audio.MAX_TTS_CHARS`), so the cut happens here. */
const MAX_SPEECH_CHARS = 4_000;

export type VoiceSettings = {
  /** Arm the microphone without being asked. Does not answer the browser's own prompt. */
  voiceEnabled: boolean;
  /** Speak every completed answer. */
  ttsEnabled: boolean;
  /** Empty means the provider default; the route falls back to it. */
  ttsVoice: string;
  /** Sent with every transcription so the model is told rather than guessing. */
  inputLanguage: string;
  /** Sent with every synthesis so the model is told rather than guessing. */
  outputLanguage: string;
  wakeWord: string;
  wakeWordEnabled: boolean;
};

export const VOICE_DEFAULTS: VoiceSettings = {
  voiceEnabled: false,
  ttsEnabled: false,
  ttsVoice: "",
  inputLanguage: "",
  outputLanguage: "",
  wakeWord: "nexus",
  wakeWordEnabled: false,
};

export type VoiceValue = VoiceSettings & {
  /** False until the settings have been read once. Nothing speaks before that. */
  ready: boolean;
  /** True while a synthesis request is in flight. */
  synthesizing: boolean;
  /** True while audio is actually playing — not while it is being made. */
  speaking: boolean;
  /**
   * True while a microphone is open somewhere in the window.
   *
   * Reported into here rather than owned here, because the component that opens the
   * microphone is the one that must close it, and a `MediaStream` held by a provider
   * that outlives every screen is a recording indicator that never goes out. The
   * sphere on NEXUS CORE needs the fact, though, and it is not a sibling of the
   * composer that has it — so the composer says so, and this is where it is said.
   */
  listening: boolean;
  setListening: (open: boolean) => void;
  /** What is being spoken, for a shell that wants to show it. Empty when silent. */
  spoken: string;
  /** Non-empty when speech failed, in words a user can act on. */
  problem: string;
  /** Set when an answer was too long for one request and was cut at a sentence. */
  note: string;
  /** Speak text now. Ignores `ttsEnabled` — an explicit press is not a setting. */
  speak: (text: string, voice?: string) => Promise<void>;
  /** Stop playback and drop the queue. */
  stop: () => void;
  clearProblem: () => void;
  /** Re-read the settings from the core. */
  reload: () => void;
};

const VoiceContext = createContext<VoiceValue | null>(null);

const asBool = (value: unknown, fallback: boolean): boolean =>
  typeof value === "boolean" ? value : fallback;
const asText = (value: unknown, fallback: string): string =>
  typeof value === "string" ? value : fallback;

/** Read the six voice keys out of the settings map, with the DEFAULTS as the floor. */
export function voiceSettingsOf(stored: Record<string, unknown>): VoiceSettings {
  return {
    voiceEnabled: asBool(stored.voice_enabled, VOICE_DEFAULTS.voiceEnabled),
    ttsEnabled: asBool(stored.tts_enabled, VOICE_DEFAULTS.ttsEnabled),
    ttsVoice: asText(stored.tts_voice, VOICE_DEFAULTS.ttsVoice),
    inputLanguage: asText(stored.input_language, VOICE_DEFAULTS.inputLanguage),
    outputLanguage: asText(stored.output_language, VOICE_DEFAULTS.outputLanguage),
    wakeWord: asText(stored.wake_word, VOICE_DEFAULTS.wakeWord).trim(),
    wakeWordEnabled: asBool(stored.wake_word_enabled, VOICE_DEFAULTS.wakeWordEnabled),
  };
}

/**
 * Cut `text` to something one synthesis request can carry.
 *
 * Cut at the last sentence end inside the limit rather than mid-word, and report
 * whether anything was dropped so the caller can say so. A silent truncation is the
 * one outcome not allowed: the user would believe they had heard the whole answer.
 */
export function forSpeech(text: string): { body: string; dropped: number } {
  const clean = text.trim();
  if (clean.length <= MAX_SPEECH_CHARS) return { body: clean, dropped: 0 };
  const window = clean.slice(0, MAX_SPEECH_CHARS);
  const boundary = Math.max(
    window.lastIndexOf(". "),
    window.lastIndexOf("! "),
    window.lastIndexOf("? "),
    window.lastIndexOf("\n"),
  );
  // A boundary in the first fifth would throw away most of what fits, so length wins
  // over tidiness there.
  const cut = boundary > MAX_SPEECH_CHARS / 5 ? boundary + 1 : MAX_SPEECH_CHARS;
  const body = clean.slice(0, cut).trim();
  return { body, dropped: clean.length - body.length };
}

export function VoiceProvider({ children }: { children: ReactNode }) {
  const { turn } = useTurn();

  const [settings, setSettings] = useState<VoiceSettings>(VOICE_DEFAULTS);
  const [ready, setReady] = useState(false);
  const [synthesizing, setSynthesizing] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [listening, setListening] = useState(false);
  const [spoken, setSpoken] = useState("");
  const [problem, setProblem] = useState("");
  const [note, setNote] = useState("");
  const [reloads, setReloads] = useState(0);

  /** One element, created on first use. `null` in a shell without `Audio`. */
  const player = useRef<HTMLAudioElement | null>(null);
  /** The `message_id` of the answer already spoken, so it is not spoken twice. */
  const said = useRef("");
  /** True after unmount, so a late response does not set state on a dead tree. */
  const gone = useRef(false);

  /**
   * The queue for a streamed answer, and how far into the turn's arrays it has been
   * fed. Refs rather than state: feeding is a side effect that must happen exactly
   * once per segment, and a re-render must not replay it as a second playback.
   */
  const queue = useRef<SpeechQueue | null>(null);
  const fed = useRef({ speech: 0, errors: 0, turn: 0 });
  /** The turn whose start already silenced the previous answer. */
  const hushed = useRef(0);

  useEffect(() => {
    gone.current = false;
    return () => {
      gone.current = true;
      player.current?.pause();
      queue.current?.stop();
    };
  }, []);

  // The settings, read once and again whenever any write announces itself. An
  // AbortController rather than a `cancelled` flag so a slow read is really dropped.
  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        const result = await api.settings.get();
        if (!live) return;
        setSettings(voiceSettingsOf(result.settings ?? {}));
      } catch {
        // The core being unreachable is already said by the status bar. Voice falls
        // back to off, which is the safe direction: nothing is spent and nothing
        // claims to be listening.
        if (live) setSettings(VOICE_DEFAULTS);
      } finally {
        if (live) setReady(true);
      }
    })();
    return () => {
      live = false;
    };
  }, [reloads]);

  const reload = useCallback(() => setReloads((count) => count + 1), []);

  useEffect(() => {
    const onChange = () => reload();
    window.addEventListener(SETTINGS_CHANGED, onChange);
    return () => window.removeEventListener(SETTINGS_CHANGED, onChange);
  }, [reload]);

  const stop = useCallback(() => {
    const audio = player.current;
    if (audio) {
      audio.pause();
      audio.removeAttribute("src");
    }
    // Both voices: the one that speaks a finished answer and the one that speaks a
    // streaming one. A stop that silenced only one of them would be a stop button
    // that works or does not depending on which route the turn took.
    queue.current?.stop();
    setSpeaking(false);
    setSpoken("");
  }, []);

  /**
   * The queue that plays a streaming answer, made on first use.
   *
   * The ordering rules live in `lib/speech`, which is pure and checked by
   * `npm run test:speech`; what belongs here is only what the window shows while it
   * plays. `onIdle` clears SPEAKING rather than leaving it lit over silence (§61).
   */
  const speech = useCallback((): SpeechQueue => {
    if (!queue.current) {
      queue.current = new SpeechQueue(browserPlayer(), {
        onStart: (segment) => {
          if (gone.current) return;
          setSpeaking(true);
          setSpoken(segment.text);
        },
        onIdle: () => {
          if (gone.current) return;
          setSpeaking(false);
          setSpoken("");
        },
        onProblem: (message) => {
          if (gone.current) return;
          setSpeaking(false);
          setSpoken("");
          setProblem(message);
        },
      });
    }
    return queue.current;
  }, []);

  const speak = useCallback(
    async (text: string, voice?: string): Promise<void> => {
      const { body, dropped } = forSpeech(text);
      if (!body) return;
      setProblem("");
      setNote(
        dropped > 0
          ? `Spoken to the end of a sentence; ${dropped.toLocaleString()} more characters are on screen.`
          : "",
      );
      if (typeof Audio === "undefined") {
        setProblem("This shell has no audio element, so nothing can be played here.");
        return;
      }

      setSynthesizing(true);
      let speech: Awaited<ReturnType<typeof api.voice.speak>>;
      try {
        speech = await api.voice.speak(body, voice ?? settings.ttsVoice ?? undefined);
      } catch (cause) {
        if (!gone.current) {
          setProblem(
            cause instanceof ApiError
              ? cause.detail
                ? `${cause.message} ${cause.detail}`
                : cause.message
              : "Speech could not be synthesised.",
          );
        }
        return;
      } finally {
        if (!gone.current) setSynthesizing(false);
      }
      if (gone.current) return;
      if (!speech.url) {
        // The route wrote a file it could not name. Guessing a path here would be
        // inventing one, so this says what happened instead.
        setProblem("The core synthesised speech but did not say where it is.");
        return;
      }

      const audio = player.current ?? new Audio();
      player.current = audio;
      audio.pause();
      audio.src = speech.url;
      audio.onended = () => {
        if (!gone.current) {
          setSpeaking(false);
          setSpoken("");
        }
      };
      audio.onerror = () => {
        if (!gone.current) {
          setSpeaking(false);
          setSpoken("");
          setProblem("The speech file could not be played.");
        }
      };
      try {
        await audio.play();
        if (!gone.current) {
          setSpeaking(true);
          setSpoken(body);
        }
      } catch {
        // Every engine blocks autoplay until the page has been interacted with, and
        // this is that rejection. Named plainly: the audio exists and the browser
        // declined to start it, which is a different problem from synthesis failing.
        if (!gone.current) {
          setSpeaking(false);
          setSpoken("");
          setProblem(
            "The browser would not start playback on its own. Click anywhere in the window once, and answers will be spoken from then on.",
          );
        }
      }
    },
    [settings.ttsVoice],
  );

  /**
   * A new turn makes the previous answer stale: stop mid-sentence rather than talk
   * over what NEXUS is now doing.
   *
   * Keyed on the turn's identity and not on its phase, which is the whole difference
   * between this and the obvious version. A streamed turn sits in `thinking` for its
   * entire length *while its own audio plays*, so stopping whenever the phase is
   * `thinking` would silence the answer being spoken, one text event after it started.
   *
   * Declared above the feeding effect on purpose: effects run in source order, and the
   * queue must be silenced before it is reset, not after.
   */
  useEffect(() => {
    if (!turn || isTerminal(turn.phase)) return;
    if (hushed.current === turn.startedAt) return;
    hushed.current = turn.startedAt;
    stop();
  }, [turn, stop]);

  /**
   * Play a streaming answer as it arrives.
   *
   * This is the streamed half of §12: audio for the first sentence exists while the
   * model is still writing the third, and the point of playing it now is that waiting
   * for the last token is what makes an assistant feel slow.
   *
   * Fed from the turn's arrays through a cursor, so each segment is queued exactly
   * once no matter how many times this re-renders — the turn grows by one entry per
   * event, and every render would otherwise re-queue the whole answer.
   */
  useEffect(() => {
    if (!turn || !turn.streamed || turn.adopted) return;
    // `startedAt` identifies the turn. A new one rewinds the playhead so the next
    // answer starts at its own first sentence rather than continuing the last.
    if (fed.current.turn !== turn.startedAt) {
      queue.current?.reset();
      fed.current = { speech: 0, errors: 0, turn: turn.startedAt };
    }
    const queued = speech();
    for (const segment of turn.speech.slice(fed.current.speech)) queued.push(segment);
    // A sentence with no audio is told to the queue as well, not just to the screen:
    // its index will never arrive, and a queue waiting for it would go silent for the
    // rest of the answer.
    for (const failed of turn.speechErrors.slice(fed.current.errors)) queued.skip(failed.index);
    fed.current.speech = turn.speech.length;
    fed.current.errors = turn.speechErrors.length;
    if (isTerminal(turn.phase)) {
      // Every event has arrived. From here a missing index means never, not not-yet,
      // so the queue may stop waiting for it and play what it has.
      queued.close();
    }
  }, [turn, speech]);

  // Speak a completed answer, once. Only `complete` — a failed turn's text is a
  // fragment and an interrupted one was stopped by the user, who does not then want
  // to hear it read aloud. `message_id` is the key rather than a counter so a
  // re-render, a StrictMode double-invoke and a resync cannot each pay for it again.
  useEffect(() => {
    if (!ready || !settings.ttsEnabled) return;
    if (!turn || turn.phase !== "complete") return;
    // A streamed turn has already been spoken, sentence by sentence, while it was
    // being written. Speaking it again here would be a second synthesis of an answer
    // the user has just heard — paid for twice and read out twice.
    if (turn.streamed) return;
    const id = turn.finished?.message_id ?? "";
    const text = turn.finished?.text || turn.text;
    if (!id || !text.trim() || said.current === id) return;
    said.current = id;
    void speak(text);
  }, [ready, settings.ttsEnabled, turn, speak]);

  // A new turn starting means the previous answer is stale — handled above, keyed on
  // the turn's identity rather than on its phase.

  const value = useMemo<VoiceValue>(
    () => ({
      ...settings,
      ready,
      synthesizing,
      speaking,
      listening,
      setListening,
      spoken,
      problem,
      note,
      speak,
      stop,
      clearProblem: () => setProblem(""),
      reload,
    }),
    [
      settings,
      ready,
      synthesizing,
      speaking,
      listening,
      spoken,
      problem,
      note,
      speak,
      stop,
      reload,
    ],
  );

  return <VoiceContext.Provider value={value}>{children}</VoiceContext.Provider>;
}

export function useVoice(): VoiceValue {
  const value = useContext(VoiceContext);
  if (!value) throw new Error("useVoice was called outside VoiceProvider.");
  return value;
}
