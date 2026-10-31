/**
 * The live task HUD (§F, §80, §81).
 *
 * A docked column above the status bar that answers one question without the user
 * having to navigate anywhere: **what is NEXUS doing to my machine right now, and is
 * it going well?** The Tasks screen answers that too, but only if you are looking at
 * it — and a run started from the instruction bar is exactly the run nobody is
 * watching a screen for.
 *
 * What it draws, and where each fact comes from:
 *
 * - The status word and dot come from `taskState` in `lib/taskhud.ts`, which returns
 *   a label as well as a colour because colour alone is not a signal (§92). The word
 *   is not decoration: `WAITING FOR YOU` and `RUNNING` are the two states a person
 *   most needs to tell apart, and they are the two that look identical if you only
 *   draw a dot.
 * - The counts are `tasks.progress()` — real `task_steps` rows by status, never an
 *   index and never elapsed time (§80). There is deliberately no progress *bar*: a
 *   percentage of a step total the planner is still revising goes backwards when it
 *   replans, and a bar that shrinks reads as a bug rather than as new information.
 * - The steps are the plan, and they are labelled as the plan. They are the window
 *   `stepWindow` chooses — six around the active one — so a forty-step task shows the
 *   step that is running rather than its first six forever, and says "18-23 of 40"
 *   instead of implying there are only six.
 * - `NOW` is what is actually executing, from the core's `tool.started` events. It is
 *   a separate line from the steps on purpose: §81 is about exactly this gap, and a
 *   step list alone will happily show a tidy row of ticks for work that never ran.
 *
 * Three things it deliberately does not have:
 *
 * - **No Pause, and no Resume-a-paused-task.** There is no `paused` status in the
 *   core's state machine; drawing a button the core cannot honour is the dead-control
 *   failure the earlier UI audit found. The stop path is `POST /cancel`, which is
 *   real, and the restart path for a task whose core died is `POST /run` on the
 *   `interrupted` status, which is also real.
 * - **No optimistic status.** Pressing Stop does not paint the task stopped. The
 *   response to the request *is* the core's post-write snapshot, so what gets applied
 *   is a read of what happened, not a guess about it.
 * - **No ticking clock inside the live region.** An `aria-live` region re-announces
 *   whenever its text changes, so a per-second elapsed time in there would be read
 *   aloud once a second for the length of the run. The spoken sentence is a separate,
 *   visually hidden line that only changes when the status, the goal or the counts do.
 */

import { useEffect, useState } from "react";

import { Icon } from "./Icon";
import { Button, Dialog, Panel, StateDot, StatusPill } from "./primitives";
import { api, type RetryPreview } from "../lib/api";
import {
  announcement,
  elapsed,
  isLive,
  isTerminalTask,
  progressLine,
  stepState,
  stepWindow,
  taskState,
  type HudTask,
} from "../lib/taskhud";
import { errorSentence, useNexus } from "../state/nexus";

/** How many of the task's currently-running tools to name. Beyond three it is a list. */
const NOW_LIMIT = 3;

