/**
 * The Automations screen's wording and arithmetic, checked by running it.
 *
 *     npm run test:automations
 *
 * Same bargain as `wake.check.mjs`: this app has no test runner, and `tsc` plus
 * `node` are enough for pure functions. `src/lib/automations.ts` is pure precisely so
 * that the part of the screen that can be wrong in a *silent* way -- the part that
 * decides what a rule claims about itself -- is not the part that needs a browser.
 *
 * The centre of it is the block below marked "the engine is part of the answer".
 * `ruleState` returning ARMED for a rule the stopped scheduler will never fire is the
 * §78 failure this whole screen exists to prevent, and it is not a failure that shows
 * up as a crash or a blank panel: it looks exactly like working software, right up
 * until the reminder does not arrive. So it is asserted directly, and over the whole
 * product of the inputs rather than one case.
 */

import assert from "node:assert/strict";
import {
  COMPOSABLE,
  LEVELS,
  TRIGGERS,
  actionLine,
  ruleState,
  scheduleHint,
  spanOf,
  statusState,
  triggerLine,
  whenNext,
} from "./.build/automations.mjs";

let n = 0;
const t = (name, fn) => { fn(); n += 1; console.log("ok  " + name); };

const rule = (over = {}) => ({
  enabled: true,
  trigger_kind: "schedule",
  schedule: "08:30",
  unavailable_reason: null,
  action: { kind: "notify", message: "study" },
  ...over,
});

// --- the engine is part of the answer -------------------------------------
t("an armed rule with a live engine is ARMED", () =>
  assert.equal(ruleState(rule(), true).label, "ARMED"));

t("the same rule with a dead engine will not fire, and says so", () => {
  const state = ruleState(rule(), false);
  assert.equal(state.label, "WILL NOT FIRE");
  assert.equal(state.state, "warn");
});

t("nothing is ever ARMED while the engine is down", () => {
  for (const enabled of [true, false])
    for (const trigger_kind of ["schedule", "interval", "manual"])
      for (const unavailable_reason of [null, "bad schedule"]) {
        const state = ruleState(rule({ enabled, trigger_kind, unavailable_reason }), false);
        assert.notEqual(state.label, "ARMED",
          `${trigger_kind} enabled=${enabled} reason=${unavailable_reason}`);
        assert.notEqual(state.state, "ok");
      }
});

t("with a live engine, only an enabled scheduled rule is ARMED", () => {
  let armed = 0;
  for (const enabled of [true, false])
    for (const trigger_kind of ["schedule", "interval", "manual"])
      for (const unavailable_reason of [null, "bad schedule"])
        if (ruleState(rule({ enabled, trigger_kind, unavailable_reason }), true).label === "ARMED")
          armed += 1;
  assert.equal(armed, 2, "schedule and interval, enabled, with no reason");
});

// --- precedence -----------------------------------------------------------
t("a reason outranks being switched on", () =>
  assert.equal(ruleState(rule({ unavailable_reason: "time must look like '08:30'" }), true).label,
    "UNAVAILABLE"));
t("a reason outranks being switched off too", () =>
  assert.equal(ruleState(rule({ enabled: false, unavailable_reason: "x" }), true).label,
    "UNAVAILABLE"));
t("off outranks the engine being down", () =>
  assert.equal(ruleState(rule({ enabled: false }), false).label, "OFF"));
t("a manual rule never depends on the engine", () => {
  assert.equal(ruleState(rule({ trigger_kind: "manual" }), false).label, "MANUAL");
  assert.equal(ruleState(rule({ trigger_kind: "manual" }), true).label, "MANUAL");
});
t("an empty reason is not a reason", () =>
  assert.equal(ruleState(rule({ unavailable_reason: "" }), true).label, "ARMED"));

// --- the future, which relativeTime cannot describe ------------------------
const now = Date.parse("2026-09-02T12:00:00Z");
const at = (seconds) => new Date(now + seconds * 1000).toISOString();

