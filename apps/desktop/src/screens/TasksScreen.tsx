/**
 * TASKS — multi-step work, and what actually happened (§24, §25, §80, §81).
 *
 * The whole screen is built around one distinction: **the plan is not the progress.**
 * `task.steps` is what was intended; `activity` is what the core recorded doing. §81
 * says agent progress must be drawn from the second one, because a step list on its own
 * will happily show a tidy row of ticks for work that never ran. So both are on screen,
 * separately labelled, and the progress bar counts real `task_steps` rows by status —
 * never elapsed time, never a step index.
 *
 * The screen writes as well as reads. `POST /api/tasks` plans one (a real reasoning
 * call, which is why the composer can sit busy for a minute), `/run` starts or resumes
 * it, `/cancel` stops it and withdraws any permission prompt it was holding, and
 * `DELETE` removes it. Each control is enabled exactly when the core would accept it:
 * Run needs a task the core calls startable (`pending` or `interrupted`), Delete is
 * refused with 409 while a run is in flight, and where a control is disabled it carries
 * the reason rather than failing on click (§56).
 *
 * Two things this screen deliberately does not have:
 *
 * - **No invented "current step".** A task with nothing running shows no running step.
 *   Highlighting the first pending row as "in progress" would be a guess.
 * - **No optimistic status.** Pressing Run does not draw a running task; the response
 *   and the `task.*` events do. The stored status and `running` are shown as the two
 *   separate facts they are — a task left `running` by a core that was killed is not
 *   running, and only `running: false` can say so.
 */

import { useEffect, useRef, useState } from "react";

import { Icon } from "../components/Icon";
import {
  ActionResult,
  Button,
  ConfirmDialog,
  DataRow,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  ListRow,
  LoadingState,
  Panel,
  Section,
  Select,
  StatusPill,
  TextArea,
  clockTime,
  duration,
  relativeTime,
  stateOf,
} from "../components/primitives";
import { api, type ActivityRow, type Task, type TaskProgress, type TaskStep } from "../lib/api";
// The socket itself, not the `link` status object from `useNexus` — this screen needs
// to hear individual `task.*` events, which is what `onTypes` fans out.
import { link as socket } from "../lib/events";
import { useAction, useAsync, usePoll } from "../lib/useAsync";
import { useNexus } from "../state/nexus";

/**
 * The status filters, each mapping to a real `statuses=` value.
 *
 * The groups are the seven statuses in `Task["status"]` split by what the user is
 * actually asking: is it still going, did it end well, or did it end badly.
 */
const FILTERS = [
  { value: "live", label: "Live", statuses: "pending,running,waiting_permission" },
  { value: "done", label: "Completed", statuses: "completed" },
  { value: "problems", label: "Failed or stopped", statuses: "failed,cancelled,interrupted" },
  { value: "all", label: "Everything", statuses: "" },
] as const;

type FilterValue = (typeof FILTERS)[number]["value"];

/** A live task changes underneath us, so the list refreshes — but only while looking. */
const LIST_POLL_MS = 6_000;

/**
 * Re-read on `task.*` events, not only on the timer.
 *
 * A running step publishes several events in a row (`task.step.started`,
 * `tool.started`, `tool.finished`, `task.step.finished`), so the reload is coalesced —
 * one re-read per burst instead of four. The timer stays as well: the socket can be
 * down, and a task that finished while it was down must not sit on screen as running.
 */
function useTaskEvents(reload: () => void, mine: (payload: Record<string, unknown>) => boolean) {
  const reloadRef = useRef(reload);
  reloadRef.current = reload;
  const mineRef = useRef(mine);
  mineRef.current = mine;

  useEffect(() => {
    let timer: number | null = null;
    const off = socket.onTypes(["task."], (event) => {
      if (!mineRef.current(event.payload)) return;
      if (timer !== null) window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        timer = null;
        reloadRef.current();
      }, 300);
    });
    return () => {
      off();
      if (timer !== null) window.clearTimeout(timer);
    };
  }, []);
}

