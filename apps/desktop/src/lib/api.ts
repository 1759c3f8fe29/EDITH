/**
 * The only way this UI reaches the machine (§65).
 *
 * Two design decisions worth stating, because both are the kind of thing that
 * gets "simplified" away later:
 *
 * 1. **No silent fallback to a default value.** Every call either returns real
 *    data or throws an `ApiError` carrying the core's own sentence. A helper that
 *    returned `[]` on failure would render an empty Memory screen for a database
 *    error, and the user would conclude they had no memories (§60, §78).
 *
 * 2. **No client-side timeout shorter than the core's.** A cold NVIDIA model has
 *    been measured taking over 100 seconds to answer. A 30-second fetch timeout
 *    would abort a request that was going to succeed and report it as a failure —
 *    the exact false negative the core was fixed to avoid. Long operations get
 *    long budgets, named at the call site.
 */

import type { FinishedTurn } from "./chat";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, message: string, detail = "") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }

  /** True when the core is unreachable, as opposed to reachable and unhappy. */
  get isOffline(): boolean {
    return this.status === 0;
  }
}

/** Deadlines, in ms, named by what they are for rather than by number. */
export const TIMEOUT = {
  /** Local reads: database queries, catalogues. */
  quick: 10_000,
  /** Anything that touches the filesystem or runs a command. */
  tool: 60_000,
  /** A tool call that may sit waiting for the user to answer a prompt. */
  awaitingUser: 300_000,
  /** Provider verification: NVIDIA cold starts have been measured past 110 s. */
  provider: 180_000,
} as const;

type Options = {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  timeoutMs?: number;
  signal?: AbortSignal;
};

async function request<T>(path: string, options: Options = {}): Promise<T> {
  const { method = "GET", body, timeoutMs = TIMEOUT.quick, signal } = options;

  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(new DOMException("timeout", "TimeoutError")), timeoutMs);
  if (signal) signal.addEventListener("abort", () => controller.abort(signal.reason), { once: true });

  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers: body === undefined ? undefined : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (cause) {
    // Status 0 means "never reached the core". Distinguished from every HTTP
    // error because it is the one case where the fix is "start the core", and
    // the shell renders a different screen for it (§60).
    const aborted = cause instanceof DOMException && cause.name === "TimeoutError";
    throw new ApiError(
      0,
      aborted ? "The core did not respond in time." : "Cannot reach the NEXUS core.",
      aborted
        ? `No answer after ${Math.round(timeoutMs / 1000)}s. It may still be working — check the activity log.`
        : "It may not be running. Start it with: python -m nexus.api",
    );
  } finally {
    window.clearTimeout(timer);
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!response.ok) {
    const record = (payload ?? {}) as Record<string, unknown>;
    // FastAPI puts a raised HTTPException's text in `detail`; the core's global
    // handler uses `message` + `detail`. Both are read so neither loses its
    // sentence on the way to the user.
    const message =
      (typeof record.message === "string" && record.message) ||
      (typeof record.detail === "string" && record.detail) ||
      `${response.status} ${response.statusText}`;
    const detail =
      typeof record.detail === "string" && record.detail !== message ? record.detail : "";
    throw new ApiError(response.status, message, detail);
  }

  return payload as T;
}

// ---------------------------------------------------------------- shared types

export type Health = {
  status: "ok" | "degraded";
  uptime_seconds: number;
  storage: { ok: boolean; detail: string; path: string; schema_version: number | null };
  /**
   * What went wrong with storage when the core started, or null. Separate from
   * `storage`, which is re-measured on every call: the core only starts the
   * monitors, the memory sweep and the embedding backfill when startup found the
   * database sound, and nothing retries them afterwards. So this can be set while
   * `storage.ok` is true, and that combination means "storage recovered but this
   * process is running without those features until it is restarted".
   */
  startup_error: string | null;
  background_features_started: boolean;
  pid: number;
  platform: string;
  session_type: string;
};

export type UnavailableTool = { name: string; title: string; reason: string };

export type Startup = {
  health: Health;
  permissions: Record<string, unknown>;
  tools: { total: number; available: number; unavailable: UnavailableTool[] };
  watching: WatchingEntry[];
  session_type: string;
};

export type WatchingEntry = { monitor: string; title: string; description: string };

export type Risk = "low" | "medium" | "high" | "critical";

export type Capability = {
  name: string;
  group: string;
  title: string;
  description: string;
  risk: Risk;
  reversible: boolean;
  consequence: string;
  grant_kinds: string[];
  second_confirmation: boolean;
  implicit: boolean;
  scoped: boolean;
};

/**
 * One stored grant, as `permissions_store.list_grants` returns it — a whole
 * `permissions` row plus two fields the repo adds.
 *
 * `expired` is computed by the core, and it is the one to trust: an unparseable
 * `expires_at` is treated as expired because permissions fail closed (§5). A UI that
 * compared `expires_at` to the browser clock would disagree with the engine whenever
 * the two clocks drift, and would show a live grant for something the engine will
 * refuse.
 *
 * `grant_kind: "deny"` is a stored *refusal*, not the absence of a grant. It has to be
 * shown as its own state — a denied capability that looked un-granted would invite the
 * user to wait for a prompt that will never come.
 */
export type Grant = {
  id: string;
  capability: string;
  scope: string;
  grant_kind: "deny" | "once" | "session" | "always" | "constrained";
  constraints: Record<string, unknown>;
  granted_at: string;
  expires_at: string | null;
  updated_at: string;
  /** Computed by the core against its own clock. Not derived from `expires_at` here. */
  expired: boolean;
};

export type SessionGrant = { capability: string; scope: string };

export type PendingRequest = {
  id: string;
  capability: string;
  scope: string;
  description: string;
  title: string;
  group: string;
  risk: Risk;
  reversible: boolean;
  consequence: string;
  grant_kinds: string[];
  second_confirmation: boolean;
  destructive: boolean;
  params: Record<string, unknown>;
  task_id: string | null;
  conversation_id: string | null;
  created_at: string;
};

/**
 * One tool, exactly as `tools/registry.py:Tool.to_dict` reports it (plus the route's
 * own `unavailable_reason`, which it re-adds on top).
 *
 * Four fields are easy to get wrong and all four are load-bearing:
 *
 * - **`available` is authoritative.** It is `not missing_binaries()`, computed by the
 *   core against this machine. Deriving availability from `unavailable_reason === null`
 *   happens to agree today, but the flag is the fact and the sentence is the
 *   explanation — the UI should not re-derive one from the other.
 * - **`verified_by_default` is whether a verifier exists at all**, which is what makes
 *   a result's `verified: null` legible. Without it, "no verifier" and "the check did
 *   not confirm" collapse into the same greyed-out pill.
 * - **`requires` entries may name alternatives with `|`** (`"wl-paste|xclip|xsel"`).
 *   Splitting on `|` and listing three names reads as three missing programs when it is
 *   one problem with one fix, so an entry is shown whole.
 * - **`group` and `reversible` come from the capability**, not the tool, and are what
 *   let the catalogue be grouped and sorted the way PERMISSIONS groups the same things.
 */
