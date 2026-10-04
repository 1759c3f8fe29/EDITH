/**
 * The live turn (§57, §58, §71, §15).
 *
 * One conversation can have one turn in flight — the core enforces that with a 409,
 * keyed by conversation id — so this holds exactly one `LiveTurn` and not a map.
 *
 * The whole point of this file is that a turn is *watchable*: §15 says the user must
 * be able to see why a model was chosen, §57 says thinking must be visible rather
 * than a spinner, and §71 forbids an indefinite THINKING. So the reducer keeps the
 * routing decision, the assembled context, every tool call with its preview and its
 * result, and — crucially — a terminal phase for every way a turn can stop,
 * including the ways that are not the core's fault.
 *
 * Four things that are decisions rather than implementation:
 *
 * 1. **Results are paired to the last unresolved call, and an orphan result is
 *    kept.** `AiCore` interleaves strictly — `tool_call`, run, `tool_result` — with
 *    one exception: the malformed-arguments path yields a `tool_result` with no
 *    preceding `tool_call`, because the call never became a call. Dropping that
 *    result would hide the only evidence that the model emitted bad JSON, so an
 *    orphan gets a synthetic call row marked `parse_error` and is shown as sent.
 *
 * 2. **`onEnd` without a `done` event is not success and not silence.** The stream
 *    can drop while the core keeps running. `endReason` carries the sentence and
 *    the phase becomes `dropped`, which is a state the UI can offer to resync from
 *    `/api/chat/active` — as opposed to a turn that simply stays in THINKING
 *    forever, which is the §71 failure.
 *
 * 3. **Reading and running are separate.** `stopReading()` aborts the SSE read;
 *    `cancel()` posts to `/api/chat/cancel` and asks the core to stop at its next
 *    checkpoint. They are offered as different controls with different words,
 *    because closing a window does not undo a file that was already moved.
 *
 * 4. **A reload resyncs from the core, not from memory.** `/api/chat/active` is
 *    consulted on mount, so a window reloaded mid-turn adopts the running turn
 *    (text so far, tools so far) instead of showing idle beside a working machine.
 *    The adopted turn has no live stream attached — that is stated in `endReason`
 *    rather than papered over.
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

import { api, ApiError, type Preview } from "../lib/api";
import {
  startTurn,
  type ChatInput,
  type ContextInfo,
  type FinishedTurn,
  type IntentInfo,
  type RoutingInfo,
  type ToolCallInfo,
  type ToolRunInfo,
} from "../lib/chat";
import type { SpokenSegment } from "../lib/speech";
import {
  playable,
  startConverse,
  type ConverseEvent,
  type SegmentEvent,
  type SpeechErrorEvent,
} from "../lib/voice";

/** One tool call, with what was previewed before it and what came back after. */
export type LiveCall = {
  call: ToolCallInfo;
  /** The permission preview the core sent with the call. Null when it sent none. */
  preview: Preview | null;
  /** Null while it is still running. */
  result: ToolRunInfo | null;
};

/**
 * Where the turn is.
 *
 * `dropped` is deliberately not folded into `failed`: a failed turn is one the core
 * reported an error for, and a dropped one is a turn whose *outcome is unknown to
 * this window*. Those need different sentences and different offers — retry versus
 * resync — so they are different phases.
 */
export type TurnPhase =
  | "idle"
  | "sending"
  | "routing"
  | "thinking"
  | "executing"
  | "complete"
  | "failed"
  | "interrupted"
  | "dropped";