export function TasksScreen({ focus }: { focus: string | null }) {
  const { go, link } = useNexus();
  const [filter, setFilter] = useState<FilterValue>("live");

  const statuses = FILTERS.find((entry) => entry.value === filter)?.statuses ?? "";
  const list = useAsync(
    () => api.tasks.list(statuses ? { statuses, limit: 100 } : { limit: 100 }),
    [statuses],
  );
  usePoll(list.reload, LIST_POLL_MS, link.connected);
  useTaskEvents(list.reload, () => true);

  const tasks = list.data?.tasks ?? [];

  return (
    <div className="grid h-full min-h-0 grid-cols-1 gap-2 p-2 lg:grid-cols-[minmax(240px,340px)_1fr]">
      <Panel
        title="TASKS"
        meta={list.data ? `${tasks.length}` : undefined}
        className="min-h-0"
        bodyClassName="flex min-h-0 flex-1 flex-col"
      >
        <div className="border-b border-line px-3 py-2">
          <Select<FilterValue>
            value={filter}
            onChange={setFilter}
            options={FILTERS.map((entry) => ({ value: entry.value, label: entry.label }))}
          />
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {list.state === "loading" ? (
            <LoadingState what="tasks" />
          ) : !list.data ? (
            <div className="p-2">
              <ErrorState
                what={list.error ?? "The task list did not load."}
                did="Nothing is listed. An empty list would claim no work has been run."
                next={list.offline ? "Start the core, then retry." : list.detail || "Retry."}
                offline={list.offline}
                onRetry={list.reload}
                compact
              />
            </div>
          ) : tasks.length === 0 ? (
            <EmptyState
              icon="tasks"
              title={filter === "live" ? "Nothing running" : "No tasks here"}
              detail={
                filter === "live"
                  ? "A task is created when NEXUS plans work in more than one step. Single tool calls happen inside a conversation instead — CONVERSATIONS shows those."
                  : "Nothing matches this filter. Try Everything."
              }
            />
          ) : (
            <ul>
              {tasks.map((task) => (
                <li key={task.id}>
                  <TaskListRow
                    task={task}
                    selected={task.id === focus}
                    onOpen={() => go("tasks", task.id)}
                  />
                </li>
              ))}
            </ul>
          )}
        </div>

        <Composer onCreated={(id) => go("tasks", id)} />
      </Panel>

      {focus ? (
        <TaskDetail id={focus} />
      ) : (
        <Panel title="TASK" className="min-h-0">
          <EmptyState
            icon="tasks"
            title="No task selected"
            detail="Pick one on the left. Each task shows its plan, its verified step results, and the separate record of what the core actually did."
          />
        </Panel>
      )}
    </div>
  );
}

/**
 * Plan a task from an objective.
 *
 * Planning is a real model call, not a form submission: the core asks the reasoning
 * model for a plan over the tools that actually exist on this machine, then validates
 * every step against the same registry execution uses and rejects the plan if a tool
 * name or an argument is wrong. So this control can sit busy for a minute, and it can
 * come back with "this cannot be done with the tools available on this computer" —
 * which is an answer, and it is shown as the failure it is rather than swallowed.
 *
 * Two buttons instead of one plus a checkbox: Plan and Plan-and-run are different
 * decisions — the second one starts touching files — and a checkbox left ticked from
 * last time is how the wrong one gets pressed.
 */
