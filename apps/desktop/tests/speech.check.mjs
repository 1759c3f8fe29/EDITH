/**
 * The speech ordering, checked by running it.
 *
 * `src/lib/speech.ts` decides what the user actually hears and in what order, and
 * it is pure -- no `Audio`, no fetch -- precisely so that decision can be run here
 * instead of being inspected by ear. The failures it guards against are the ones
 * that are hardest to notice in a browser: sentence four playing before sentence
 * three, or one failed sentence silencing the rest of the answer.
 *
 *     npm run test:speech
 *
 * The fake player below never resolves on its own. Every `finish()` in a test is a
 * file ending, which is what makes "only one plays at a time" an assertion rather
 * than a hope.
 */

import assert from "node:assert/strict";
import { SpeechQueue, PlaybackBlocked, PlaybackFailed, BLOCKED_SENTENCE } from "./.build/speech.mjs";

function fakePlayer() {
  const player = {
    played: [],
    stops: 0,
    open: [],
    play(url) {
      player.played.push(url);
      return new Promise((resolve, reject) => player.open.push({ url, resolve, reject }));
    },
    stop() {
      player.stops += 1;
    },
    /** End the file that is playing, the way the browser's `ended` event would. */
    finish() {
      const current = player.open.shift();
      assert.ok(current, "nothing is playing, so nothing can finish");
      current.resolve();
      return settle();
    },
    fail(error) {
      const current = player.open.shift();
      assert.ok(current, "nothing is playing, so nothing can fail");
      current.reject(error);
      return settle();
    },
  };
  return player;
}

/** Let the queue's promise callbacks run. Two ticks: `then` chains inside `pump`. */
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

const seg = (index, url = `/api/voice/audio/${index}.wav`) => ({ index, text: `s${index}`, url });

let n = 0;
const results = [];
const t = async (name, fn) => {
  await fn();
  n += 1;
  results.push(name);
  console.log("ok  " + name);
};

await t("the first segment plays as soon as it arrives", async () => {
  const player = fakePlayer();
  const queue = new SpeechQueue(player);
  queue.push(seg(0));
  assert.deepEqual(player.played, ["/api/voice/audio/0.wav"]);
});

await t("only one file plays at a time", async () => {
  const player = fakePlayer();
  const queue = new SpeechQueue(player);
  queue.push(seg(0));
  queue.push(seg(1));
  queue.push(seg(2));
  assert.equal(player.played.length, 1);
  assert.equal(player.open.length, 1);
  await player.finish();
  assert.equal(player.played.length, 2);
});

await t("audio that arrives out of order is played in order", async () => {
  const player = fakePlayer();
  const queue = new SpeechQueue(player);
  queue.push(seg(1));
  assert.deepEqual(player.played, [], "segment 1 must wait for segment 0");
  queue.push(seg(0));
  assert.deepEqual(player.played, ["/api/voice/audio/0.wav"]);
  await player.finish();
  assert.deepEqual(player.played, ["/api/voice/audio/0.wav", "/api/voice/audio/1.wav"]);
});

await t("a live turn waits for a gap rather than skipping ahead", async () => {
  const player = fakePlayer();
  const queue = new SpeechQueue(player);
  queue.push(seg(0));
  await player.finish();
  queue.push(seg(2));
  assert.deepEqual(player.played, ["/api/voice/audio/0.wav"], "1 is still being synthesised");
  assert.equal(queue.pending, 1);
});

await t("a segment that could not be spoken does not stall the rest", async () => {
  const player = fakePlayer();
  const queue = new SpeechQueue(player);
  queue.push(seg(0));
  await player.finish();
  queue.push(seg(2));
  queue.skip(1); // the core's `speech_error` for segment 1
  assert.deepEqual(player.played, ["/api/voice/audio/0.wav", "/api/voice/audio/2.wav"]);
});

await t("a skip that arrives before the audio behind it still lets it through", async () => {
  const player = fakePlayer();
  const queue = new SpeechQueue(player);
  queue.skip(0);
  assert.deepEqual(player.played, []);
  queue.push(seg(1));
  assert.deepEqual(player.played, ["/api/voice/audio/1.wav"]);
});

await t("closing the stream plays what is left when an index never arrives", async () => {
  const player = fakePlayer();
  const queue = new SpeechQueue(player);
  queue.push(seg(0));
  await player.finish();
  queue.push(seg(3));
  assert.deepEqual(player.played, ["/api/voice/audio/0.wav"], "still waiting for 1");
  queue.close();
  assert.deepEqual(player.played, ["/api/voice/audio/0.wav", "/api/voice/audio/3.wav"]);
});

