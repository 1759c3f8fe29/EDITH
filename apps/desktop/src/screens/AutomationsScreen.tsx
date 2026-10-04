/**
 * AUTOMATIONS — the rules that fire without being asked (§31-§34, §47).
 *
 * An automation is a trigger plus an action, stored in the database and fired by a
 * loop in the core. Three things shape this screen, and all three are the same
 * concern from different angles: a rule that looks armed and is not is worse than
 * no rule at all, because the user stops carrying the thing themselves.
 *
 * 1. **The scheduler's own state is shown above the rules, not hidden behind
 *    them.** `GET /api/automations` returns the engine alongside the rows for
 *    exactly this reason: a rule with a perfect schedule never fires if the loop is
 *    not running, and rows alone have no way to say so. When the loop is down, the
 *    per-rule pill stops saying ARMED and says WILL NOT FIRE — an "on" toggle above
 *    something inert is the §78 failure this whole feature was built to avoid.
 *
 * 2. **The core's answer is what gets rendered, never the request.** Asking to
 *    enable a rule with an unparseable schedule gets a *disabled* row back with a
 *    reason on it. So every mutation here reads the returned row and reports what
 *    actually happened, including "you asked for on and it came back off, because…".
 *
 * 3. **RUN NOW is a real fire.** It executes the action and writes a real run row —
 *    a test fire that left no trace would make "last run" a lie by omission — and it
 *    deliberately does not move the rule's next occurrence, so checking a reminder
 *    works at 3pm does not cancel it at 8pm. Both facts are said on screen, because
 *    a button whose side effects are surprising is not a safe button.
 *
 * What this screen cannot do is compose an action the core cannot run. The kinds
 * come from the engine (`action_kinds`), and the form only offers the ones it knows
 * how to fill in. If the core grows a kind this file has no editor for, it is named
 * as available rather than silently dropped or offered as an empty form.
 */

import { useState } from "react";

import {
  ActionResult,
  Button,
  ConfirmDialog,
  DataRow,
  EmptyState,
  ErrorState,
  Field,
  IconButton,
  ListRow,
  LoadingState,
  Panel,
  Section,
  Select,
  StatusPill,
  TextInput,
  Toggle,
  clockTime,
  duration,
  relativeTime,
} from "../components/primitives";
import { api, type Automation, type AutomationEngine, type AutomationRun } from "../lib/api";
import {
  COMPOSABLE,
  LEVELS,
  TRIGGERS,
  actionLine,
  ruleState,
  scheduleHint,
  statusState,
  triggerLine,
  whenNext,
  type LevelValue,
  type TriggerValue,
} from "../lib/automations";
import { useAction, useAsync, usePoll } from "../lib/useAsync";
import { useNexus } from "../state/nexus";

/**
 * The rules change under us whenever one fires, and `due_now` is read live by the
 * core on every call. Slower than the task list because nothing here is streaming.
 */
const POLL_MS = 10_000;