function Composer({ onCreated }: { onCreated: (id: string) => void }) {
  const [objective, setObjective] = useState("");
  const [pressed, setPressed] = useState<"plan" | "run" | null>(null);
  // A ref, not the state: `useAction.run()` is called in the same handler that sets
  // the state, and the action closure would still read the previous render's value.
  const runAfterPlanning = useRef(false);

  const submit = useAction(async () => {
    const detail = await api.tasks.create(objective.trim(), { run: runAfterPlanning.current });
    // Selected before anything is thrown: a refused plan is still a real task with the
    // reason recorded on it, and that is what the user should be looking at.
    onCreated(detail.task.id);
    if (detail.task.status === "failed") {
      throw new Error(detail.task.error || "The planner returned no steps and no reason.");
    }
    setObjective("");
    const steps = detail.task.steps?.length ?? 0;
    const planned = `${steps} step${steps === 1 ? "" : "s"} planned.`;
    return detail.running ? `${planned} It is running now.` : `${planned} Press Run to start it.`;
  });

  const empty = objective.trim().length === 0;
  const press = (run: boolean) => {
    runAfterPlanning.current = run;
    setPressed(run ? "run" : "plan");
    submit.run();
  };

  return (
    <div className="border-t border-line px-3 py-2">
      <Field
        label="Plan a task"
        hint="One objective in plain words. NEXUS plans it with the tools on this machine."
        htmlFor="task-objective"
      >
        <TextArea
          id="task-objective"
          value={objective}
          onChange={setObjective}
          rows={2}
          disabled={submit.busy}
          placeholder="Tidy up ~/Downloads and tell me what is taking the most space"
        />
      </Field>
      <div className="mt-2 flex gap-2">
        <Button
          size="sm"
          icon="tasks"
          onClick={() => press(false)}
          busy={submit.busy && pressed === "plan"}
          disabled={empty || submit.busy}
          reason={empty ? "Type an objective first." : "Planning."}
        >
          Plan
        </Button>
        <Button
          size="sm"
          tone="accent"
          icon="play"
          onClick={() => press(true)}
          busy={submit.busy && pressed === "run"}
          disabled={empty || submit.busy}
          reason={empty ? "Type an objective first." : "Planning."}
        >
          Plan and run
        </Button>
      </div>
      {submit.busy && (
        <p className="mt-1.5 text-[10px] leading-relaxed text-ink-faint">
          Asking the reasoning model for a plan. This can take a while — cold starts have
          been measured past 110 s.
        </p>
      )}
      <ActionResult done={submit.done} failed={submit.failed} onClear={submit.clear} />
    </div>
  );
}

/**
 * One row in the list.
 *
 * `step_count`/`done_count` are optional on `Task` — the list query may not compute
 * them. When they are absent the row says nothing about progress rather than printing
 * "0 of 0", which reads as a task that planned nothing.
 */
function TaskListRow({
  task,
  selected,
  onOpen,
}: {
  task: Task;
  selected: boolean;
  onOpen: () => void;
}) {
  const counted = task.step_count !== undefined && task.done_count !== undefined;
  return (
    <ListRow selected={selected} onClick={onOpen}>
      <div className="flex items-start gap-2">
        <StatusPill state={stateOf(task.status)} size="sm" pulse={task.status === "running"}>
          {task.status.replace(/_/g, " ")}
        </StatusPill>
      </div>
      <p className="mt-1 break-words text-xs text-ink">{task.objective}</p>
      <p className="mt-0.5 font-mono text-[10px] text-ink-faint">
        {counted ? `${task.done_count} of ${task.step_count} steps · ` : ""}
        {relativeTime(task.updated_at)}
      </p>
    </ListRow>
  );
}

/** A running task is re-read on a timer; a finished one is not. */
const DETAIL_POLL_MS = 4_000;

function TaskDetail({ id }: { id: string }) {
  const { link } = useNexus();
  const detail = useAsync(() => api.tasks.get(id), [id]);
  // `id` on a task snapshot, `task_id` on a step event — both mean this task.
  useTaskEvents(detail.reload, (payload) => payload.id === id || payload.task_id === id);

  const task = detail.data?.task;
  const live =
    task?.status === "running" ||
    task?.status === "pending" ||
    task?.status === "waiting_permission";
  usePoll(detail.reload, DETAIL_POLL_MS, link.connected && Boolean(live));

  if (detail.state === "loading") {
    return (
      <Panel title="TASK" className="min-h-0">
        <LoadingState what="this task" />
      </Panel>
    );
  }

  if (!detail.data || !task) {
    return (
      <Panel title="TASK" className="min-h-0">
        <div className="p-2">
          <ErrorState
            what={detail.error ?? "This task did not load."}
            did="Nothing is shown. A blank plan would look like a task with no steps."
            next={detail.offline ? "Start the core, then retry." : detail.detail || "Retry."}
            offline={detail.offline}
            onRetry={detail.reload}
            compact
          />
        </div>
      </Panel>
    );
  }

  const { progress, activity, running } = detail.data;
  const steps = task.steps ?? [];

  return (
    <Panel
      title="TASK"
      meta={`${task.status.replace(/_/g, " ")} · ${relativeTime(task.updated_at)}`}
      className="min-h-0"
      bodyClassName="flex min-h-0 flex-1 flex-col"
      active={running || Boolean(live)}
      actions={
        <Button
          size="sm"
          tone="ghost"
          icon="refresh"
          onClick={detail.reload}
          busy={detail.refreshing}
        >
          Re-read
        </Button>
      }
    >
      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-2">
        <div className="chamfer-sm border border-line-bright bg-raised px-2.5 py-2">
          <span className="label">Objective</span>
          <p className="mt-1 whitespace-pre-wrap break-words text-xs leading-relaxed text-ink">
            {task.objective}
          </p>
        </div>

        <Outcome task={task} />
        <Progress progress={progress} status={task.status} />
        <Timing task={task} />
        <Plan steps={steps} />
        <Happened activity={activity} />

        <Controls task={task} running={running} onChanged={detail.reload} />
      </div>
    </Panel>
  );
}