await t("closing an empty queue plays nothing and says it is idle", async () => {
  const player = fakePlayer();
  let idle = 0;
  const queue = new SpeechQueue(player, { onIdle: () => (idle += 1) });
  queue.close();
  assert.deepEqual(player.played, []);
  assert.ok(idle > 0);
});

await t("a hole at the end is skipped, and the rest still plays in order", async () => {
  // Every event has arrived and segment 0 is missing -- a worker that died, or a
  // frame that was lost. The remaining two are still an ordered pair.
  const player = fakePlayer();
  const queue = new SpeechQueue(player);
  queue.push(seg(2));
  queue.push(seg(1));
  assert.deepEqual(player.played, [], "0 might still be coming while the turn is live");
  queue.close();
  assert.deepEqual(player.played, ["/api/voice/audio/1.wav"]);
  await player.finish();
  assert.deepEqual(player.played, ["/api/voice/audio/1.wav", "/api/voice/audio/2.wav"]);
});

await t("audio that arrives after the playhead has passed it is dropped", async () => {
  // A stream that closed while segment 1 was still in flight: playing it now would
  // say a sentence after the sentence that followed it.
  const player = fakePlayer();
  const queue = new SpeechQueue(player);
  queue.push(seg(2));
  queue.close();
  assert.deepEqual(player.played, ["/api/voice/audio/2.wav"]);
  queue.push(seg(1));
  await player.finish();
  assert.deepEqual(player.played, ["/api/voice/audio/2.wav"]);
});

await t("audio for a sentence already spoken is dropped, not replayed", async () => {
  const player = fakePlayer();
  const queue = new SpeechQueue(player);
  queue.push(seg(0));
  await player.finish();
  queue.push(seg(0, "/api/voice/audio/late.wav"));
  assert.deepEqual(player.played, ["/api/voice/audio/0.wav"]);
});

await t("a blocked browser is reported once and stops the queue", async () => {
  const player = fakePlayer();
  const problems = [];
  const queue = new SpeechQueue(player, {
    onProblem: (message, blocked) => problems.push([message, blocked]),
  });
  queue.push(seg(0));
  queue.push(seg(1));
  await player.fail(new PlaybackBlocked());
  assert.deepEqual(problems, [[BLOCKED_SENTENCE, true]]);
  assert.deepEqual(player.played, ["/api/voice/audio/0.wav"], "sentence 1 would have failed too");
  queue.push(seg(2));
  assert.equal(player.played.length, 1, "a stopped queue accepts nothing further");
});

await t("one unplayable file does not end the answer", async () => {
  const player = fakePlayer();
  const problems = [];
  const queue = new SpeechQueue(player, {
    onProblem: (message, blocked) => problems.push([message, blocked]),
  });
  queue.push(seg(0));
  queue.push(seg(1));
  await player.fail(new PlaybackFailed("the file is empty"));
  assert.equal(problems.length, 1);
  assert.equal(problems[0][1], false, "not a blocked browser");
  assert.match(problems[0][0], /the file is empty/);
  assert.deepEqual(player.played, ["/api/voice/audio/0.wav", "/api/voice/audio/1.wav"]);
});

await t("stopping drops the backlog and silences the player", async () => {
  const player = fakePlayer();
  const queue = new SpeechQueue(player);
  queue.push(seg(0));
  queue.push(seg(1));
  queue.stop();
  assert.equal(player.stops, 1);
  assert.equal(queue.pending, 0);
  await player.finish(); // the abandoned file ends after the stop
  assert.deepEqual(player.played, ["/api/voice/audio/0.wav"], "the backlog is gone");
});

await t("a reset queue speaks the next answer from its first sentence", async () => {
  const player = fakePlayer();
  const queue = new SpeechQueue(player);
  queue.push(seg(0));
  queue.stop();
  queue.reset();
  queue.push(seg(0, "/api/voice/audio/next-0.wav"));
  assert.deepEqual(player.played, ["/api/voice/audio/0.wav", "/api/voice/audio/next-0.wav"]);
});

await t("what is playing is announced, and idle is announced when it drains", async () => {
  const player = fakePlayer();
  const started = [];
  let idle = 0;
  const queue = new SpeechQueue(player, {
    onStart: (segment) => started.push(segment.text),
    onIdle: () => (idle += 1),
  });
  queue.push(seg(0));
  queue.push(seg(1));
  assert.deepEqual(started, ["s0"]);
  assert.equal(queue.active, true);
  await player.finish();
  await player.finish();
  assert.deepEqual(started, ["s0", "s1"]);
  assert.equal(queue.active, false);
  assert.equal(idle, 1);
});

console.log(`\n${n} checks passed`);
