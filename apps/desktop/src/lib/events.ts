/**
 * The event socket (§40, §97).
 *
 * One WebSocket for the whole window, carrying everything that happens whether or
 * not the user asked for it: permission prompts, monitor observations, tool
 * invocations, turn completions, memory proposals. The alternative — polling —
 * cannot be made honest: a UI that polls every second will show a permission
 * dialog for up to a second after the request already timed out.
 *
 * Three things this module is careful about, each because the naive version is
 * wrong in a way that only shows up after an hour of use:
 *
 * 1. **Sequence numbers, not timestamps.** The core replays from the last `seq`
 *    this client saw. Reconnecting is therefore lossless *within* the core's
 *    buffer and, crucially, *visibly* lossy beyond it — `gap` is reported so the
 *    activity feed can say "some events were missed" instead of silently having a
 *    hole in it.
 * 2. **Backoff with a ceiling, and a status the UI can render.** A core that is
 *    down should not be hammered, and "reconnecting in 8s" is a state the user can
 *    understand. `connected: false` is never presented as idle.
 * 3. **One socket, many listeners.** Every screen subscribing separately would
 *    open a dozen sockets and each would get its own replay. Listeners here are
 *    fan-out over a single connection.
 */

export type CoreEvent = {
  seq: number;
  type: string;
  at: string;
  payload: Record<string, unknown>;
};

export type LinkStatus = {
  connected: boolean;
  /** Set while waiting to retry, so the UI can count down instead of saying nothing. */
  retryInSeconds: number;
  attempts: number;
  /** Why we are not connected. Empty when connected. */
  reason: string;
  /** True when the core could not replay everything we missed. */
  gap: boolean;
  lastSeq: number;
};

type Listener = (event: CoreEvent) => void;
type StatusListener = (status: LinkStatus) => void;

const RETRY_STEPS_MS = [500, 1_000, 2_000, 4_000, 8_000, 15_000] as const;

class EventLink {
  private socket: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private statusListeners = new Set<StatusListener>();
  private timer: number | null = null;
  private countdown: number | null = null;
  private attempts = 0;
  private lastSeq = 0;
  private closing = false;

  private status: LinkStatus = {
    connected: false,
    retryInSeconds: 0,
    attempts: 0,
    reason: "Not connected yet.",
    gap: false,
    lastSeq: 0,
  };

  /** Subscribe to every event. Returns an unsubscribe. */
  on(listener: Listener): () => void {
    this.listeners.add(listener);
    this.ensureOpen();
    return () => {
      this.listeners.delete(listener);
    };
  }

  /** Subscribe to only the events whose type starts with one of `prefixes`. */
  onTypes(prefixes: string[], listener: Listener): () => void {
    return this.on((event) => {
      if (prefixes.some((prefix) => event.type.startsWith(prefix))) listener(event);
    });
  }

  onStatus(listener: StatusListener): () => void {
    this.statusListeners.add(listener);
    listener(this.status);
    this.ensureOpen();
    return () => {
      this.statusListeners.delete(listener);
    };
  }

  current(): LinkStatus {
    return this.status;
  }

  /** Answer a permission prompt on the channel it arrived on (§97). */
  respond(requestId: string, choice: string, constraints?: Record<string, unknown>): boolean {
    return this.send({
      type: "permission.respond",
      request_id: requestId,
      choice,
      constraints: constraints ?? null,
    });
  }

  cancelPending(requestId: string): boolean {
    return this.send({ type: "permission.cancel", request_id: requestId });
  }

  /**
   * Dismiss the "some events were missed" notice.
   *
   * `gap` is deliberately sticky — a hole in the activity feed does not stop being
   * true when the socket comes back, so nothing clears it automatically. This is
   * the user saying they have seen it, which is the only thing that should.
   */
  acknowledgeGap(): void {
    this.emitStatus({ gap: false });
  }

  /** Force a reconnect now — for a "retry" button, so the user is not made to wait out the backoff. */
  reconnectNow(): void {
    this.clearTimers();
    this.attempts = 0;
    if (this.socket) {
      this.closing = true;
      this.socket.close();
      this.socket = null;
      this.closing = false;
    }
    this.open();
  }

  private send(message: Record<string, unknown>): boolean {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return false;
    this.socket.send(JSON.stringify(message));
    return true;
  }