export type Tool = {
  name: string;
  title: string;
  description: string;
  capability: string;
  group: string;
  risk: Risk;
  reversible: boolean;
  read_only: boolean;
  idempotent: boolean;
  verified_by_default: boolean;
  parameters: Record<string, unknown>;
  available: boolean;
  unavailable_reason: string | null;
  requires: string[];
};

export type ToolResult = {
  ok: boolean;
  data: unknown;
  error: string | null;
  /** Tri-state on purpose: null means "this tool has no verifier", not "unverified". */
  verified: boolean | null;
  verification: string;
  duration_ms: number | null;
  summary: string;
};

/**
 * `Decision.to_dict()` from the permission engine.
 *
 * `source` is the field that lets the UI answer "why was I not asked?" — the engine
 * records `hard_limit | stored | session | user | timeout | disabled | implicit`, and
 * "already allowed for this session" and "reads only this machine's own state" are
 * different reassurances. Without it, a silent allow looks like the engine was skipped.
 */
export type PermissionDecision = {
  outcome: "allow" | "deny" | "ask";
  capability: string;
  scope: string;
  reason: string;
  source: string;
  detail: string;
  constraints: Record<string, unknown>;
};

export type Preview = {
  ok: boolean;
  error: string | null;
  tool: string;
  title: string;
  capability: string;
  risk: Risk;
  reversible: boolean;
  read_only: boolean;
  effect: string;
  consequence: string;
  will_prompt: boolean;
  /** False means no verifier exists — the result's `verified` will be `null`, not `false`. */
  verifiable: boolean;
  permission: PermissionDecision;
};

/**
 * One monitor, exactly as `monitors/base.py:Monitor.to_dict` reports it.
 *
 * Three fields carry the weight here:
 *
 * - **`enabled` is not `running`.** `enabled` is the per-monitor setting; `running` is
 *   whether a task is actually observing. The master switch (`monitor_enabled`) sits
 *   above both, so an enabled monitor with the master switch off is not watching, and
 *   the screen must never merge these into one indicator.
 * - **`unavailable_reason` is why a monitor that was asked to run is not running** —
 *   a missing binary, a Wayland session. It does not fix itself on the next tick.
 * - **`enabled_setting` names the settings key that turns it on**, so a toggle writes
 *   the key the core actually reads instead of a key the UI guessed from the id. A
 *   guessed key is accepted by `set_many`, read by nothing, and looks like it worked.
 */
export type MonitorState = {
  id: string;
  title: string;
  description: string;
  enabled: boolean;
  running: boolean;
  interval_seconds: number;
  samples: number;
  observations: number;
  last_run_at: string | null;
  last_observation_at: string | null;
  last_error: string | null;
  consecutive_errors: number;
  unavailable_reason: string | null;
  enabled_setting: string;
  interval_setting: string;
  capability: string;
};

/**
 * Totals over the observation history, from `db/repos/ambient.py:counts`.
 *
 * `retention_days` is part of the same payload for a reason: a count is only
 * interpretable next to how long rows survive. "412 observations" over a 7-day window
 * and over an unbounded one describe different machines, and `oldest`/`newest` are what
 * make the window visible instead of assumed.
 */
export type AmbientCounts = {
  total: number;
  by_monitor: Record<string, number>;
  by_kind: Record<string, number>;
  alerts: number;
  oldest: string | null;
  newest: string | null;
  retention_days: number;
};

/**
 * `Supervisor.status()` exactly — the four facts, and nothing derived.
 *
 * This is a separate type from `MonitorsOverview` because the settings PATCH returns
 * *this* under its `status` key, not the full overview: no `settings`, no
 * `ambient_counts`. Typing that response as the overview would let a screen read
 * `status.ambient_counts.total` and get `undefined` at runtime with no compile error.
 */
export type MonitorsStatus = {
  master_enabled: boolean;
  started: boolean;
  monitors: MonitorState[];
  watching: WatchingEntry[];
};

export type MonitorsOverview = MonitorsStatus & {
  /** Only the `monitor_*` and `ambient_*` keys — the route filters by prefix. */
  settings: Record<string, unknown>;
  ambient_counts: AmbientCounts;
};

export type AmbientEvent = {
  id: string;
  monitor: string;
  kind: string;
  summary: string;
  severity: "info" | "notice" | "alert";
  observed_at: string;
  created_at?: string;
  /** The observation's own fields. Shape varies by monitor, so it stays opaque. */
  data?: Record<string, unknown>;
  /** True when this row reports a condition *ending* — shown as resolved, not repeated. */
  clears?: boolean;
  dedupe_key?: string;
  /**
   * Present on `/grouped`, absent from `/timeline`'s raw rows: consecutive sightings
   * of the same `dedupe_key` are folded into one entry. A battery that stayed low for
   * an hour is one row with `occurrences: 12`, not twelve rows. A row with an empty
   * `dedupe_key` describes a moment rather than a condition and is never folded, so
   * two identical-looking window switches stay two entries.
   */
  occurrences?: number;
  first_at?: string;
  last_at?: string;
};

export type Memory = {
  id: string;
  title: string;
  body: string;
  category: string;
  approved: number;
  pinned: number;
  created_at: string;
  updated_at: string;
  project_id: string | null;
  vector_model?: string | null;
};

export type Conversation = {
  id: string;
  title: string;
  project_id: string | null;
  archived: number;
  created_at: string;
  updated_at: string;
  message_count?: number;
};

export type Message = {
  id: string;
  conversation_id: string;
  role: string;
  content: string;
  status: string;
  created_at: string;
  provider?: string | null;
  model?: string | null;
  capability?: string | null;
};

export type ActivityRow = {
  // TEXT primary key in the schema, not an autoincrement integer. Typed as a
  // number this silently works everywhere except a comparison, which is the worst
  // place for it to break.
  id: string;
  kind: string;
  level: "info" | "success" | "warning" | "error";
  summary: string;
  detail: Record<string, unknown> | null;
  created_at: string;
  task_id: string | null;
  conversation_id?: string | null;
};

export type Candidate = {
  key: string;
  provider: string;
  model: string;
  label: string;
  skipped: string | null;
};

export type RouteDecision = {
  job: string;
  transport: "chat" | "media";
  key: string;
  provider: string;
  model: string;
  label: string;
  reason: string;
  considered: Candidate[];
  fallbacks: Candidate[];
  warnings: string[];
};

/**
 * One model, from `providers/base.py:ModelSpec.to_dict`.
 *
 * `free` and the two cost fields are separate facts and all three can be absent-ish:
 * a `null` cost means the price was never declared, not that the model is free — only
 * `free: true` says that. Rendering a null as "$0" would recommend a paid model as a
 * cheap one.
 */
