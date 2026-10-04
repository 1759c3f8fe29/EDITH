/**
 * Loading real data, with every state the spec requires (§46).
 *
 * §46: "Every major screen and feature must have real loading / empty / active /
 * disabled / processing / success / warning / error / offline / permission-required
 * states. Do not design only the happy path." That is not achievable if each screen
 * invents its own `useEffect` + `setState` pair — one of them will forget the error
 * branch and render an empty list for a failed request, which reads as "you have
 * nothing" rather than "this did not load".
 *
 * So loading is centralised here and the result type makes the error branch
 * impossible to skip: there is no `data` until `state === "ready"`.
 *
 * Two properties worth naming:
 *
 * - **A stale response never overwrites a fresh one.** Each run takes a generation
 *   number and a late reply from an abandoned request is dropped. Without it,
 *   typing in a search box renders results for whichever request happens to finish
 *   last, which is not necessarily the last one typed.
 * - **Refresh does not blank the screen.** `refreshing` is separate from `loading`,
 *   so a poll or a manual reload updates in place instead of flashing a skeleton
 *   over data the user was reading.
 *
 * `detail` is present on every variant, typed as the empty string on the two that
 * cannot carry one. A caller rendering an error already knows `data` is null; making
 * it narrow the union a second time just to read a secondary message is friction that
 * ends in `(state as any).detail`, and an absent detail and an empty detail mean the
 * same thing on screen.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "./api";

export type AsyncState<T> =
  | { state: "loading"; refreshing: false; data: null; error: null; offline: false; detail: "" }
  | { state: "ready"; refreshing: boolean; data: T; error: null; offline: false; detail: "" }
  | { state: "error"; refreshing: boolean; data: T | null; error: string; offline: boolean; detail: string };

export type AsyncResult<T> = AsyncState<T> & {
  /** Re-run without clearing what is on screen. */
  reload: () => void;
  /** Replace the data locally, for an optimistic update the caller already verified. */
  set: (next: T) => void;
};

const LOADING = {
  state: "loading",
  refreshing: false,
  data: null,
  error: null,
  offline: false,
  detail: "",
} as const;

export function useAsync<T>(load: () => Promise<T>, deps: unknown[] = []): AsyncResult<T> {
  const [value, setValue] = useState<AsyncState<T>>(LOADING);

  // The loader is captured in a ref so that a caller writing an inline arrow
  // function does not re-trigger the effect on every render. `deps` is the
  // explicit, honest way to say when it should re-run.
  const loader = useRef(load);
  loader.current = load;

  const generation = useRef(0);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const run = useCallback((mode: "initial" | "refresh") => {
    const mine = ++generation.current;
    setValue((previous) =>
      mode === "refresh" && previous.state !== "loading"
        ? { ...previous, refreshing: true }
        : LOADING,
    );

    loader
      .current()
      .then((data) => {
        if (!alive.current || mine !== generation.current) return;
        setValue({ state: "ready", refreshing: false, data, error: null, offline: false, detail: "" });
      })
      .catch((cause: unknown) => {
        if (!alive.current || mine !== generation.current) return;
        const api = cause instanceof ApiError ? cause : null;
        setValue((previous) => ({
          state: "error",
          refreshing: false,
          // Kept, not discarded: showing the last known data *labelled as stale*
          // beats blanking the screen, and §37 asks for exactly that distinction.
          data: previous.data,
          error: api?.message ?? (cause instanceof Error ? cause.message : String(cause)),
          detail: api?.detail ?? "",
          offline: api?.isOffline ?? false,
        }));
      });
  }, []);

  useEffect(() => {
    run("initial");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  const reload = useCallback(() => run("refresh"), [run]);
  const set = useCallback((next: T) => {
    setValue({ state: "ready", refreshing: false, data: next, error: null, offline: false, detail: "" });
  }, []);

  return { ...value, reload, set } as AsyncResult<T>;
}

/**
 * Run an action and report what actually happened (§56, §59).
 *
 * Every button that changes something on the machine goes through this, which is
 * what makes "no dead buttons" checkable rather than aspirational: the control is
 * disabled while in flight (so it cannot fire twice), and it ends in a real
 * success or a real error message — never in the same state it started, which
 * would be indistinguishable from nothing having happened.
 */
export type ActionState = {
  busy: boolean;
  /** A short sentence from the last successful run. */
  done: string;
  /** The failure, in the core's own words. */
  failed: string;
  run: () => void;
  clear: () => void;
};

export function useAction(
  action: () => Promise<string | void>,
  options: { onDone?: () => void } = {},
): ActionState {
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState("");
  const [failed, setFailed] = useState("");

  const act = useRef(action);
  act.current = action;
  const onDone = useRef(options.onDone);
  onDone.current = options.onDone;

  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const busyRef = useRef(false);
  const run = useCallback(() => {
    // Guarded on a ref, not on the state: two clicks in the same frame both see
    // the pre-render value of `busy` and both fire. This is the duplicate-action
    // prevention §59 asks for, at the one place every action passes through.
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setDone("");
    setFailed("");

    act
      .current()
      .then((message) => {
        if (!alive.current) return;
        setDone(typeof message === "string" && message ? message : "Done.");
        onDone.current?.();
      })
      .catch((cause: unknown) => {
        if (!alive.current) return;
        const api = cause instanceof ApiError ? cause : null;
        const text = api?.message ?? (cause instanceof Error ? cause.message : String(cause));
        setFailed(api?.detail ? `${text} ${api.detail}` : text);
      })
      .finally(() => {
        busyRef.current = false;
        if (alive.current) setBusy(false);
      });
  }, []);

  const clear = useCallback(() => {
    setDone("");
    setFailed("");
  }, []);

  return { busy, done, failed, run, clear };
}

/**
 * Re-run something on an interval, but only while the tab is visible.
 *
 * A background tab polling the core every ten seconds spends the user's battery to
 * refresh a screen nobody is looking at. The visibility check is also what stops a
 * laptop waking from sleep with a hundred queued intervals.
 */
export function usePoll(callback: () => void, everyMs: number, enabled = true): void {
  const fn = useRef(callback);
  fn.current = callback;

  useEffect(() => {
    if (!enabled || everyMs <= 0) return;
    let timer = window.setInterval(() => {
      if (document.visibilityState === "visible") fn.current();
    }, everyMs);

    // Coming back to a hidden tab should refresh immediately rather than waiting
    // out the rest of an interval that elapsed while it was hidden.
    const onVisible = () => {
      if (document.visibilityState !== "visible") return;
      fn.current();
      window.clearInterval(timer);
      timer = window.setInterval(() => {
        if (document.visibilityState === "visible") fn.current();
      }, everyMs);
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [everyMs, enabled]);
}
