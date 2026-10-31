/**
 * The Task HUD's reducer, checked by running it.
 *
 *     npm run test:taskhud
 *
 * Same bargain as `wake.check.mjs` and `automations.check.mjs`: this app has no
 * test runner, and `tsc` plus `node` are enough for pure functions.
 *
 * The centre of it is "out-of-order events", below. Every other assertion here
 * would still pass if the reducer were plain last-write-wins; that block is the
 * one that fails, and the failure it describes is silent -- a step repainted as
 * running, or a cancelled task wearing a spinner, is software that looks like it
 * is working right up until the user acts on it.
 */

import assert from "node:assert/strict";
import {
  TERMINAL_HOLD_MS,
  announcement,
  applyEvent,
  dismiss,
  elapsed,
  liveCount,
  ordered,
  progressLine,
  seed,
  stepWindow,
  sweep,
  taskState,
} from "./.build/taskhud.mjs";

let n = 0;
const t = (name, fn) => { fn(); n += 1; console.log("ok  " + name); };

const task = (over = {}) => ({
  id: "t1",
  objective: "Summarise the downloads folder",
  status: "running",
  steps: [],
  progress: { total: 0, completed: 0, failed: 0, pending: 0, running: 0, skipped: 0, waiting_permission: 0 },
  started_at: "2026-10-12T09:00:00+00:00",
  finished_at: null,
  error: null,
  ...over,
});

const step = (over = {}) => ({
  id: "s1",
  idx: 0,
  intent: "list the folder",
  tool: "files.list",
  status: "pending",
  error: null,
  ...over,
});

const live = () => seed({}, [task()], 1000);

// --- steps merge by id ------------------------------------------------------
t("a step event lands on its task and creates no other row", () => {
  const next = applyEvent(live(), "task.step.started",
    { task_id: "t1", step: step({ status: "running" }) }, 2000);
  assert.deepEqual(Object.keys(next), ["t1"]);
  assert.equal(next.t1.steps.length, 1);
  assert.equal(next.t1.steps[0].status, "running");
});

t("steps stay in idx order however they arrive", () => {
  let state = live();
  state = applyEvent(state, "task.step.started", { task_id: "t1", step: step({ id: "b", idx: 1, status: "running" }) }, 2000);
  state = applyEvent(state, "task.step.started", { task_id: "t1", step: step({ id: "a", idx: 0, status: "completed" }) }, 2001);
  assert.deepEqual(state.t1.steps.map((s) => s.id), ["a", "b"]);
});

t("a repeated event for one step updates it in place, never appends", () => {
  let state = live();
  state = applyEvent(state, "task.step.started", { task_id: "t1", step: step({ status: "running" }) }, 2000);
  state = applyEvent(state, "task.step.updated", { task_id: "t1", step: step({ status: "running" }) }, 2001);
  state = applyEvent(state, "task.step.finished", { task_id: "t1", step: step({ status: "completed" }) }, 2002);
  assert.equal(state.t1.steps.length, 1);
  assert.equal(state.t1.steps[0].status, "completed");
});

// --- out-of-order events ----------------------------------------------------
t("a late step.started cannot walk a completed step back to running", () => {
  let state = live();
  state = applyEvent(state, "task.step.finished", { task_id: "t1", step: step({ status: "completed" }) }, 2000);
  state = applyEvent(state, "task.step.started", { task_id: "t1", step: step({ status: "running" }) }, 1999);
  assert.equal(state.t1.steps[0].status, "completed",
    "the older event repainted a finished step as running");
});

t("a completed step never becomes running, whatever arrives", () => {
  for (const late of ["pending", "running", "waiting_permission"]) {
    let state = live();
    state = applyEvent(state, "task.step.finished", { task_id: "t1", step: step({ status: "failed" }) }, 2000);
    state = applyEvent(state, "task.step.updated", { task_id: "t1", step: step({ status: late }) }, 2001);
    assert.equal(state.t1.steps[0].status, "failed", `a late ${late} event overwrote a failed step`);
  }
});

