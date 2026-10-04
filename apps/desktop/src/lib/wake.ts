/**
 * Hands-free listening, as arithmetic rather than as a promise (§9, §12, §61, §71).
 *
 * A wake word is the difference between an assistant you operate and one you talk to.
 * The hard part is not matching the word -- it is deciding what audio is worth paying
 * a cloud transcription for, because this build has no local speech model. Every
 * decision below exists to keep that bill honest and bounded.
 *
 * 1. **The gate is local and the transcription is remote.** Loudness is measured in
 *    this process from raw samples. Only a segment that crossed the room's own noise
 *    floor and lasted long enough to be a phrase is ever uploaded. Silence, fan noise
 *    and a door closing cost nothing.
 *
 * 2. **The noise floor is measured, not assumed.** A fixed threshold works in one
 *    room and fails in the next. The floor is averaged over the first second and then
 *    tracked slowly while nothing is being said, so a machine that starts a fan does
 *    not turn into a machine that transcribes a fan.
 *
 * 3. **There is a hard ceiling on how often audio can be sent.** A television left on
 *    is speech, and a speech-shaped noise source would otherwise transcribe forever.
 *    `UtteranceBudget` stops the listener instead, so the failure mode is "hands-free
 *    stopped and said why" rather than an invoice.
 *
 * 4. **The wake word must be at the head of what was heard.** Matching it anywhere
 *    would make every answer that mentions NEXUS a command. Four tokens of slack is
 *    enough for "um, nexus" and "hey nexus" without turning prose into instructions.
 *
 * 5. **Nothing here touches the DOM or React.** The state machine is a function of
 *    (level, timestamp), so it is verifiable without a microphone -- which matters,
 *    because the alternative is shipping a feature whose only test is a room.
 */

/** Root mean square of one block of samples, in the same 0..1 scale as the samples. */
export function rmsOf(samples: Float32Array): number {
  if (samples.length === 0) return 0;
  let sum = 0;
  for (let index = 0; index < samples.length; index += 1) {
    const value = samples[index] ?? 0;
    sum += value * value;
  }
  return Math.sqrt(sum / samples.length);
}

/**
 * Punctuation a transcriber adds that a comparison must not see.
 *
 * Two classes, because they behave differently. Quotes and apostrophes are *deleted*:
 * they sit inside a word, and spacing them would split "nexus’s" into two tokens and
 * stop it matching the wake word. Everything else becomes a space, because it sits
 * between words and "nexus,open" is two of them.
 */
