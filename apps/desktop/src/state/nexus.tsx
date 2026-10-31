/**
 * The one piece of shared state (§40, §46, §60, §64, §97).
 *
 * Five things belong here and nothing else does: the connection to the core, the
 * startup snapshot, the permission queue, the activity feed, and the accent. Each
 * is here because more than one zone of the shell reads it and they must never
 * disagree — a sphere showing OFFLINE beside a panel happily listing devices is
 * exactly the incoherence §60 is about.
 *
 * Everything else — a memory list, a task, a model catalogue — is loaded by the
 * panel that shows it, through `useAsync`. Hoisting those here would turn the
 * provider into a cache with no invalidation story, and every screen would render
 * from data of unknown age.
 *
 * Two decisions that are the whole design:
 *
 * 1. **Permissions are event-carried.** `permission.requested` already contains the
 *    entire request, so the queue is built from the event rather than from a refetch
 *    it would trigger. This is the one interaction in the product where a round
 *    trip is visible: a tool call is *blocked* waiting on it, so the dialog has to
 *    appear the moment the core asks, not a request later. The queue is still seeded
 *    from `GET /api/permissions/pending` on connect, because a request raised before
 *    this window existed will not be in the replay buffer.
 *
 * 2. **The activity feed is bus-as-signal, database-as-truth.** `activity.record()`
 *    deliberately does not publish to the bus — so there is no `activity.*` event to
 *    subscribe to, and inventing one on the client would mean rendering a feed that
 *    is not what got stored. Instead *any* bus traffic is treated as "something
 *    happened, re-read the log", and `/api/activity` stays authoritative. A slow
 *    poll backs it up, because rows written with no accompanying event exist and are
 *    the exact reason this is not a pure event stream.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";

import { ACCENTS, AccentProvider, type Accent } from "../components/primitives";
import {
  api,
  ApiError,
  type ActivityRow,
  type PendingRequest,
  type Startup,
  type TaskDetail,
  type Speech,
} from "../lib/api";
import { link, type LinkStatus } from "../lib/events";
import {
  applyEvent,
  dismiss,
  isLive,
  ordered,
  seed,
  sweep,
  type HudState,
  type HudTask,
} from "../lib/taskhud";
import { useAsync, usePoll, type AsyncResult } from "../lib/useAsync";
import { SCREENS, type ScreenId } from "./screens";

/** How many activity rows the shared feed keeps. The Activity screen pages beyond it. */
const ACTIVITY_LIMIT = 120;

/**
 * Coalescing window for the activity refetch. A round of tool calls publishes half a
 * dozen events in a few milliseconds; without this each one would be its own query.
 */
const ACTIVITY_COALESCE_MS = 300;

/** Backstop poll: catches rows written with no bus event at all. */
const ACTIVITY_POLL_MS = 20_000;

/** The startup snapshot changes when tools appear or monitors start, not per second. */
const STARTUP_POLL_MS = 60_000;

/**
 * Bus events that fire faster than a human reads and never write an activity row.
 * `terminal.output` is one event *per line of output* — a build log would put the
 * activity query into a loop.
 */
const NOISY_EVENTS = ["terminal.output", "stream.heartbeat"];

/** The task statuses that mean "still going", as the list route's `statuses=` filter. */
const LIVE_TASK_STATUSES = "pending,running,waiting_permission";

/**
 * How often finished tasks are reaped from the HUD.
 *
 * Cheap by construction: `sweep` returns the same object when nothing has passed its
 * hold window, so the tick costs a set-compare while nothing is ending.
 */
const TASK_SWEEP_MS = 1_000;

const ACCENT_KEY = "nexus.accent";
const SCREEN_KEY = "nexus.screen";

/** A tool the core says is running right now, from `tool.started` / `tool.finished`. */
export type RunningTool = {
  tool: string;
  title: string;
  /** What the permission layer would describe this call as. Already redacted. */
  description: string;
  startedAt: number;
  taskId: string | null;
  stepId: string | null;
  /** Composite key (tool:taskId:stepId) so concurrent runs of the same tool are distinguishable. */
  runKey: string;
};

/** A proactive suggestion from the engine (L10) — a proposal the user can act on. */
export type ProactiveSuggestion = {
  id: string; // activity_id
  kind: string;
  summary: string;
  judgement: Record<string, unknown>;
  held: boolean;
  suppressed: number;
  action?: {
    tool: string;
    params: Record<string, unknown>;
    label: string;
  };
};