export function TaskHud() {
  const {
    tasks,
    taskBusy,
    taskErrors,
    runningTools,
    stopTask,
    retryTask,
    runTask,
    stopAllTasks,
    dismissTask,
    go,
  } = useNexus();

  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [asking, setAsking] = useState<Asking | null>(null);

  const live = tasks.filter((task) => isLive(task.status)).length;
  const now = useNow(live > 0 ? 1_000 : 0);

  /*
   * What the screen reader hears.
   *
   * Recomputing this on every render is safe, and that is the point: React writes a
   * text node only when its value actually changed, so the per-second clock tick
   * re-renders this `<p>` with an identical string and nothing is announced. What
   * makes a live region chatty is putting a *changing* value inside it — which is
   * why `announcement` reads no clock, and why the visible elapsed time below is
   * outside this element.
   */
  const spoken = announcement(tasks);

  function askRetry(task: HudTask) {
    setAsking({ task, preview: null, failed: "" });
    api.tasks
      .retryPreview(task.id)
      .then((preview) =>
        setAsking((current) =>
          current && current.task.id === task.id ? { ...current, preview } : current,
        ),
      )
      .catch((cause: unknown) =>
        setAsking((current) =>
          current && current.task.id === task.id
            ? { ...current, failed: errorSentence(cause) }
            : current,
        ),
      );
  }

  return (
    <>
      {/* Always mounted, including when the panel below is not: a live region
          announces *changes* to its text, and one that mounts with the first task
          has nothing to compare against. */}
      <p className="sr-only" role="status" aria-live="polite">
        {spoken}
      </p>

      {tasks.length > 0 && (
        <div className="pointer-events-none fixed right-3 bottom-9 z-40 flex w-[23rem] max-w-[calc(100vw-1.5rem)] flex-col gap-1">
          {live > 1 && (
            <div className="pointer-events-auto">
              <Button
                tone="danger"
                size="sm"
                block
                icon="stop"
                onClick={stopAllTasks}
                title={`Stop all ${live} running tasks`}
              >
                Stop all {live} running tasks
              </Button>
            </div>
          )}

          {/* The same `Panel` every screen uses, clipped and capped: the vocabulary
              is shared so the HUD reads as part of the product rather than as a
              toast stacked on top of it. */}
          <Panel
            title="TASKS"
            meta={live ? `${live} running` : `${tasks.length} recent`}
            scroll
            className="arrive pointer-events-auto max-h-[60vh]"
          >
            <div className="flex flex-col gap-1.5 p-2">
              {tasks.map((task) => (
                <TaskRow
                  key={task.id}
                  task={task}
                  now={now}
                  busy={taskBusy.includes(task.id)}
                  error={taskErrors[task.id] ?? ""}
                  open={!(collapsed[task.id] ?? false)}
                  running={runningTools.filter((tool) => tool.taskId === task.id)}
                  onToggle={() =>
                    setCollapsed((current) => ({ ...current, [task.id]: !(current[task.id] ?? false) }))
                  }
                  onStop={() => stopTask(task.id)}
                  onRun={() => runTask(task.id)}
                  onRetry={() => askRetry(task)}
                  onDismiss={() => dismissTask(task.id)}
                  onOpen={() => go("tasks", task.id)}
                />
              ))}
            </div>
          </Panel>
        </div>
      )}

      <RetryDialog
        asking={asking}
        busy={asking ? taskBusy.includes(asking.task.id) : false}
        onCancel={() => setAsking(null)}
        onConfirm={() => {
          if (asking) retryTask(asking.task.id);
          setAsking(null);
        }}
      />
    </>
  );
}

type Asking = { task: HudTask; preview: RetryPreview | null; failed: string };

/**
 * The retry confirmation.
 *
 * A retry is not free: it re-runs real tools against this machine, and some of them
 * are not idempotent — running `file.write` twice is fine, re-running something that
 * already appended to a file is not. The core refuses to repeat a non-idempotent tool
 * on its own and defers to a human, and that deferral only means something if the
 * human was shown *which* steps and *which* tools first. So this asks the core what a
 * retry would do and puts the answer on the button's confirmation, which is why it is
 * a round trip later than the click rather than instant.
 */