export type LiveTurn = {
  /** Empty until the `start` event names it — a new conversation has no id yet. */
  conversationId: string;
  /** What the user actually sent, kept so the exchange reads as a pair immediately. */
  prompt: string;
  text: string;
  /** Only some models emit this. Empty string means none was sent, not none existed. */
  reasoning: string;
  intent: IntentInfo | null;
  routing: RoutingInfo | null;
  context: Partial<ContextInfo> | null;
  calls: LiveCall[];
  /** Sentences the core wanted the user to read — degradations, substitutions (§30). */
  notices: string[];
  error: string | null;
  /** The core's own classification of the error, for choosing what to offer next. */
  errorKind: string;
  phase: TurnPhase;
  startedAt: number;
  /** The full record, once `done` arrived. Null for every non-`done` ending. */
  finished: FinishedTurn | null;
  /** Why the stream stopped, when it stopped for a reason worth saying. */
  endReason: string;
  /** True for a turn adopted from `/api/chat/active` — no stream is attached to it. */
  adopted: boolean;
  /**
   * What the core heard. Empty when the request was typed.
   *
   * The voice fields live on the turn rather than in the voice provider because they
   * *are* the turn: the transcript is the request the answer was built from, and a
   * transcription the user cannot see is the §61 failure — an answer that reads as
   * the model being stupid when it was the microphone.
   */
  heard: string;
  /** Which model transcribed it, so a mishearing is attributable rather than magic. */
  heardBy: string;
  /** The voice speaking this answer. Null when nothing is being spoken. */
  voice: { routed: string; model: string; voice: string } | null;
  /** Sentences cut for speech, in order, whether or not their audio exists yet. */
  segments: SegmentEvent[];
  /** Audio as it lands. The voice provider plays these in index order. */
  speech: SpokenSegment[];
  /** Sentences that have no audio, and why. The answer still finished. */
  speechErrors: SpeechErrorEvent[];
  /** True when this turn was streamed with speech, so nothing speaks it again. */
  streamed: boolean;
};

/**
 * What `send` takes: a chat turn, plus the three fields only a spoken one uses.
 *
 * `speak` is what chooses the route, and it is the caller's decision rather than a
 * setting read in here — an explicit press of a speaker button is not the same fact
 * as `tts_enabled`, and a provider that conflated them would speak an answer the user
 * asked to read.
 */
export type SendInput = ChatInput & {
  /** Stream the answer through `/api/voice/converse` and speak it as it is written. */
  speak?: boolean;
  /** Base64 of a recording, transcribed inside the turn. */
  audio?: string | null;
  /** Language hint for that transcription. */
  language?: string | null;
  /** Which voice to speak in. Empty means the `tts_voice` setting. */
  voice?: string | null;
};

export type TurnValue = {
  /** Null when nothing has run in this window yet. */
  turn: LiveTurn | null;
  /** True while a stream is attached and no terminal phase has been reached. */
  running: boolean;
  /** True while a tool is mid-run inside this turn. Drives the sphere's EXECUTING. */
  executing: boolean;
  send: (input: SendInput) => void;
  /** Ask the core to stop at its next checkpoint. Not the same as `stopReading`. */
  cancel: () => Promise<void>;
  /** Stop reading the stream. The turn keeps running in the core. */
  stopReading: () => void;
  /** Re-read `/api/chat/active` and adopt whatever is running. */
  resync: () => Promise<void>;
  /** Forget the finished turn, so the console shows a clean slate. */
  clear: () => void;
  /** Non-empty when `send` was refused before a stream existed. */
  refusal: string;
};

const TurnContext = createContext<TurnValue | null>(null);

const TERMINAL: TurnPhase[] = ["idle", "complete", "failed", "interrupted", "dropped"];
/** True when the turn has stopped, whichever of the five ways it stopped. */
export const isTerminal = (phase: TurnPhase): boolean => TERMINAL.includes(phase);

function blank(prompt: string, streamed = false): LiveTurn {
  return {
    conversationId: "",
    prompt,
    text: "",
    reasoning: "",
    intent: null,
    routing: null,
    context: null,
    calls: [],
    notices: [],
    error: null,
    errorKind: "",
    phase: "sending",
    startedAt: Date.now(),
    finished: null,
    endReason: "",
    adopted: false,
    heard: "",
    heardBy: "",
    voice: null,
    segments: [],
    speech: [],
    speechErrors: [],
    streamed,
  };
}

/**
 * Apply one stream event.
 *
 * Written as a pure function of (turn, event) so the pairing rules are readable in
 * one place and testable without a socket. Every branch returns a new object —
 * mutating would not re-render, and the bug would look like "the model stopped
 * streaming".
 *
 * Takes `ConverseEvent`, which is `TurnEvent` plus the four the voice route adds. One
 * reducer for both routes on purpose: a second one would be the same forty lines with
 * its own drift, and the difference between a spoken turn and a typed one is which
 * events arrive, not what they mean.
 */