  private ensureOpen(): void {
    if (!this.socket && this.timer === null) this.open();
  }

  private open(): void {
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    // Same-origin, so the Vite dev proxy and the packaged shell both work without
    // a configured host. A hardcoded 127.0.0.1:8787 would break the moment the UI
    // is served from anywhere else.
    const url = `${scheme}://${window.location.host}/ws/events?after_seq=${this.lastSeq}`;

    let socket: WebSocket;
    try {
      socket = new WebSocket(url);
    } catch (cause) {
      this.scheduleRetry(cause instanceof Error ? cause.message : "Could not open the event socket.");
      return;
    }
    this.socket = socket;

    socket.onopen = () => {
      this.attempts = 0;
      this.clearTimers();
      this.emitStatus({ connected: true, reason: "", retryInSeconds: 0, attempts: 0 });
    };

    socket.onmessage = (message) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(String(message.data));
      } catch {
        return;
      }
      if (!parsed || typeof parsed !== "object") return;
      const record = parsed as Record<string, unknown>;

      // The core flattens the envelope: `Event.to_dict()` returns
      // `{seq, type, at, **payload}`, not a nested `payload` object. So the fields
      // are lifted back out here rather than reached for under a key that does not
      // exist. Replies to things we sent (pong, permission results) carry no seq;
      // they are still events as far as listeners are concerned — the permission
      // dialog needs to see its own result — so they get seq 0 rather than a drop.
      const { seq: rawSeq, type: rawType, at: rawAt, ...payload } = record;
      const seq = typeof rawSeq === "number" ? rawSeq : 0;
      if (seq > 0) this.lastSeq = Math.max(this.lastSeq, seq);

      const event: CoreEvent = {
        seq,
        type: String(rawType ?? ""),
        at: String(rawAt ?? ""),
        payload,
      };

      // The core says so itself when it could not replay everything. Kept in the
      // status rather than only logged, because "your activity feed has a hole in
      // it" is something the user has to be able to see.
      if (event.type === "stream.resumed" && event.payload.complete === false) {
        this.emitStatus({ gap: true });
      }
      if (event.type === "stream.dropped") {
        this.emitStatus({ gap: true });
      }

      this.emitStatus({ lastSeq: this.lastSeq });
      for (const listener of this.listeners) listener(event);
    };

    socket.onerror = () => {
      // Nothing actionable here: `onclose` always follows, and that is where the
      // retry belongs. Handled explicitly so the browser does not log it as
      // unhandled.
    };

    socket.onclose = (closed) => {
      this.socket = null;
      if (this.closing) return;
      this.scheduleRetry(
        closed.wasClean && closed.code === 1000
          ? "The core closed the event stream."
          : "Lost the connection to the core.",
      );
    };
  }

  private scheduleRetry(reason: string): void {
    this.clearTimers();
    const step = RETRY_STEPS_MS[Math.min(this.attempts, RETRY_STEPS_MS.length - 1)] ?? 15_000;
    this.attempts += 1;

    let remaining = Math.round(step / 1000);
    this.emitStatus({
      connected: false,
      reason,
      attempts: this.attempts,
      retryInSeconds: remaining,
    });

    // A visible countdown, not a silent wait. A user looking at "offline" with no
    // further information cannot tell a retrying app from a dead one.
    this.countdown = window.setInterval(() => {
      remaining = Math.max(0, remaining - 1);
      this.emitStatus({ retryInSeconds: remaining });
    }, 1_000);

    this.timer = window.setTimeout(() => {
      this.clearTimers();
      this.open();
    }, step);
  }

  private clearTimers(): void {
    if (this.timer !== null) {
      window.clearTimeout(this.timer);
      this.timer = null;
    }
    if (this.countdown !== null) {
      window.clearInterval(this.countdown);
      this.countdown = null;
    }
  }

  private emitStatus(patch: Partial<LinkStatus>): void {
    const next = { ...this.status, ...patch };
    // Cheap identity check: status updates arrive with every event, and rerendering
    // the shell on each token would be measurable.
    const changed = (Object.keys(next) as Array<keyof LinkStatus>).some(
      (key) => next[key] !== this.status[key],
    );
    if (!changed) return;
    this.status = next;
    for (const listener of this.statusListeners) listener(next);
  }
}

/** The one socket for this window. */
export const link = new EventLink();
