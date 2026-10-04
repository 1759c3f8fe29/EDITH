/**
 * The arithmetic and the wording behind the Automations screen (§31-§34, §47, §78).
 *
 * Pulled out of `screens/AutomationsScreen.tsx` for one reason: these are the
 * functions that decide what the user is *told* about a rule, and a screen component
 * cannot be run without a browser. Here they are pure -- (row, engine state, clock)
 * in, string out -- so `npm run test:automations` can check them, the same bargain
 * `lib/wake.ts` makes with `tests/wake.check.mjs`.
 *
 * Two of them are load-bearing rather than cosmetic:
 *
 * `ruleState` is the §78 rule in code. A rule that is enabled and correctly scheduled
 * still cannot fire if the loop that fires it is stopped or switched off, and it must
 * not wear the same pill as one that will go off on time. That is why it takes
 * `engineLive` at all: the honest answer is a property of the pair, not of the row.
 *
 * `whenNext` exists because `relativeTime` in `components/primitives.tsx` is
 * past-only and answers "just now" for anything ahead of the clock. Pointed at
 * `next_run_at` that would render tomorrow's eight o'clock reminder as happening this
 * second. Overdue is said out loud rather than hidden, because a rule past its time
 * that has not fired is the visible symptom of a stopped engine.
 *
 * Deliberately imports nothing. `tsc src/lib/automations.ts` has to compile this file
 * alone, and the parameter types are the narrow set of fields each function reads
 * rather than `Automation` from `lib/api.ts` -- a real `Automation` is structurally
 * assignable to every one of them, so the screen's own call sites are what prove the
 * shapes still match the API. Same for the state names: they are string literals
 * here, and the screen passes them where `State` from `components/primitives.tsx` is
 * required, so a label that stops being a real state fails `tsc --noEmit`.
 */

/** The fields of an automation row that any of these functions reads. */
export type RuleFacts = {
  enabled: boolean;
  trigger_kind: string;
  schedule: string;
  /** Non-null only when the core refused to arm the rule, and then it says why. */
  unavailable_reason: string | null;
  action: { kind?: string; message?: string };
};

/** A subset of `State` in `components/primitives.tsx`; checked there, not here. */
export type RuleStateName = "ok" | "warn" | "idle" | "disabled";

/** A subset of `State`; `active` is a run still in flight, not a live engine. */
export type RunStateName = "ok" | "failed" | "cancelled" | "active" | "idle";

/**
 * The rule's state as one word, told against the engine rather than in isolation.
 *
 * Order is the whole of it. An unavailable rule is unavailable whether or not it is
 * switched on, an off rule is off whether or not the engine runs, and a manual rule
 * never depended on the engine in the first place -- so only a rule that has run out
 * of its own reasons not to fire gets measured against the loop.
 */
export function ruleState(
  rule: Pick<RuleFacts, "enabled" | "trigger_kind" | "unavailable_reason">,
  engineLive: boolean,
): { state: RuleStateName; label: string } {
  if (rule.unavailable_reason) return { state: "warn", label: "UNAVAILABLE" };
  if (!rule.enabled) return { state: "disabled", label: "OFF" };
  if (rule.trigger_kind === "manual") return { state: "idle", label: "MANUAL" };
  if (!engineLive) return { state: "warn", label: "WILL NOT FIRE" };
  return { state: "ok", label: "ARMED" };
}

/** How long until a future timestamp, or how far past it we already are. */
export function whenNext(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  const seconds = Math.round((then - now) / 1000);
  if (seconds < 0) return `overdue by ${spanOf(-seconds)}`;
  if (seconds < 60) return `in ${seconds}s`;
  return `in ${spanOf(seconds)}`;
}

/** A gap in seconds as the coarsest unit that still says something useful. */
export function spanOf(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  return `${Math.round(hours / 24)}d`;
}

/**
 * What one rule does, in a sentence, from the stored action.
 *
 * The two failure wordings are not padding. `nexus.automations.execute` refuses an
 * action with no kind, and `_notify` refuses one with an empty message -- both
 * permanently, disabling the rule. A row in either state is therefore something the
 * user will meet, and "no action" is a more useful thing to read than an empty cell.
 */
export function actionLine(rule: Pick<RuleFacts, "action">): string {
  const kind = String(rule.action?.kind ?? "");
  if (!kind) return "no action — this rule has nothing to do";
  if (kind === "notify") {
    const message = String(rule.action?.message ?? "").trim();
    return message ? `Remind: "${message}"` : "notify, but with no message to deliver";
  }
  return kind;
}

/** The schedule as words, from the two fields that define it. */
export function triggerLine(rule: Pick<RuleFacts, "trigger_kind" | "schedule">): string {
  if (rule.trigger_kind === "manual") return "Manual only";
  if (rule.trigger_kind === "interval") return `Every ${rule.schedule || "—"}`;
  return rule.schedule ? `At ${rule.schedule}` : "No time set";
}

/** `automation_runs.status` is a CHECK-constrained set; map it to the shared vocabulary. */
export function statusState(status: string): RunStateName {
  switch (status) {
    case "completed":
      return "ok";
    case "failed":
      return "failed";
    case "skipped":
      return "cancelled";
    case "running":
      return "active";
    default:
      return "idle";
  }
}

/**
 * `db.repos.automations.TRIGGER_KINDS`, with what each one means in words.
 *
 * Here rather than in the form so that `scheduleHint` can switch on the union with
 * no `default` branch: adding a fourth kind then stops this file compiling until it
 * has a hint, instead of silently inheriting the manual one.
 */
export const TRIGGERS = [
  { value: "schedule", label: "Daily at a time" },
  { value: "interval", label: "Every so often" },
  { value: "manual", label: "Only when I run it" },
] as const;

export type TriggerValue = (typeof TRIGGERS)[number]["value"];

/** `nexus.automations._notify` accepts these and falls back to `info` for anything else. */
export const LEVELS = [
  { value: "info", label: "Info" },
  { value: "success", label: "Success" },
  { value: "warning", label: "Warning" },
  { value: "error", label: "Error" },
] as const;

export type LevelValue = (typeof LEVELS)[number]["value"];

/**
 * Action kinds this app can fill in a form for.
 *
 * Deliberately a separate list from the engine's `action_kinds`: that one says what
 * the core can *run*, this one says what the form can *compose*, and they are not the
 * same claim. When the core reports a kind that is not in here, the screen names it
 * as available rather than offering an editor that would submit an empty action.
 */
export const COMPOSABLE: readonly string[] = ["notify"];

/** What to type into the schedule box, for the trigger kind that is selected. */
export function scheduleHint(trigger: TriggerValue): string {
  switch (trigger) {
    case "schedule":
      return "A time of day: 08:30. Add days to restrict it: mon,wed,fri 08:30. Local time.";
    case "interval":
      return "A gap: 15m, 2h, 90s. Thirty seconds is the shortest the core accepts.";
    case "manual":
      return "Nothing to schedule — this rule only fires from the RUN NOW button.";
  }
}
