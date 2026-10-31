/**
 * The live task HUD's arithmetic (§F, §80).
 *
 * Pulled out of `components/TaskHud.tsx` for the same reason `lib/automations.ts`
 * was pulled out of its screen: this is the part that decides what the user is
 * *told* about work in progress, and a React component cannot be run without a
 * browser. Here it is pure -- state plus event plus clock in, new state out -- so
 * `npm run test:taskhud` can check it against `tests/taskhud.check.mjs`.
 *
 * Three rules are load-bearing rather than tidy, and each of them is a bug this
 * file exists to not have:
 *
 * **Events arrive out of order, so status is a lattice, not last-write-wins.**
 * The socket replays a buffer on reconnect, and `task.step.updated` is published
 * from a retry path that can interleave with the `task.step.finished` of the
 * attempt before it. Applying "whatever arrived last" would let a late
 * `step.started` walk a completed step back to *running* -- permanently, because
 * nothing else would arrive to correct it. So a step that has reached a terminal
 * status is never moved back to a live one, and a task that has reached a
 * terminal status stays there until something explicitly restarts it.
 *
 * **A task is never shown as running after it has been cancelled.** That is the
 * §79/§80 failure in its purest form: a stopped run wearing a spinner. It also
 * has a second edge -- a *cancelling* task is not a cancelled one, and there is no
 * "cancelling" status in the core's state machine, so nothing here invents one.
 *
 * **A step event for an unknown task creates nothing.** `task.step.*` carries a
 * task id and a step, never the objective, so a row built from one would be a
 * HUD entry with no goal that no other reader can explain. Live tasks are seeded
 * from the API on connect; anything the seed did not produce is dropped.
 *
 * Deliberately imports nothing. `tsc src/lib/taskhud.ts` has to compile this file
 * alone, so the event payload types are the narrow set of fields actually read,
 * and the status names are string literals that mirror the core's own CHECK
 * constraints (`nexus/db/schema.py`). `components/TaskHud.tsx` passes them where
 * `State` from `components/primitives.tsx` is required, so a name that stops being
 * a real state fails `tsc --noEmit`.
 */

/** Mirrors the CHECK constraint on `tasks.status`. */
export type HudTaskStatus =
  | "pending"
  | "running"
  | "waiting_permission"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

/** Mirrors the CHECK constraint on `task_steps.status`. */
export type HudStepStatus =
  | "pending"
  | "running"
  | "waiting_permission"
  | "completed"
  | "failed"
  | "skipped"
  | "cancelled";

/**
 * A subset of `State` in `components/primitives.tsx`, which is the app's one list
 * of state colours. Checked there, not here -- a name that stops being a real
 * state fails `tsc --noEmit` at the component's call site.
 */
export type HudStateName = "ok" | "active" | "pending" | "failed" | "cancelled" | "waiting" | "warn";

/** Real counts from `tasks.progress()` -- the only thing the UI may show (§80). */
export type HudProgress = {
  total: number;
  completed: number;
  failed: number;
  pending: number;
  running: number;
  skipped: number;
  waiting_permission: number;
};

export type HudStep = {
  id: string;
  idx: number;
  intent: string;
  tool: string | null;
  status: HudStepStatus;
  error: string | null;
};

export type HudTask = {
  id: string;
  objective: string;
  status: HudTaskStatus;
  steps: HudStep[];
  progress: HudProgress;
  startedAt: string | null;
  finishedAt: string | null;
  error: string | null;
  /**
   * When this task was first seen in a terminal status, or null while it is live.
   * The HUD keeps a finished task on screen for a few seconds so a run that ends
   * while the user is looking at it does not simply vanish; this is what that
   * clock is measured from.
   */
  settledAt: number | null;
};

/** Tasks by id. An object rather than an array so a merge is keyed, not searched. */
export type HudState = Record<string, HudTask>;

/**
 * How long a finished task stays on screen. Long enough to read "failed" and
 * register why, short enough that the panel is not a graveyard.
 */
export const TERMINAL_HOLD_MS = 12_000;