t("a snapshot that arrives late does not delete a step the newer one added", () => {
  const older = task({ steps: [step({ id: "a", idx: 0, status: "completed" })] });
  const newer = task({ steps: [
    step({ id: "a", idx: 0, status: "completed" }),
    step({ id: "b", idx: 1, status: "running" }),
  ] });
  let state = seed({}, [newer], 1000);
  state = applyEvent(state, "task.planned", older, 1500);
  assert.deepEqual(state.t1.steps.map((s) => s.id), ["a", "b"]);
});

t("events converge to the same state in any order", () => {
  const events = [
    ["task.step.started", { task_id: "t1", step: step({ status: "running" }) }, 10],
    ["task.step.finished", { task_id: "t1", step: step({ status: "completed" }) }, 11],
    ["task.step.updated", { task_id: "t1", step: step({ status: "running" }) }, 12],
  ];
  const orders = [
    [0, 1, 2], [2, 1, 0], [1, 0, 2], [0, 2, 1], [1, 2, 0], [2, 0, 1],
  ];
  for (const order of orders) {
    let state = live();
    for (const i of order) {
      const [type, payload, at] = events[i];
      state = applyEvent(state, type, payload, at);
    }
    assert.equal(state.t1.steps[0].status, "completed", `order ${order} did not converge`);
  }
});

// --- a cancelled task is never running --------------------------------------
t("a cancelled task never reads as running", () => {
  let state = live();
  state = applyEvent(state, "task.cancelled", task({ status: "cancelled" }), 2000);
  state = applyEvent(state, "task.step.started", { task_id: "t1", step: step({ status: "running" }) }, 2001);
  state = applyEvent(state, "task.planned", task({ status: "running" }), 2002);
  assert.equal(state.t1.status, "cancelled");
  assert.equal(liveCount(state), 0, "a stopped task was still counted as live");
  assert.notEqual(taskState(state.t1.status).label, "RUNNING");
});

t("every terminal status resists being revived by a replayed event", () => {
  for (const ended of ["completed", "failed", "cancelled", "interrupted"]) {
    let state = live();
    state = applyEvent(state, `task.${ended}`, task({ status: ended }), 2000);
    state = applyEvent(state, "task.planned", task({ status: "running" }), 2001);
    state = applyEvent(state, "task.replanned", task({ status: "running" }), 2002);
    assert.equal(state.t1.status, ended, `${ended} was revived to ${state.t1.status}`);
  }
});

t("an explicit retry does restart a finished task", () => {
  let state = live();
  state = applyEvent(state, "task.failed", task({ status: "failed" }), 2000);
  state = applyEvent(state, "task.retried", task({ status: "pending" }), 3000);
  assert.equal(state.t1.status, "pending");
  assert.equal(state.t1.settledAt, null, "a restarted task kept its old settlement clock");
});

// --- unknown ids ------------------------------------------------------------
t("a step event for an unknown task creates no phantom row", () => {
  const state = applyEvent(live(), "task.step.started",
    { task_id: "never-seen", step: step() }, 2000);
  assert.deepEqual(Object.keys(state), ["t1"]);
});

t("a step event with no task id changes nothing", () => {
  const before = live();
  const after = applyEvent(before, "task.step.started", { step: step() }, 2000);
  assert.equal(after, before, "the state object was replaced for an unusable event");
});

t("an unknown event type returns the same object", () => {
  const before = live();
  const after = applyEvent(before, "stream.heartbeat", { x: 1 }, 2000);
  assert.equal(after, before);
});

t("a task snapshot with no id is ignored", () => {
  const before = live();
  assert.equal(applyEvent(before, "task.planned", { objective: "x" }, 2000), before);
});