const POSSESSIVE = /['‘’]s\b/g;
const INSIDE_WORD = /['"‘’“”]/g;
const BETWEEN_WORDS = /[.,!?;:()\[\]{}…—–]/g;

/** Lowercase, drop punctuation, collapse whitespace. Comparison form only. */
export function normalizeHeard(text: string): string {
  return text
    .toLowerCase()
    // "nexus's" is the same word as "nexus" for the purpose of deciding who was
    // addressed. Dropped before the apostrophe is, or it would become "nexuss".
    .replace(POSSESSIVE, "")
    .replace(INSIDE_WORD, "")
    .replace(BETWEEN_WORDS, " ")
    .replace(/\s+/g, " ")
    .trim();
}

export type WakeMatch = {
  /** True when the wake word was found at the head of the utterance. */
  addressed: boolean;
  /** What followed the wake word, in its original words. Empty when nothing did. */
  command: string;
  /** The whole utterance, trimmed, for a UI that wants to show what was heard. */
  heard: string;
};

/**
 * Decide whether an utterance was addressed to NEXUS, and what it asked for.
 *
 * The wake word may be several words ("hey nexus"). It has to begin within the first
 * `slack` tokens, which allows a filler word in front of it and refuses a sentence
 * that merely mentions the name. The command is returned in its *original* casing and
 * punctuation, because it is about to be sent to a model as an instruction and
 * "open Firefox" is not "open firefox" to a shell command.
 */
export function matchWake(transcript: string, wakeWord: string, slack = 4): WakeMatch {
  const heard = transcript.trim();
  const wake = normalizeHeard(wakeWord);
  if (!heard || !wake) return { addressed: false, command: "", heard };

  const raw = heard.split(/\s+/);
  const norm = raw.map((token) => normalizeHeard(token));
  const wanted = wake.split(" ");

  const limit = Math.min(raw.length, slack);
  for (let start = 0; start < limit; start += 1) {
    let hit = true;
    for (let offset = 0; offset < wanted.length; offset += 1) {
      if (norm[start + offset] !== wanted[offset]) {
        hit = false;
        break;
      }
    }
    if (!hit) continue;
    const rest = raw.slice(start + wanted.length).join(" ");
    // A leading comma or full stop belongs to the wake word ("Nexus, open...") and
    // would otherwise be the first character of the instruction.
    const command = rest.replace(/^[\s,.:;!?—–-]+/, "").trim();
    return { addressed: true, command, heard };
  }
  return { addressed: false, command: "", heard };
}

/** What the gate decided about the block just fed to it. */
export type GateVerdict =
  /** Still learning the room. Nothing is captured during this. */
  | "calibrating"
  /** Below the floor, nothing open. */
  | "quiet"
  /** Speech started with this block. The caller begins capturing. */
  | "open"
  /** Speech is continuing. */
  | "hold"
  /** Speech ended and the segment is long enough to be worth transcribing. */
  | "close"
  /** Speech ended too soon to be a phrase -- a cough, a keystroke. Throw it away. */
  | "discard"
  /** The segment hit the ceiling. Closed here so one long noise cannot cost forever. */
  | "cap";

export type GateOptions = {
  /** How long the floor is measured before anything can open. */
  calibrationMs?: number;
  /** Speech must exceed the floor by this factor. */
  floorFactor?: number;
  /** A floor of zero (a muted device) must not make everything speech. */
  minThreshold?: number;
  /** Silence this long ends a segment. */
  silenceMs?: number;
  /** Shorter than this is not a phrase. */
  minUtteranceMs?: number;
  /** Longer than this is closed anyway. */
  maxUtteranceMs?: number;
};

const GATE_DEFAULTS: Required<GateOptions> = {
  calibrationMs: 1_000,
  floorFactor: 2.5,
  minThreshold: 0.012,
  silenceMs: 900,
  minUtteranceMs: 350,
  maxUtteranceMs: 12_000,
};

/**
 * Voice activity, as a state machine over (level, now).
 *
 * Two frames above the threshold are required to open, so a single sample spike does
 * not start a recording; one continuous stretch of silence closes it. The floor keeps
 * adapting while nothing is open -- slowly, at 2% per block, so a person speaking does
 * not raise the floor to meet themselves and shut the gate mid-sentence.
 */
export class SpeechGate {
  private readonly options: Required<GateOptions>;
  private startedAt = 0;
  private floorSum = 0;
  private floorCount = 0;
  private floor = 0;
  private ready = false;
  private open = false;
  private loudBlocks = 0;
  private openedAt = 0;
  private lastLoudAt = 0;

  constructor(options: GateOptions = {}) {
    this.options = { ...GATE_DEFAULTS, ...options };
  }

  /** Begin (or restart) calibration. `now` is a monotonic millisecond clock. */
  start(now: number): void {
    this.startedAt = now;
    this.floorSum = 0;
    this.floorCount = 0;
    this.floor = 0;
    this.ready = false;
    this.open = false;
    this.loudBlocks = 0;
    this.openedAt = 0;
    this.lastLoudAt = 0;
  }

  /** The quiet the gate waits out before calling a phrase finished. */
  get silenceMs(): number {
    return this.options.silenceMs;
  }

  /** The level a block must beat to count as speech. Zero until calibrated. */
  get threshold(): number {
    if (!this.ready) return 0;
    return Math.max(this.options.minThreshold, this.floor * this.options.floorFactor);
  }

  /** Milliseconds of audio in the segment currently open. Zero when none is. */
  openFor(now: number): number {
    return this.open ? now - this.openedAt : 0;
  }

  feed(level: number, now: number): GateVerdict {
    if (!this.ready) {
      this.floorSum += level;
      this.floorCount += 1;
      if (now - this.startedAt < this.options.calibrationMs) return "calibrating";
      this.floor = this.floorCount > 0 ? this.floorSum / this.floorCount : 0;
      this.ready = true;
      return "calibrating";
    }

    const loud = level >= this.threshold;

    if (!this.open) {
      // Track the room while it is quiet. Only quiet blocks move the floor, or a
      // sentence would raise the bar it is being measured against.
      if (!loud) this.floor = this.floor * 0.98 + level * 0.02;
      this.loudBlocks = loud ? this.loudBlocks + 1 : 0;
      if (this.loudBlocks < 2) return "quiet";
      this.open = true;
      this.openedAt = now;
      this.lastLoudAt = now;
      this.loudBlocks = 0;
      return "open";
    }

    if (loud) this.lastLoudAt = now;
    if (now - this.openedAt >= this.options.maxUtteranceMs) {
      this.open = false;
      return "cap";
    }
    if (now - this.lastLoudAt < this.options.silenceMs) return "hold";
    this.open = false;
    const spoken = this.lastLoudAt - this.openedAt;
    return spoken >= this.options.minUtteranceMs ? "close" : "discard";
  }
}

/**
 * A ceiling on how much audio hands-free listening may send.
 *
 * Not a rate limiter that drops the extra -- a limiter that *stops*. Something
 * producing more than this is not a person talking to their computer, and quietly
 * discarding half of it would leave hands-free half-working with no explanation.
 */
export class UtteranceBudget {
  private readonly stamps: number[] = [];

  constructor(
    private readonly limit = 20,
    private readonly windowMs = 60_000,
  ) {}

  /** Record an utterance about to be sent. False means the ceiling has been hit. */
  allow(now: number): boolean {
    while (this.stamps.length > 0 && now - (this.stamps[0] ?? 0) > this.windowMs) {
      this.stamps.shift();
    }
    if (this.stamps.length >= this.limit) return false;
    this.stamps.push(now);
    return true;
  }

  /** How many are left in the current window, for a UI that wants to say so. */
  remaining(now: number): number {
    while (this.stamps.length > 0 && now - (this.stamps[0] ?? 0) > this.windowMs) {
      this.stamps.shift();
    }
    return Math.max(0, this.limit - this.stamps.length);
  }

  reset(): void {
    this.stamps.length = 0;
  }
}

/**
 * Turn captured float samples into a 16-bit PCM WAV file.
 *
 * Written here rather than reaching for `MediaRecorder` because the wake path needs
 * *pre-roll*: the gate only knows speech started after it has already started, so the
 * first syllable of the wake word is in the past by then. A ring buffer of raw samples
 * can be replayed; a WebM stream cannot be sliced from the middle and still decode.
 *
 * 16-bit mono at the capture rate. No resampling: a naive decimation would alias, and
 * an aliased wake word is a wake word that stops working in exactly the way that is
 * hardest to explain.
 */
export function encodeWav(blocks: Float32Array[], sampleRate: number): ArrayBuffer {
  let count = 0;
  for (const block of blocks) count += block.length;

  const buffer = new ArrayBuffer(44 + count * 2);
  const view = new DataView(buffer);

  const ascii = (at: number, text: string): void => {
    for (let index = 0; index < text.length; index += 1) {
      view.setUint8(at + index, text.charCodeAt(index));
    }
  };

  ascii(0, "RIFF");
  view.setUint32(4, 36 + count * 2, true);
  ascii(8, "WAVE");
  ascii(12, "fmt ");
  view.setUint32(16, 16, true); // PCM header length
  view.setUint16(20, 1, true); // format: uncompressed PCM
  view.setUint16(22, 1, true); // channels
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // bytes per second
  view.setUint16(32, 2, true); // bytes per frame
  view.setUint16(34, 16, true); // bits per sample
  ascii(36, "data");
  view.setUint32(40, count * 2, true);

  let at = 44;
  for (const block of blocks) {
    for (let index = 0; index < block.length; index += 1) {
      // Clamp before scaling: a sample above 1.0 (which a gain stage can produce)
      // would wrap around and become a loud click at the opposite polarity.
      const sample = Math.max(-1, Math.min(1, block[index] ?? 0));
      view.setInt16(at, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
      at += 2;
    }
  }
  return buffer;
}

/**
 * The last `ms` milliseconds of audio, kept so a segment can include what came before
 * the gate noticed it.
 *
 * Bounded by construction: blocks are dropped from the front once the held duration
 * exceeds the window, so an armed microphone has a fixed memory cost no matter how
 * long it stays armed.
 */
export class PreRoll {
  private readonly blocks: Float32Array[] = [];
  private held = 0;
  private readonly limit: number;

  constructor(ms: number, sampleRate: number) {
    this.limit = Math.max(1, Math.round((ms / 1000) * sampleRate));
  }

  push(block: Float32Array): void {
    this.blocks.push(block);
    this.held += block.length;
    while (this.held - (this.blocks[0]?.length ?? 0) >= this.limit && this.blocks.length > 1) {
      this.held -= this.blocks.shift()?.length ?? 0;
    }
  }

  /** Everything held, oldest first. The caller owns the array; this keeps its own. */
  drain(): Float32Array[] {
    const held = this.blocks.slice();
    this.blocks.length = 0;
    this.held = 0;
    return held;
  }

  get samples(): number {
    return this.held;
  }
}

/**
 * Drop the trailing silence a closed segment necessarily ends with.
 *
 * The gate can only know a phrase ended by waiting out `silenceMs` of quiet, so every
 * segment it closes carries that quiet on the end -- about a third of a short phrase.
 * Uploading it costs bytes and transcription time and adds nothing, so it is cut here,
 * with a small margin kept: a final consonant sits in the first fraction of what looks
 * like silence, and clipping it turns "processes" into "processe".
 */
export function trimTail(
  blocks: Float32Array[],
  sampleRate: number,
  dropMs: number,
  keepMs = 200,
): Float32Array[] {
  const drop = Math.round(((dropMs - keepMs) / 1000) * sampleRate);
  if (drop <= 0) return blocks;
  const kept = blocks.slice();
  let left = drop;
  while (left > 0 && kept.length > 1) {
    const last = kept[kept.length - 1];
    if (!last) break;
    if (last.length <= left) {
      left -= last.length;
      kept.pop();
      continue;
    }
    kept[kept.length - 1] = last.subarray(0, last.length - left);
    left = 0;
  }
  return kept;
}