export type NexusValue = {
  /* ---- the connection ---- */
  link: LinkStatus;
  reconnect: () => void;
  dismissGap: () => void;

  /* ---- the machine as it was when we last asked ---- */
  startup: AsyncResult<Startup>;

  /* ---- the permission queue (§41, §97) ---- */
  pending: PendingRequest[];
  /** Answer a request. Sent over the socket if it is open, over HTTP if it is not. */
  respond: (id: string, choice: string, constraints?: Record<string, unknown>) => Promise<void>;
  withdraw: (id: string) => Promise<void>;

  /* ---- what is running on this machine right now ---- */
  runningTools: RunningTool[];

  /* ---- multi-step work, as it happens (§24, §80) ---- */
  /**
   * Every task the HUD is showing, in the order it draws them. Live tasks first.
   * Seeded from the core and then fed by `task.*` events — never by a guess about
   * what a button press should have done.
   */
  tasks: HudTask[];
  /** Task ids with a stop/retry in flight, so their buttons can be disabled. */
  taskBusy: string[];
  /** Why the last stop/retry on a task failed, in the core's own words. */
  taskErrors: Record<string, string>;
  stopTask: (id: string) => void;
  retryTask: (id: string) => void;
  /** Carry on a task the core left `interrupted`. See the note at the implementation. */
  runTask: (id: string) => void;
  /** Stop every live task. Each is cancelled independently; the failures are reported. */
  stopAllTasks: () => void;
  /** Take one task off the HUD. Display only — nothing on the core changes. */
  dismissTask: (id: string) => void;

  /* ---- proactive suggestions (Ultron-level AI) ---- */
  /** Active proactive suggestions the user can act on or dismiss. */
  proactiveSuggestions: ProactiveSuggestion[];
  /** Dismiss a proactive suggestion. */
  dismissProactiveSuggestion: (id: string) => void;
  /** Speak a proactive announcement via TTS. */
  speakProactive: (text: string) => Promise<Speech | null>;

  /* ---- the log ---- */
  activity: ActivityRow[];
  activityCounts: Record<string, number>;
  activityError: string;
  activityOffline: boolean;
  activityLoading: boolean;
  refreshActivity: () => void;

  /* ---- chrome ---- */
  accent: Accent;
  setAccent: (next: Accent) => void;
  screen: ScreenId;
  /** `focus` names something inside the screen — a conversation id, a task id. */
  focus: string | null;
  go: (screen: ScreenId, focus?: string | null) => void;
};

const NexusContext = createContext<NexusValue | null>(null);

/**
 * The link status, as an external store.
 *
 * `useSyncExternalStore` rather than a `useState` + subscribe pair because the
 * status object is replaced only when something actually changed (the link does its
 * own identity check), so React can bail out of the render itself. With `useState`
 * every event would schedule a render of the whole shell and then discard it.
 */
export function useLink(): LinkStatus {
  return useSyncExternalStore(
    useCallback((notify: () => void) => link.onStatus(notify), []),
    () => link.current(),
    () => link.current(),
  );
}

function storedAccent(): Accent {
  try {
    const saved = window.localStorage.getItem(ACCENT_KEY);
    if (saved && (ACCENTS as readonly string[]).includes(saved)) return saved as Accent;
  } catch {
    // Private-mode browsers throw on localStorage. A theme is not worth a crash.
  }
  return "cyan";
}

function storedScreen(): ScreenId {
  try {
    const saved = window.localStorage.getItem(SCREEN_KEY);
    if (saved && SCREENS.some((screen) => screen.id === saved)) return saved as ScreenId;
  } catch {
    /* fall through */
  }
  return "core";
}

function sentence(cause: unknown): string {
  if (cause instanceof ApiError) return cause.detail ? `${cause.message} ${cause.detail}` : cause.message;
  return cause instanceof Error ? cause.message : String(cause);
}