export function applyTurnEvent(turn: LiveTurn, event: ConverseEvent): LiveTurn {
  switch (event.type) {
    case "start":
      return {
        ...turn,
        conversationId: event.conversation_id,
        phase: "routing",
        // `spoken` is only on the voice route's `start`. Read from the event rather
        // than assumed from the request, so the flag that suppresses the
        // speak-it-afterwards path is the core's answer and not this side's hope.
        streamed: "spoken" in event ? Boolean(event.spoken) : turn.streamed,
      };

    case "heard":
      // The transcript is also the prompt: a spoken turn started with no prompt at
      // all, and showing the exchange without it would leave the answer replying to
      // nothing.
      return { ...turn, heard: event.text, heardBy: event.routed, prompt: turn.prompt || event.text };

    case "voice":
      return {
        ...turn,
        voice: { routed: event.routed, model: event.model, voice: event.voice },
      };

    case "segment":
      return { ...turn, segments: [...turn.segments, event] };

    case "speech": {
      const ready = playable(event);
      // A `speech` event that names no file is not audio. Recorded as a problem
      // rather than dropped, because "the core synthesised something it could not
      // name" is a real fault and silence is how it would otherwise present.
      if (!ready) {
        return {
          ...turn,
          speechErrors: [
            ...turn.speechErrors,
            {
              type: "speech_error",
              index: event.index,
              text: event.text,
              error: "The core synthesised this sentence but did not say where it is.",
            },
          ],
        };
      }
      return { ...turn, speech: [...turn.speech, ready] };
    }

    case "speech_error":
      return { ...turn, speechErrors: [...turn.speechErrors, event] };

    case "routing":
      return { ...turn, intent: event.intent, routing: event.routing, phase: "thinking" };

    case "context":
      return { ...turn, context: event.context, phase: "thinking" };

    case "text":
      // Phase moves back to `thinking` from `executing`: a tool has finished and the
      // model is composing again. Without this the sphere would stay on EXECUTING
      // for the rest of the turn after the first tool ran.
      return { ...turn, text: turn.text + event.text, phase: "thinking" };

    case "reasoning":
      return { ...turn, reasoning: turn.reasoning + event.text, phase: "thinking" };

    case "tool_call":
      return {
        ...turn,
        phase: "executing",
        calls: [...turn.calls, { call: event.call, preview: event.preview, result: null }],
      };

    case "tool_result": {
      const at = lastUnresolved(turn.calls);
      if (at === -1) {
        // No call is waiting for this. That is the malformed-arguments path: the
        // core rejected the model's JSON before it became a call, and yielded only
        // the result. Shown with a synthetic call row so the run is still visible
        // and still attributable to a tool name.
        return {
          ...turn,
          calls: [...turn.calls, { call: orphanCall(event.result), preview: null, result: event.result }],
        };
      }
      const calls = turn.calls.slice();
      const existing = calls[at];
      if (!existing) return turn;
      calls[at] = { ...existing, result: event.result };
      return { ...turn, calls };
    }

    case "notice":
      return { ...turn, notices: [...turn.notices, event.text] };

    case "error":
      // Not terminal on its own. The core can emit an error and still finish the
      // turn with partial text, and `done` is what says it is over — so the error
      // is recorded and the phase is left for `done`/`onEnd` to decide.
      return { ...turn, error: event.error, errorKind: event.kind };

    case "done": {
      const finished = event.turn;
      return {
        ...turn,
        conversationId: finished.conversation_id || turn.conversationId,
        // The core's own text wins over the accumulated deltas: it is what was
        // persisted, and a dropped delta would otherwise leave the screen showing
        // something different from the stored message.
        text: finished.text || turn.text,
        intent: finished.intent ?? turn.intent,
        routing: finished.routing ?? turn.routing,
        context: finished.context ?? turn.context,
        notices: finished.notices.length > 0 ? finished.notices : turn.notices,
        error: finished.error ?? turn.error,
        calls: reconcile(turn.calls, finished.tools),
        finished,
        phase: phaseOf(finished.status),
      };
    }

    default:
      return turn;
  }
}

/** The index of the call still waiting for a result, latest first. */
function lastUnresolved(calls: LiveCall[]): number {
  for (let index = calls.length - 1; index >= 0; index -= 1) {
    if (!calls[index]?.result) return index;
  }
  return -1;
}

function orphanCall(result: ToolRunInfo): ToolCallInfo {
  return {
    id: `orphan-${result.name}-${Math.random().toString(36).slice(2, 8)}`,
    name: result.name,
    arguments: result.arguments,
    raw_arguments: "",
    // `rejected` is where the core puts the reason a call never ran. Surfacing it
    // as `parse_error` is honest: this row exists precisely because there was no
    // well-formed call to show.
    parse_error: result.rejected ?? "The model's arguments could not be read.",
  };
}