/**
 * Run, Cancel, Delete — each enabled exactly when the core would accept it.
 *
 * `running` is the core's own answer to "is a run in flight in this process", and it is
 * not the same fact as `status === "running"`: a task whose core was killed mid-run
 * keeps that status forever. Where the two disagree this says so, because the fix is
 * different — a live run needs Cancel or patience, a stranded one needs closing out.
 *
 * The core is the authority on all three, not this component: `start` refuses anything
 * that is not `pending` or `interrupted`, `cancel` is a no-op on a finished task, and
 * `DELETE` answers 409 while a run is in flight. The disabled states below mirror those
 * rules so the button says why instead of failing on click (§56).
 */
function Controls({
  task,
  running,
  onChanged,
}: {
  task: Task;
  running: boolean;
  onChanged: () => void;
}) {
  const { go } = useNexus();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [askRetry, setAskRetry] = useState(false);

  const finished =
    task.status === "completed" || task.status === "failed" || task.status === "cancelled";
  const startable = task.status === "pending" || task.status === "interrupted";
  const stranded = !running && (task.status === "running" || task.status === "waiting_permission");
  // Mirrors `agent.RETRYABLE`. `completed` is absent because there is nothing to
  // re-run, and `interrupted` because that is Resume's job — a resume picks up the
  // first pending step, a retry rewrites stored statuses first, and calling both
  // "retry" would make them indistinguishable in the log.
  const retryable = (task.status === "failed" || task.status === "cancelled") && !running;

  const start = useAction(async () => {
    const result = await api.tasks.run(task.id);
    onChanged();
    return result.running
      ? "Started. Steps change as they run — the plan below updates itself."
      : "The core accepted it, but reports nothing running. Re-read to see why.";
  });

  const stop = useAction(async () => {
    await api.tasks.cancel(task.id);
    onChanged();
    return "Cancelled. Any permission prompt it was holding has been withdrawn.";
  });

  const again = useAction(async () => {
    const result = await api.tasks.retry(task.id);
    setAskRetry(false);
    onChanged();
    return result.running
      ? "Retrying. The steps that did not succeed are queued again; the ones that worked are kept."
      : "The core accepted the retry but reports nothing running. Re-read to see why.";
  });

  const remove = useAction(async () => {
    await api.tasks.remove(task.id);
    setConfirmDelete(false);
    // The panel goes with it, so the confirmation is the task disappearing from the list.
    go("tasks", null);
    return "Deleted.";
  });

  const runReason = running
    ? "It is already running."
    : stranded
      ? "Its stored status still says running, so the core refuses to start it. Cancel it first."
      : finished
        ? `This task is ${task.status}. Plan a new task to do the work again.`
        : "";

  return (
    <div className="border-t border-line px-1 pt-2">
      <div className="flex flex-wrap gap-2">
        <Button
          size="sm"
          tone="accent"
          icon="play"
          onClick={start.run}
          busy={start.busy}
          disabled={!startable || running || start.busy}
          reason={runReason || "Starting."}
        >
          {task.status === "interrupted" ? "Resume" : "Run"}
        </Button>
        <Button
          size="sm"
          icon="stop"
          onClick={stop.run}
          busy={stop.busy}
          disabled={finished || stop.busy}
          reason={finished ? `Already ${task.status}.` : "Cancelling."}
        >
          Cancel
        </Button>
        <Button
          size="sm"
          icon="refresh"
          onClick={() => setAskRetry(true)}
          disabled={!retryable || again.busy}
          reason={
            running
              ? "A run is in flight. Cancel it before retrying."
              : task.status === "completed"
                ? "It completed. There is no step left that a retry would re-run."
                : task.status === "interrupted"
                  ? "It was interrupted, not failed — Resume picks it up at its first unfinished step."
                  : task.status === "pending"
                    ? "It has not run yet, so there is nothing to retry. Run it."
                    : "Retrying."
          }
        >
          Retry
        </Button>
        <Button
          size="sm"
          tone="danger"
          icon="trash"
          onClick={() => setConfirmDelete(true)}
          disabled={running}
          reason="It is running. Cancel it first — the core refuses to delete a task mid-run."
        >
          Delete
        </Button>
      </div>

      <ActionResult done={start.done} failed={start.failed} onClear={start.clear} />
      <ActionResult done={stop.done} failed={stop.failed} onClear={stop.clear} />
      <ActionResult done={again.done} failed={again.failed} onClear={again.clear} />

      {stranded && (
        <p className="mt-2 text-[11px] leading-relaxed text-warn">
          Marked {task.status.replace(/_/g, " ")}, but no run is in flight in the core. A
          restart is reconciled at startup, so this is a run that stopped inside a core
          that is still up — the reason is in its log. Cancel it to close it out.
        </p>
      )}
      {running && task.status === "waiting_permission" && (
        <p className="mt-2 text-[11px] leading-relaxed text-ink-dim">
          A step is waiting for permission. The prompt is on screen wherever you are —
          answering it lets the run continue; cancelling here withdraws it.
        </p>
      )}

      {askRetry && (
        <RetryDialog
          task={task}
          onCancel={() => setAskRetry(false)}
          onConfirm={again.run}
          busy={again.busy}
          failed={again.failed}
        />
      )}

      <ConfirmDialog
        open={confirmDelete}
        title="Delete this task?"
        consequence="The task, its plan and its step results are removed from the database. The activity log keeps the record of what was actually done — deleting the task does not undo any of it."
        confirmLabel="Delete"
        onConfirm={remove.run}
        onCancel={() => setConfirmDelete(false)}
        busy={remove.busy}
        failed={remove.failed}
      />
    </div>
  );
}

