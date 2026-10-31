/**
 * The wake-word arithmetic, checked by running it.
 *
 * This app has no test runner and does not get one for a 360-line module: `vitest`
 * plus jsdom would be the largest thing in `devDependencies` and §69 says not to add
 * frameworks the product does not need. What it needs is for these numbers to be
 * right, and `tsc` plus `node` -- both already here -- are enough for that.
 *
 *     npm run test:wake
 *
 * Only `src/lib/wake.ts` is covered, deliberately: it is the half of hands-free
 * listening that decides what audio gets paid for, and it is pure, so it can be run
 * without a microphone. The other half (`useWake.ts`) is Web Audio wiring, which needs
 * a room.
 */

import assert from "node:assert/strict";
import { rmsOf, normalizeHeard, matchWake, SpeechGate, UtteranceBudget, encodeWav, PreRoll, trimTail } from "./.build/wake.mjs";

let n = 0;
const t = (name, fn) => { fn(); n += 1; console.log("ok  " + name); };

t("rms of silence is zero", () => assert.equal(rmsOf(new Float32Array(64)), 0));
t("rms of full scale is one", () => assert.equal(rmsOf(new Float32Array([1, -1, 1, -1])), 1));
t("rms of empty is zero, not NaN", () => assert.equal(rmsOf(new Float32Array(0)), 0));

t("normalize strips punctuation and case", () =>
  assert.equal(normalizeHeard("  Nexus,   OPEN   Firefox! "), "nexus open firefox"));

t("wake word at the head is addressed", () => {
  const m = matchWake("Nexus, open Firefox", "nexus");
  assert.equal(m.addressed, true);
  assert.equal(m.command, "open Firefox");
});
t("original casing survives", () => assert.equal(matchWake("nexus run LS -la", "nexus").command, "run LS -la"));
t("filler in front still counts", () => {
  const m = matchWake("Um, hey Nexus what time is it", "nexus");
  assert.equal(m.addressed, true);
  assert.equal(m.command, "what time is it");
});
t("a mention deep in a sentence is not a command", () => {
  const m = matchWake("I was telling Sam that nexus can open Firefox", "nexus");
  assert.equal(m.addressed, false);
  assert.equal(m.command, "");
});
t("multi word wake phrase", () => {
  const m = matchWake("Hey Nexus, what is the weather", "hey nexus");
  assert.equal(m.addressed, true);
  assert.equal(m.command, "what is the weather");
});
t("wake word alone leaves no command", () => {
  const m = matchWake("Nexus.", "nexus");
  assert.equal(m.addressed, true);
  assert.equal(m.command, "");
});
t("possessive still matches", () => assert.equal(matchWake("Nexus's status", "nexus").addressed, true));
t("empty wake word matches nothing", () => assert.equal(matchWake("nexus go", "  ").addressed, false));
t("empty transcript matches nothing", () => assert.equal(matchWake("   ", "nexus").addressed, false));
t("heard is preserved for display", () => assert.equal(matchWake("  turn on the lamp ", "nexus").heard, "turn on the lamp"));

// --- the gate -------------------------------------------------------------
const feed = (gate, level, from, ms, step = 20) => {
  let last = "";
  for (let at = from; at < from + ms; at += step) last = gate.feed(level, at);
  return last;
};

t("nothing opens during calibration however loud", () => {
  const gate = new SpeechGate();
  gate.start(0);
  const verdicts = new Set();
  for (let at = 0; at < 1000; at += 20) verdicts.add(gate.feed(0.5, at));
  assert.deepEqual([...verdicts], ["calibrating"]);
});

t("a quiet room yields a low floor and speech opens", () => {
  const gate = new SpeechGate();
  gate.start(0);
  feed(gate, 0.001, 0, 1020);            // calibrate on near-silence
  assert.ok(gate.threshold >= 0.012, "threshold floor applies: " + gate.threshold);
  assert.equal(gate.feed(0.2, 1040), "quiet");   // one loud block is not speech
  assert.equal(gate.feed(0.2, 1060), "open");    // two are
  assert.equal(gate.feed(0.2, 1080), "hold");
});

t("a noisy room raises the threshold above the noise", () => {
  const gate = new SpeechGate();
  gate.start(0);
  feed(gate, 0.05, 0, 1020);
  assert.ok(gate.threshold > 0.05, "threshold " + gate.threshold + " must exceed the noise");
  assert.equal(feed(gate, 0.05, 1040, 500), "quiet");
});

t("silence closes a phrase and it is offered for transcription", () => {
  const gate = new SpeechGate();
  gate.start(0);
  feed(gate, 0.001, 0, 1020);
  gate.feed(0.3, 1040); gate.feed(0.3, 1060);          // open
  feed(gate, 0.3, 1080, 600);                          // 600ms of speech
  let last = "";
  for (let at = 1700; at < 2700; at += 20) { last = gate.feed(0.001, at); if (last !== "hold") break; }
  assert.equal(last, "close");
});