// --- the terminal hold window ----------------------------------------------
t("a finished task leaves after the hold window", () => {
  let state = applyEvent(live(), "task.completed", task({ status: "completed" }), 1000);
  assert.equal(Object.keys(sweep(state, 1000 + TERMINAL_HOLD_MS - 1)).length, 1);
  assert.equal(Object.keys(sweep(state, 1000 + TERMINAL_HOLD_MS)).length, 0);
});

t("a live task is never swept away, however long it runs", () => {
  const state = live();
  assert.equal(Object.keys(sweep(state, 1000 + TERMINAL_HOLD_MS * 100)).length, 1);
});

t("sweeping nothing changes returns the same object", () => {
  const state = live();
  assert.equal(sweep(state, 1000), state);
});

t("dismissing removes exactly one task", () => {
  let state = seed({}, [task(), task({ id: "t2" })], 1000);
  state = dismiss(state, "t1");
  assert.deepEqual(Object.keys(state), ["t2"]);
  assert.equal(dismiss(state, "gone"), state);
});

// --- counts, wording and the clock -----------------------------------------
t("progress counts come from the core and are never blanked by a partial event", () => {
  const full = task({ progress: { total: 4, completed: 2, failed: 0, pending: 1, running: 1, skipped: 0, waiting_permission: 0 } });
  let state = seed({}, [full], 1000);
  state = applyEvent(state, "task.step.updated", { task_id: "t1", step: step({ status: "running" }) }, 2000);
  assert.equal(state.t1.progress.total, 4, "a progress-less event zeroed the counts");
  assert.equal(progressLine(state.t1), "2/4 steps");
});

t("a task with no steps says so rather than claiming 0%", () =>
  assert.equal(progressLine(live().t1), "no steps yet"));

t("failed and skipped steps are named in the progress line", () => {
  const row = task({ progress: { total: 5, completed: 2, failed: 1, pending: 0, running: 0, skipped: 1, waiting_permission: 0 } });
  const line = progressLine(seed({}, [row], 0).t1);
  assert.match(line, /2\/5 steps/);
  assert.match(line, /1 failed/);
  assert.match(line, /1 skipped/);
});

t("waiting_permission reads as waiting for the user, not as running", () => {
  const state = seed({}, [task({ status: "waiting_permission" })], 0);
  assert.equal(taskState(state.t1.status).label, "WAITING FOR YOU");
  assert.notEqual(taskState(state.t1.status).label, "RUNNING");
});

t("no two task statuses share a label", () => {
  const seen = new Map();
  for (const status of ["pending", "running", "waiting_permission", "completed", "failed", "cancelled", "interrupted"]) {
    const { label } = taskState(status);
    assert.ok(!seen.has(label), `${status} and ${seen.get(label)} both read as ${label}`);
    seen.set(label, status);
  }
});

t("elapsed is measured from the task, not from when this window saw it", () => {
  const state = seed({}, [task()], 9_999_999_000);
  const label = elapsed(state.t1, Date.parse("2026-10-12T09:00:30+00:00"));
  assert.equal(label, "30s", `a reloaded window reported ${label} for a 30s-old task`);
});

t("a finished task's elapsed stops at finished_at", () => {
  const row = task({ finished_at: "2026-10-12T09:02:00+00:00" });
  const state = seed({}, [row], 0);
  assert.equal(elapsed(state.t1, Date.parse("2026-10-12T10:00:00+00:00")), "2m 0s");
});

t("a task that has not started says so instead of showing zero", () => {
  const state = seed({}, [task({ status: "pending", started_at: null })], 0);
  assert.equal(elapsed(state.t1, 5000), "not started");
});

// --- the announcement (the live region) -------------------------------------
const runningRow = task({ started_at: "2026-10-12T09:00:00+00:00" });
const waitingRow = task({ id: "t2", status: "waiting_permission", objective: "Install the printer" });

t("the announcement names the state in words", () => {
  const says = announcement([waitingRow]);
  assert.match(says, /WAITING FOR YOU/);
  assert.ok(!/RUNNING/.test(says), `a blocked task was announced as running: ${says}`);
});