/**
 * Make the call list agree with the core's final `tools` array.
 *
 * The stream is the truth while it is running, but `done` carries the record that
 * was stored — and if a `tool_result` frame was lost, the streamed list is short.
 * Extra rows from the record are appended rather than the list being replaced,
 * because the streamed rows carry previews and raw arguments the record does not.
 */
function reconcile(calls: LiveCall[], tools: ToolRunInfo[]): LiveCall[] {
  if (tools.length <= calls.length) {
    // Fill in any result the stream never delivered, positionally: the core appends
    // to `Turn.tools` in the same order it yields them.
    return calls.map((entry, index) => {
      if (entry.result) return entry;
      const fromRecord = tools[index];
      return fromRecord ? { ...entry, result: fromRecord } : entry;
    });
  }
  const merged = calls.map((entry, index) => {
    const fromRecord = tools[index];
    return entry.result || !fromRecord ? entry : { ...entry, result: fromRecord };
  });
  for (const extra of tools.slice(calls.length)) {
    merged.push({ call: orphanCall(extra), preview: null, result: extra });
  }
  return merged;
}

function phaseOf(status: FinishedTurn["status"]): TurnPhase {
  if (status === "complete") return "complete";
  if (status === "interrupted") return "interrupted";
  if (status === "failed") return "failed";
  // `streaming` in a `done` event means the core finished without setting a final
  // status. Treated as complete rather than as running, because the stream is over
  // either way and a permanent THINKING is the worse of the two wrong answers.
  return "complete";
}

/** Rebuild a `LiveTurn` from a turn the core says is running (§71 resync). */
export function adopt(active: FinishedTurn): LiveTurn {
  return {
    conversationId: active.conversation_id,
    prompt: "",
    text: active.text,
    reasoning: "",
    intent: active.intent,
    routing: active.routing,
    context: active.context,
    calls: active.tools.map((result) => ({
      call: orphanCall(result),
      preview: null,
      result,
    })),
    notices: active.notices,
    error: active.error,
    errorKind: "",
    phase: active.status === "streaming" ? "thinking" : phaseOf(active.status),
    startedAt: Date.now(),
    finished: null,
    endReason:
      active.status === "streaming"
        ? "This turn was already running when the window loaded, so its text is not streaming here. It will finish in the core."
        : "",
    adopted: true,
    // A turn adopted from `/api/chat/active` has no audio attached to it, and cannot:
    // the segments were spoken into a connection this window does not hold. Left empty
    // rather than reconstructed from the text, which would be a second synthesis of an
    // answer the user has already heard.
    heard: "",
    heardBy: "",
    voice: null,
    segments: [],
    speech: [],
    speechErrors: [],
    // True, so nothing speaks it now. The audio, if there was any, already played in
    // whatever window started this turn.
    streamed: true,
  };
}

