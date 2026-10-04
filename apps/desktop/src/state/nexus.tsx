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
import { api, ApiError, type ActivityRow, type PendingRequest, type Startup } from "../lib/api";
import { link, type LinkStatus } from "../lib/events";
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

        if (type === "tool.started") {
          setRunningTools((current) => [
            ...current,
            {
              tool: String(payload.tool ?? "unknown"),
              title: String(payload.title ?? payload.tool ?? "A tool"),
              description: String(payload.description ?? ""),
              startedAt: Date.now(),
              taskId: typeof payload.task_id === "string" ? payload.task_id : null,
            },
          ]);
          return;
        }

        if (type === "tool.finished" || type === "tool.blocked") {
          // Removed by name, oldest first: `tool.started` carries no run id, so
          // two concurrent runs of the same tool are indistinguishable. Clearing
          // the oldest keeps the count right, which is what the state depends on.
          const name = String(payload.tool ?? "");
          setRunningTools((current) => {
            const at = current.findIndex((item) => item.tool === name);
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
    startup.reload();
    // `startup.reload` is stable but not worth listing — it would re-run this on
    // every render of the provider.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connected, seedPending, reloadFeed]);

  /*
   * Losing the connection clears the running-tool list, and that is a correctness
   * fix rather than tidying: those entries are cleared by `tool.finished`, which
   * arrives over the socket. With the socket down they would sit there forever, and
   * the sphere would show EXECUTING for a tool that finished ten minutes ago.
   */
  useEffect(() => {
    if (!connected) setRunningTools([]);
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