export function AutomationsScreen({ focus }: { focus: string | null }) {
  const { go, link } = useNexus();
  const [composing, setComposing] = useState(false);

  const list = useAsync(() => api.automations.list(), []);
  usePoll(list.reload, POLL_MS, link.connected);

  const rules = list.data?.automations ?? [];
  const engine = list.data?.engine ?? null;
  const engineLive = Boolean(engine?.running && engine?.enabled);
  const selected = rules.find((rule) => rule.id === focus) ?? null;
  const armed = rules.filter((rule) => rule.enabled && rule.trigger_kind !== "manual").length;

  return (
    <div className="flex h-full min-h-0 flex-col gap-2 p-2">
      {engine && <EngineBar engine={engine} armed={armed} onChanged={list.reload} />}

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-2 lg:grid-cols-[minmax(260px,380px)_1fr]">
        <Panel
          title="RULES"
          meta={list.data ? `${rules.length}` : undefined}
          className="min-h-0"
          bodyClassName="flex min-h-0 flex-1 flex-col"
          actions={
            <IconButton
              name="plus"
              label="Create an automation"
              onClick={() => setComposing(true)}
              disabled={!link.connected}
              reason="The core is not reachable, so it cannot store one."
            />
          }
        >
          {composing && engine && (
            <Composer
              engine={engine}
              onClose={() => setComposing(false)}
              onCreated={(rule) => {
                list.reload();
                go("automations", rule.id);
              }}
            />
          )}

          <div className="min-h-0 flex-1 overflow-y-auto">
            {list.state === "loading" ? (
              <LoadingState what="automations" />
            ) : !list.data ? (
              <div className="p-2">
                <ErrorState
                  what={list.error ?? "The automation list did not load."}
                  did="Nothing is listed. An empty list would claim you have no rules, and a rule you think is armed is the one thing this screen must never get wrong."
                  next={list.offline ? "Start the core, then retry." : list.detail || "Retry."}
                  offline={list.offline}
                  onRetry={list.reload}
                  compact
                />
              </div>
            ) : rules.length === 0 ? (
              <EmptyState
                icon="automations"
                title="No automations yet"
                detail="A rule is a time and a thing to do. NEXUS can deliver a reminder on a schedule, every so often, or only when you run it by hand."
                action={
                  <Button
                    size="sm"
                    tone="accent"
                    icon="plus"
                    onClick={() => setComposing(true)}
                    disabled={!link.connected}
                    reason="The core is not reachable."
                  >
                    Create one
                  </Button>
                }
              />
            ) : (
              <ul>
                {rules.map((rule) => {
                  const pill = ruleState(rule, engineLive);
                  return (
                    <li key={rule.id}>
                      <ListRow
                        selected={rule.id === focus}
                        onClick={() => go("automations", rule.id)}
                      >
                        <div className="flex items-start gap-2">
                          <div className="min-w-0 flex-1">
                            <p className="truncate text-xs text-ink">{rule.name}</p>
                            <p className="mt-0.5 truncate font-mono text-[10px] text-ink-faint">
                              {triggerLine(rule)}
                              {rule.enabled && rule.next_run_at
                                ? ` · ${whenNext(rule.next_run_at)}`
                                : ""}
                            </p>
                          </div>
                          <StatusPill state={pill.state} size="sm">
                            {pill.label}
                          </StatusPill>
                        </div>
                        {rule.unavailable_reason && (
                          <p className="mt-1 break-words text-[10px] leading-relaxed text-warn">
                            {rule.unavailable_reason}
                          </p>
                        )}
                      </ListRow>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </Panel>

        {selected && engine ? (
          <Detail
            key={selected.id}
            rule={selected}
            engine={engine}
            engineLive={engineLive}
            onChanged={list.reload}
          />
        ) : (
          <Panel title="AUTOMATION" className="min-h-0">
            <EmptyState
              icon="automations"
              title={focus ? "That automation is not in the list" : "No automation selected"}
              detail={
                focus
                  ? "It may have been deleted. The list on the left is everything the core has."
                  : "Pick one on the left to see when it next fires, what it will do, and every time it has run."
              }
            />
          </Panel>
        )}
      </div>
    </div>
  );
}

/**
 * The scheduler itself, above the rules it fires.
 *
 * Two switches decide whether anything happens, and they fail differently, so they
 * are reported differently rather than collapsed into one "on/off". `running` is the
 * loop's existence in this process — the user cannot start it from here, and the bar
 * says so instead of offering a button that would do nothing. `enabled` is the
 * `automations_enabled` setting, which the loop re-reads every tick, so that one *is*
 * a real control and takes effect without a restart.
 */
function EngineBar({
  engine,
  armed,
  onChanged,
}: {
  engine: AutomationEngine;
  armed: number;
  onChanged: () => void;
}) {
  const live = engine.running && engine.enabled;

  const master = useAction(async () => {
    const next = !engine.enabled;
    await api.settings.patch({ automations_enabled: next });
    onChanged();
    if (!next) {
      return "Switched off. Rules stay exactly as they are; nothing fires until this goes back on.";
    }
    return engine.running
      ? `Switched on. The loop picks this up within one tick — at most ${Math.round(engine.tick_seconds)}s.`
      : "Switched on, but the scheduler loop is not running in this core, so nothing will fire until the core is restarted.";
  });

  const problem = !engine.running
    ? engine.stopped_reason
      ? `The scheduler stopped: ${engine.stopped_reason}. It starts with the core, so restarting the core is what brings it back.`
      : "The scheduler loop is not running in this core. It normally starts with the core; until it is running, nothing fires."
    : !engine.enabled
      ? "Automations are switched off. The loop is alive and ticking, but it skips every rule while this is off."
      : "";

  return (
    <div
      className={[
        "chamfer-sm shrink-0 border px-3 py-2",
        live ? "border-line-bright bg-panel" : "border-warn/45 bg-warn/8",
      ].join(" ")}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <span className="label marker">SCHEDULER</span>
        <StatusPill state={live ? "ok" : "warn"} pulse={live}>
          {live ? "RUNNING" : engine.running ? "SWITCHED OFF" : "NOT RUNNING"}
        </StatusPill>
        <span className="font-mono text-[10px] text-ink-faint">
          {engine.ticks} ticks · every {Math.round(engine.tick_seconds)}s · {armed} rule
          {armed === 1 ? "" : "s"} armed
          {engine.due_now > 0 ? ` · ${engine.due_now} due now` : ""}
        </span>
        <div className="ml-auto flex items-center gap-2">
          <span className="label">Automations</span>
          <Toggle
            on={engine.enabled}
            onChange={master.run}
            busy={master.busy}
            label={engine.enabled ? "Switch automations off" : "Switch automations on"}
          />
        </div>
      </div>

      {problem && <p className="mt-1.5 text-[11px] leading-relaxed text-warn">{problem}</p>}

      {engine.last && (
        <p className="mt-1 font-mono text-[10px] text-ink-faint">
          Last pass {relativeTime(engine.last.at)} — fired {engine.last.fired}, failed{" "}
          {engine.last.failed}, skipped {engine.last.skipped}
        </p>
      )}

      <ActionResult done={master.done} failed={master.failed} onClear={master.clear} />
    </div>
  );
}

/** One rule: when it fires, what it does, and everything it has ever done. */
function Detail({
  rule,
  engine,
  engineLive,
  onChanged,
}: {
  rule: Automation;
  engine: AutomationEngine;
  engineLive: boolean;
  onChanged: () => void;
}) {
  const { go, link } = useNexus();
  const [editing, setEditing] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const detail = useAsync(() => api.automations.get(rule.id), [rule.id]);
  usePoll(detail.reload, POLL_MS, link.connected);
  const runs: AutomationRun[] = detail.data?.runs ?? [];

  const reloadBoth = () => {
    onChanged();
    detail.reload();
  };

  const toggle = useAction(async () => {
    const next = !rule.enabled;
    const updated = await api.automations.update(rule.id, { enabled: next });
    reloadBoth();
    if (next && !updated.enabled) {
      // The core re-checks the schedule on every enable and refuses rather than
      // arming something that cannot fire. Reported as a failure because the thing
      // that was asked for did not happen.
      throw new Error(
        updated.unavailable_reason
          ? `It went straight back off: ${updated.unavailable_reason}`
          : "It went straight back off, and the core gave no reason.",
      );
    }
    if (!next) return "Switched off. Nothing fires until it is switched back on.";
    if (updated.trigger_kind === "manual") {
      return "Switched on. It has no schedule, so it still only fires from RUN NOW.";
    }
    if (!engineLive) {
      return `Switched on, but the scheduler is not running, so it will not fire yet. Next run would be ${clockTime(updated.next_run_at)}.`;
    }
    return `Switched on. Next run ${clockTime(updated.next_run_at)} (${whenNext(updated.next_run_at)}).`;
  });

  const runNow = useAction(async () => {
    const result = await api.automations.run(rule.id);
    reloadBoth();
    if (result.status !== "completed") {
      throw new Error(`The run ended as ${result.status}: ${result.summary}`);
    }
    return `Ran it. ${result.summary} — recorded in the history below and in ACTIVITY. Its schedule was not moved.`;
  });

  const remove = useAction(async () => {
    const result = await api.automations.remove(rule.id);
    if (result.deleted !== rule.id) {
      throw new Error("The core did not confirm the deletion — the rule may still exist.");
    }
    setConfirmDelete(false);
    onChanged();
    go("automations", null);
    return `Deleted "${rule.name}".`;
  });

  const pill = ruleState(rule, engineLive);
  const lastStatus = String(rule.last_result?.status ?? "");

  return (
    <Panel
      title={rule.name.toUpperCase()}
      meta={triggerLine(rule)}
      className="min-h-0"
      bodyClassName="flex min-h-0 flex-1 flex-col"
      actions={
        <>
          <StatusPill state={pill.state} size="sm">
            {pill.label}
          </StatusPill>
          <IconButton
            name="play"
            label="Run this automation now"
            onClick={runNow.run}
            busy={runNow.busy}
            disabled={!link.connected}
            reason="The core is not reachable."
          />
          <IconButton
            name="edit"
            label="Edit this automation"
            onClick={() => setEditing(true)}
            disabled={editing}
            reason="Already editing."
          />
          <IconButton
            name="trash"
            label="Delete this automation"
            tone="danger"
            onClick={() => setConfirmDelete(true)}
          />
        </>
      }
    >
      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-2">
        {rule.unavailable_reason && (
          <div className="chamfer-sm border border-warn/45 bg-warn/8 px-2.5 py-2 text-xs">
            <p className="label text-warn">NEXUS turned this rule off</p>
            <p className="mt-1 break-words text-ink-soft">{rule.unavailable_reason}</p>
            <p className="mt-1 text-[11px] text-ink-dim">
              Fix it below and switch it back on. Until then it is stored but inert — which is
              why it does not sit in the list looking healthy.
            </p>
          </div>
        )}

        {editing ? (
          <RuleForm
            engine={engine}
            rule={rule}
            onCancel={() => setEditing(false)}
            onSaved={() => {
              setEditing(false);
              reloadBoth();
            }}
          />
        ) : (
          <div className="chamfer-sm border border-line bg-base">
            <div className="flex items-center gap-3 border-b border-line px-3 py-2">
              <div className="min-w-0">
                <span className="label">Switched on</span>
                <p className="mt-0.5 text-[11px] text-ink-dim">
                  {rule.enabled
                    ? engineLive || rule.trigger_kind === "manual"
                      ? "This rule is live."
                      : "On, but the scheduler above is not running it."
                    : "Off. It stays stored and can be run by hand."}
                </p>
              </div>
              <div className="ml-auto">
                <Toggle
                  on={rule.enabled}
                  onChange={toggle.run}
                  busy={toggle.busy}
                  disabled={!link.connected}
                  reason="The core is not reachable."
                  label={rule.enabled ? `Switch off ${rule.name}` : `Switch on ${rule.name}`}
                />
              </div>
            </div>
            <DataRow label="Trigger" value={triggerLine(rule)} />
            <DataRow
              label="Next run"
              value={
                rule.trigger_kind === "manual"
                  ? "only when you run it"
                  : rule.next_run_at
                    ? `${clockTime(rule.next_run_at)} · ${whenNext(rule.next_run_at)}`
                    : rule.enabled
                      ? "none scheduled"
                      : "none — it is switched off"
              }
              state={rule.next_run_at && engineLive ? "ok" : undefined}
            />
            <DataRow label="Action" value={actionLine(rule)} />
          </div>
        )}

        <ActionResult done={toggle.done} failed={toggle.failed} onClear={toggle.clear} />
        <ActionResult done={runNow.done} failed={runNow.failed} onClear={runNow.clear} />
        <ActionResult done={remove.done} failed={remove.failed} onClear={remove.clear} />

        <Section title="Last run">
          <div className="flex flex-col">
            <DataRow
              label="When"
              value={rule.last_run_at ? `${clockTime(rule.last_run_at)} · ${relativeTime(rule.last_run_at)}` : "never run"}
            />
            <DataRow
              label="Outcome"
              value={lastStatus || "—"}
              state={lastStatus ? undefined : undefined}
            />
            {rule.last_result?.summary && (
              <DataRow label="Detail" value={String(rule.last_result.summary)} />
            )}
          </div>
          <p className="px-3 py-1.5 text-[11px] leading-relaxed text-ink-dim">
            Every attempt is recorded, including the failed and skipped ones, so this is the
            last <em>attempt</em> rather than the last success.
          </p>
        </Section>

        <Section title="History" meta={runs.length ? `${runs.length}` : undefined}>
          {detail.state === "loading" ? (
            <LoadingState what="run history" />
          ) : !detail.data ? (
            <div className="px-3 pb-2">
              <ErrorState
                what={detail.error ?? "The run history did not load."}
                did="Nothing is listed rather than an empty history, which would read as 'this has never run'."
                next={detail.offline ? "Start the core, then retry." : detail.detail || "Retry."}
                offline={detail.offline}
                onRetry={detail.reload}
                compact
              />
            </div>
          ) : runs.length === 0 ? (
            <p className="px-3 pb-2 text-[11px] text-ink-dim">
              This rule has never run. RUN NOW fires it once without moving its schedule.
            </p>
          ) : (
            <ul className="flex flex-col">
              {runs.map((run) => (
                <li key={run.id} className="border-b border-line/60 px-3 py-1.5 last:border-b-0">
                  <div className="flex items-baseline gap-2">
                    <StatusPill state={statusState(run.status)} size="sm">
                      {run.status}
                    </StatusPill>
                    <span className="font-mono text-[10px] text-ink-faint">
                      {clockTime(run.started_at)} · {relativeTime(run.started_at)}
                    </span>
                    {run.detail?.manual && (
                      <span className="font-mono text-[9px] uppercase tracking-wider text-ink-dim">
                        by hand
                      </span>
                    )}
                    <span className="ml-auto font-mono text-[10px] text-ink-faint">
                      {runLength(run)}
                    </span>
                  </div>
                  {(run.detail?.summary || run.detail?.error) && (
                    <p className="mt-0.5 break-words text-[11px] text-ink-soft">
                      {String(run.detail.error || run.detail.summary)}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Section>

        <Section title="Identity">
          <div className="flex flex-col">
            <DataRow label="Created" value={clockTime(rule.created_at)} />
            <DataRow label="Updated" value={clockTime(rule.updated_at)} />
            <DataRow label="Id" value={rule.id} />
          </div>
        </Section>
      </div>

      <ConfirmDialog
        open={confirmDelete}
        title="Delete this automation"
        consequence={`"${rule.name}" is removed, and so are its ${runs.length} recorded run${runs.length === 1 ? "" : "s"} — the run history is deleted with the rule. Anything it already delivered stays in ACTIVITY. This cannot be undone.`}
        confirmLabel="Delete the automation"
        onConfirm={remove.run}
        onCancel={() => setConfirmDelete(false)}
        busy={remove.busy}
        failed={remove.failed}
      />
    </Panel>
  );
}

/** How long a run took, or that it has not finished. */
function runLength(run: AutomationRun): string {
  if (!run.finished_at) return "unfinished";
  const started = Date.parse(run.started_at);
  const finished = Date.parse(run.finished_at);
  if (Number.isNaN(started) || Number.isNaN(finished)) return "—";
  return duration(finished - started);
}

/**
 * Creating one.
 *
 * The one thing this does that is not obvious: a rule the core cannot schedule is
 * *stored*, disabled, with the reason on it — a 200 does not mean "this will fire".
 * So the created row is read back and the message says which of the two happened,
 * and the screen navigates to it either way so the reason is on screen.
 */
function Composer({
  engine,
  onClose,
  onCreated,
}: {
  engine: AutomationEngine;
  onClose: () => void;
  onCreated: (rule: Automation) => void;
}) {
  return (
    <div className="border-b border-line bg-raised/40 px-3 py-2.5">
      <div className="mb-2 flex items-center justify-between">
        <span className="label">New automation</span>
        <IconButton name="close" label="Close without creating" onClick={onClose} />
      </div>
      <RuleForm engine={engine} rule={null} onCancel={onClose} onSaved={onCreated} />
    </div>
  );
}

/**
 * The one form, used for both create and edit.
 *
 * Deliberately does not re-implement the core's schedule parser. The hints say what
 * the shapes are; the core decides, and whatever it decides is what gets reported —
 * a second parser here would eventually disagree with the one that actually fires
 * things, and the screen would be confidently wrong about when a rule goes off.
 */
function RuleForm({
  engine,
  rule,
  onCancel,
  onSaved,
}: {
  engine: AutomationEngine;
  rule: Automation | null;
  onCancel: () => void;
  onSaved: (rule: Automation) => void;
}) {
  const [name, setName] = useState(rule?.name ?? "");
  const [trigger, setTrigger] = useState<TriggerValue>(
    (TRIGGERS.find((t) => t.value === rule?.trigger_kind)?.value ?? "schedule") as TriggerValue,
  );
  const [schedule, setSchedule] = useState(rule?.schedule ?? "");
  const [message, setMessage] = useState(String(rule?.action?.message ?? ""));
  const [level, setLevel] = useState<LevelValue>(
    (LEVELS.find((l) => l.value === String(rule?.action?.level ?? "info"))?.value ??
      "info") as LevelValue,
  );

  // What the core says it can run, minus what this form knows how to fill in. Named
  // on screen rather than quietly dropped: "NEXUS cannot do that" and "this form
  // cannot write that down" are different sentences and only one of them is true.
  const kind = String(rule?.action?.kind ?? "notify");
  const editable = COMPOSABLE.includes(kind);
  const uncomposable = engine.action_kinds.filter((k) => !COMPOSABLE.includes(k));

  const save = useAction(async () => {
    const cleanName = name.trim();
    if (!cleanName) throw new Error("An automation needs a name.");
    const cleanMessage = message.trim();
    if (editable && !cleanMessage) throw new Error("A reminder needs a message to deliver.");

    const action = editable ? { kind: "notify", message: cleanMessage, level } : undefined;
    const cleanSchedule = trigger === "manual" ? "" : schedule.trim();

    if (rule) {
      const updated = await api.automations.update(rule.id, {
        name: cleanName,
        trigger_kind: trigger,
        schedule: cleanSchedule,
        ...(action ? { action } : {}),
      });
      onSaved(updated);
      if (updated.unavailable_reason) {
        throw new Error(
          `Saved, but NEXUS turned it off: ${updated.unavailable_reason} It will not fire until that is fixed.`,
        );
      }
      return updated.trigger_kind === "manual"
        ? "Saved. It runs only from RUN NOW."
        : `Saved. Next run ${clockTime(updated.next_run_at)} (${whenNext(updated.next_run_at)}).`;
    }

    const created = await api.automations.create({
      name: cleanName,
      trigger_kind: trigger,
      schedule: cleanSchedule,
      action: action ?? { kind: "notify", message: cleanMessage, level },
    });
    onSaved(created);
    if (created.unavailable_reason) {
      return `Stored as "${created.name}", but switched off: ${created.unavailable_reason} The reason is on the rule — fix the schedule and switch it on.`;
    }
    return created.trigger_kind === "manual"
      ? `Created "${created.name}". It runs only from RUN NOW.`
      : `Created "${created.name}". Next run ${clockTime(created.next_run_at)} (${whenNext(created.next_run_at)}).`;
  });

  const id = rule ? `edit-${rule.id}` : "new-automation";

  return (
    <div className="space-y-2">
      <Field label="Name" htmlFor={`${id}-name`}>
        <TextInput
          id={`${id}-name`}
          value={name}
          onChange={setName}
          autoFocus
          placeholder="Stand up and stretch"
        />
      </Field>

      <Field label="When" htmlFor={`${id}-trigger`}>
        <Select<TriggerValue>
          id={`${id}-trigger`}
          value={trigger}
          onChange={setTrigger}
          options={TRIGGERS.map((t) => ({ value: t.value, label: t.label }))}
        />
      </Field>

      {trigger !== "manual" && (
        <Field label="Schedule" htmlFor={`${id}-schedule`} hint={scheduleHint(trigger)}>
          <TextInput
            id={`${id}-schedule`}
            value={schedule}
            onChange={setSchedule}
            placeholder={trigger === "interval" ? "45m" : "08:30"}
            onEnter={save.run}
          />
        </Field>
      )}
      {trigger === "manual" && (
        <p className="text-[11px] text-ink-dim">{scheduleHint("manual")}</p>
      )}

      {editable ? (
        <>
          <Field
            label="Reminder"
            htmlFor={`${id}-message`}
            hint="Delivered to ACTIVITY when it fires, and announced live if the window is open."
          >
            <TextInput
              id={`${id}-message`}
              value={message}
              onChange={setMessage}
              placeholder="Time to stretch."
              onEnter={save.run}
            />
          </Field>
          <Field label="Level" htmlFor={`${id}-level`}>
            <Select<LevelValue>
              id={`${id}-level`}
              value={level}
              onChange={setLevel}
              options={LEVELS.map((l) => ({ value: l.value, label: l.label }))}
            />
          </Field>
        </>
      ) : (
        <div className="chamfer-sm border border-line-bright bg-base px-2.5 py-2 text-[11px]">
          <p className="label">Action: {kind}</p>
          <p className="mt-1 text-ink-dim">
            This screen has no editor for that kind, so the action is left exactly as it is.
            The name and the schedule above can still be changed.
          </p>
        </div>
      )}

      {uncomposable.length > 0 && (
        <p className="text-[11px] text-ink-dim">
          The core can also run: {uncomposable.join(", ")}. This form cannot compose those yet.
        </p>
      )}

      <div className="flex items-center gap-2">
        <Button size="sm" tone="accent" icon="check" onClick={save.run} busy={save.busy}>
          {rule ? "Save" : "Create"}
        </Button>
        <Button size="sm" tone="ghost" onClick={onCancel} disabled={save.busy} reason="Saving.">
          Cancel
        </Button>
      </div>

      <ActionResult done={save.done} failed={save.failed} onClear={save.clear} />
    </div>
  );
}