export function NexusProvider({ children }: { children: ReactNode }) {
  const status = useLink();

  const startup = useAsync(() => api.startup(), []);

  /* ------------------------------------------------------------- permissions */

  const [pending, setPending] = useState<PendingRequest[]>([]);

  const seedPending = useCallback(() => {
    api.permissions
      .pending()
      .then((result) => setPending(result.pending))
      .catch(() => {
        // Left alone rather than cleared. An unreachable core does not mean the
        // queue is empty, and emptying it here would dismiss a dialog for a tool
        // call that is still blocked and will still act if it is answered.
      });
  }, []);

  /* --------------------------------------------------------- running tools */

  const [runningTools, setRunningTools] = useState<RunningTool[]>([]);

  /* ---------------------------------------------------------------- tasks */

  const [tasks, setTasks] = useState<HudState>({});
  const [taskBusy, setTaskBusy] = useState<string[]>([]);
  const [taskErrors, setTaskErrors] = useState<Record<string, string>>({});

  /**
   * Merge one task's full detail — the task, its steps, its real counts.
   *
   * `GET /api/tasks/{id}` returns `progress` *beside* the task rather than inside it,
   * so the two are joined here. Both halves come from the core; neither is derived.
   */
  const applyDetail = useCallback((detail: TaskDetail) => {
    setTasks((current) =>
      seed(current, [{ ...detail.task, progress: detail.progress }], Date.now()),
    );
  }, []);

  /*
   * The seed. The core is the authority on which tasks exist; the socket is the
   * authority on what they are doing from then on.
   *
   * Two reads rather than one, deliberately. `GET /api/tasks` answers "which tasks
   * are live" — it filters in SQL, and a run whose `task.started` has scrolled out of
   * the replay buffer is in its reply and would otherwise be invisible. But it
   * returns task *rows*: no steps, and no `progress`, which on this panel would
   * render as "no steps yet". That is a claim, and a false one. So each live task is
   * then read in full. It is a handful of requests, bounded by how much work is
   * actually running, and it happens on connect and after a button — not on a timer.
   *
   * A task whose detail read fails is not shown at all, rather than shown with
   * invented zeroes. Its next event carries the whole snapshot anyway.
   */
  const refreshTasks = useCallback(() => {
    api.tasks
      .list({ statuses: LIVE_TASK_STATUSES, limit: 50 })
      .then((result) => {
        for (const row of result.tasks) {
          api.tasks
            .get(row.id)
            .then(applyDetail)
            .catch(() => {
              /* see above: absence over a guess */
            });
        }
      })
      .catch(() => {
        // Left alone rather than emptied, for the same reason the permission queue
        // is: an unreachable core does not mean nothing is running.
      });
  }, [applyDetail]);

  /*
   * A local clock, because §80's other half is that a finished task must not stay on
   * screen as if it were still going. `sweep` is identity-preserving when there is
   * nothing to reap, so this is a set-compare per second rather than a render.
   */
  useEffect(() => {
    const timer = window.setInterval(() => {
      setTasks((current) => sweep(current, Date.now()));
    }, TASK_SWEEP_MS);
    return () => window.clearInterval(timer);
  }, []);

  /*
   * One action per task at a time, guarded on a ref rather than on `taskBusy`:
   * two clicks in the same frame both read the pre-render value, and both would
   * fire. Pressing Stop twice is not a cosmetic problem — it is two cancels racing.
   */
  const acting = useRef(new Set<string>());

  const actOnTask = useCallback(
    (id: string, act: () => Promise<TaskDetail>) => {
      if (acting.current.has(id)) return;
      acting.current.add(id);
      setTaskBusy((current) => [...current, id]);
      setTaskErrors((current) => {
        if (!(id in current)) return current;
        const next = { ...current };
        delete next[id];
        return next;
      });

      act()
        .then((detail) => {
          /*
           * The response body is the core's own snapshot of the task *after* the
           * write, so applying it is a read, not optimism. It matters for Stop: a
           * cancelled task is not in the `statuses=` list above, so without this the
           * HUD would keep drawing a spinner until the matching event arrived — the
           * exact "stopped run wearing a spinner" failure this panel exists to
           * remove.
           */
          applyDetail(detail);
        })
        .catch((cause: unknown) => {
          setTaskErrors((current) => ({ ...current, [id]: sentence(cause) }));
        })
        .finally(() => {
          acting.current.delete(id);
          setTaskBusy((current) => current.filter((item) => item !== id));
        });
    },
    [applyDetail],
  );

  const stopTask = useCallback(
    (id: string) => actOnTask(id, () => api.tasks.cancel(id)),
    [actOnTask],
  );

  const retryTask = useCallback(
    (id: string) => actOnTask(id, () => api.tasks.retry(id)),
    [actOnTask],
  );

  /*
   * Carry on an interrupted task. Not the same operation as a retry: `interrupted`
   * means the core process died mid-run, the completed steps are still completed, and
   * `POST /run` picks up at the next pending one — where a retry would *rewrite* the
   * statuses of steps that failed, which is a different decision and a different
   * button.
   */
  const runTask = useCallback(
    (id: string) => actOnTask(id, () => api.tasks.run(id)),
    [actOnTask],
  );

  const dismissTask = useCallback((id: string) => {
    setTasks((current) => dismiss(current, id));
    setTaskErrors((current) => {
      if (!(id in current)) return current;
      const next = { ...current };
      delete next[id];
      return next;
    });
  }, []);

  /*
   * Emergency stop. Deliberately not `Promise.all` over one shared failure: each run
   * is independent, and one core-side refusal must not stop the others being asked.
   * `actOnTask` already reports per task, so the user sees which one did not stop.
   */
  // Read through a ref rather than closing over `tasks`: this callback is handed to a
  // button and must see the list as it is when it is pressed, not as it was when the
  // provider last built its value object.
  const tasksRef = useRef(tasks);
  tasksRef.current = tasks;
  const stopAllTasks = useCallback(() => {
    for (const task of ordered(tasksRef.current)) {
      if (isLive(task.status)) actOnTask(task.id, () => api.tasks.cancel(task.id));
    }
  }, [actOnTask]);

  /* ------------------------------------------------------------- the log */

  const feed = useAsync(() => api.activity({ limit: ACTIVITY_LIMIT }), []);
  const reloadFeed = feed.reload;

  const coalesce = useRef<number | null>(null);
  const queueFeedRefresh = useCallback(() => {
    if (coalesce.current !== null) return;
    coalesce.current = window.setTimeout(() => {
      coalesce.current = null;
      reloadFeed();
    }, ACTIVITY_COALESCE_MS);
  }, [reloadFeed]);

  useEffect(
    () => () => {
      if (coalesce.current !== null) window.clearTimeout(coalesce.current);
    },
    [],
  );

  /* -------------------------------------------------------- the subscription */

  useEffect(
    () =>
      link.on((event) => {
        const { type, payload } = event;

        if (type === "permission.requested") {
          // The event carries the whole request, so this is the request — not a
          // hint that one exists. Deduped by id because a reconnect replays.
          const request = payload as unknown as PendingRequest;
          if (!request.id) return;
          setPending((current) =>
            current.some((item) => item.id === request.id) ? current : [...current, request],
          );
          queueFeedRefresh();
          return;
        }

        if (
          type === "permission.resolved" ||
          type === "permission.timeout" ||
          type === "permission.cancelled"
        ) {
          const id = typeof payload.id === "string" ? payload.id : "";
          if (id) setPending((current) => current.filter((item) => item.id !== id));
          queueFeedRefresh();
          return;
        }

        if (type.startsWith("task.")) {
          // Every `task.*` event carries a whole snapshot (the task, its steps, real
          // counts) and every `task.step.*` one carries a step; `lib/taskhud.ts` is
          // where the merge lives, including the part that makes a replayed event
          // harmless. Applied here rather than in the panel because the panel
          // unmounts and a run does not.
          setTasks((current) => applyEvent(current, type, payload, Date.now()));
          // Falls through to the feed refresh below: a task event is a real thing
          // that happened and belongs in the activity log.
        }

        if (type === "proactive.suggestion") {
          // Proactive engine proposals — add to visible suggestions UI
          const payload_ = payload as {
            activity_id?: string;
            judgement?: Record<string, unknown>;
            summary?: string;
            held?: boolean;
            suppressed?: number;
            action?: { tool: string; params: Record<string, unknown>; label: string };
          };
          const suggestion: ProactiveSuggestion = {
            id: payload_.activity_id ?? crypto.randomUUID(),
            kind: String(payload_.judgement?.kind ?? "proactive"),
            summary: payload_.summary ?? "NEXUS has a suggestion",
            judgement: payload_.judgement ?? {},
            held: payload_.held ?? false,
            suppressed: payload_.suppressed ?? 0,
            action: payload_.action,
          };
          setProactiveSuggestions((current) => {
            // Dedupe by activity_id
            if (current.some((s) => s.id === suggestion.id)) return current;
            return [...current, suggestion];
          });
          queueFeedRefresh();
          return;
        }

        if (type === "tool.started") {
          const toolName = String(payload.tool ?? "unknown");
          const taskId = typeof payload.task_id === "string" ? payload.task_id : null;
          const stepId = typeof payload.step_id === "string" ? payload.step_id : null;
          // Use a composite key so concurrent runs of the same tool are distinguishable
          const runKey = `${toolName}:${taskId ?? "notask"}:${stepId ?? "nostep"}`;
          setRunningTools((current) => [
            ...current,
            {
              tool: toolName,
              title: String(payload.title ?? payload.tool ?? "A tool"),
              description: String(payload.description ?? ""),
              startedAt: Date.now(),
              taskId,
              stepId,
              runKey,
            },
          ]);
          return;
        }

        if (type === "tool.finished" || type === "tool.blocked") {
          // Match by runKey (composite of tool+task+step) to distinguish concurrent runs
          const toolName = String(payload.tool ?? "");
          const taskId = typeof payload.task_id === "string" ? payload.task_id : null;
          const stepId = typeof payload.step_id === "string" ? payload.step_id : null;
          const runKey = `${toolName}:${taskId ?? "notask"}:${stepId ?? "nostep"}`;
          setRunningTools((current) => {
            const at = current.findIndex((item) => item.runKey === runKey);
            if (at === -1) return current;
            return [...current.slice(0, at), ...current.slice(at + 1)];
          });
          queueFeedRefresh();
          return;
        }

        if (NOISY_EVENTS.includes(type)) return;
        queueFeedRefresh();
      }),
    [queueFeedRefresh],
  );

  /*
   * Reconnecting is the moment to re-read anything that could have changed while we
   * were not listening. The permission queue especially: a request raised during the
   * outage may be older than the replay buffer, and a blocked tool call waiting on a
   * dialog this window never drew is the worst failure in the product.
   */
  const connected = status.connected;
  useEffect(() => {
    if (!connected) return;
    seedPending();
    reloadFeed();
    refreshTasks();
    startup.reload();
    // `startup.reload` is stable but not worth listing — it would re-run this on
    // every render of the provider.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connected, seedPending, reloadFeed, refreshTasks]);

  /*
   * Losing the connection clears the running-tool list and the task HUD, and that is
   * a correctness fix rather than tidying: both are kept current by events that
   * arrive over the socket. With the socket down, a tool and a task that finished
   * ten minutes ago would sit there forever, and the sphere would show EXECUTING and
   * the HUD a spinner for work that is not happening. A stale "working" is worse than
   * no answer, because the user acts on it.
   */
  useEffect(() => {
    if (connected) return;
    setRunningTools([]);
    setTasks({});
  }, [connected]);

  usePoll(reloadFeed, ACTIVITY_POLL_MS, connected);
  usePoll(startup.reload, STARTUP_POLL_MS, connected);

  /* ------------------------------------------------------------- answering */

  const respond = useCallback(
    async (id: string, choice: string, constraints?: Record<string, unknown>) => {
      // The socket first, because the request is waiting on an in-process future
      // and the socket is the shortest path to it. HTTP is not a fallback for a
      // *failure* — both routes reach the same engine — it is the path when the
      // socket happens to be closed.
      if (link.respond(id, choice, constraints)) {
        setPending((current) => current.filter((item) => item.id !== id));
        return;
      }
      const result = await api.permissions.respond(id, choice, constraints);
      if (!result.ok) {
        throw new ApiError(
          409,
          "That request is no longer waiting for an answer.",
          "It was withdrawn, answered elsewhere, or it timed out.",
        );
      }
      setPending((current) => current.filter((item) => item.id !== id));
    },
    [],
  );

  const withdraw = useCallback(async (id: string) => {
    if (link.cancelPending(id)) {
      setPending((current) => current.filter((item) => item.id !== id));
      return;
    }
    await api.permissions.cancel(id);
    setPending((current) => current.filter((item) => item.id !== id));
  }, []);

  /* --------------------------------------------------------------- chrome */

  const [accent, setAccentState] = useState<Accent>(storedAccent);
  const setAccent = useCallback((next: Accent) => {
    setAccentState(next);
    try {
      window.localStorage.setItem(ACCENT_KEY, next);
    } catch {
      /* the theme still applies for this session */
    }
  }, []);

  // `data-accent` on <html> is where styles.css reads the palette from, so this is
  // the one place the token indirection is actually switched.
  useEffect(() => {
    document.documentElement.dataset.accent = accent;
  }, [accent]);

  const [screen, setScreen] = useState<ScreenId>(storedScreen);
  const [focus, setFocus] = useState<string | null>(null);

  // Proactive suggestions state (L10 - Ultron-level AI)
  const [proactiveSuggestions, setProactiveSuggestions] = useState<ProactiveSuggestion[]>([]);

  const dismissProactiveSuggestion = useCallback((id: string) => {
    setProactiveSuggestions((current) => current.filter((s) => s.id !== id));
  }, []);

  /** Speak a proactive announcement via TTS. Returns the speech info or null if TTS is disabled. */
  const speakProactive = useCallback(async (text: string): Promise<Speech | null> => {
    try {
      // Check if TTS is enabled in settings
      const settings = await api.settings.get();
      const ttsEnabled = settings.settings.tts_enabled === true;
      if (!ttsEnabled) {
        console.log("[proactive] TTS disabled, skipping speak");
        return null;
      }
      const speech = await api.voice.speak(text);
      return speech;
    } catch (cause) {
      console.warn("[proactive] speak failed:", cause);
      return null;
    }
  }, []);

  const go = useCallback((next: ScreenId, nextFocus: string | null = null) => {
    setScreen(next);
    setFocus(nextFocus);
    try {
      window.localStorage.setItem(SCREEN_KEY, next);
    } catch {
      /* navigation still works, it just will not be remembered */
    }
  }, []);

  const accentValue = useMemo(() => ({ accent, setAccent }), [accent, setAccent]);

  const value = useMemo<NexusValue>(
    () => ({
      link: status,
      reconnect: () => link.reconnectNow(),
      dismissGap: () => link.acknowledgeGap(),
      startup,
      pending,
      respond,
      withdraw,
      runningTools,
      tasks: ordered(tasks),
      taskBusy,
      taskErrors,
      stopTask,
      retryTask,
      runTask,
      stopAllTasks,
      dismissTask,
      proactiveSuggestions,
      dismissProactiveSuggestion,
      speakProactive,
      activity: feed.data?.activity ?? [],
      activityCounts: feed.data?.counts ?? {},
      activityError: feed.state === "error" ? feed.error : "",
      activityOffline: feed.state === "error" ? feed.offline : false,
      activityLoading: feed.state === "loading",
      refreshActivity: reloadFeed,
      accent,
      setAccent,
      screen,
      focus,
      go,
    }),
    [
      status,
      startup,
      pending,
      respond,
      withdraw,
      runningTools,
      tasks,
      taskBusy,
      taskErrors,
      stopTask,
      retryTask,
      runTask,
      stopAllTasks,
      dismissTask,
      proactiveSuggestions,
      dismissProactiveSuggestion,
      speakProactive,
      feed,
      reloadFeed,
      accent,
      setAccent,
      screen,
      focus,
      go,
    ],
  );

  return (
    <NexusContext.Provider value={value}>
      <AccentProvider value={accentValue}>{children}</AccentProvider>
    </NexusContext.Provider>
  );
}

export function useNexus(): NexusValue {
  const value = useContext(NexusContext);
  if (!value) throw new Error("useNexus must be used inside <NexusProvider>.");
  return value;
}

/**
 * The head of the permission queue.
 *
 * One dialog at a time, oldest first, deliberately: stacking modals for several
 * requests makes it easy to approve the wrong one, and each request is holding its
 * own tool call so none of them is lost by being made to wait.
 */
export function usePermissionPrompt(): PendingRequest | null {
  const { pending } = useNexus();
  return pending[0] ?? null;
}

/** A one-line, honest description of the link, for the status bar. */
export function linkSentence(status: LinkStatus): string {
  if (status.connected) return "Connected to the core.";
  if (status.retryInSeconds > 0) {
    return `${status.reason} Retrying in ${status.retryInSeconds}s (attempt ${status.attempts}).`;
  }
  return status.reason || "Not connected.";
}

export { sentence as errorSentence };