t("the announcement never carries the clock", () => {
  // The whole point: this string is rendered inside aria-live, and a live region
  // re-announces whenever its text changes. An elapsed time in here would be read
  // aloud once a second for as long as the run lasts.
  //
  // Both halves are needed. The value check compares against what `elapsed` actually
  // says right now — a task started hours ago reads "3h 12m", which a `\d+s` pattern
  // would sail straight past. The shape check catches a duration this exact second
  // did not produce.
  const state = seed({}, [runningRow], 1000);
  const clock = elapsed(state.t1);
  const says = announcement(ordered(state));
  assert.ok(clock.length > 0, "elapsed said nothing, so the comparison proves nothing");
  assert.ok(!says.includes(clock), `the announcement carried the clock (${clock}): ${says}`);
  assert.ok(!/\d+\s*[hms]\b/.test(says), `a duration leaked into the announcement: ${says}`);
  assert.equal(announcement(ordered(state)), says, "the announcement changed with no event");
});

t("the announcement counts the tasks when there is more than one", () => {
  const says = announcement([runningRow, waitingRow]);
  assert.match(says, /^2 tasks running/);
});

t("no tasks means nothing is announced", () => assert.equal(announcement([]), ""));

// --- the draw order ---------------------------------------------------------
t("live tasks are drawn before finished ones, and a refusal is not a reorder", () => {
  const rows = [
    task({ id: "late", status: "running", started_at: "2026-10-12T09:05:00+00:00" }),
    task({ id: "early", status: "running", started_at: "2026-10-12T09:00:00+00:00" }),
    task({ id: "done", status: "completed" }),
  ];
  let state = seed({}, rows, 1000);
  state = applyEvent(state, "task.completed", task({ id: "done", status: "completed" }), 2000);
  assert.deepEqual(ordered(state).map((t) => t.id), ["early", "late", "done"],
    "live tasks did not come first, oldest first");
});

t("a task that has just ended appears above one that ended earlier", () => {
  let state = seed({}, [task({ id: "a" }), task({ id: "b" })], 1000);
  state = applyEvent(state, "task.completed", task({ id: "a", status: "completed" }), 2000);
  state = applyEvent(state, "task.failed", task({ id: "b", status: "failed" }), 5000);
  assert.deepEqual(ordered(state).map((t) => t.id), ["b", "a"]);
});

t("a queued task that has not started sorts after one that has", () => {
  const state = seed({}, [
    task({ id: "queued", status: "pending", started_at: null }),
    task({ id: "going", status: "running" }),
  ], 0);
  assert.deepEqual(ordered(state).map((t) => t.id), ["going", "queued"]);
});

// --- the step window --------------------------------------------------------
const manySteps = (count) => Array.from({ length: count }, (_, i) =>
  step({ id: `s${i}`, idx: i, status: i < count - 1 ? "completed" : "running" }));

t("a short task shows all its steps and does not claim to be sliced", () => {
  const { shown, sliced } = stepWindow(manySteps(3));
  assert.equal(shown.length, 3);
  assert.equal(sliced, false);
});

t("a long task's window follows the running step rather than the first six", () => {
  const { shown, first, last, total, sliced } = stepWindow(manySteps(40));
  assert.equal(shown.length, 6);
  assert.equal(shown[shown.length - 1].status, "running", "the running step is not in the window");
  assert.equal(total, 40);
  assert.equal(sliced, true);
  assert.equal(last - first, 5);
});

t("the window stays inside the list at both ends", () => {
  const window = stepWindow(manySteps(40));
  assert.ok(window.first >= 1 && window.last <= 40, `${window.first}..${window.last}`);

  const allDone = Array.from({ length: 40 }, (_, i) => step({ id: `s${i}`, idx: i, status: "completed" }));
  const tail = stepWindow(allDone);
  assert.equal(tail.last, 40, "a fully finished task did not show its last steps");
  assert.ok(tail.first >= 1);
});

console.log(`\n${n} checks passed`);
