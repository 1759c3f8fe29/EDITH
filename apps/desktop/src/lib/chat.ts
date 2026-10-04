/**
 * The turn stream (§57, §58, §71).
 *
 * `POST /api/chat` answers with server-sent events, so this is not a `request<T>`
 * — it is a long-lived read that has to survive a cold model taking two minutes
 * to produce its first token.
 *
 * The reading itself lives in `./sse`, shared with `POST /api/voice/converse`,
 * because both routes frame their events through one function in the core and a
 * second parser here would be free to disagree with it. What stays in this file is
 * the part that is genuinely about a chat turn: the event *shapes*, and the
 * sentences shown when a turn ends badly.
 *
 * Two properties inherited from `./sse` and relied on throughout the UI:
 *
 * 1. **A dropped connection is never silently an ended turn.** If the stream stops
 *    without a `done` event, `onEnd` is called with the reason, so the UI can say
 *    "the connection dropped, the turn may still be running" and offer to resync
 *    from `/api/chat/active` — instead of leaving the sphere in THINKING forever.
 * 2. **Aborting the read does not cancel the turn.** They are different acts. The
 *    core keeps running and persists the answer; cancelling is an explicit
 *    `api.chat.cancel(...)`. Anything else would abandon work mid-tool.
 */
import { ApiError, type Preview, type RouteDecision } from "./api";
import { openStream, type StreamHandlers } from "./sse";

/**
 * Every event the core emits during a turn.
 *
 * These mirror `AiCore.stream`'s yielded dicts exactly, field for field. That is
 * worth being fussy about: the route frames those dicts straight onto the wire
 * without a translation layer, so a name invented here would typecheck perfectly
 * and then read `undefined` at runtime — the kind of bug that looks like "the model
 * did not call any tools" rather than like a client mistake.
 */
export type TurnEvent =
  | { type: "start"; conversation_id: string }
  | { type: "routing"; intent: IntentInfo; routing: RoutingInfo }
  | { type: "context"; context: ContextInfo }
  | { type: "text"; text: string }
  | { type: "reasoning"; text: string }
  | { type: "tool_call"; call: ToolCallInfo; preview: Preview | null }
  | { type: "tool_result"; result: ToolRunInfo }
  // `kind` is optional because only some notices have one: `/api/chat` sends the
  // sentence alone, while the voice route tags `tts_unavailable` so the UI can say
  // "on screen only" in its own words rather than reprinting the core's.
  | { type: "notice"; text: string; kind?: string }
  | { type: "error"; error: string; kind: string }
  | { type: "done"; turn: FinishedTurn };

export type ToolCallInfo = {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
  /** The model's original JSON. Kept so a malformed call can be shown as sent. */
  raw_arguments: string;
  /** Non-null when the model's arguments would not parse. Such a call never runs. */
  parse_error: string | null;
};

export type ToolRunInfo = {
  name: string;
  arguments: Record<string, unknown>;
  ok: boolean;
  /** Tri-state: null means this tool has no verifier, not "unverified". */
  verified: boolean | null;
  summary: string;
  error: string | null;
  duration_ms: number | null;
  /** Set when the call never ran at all, with the reason why. */
  rejected: string | null;
};

/**
 * How the request was read, and how much to trust that reading.
 *
 * `score` below 0.6 is what the core calls not confident, and it is not a defect —
 * it means no rule fired clearly, so tools stay attached rather than the classifier
 * guessing harder. `signals` are the phrases that decided it, which is what makes
 * "why did it route there" answerable (§15) without re-running the request.
 */
export type IntentInfo = {
  intent:
    | "conversation"
    | "action"
    | "multi_step"
    | "inspection"
    | "visual"
    | "memory_write"
    | "automation"
    | "reasoning";
  job: string;
  score: number;
  signals: string[];
  needs_vision: boolean;
  needs_tools: boolean;
};

/** Confident is a threshold on the score, not a field the core sends. */
export const CONFIDENT_SCORE = 0.6;
export const isConfident = (intent: IntentInfo | null): boolean =>
  Boolean(intent && intent.score >= CONFIDENT_SCORE);

/**
 * Which model answered, and why. This is `router.Decision.to_dict()` — the same
 * shape `/api/models/resolve` returns, so the routing panel and the live turn
 * render through one component.
 */
export type RoutingInfo = RouteDecision;

/**
 * What context was assembled, and what did not fit.
 *
 * `turns` and `memories` are counts, not arrays — the core deliberately sends
 * sizes rather than the content, because the content is already in the prompt and
 * shipping it twice would put the user's memories on the wire for a status line.
 * `dropped` is the honest account of what was shed to fit the window.
 */
export type ContextInfo = {
  blocks: Array<{ kind: string; heading: string; tokens: number }>;
  turns: number;
  memories: number;
  dropped: string[];
  notices: string[];
  tokens: number;
  budget: number;
};

export type FinishedTurn = {
  conversation_id: string;
  message_id: string;
  /**
   * `interrupted` is its own outcome, distinct from `failed`: the user stopped it,
   * or the provider stream dropped after some text had already arrived. Either way
   * partial work exists and must be labelled as partial rather than as a failure.
   */
  status: "complete" | "failed" | "interrupted" | "streaming";
  text: string;
  intent: IntentInfo | null;
  routing: RoutingInfo | null;
  tools: ToolRunInfo[];
  usage: Record<string, number>;
  rounds: number;
  latency_ms: number;
  error: string | null;
  notices: string[];
  /**
   * Partial on purpose. `Turn.context` starts as `{}` and is only filled once the
   * context assembler has run, so a turn that failed at routing finishes with an
   * empty object here. Typed as a full `ContextInfo` it would typecheck and then
   * render `undefined tokens of undefined`.
   */
  context: Partial<ContextInfo>;
};

export type ChatInput = {
  message: string;
  conversationId?: string | null;
  projectId?: string | null;
  taskId?: string | null;
  mode?: string | null;
  job?: string | null;
  /** Base64 image data the user picked, or a path to an image NEXUS captured. */
  attachments?: Array<{ data: string; media_type: string } | { path: string }>;
};

export type ChatHandlers = StreamHandlers<TurnEvent>;

/**
 * Start a turn. Returns a handle: `abort()` stops *reading* the stream, which is
 * not the same as stopping the turn (use `api.chat.cancel`).
 */
export function startTurn(input: ChatInput, handlers: ChatHandlers): { abort: () => void } {
  return openStream<TurnEvent>({
    url: "/api/chat",
    body: {
      message: input.message,
      conversation_id: input.conversationId ?? null,
      project_id: input.projectId ?? null,
      task_id: input.taskId ?? null,
      mode: input.mode ?? null,
      job: input.job ?? null,
      attachments: input.attachments ?? null,
    },
    handlers,
    refusal: (error, kind) => ({ type: "error", error, kind }),
  });
}

/** True when a thrown value is the core refusing rather than being unreachable. */
export function isRefusal(error: unknown): error is ApiError {
  return error instanceof ApiError && !error.isOffline;
}