/**
 * What Retry would actually do, asked before it does it.
 *
 * "Retry?" is not a question anyone can answer, so this dialog is the question: these
 * steps run again, this many are kept, and these tools are ones where running twice is
 * not the same as running once. The core's own step-level policy refuses to repeat a
 * non-idempotent tool *automatically* — it defers to a human — and this is that human's
 * only sight of them, which is why `not_idempotent` is drawn as a warning rather than a
 * footnote.
 *
 * The dialog is what fetches the preview, and it is mounted only while open, so the
 * list is the task as it is at the moment of asking and not an answer cached from when
 * the screen loaded. Confirm stays disabled until that answer arrives: agreeing to an
 * unknown set of real tool calls is not agreement.
 *
 * `can_retry: false` is shown rather than hidden. The button that opened this is
 * enabled on status alone, which is the core's first two refusals; the third — a failed
 * task whose every step nonetheless completed — is only knowable here, and saying it is
 * more use than a greyed-out button with no explanation.
 */
function RetryDialog({
  task,
  onCancel,
  onConfirm,
  busy,
  failed,
}: {
  task: Task;
  onCancel: () => void;
  onConfirm: () => void;
  busy: boolean;
  failed: string;
}) {
  const preview = useAsync(() => api.tasks.retryPreview(task.id), [task.id]);
  const data = preview.data;
  const ready = preview.state === "ready" && Boolean(data);

  return (
    <Dialog
      open
      onClose={onCancel}
      title="Retry this task?"
      width="md"
      dismissable={!busy}
      footer={
        <>
          <Button tone="ghost" size="sm" onClick={onCancel} disabled={busy} reason="Retrying.">
            Cancel
          </Button>
          <Button
            tone="accent"
            size="sm"
            icon="refresh"
            onClick={onConfirm}
            busy={busy}
            disabled={!ready || !data?.can_retry}
            reason={
              !ready
                ? "Waiting for the core to say what would run."
                : data?.reason || "Retrying."
            }
          >
            {ready && data?.steps.length
              ? `Re-run ${data.steps.length} step${data.steps.length === 1 ? "" : "s"}`
              : "Retry"}
          </Button>
        </>
      }
    >
      {preview.state === "loading" && <LoadingState what="what a retry would re-run" />}

      {preview.state === "error" && (
        <ErrorState
          what={preview.error ?? "The core did not say what a retry would re-run."}
          did="Nothing has been retried. Without the list, there is nothing here to agree to."
          next={preview.offline ? "Start the core, then try again." : preview.detail || "Try again."}
          offline={preview.offline}
          onRetry={preview.reload}
          compact
        />
      )}

      {ready && data && (
        <div className="space-y-2">
          {!data.can_retry && (
            <div className="chamfer-sm border border-warn/40 bg-warn/8 px-2.5 py-2">
              <span className="label text-warn">Nothing to retry</span>
              <p className="mt-1 text-[11px] leading-relaxed text-warn">{data.reason}</p>
            </div>
          )}

          {data.not_idempotent.length > 0 && (
            <div className="chamfer-sm border border-danger/40 bg-danger/8 px-2.5 py-2">
              <span className="label text-danger">Running these twice is not the same as once</span>
              <p className="mt-1 text-[11px] leading-relaxed text-danger">
                {[...new Set(data.not_idempotent)].join(", ")} — the core will not repeat these on
                its own for exactly this reason. A step that failed part-way may already have
                changed something, and retrying does it again from the start.
              </p>
            </div>
          )}

          {data.steps.length > 0 && (
            <Section title="Would run again" meta={`${data.steps.length} of ${data.steps.length + data.keeping}`}>
              <div className="divide-y divide-line">
                {data.steps.map((step) => (
                  <div key={step.id} className="flex items-start gap-2 px-3 py-1.5">
                    <span className="mt-px font-mono text-[10px] text-ink-faint">
                      {String(step.idx + 1).padStart(2, "0")}
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="break-words text-[11px] leading-relaxed text-ink">
                        {step.intent || "(no intent recorded)"}
                      </p>
                      <p className="mt-0.5 font-mono text-[10px] text-ink-faint">
                        {step.status}
                        {step.tool ? ` · ${step.tool}` : " · no tool"}
                        {step.idempotent === false ? " · repeat is not safe" : ""}
                        {step.idempotent === null && step.tool === null ? " · nothing to repeat" : ""}
                      </p>
                    </div>
                  </div>
                ))}
              </div>
            </Section>
          )}

          <p className="text-[11px] leading-relaxed text-ink-soft">
            {data.keeping > 0
              ? `${data.keeping} step${data.keeping === 1 ? "" : "s"} already succeeded and ${
                  data.keeping === 1 ? "is" : "are"
                } kept — a retry starts from the last good step, it does not do the whole task again.`
              : "No step on this task has succeeded, so a retry starts from the beginning."}
          </p>
        </div>
      )}

      {failed && <ActionResult done="" failed={failed} />}
    </Dialog>
  );
}