export type ModelSpec = {
  key: string;
  id: string;
  provider: string;
  label: string;
  context_window: number;
  max_output_tokens: number;
  supports_tools: boolean;
  supports_vision: boolean;
  supports_streaming: boolean;
  strengths: string[];
  input_cost_per_million: number | null;
  output_cost_per_million: number | null;
  latency: "fast" | "medium" | "slow";
  free: boolean;
  notes: string;
};

/**
 * One provider, from `Provider.to_dict`.
 *
 * `configured` means *a key is present*, not that the key works. Proving a key needs
 * `POST /api/models/verify`, which makes real network calls — the overview route makes
 * none, and says so. Treating `configured` as "working" is the fake status §60 warns
 * about, so the screen has to draw them as two different things.
 */
export type ProviderInfo = {
  id: string;
  name: string;
  /** The environment variable the key is read from. Never the key itself. */
  credential: string;
  console_url: string;
  configured: boolean;
  models: ModelSpec[];
};

/** One row of the job table, from `router/jobs.py:catalogue`. */
export type JobSpec = {
  job: string;
  setting: string;
  title: string;
  description: string;
  transport: "chat" | "media";
  /** The head of the chain — what the job uses when nothing is pinned. */
  default: string;
  candidates: string[];
};

/**
 * What a real call proved about each model — `db/repos/model_probes.py`.
 *
 * `blocked` and `flaky` are counted apart because only one of them changes routing: a
 * durable refusal (a 404 for this account, a rejected key) removes the model from the
 * chain, while a timeout leaves it in. NIM cold starts have been measured past 250
 * seconds, so benching a slow model would quietly shrink the bench with nothing on
 * screen to explain it.
 */
export type ProbeStats = {
  total: number;
  callable: number;
  blocked: number;
  flaky: number;
  last_checked: string | null;
};

export type ProbeRow = {
  key: string;
  provider: string;
  model: string;
  transport: string;
  ok: number;
  error_kind: string | null;
  /** 1 when the failure will not change on its own — the flag that skips the model. */
  durable: number;
  detail: string;
  latency_ms: number | null;
  checked_at: string;
};

/** One line of a live probe run: the target, merged with what happened to it. */
export type ProbeResult = {
  key: string;
  provider: string;
  model: string;
  transport: string;
  job: string;
  ok?: boolean;
  error_kind?: string | null;
  detail?: string;
  latency_ms?: number | null;
  stored?: boolean;
};

/**
 * One NIM specialist endpoint — `providers/nim_catalogue.py:catalogue`.
 *
 * `listed` is the one field that needs care. It means the provider's own `/models`
 * listing names this id, which proves the *name* is right and nothing else; NIM's
 * listing does not enumerate speech or reranking, so `listed: false` is not evidence
 * the endpoint is broken. `listing_checked` dates the claim. Only a probe proves a
 * call works, so the screen says "not in the listing" rather than "unavailable".
 */
export type NimSpecialist = {
  model: string;
  label: string;
  modality: string;
  jobs: string[];
  endpoint: string;
  listed: boolean;
  listing_checked: string;
  /** The endpoint rejects a text segment sent alongside the image. */
  image_only: boolean;
  /** The answer is a tool call or structured object, not prose. */
  structured_response: boolean;
  streamable: boolean;
  sample_rate: number | null;
  languages: string[];
  notes: string;
};

export type ModelsOverview = {
  providers: ProviderInfo[];
  nim_specialists: NimSpecialist[];
  routing: {
    main_model: string;
    router_mode: string;
    allow_fallback: boolean;
    jobs: JobSpec[];
    /** job → the pinned key, or `""` when the job follows its own chain. */
    assignments: Record<string, string>;
  };
  configured: string[];
  models: { total: number; available: number };
  /** Remembered from the last probe. No network call is made to produce this. */
  probes: { stats: ProbeStats; blocked: Record<string, string> };
};

/** A live credential check — `VerificationResult.to_dict`. */
export type Verification = {
  ok: boolean;
  message: string;
  models_seen: string[];
  latency_ms: number | null;
  error_kind: string | null;
};

export type Invocation = {
  id: string;
  tool: string;
  /** Derived by the route from the tool registry — not a column, so it may be absent. */
  capability?: string;
  params: Record<string, unknown>;
  ok: number;
  verified: number | null;
  error: string | null;
  duration_ms: number | null;
  created_at: string;
  task_id?: string | null;
};

/**
 * Real rates from `tool_invocations`, not a guess — `invocation_stats`.
 *
 * `verified` counts rows where the flag is exactly 1, so a tool with no verifier is
 * absent from it rather than counted as unverified. `succeeded` and `verified` are
 * therefore both out of `total` but they answer different questions, and neither is
 * a percentage the UI should invent when `total` is 0.
 */
export type InvocationStats = {
  total: number;
  succeeded: number;
  verified: number;
  avg_ms: number | null;
};

export type Task = {
  id: string;
  objective: string;
  status:
    | "pending"
    | "running"
    | "waiting_permission"
    | "completed"
    | "failed"
    | "cancelled"
    | "interrupted";
  project_id: string | null;
  conversation_id: string | null;
  result: string | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
  step_count?: number;
  done_count?: number;
};

/**
 * The payload of every task route that returns one task: the GET, and the three
 * write routes. `running` is whether a run is in flight *in the core process* —
 * not the stored status, which cannot tell a live run from one whose core died.
 */
export type TaskDetail = {
  task: Task & { steps: TaskStep[] };
  progress: TaskProgress;
  running: boolean;
  activity: ActivityRow[];
};

export type TaskStep = {
  id: string;
  task_id: string;
  idx: number;
  intent: string;
  capability: string | null;
  tool: string | null;
  params: Record<string, unknown>;
  // Step ids, or the integer ``idx`` of an earlier step — the core's
  // ``next_runnable_step`` resolves both, and the planner writes the integers.
  depends_on: (string | number)[];
  status: "pending" | "running" | "waiting_permission" | "completed" | "failed" | "skipped" | "cancelled";
  result: string | null;
  error: string | null;
  // Tri-state, and the null matters: no verifier exists for this step, which is
  // not the same fact as "checked and not confirmed".
  verified: number | null;
  // No `verification`, `started_at` or `finished_at` here: `task_steps` has no such
  // columns (see db/schema.py). A verifier's explanation is folded into `result` or
  // `error` by the agent, which is the only place it survives.
};

/**
 * Step counts, grouped by status.
 *
 * The keys are fixed by `db/repos/tasks.py:progress`, which always returns all seven
 * even when they are zero — so this is a closed shape rather than a `Record`. §80 says
 * progress may only be shown from real step counts, and a typed `completed` / `total`
 * is what makes a progress bar answerable instead of decorative.
 */
export type TaskProgress = {
  total: number;
  completed: number;
  failed: number;
  pending: number;
  running: number;
  skipped: number;
  waiting_permission: number;
};

