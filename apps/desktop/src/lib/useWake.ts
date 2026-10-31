/**
 * The microphone half of hands-free listening (§9, §12, §61, §71).
 *
 * `wake.ts` decides *whether* audio is worth sending; this opens the device, taps the
 * samples, and hands finished segments to the transcriber. It is a separate file for a
 * reason that is not tidiness: everything here needs a real room and a real device, so
 * keeping it apart is what lets the arithmetic next door be verified by `npm run
 * test:wake` instead of by talking to a laptop.
 *
 * Four things worth knowing before changing this.
 *
 * 1. **Raw samples, not `MediaRecorder`.** The gate can only report that speech
 *    started after it has already started, so the first syllable of the wake word is
 *    in the past by then. A ring buffer of floats can be replayed with that lead-in;
 *    a WebM stream cannot be cut from the middle and still decode. That is also why
 *    the upload is a WAV this file writes itself.
 *
 * 2. **`enabled` going false releases the device, it does not pause it.** A held
 *    `MediaStream` keeps the operating system's recording light on, and a light that
 *    stays lit while nothing is listening is the §60 failure in the form users find
 *    most alarming. Re-arming re-measures the room, which costs one second.
 *
 * 3. **The caller decides when to arm, and is expected to disarm while NEXUS speaks.**
 *    Otherwise the assistant hears its own voice, and "Nexus" inside a spoken answer
 *    becomes a command. Echo cancellation reduces that; it does not settle it.
 *
 * 4. **`AudioWorklet` first, `ScriptProcessorNode` second.** The worklet is the
 *    supported path and runs off the main thread. The fallback is deprecated and still
 *    the only tap in some WebKit builds, which is what this app ships inside.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { api, ApiError } from "./api";
import {
  PreRoll,
  SpeechGate,
  UtteranceBudget,
  encodeWav,
  matchWake,
  rmsOf,
  trimTail,
} from "./wake";

/** How much audio before the gate opened is kept and prepended. */
const PRE_ROLL_MS = 500;
/** Samples per block handed to the main thread. ~43ms at 48kHz. */
const BLOCK = 2048;
/** Utterances per minute before hands-free stops itself. */
const BUDGET_PER_MINUTE = 20;

/**
 * A monotonic clock.
 *
 * `performance.now()` rather than `Date.now()` because every duration here is a
 * threshold: a clock correction mid-sentence would either close a segment early or
 * make a 12-second cap arrive at an arbitrary time.
 */
const clock = (): number =>
  typeof performance !== "undefined" ? performance.now() : Date.now();

export type WakeState =
  /** Not armed. */
  | "off"
  /** Asking for the device. */
  | "opening"
  /** Measuring the room. Nothing is captured yet. */
  | "calibrating"
  /** Armed and quiet. */
  | "waiting"
  /** Capturing a segment. */
  | "hearing"
  /** Segment sent, waiting for words back. */
  | "checking"
  /** Stopped on purpose, with a reason in `problem`. Needs `resume()`. */
  | "stopped";

export type WakeReport = {
  state: WakeState;
  /** Live loudness, 0..1. For a meter, not for a decision. */
  level: number;
  /** What loudness currently counts as speech. Zero until calibrated. */
  threshold: number;
  /** The last thing transcribed, whether or not it was addressed to NEXUS. */
  lastHeard: string;
  /** True when `lastHeard` was not addressed to NEXUS and so was not acted on. */
  ignored: boolean;
  /** Why hands-free is not working, in words a user can act on. */
  problem: string;
  /** Utterances left in this minute before the ceiling stops it. */
  remaining: number;
  /** Clear a stop and arm again. */
  resume: () => void;
};

const WORKLET = `
class NexusTap extends AudioWorkletProcessor {
  constructor() {
    super();
    this.held = new Float32Array(${BLOCK});
    this.at = 0;
  }
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) {
      for (let index = 0; index < channel.length; index += 1) {
        this.held[this.at] = channel[index];
        this.at += 1;
        if (this.at === this.held.length) {
          this.port.postMessage(this.held.slice(0));
          this.at = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor("nexus-tap", NexusTap);
`;