export function TurnProvider({ children }: { children: ReactNode }) {
  const [turn, setTurn] = useState<LiveTurn | null>(null);
  const [refusal, setRefusal] = useState("");

  const stream = useRef<{ abort: () => void } | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      stream.current?.abort();
      stream.current = null;
    };
  }, []);

  const resync = useCallback(async () => {
    try {
      const result = await api.chat.active();
      if (!alive.current) return;
      const running = result.active[0];
      // No running turn is a real answer, not a failed one: it means the turn
      // finished while this window was away. The existing turn is left on screen
      // rather than blanked, because its text is still the last thing that happened.
      if (running) setTurn(adopt(running));
    } catch {
      // A failed resync must not invent a state. Whatever the turn already says —
      // including "the connection dropped" — stays, which is more accurate than
      // replacing it with a second error about the resync itself.
    }
  }, []);

  // On mount, exactly once: a reloaded window is the case §71 is about.
  useEffect(() => {
    void resync();
  }, [resync]);

  const send = useCallback((input: SendInput) => {
    // One at a time, matching the core's own rule. Guarded here as well so the
    // second click does not fire a request the core will answer with a 409.
    if (stream.current) return;
    setRefusal("");
    const spoken = Boolean(input.speak);
    setTurn(blank(input.message, spoken));

    const handlers = {
      onEvent: (event: ConverseEvent) => {
        if (!alive.current) return;
        setTurn((current) => (current ? applyTurnEvent(current, event) : current));
      },
      onEnd: (reason: string) => {
        stream.current = null;
        if (!alive.current) return;
        setTurn((current) => {
          if (!current) return current;
          if (isTerminal(current.phase)) {
            // `done` already settled it. An empty reason here is the normal path.
            return reason ? { ...current, endReason: reason } : current;
          }
          // The stream stopped without finishing the turn. `dropped` rather than
          // `failed`: the core may well still be working, and the honest offer is
          // to resync rather than to retry and run everything twice.
          return { ...current, phase: "dropped", endReason: reason };
        });
      },
    };

    // Two routes, one for each intention. `/api/voice/converse` is chosen when the
    // answer is to be spoken, because it is the route that speaks *while* the model
    // writes — the same turn, the same events, plus audio per sentence. Sending every
    // turn through it would mean paying for the routing of a TTS model on turns
    // nobody asked to hear.
    stream.current = spoken
      ? startConverse(
          {
            message: input.message,
            audio: input.audio ?? null,
            language: input.language ?? null,
            conversationId: input.conversationId ?? null,
            projectId: input.projectId ?? null,
            mode: input.mode ?? null,
            voice: input.voice ?? null,
            speak: true,
          },
          handlers,
        )
      : startTurn(input, handlers);
  }, []);

  const stopReading = useCallback(() => {
    stream.current?.abort();
    stream.current = null;
    setTurn((current) =>
      current && !isTerminal(current.phase)
        ? {
            ...current,
            phase: "dropped",
            endReason:
              "Stopped watching this turn. It is still running in the core — use Stop to cancel it.",
          }
        : current,
    );
  }, []);

  const cancel = useCallback(async () => {
    const conversationId = turn?.conversationId ?? "";
    if (!conversationId) {
      // The `start` event has not arrived, so the core has no id to cancel by. The
      // read is abandoned instead, and that difference is stated rather than a
      // cancel being faked.
      stopReading();
      return;
    }
    try {
      const result = await api.chat.cancel(conversationId);
      if (!alive.current) return;
      setTurn((current) =>
        current
          ? {
              ...current,
              endReason: result.stopped
                ? "Stopping at the next safe point. Anything that already ran was not undone."
                : result.message,
            }
          : current,
      );
    } catch (cause) {
      if (!alive.current) return;
      const message =
        cause instanceof ApiError
          ? cause.detail
            ? `${cause.message} ${cause.detail}`
            : cause.message
          : "Could not reach the core to cancel the turn.";
      setTurn((current) => (current ? { ...current, endReason: message } : current));
    }
  }, [turn?.conversationId, stopReading]);

  const clear = useCallback(() => {
    if (stream.current) return;
    setTurn(null);
    setRefusal("");
  }, []);

  const running = Boolean(turn && !isTerminal(turn.phase) && !turn.adopted);
  const executing = turn?.phase === "executing";

  const value = useMemo<TurnValue>(
    () => ({ turn, running, executing, send, cancel, stopReading, resync, clear, refusal }),
    [turn, running, executing, send, cancel, stopReading, resync, clear, refusal],
  );

  return <TurnContext.Provider value={value}>{children}</TurnContext.Provider>;
}

export function useTurn(): TurnValue {
  const value = useContext(TurnContext);
  if (!value) throw new Error("useTurn must be used inside <TurnProvider>.");
  return value;
}

/**
 * A one-line, honest account of where the turn is.
 *
 * Used under the sphere and in the console header, so both say the same words about
 * the same state (§60).
 */
export function turnSentence(turn: LiveTurn | null): string {
  if (!turn) return "";
  switch (turn.phase) {
    case "sending":
      return "Sending to the core.";
    case "routing":
      return "Choosing a model.";
    case "thinking":
      return turn.routing ? `${turn.routing.label} is answering.` : "Working.";
    case "executing": {
      const open = turn.calls.find((entry) => !entry.result);
      return open ? `Running ${open.call.name}.` : "Running a tool.";
    }
    case "complete":
      return turn.error ? `Finished with a problem: ${turn.error}` : "Done.";
    case "failed":
      return turn.error ?? "The turn failed.";
    case "interrupted":
      return "Stopped. Anything that already ran was not undone.";
    case "dropped":
      return turn.endReason || "The stream ended without finishing the turn.";
    default:
      return "";
  }
}