t("a cough is discarded rather than paid for", () => {
  const gate = new SpeechGate({ silenceMs: 200, minUtteranceMs: 350 });
  gate.start(0);
  feed(gate, 0.001, 0, 1020);
  gate.feed(0.4, 1040); gate.feed(0.4, 1060);          // open, 20ms of sound
  let last = "";
  for (let at = 1080; at < 2200; at += 20) { last = gate.feed(0.001, at); if (last !== "hold") break; }
  assert.equal(last, "discard");
});

t("an endless noise is capped", () => {
  const gate = new SpeechGate();
  gate.start(0);
  feed(gate, 0.001, 0, 1020);
  gate.feed(0.4, 1040); gate.feed(0.4, 1060);
  let last = "";
  for (let at = 1080; at < 60000; at += 20) { last = gate.feed(0.4, at); if (last === "cap") break; }
  assert.equal(last, "cap");
});

t("openFor reports the live segment length", () => {
  const gate = new SpeechGate();
  gate.start(0);
  feed(gate, 0.001, 0, 1020);
  gate.feed(0.4, 1040); gate.feed(0.4, 1060);
  assert.equal(gate.openFor(1560), 500);
  assert.equal(new SpeechGate().openFor(999), 0);
});

// --- the budget -----------------------------------------------------------
t("the budget stops instead of dropping", () => {
  const budget = new UtteranceBudget(3, 1000);
  assert.equal(budget.allow(0), true);
  assert.equal(budget.allow(10), true);
  assert.equal(budget.allow(20), true);
  assert.equal(budget.allow(30), false);
  assert.equal(budget.remaining(30), 0);
  assert.equal(budget.allow(1500), true, "the window slides");
});

// --- wav ------------------------------------------------------------------
t("encodeWav writes a real RIFF header and the samples", () => {
  const wav = encodeWav([new Float32Array([0, 1, -1]), new Float32Array([0.5])], 48000);
  const view = new DataView(wav.buffer, wav.byteOffset, wav.byteLength);
  const bytes = new Uint8Array(wav.buffer, wav.byteOffset, wav.byteLength);
  const text = (at, len) => String.fromCharCode(...bytes.slice(at, at + len));
  assert.equal(text(0, 4), "RIFF");
  assert.equal(text(8, 4), "WAVE");
  assert.equal(text(12, 4), "fmt ");
  assert.equal(text(36, 4), "data");
  assert.equal(view.getUint16(22, true), 1, "mono");
  assert.equal(view.getUint32(24, true), 48000);
  assert.equal(view.getUint16(34, true), 16);
  assert.equal(view.getUint32(40, true), 8, "four samples, two bytes each");
  assert.equal(wav.byteLength, 44 + 8);
  assert.equal(view.getInt16(44 + 2, true), 32767, "+1.0 saturates high");
  assert.equal(view.getInt16(44 + 4, true), -32768, "-1.0 saturates low");
});
t("out of range samples clamp rather than wrap", () => {
  const wav = encodeWav([new Float32Array([4, -4])], 16000);
  const view = new DataView(wav.buffer, wav.byteOffset, wav.byteLength);
  assert.equal(view.getInt16(44, true), 32767);
  assert.equal(view.getInt16(46, true), -32768);
});

// --- pre-roll -------------------------------------------------------------
t("pre-roll is bounded and keeps the newest audio", () => {
  const roll = new PreRoll(500, 1000);      // 500 samples at 1kHz
  for (let i = 0; i < 100; i += 1) roll.push(new Float32Array(100).fill(i));
  assert.ok(roll.samples <= 600, "held " + roll.samples);
  const held = roll.drain();
  assert.equal(held.at(-1)[0], 99, "the newest block survives");
  assert.equal(roll.samples, 0, "drain empties it");
});

// --- tail trimming --------------------------------------------------------
t("the silence that closed a phrase is cut, with a margin kept", () => {
  const blocks = Array.from({ length: 10 }, () => new Float32Array(100));  // 1000 samples
  const kept = trimTail(blocks, 1000, 900, 200);                            // drop 700 samples
  const total = kept.reduce((n, b) => n + b.length, 0);
  assert.equal(total, 300);
});
t("trimming never empties the capture", () => {
  const kept = trimTail([new Float32Array(100)], 1000, 5000, 0);
  assert.equal(kept.length, 1);
  assert.ok(kept[0].length > 0);
});
t("a margin larger than the silence trims nothing", () =>
  assert.equal(trimTail([new Float32Array(100)], 1000, 200, 200).length, 1));
t("the gate reports the silence it waits out", () => assert.equal(new SpeechGate().silenceMs, 900));

console.log("\n" + n + " checks passed");