/**
 * The result, or the failure, in the task's own words.
 *
 * A completed task with an empty `result` says exactly that. Substituting "Done" for a
 * result the core never wrote would be putting words in its mouth.
 */
function Outcome({ task }: { task: Task }) {
  if (task.error) {
    return (
      <div className="chamfer-sm border border-danger/40 bg-danger/8 px-2.5 py-2">
        <span className="label text-danger">It failed</span>
        <p className="mt-1 whitespace-pre-wrap break-words text-[11px] leading-relaxed text-danger">
          {task.error}
        </p>
      </div>
    );
  }
  if (task.status === "completed") {
    return (
      <div className="chamfer-sm border border-ok/40 bg-ok/8 px-2.5 py-2">
        <span className="label text-ok">Result</span>
        <p className="mt-1 whitespace-pre-wrap break-words text-[11px] leading-relaxed text-ink">
          {task.result || "It finished without writing a result."}
        </p>
      </div>
    );
  }
  if (task.result) {
    return (
      <div className="chamfer-sm border border-line bg-base px-2.5 py-2">
        <span className="label">Latest result so far</span>
        <p className="mt-1 whitespace-pre-wrap break-words text-[11px] leading-relaxed text-ink-soft">
          {task.result}
        </p>
      </div>
    );
  }
  return null;
}

