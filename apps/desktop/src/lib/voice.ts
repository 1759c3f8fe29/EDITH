/**
 * Hearing, thinking and speaking as one stream (§9, §12, §61).
 *
 * `POST /api/voice/converse` is the voice-first turn: it transcribes the recording,
 * streams the answer token by token, and streams audio *sentence by sentence* while
 * the model is still writing. This module is the client for it.
 *
 * Why it exists next to `./chat` rather than inside it: the two routes carry the
 * same turn events, but this one carries four more, and the difference is not
 * cosmetic. `heard` is what the machine believed the user said -- the one thing a
 * voice interface must show, because a misheard word explains an answer that
 * otherwise looks like the model being stupid (§61). `segment` says a sentence has
 * been cut and is being synthesised, so the UI can show the queue instead of a
 * spinner. `speech` and `speech_error` are the audio and the reason there is none.
 *
 * What this module does *not* do is decide what plays when. That is `./speech`,
 * which is pure and tested; here the events are typed and the queue is fed.
 *
 * One honest limitation, stated because the alternative is implying otherwise:
 * hearing is per *utterance*, not per frame. The recording is transcribed in one
 * request because Mistral's ASR is request/response and NVIDIA's streaming ASR
 * (Parakeet) is gRPC-only with no HTTP endpoint to reach it through. So the user
 * finishes speaking, and from that moment everything is streamed.
 */

import { openStream, type StreamHandlers } from "./sse";
import {
  PlaybackBlocked,
  PlaybackFailed,
  type Player,
  type SpokenSegment,
} from "./speech";
import type { TurnEvent } from "./chat";

/**
 * What the core heard, before it answers it.
 *
 * `routed` names the model that transcribed, so a bad transcription is
 * attributable to a model rather than to "voice input not working".
 */
export type HeardEvent = { type: "heard"; text: string; routed: string };

/** Which model is speaking, and in which voice. Sent once, before the first segment. */
export type VoiceRoutedEvent = {
  type: "voice";
  routed: string;
  model: string;
  voice: string;
};

/**
 * A sentence has been cut and is on its way to being synthesised.
 *
 * `source` is non-null only for code: the segmenter says "a code block" out loud
 * rather than reciting it, and the source is sent so the transcript can show what
 * the voice declined to read.
 */
export type SegmentEvent = {
  type: "segment";
  index: number;
  text: string;
  kind: string;
  source: string | null;
};

/** Audio for one segment. `path` is for the desktop shell, `url` for a web view. */
export type SpeechEvent = {
  type: "speech";
  index: number;
  text: string;
  kind: string;
  path: string | null;
  url: string | null;
};

/** This sentence has no audio, and why. The turn continues. */
export type SpeechErrorEvent = {
  type: "speech_error";
  index: number;
  text: string;
  error: string;
};

/**
 * The `start` event of a voice turn.
 *
 * `spoken` repeats what was asked for rather than being assumed, so a turn that was
 * requested silent cannot be rendered with a speaker icon that will never light.
 */
export type VoiceStartEvent = {
  type: "start";
  conversation_id: string;
  spoken: boolean;
};

/**
 * Everything the route can send: the chat turn's events plus this route's own.
 *
 * `TurnEvent` is included whole, `start` and all, rather than with its `start`
 * excluded in favour of the wider one. Both routes really do send a `start`, and only
 * one of them puts `spoken` on it — so narrowing on `type === "start"` gives a union
 * whose extra field has to be tested for (`"spoken" in event`). That is the shape of
 * the wire, and a type that hid it would let a reader of `/api/chat` events read
 * `spoken` and get `undefined`.
 */
export type ConverseEvent =
  | VoiceStartEvent
  | HeardEvent
  | VoiceRoutedEvent
  | SegmentEvent
  | SpeechEvent
  | SpeechErrorEvent
  | TurnEvent;

export type ConverseInput = {
  /** Typed text. Either this or `audio` must be present; both is not an error.  */
  message?: string;
  /** Base64 of a recording, as `MediaRecorder` produced it. */
  audio?: string | null;
  /** BCP-47ish hint passed to the transcriber, so it is told rather than guessing. */
  language?: string | null;
  conversationId?: string | null;
  projectId?: string | null;
  mode?: string | null;
  /** Direct model key (provider:model) to bypass the router. Empty = auto (use router). */
  model?: string | null;
  /** Empty means the `tts_voice` setting, then the provider default. */
  voice?: string | null;
  /** False runs the same turn with synthesis off -- a choice, not a limitation. */
  speak?: boolean;
};

export type ConverseHandlers = StreamHandlers<ConverseEvent>;

/**
 * Start a voice turn. `abort()` stops reading, which stops the *voice* -- the core
 * treats a closed connection as "nobody is listening" and finishes the answer
 * anyway. Stopping the turn itself is `api.chat.cancel(conversationId)`.
 */
export function startConverse(
  input: ConverseInput,
  handlers: ConverseHandlers,
): { abort: () => void } {
  return openStream<ConverseEvent>({
    url: "/api/voice/converse",
    body: {
      message: input.message ?? null,
      audio: input.audio ?? null,
      language: input.language ?? null,
      conversation_id: input.conversationId ?? null,
      project_id: input.projectId ?? null,
      mode: input.mode ?? null,
      model: input.model ?? null,
      voice: input.voice ?? null,
      speak: input.speak ?? true,
    },
    handlers,
    refusal: (error, kind) => ({ type: "error", error, kind }),
  });
}

/** A `speech` event reduced to something playable, or null when it named no file. */
export function playable(event: SpeechEvent): SpokenSegment | null {
  // `url` and not `path`: a web view cannot open a filesystem path, and the route
  // that serves the file (`/api/voice/audio/{name}`) works in both shells. A missing
  // url means the core synthesised something it could not name, which is worth
  // skipping rather than guessing a path for.
  if (!event.url) return null;
  return { index: event.index, text: event.text, url: event.url };
}

/**
 * A `Player` backed by one `Audio` element for the whole window.
 *
 * One element, not one per segment: two overlapping voices are not more responsive,
 * they are unintelligible. The promise resolves on `ended`, which is what lets the
 * queue start the next sentence, and rejects with `PlaybackBlocked` when the browser
 * refuses to autoplay -- a different problem from a file that will not play, and one
 * the user can actually fix by clicking once.
 */
export function browserPlayer(): Player {
  let element: HTMLAudioElement | null = null;
  let pending: (() => void) | null = null;

  const settle = () => {
    const done = pending;
    pending = null;
    if (done) done();
  };

  return {
    play(url: string): Promise<void> {
      if (typeof Audio === "undefined") {
        return Promise.reject(new PlaybackFailed("this shell has no audio element"));
      }
      const audio = (element ??= new Audio());
      audio.pause();
      audio.src = url;
      return new Promise<void>((resolve, reject) => {
        pending = resolve;
        audio.onended = () => settle();
        audio.onerror = () => {
          pending = null;
          reject(new PlaybackFailed("the file could not be played"));
        };
        audio.play().catch(() => {
          pending = null;
          reject(new PlaybackBlocked());
        });
      });
    },
    stop(): void {
      const audio = element;
      if (!audio) return;
      // Handlers first: clearing `src` makes some engines fire `error`, and an error
      // for audio we deliberately stopped would be reported to the user as a fault.
      audio.onended = null;
      audio.onerror = null;
      audio.pause();
      audio.removeAttribute("src");
      // Resolve rather than reject: playback did end, just early. Rejecting would
      // put "a spoken sentence could not be played" on screen for a stop the user
      // asked for.
      settle();
    },
  };
}