/** The steps the HUD keeps for a task. Enough to see the shape of the run. */
export const MAX_STEPS_KEPT = 60;

const TASK_STATUSES: readonly HudTaskStatus[] = [
  "pending",
  "running",
  "waiting_permission",
  "completed",
  "failed",
  "cancelled",
  "interrupted",
];

const STEP_STATUSES: readonly HudStepStatus[] = [
  "pending",
  "running",
  "waiting_permission",
  "completed",
  "failed",
  "skipped",
  "cancelled",
];

const TERMINAL_TASK: readonly HudTaskStatus[] = ["completed", "failed", "cancelled", "interrupted"];
const TERMINAL_STEP: readonly HudStepStatus[] = ["completed", "failed", "skipped", "cancelled"];

/** The events that can begin a task again after it has ended. */
const RESTART_EVENTS = new Set(["task.retried", "task.started"]);

const EMPTY_PROGRESS: HudProgress = {
  total: 0,
  completed: 0,
  failed: 0,
  pending: 0,
  running: 0,
  skipped: 0,
  waiting_permission: 0,
};

export function isTaskStatus(value: unknown): value is HudTaskStatus {
  return typeof value === "string" && (TASK_STATUSES as readonly string[]).includes(value);
}

export function isStepStatus(value: unknown): value is HudStepStatus {
  return typeof value === "string" && (STEP_STATUSES as readonly string[]).includes(value);
}

export function isTerminalTask(status: HudTaskStatus): boolean {
  return TERMINAL_TASK.includes(status);
}

export function isTerminalStep(status: HudStepStatus): boolean {
  return TERMINAL_STEP.includes(status);
}