/**
 * The answer to "what would Retry actually do?", from `GET /api/tasks/{id}/retry`.
 *
 * `can_retry` is false with a `reason` whenever the task's state makes a retry
 * meaningless — a run already in flight, a status a retry does not apply to, or no
 * step in a state worth re-running. The reason is the same sentence `POST` would
 * refuse with, so the screen can explain the disabled button instead of just
 * greying it out.
 */
export type RetryPreview = {
  can_retry: boolean;
  /** Empty when `can_retry` is true. Otherwise why not, in words. */
  reason: string;
  /** Exactly the steps that would run again, in plan order. */
  steps: RetryPreviewStep[];
  /** How many completed steps are kept rather than redone (§64). */
  keeping: number;
  /**
   * Tool names among those steps where running twice is not the same as running
   * once. Named by the core rather than derived here: it is the fact that should
   * change the user's answer, and a warning has to be findable without counting.
   */
  not_idempotent: string[];
};

export type RetryPreviewStep = {
  id: string;
  idx: number;
  intent: string;
  tool: string | null;
  status: string;
  /**
   * Tri-state. `null` means the step calls no tool at all — a thinking step, where
   * idempotence is not a fact about it either way and `false` would read as a
   * warning that does not apply.
   */
  idempotent: boolean | null;
};

export type Project = {
  id: string;
  title: string;
  description: string;
  status: string;
  created_at: string;
  updated_at: string;
  conversation_count?: number;
  task_count?: number;
  memory_count?: number;
};

/**
 * A stored rule: a trigger, a schedule, and an action.
 *
 * `enabled` and `unavailable_reason` must be read together. The core stores a rule
 * whose schedule or action it cannot honour *disabled, with the reason on the row*,
 * rather than dropping it or accepting it silently — so a rule can be present,
 * visibly off, and carrying a sentence saying why. A screen that rendered only
 * `enabled` would show a plain "off" toggle for something the user turned on (§78).
 *
 * `next_run_at` is computed from the schedule by the core and is null whenever the
 * rule is disabled, manual, or unparseable. It is never a guess made here.
 */
export type Automation = {
  id: string;
  name: string;
  trigger_kind: string;
  schedule: string;
  action: { kind?: string; message?: string; [key: string]: unknown };
  enabled: boolean;
  next_run_at: string | null;
  last_run_at?: string | null;
  /** The last attempt's outcome, including failures — not the last *success*. */
  last_result?: { status?: string; summary?: string; [key: string]: unknown } | null;
  /** Present and non-null only when the core refused to arm this rule. */
  unavailable_reason: string | null;
  /**
   * What the user agreed this rule may do, as capability names. Empty for every
   * rule that exists today, because `notify` reaches nothing outside NEXUS's own
   * database — so this is not yet rendered anywhere. It is typed now because the
   * core checks an action against it at fire time, and a screen that edits an
   * action without carrying this forward would silently reset a rule's consent.
   */
  capability_scope: string[];
  created_at: string;
  updated_at: string;
};

/** One attempt. Written for skipped and failed fires too, so "last run" is honest. */
export type AutomationRun = {
  id: string;
  automation_id: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  detail: { summary?: string; error?: string; manual?: boolean; [key: string]: unknown };
  /** The permission that stopped this run, when one did. */
  denied_capability?: string | null;
};

/**
 * The scheduler loop's own state.
 *
 * Returned alongside the rules by `list`, because the list is not interpretable
 * without it: a rule with a perfect schedule still never fires if the loop is not
 * running, and rows alone have no way to say so. `stopped_reason` is set when the
 * loop gave up rather than never having started.
 */
export type AutomationEngine = {
  running: boolean;
  enabled: boolean;
  tick_seconds: number;
  max_lateness_seconds: number;
  action_kinds: string[];
  ticks: number;
  /** Read live from the database on every call, not cached from the last tick. */
  due_now: number;
  stopped_reason: string | null;
  last: { fired: number; failed: number; skipped: number; at: string } | null;
};

/** Whether memory is searchable by meaning. Read from stored state, never a probe. */
export type EmbeddingStatus = {
  enabled: boolean;
  available: boolean;
  reason: string;
  model: string;
  approved: number;
  embedded: number;
  /** Above zero means some memories can only be found by the user's exact words. */
  unembedded: number;
  vector_models: string[];
  model_mismatch: boolean;
  threshold: number;
};

export type SweepRun = {
  examined: number;
  proposed: number;
  skipped?: number;
  error: string | null;
  finished_at?: string | null;
  duration_ms?: number | null;
};

export type SweepStatus = {
  running: boolean;
  enabled: boolean;
  interval_seconds: number;
  batch_size: number;
  grace_seconds: number;
  runs: number;
  cursor: string;
  backlog: number;
  stopped_reason: string | null;
  last: SweepRun | null;
  recent: SweepRun[];
};

/**
 * A memory recall result.
 *
 * `semantic_ran` and `semantic_hits` are two separate facts on purpose: "meaning
 * matching ran and nothing was relevant" and "meaning matching never happened"
 * look identical if you only keep one of them, and they call for different words
 * on screen.
 */
export type Recall = {
  items: Array<Memory & { score?: number; via?: string }>;
  semantic: boolean;
  semantic_ran: boolean;
  semantic_hits: number;
  keyword_hits: number;
  notice: string;
};

/**
 * A turn that is running right now — what a reloaded window needs to resync.
 *
 * `AiCore.active()` returns `[t.to_dict() for t in self._active.values()]`, so this
 * is the *same* dict a finished turn serialises to, with `status: "streaming"`. It
 * is therefore an alias rather than a hand-written subset: a subset would drop
 * `context` and `notices` and retype `intent`/`routing` as opaque records, and the
 * resync would render less than it was given.
 *
 * The import is type-only and is erased at compile time, so this does not create a
 * runtime cycle with `chat.ts` (which imports `ApiError` from here as a value).
 */
export type ActiveTurn = FinishedTurn;

/**
 * What the `system.status` tool actually reports, typed from the tool itself
 * (`tools/system.py:system_status`) rather than guessed.
 *
 * Every optional-or-null field here is a real absence on some machine: a desktop has
 * no `BAT*` in `/sys/class/power_supply`, so `battery` is genuinely `null`; a kernel
 * that does not expose `energy_now`/`power_now` cannot give `time_remaining`, so it is
 * omitted rather than estimated. Typing those as present would put "0%" and "0m left"
 * on screen for facts the machine never stated (§30, §61).
 */