function RetryDialog({
  asking,
  busy,
  onCancel,
  onConfirm,
}: {
  asking: Asking | null;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const preview = asking?.preview ?? null;
  return (
    <Dialog
      open={asking !== null}
      onClose={onCancel}
      title="Retry this task?"
      width="sm"
      dismissable={!busy}
      footer={
        <>
          <Button tone="ghost" size="sm" onClick={onCancel} disabled={busy} reason="Running.">
            Cancel
          </Button>
          {/* Rendered only when the core says a retry applies. A disabled Retry with
              the reason already on screen would be one more control to read past. */}
          {preview?.can_retry && (
            <Button tone="danger" size="sm" icon="refresh" onClick={onConfirm} busy={busy}>
              Retry
            </Button>
          )}
        </>
      }
    >
      {!preview && !asking?.failed && (
        <p className="text-xs text-ink-dim">Working out what a retry would re-run…</p>
      )}
      {asking?.failed && <p className="text-xs text-danger">{asking.failed}</p>}
      {preview && (
        <div className="flex flex-col gap-2">
          <p className="text-xs leading-relaxed text-ink">
            {preview.can_retry ? (
              <>
                <span className="font-mono text-ink">{preview.steps.length}</span> step(s) would
                run again; <span className="font-mono text-ink">{preview.keeping}</span> completed
                step(s) are kept.
              </>
            ) : (
              preview.reason
            )}
          </p>

          {preview.not_idempotent.length > 0 && (
            <p className="border border-danger/40 bg-danger/12 p-2 text-[11px] leading-relaxed text-danger">
              {/* Named by the core, and named here rather than folded into a count:
                  it is the one fact that should change the answer. */}
              Running these twice is not the same as running them once:{" "}
              <span className="font-mono">{unique(preview.not_idempotent).join(", ")}</span>
            </p>
          )}

          {preview.steps.length > 0 && (
            <ul className="flex flex-col gap-0.5">
              {preview.steps.slice(0, 8).map((step) => (
                <li key={step.id} className="flex items-center gap-2 text-[11px] text-ink-dim">
                  <span className="font-mono text-ink-faint">{step.idx + 1}</span>
                  <span className="min-w-0 flex-1 truncate">{step.intent}</span>
                  <span className="font-mono text-[10px] text-ink-faint">{step.tool ?? "—"}</span>
                </li>
              ))}
              {preview.steps.length > 8 && (
                <li className="text-[11px] text-ink-faint">
                  and {preview.steps.length - 8} more
                </li>
              )}
            </ul>
          )}
        </div>
      )}
    </Dialog>
  );
}

function TaskRow({
  task,
  now,
  busy,
  error,
  open,
  running,
  onToggle,
  onStop,
  onRun,
  onRetry,
  onDismiss,
  onOpen,
}: {
  task: HudTask;
  now: number;
  busy: boolean;
  error: string;
  open: boolean;
  running: { tool: string; title: string }[];
  onToggle: () => void;
  onStop: () => void;
  onRun: () => void;
  onRetry: () => void;
  onDismiss: () => void;
  onOpen: () => void;
}) {
  const { state, label } = taskState(task.status);
  // Named `view`, not `window`: this app has no server-side rendering but it does
  // have a global `window`, and a local called `window` is a trap for the next edit.
  const view = stepWindow(task.steps);
  const ended = isTerminalTask(task.status);

  return (
    <article className="border border-line bg-base/60 p-1.5">
      <div className="flex items-start gap-1.5">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          aria-label={open ? `Collapse ${task.objective}` : `Expand ${task.objective}`}
          className="mt-0.5 shrink-0 text-ink-faint hover:text-ink"
        >
          <Icon name={open ? "chevron-down" : "chevron-right"} size={12} />
        </button>

        <div className="min-w-0 flex-1">
          <p className="truncate text-[11px] leading-snug text-ink" title={task.objective}>
            {task.objective}
          </p>
          <div className="mt-0.5 flex items-center gap-2">
            <StatusPill state={state} size="sm" pulse={task.status === "running"}>
              {label}
            </StatusPill>
            {/* Outside the live region on purpose: this is the tick that changes
                every second, and a screen reader would read it every second. */}
            <span className="font-mono text-[10px] text-ink-faint">
              {elapsed(task, now)}
            </span>
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-1">
          {isLive(task.status) && (
            <Button
              tone="danger"
              size="sm"
              icon="stop"
              busy={busy}
              onClick={onStop}
              title="Stop this task"
            >
              Stop
            </Button>
          )}
          {task.status === "interrupted" && (
            <Button
              tone="default"
              size="sm"
              icon="play"
              busy={busy}
              onClick={onRun}
              title="The core stopped mid-task. Carry on from the next pending step."
            >
              Resume
            </Button>
          )}
          {(task.status === "failed" || task.status === "cancelled") && (
            <Button
              tone="default"
              size="sm"
              icon="refresh"
              busy={busy}
              onClick={onRetry}
              title="Re-run the steps that did not succeed"
            >
              Retry
            </Button>
          )}
        </div>
      </div>

      {open && (
        <div className="mt-1.5 flex flex-col gap-1.5">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[10px] text-ink-dim">{progressLine(task)}</span>
            <button
              type="button"
              onClick={onOpen}
              className="ml-auto shrink-0 text-[10px] text-ink-faint underline decoration-dotted hover:text-accent"
            >
              open task
            </button>
          </div>

          {running.length > 0 && (
            <ul className="flex flex-col gap-0.5">
              {running.slice(0, NOW_LIMIT).map((tool, at) => (
                <li
                  key={`${tool.tool}-${at}`}
                  className="flex items-center gap-1.5 text-[10px] text-accent"
                >
                  <Icon name="spinner" size={10} className="shrink-0 animate-spin" />
                  <span className="truncate font-mono">{tool.title}</span>
                </li>
              ))}
            </ul>
          )}

          {task.error && (
            <p className="border border-danger/40 bg-danger/12 p-1.5 text-[10px] leading-relaxed text-danger">
              {task.error}
            </p>
          )}

          {error && (
            <p className="border border-danger/40 bg-danger/12 p-1.5 text-[10px] leading-relaxed text-danger">
              {error}
            </p>
          )}

          {view.shown.length > 0 && (
            <ol className="flex flex-col gap-0.5">
              {view.shown.map((step) => {
                const stepStyle = stepState(step.status);
                return (
                  <li key={step.id} className="flex items-start gap-1.5 text-[10px]">
                    <span className="w-5 shrink-0 text-right font-mono text-ink-faint">
                      {step.idx + 1}
                    </span>
                    <StateDot state={stepStyle.state} pulse={step.status === "running"} />
                    <span
                      className={[
                        "min-w-0 flex-1 leading-snug",
                        step.status === "pending" ? "text-ink-faint" : "text-ink-dim",
                        step.status === "running" ? "text-ink" : "",
                      ].join(" ")}
                    >
                      {step.intent}
                    </span>
                    <span className="shrink-0 font-mono text-[9px] text-ink-faint">
                      {step.tool ?? "—"}
                    </span>
                  </li>
                );
              })}
              {view.sliced && (
                <li className="pl-6 text-[9px] text-ink-faint">
                  steps {view.first}–{view.last} of {view.total}
                </li>
              )}
            </ol>
          )}
        </div>
      )}

      {open && ended && (
        <div className="mt-1.5 flex justify-end">
          <Button tone="ghost" size="sm" icon="close" onClick={onDismiss}>
            Dismiss
          </Button>
        </div>
      )}
    </article>
  );
}

/**
 * A second-resolution clock, but only while something is actually ticking.
 *
 * `everyMs` of 0 stops the interval rather than scheduling a no-op one: with every
 * task finished there is nothing on screen that changes with the clock, and a timer
 * that wakes the window once a second to render the same pixels is exactly the cost
 * §94's "motion means something is happening" is about.
 */
function useNow(everyMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (everyMs <= 0) return;
    const timer = window.setInterval(() => {
      // A background window does not need to re-render for a clock nobody sees.
      if (document.visibilityState === "visible") setNow(Date.now());
    }, everyMs);
    return () => window.clearInterval(timer);
  }, [everyMs]);
  return now;
}

function unique(values: string[]): string[] {
  return [...new Set(values)];
}