t("no timestamp is a dash, not a guess", () => {
  assert.equal(whenNext(null, now), "—");
  assert.equal(whenNext(undefined, now), "—");
  assert.equal(whenNext("", now), "—");
});
t("an unparseable timestamp is a dash", () => assert.equal(whenNext("soon", now), "—"));
t("seconds away reads in seconds", () => assert.equal(whenNext(at(30), now), "in 30s"));
t("a minute and a half rounds to minutes", () => assert.equal(whenNext(at(90), now), "in 2m"));
t("an hour away reads in hours", () => assert.equal(whenNext(at(3600), now), "in 1h 0m"));
t("tomorrow is not 'just now'", () => {
  const answer = whenNext(at(86400), now);
  assert.equal(answer, "in 1d");
  assert.ok(!answer.includes("now"), answer);
});
t("past due is stated as overdue", () =>
  assert.equal(whenNext(at(-120), now), "overdue by 2m"));
t("due this instant is not overdue", () => assert.equal(whenNext(at(0), now), "in 0s"));

// --- spans ----------------------------------------------------------------
t("under a minute stays in seconds", () => assert.equal(spanOf(59), "59s"));
t("a minute is a minute", () => assert.equal(spanOf(60), "1m"));
t("under a day carries the minutes", () => assert.equal(spanOf(3600 * 5 + 60 * 7), "5h 7m"));
t("a day rounds to days", () => assert.equal(spanOf(86400), "1d"));
t("no span is ever NaN or empty", () => {
  for (const s of [0, 1, 59, 60, 61, 3599, 3600, 86399, 86400, 604800]) {
    const out = spanOf(s);
    assert.ok(out.length > 0 && !out.includes("NaN"), `${s} -> ${out}`);
  }
});

// --- what a rule does -----------------------------------------------------
t("an action with no kind says there is nothing to do", () =>
  assert.equal(actionLine({ action: {} }), "no action — this rule has nothing to do"));
t("a notify reads as the reminder it is", () =>
  assert.equal(actionLine({ action: { kind: "notify", message: "study" } }), 'Remind: "study"'));
t("a notify with a blank message says the message is missing", () =>
  assert.equal(actionLine({ action: { kind: "notify", message: "   " } }),
    "notify, but with no message to deliver"));
t("a kind this build has no wording for is named, not hidden", () =>
  assert.equal(actionLine({ action: { kind: "tool" } }), "tool"));

// --- when a rule fires ----------------------------------------------------
t("manual says manual", () =>
  assert.equal(triggerLine({ trigger_kind: "manual", schedule: "" }), "Manual only"));
t("an interval reads as a gap", () =>
  assert.equal(triggerLine({ trigger_kind: "interval", schedule: "15m" }), "Every 15m"));
t("a schedule reads as a time", () =>
  assert.equal(triggerLine({ trigger_kind: "schedule", schedule: "mon,wed 08:30" }),
    "At mon,wed 08:30"));
t("a schedule with no time says so", () =>
  assert.equal(triggerLine({ trigger_kind: "schedule", schedule: "" }), "No time set"));

// --- run status -----------------------------------------------------------
t("every status the core can write has a state", () => {
  assert.equal(statusState("completed"), "ok");
  assert.equal(statusState("failed"), "failed");
  assert.equal(statusState("skipped"), "cancelled");
  assert.equal(statusState("running"), "active");
});
t("an unknown status is idle, not a crash", () => assert.equal(statusState("weird"), "idle"));
t("a skipped run does not read as a success", () =>
  assert.notEqual(statusState("skipped"), statusState("completed")));

// --- the form's own data --------------------------------------------------
t("every trigger the form offers has its own hint", () => {
  const hints = TRIGGERS.map((trigger) => scheduleHint(trigger.value));
  for (const hint of hints) assert.ok(hint.length > 20, hint);
  assert.equal(new Set(hints).size, TRIGGERS.length, "two triggers share a hint");
});
t("the composable kinds are a real, non-empty list", () => {
  assert.ok(COMPOSABLE.length > 0);
  for (const kind of COMPOSABLE) assert.equal(typeof kind, "string");
});
t("the levels are the four the core accepts", () =>
  assert.deepEqual(LEVELS.map((level) => level.value), ["info", "success", "warning", "error"]));

console.log("\n" + n + " checks passed");
