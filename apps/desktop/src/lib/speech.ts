/**
 * Playing an answer while it is still being written (§12, §61).
 *
 * `POST /api/voice/converse` sends a `speech` event per sentence, the first of them
 * while the model is still typing. That is the whole point of the route, and it
 * hands this side a problem the old "speak the finished answer" path never had:
 * audio now arrives as a *sequence*, and a sequence can be got wrong.
 *
 * So this module is one small state machine with one job -- decide what plays next
 * -- and it is deliberately free of the DOM so that decision can be run and checked
 * without a sound card (`npm run test:speech`).
 *
 * Four rules, each of which exists because the obvious implementation breaks it.
 *
 * 1. **Strictly one at a time, strictly by index.** Two `Audio` elements playing
 *    sentence three and sentence four together is not a faster answer, it is an
 *    unintelligible one. `play()` is only called again once the previous file has
 *    finished, and only for the index that comes next.
 * 2. **A sentence with no audio must not stall the ones after it.** The core sends
 *    `speech_error` for a segment it could not synthesise, and that index will never
 *    arrive. Waiting for it forever would turn one failed sentence into silence for
 *    the rest of the answer, so :meth:`skip` fills the hole and the queue moves on.
 * 3. **When the stream is over, whatever is left is played.** While a turn is live,
 *    a missing index means "not synthesised yet" and waiting is right. Once the
 *    stream has ended it means "never coming" -- a lost frame, a worker that died --
 *    and :meth:`close` switches the queue from waiting for the next index to playing
 *    the lowest one it has. Strict order while it can be, no permanent silence when
 *    it cannot.
 * 4. **A browser that refuses to autoplay is said once, not per sentence.** Every
 *    engine blocks programmatic playback until the page has been interacted with.
 *    Reporting that per segment would produce one identical complaint per sentence
 *    for the rest of the turn, so the first rejection stops the queue and is stated
 *    once -- which is also the only honest thing to show, because nothing after it
 *    would have played either.
 */

/** Audio for one segment: the core's `speech` event, reduced to what playback needs. */
export type SpokenSegment = {
  /** The segment's position in the answer. The queue plays in this order. */
  index: number;
  /** What is being said, for a shell that wants to show the spoken line. */
  text: string;
  /** `/api/voice/audio/{name}` — the route that serves the file the core wrote. */
  url: string;
};

/** The browser declined to start playback until the page has been interacted with. */
export class PlaybackBlocked extends Error {
  constructor(message = "playback was blocked") {
    super(message);
    this.name = "PlaybackBlocked";
  }
}

/** The file exists and could not be played, which is a different problem. */
export class PlaybackFailed extends Error {
  constructor(message = "the audio could not be played") {
    super(message);
    this.name = "PlaybackFailed";
  }
}

/**
 * Whatever actually makes sound.
 *
 * An interface rather than an `Audio` element so the ordering above can be tested
 * by running it. `play` must resolve when the file has finished playing -- not when
 * it has started -- because "finished" is what lets the next segment begin.
 */
export type Player = {
  play: (url: string) => Promise<void>;
  stop: () => void;
};

export type SpeechHandlers = {
  /** A segment has begun playing. */
  onStart?: (segment: SpokenSegment) => void;
  /** Nothing is playing and nothing is waiting. */
  onIdle?: () => void;
  /** Playback failed. `blocked` means the browser refused, not that audio is broken. */
  onProblem?: (message: string, blocked: boolean) => void;
};

const BLOCKED_SENTENCE =
  "The browser would not start playback on its own. Click anywhere in the window once, and answers will be spoken from then on.";

export class SpeechQueue {
  private readonly player: Player;
  private readonly handlers: SpeechHandlers;
  /** Index → audio, or null for an index that will never have any. */
  private waiting = new Map<number, SpokenSegment | null>();
  private next = 0;
  private playing = false;
  /** True while the turn is live, so a gap means "not yet" rather than "never". */
  private strict = true;
  private stopped = false;

  constructor(player: Player, handlers: SpeechHandlers = {}) {
    this.player = player;
    this.handlers = handlers;
  }

  /** Audio for a segment is ready. */
  push(segment: SpokenSegment): void {
    if (this.stopped) return;
    // A segment already spoken (or already skipped past) cannot be inserted behind
    // the playhead -- playing it now would say a sentence out of order, which is the
    // one failure this class exists to prevent.
    if (segment.index < this.next) return;
    this.waiting.set(segment.index, segment);
    this.pump();
  }

  /** This segment has no audio, and never will. Rule 2. */
  skip(index: number): void {
    if (this.stopped || index < this.next) return;
    if (!this.waiting.has(index)) this.waiting.set(index, null);
    this.pump();
  }

  /**
   * The stream has ended: play whatever is left, in order, without waiting for
   * indices that are not coming. Rule 3.
   */
  close(): void {
    this.strict = false;
    this.pump();
  }

  /** Stop now and drop the backlog. A new answer is not spoken over the old one. */
  stop(): void {
    this.stopped = true;
    this.waiting.clear();
    this.player.stop();
  }

  /** Ready for another turn: the playhead returns to the first sentence. */
  reset(): void {
    this.stop();
    this.stopped = false;
    this.playing = false;
    this.strict = true;
    this.next = 0;
  }

  /** True while a file is playing. Not "true while audio exists". */
  get active(): boolean {
    return this.playing;
  }

  /** How many segments are waiting for their turn. */
  get pending(): number {
    return this.waiting.size;
  }

  private take(): SpokenSegment | null {
    for (;;) {
      if (this.waiting.has(this.next)) {
        const found = this.waiting.get(this.next) ?? null;
        this.waiting.delete(this.next);
        this.next += 1;
        if (found) return found;
        continue;
      }
      if (this.strict || this.waiting.size === 0) return null;
      // Past the end of the stream with a hole in the sequence: the missing index is
      // never arriving, so the playhead moves to the lowest index that did.
      let lowest = Number.POSITIVE_INFINITY;
      for (const index of this.waiting.keys()) if (index < lowest) lowest = index;
      this.next = lowest;
    }
  }

  private pump(): void {
    if (this.playing || this.stopped) return;
    const segment = this.take();
    if (!segment) {
      this.handlers.onIdle?.();
      return;
    }
    this.playing = true;
    this.handlers.onStart?.(segment);
    void this.player
      .play(segment.url)
      .then(() => {
        this.playing = false;
        this.pump();
      })
      .catch((cause: unknown) => {
        this.playing = false;
        if (cause instanceof PlaybackBlocked) {
          // Rule 4: nothing after this would have played either, so the queue ends
          // here and the reason is given once.
          this.handlers.onProblem?.(BLOCKED_SENTENCE, true);
          this.stop();
          return;
        }
        const message =
          cause instanceof Error && cause.message
            ? `A spoken sentence could not be played: ${cause.message}`
            : "A spoken sentence could not be played.";
        this.handlers.onProblem?.(message, false);
        // One unplayable file is not the end of the answer -- keep going.
        this.pump();
      });
  }
}

export { BLOCKED_SENTENCE };