/** A task the user should think of as in progress right now. */
export function isLive(status: HudTaskStatus): boolean {
  return status === "pending" || status === "running" || status === "waiting_permission";
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function readProgress(raw: unknown): HudProgress | null {
  if (raw === null || typeof raw !== "object") return null;
  const source = raw as Record<string, unknown>;
  return {
    total: asNumber(source.total),
    completed: asNumber(source.completed),
    failed: asNumber(source.failed),
    pending: asNumber(source.pending),
    running: asNumber(source.running),
    skipped: asNumber(source.skipped),
    waiting_permission: asNumber(source.waiting_permission),
  };
}

/** One step row from the core, as the HUD keeps it. */
export function readStep(raw: unknown): HudStep | null {
  if (raw === null || typeof raw !== "object") return null;
  const source = raw as Record<string, unknown>;
  const id = asString(source.id);
  if (!id) return null;
  if (!isStepStatus(source.status)) return null;
  return {
    id,
    idx: asNumber(source.idx),
    intent: asString(source.intent),
    tool: typeof source.tool === "string" && source.tool ? source.tool : null,
    status: source.status,
    error: typeof source.error === "string" && source.error ? source.error : null,
  };
}

/**
 * Merge one step into a task's list, refusing to move a finished step backwards.
 *
 * The refusal is the whole point. A `step.updated` republished from the retry path
 * and a `step.finished` from the attempt before it are both in flight, and without
 * this the finished step would be repainted as running the moment the older event
 * landed -- and then corrected by nothing, because the run had already moved on.
 */
function mergeStep(steps: HudStep[], step: HudStep): HudStep[] {
  const at = steps.findIndex((existing) => existing.id === step.id);
  if (at === -1) {
    const next = [...steps, step];
    next.sort((a, b) => a.idx - b.idx);
    return next.slice(0, MAX_STEPS_KEPT);
  }
  // Non-null because `at` came from `findIndex` on this same array, and the array is
  // not touched in between. The app's `noUncheckedIndexedAccess` cannot see that.
  const existing = steps[at]!;
  if (isTerminalStep(existing.status) && !isTerminalStep(step.status)) {
    return steps;
  }
  const merged: HudStep = { ...existing, ...step };
  const next = [...steps];
  next[at] = merged;
  return next;
}

function readTaskSnapshot(payload: Record<string, unknown>, previous: HudTask | undefined): HudTask | null {
  const id = asString(payload.id);
  if (!id) return null;
  const rawSteps = Array.isArray(payload.steps) ? payload.steps : [];
  const steps = rawSteps
    .map(readStep)
    .filter((step): step is HudStep => step !== null);
  const progress = readProgress(payload.progress) ?? previous?.progress ?? EMPTY_PROGRESS;

  let status: HudTaskStatus;
  if (isTaskStatus(payload.status)) status = payload.status;
  else status = previous?.status ?? "pending";

  return {
    id,
    objective: asString(payload.objective, previous?.objective ?? ""),
    status,
    // Events carry a whole step list, but they can be replayed in a different
    // order than they were produced, so the merge is by step id and never a
    // blind replacement -- an older snapshot must not delete a step the newer
    // one had already added.
    steps: mergeStepLists(previous?.steps ?? [], steps),
    progress,
    startedAt: asString(payload.started_at) || previous?.startedAt || null,
    finishedAt: asString(payload.finished_at) || previous?.finishedAt || null,
    error: asString(payload.error) || null,
    settledAt: previous?.settledAt ?? null,
  };
}

/** Union of two step lists by id, keeping each step's most advanced state. */
function mergeStepLists(current: HudStep[], incoming: HudStep[]): HudStep[] {
  let out = current;
  for (const step of incoming) out = mergeStep(out, step);
  return out;
}

/**
 * Apply one event. Unknown event types return the state unchanged, by identity,
 * so React can bail out of a re-render rather than churning on every heartbeat.
 */
export function applyEvent(
  state: HudState,
  type: string,
  payload: unknown,
  now: number,
): HudState {
  if (payload === null || typeof payload !== "object") return state;
  const body = payload as Record<string, unknown>;

  // Every `task.*` snapshot event carries the same object: the task row, its
  // steps and real counts. Which event it is decides only whether a terminal
  // task may be revived. The `task.step.*` family carries a step instead of a
  // snapshot and is handled below.
  if (type.startsWith("task.") && !type.startsWith("task.step.")) {
    const id = asString(body.id);
    if (!id) return state;
    const previous = state[id];

    // A replayed `task.replanned` from before a cancel must not restart a task
    // the user stopped. Only an explicit restart does that.
    if (previous && isTerminalTask(previous.status) && !RESTART_EVENTS.has(type)) {
      return state;
    }

    const merged = readTaskSnapshot(body, previous);
    if (!merged) return state;
    merged.settledAt = isTerminalTask(merged.status)
      ? previous?.settledAt ?? now
      : null;
    return { ...state, [id]: merged };
  }

  if (type.startsWith("task.step.")) {
    const taskId = asString(body.task_id);
    if (!taskId) return state;
    // No phantom rows: a step event says nothing about what the task is for, and
    // a HUD entry with a blank goal is worse than a missing one. Live tasks are
    // seeded from `api.tasks.list` when the window connects.
    const existing = state[taskId];
    if (!existing) return state;

    const step = readStep(body.step);
    const progress = readProgress(body.progress);
    return {
      ...state,
      [taskId]: {
        ...existing,
        steps: step ? mergeStep(existing.steps, step) : existing.steps,
        // `task.step.updated` and `task.step.retry` omit `progress`, so the last
        // known counts are kept rather than zeroed. Blanking them would tell the
        // user a half-finished run had done nothing.
        progress: progress ?? existing.progress,
      },
    };
  }

  return state;
}

/** Seed the HUD from `api.tasks.list`, so a reloaded window shows a live run. */
export function seed(state: HudState, tasks: unknown, now: number): HudState {
  if (!Array.isArray(tasks)) return state;
  let next = state;
  for (const raw of tasks) {
    if (raw === null || typeof raw !== "object") continue;
    const body = raw as Record<string, unknown>;
    const id = asString(body.id);
    if (!id) continue;
    const merged = readTaskSnapshot(body, next[id]);
    if (!merged) continue;
    merged.settledAt = isTerminalTask(merged.status) ? now : null;
    next = { ...next, [merged.id]: merged };
  }
  return next;
}

/** Drop terminal tasks whose hold window has passed. Live tasks are never dropped. */
export function sweep(state: HudState, now: number): HudState {
  let changed = false;
  const next: HudState = {};
  for (const [id, task] of Object.entries(state)) {
    if (isTerminalTask(task.status) && task.settledAt !== null
        && now - task.settledAt >= TERMINAL_HOLD_MS) {
      changed = true;
      continue;
    }
    next[id] = task;
  }
  return changed ? next : state;
}

/** Remove one task, because the user closed it. */
export function dismiss(state: HudState, id: string): HudState {
  if (!(id in state)) return state;
  const next = { ...state };
  delete next[id];
  return next;
}

/** How many tasks are in progress right now. Drives the emergency-stop bar. */
export function liveCount(state: HudState): number {
  return Object.values(state).filter((task) => isLive(task.status)).length;
}

/**
 * The order the HUD draws tasks in: everything still going first, oldest first, then
 * whatever has just ended, most recently settled first.
 *
 * Oldest first among the live ones, not newest, because the run that has been going
 * for four minutes is the one the user is waiting on — and because a list that
 * reorders itself every time a task starts moves the entry under the cursor. Ended
 * tasks sort the other way round so one that has just finished appears immediately
 * below the live ones, where the hold window is visible, rather than at the bottom.
 */
export function ordered(state: HudState): HudTask[] {
  const tasks = Object.values(state);
  const live = tasks.filter((task) => isLive(task.status));
  const ended = tasks.filter((task) => !isLive(task.status));
  live.sort((a, b) => startKey(a) - startKey(b) || a.id.localeCompare(b.id));
  ended.sort((a, b) => (b.settledAt ?? 0) - (a.settledAt ?? 0) || a.id.localeCompare(b.id));
  return [...live, ...ended];
}

/** When a task began, as a sort key. A task that has not started sorts last. */
function startKey(task: HudTask): number {
  const at = task.startedAt ? Date.parse(task.startedAt) : Number.NaN;
  return Number.isNaN(at) ? Number.POSITIVE_INFINITY : at;
}

/**
 * The state word and colour for a task. The word is not decoration: colour alone
 * is not an accessible signal (`StateDot`'s own docstring in
 * `components/primitives.tsx` says the same), so `TaskHud.tsx` renders both, and
 * this function decides the pair once.
 *
 * The colours are the app's shared ones, with one deliberate exception.
 * `stateOf("interrupted")` in `primitives.tsx` maps to `cancelled`, which is right
 * for a list: it is a task that is not running. Here it is `warn`, because the HUD
 * is the surface you look at to decide what to do next, and "the process died"
 * needs a different answer from the user than "you stopped this" -- the second
 * needs nothing, the first needs the task started again.
 */
export function taskState(status: HudTaskStatus): { state: HudStateName; label: string } {
  switch (status) {
    case "running":
      return { state: "active", label: "RUNNING" };
    case "waiting_permission":
      // Deliberately not "running". A task blocked on a permission dialog is
      // doing nothing at all, and showing it as running is the exact untruth
      // this HUD is meant to remove.
      return { state: "waiting", label: "WAITING FOR YOU" };
    case "pending":
      return { state: "pending", label: "QUEUED" };
    case "completed":
      return { state: "ok", label: "DONE" };
    case "failed":
      return { state: "failed", label: "FAILED" };
    case "cancelled":
      return { state: "cancelled", label: "STOPPED" };
    case "interrupted":
      return { state: "warn", label: "INTERRUPTED" };
  }
}

/** The state word and colour for a step. */
export function stepState(status: HudStepStatus): { state: HudStateName; label: string } {
  switch (status) {
    case "completed":
      return { state: "ok", label: "done" };
    case "failed":
      return { state: "failed", label: "failed" };
    case "running":
      return { state: "active", label: "running" };
    case "waiting_permission":
      return { state: "waiting", label: "waiting for you" };
    case "skipped":
      return { state: "cancelled", label: "skipped" };
    case "cancelled":
      return { state: "cancelled", label: "stopped" };
    case "pending":
      return { state: "pending", label: "queued" };
  }
}

/**
 * The six-step window centred on the active step.
 *
 * Centred rather than truncated from the start because a task with forty steps
 * would otherwise show its first six forever, and the step the user needs to see
 * is the one running now. Returns the window plus whether it is a slice, so the
 * component can say "steps 18-23 of 40" instead of implying there are only six.
 */
export function stepWindow(
  steps: HudStep[],
  size = 6,
): { shown: HudStep[]; first: number; last: number; total: number; sliced: boolean } {
  const total = steps.length;
  if (total <= size) {
    return { shown: steps, first: total ? 1 : 0, last: total, total, sliced: false };
  }
  const activeAt = steps.findIndex((step) => !isTerminalStep(step.status));
  const centre = activeAt === -1 ? total - 1 : activeAt;
  let start = centre - Math.floor(size / 2);
  start = Math.max(0, Math.min(start, total - size));
  return {
    shown: steps.slice(start, start + size),
    first: start + 1,
    last: start + size,
    total,
    sliced: true,
  };
}

/**
 * How long a task has been going, or how long it took.
 *
 * Measured from `started_at` and never from when this window first saw the task:
 * a reloaded page would otherwise report every run as seconds old. A task with no
 * `started_at` has not begun, and says so rather than showing 0s.
 */
export function elapsed(task: Pick<HudTask, "startedAt" | "finishedAt" | "status">,
                        now = Date.now()): string {
  if (!task.startedAt) return isLive(task.status) ? "not started" : "—";
  const from = Date.parse(task.startedAt);
  if (Number.isNaN(from)) return "—";
  const until = task.finishedAt ? Date.parse(task.finishedAt) : now;
  const seconds = Math.max(0, Math.round(((Number.isNaN(until) ? now : until) - from) / 1000));
  return spanText(seconds);
}

/** A duration as the coarsest unit that still says something useful. */
export function spanText(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

/**
 * The one-line progress figure. Counts and never a percentage: a percentage of a
 * step total that the planner is still revising is a number that goes backwards,
 * and 40% shrinking to 20% reads as a bug rather than as a replan (§80).
 */
export function progressLine(task: HudTask): string {
  const { completed, failed, skipped, total, waiting_permission } = task.progress;
  if (!total) return "no steps yet";
  const parts = [`${completed}/${total} steps`];
  if (failed) parts.push(`${failed} failed`);
  if (skipped) parts.push(`${skipped} skipped`);
  if (waiting_permission) parts.push(`${waiting_permission} waiting on you`);
  return parts.join(" · ");
}

/** The step the HUD highlights, or null when every step has finished. */
export function activeStep(task: HudTask): HudStep | null {
  return task.steps.find((step) => !isTerminalStep(step.status)) ?? null;
}

/**
 * The one sentence a screen reader hears when the picture changes.
 *
 * Deliberately built from the statuses, the goal and the counts, and from nothing
 * else — no clock, no step list, no "3s ago". It is rendered inside an
 * `aria-live="polite"` region, and a live region re-announces whenever its text
 * changes, so a ticking elapsed time in here would be read aloud once per second for
 * as long as the run lasts. The visible clock is outside that region for the same
 * reason; this is the part worth interrupting someone for.
 */
export function announcement(tasks: HudTask[]): string {
  const running = tasks.filter((task) => isLive(task.status));
  if (running.length > 1) {
    return `${running.length} tasks running: ${running.map((task) => task.objective).join("; ")}.`;
  }
  if (running.length === 1) {
    // Non-null by the length check above; `noUncheckedIndexedAccess` cannot see that.
    const task = running[0]!;
    const window = stepWindow(task.steps);
    const where = window.sliced ? `, step ${window.last} of ${window.total}` : "";
    return `${taskState(task.status).label}: ${task.objective}. ${progressLine(task)}${where}.`;
  }
  const last = tasks[0]!;
  if (!last) return "";
  return `${taskState(last.status).label}: ${last.objective}.`;
}
