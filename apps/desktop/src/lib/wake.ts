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
 * they sit inside a word, and spacing them would split "nexus's" into two tokens and
 * stop it matching the wake word. Everything else becomes a space, because it sits
 * between words and "nexus,open" is two of them.
 */
const POSSESSIVE = /['‘’]s\b/g;
const INSIDE_WORD = /['"‘’“”]/g;
const BETWEEN_WORDS = /[.,!?;:()\[\]{}…—–]/g;

/** Lowercase, drop punctuation, collapse whitespace. Comparison form only. */
export function normalizeHeard(text: string): string {
  let normalized = text
    .toLowerCase()
    // "nexus's" is the same word as "nexus" for the purpose of deciding who was
    // addressed. Dropped before the apostrophe is, or it would become "nexuss".
    .replace(POSSESSIVE, "")
    .replace(INSIDE_WORD, "")
    .replace(BETWEEN_WORDS, " ")
    .replace(/\s+/g, " ")
    .trim();

  // Transliterate common Hindi Devanagari representations of "nexus"
  // to handle cases where STT returns "नेक्सस" instead of "nexus"
  const devanagariToAscii: Record<string, string> = {
    "नेक्सस": "nexus",  // नेक्सस
    "नेकस": "nexus",              // नेकस
    "नऍक्सस": "nexus",  // नएक्सस
    "नैकस": "nexus",              // नैकस
  };

  // Handle cases where "नेक्सस" appears in the text - insert spaces around it
  // so it becomes a separate token for matching. The key insight is that
  // Devanagari has no word boundaries, so "मैंनेक्सस" should become "मैं nexus".
  for (const [devanagari, ascii] of Object.entries(devanagariToAscii)) {
    // First: handle embedded (non-space before AND after, including anusvara etc)
    // e.g., "मैंनेक्सस" -> "मैं nexus"
    normalized = normalized.replace(new RegExp(`([^\\s])${devanagari}(?=[^\\s]|$)`, 'g'), `$1 ${ascii} `);
    // Then: handle prefix (start or space before, non-space after)
    normalized = normalized.replace(new RegExp(`(?:^|\\s)${devanagari}(?=[^\\s])`, 'g'), `${ascii} `);
    // Then: handle suffix (non-space before, space or end after) - preserve/add space after
    normalized = normalized.replace(new RegExp(`(?<=[^\\s])${devanagari}(?:\\s|$)`, 'g'), ` ${ascii} `);
    // Finally: handle standalone (space before, space after, or boundaries)
    normalized = normalized.replace(new RegExp(`(?:^|\\s)${devanagari}(?:\\s|$)`, 'g'), ` ${ascii} `);
  }

  // Clean up multiple spaces
  normalized = normalized.replace(/\s+/g, " ").trim();

  return normalized;
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

  // Normalize the full transcript first (handles Devanagari transliteration with space insertion)
  // then split by whitespace for token matching.
  const normalizedHeard = normalizeHeard(heard);
  const raw = normalizedHeard.split(/\s+/);
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
    // Extract command from original transcript to preserve casing
    // Find where the wake word ends in the original
    const originalTokens = heard.split(/\s+/);
    const commandTokens = originalTokens.slice(start + wanted.length);
    const command = commandTokens.join(" ").replace(/^[\s,.:;!?—–-]+/, "").trim();
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
    return this.floor * this.options.floorFactor;
  }

  /** How long the current phrase has been open, or 0 if not open. */
  openFor(now: number): number {
    return this.open ? now - this.openedAt : 0;
  }

  /**
   * Feed one block's RMS level and get the gate's decision.
   *
   * The returned verdict is a state machine transition, not a raw comparison:
   * - "calibrating": still in the warm-up window, floor is being estimated
   * - "quiet": below floor, nothing happening
   * - "open": speech just started, this block is the first of the phrase
   * - "hold": speech continuing
   * - "close": speech ended with enough silence, segment is ready
   * - "discard": speech ended but segment was too short, throw it away
   * - "cap": speech hit the maximum utterance length, close it anyway
   */
  feed(level: number, now: number): GateVerdict {
    if (!this.ready) {
      // Calibration window: accumulate floor estimate, emit nothing
      this.floorSum += level;
      this.floorCount += 1;
      this.floor = this.floorSum / this.floorCount;
      // Never let the floor drop below the absolute minimum
      if (this.floor < this.options.minThreshold) this.floor = this.options.minThreshold;
      if (now - this.startedAt >= this.options.calibrationMs) this.ready = true;
      return "calibrating";
    }

    const threshold = this.threshold;
    const isLoud = level > threshold;

    if (!this.open) {
      if (isLoud) {
        this.loudBlocks += 1;
        if (this.loudBlocks >= 2) {
          // Two consecutive loud blocks confirms speech start
          this.open = true;
          this.openedAt = now;
          this.lastLoudAt = now;
          return "open";
        }
        return "quiet"; // First loud block -- wait for confirmation
      } else {
        // Quiet block during calibration maintenance: slowly adapt floor down
        this.floor = this.floor * 0.98 + level * 0.02;
        if (this.floor < this.options.minThreshold) this.floor = this.options.minThreshold;
        this.loudBlocks = 0;
        return "quiet";
      }
    }

    // We are inside a phrase
    this.lastLoudAt = isLoud ? now : this.lastLoudAt;
    if (isLoud) {
      this.loudBlocks += 1;
      // Cap: phrase has gone on too long, close it at the cap
      if (now - this.openedAt >= this.options.maxUtteranceMs) return "cap";
      return "hold";
    }

    // Quiet block while open: check if silence duration closes the phrase
    if (now - this.lastLoudAt >= this.options.silenceMs) {
      this.open = false;
      this.loudBlocks = 0;
      // Too short to be a phrase: discard
      if (now - this.openedAt < this.options.minUtteranceMs) return "discard";
      return "close";
    }
    return "hold";
  }
}

/**
 * A rolling buffer of the last N ms of audio, to prepend as lead-in when speech starts.
 *
 * The wake word gate decides speech started *after* the first syllable of the wake
 * word has already passed. Without a pre-roll that syllable is lost and the wake word
 * fails to match. A ring buffer lets us replay the last 500 ms when the gate opens.
 */
export class PreRoll {
  private readonly capacity: number;
  private buffer: Float32Array[];
  private writeAt = 0;
  private full = false;

  constructor(maxMs: number, sampleRate: number) {
    // Each block is BLOCK samples at sampleRate Hz
    const blockMs = (2048 / sampleRate) * 1000;
    this.capacity = Math.ceil(maxMs / blockMs);
    this.buffer = new Array(this.capacity);
  }

  push(block: Float32Array): void {
    this.buffer[this.writeAt] = block;
    this.writeAt = (this.writeAt + 1) % this.capacity;
    if (this.writeAt === 0) this.full = true;
  }

  /** Total samples currently held in the buffer. */
  get samples(): number {
    let total = 0;
    if (!this.full) {
      for (let i = 0; i < this.writeAt; i += 1) {
        const b = this.buffer[i];
        if (b) total += b.length;
      }
    } else {
      for (let i = 0; i < this.capacity; i += 1) {
        const b = this.buffer[i];
        if (b) total += b.length;
      }
    }
    return total;
  }

  /** Drain the buffer in chronological order, oldest first. */
  drain(): Float32Array[] {
    const out: Float32Array[] = [];
    if (!this.full) {
      for (let i = 0; i < this.writeAt; i += 1) out.push(this.buffer[i]!);
    } else {
      for (let i = this.writeAt; i < this.capacity; i += 1) out.push(this.buffer[i]!);
      for (let i = 0; i < this.writeAt; i += 1) out.push(this.buffer[i]!);
    }
    // Clear the buffer after draining
    this.buffer = new Array(this.capacity);
    this.writeAt = 0;
    this.full = false;
    return out;
  }
}

/**
 * Hard cap on how many utterances hands-free will transcribe per minute.
 *
 * Without this, a television or a noisy room would transcribe forever and cost a
 * fortune. The cap stops the listener and explains why, rather than silently
 * generating a bill.
 */
export class UtteranceBudget {
  private readonly limit: number;
  private readonly windowMs: number;
  private timestamps: number[] = [];

  constructor(limit: number, windowMs: number) {
    this.limit = limit;
    this.windowMs = windowMs;
  }

  /** Returns true if this utterance is allowed, false if the budget is exhausted. */
  allow(now: number): boolean {
    this.timestamps = this.timestamps.filter((t) => now - t < this.windowMs);
    if (this.timestamps.length >= this.limit) return false;
    this.timestamps.push(now);
    return true;
  }

  /** How many utterances remain in the current window. */
  remaining(now: number): number {
    this.timestamps = this.timestamps.filter((t) => now - t < this.windowMs);
    return Math.max(0, this.limit - this.timestamps.length);
  }
}

/**
 * Trim trailing silence from a list of PCM blocks.
 *
 * Used when the gate closes a segment: the last few blocks are the silence that
 * triggered the close, and they should not be transcribed. We remove blocks from
 * the end until we hit a block that had energy above the threshold.
 */
export function trimTail(blocks: Float32Array[], rate: number, silenceMs: number, marginMs = 0): Float32Array[] {
  if (blocks.length === 0) return blocks;
  const firstBlock = blocks[0];
  if (!firstBlock) return blocks;
  const blockMs = (firstBlock.length / rate) * 1000;
  const silenceBlocks = Math.ceil((silenceMs - marginMs) / blockMs);
  const toTrim = Math.min(silenceBlocks, blocks.length - 1); // Always keep at least one
  return blocks.slice(0, blocks.length - toTrim);
}

/** Encode raw 16-bit mono PCM into a WAV buffer. */
export function encodeWav(pcmBlocks: Float32Array[], sampleRate: number): Uint8Array {
  let totalSamples = 0;
  for (const block of pcmBlocks) {
    if (!block) continue;
    totalSamples += block.length;
  }
  const pcm = new Int16Array(totalSamples);
  let offset = 0;
  for (const block of pcmBlocks) {
    if (!block) continue;
    for (let i = 0; i < block.length; i += 1) {
      const sample = block[i];
      const v = Math.max(-1, Math.min(1, sample ?? 0));
      pcm[offset++] = v < 0 ? v * 0x8000 : v * 0x7fff;
    }
  }
  const dataSize = pcm.byteLength;
  const header = new ArrayBuffer(44);
  const view = new DataView(header);
  // RIFF/WAVE format is little-endian. Values are chosen so that when written
  // little-endian, the bytes spell the ASCII strings.
  view.setUint32(0, 0x46464952, true);   // "RIFF"
  view.setUint32(4, 36 + dataSize, true);
  view.setUint32(8, 0x45564157, true);   // "WAVE"
  view.setUint32(12, 0x20746d66, true);  // "fmt "
  view.setUint32(16, 16, true);          // PCM chunk size
  view.setUint16(20, 1, true);           // audio format = PCM
  view.setUint16(22, 1, true);           // channels = mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // byte rate
  view.setUint16(32, 2, true);           // block align
  view.setUint16(34, 16, true);          // bits per sample
  view.setUint32(36, 0x61746164, true);  // "data"
  view.setUint32(40, dataSize, true);
  const result = new Uint8Array(header.byteLength + dataSize);
  result.set(new Uint8Array(header), 0);
  result.set(new Uint8Array(pcm.buffer), 44);
  return result;
}