export type SystemSnapshot = {
  host: string;
  os: string;
  kernel: string;
  uptime_seconds: number;
  uptime_human: string;
  cpu: {
    model: string;
    cores: number | null;
    load_1m: number | null;
    load_5m: number | null;
    load_15m: number | null;
  };
  memory: {
    total: number;
    available: number;
    used: number;
    percent_used: number;
    total_human: string;
    available_human: string;
    swap_total: number;
    swap_free: number;
  };
  disks: Array<{
    mount: string;
    total: number;
    used: number;
    free: number;
    percent_used: number;
    total_human: string;
    free_human: string;
  }>;
  battery: {
    percent: number | null;
    status: string;
    charging: boolean;
    device: string;
    time_remaining?: string;
  } | null;
  network: {
    interfaces: Array<{ name: string; state: string; type: string }>;
    has_default_route: boolean;
    /** The tool's own caveat about what a default route does and does not prove. */
    note: string;
  };
};

/**
 * One voice from `/api/voice/voices/live`.
 *
 * The field names are the provider's, not ours: `mistral_audio.voices()` filters the
 * upstream list down to rows that have a `slug` and passes the rest through. The slug
 * is the identifier `speak` takes; the name is for reading. `gender`, `languages` and
 * `tags` are whatever the provider declared and may be empty — an empty `languages`
 * means it said nothing, not that the voice speaks nothing.
 */
export type Voice = {
  slug: string;
  /** `null` when the provider named the voice with a slug and nothing else. */
  name: string | null;
  gender: string | null;
  languages: string[];
  tags: string[];
};

/**
 * What `/api/voice/speak` actually returns.
 *
 * A location rather than bytes, so one synthesis can be replayed without being paid
 * for twice — and two locations, because the two shells cannot use the same one.
 * `path` is on disk, for a desktop shell that can hand it to the system player;
 * `url` is `/api/voice/audio/{name}`, which is what a web view must use because it
 * cannot open `file://` from an http origin. Play `url`.
 *
 * `url` is null only if the route wrote a file it could not name, which would be a
 * core bug — the caller should treat null as "nothing to play" rather than guessing
 * a path.
 */
export type Speech = {
  path: string;
  /** `/api/voice/audio/{name}` — playable. Null means there is nothing to play. */
  url: string | null;
  model: string;
  bytes: number;
  media_type: string;
  duration_s: number | null;
  latency_ms: number;
  /** The router key that produced it — §26 wants the model visible, not just "voice". */
  routed: string;
  voice: string;
};

/**
 * A transcription.
 *
 * `empty: true` is a *successful* call that heard nothing — a muted microphone or a
 * silent room. Distinct from a failure, and it must not be rendered as a blank
 * message the user thinks NEXUS ignored.
 */
export type Transcript = {
  text: string;
  model: string;
  language: string | null;
  duration_s: number | null;
  words: number;
  latency_ms: number;
  routed: string;
  empty: boolean;
  message?: string;
};

// ------------------------------------------------------------------- endpoints

/**
 * `db.maintenance.stats()` — what is on disk, and whether SQLite still trusts it.
 *
 * `integrity_ok` is the result of a real `PRAGMA integrity_check`, not an inference
 * from "the query worked". `counts` is per table and includes `memory_vectors` and
 * `search_index`, neither of which is an exported table — they are derived stores, and
 * a vector count that lags the memory count is the symptom of a half-finished embed.
 */
export type StorageStats = {
  path: string;
  bytes: number;
  schema_version: number | null;
  integrity_ok: boolean;
  integrity_detail: string;
  counts: Record<string, number>;
};

/**
 * What a retention sweep did, or would do.
 *
 * `plan` is the configured day counts, where **0 means keep forever** and is skipped
 * entirely — so a zero in the plan is not "delete everything older than today". With
 * `dry_run: true` the `removed` numbers are what *would* go; nothing was deleted.
 */
export type RetentionReport = {
  plan: Record<string, number>;
  removed: Record<string, number>;
  dry_run: boolean;
};

export type VacuumReport = { bytes_before: number; bytes_after: number; reclaimed: number };

/** `export_all()` — every exported table inline. `excluded` names what is deliberately absent. */
export type ExportPayload = {
  format: string;
  schema_version: number | null;
  exported_at: string;
  excluded: string[];
  tables: Record<string, unknown[]>;
  counts: Record<string, number>;
};

/**
 * The name of the event fired when stored settings change.
 *
 * Some settings are read continuously rather than on a screen: `tts_enabled` decides
 * whether every answer is spoken, `input_language` goes on every transcription. A
 * consumer of those cannot poll and must not go stale after the user flips a toggle
 * on the Settings screen, so every write announces itself here and live readers
 * re-fetch. Fired from `api.settings.patch`/`reset`, which is every write path.
 */
export const SETTINGS_CHANGED = "nexus:settings-changed";

function announceSettings(): void {
  try {
    window.dispatchEvent(new Event(SETTINGS_CHANGED));
  } catch {
    /* The write already succeeded; only the live re-read is missed. */
  }
}