/**
 * Progress, counted from `task_steps` (§80).
 *
 * The bar is `completed / total` and nothing else. `waiting_permission` is called out
 * because it is the one status where the task is not stuck on the machine — it is stuck
 * on the user, and that needs different words from "running".
 */
function Progress({ progress, status }: { progress: TaskProgress; status: Task["status"] }) {
  const { total, completed, failed, running, pending, skipped, waiting_permission } = progress;

  if (total === 0) {
    return (
      <Section title="Progress">
        <p className="px-3 py-2 text-[11px] leading-relaxed text-ink-soft">
          No steps were written for this task
          {status === "pending" ? " yet — it has not been planned." : "."} There is nothing
          to count, so nothing is shown as progress.
        </p>
      </Section>
    );
  }

  const percent = Math.round((completed / total) * 100);

  return (
    <Section title="Progress" meta={`${completed} of ${total} steps done`}>
      <div className="px-3 py-2">
        <div
          role="meter"
          aria-valuenow={completed}
          aria-valuemin={0}
          aria-valuemax={total}
          aria-label={`${completed} of ${total} steps completed`}
          className="h-1.5 w-full overflow-hidden bg-line"
        >
          <div
            className={`h-full ${failed > 0 ? "bg-danger" : "bg-accent"}`}
            style={{ width: `${percent}%` }}
          />
        </div>
        <p className="mt-1.5 font-mono text-[10px] text-ink-faint">
          {percent}% · counted from step rows, not from elapsed time
        </p>
      </div>
      <div className="flex flex-col">
        {running > 0 && <DataRow label="Running" value={String(running)} state="active" />}
        {waiting_permission > 0 && (
          <DataRow
            label="Waiting on you"
            value={`${waiting_permission} step${waiting_permission === 1 ? "" : "s"}`}
            state="pending"
            title="These steps are not stuck on the machine. PERMISSIONS has the request."
          />
        )}
        {pending > 0 && <DataRow label="Not started" value={String(pending)} state="idle" />}
        {failed > 0 && <DataRow label="Failed" value={String(failed)} state="failed" />}
        {skipped > 0 && (
          <DataRow
            label="Skipped"
            value={String(skipped)}
            state="cancelled"
            title="A step whose dependency did not complete."
          />
        )}
      </div>
    </Section>
  );
}

/** When it ran. Every timestamp is nullable, and an absent one is left absent. */
function Timing({ task }: { task: Task }) {
  return (
    <Section title="Timing">
      <div className="flex flex-col">
        <DataRow label="Created" value={clockTime(task.created_at)} />
        <DataRow
          label="Started"
          value={task.started_at ? clockTime(task.started_at) : "it never started"}
          state={task.started_at ? "idle" : "pending"}
        />
        <DataRow
          label="Finished"
          value={task.finished_at ? clockTime(task.finished_at) : "not finished"}
          state={task.finished_at ? "idle" : "active"}
        />
        {task.started_at && task.finished_at && (
          <DataRow
            label="Took"
            value={duration(
              new Date(task.finished_at).getTime() - new Date(task.started_at).getTime(),
            )}
          />
        )}
        {task.conversation_id && (
          <DataRow
            label="From conversation"
            value={task.conversation_id}
            title="The thread this task was planned in."
          />
        )}
      </div>
    </Section>
  );
}

/**
 * The plan: what was intended, step by step.
 *
 * The verification tri-state repeats here because it is a per-step fact. `verified` is
 * `0 | 1 | null` over the wire, and `null` means the step's tool has no verifier — it is
 * rendered as "no verifier" rather than as a failed check.
 */