export type WakeOptions = {
  /** Arm the microphone. False releases it. */
  enabled: boolean;
  wakeWord: string;
  /** Passed to transcription so the model is told the language, not guessing it. */
  language?: string;
  /** Called with what followed the wake word. Never called with an empty string. */
  onCommand: (command: string) => void;
};

/**
 * Listen for the wake word for as long as `enabled` is true.
 *
 * The callback and the wake word are read through refs so that changing either does
 * not tear the device down and re-measure the room: the effect depends on `enabled`
 * alone, which is the only thing that should be able to open or close a microphone.
 */
export function useWake({ enabled, wakeWord, language, onCommand }: WakeOptions): WakeReport {
  const [state, setState] = useState<WakeState>("off");
  const [level, setLevel] = useState(0);
  const [threshold, setThreshold] = useState(0);
  const [lastHeard, setLastHeard] = useState("");
  const [ignored, setIgnored] = useState(false);
  const [problem, setProblem] = useState("");
  const [remaining, setRemaining] = useState(BUDGET_PER_MINUTE);
  /** Bumped by `resume()` to re-run the effect after a self-inflicted stop. */
  const [attempt, setAttempt] = useState(0);
  /** Set when the budget stopped it, so the effect does not immediately re-arm. */
  const halted = useRef(false);

  const word = useRef(wakeWord);
  const speech = useRef(language);
  const command = useRef(onCommand);
  useEffect(() => {
    word.current = wakeWord;
    speech.current = language;
    command.current = onCommand;
  }, [wakeWord, language, onCommand]);

  const resume = useCallback(() => {
    halted.current = false;
    setProblem("");
    setAttempt((count) => count + 1);
  }, []);

  useEffect(() => {
    if (!enabled) {
      setState("off");
      setLevel(0);
      setThreshold(0);
      return;
    }
    if (halted.current) return;

    let dead = false;
    let stream: MediaStream | null = null;
    let context: AudioContext | null = null;
    let node: AudioNode | null = null;
    let source: MediaStreamAudioSourceNode | null = null;
    let meter: number | undefined;

    const gate = new SpeechGate();
    const budget = new UtteranceBudget(BUDGET_PER_MINUTE, 60_000);
    let roll: PreRoll | null = null;
    let capturing: Float32Array[] | null = null;
    /** The live level, published on a timer rather than per block (23 renders/sec). */
    let latest = 0;

    const release = () => {
      if (meter !== undefined) window.clearInterval(meter);
      node?.disconnect();
      source?.disconnect();
      stream?.getTracks().forEach((track) => track.stop());
      void context?.close().catch(() => undefined);
      stream = null;
      context = null;
      node = null;
      source = null;
    };

    const halt = (why: string) => {
      halted.current = true;
      release();
      if (dead) return;
      setState("stopped");
      setLevel(0);
      setProblem(why);
    };

    const finish = async (blocks: Float32Array[], rate: number) => {
      const now = clock();
      if (!budget.allow(now)) {
        halt(
          `More than ${BUDGET_PER_MINUTE} phrases were heard in a minute, which is a noise source rather than a conversation. Hands-free stopped so it is not transcribing a room. Press Resume when it is quieter.`,
        );
        return;
      }
      if (!dead) {
        setRemaining(budget.remaining(now));
        setState("checking");
      }
      const wav = new Blob([encodeWav(blocks, rate) as unknown as ArrayBuffer], { type: "audio/wav" });
      try {
        const heard = await api.voice.transcribe(wav, "utterance.wav", speech.current || undefined);
        if (dead) return;
        const text = (heard.text || "").trim();
        if (heard.empty || !text) {
          // The gate thought it was speech and the model found no words in it. Said
          // plainly rather than silently: it is the honest description of a room with
          // a fan in it, and it tells the user why nothing happened.
          setLastHeard("");
          setIgnored(false);
          setState("waiting");
          return;
        }
        const match = matchWake(text, word.current);
        setLastHeard(match.heard);
        setIgnored(!match.addressed);
        setState("waiting");
        if (match.addressed && match.command) command.current(match.command);
      } catch (cause) {
        if (dead) return;
        setState("waiting");
        setProblem(
          cause instanceof ApiError
            ? cause.detail
              ? `${cause.message} ${cause.detail}`
              : cause.message
            : "That phrase could not be transcribed.",
        );
      }
    };

    const onBlock = (block: Float32Array, rate: number) => {
      if (dead || !roll) return;
      latest = rmsOf(block);
      const verdict = gate.feed(latest, clock());
      switch (verdict) {
        case "calibrating":
          roll.push(block);
          return;
        case "quiet":
          roll.push(block);
          return;
        case "open":
          // The pre-roll is the lead-in: everything up to and including the moment the
          // gate decided this was speech, which is where the wake word actually starts.
          capturing = [...roll.drain(), block];
          setState("hearing");
          return;
        case "hold":
          capturing?.push(block);
          return;
        case "discard":
          capturing = null;
          setState("waiting");
          return;
        case "close":
        case "cap": {
          const blocks = capturing;
          capturing = null;
          if (!blocks || blocks.length === 0) {
            setState("waiting");
            return;
          }
          // A closed segment ends with the silence that closed it; a capped one ends
          // mid-noise and has nothing to trim.
          void finish(
            verdict === "close" ? trimTail(blocks, rate, gate.silenceMs) : blocks,
            rate,
          );
          return;
        }
      }
    };

    void (async () => {
      setProblem("");
      setState("opening");
      if (!navigator.mediaDevices?.getUserMedia) {
        halt("This shell has no microphone API, so hands-free listening cannot run here.");
        return;
      }
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            // All three are the browser's own processing, and all three help here:
            // echo cancellation is what keeps a spoken answer from being heard back as
            // a command, and the other two make the noise floor mean something.
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
        });
      } catch (cause) {
        halt(
          cause instanceof DOMException && cause.name === "NotAllowedError"
            ? "The browser refused the microphone, so hands-free listening is off. Allow it for this window and press Resume."
            : "No microphone could be opened for hands-free listening.",
        );
        return;
      }
      if (dead) {
        release();
        return;
      }

      const Engine = window.AudioContext ?? (window as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!Engine) {
        halt("This shell has no Web Audio, so loudness cannot be measured here.");
        return;
      }
      context = new Engine();
      // Chrome starts a context suspended until a gesture. Resumed here so a window
      // that was never clicked still arms rather than sitting at 0.0 forever.
      if (context.state === "suspended") await context.resume().catch(() => undefined);
      if (dead) {
        release();
        return;
      }
      const rate = context.sampleRate;
      roll = new PreRoll(PRE_ROLL_MS, rate);
      gate.start(clock());
      source = context.createMediaStreamSource(stream);

      let tapped = false;
      if (context.audioWorklet) {
        const url = URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" }));
        try {
          await context.audioWorklet.addModule(url);
          if (dead) {
            release();
            return;
          }
          const worklet = new AudioWorkletNode(context, "nexus-tap");
          worklet.port.onmessage = (event: MessageEvent<Float32Array>) => onBlock(event.data, rate);
          node = worklet;
          tapped = true;
        } catch {
          // Fall through to the deprecated tap rather than failing: a build without
          // AudioWorklet is a build where hands-free would otherwise not exist.
        } finally {
          URL.revokeObjectURL(url);
        }
      }
      if (!tapped) {
        const legacy = context.createScriptProcessor(BLOCK, 1, 1);
        legacy.onaudioprocess = (event) => {
          onBlock(new Float32Array(event.inputBuffer.getChannelData(0)), rate);
        };
        node = legacy;
        // A ScriptProcessorNode only runs while connected to a destination. Nothing is
        // played: the gain is zero, so the tap ticks and the room stays silent.
        const mute = context.createGain();
        mute.gain.value = 0;
        legacy.connect(mute);
        mute.connect(context.destination);
      }
      if (node) source.connect(node);

      meter = window.setInterval(() => {
        if (dead) return;
        setLevel(latest);
        setThreshold(gate.threshold);
      }, 120);

      setState("calibrating");
      // Calibration is a fixed second inside the gate; this only moves the label off
      // "calibrating" once the gate will actually accept a block.
      window.setTimeout(() => {
        if (!dead) setState((current) => (current === "calibrating" ? "waiting" : current));
      }, 1_100);
    })();

    return () => {
      dead = true;
      release();
    };
  }, [enabled, attempt]);

  return { state, level, threshold, lastHeard, ignored, problem, remaining, resume };
}