export const api = {
  health: () => request<Health>("/health"),
  startup: () => request<Startup>("/api/startup"),
  systemStatus: () =>
    request<{ data: SystemSnapshot; summary: string; verified: boolean | null }>(
      "/api/system/status",
      { timeoutMs: TIMEOUT.tool },
    ),
  diagnostics: (limit = 200) =>
    request<{ records: unknown[]; database: Record<string, unknown> }>(
      `/api/diagnostics?limit=${limit}`,
    ),

  permissions: {
    overview: () =>
      request<{
        groups: Array<{ name: string; capabilities: Capability[] }>;
        grants: Grant[];
        /** Granted for this process only. Cleared on restart (§7), so never listed as stored. */
        session_grants: SessionGrant[];
        pending: PendingRequest[];
        /**
         * `engine.overview()`. Overlaps the keys above on purpose — it is the engine's
         * own view, and `grants_by_capability` is the part that is not derivable here
         * without re-grouping.
         */
        overview: {
          capabilities: Capability[];
          groups: string[];
          grants: Grant[];
          grants_by_capability: Record<string, Grant[]>;
          session_grants: SessionGrant[];
          pending: PendingRequest[];
        };
      }>("/api/permissions"),
    pending: () => request<{ pending: PendingRequest[] }>("/api/permissions/pending"),
    respond: (requestId: string, choice: string, constraints?: Record<string, unknown>) =>
      request<{ ok: boolean; decision?: Record<string, unknown> }>(
        `/api/permissions/${requestId}/respond`,
        { method: "POST", body: { choice, constraints: constraints ?? null } },
      ),
    cancel: (requestId: string) =>
      request<{ ok: boolean; message: string }>(`/api/permissions/${requestId}/cancel`, {
        method: "POST",
      }),
    revoke: (capability: string, scope = "*") =>
      request<{ ok: boolean; message: string }>("/api/permissions/revoke", {
        method: "POST",
        body: { capability, scope },
      }),
    revokeAll: () =>
      request<{ ok: boolean; counts: Record<string, number>; message: string }>(
        "/api/permissions/revoke-all",
        { method: "POST" },
      ),
    clearSession: () =>
      request<{ ok: boolean; cleared: number; message: string }>("/api/permissions/clear-session", {
        method: "POST",
      }),
    invocations: (limit = 100, tool?: string) =>
      request<{ invocations: Invocation[]; stats: InvocationStats }>(
        `/api/permissions/invocations?limit=${limit}${tool ? `&tool=${encodeURIComponent(tool)}` : ""}`,
      ),
  },

  tools: {
    list: () =>
      request<{ tools: Tool[]; total: number; available: number }>("/api/tools"),
    byCapability: () => request<{ map: Record<string, string[]> }>("/api/tools/by-capability"),
    /** The function-calling schemas the model is actually handed. */
    schemas: (onlyAvailable = true) =>
      request<{ schemas: Array<Record<string, unknown>> }>(
        `/api/tools/schemas?only_available=${onlyAvailable}`,
      ),
    preview: (name: string, params: Record<string, unknown> = {}) =>
      request<Preview>("/api/tools/preview", { method: "POST", body: { name, params } }),
    execute: (name: string, params: Record<string, unknown> = {}, conversationId?: string) =>
      request<ToolResult>("/api/tools/execute", {
        method: "POST",
        body: { name, params, conversation_id: conversationId ?? null },
        // The long budget: this call blocks while the permission dialog is open.
        timeoutMs: TIMEOUT.awaitingUser,
      }),
  },

  monitors: {
    overview: () => request<MonitorsOverview>("/api/monitors"),
    watching: () => request<{ watching: WatchingEntry[]; any: boolean }>("/api/monitors/watching"),
    updateSettings: (values: Record<string, unknown>) =>
      request<{
        ok: boolean;
        changes: Record<string, string>;
        unavailable: string[];
        status: MonitorsStatus;
      }>("/api/monitors/settings", { method: "PATCH", body: { values }, timeoutMs: TIMEOUT.tool }),
    timeline: (params: { limit?: number; monitor?: string; kind?: string; since?: string } = {}) => {
      const q = new URLSearchParams();
      if (params.limit) q.set("limit", String(params.limit));
      if (params.monitor) q.set("monitor", params.monitor);
      if (params.kind) q.set("kind", params.kind);
      if (params.since) q.set("since", params.since);
      return request<{ events: AmbientEvent[]; counts: AmbientCounts }>(
        `/api/monitors/timeline?${q}`,
      );
    },
    /** The same observations collapsed into sessions, which is how a day reads. */
    grouped: (params: { limit?: number; monitor?: string; since?: string } = {}) => {
      const q = new URLSearchParams();
      if (params.limit) q.set("limit", String(params.limit));
      if (params.monitor) q.set("monitor", params.monitor);
      if (params.since) q.set("since", params.since);
      return request<{ timeline: AmbientEvent[] }>(`/api/monitors/grouped?${q}`);
    },
    forget: (body: { since?: string; monitor?: string; everything?: boolean }) =>
      request<{ ok: boolean; removed: number; message: string }>("/api/monitors/forget", {
        method: "POST",
        body,
      }),
  },

  conversations: {
    list: (archived = false) =>
      request<{ conversations: Conversation[] }>(`/api/conversations?archived=${archived}`),
    create: (title = "New conversation") =>
      request<{ conversation: Conversation }>("/api/conversations", {
        method: "POST",
        body: { title },
      }),
    get: (id: string) =>
      request<{ conversation: Conversation; messages: Message[] }>(`/api/conversations/${id}`),
    rename: (id: string, title: string) =>
      request<{ conversation: Conversation }>(`/api/conversations/${id}`, {
        method: "PATCH",
        body: { title },
      }),
    archive: (id: string, archived: boolean) =>
      request<{ conversation: Conversation }>(`/api/conversations/${id}`, {
        method: "PATCH",
        body: { archived },
      }),
    remove: (id: string) =>
      request<{ ok: boolean; message: string }>(`/api/conversations/${id}`, { method: "DELETE" }),
  },

  memories: {
    list: (includePending = false) =>
      request<{
        memories: Memory[];
        pending_count: number;
        coverage: { approved: number; embedded: number };
      }>(`/api/memories?include_pending=${includePending}`),
    pending: () => request<{ memories: Memory[]; count: number }>("/api/memories/pending"),
    create: (title: string, body: string, category = "general") =>
      request<{ memory: Memory; indexing: string }>("/api/memories", {
        method: "POST",
        body: { title, body, category },
      }),
    approve: (id: string) =>
      request<{ memory: Memory; indexing: string }>(`/api/memories/${id}/approve`, {
        method: "POST",
      }),
    update: (id: string, patch: Partial<Pick<Memory, "title" | "body" | "category">> & { pinned?: boolean }) =>
      request<{ memory: Memory; indexing: string }>(`/api/memories/${id}`, {
        method: "PATCH",
        body: patch,
      }),
    remove: (id: string) =>
      request<{ ok: boolean; message: string; verified: boolean }>(`/api/memories/${id}`, {
        method: "DELETE",
      }),
    search: (q: string) =>
      request<{ memories: Memory[]; mode: string }>(
        `/api/memories/search?q=${encodeURIComponent(q)}`,
      ),
    /** Meaning + keyword, the same call the AI makes. Can involve a provider. */
    recall: (q: string, limit = 8, projectId?: string) => {
      const p = new URLSearchParams({ q, limit: String(limit) });
      if (projectId) p.set("project_id", projectId);
      return request<Recall>(`/api/memories/recall?${p}`, { timeoutMs: TIMEOUT.provider });
    },
    embeddings: () => request<{ embeddings: EmbeddingStatus }>("/api/memories/embeddings"),
    backfill: (limit = 500) =>
      request<{
        backfill: { embedded: number; failed: number; pending: number };
        coverage: { approved: number; embedded: number };
      }>("/api/memories/embeddings/backfill", {
        method: "POST",
        body: { limit },
        timeoutMs: TIMEOUT.provider,
      }),
    sweep: () => request<{ sweep: SweepStatus }>("/api/memories/sweep"),
    sweepRun: () =>
      request<{ result: SweepRun; sweep: SweepStatus; message: string }>(
        "/api/memories/sweep/run",
        { method: "POST", timeoutMs: TIMEOUT.provider },
      ),
  },

  tasks: {
    list: (params: { limit?: number; statuses?: string; projectId?: string } = {}) => {
      const q = new URLSearchParams();
      if (params.limit) q.set("limit", String(params.limit));
      if (params.statuses) q.set("statuses", params.statuses);
      if (params.projectId) q.set("project_id", params.projectId);
      return request<{ tasks: Task[] }>(`/api/tasks?${q}`);
    },
    /**
     * One task with its plan, its progress, and its activity.
     *
     * The steps say what was *planned*; the activity says what *happened*. Agent
     * progress has to be drawn from the second one — a step list alone can show a
     * tidy sequence of ticks for work that never ran.
     */
    get: (id: string) => request<TaskDetail>(`/api/tasks/${id}`),
    /**
     * Plan a task: a real model call that turns an objective into steps, so it is
     * slow and it can fail with a reason (no tool can do this, no model available).
     * With `run: true` the run is scheduled before the response returns; the
     * response is the plan either way, never a promise of one.
     */
    create: (
      objective: string,
      opts: { projectId?: string | null; conversationId?: string | null; run?: boolean } = {},
    ) =>
      request<TaskDetail>("/api/tasks", {
        method: "POST",
        body: {
          objective,
          project_id: opts.projectId ?? null,
          conversation_id: opts.conversationId ?? null,
          run: opts.run ?? false,
        },
        // Planning is one reasoning call. Cold starts have been measured past 110 s.
        timeoutMs: TIMEOUT.provider,
      }),
    /** Start (or resume) the run. Returns immediately — the work continues in the core. */
    run: (id: string) => request<TaskDetail>(`/api/tasks/${id}/run`, { method: "POST" }),
    /**
     * What a retry *would* re-run, without re-running it.
     *
     * A GET, and a separate call from `retry`, because a retry executes real tools
     * against this machine and some of them are not idempotent. The core's
     * step-level policy never repeats a non-idempotent tool on its own — it defers
     * to a human — and that deferral only means something if the human was shown
     * which steps and which tools before they agreed. `not_idempotent` is the field
     * that changes the answer, which is why the core names it separately instead of
     * leaving it to be counted out of `steps`.
     */
    retryPreview: (id: string) => request<RetryPreview>(`/api/tasks/${id}/retry`),
    /**
     * Re-run the steps of a finished task that did not succeed. 409 with a reason
     * when the task's state makes that meaningless — the same reason the preview
     * would have shown, so a caller that skipped the preview still gets it.
     * Completed steps are kept: this is "retry from the last good step".
     */
    retry: (id: string) => request<TaskDetail>(`/api/tasks/${id}/retry`, { method: "POST" }),
    /** Stop the run and withdraw any permission prompt it is waiting on. */
    cancel: (id: string) => request<TaskDetail>(`/api/tasks/${id}/cancel`, { method: "POST" }),
    /** Delete the task and its steps. Refused with 409 while a run is in flight. */
    remove: (id: string) => request<{ deleted: boolean; id: string }>(`/api/tasks/${id}`, { method: "DELETE" }),
  },

  projects: {
    list: (status: string | null = "active") =>
      request<{ projects: Project[] }>(
        `/api/projects${status ? `?status=${encodeURIComponent(status)}` : ""}`,
      ),
    create: (title: string, description = "") =>
      request<{ project: Project }>("/api/projects", {
        method: "POST",
        body: { title, description },
      }),
    update: (id: string, patch: { title?: string; description?: string; status?: string }) =>
      request<{ project: Project }>(`/api/projects/${id}`, { method: "PATCH", body: patch }),
    /**
     * Delete a project.
     *
     * The route returns `db/repos/projects.py:delete` verbatim, so the fields are
     * `deleted` and `detached` — there is no `ok` and no `message` to render. Deleting
     * a project detaches its conversations, memories and tasks rather than deleting
     * them, and `detached` is the count of what was let go, so the confirmation can
     * state the real consequence instead of implying a cascade.
     */
    remove: (id: string) =>
      request<{
        deleted: boolean;
        detached: { conversations: number; memories: number; tasks: number };
      }>(`/api/projects/${id}`, { method: "DELETE" }),
  },

  automations: {
    /**
     * Every rule, plus the engine state, in one call.
     *
     * Not two calls, because the two are only meaningful together and a screen that
     * fetched them separately could render armed-looking rows for a stopped loop
     * during the gap.
     */
    list: () => request<{ automations: Automation[]; engine: AutomationEngine }>("/api/automations"),
    /**
     * Create a rule. Returns the stored row directly, not wrapped.
     *
     * A success does not mean "this will fire": an unparseable schedule is stored
     * disabled with `unavailable_reason` set, so the caller must read the row back
     * rather than assume. An unrunnable *action* is a 400 instead — that one is
     * caught at the door, while the user is still looking at the form.
     */
    create: (body: {
      name: string;
      trigger_kind: string;
      schedule: string;
      action: Record<string, unknown>;
      enabled?: boolean;
    }) => request<Automation>("/api/automations", { method: "POST", body }),
    /** One rule with its run history attached as `runs`. */
    get: (id: string) => request<Automation & { runs: AutomationRun[] }>(`/api/automations/${id}`),
    /**
     * Patch a rule. Any change to timing recomputes `next_run_at` in the core, and
     * re-enabling a rule with a bad schedule turns it straight back off with the
     * reason — so the returned row, not the request, is what the screen must show.
     */
    update: (
      id: string,
      patch: {
        name?: string;
        trigger_kind?: string;
        schedule?: string;
        action?: Record<string, unknown>;
        enabled?: boolean;
      },
    ) => request<Automation>(`/api/automations/${id}`, { method: "PATCH", body: patch }),
    remove: (id: string) =>
      request<{ deleted: string }>(`/api/automations/${id}`, { method: "DELETE" }),
    /**
     * Fire one rule now, without waiting for its schedule.
     *
     * Runs the real action and writes a real run row — a test fire that left no
     * trace would make "last run" a lie by omission. It deliberately does not move
     * the rule's own next occurrence: checking a reminder works at 3pm must not
     * cancel it at 8pm. Given the tool budget because it executes an action.
     */
    run: (id: string) =>
      request<{
        status: string;
        summary: string;
        detail: Record<string, unknown>;
        automation: Automation;
      }>(`/api/automations/${id}/run`, { method: "POST", timeoutMs: TIMEOUT.tool }),
    runs: (id: string, limit = 20) =>
      request<{ runs: AutomationRun[] }>(`/api/automations/${id}/runs?limit=${limit}`),
  },

  chat: {
    /** Stop the turn on a conversation. `stopped: false` means nothing was running. */
    cancel: (conversationId: string) =>
      request<{ ok: boolean; stopped: boolean; message: string }>("/api/chat/cancel", {
        method: "POST",
        body: { conversation_id: conversationId },
      }),
    /** What is running right now — how a reloaded window avoids a stuck THINKING. */
    active: () => request<{ active: ActiveTurn[]; count: number }>("/api/chat/active"),
  },

  voice: {
    /** The default voice, with no network call, so Settings opens offline. */
    voices: () => request<{ default: string; configured: boolean }>("/api/voice/voices"),
    voicesLive: () =>
      request<{ voices: Voice[]; count: number; default: string }>("/api/voice/voices/live", {
        timeoutMs: TIMEOUT.provider,
      }),
    /**
     * Send a recording for transcription.
     *
     * `multipart/form-data`, not JSON: the route takes an `UploadFile`, and base64
     * in a JSON body would inflate a 25 MB ceiling by a third for no gain. The
     * `Content-Type` header is deliberately *not* set — the browser has to add its
     * own multipart boundary, and setting it by hand produces a boundary-less
     * header the server cannot parse.
     */
    transcribe: async (
      audio: Blob,
      filename = "recording.webm",
      language?: string,
    ): Promise<Transcript> => {
      const form = new FormData();
      form.append("file", audio, filename);
      // Omitted rather than sent empty: the route falls back to the
      // `input_language` setting, and an empty string would override it with nothing.
      if (language && language.trim()) form.append("language", language.trim());
      const controller = new AbortController();
      const timer = window.setTimeout(
        () => controller.abort(new DOMException("timeout", "TimeoutError")),
        TIMEOUT.provider,
      );
      let response: Response;
      try {
        response = await fetch("/api/voice/transcribe", {
          method: "POST",
          body: form,
          signal: controller.signal,
        });
      } catch (cause) {
        const timedOut = cause instanceof DOMException && cause.name === "TimeoutError";
        throw new ApiError(
          0,
          timedOut ? "Transcription did not finish in time." : "Cannot reach the NEXUS core.",
          timedOut ? "The recording may have been too long." : "It may not be running.",
        );
      } finally {
        window.clearTimeout(timer);
      }
      const text = await response.text();
      let payload: unknown = null;
      try {
        payload = JSON.parse(text);
      } catch {
        payload = null;
      }
      if (!response.ok) {
        const record = (payload ?? {}) as Record<string, unknown>;
        throw new ApiError(
          response.status,
          (typeof record.detail === "string" && record.detail) ||
            (typeof record.message === "string" && record.message) ||
            `${response.status} ${response.statusText}`,
        );
      }
      return payload as Transcript;
    },
    /**
     * Synthesise speech. Returns where the audio is, not the audio — see
     * {@link Speech}. Play `url`; `path` is for a shell with filesystem access.
     *
     * `voice` omitted means the `tts_voice` setting decides, and an empty setting
     * means the provider default. Passing a voice here overrides both.
     */
    speak: (text: string, voice?: string) =>
      request<Speech>("/api/voice/speak", {
        method: "POST",
        body: { text, voice: voice ?? null },
        timeoutMs: TIMEOUT.provider,
      }),
  },

  activity: (params: { limit?: number; kinds?: string; levels?: string; before?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.limit) q.set("limit", String(params.limit));
    if (params.kinds) q.set("kinds", params.kinds);
    if (params.levels) q.set("levels", params.levels);
    // Keyset pagination on `created_at`, not an offset: rows arrive constantly, and
    // an offset page 2 would skip whatever landed while page 1 was being read.
    if (params.before) q.set("before", params.before);
    return request<{ activity: ActivityRow[]; counts: Record<string, number> }>(`/api/activity?${q}`);
  },

  search: (q: string) =>
    request<{ results: unknown[]; counts: Record<string, number>; index_size: number }>(
      `/api/search?q=${encodeURIComponent(q)}`,
    ),

  models: {
    overview: () => request<ModelsOverview>("/api/models"),
    jobs: () => request<{ jobs: JobSpec[]; main_model: string }>("/api/models/jobs"),
    resolve: (job: string, opts: { needsVision?: boolean; minContextTokens?: number } = {}) => {
      const q = new URLSearchParams({ job });
      if (opts.needsVision) q.set("needs_vision", "true");
      if (opts.minContextTokens) q.set("min_context_tokens", String(opts.minContextTokens));
      // 409 when the configuration has no model that can do the job, 404 for an
      // unknown job name. Both arrive as an ApiError with the reason as its message.
      return request<{ decision: RouteDecision }>(`/api/models/resolve?${q}`);
    },
    nim: (modality?: string) =>
      request<{ specialists: NimSpecialist[] }>(
        `/api/models/nim${modality ? `?modality=${encodeURIComponent(modality)}` : ""}`,
      ),
    /** The only route that makes network calls on purpose. POST, so a refresh cannot spend quota. */
    verify: () =>
      request<{ results: Record<string, Verification>; checked: number; ok: number }>(
        "/api/models/verify",
        { method: "POST", timeoutMs: TIMEOUT.provider },
      ),
    /** Read-only: what past probes measured, with no network use. */
    probes: () =>
      request<{ probes: ProbeRow[]; stats: ProbeStats; blocked: Record<string, string> }>(
        "/api/models/probes",
      ),
    /**
     * Call every routable model once. The most expensive route in the API — it contacts
     * every model in every chain, fallbacks included, so it is never automatic.
     */
    probe: (jobs?: string[]) =>
      request<{
        checked: number;
        callable: number;
        blocked: number;
        flaky: number;
        duration_ms: number;
        results: ProbeResult[];
        summary: string;
      }>("/api/models/probe", {
        method: "POST",
        body: jobs && jobs.length > 0 ? { jobs } : {},
        timeoutMs: TIMEOUT.provider,
      }),
    /** Forget stored verdicts, so the next probe measures a model the user has just enabled. */
    clearProbes: (key?: string) =>
      request<{ cleared: number; key: string | null }>(
        `/api/models/probes${key ? `?key=${encodeURIComponent(key)}` : ""}`,
        { method: "DELETE" },
      ),
    embed: (text: string, inputType: "query" | "passage" = "query") =>
      request<{ model: string; input_type: string; dimensions: number; vector: number[] }>(
        "/api/models/embed",
        { method: "POST", body: { text, input_type: inputType }, timeoutMs: TIMEOUT.provider },
      ),
  },

  settings: {
    get: () =>
      request<{
        settings: Record<string, unknown>;
        defaults: Record<string, unknown>;
        orphans: string[];
      }>("/api/settings"),
    patch: async (values: Record<string, unknown>) => {
      const result = await request<{ ok: boolean; settings: Record<string, unknown> }>(
        "/api/settings",
        { method: "PATCH", body: { values } },
      );
      announceSettings();
      return result;
    },
    reset: async (key: string) => {
      const result = await request<{ ok: boolean; key: string; value: unknown }>(
        `/api/settings/${encodeURIComponent(key)}/reset`,
        { method: "POST" },
      );
      announceSettings();
      return result;
    },
  },

  storage: {
    stats: () => request<StorageStats>("/api/storage"),
    /**
     * Everything, as one JSON body (§70). The browser saves it; choosing where it
     * lands is the user's, not ours.
     */
    export: () =>
      request<ExportPayload>("/api/storage/export", { method: "POST", timeoutMs: TIMEOUT.tool }),
    vacuum: () =>
      request<VacuumReport>("/api/storage/vacuum", {
        method: "POST",
        timeoutMs: TIMEOUT.tool,
      }),
    /**
     * Apply the retention settings.
     *
     * `dryRun` defaults to true here as well as on the route, so the harmless call
     * is the one you get by accident. The real sweep deletes rows and must be an
     * explicit `false`.
     */
    retentionSweep: (dryRun = true) =>
      request<RetentionReport>("/api/storage/retention-sweep", {
        method: "POST",
        body: { dry_run: dryRun },
        timeoutMs: TIMEOUT.tool,
      }),
  },
};
