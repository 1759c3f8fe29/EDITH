/**
 * Reading a server-sent-event stream from the core, once (§57, §65).
 *
 * Two routes stream a turn -- `POST /api/chat` and `POST /api/voice/converse` --
 * and they frame their events identically because they share
 * `nexus.api.sse.drain` on the other side. So the reading half is shared here
 * rather than copied: a parser that exists twice is a parser that will disagree
 * with itself the first time either end changes, and the disagreement shows up as
 * "the model stopped streaming" rather than as a parsing bug.
 *
 * `fetch` plus a hand-written parser rather than `EventSource`, for one
 * disqualifying reason: `EventSource` can only issue GET requests with no body,
 * and a turn carries a message, a mode, possibly a screenshot, and possibly a
 * recording. The parser is small because the core only ever sends `data:` lines
 * and `:` comments.
 *
 * Two properties this module exists to guarantee, both inherited by every caller:
 *
 * 1. **A dropped connection is never silently an ended turn.** `onEnd` is called
 *    exactly once, with the reason it stopped, so the UI can say "the connection
 *    dropped, the turn may still be running" instead of leaving the sphere in
 *    THINKING forever (§71).
 * 2. **Aborting the read does not cancel the turn.** They are different acts. The
 *    core keeps running and persists the answer; cancelling is an explicit
 *    `api.chat.cancel(...)`. Anything else would abandon work mid-tool.
 */

/** The minimum every streamed event has: the core always sends a `type`. */
export type Framed = { type: string };

export type StreamHandlers<E extends Framed> = {
  onEvent: (event: E) => void;
  /**
   * Called exactly once. `reason` is empty when the stream ended the way it was
   * supposed to; otherwise it says what went wrong, and the turn may still be
   * running in the core.
   */
  onEnd: (reason: string) => void;
};

export type StreamRequest<E extends Framed> = {
  url: string;
  body: unknown;
  handlers: StreamHandlers<E>;
  /**
   * The event type that means "this stream finished properly". Its absence is what
   * turns a clean-looking end into a stated reason.
   */
  terminal?: string;
  /** What to say when the core cannot be reached at all. */
  offline?: string;
  /** What to say when the read stops before `terminal` arrived. */
  truncated?: string;
  /** Turns a pre-stream refusal into an event the caller understands. */
  refusal?: (message: string, kind: string) => E;
};

/**
 * Start reading. Returns a handle: `abort()` stops *reading*, which is not the
 * same as stopping the work on the other end.
 */
export function openStream<E extends Framed>(request: StreamRequest<E>): { abort: () => void } {
  const controller = new AbortController();
  void pump(request, controller.signal);
  return { abort: () => controller.abort(new DOMException("closed", "AbortError")) };
}

async function pump<E extends Framed>(request: StreamRequest<E>, signal: AbortSignal): Promise<void> {
  const {
    url,
    body,
    handlers,
    terminal = "done",
    offline = "Cannot reach the NEXUS core. It may not be running.",
    truncated = "The connection to the core dropped mid-turn. The turn may still be running — reload to resync.",
    refusal,
  } = request;

  let ended = false;
  const end = (reason: string) => {
    if (ended) return;
    ended = true;
    handlers.onEnd(reason);
  };

  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "text/event-stream" },
      body: JSON.stringify(body),
      signal,
    });
  } catch (cause) {
    end(isAbort(cause) ? "" : offline);
    return;
  }

  if (!response.ok) {
    // The core refused before the stream started, and its refusal is a sentence
    // worth showing: an unknown mode, a conversation that no longer exists, a turn
    // already running. Surfaced as an event as well as a reason so it lands in the
    // console like any other failure rather than only in a toast.
    const message = await refusalText(response);
    if (refusal) handlers.onEvent(refusal(message, httpKind(response.status)));
    end(message);
    return;
  }

  if (!response.body) {
    end("The core answered without a stream body.");
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finished = false;

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // Frames are separated by a blank line. The trailing partial frame stays in
      // the buffer -- a token can arrive split across two TCP reads, and parsing
      // half of one would drop it.
      let cut = buffer.indexOf("\n\n");
      while (cut !== -1) {
        const event = parseFrame<E>(buffer.slice(0, cut));
        buffer = buffer.slice(cut + 2);
        if (event) {
          handlers.onEvent(event);
          if (event.type === terminal) finished = true;
        }
        cut = buffer.indexOf("\n\n");
      }
    }
  } catch (cause) {
    end(isAbort(cause) ? "" : truncated);
    return;
  }

  end(
    finished
      ? ""
      : "The stream ended without finishing the turn. Check the activity log for what happened.",
  );
}

const isAbort = (cause: unknown): boolean =>
  cause instanceof DOMException && cause.name === "AbortError";

async function refusalText(response: Response): Promise<string> {
  const text = await response.text().catch(() => "");
  try {
    const parsed = JSON.parse(text) as { detail?: string; message?: string };
    return parsed.detail || parsed.message || `${response.status} ${response.statusText}`;
  } catch {
    return `${response.status} ${response.statusText}`;
  }
}

/**
 * One frame to one event, or null when the frame carries nothing.
 *
 * `: waiting 30s` is the core's heartbeat -- a comment, deliberately not an event,
 * so it needs no filtering downstream. An unreadable frame is *reported* rather
 * than dropped: silently skipping it would turn one bad payload into a missing
 * sentence with no explanation.
 */
export function parseFrame<E extends Framed>(frame: string): E | null {
  const data = frame
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (!data) return null;
  try {
    return JSON.parse(data) as E;
  } catch {
    return { type: "notice", text: "One event from the core could not be read." } as unknown as E;
  }
}

/** The core's own error `kind` vocabulary, from an HTTP status. */
export function httpKind(status: number): string {
  if (status === 409) return "busy";
  if (status === 404) return "missing";
  if (status === 403) return "forbidden";
  if (status === 413) return "too_large";
  if (status >= 500) return "internal";
  return "invalid";
}