function Plan({ steps }: { steps: TaskStep[] }) {
  if (steps.length === 0) return null;
  return (
    <Section title="The plan" meta={`${steps.length} step${steps.length === 1 ? "" : "s"}`}>
      <p className="border-b border-line/60 px-3 py-1.5 text-[11px] leading-relaxed text-ink-dim">
        What NEXUS intended to do. A tick here means the step reported success — the
        record of what actually ran is below.
      </p>
      <ol>
        {steps.map((step) => (
          <li key={step.id} className="border-b border-line/60 px-3 py-2">
            <div className="flex items-start gap-2">
              <span className="mt-0.5 shrink-0 font-mono text-[10px] text-ink-faint">
                {String(step.idx + 1).padStart(2, "0")}
              </span>
              <div className="min-w-0 flex-1">
                <p className="break-words text-[11px] leading-relaxed text-ink">{step.intent}</p>
                <div className="mt-1 flex flex-wrap items-center gap-1.5">
                  <StatusPill
                    state={stateOf(step.status)}
                    size="sm"
                    pulse={step.status === "running"}
                  >
                    {step.status.replace(/_/g, " ")}
                  </StatusPill>
                  {step.tool && (
                    <span className="font-mono text-[10px] text-ink-faint">{step.tool}</span>
                  )}
                  {step.status === "completed" && (
                    <StatusPill
                      state={step.verified === null ? "idle" : step.verified ? "ok" : "pending"}
                      size="sm"
                    >
                      {step.verified === null
                        ? "NO VERIFIER"
                        : step.verified
                          ? "VERIFIED"
                          : "UNVERIFIED"}
                    </StatusPill>
                  )}
                  {step.depends_on.length > 0 && (
                    <span
                      className="font-mono text-[10px] text-ink-faint"
                      title="This step waited for other steps."
                    >
                      after {step.depends_on.length}
                    </span>
                  )}
                </div>
                {step.result && (
                  <p className="mt-1 whitespace-pre-wrap break-words text-[11px] leading-relaxed text-ink-soft">
                    {step.result}
                  </p>
                )}
                {step.error && (
                  <p className="mt-1 whitespace-pre-wrap break-words text-[11px] leading-relaxed text-danger">
                    {step.error}
                  </p>
                )}
              </div>
            </div>
          </li>
        ))}
      </ol>
    </Section>
  );
}

/**
 * What happened: the activity rows this task wrote (§81).
 *
 * This is the honest half. These rows were written as the work ran, by the core, and
 * they are the only evidence that the plan above corresponds to anything. An empty
 * activity log for a completed task is a real and alarming answer, so it is shown as
 * one instead of being hidden.
 */
function Happened({ activity }: { activity: ActivityRow[] }) {
  return (
    <Section title="What actually happened" meta={`${activity.length} recorded`}>
      {activity.length === 0 ? (
        <p className="px-3 py-2 text-[11px] leading-relaxed text-warn">
          The core recorded nothing for this task. The plan above says what was intended;
          there is no evidence here that any of it ran.
        </p>
      ) : (
        <ul>
          {activity.map((row) => (
            <li key={row.id} className="flex items-start gap-2 border-b border-line/60 px-3 py-1.5">
              <Icon
                name={
                  row.level === "error"
                    ? "alert"
                    : row.level === "success"
                      ? "check"
                      : row.level === "warning"
                        ? "info"
                        : "chevron-right"
                }
                size={11}
                className={[
                  "mt-0.5 shrink-0",
                  row.level === "error"
                    ? "text-danger"
                    : row.level === "success"
                      ? "text-ok"
                      : row.level === "warning"
                        ? "text-warn"
                        : "text-ink-faint",
                ].join(" ")}
              />
              <div className="min-w-0 flex-1">
                <p className="break-words text-[11px] leading-relaxed text-ink-soft">
                  {row.summary}
                </p>
                <p className="font-mono text-[10px] text-ink-faint">
                  {row.kind} · {clockTime(row.created_at)}
                </p>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}
