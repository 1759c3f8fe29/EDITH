/**
 * AWARENESS — what NEXUS is watching, and what it wrote down (§39, §47, §54, L5).
 *
 * This screen exists because "the assistant can see your screen" is a sentence the user
 * must be able to verify, not take on trust. Three facts are kept apart on purpose, and
 * the route module says why:
 *
 * - **`monitor_enabled` is the master switch.** It is `False` by default and while it is
 *   off nothing observes, regardless of the per-monitor toggles. So it is drawn once, at
 *   the top, and every per-monitor row states that it is subordinate to it.
 * - **`enabled` is not `running`.** `enabled` is the stored setting; `running` is whether
 *   a task is actually in the loop. Merging them into one indicator would let a monitor
 *   that failed to start look identical to one that is watching.
 * - **A monitor that cannot run says so.** `apply_settings` returns `"unavailable"` for a
 *   monitor whose binary is missing, and `PATCH /api/monitors/settings` surfaces that
 *   list unchanged. A toggle that flips on and then quietly observes nothing is exactly
 *   the fake feature §80 forbids, so the response is read and reported.
 *
 * Two smaller commitments:
 *
 * - **Toggles write `enabled_setting`, never a constructed key.** `Monitor.to_dict()`
 *   ships the exact settings key the core reads. Guessing `monitor_${id}_enabled` would
 *   work for today's four monitors and silently break on the fifth — `set_many` stores
 *   any key, and a key nothing reads is a control that appears to save and does nothing.
 * - **Forgetting requires a choice.** `POST /api/monitors/forget` 400s on an unqualified
 *   body, and ambient rows are hard-deleted with no tombstone. The UI asks what to
 *   forget rather than offering one button that means "everything".
 */

import { useRef, useState } from "react";

import { Icon } from "../components/Icon";
import {
  ActionResult,
  Button,
  ConfirmDialog,
  DataRow,
  EmptyState,
  ErrorState,
  IconButton,
  LoadingState,
  Panel,
  Section,
  Select,
  StatusPill,
  Tabs,
  Toggle,
  clockTime,
  duration,
  relativeTime,
} from "../components/primitives";
import {
  api,
  type AmbientCounts,
  type AmbientEvent,
  type MonitorState,
  type MonitorsOverview,
} from "../lib/api";
import { useAction, useAsync, usePoll } from "../lib/useAsync";
import { useNexus } from "../state/nexus";

/** The overview is cheap — counts, not events — so it can refresh while looked at. */
const OVERVIEW_POLL_MS = 8_000;
/** The timeline is a paged query. Slower, and only while the tab is open. */
const TIMELINE_POLL_MS = 15_000;

/**
 * The monitors that observe *the user* rather than the machine, from
 * `supervisor._OBSERVES_USER`. Only these three make `watching` non-empty, and only
 * these three warrant the standing indicator: a CPU gauge is not surveillance.
 */
const OBSERVES_USER = new Set(["screen", "window", "clipboard"]);

type Tab = "monitors" | "timeline" | "history";

export function AwarenessScreen() {
  const { link } = useNexus();
  const [tab, setTab] = useState<Tab>("monitors");

  const overview = useAsync(() => api.monitors.overview(), []);
  usePoll(overview.reload, OVERVIEW_POLL_MS, link.connected);

  const data = overview.data;

  return (
    <div className="flex h-full min-h-0 flex-col gap-2 p-2">
      <Panel
        title="AWARENESS"
        meta={data ? `${data.monitors.length} monitors` : undefined}
        className="min-h-0 flex-1"
        bodyClassName="flex min-h-0 flex-1 flex-col"
        actions={
          <Button
            size="sm"
            tone="ghost"
            icon="refresh"
            onClick={overview.reload}
            busy={overview.refreshing}
          >
            Re-read
          </Button>
        }
      >
        <Tabs<Tab>
          value={tab}
          onChange={setTab}
          tabs={[
            { value: "monitors", label: "Monitors" },
            { value: "timeline", label: "What it saw" },
            { value: "history", label: "History" },
          ]}
          meta={
            data ? (
              <span className="font-mono text-[10px] text-ink-faint">
                {data.ambient_counts.total} observations kept
              </span>
            ) : undefined
          }
        />

        {overview.state === "loading" ? (
          <LoadingState what="the monitors" />
        ) : !data ? (
          <div className="p-2">
            <ErrorState
              what={overview.error ?? "The awareness state did not load."}
              did="Nothing is shown. An empty list of monitors would read as 'nothing is watching you', which is a claim this screen cannot make without the core."
              next={overview.offline ? "Start the core, then retry." : overview.detail || "Retry."}
              offline={overview.offline}
              onRetry={overview.reload}
              compact
            />
          </div>
        ) : tab === "monitors" ? (
          <MonitorsTab overview={data} onChanged={overview.reload} />
        ) : tab === "timeline" ? (
          <TimelineTab monitors={data.monitors} />
        ) : (
          <HistoryTab overview={data} onChanged={overview.reload} />
        )}
      </Panel>
    </div>
  );
}

/**
 * `useAction` runs a closure that takes no arguments, but every control here writes a
 * *different* settings key. The values are handed over through a ref set immediately
 * before `run()`, in the same synchronous event handler, so there is no window in which
 * a second click could overwrite the first's payload — and the ref is cleared as it is
 * read, so a `run()` with nothing staged fails loudly instead of re-sending the last one.
 */
function useSettingsWriter(
  describe: (values: Record<string, unknown>, result: SettingsResult) => string,
  after: (result: SettingsResult) => void,
) {
  const staged = useRef<Record<string, unknown> | null>(null);
  const action = useAction(async () => {
    const values = staged.current;
    staged.current = null;
    if (!values) throw new Error("Nothing was staged to write — the click did not carry a setting.");
    const result = await api.monitors.updateSettings(values);
    after(result);
    return describe(values, result);
  });
  return {
    ...action,
    write: (values: Record<string, unknown>) => {
      staged.current = values;
      action.run();
    },
  };
}

type SettingsResult = Awaited<ReturnType<typeof api.monitors.updateSettings>>;

/* ------------------------------------------------------------------ monitors */

/** Read a boolean out of the untyped settings bag, keeping "absent" distinct. */
function boolSetting(settings: Record<string, unknown>, key: string): boolean | undefined {
  const value = settings[key];
  return typeof value === "boolean" ? value : undefined;
}

function numSetting(settings: Record<string, unknown>, key: string): number | undefined {
  const value = settings[key];
  return typeof value === "number" ? value : undefined;
}

function MonitorsTab({
  overview,
  onChanged,
}: {
  overview: MonitorsOverview;
  onChanged: () => void;
}) {
  const { link } = useNexus();
  const [lastUnavailable, setLastUnavailable] = useState<string[]>([]);

  /**
   * One writer for every toggle and every interval on this tab.
   *
   * It PATCHes, then reports what came back: `changes` says what the supervisor did per
   * monitor, and `unavailable` is the one outcome that has to become a sentence rather
   * than a state change. The overview is re-read rather than the response spliced in —
   * the returned `status` omits `settings` and `ambient_counts`, so splicing it would
   * blank the very values the rows are drawn from.
   */
  const write = useSettingsWriter(
    (values, result) => {
      const keys = Object.keys(values).join(", ");
      if (result.unavailable.length > 0) {
        return `Saved ${keys}. ${result.unavailable.join(", ")} cannot run on this machine — see the reason on the row.`;
      }
      const acted = Object.entries(result.changes)
        .filter(([, outcome]) => outcome !== "unchanged")
        .map(([id, outcome]) => `${id} ${outcome}`);
      return acted.length > 0
        ? `Saved. ${acted.join(", ")}.`
        : `Saved ${keys}. No monitor changed state.`;
    },
    (result) => {
      setLastUnavailable(result.unavailable);
      onChanged();
    },
  );

  const master = boolSetting(overview.settings, "monitor_enabled") ?? overview.master_enabled;

  return (
    <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-2">
      <div
        className={[
          "chamfer-sm border px-2.5 py-2",
          master ? "border-accent/40 bg-accent/6" : "border-line-bright bg-raised",
        ].join(" ")}
      >
        <div className="flex items-start gap-3">
          <Toggle
            on={master}
            onChange={(next) => write.write({ monitor_enabled: next })}
            label="Awareness master switch"
            busy={write.busy}
            disabled={!link.connected}
            reason="The core is not reachable, so it cannot start or stop a monitor."
          />
          <div className="min-w-0 flex-1">
            <span className={`label ${master ? "label-accent" : ""}`}>
              Awareness is {master ? "on" : "off"}
            </span>
            <p className="mt-1 text-[11px] leading-relaxed text-ink-soft">
              {master
                ? "The monitors below run according to their own switches. Turning this off stops all of them at once, whatever they are set to."
                : "Nothing observes anything while this is off, no matter what the monitors below say. Their switches are stored settings, not activity — this is the one that decides."}
            </p>
            <p className="mt-1 font-mono text-[10px] text-ink-faint">
              monitor_enabled · supervisor {overview.started ? "started" : "not started"}
            </p>
          </div>
        </div>
      </div>

      <ActionResult done={write.done} failed={write.failed} onClear={write.clear} />

      <div className="space-y-2">
        {overview.monitors.map((monitor) => (
          <MonitorCard
            key={monitor.id}
            monitor={monitor}
            master={master}
            settings={overview.settings}
            unavailable={lastUnavailable.includes(monitor.id)}
            busy={write.busy}
            onWrite={write.write}
          />
        ))}
      </div>

      <Section title="Alert thresholds" meta="system health only">
        <p className="border-b border-line/60 px-3 py-1.5 text-[11px] leading-relaxed text-ink-dim">
          The system monitor writes an alert when a reading crosses one of these. Battery
          is the one that fires when the value drops <em>below</em> the number.
        </p>
        <div className="flex flex-col">
          {(
            [
              ["monitor_cpu_alert_percent", "CPU above"],
              ["monitor_memory_alert_percent", "Memory above"],
              ["monitor_disk_alert_percent", "Disk above"],
              ["monitor_battery_alert_percent", "Battery below"],
            ] as const
          ).map(([key, label]) => {
            const value = numSetting(overview.settings, key);
            return (
              <div
                key={key}
                className="flex items-center gap-2 border-b border-line/60 px-3 py-1.5 last:border-b-0"
              >
                <span className="flex-1 text-[11px] text-ink-soft">{label}</span>
                {value === undefined ? (
                  <span className="font-mono text-[10px] text-ink-faint">not reported</span>
                ) : (
                  <Select<string>
                    value={String(value)}
                    disabled={write.busy || !link.connected}
                    onChange={(next) => write.write({ [key]: Number(next) })}
                    options={PERCENTS.map((n) => ({ value: String(n), label: `${n}%` }))}
                  />
                )}
              </div>
            );
          })}
        </div>
      </Section>
    </div>
  );
}

/** Closed list, because the setting is an int and a free field invites 900. */
const PERCENTS = [5, 10, 15, 20, 25, 50, 75, 80, 85, 90, 92, 95, 98] as const;

/** Intervals offered per monitor, in seconds. Each list starts at the monitor's default. */
const INTERVALS = [1, 2, 3, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600] as const;

/**
 * One monitor: the setting, the reality, and the distance between them.
 *
 * The row's own state dot is `running`, never `enabled`. When the two disagree the card
 * says which way and why — an enabled monitor that is not running is either blocked by
 * the master switch, or blocked by `unavailable_reason`, and those need different words.
 */
function MonitorCard({
  monitor,
  master,
  settings,
  unavailable,
  busy,
  onWrite,
}: {
  monitor: MonitorState;
  master: boolean;
  settings: Record<string, unknown>;
  unavailable: boolean;
  busy: boolean;
  onWrite: (values: Record<string, unknown>) => void;
}) {
  const { link } = useNexus();
  const [open, setOpen] = useState(false);

  const blocked = Boolean(monitor.unavailable_reason);
  const interval = numSetting(settings, monitor.interval_setting) ?? monitor.interval_seconds;
  const observesUser = OBSERVES_USER.has(monitor.id);

  return (
    <div
      className={[
        "chamfer-sm border px-2.5 py-2",
        monitor.running ? "border-accent/35 bg-accent/4" : "border-line bg-base",
      ].join(" ")}
    >
      <div className="flex items-start gap-3">
        <Toggle
          on={monitor.enabled}
          onChange={(next) => onWrite({ [monitor.enabled_setting]: next })}
          label={`${monitor.title}: ${monitor.enabled ? "on" : "off"}`}
          busy={busy}
          disabled={!link.connected || blocked}
          reason={
            blocked
              ? `It cannot run here: ${monitor.unavailable_reason}`
              : "The core is not reachable, so this cannot be saved."
          }
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <p className="text-xs text-ink">{monitor.title}</p>
            <StatusPill
              state={monitor.running ? "active" : blocked ? "failed" : "idle"}
              size="sm"
              pulse={monitor.running}
            >
              {monitor.running ? "observing" : blocked ? "cannot run" : "not running"}
            </StatusPill>
            {observesUser && (
              <StatusPill state="pending" size="sm">
                SEES YOU
              </StatusPill>
            )}
          </div>
          <p className="mt-1 text-[11px] leading-relaxed text-ink-soft">{monitor.description}</p>

          {/* The whole point of the screen: setting versus reality, in words. */}
          {monitor.enabled && !monitor.running && (
            <p className="mt-1.5 text-[11px] leading-relaxed text-warn">
              {blocked
                ? `Switched on, but not observing: ${monitor.unavailable_reason}`
                : !master
                  ? "Switched on, but not observing — the master switch above is off."
                  : "Switched on, but no task is observing. The supervisor has not started it."}
            </p>
          )}
          {!monitor.enabled && monitor.running && (
            <p className="mt-1.5 text-[11px] leading-relaxed text-warn">
              Switched off in settings, yet a task is still observing. Re-read to confirm;
              if it persists the supervisor did not reconcile.
            </p>
          )}
          {unavailable && (
            <p className="mt-1.5 text-[11px] leading-relaxed text-danger">
              The last save reported this monitor as unavailable, so the switch was stored
              but nothing started.
            </p>
          )}

          <button
            type="button"
            onClick={() => setOpen((current) => !current)}
            className="mt-1.5 flex items-center gap-1 font-mono text-[10px] uppercase tracking-wider text-ink-dim hover:text-accent"
          >
            <Icon name={open ? "chevron-down" : "chevron-right"} size={10} />
            {open ? "less" : "detail"}
          </button>
        </div>
      </div>

      {open && (
        <div className="mt-2 border-t border-line/60 pt-2">
          <div className="mb-2 flex items-center gap-2 px-1">
            <span className="flex-1 text-[11px] text-ink-soft">Checks every</span>
            <Select<string>
              value={String(interval)}
              disabled={busy || !link.connected}
              onChange={(next) => onWrite({ [monitor.interval_setting]: Number(next) })}
              options={INTERVALS.map((n) => ({ value: String(n), label: duration(n * 1000) }))}
            />
          </div>
          <div className="flex flex-col">
            <DataRow
              label="Runs recorded"
              value={String(monitor.samples)}
              title="How many times the loop has executed since the core started."
            />
            <DataRow
              label="Observations written"
              value={String(monitor.observations)}
              state={monitor.observations > 0 ? "ok" : "idle"}
              title="A run that saw no change writes nothing, so this is lower than the run count."
            />
            <DataRow
              label="Last run"
              value={monitor.last_run_at ? clockTime(monitor.last_run_at) : "it has never run"}
              state={monitor.last_run_at ? "idle" : "pending"}
            />
            <DataRow
              label="Last observation"
              value={
                monitor.last_observation_at
                  ? clockTime(monitor.last_observation_at)
                  : "it has never written one"
              }
              state={monitor.last_observation_at ? "idle" : "pending"}
            />
            {monitor.consecutive_errors > 0 && (
              <DataRow
                label="Failing"
                value={`${monitor.consecutive_errors} run${monitor.consecutive_errors === 1 ? "" : "s"} in a row`}
                state="failed"
              />
            )}
            {monitor.last_error && (
              <DataRow label="Last error" value={monitor.last_error} state="failed" />
            )}
            <DataRow
              label="Needs permission"
              value={monitor.capability || "none declared"}
              title="Checked once at start. A capability the user denied stops the monitor."
            />
            <DataRow label="Setting key" value={monitor.enabled_setting} />
          </div>
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ timeline */

/**
 * What was actually observed.
 *
 * Two views of the same rows, and the difference is stated rather than left to be
 * discovered: `/grouped` folds consecutive sightings of one condition into a single
 * entry with `occurrences`, `/timeline` returns every row. A battery that stayed low for
 * an hour is one line in the first and twelve in the second, and both are true answers
 * to different questions.
 */
function TimelineTab({ monitors }: { monitors: MonitorState[] }) {
  const { link } = useNexus();
  const [monitor, setMonitor] = useState("");
  const [folded, setFolded] = useState(true);

  const feed = useAsync(
    async () => {
      const params = monitor ? { limit: 200, monitor } : { limit: 200 };
      if (folded) {
        const result = await api.monitors.grouped(params);
        return { events: result.timeline, folded: true };
      }
      const result = await api.monitors.timeline(params);
      return { events: result.events, folded: false };
    },
    [monitor, folded],
  );
  usePoll(feed.reload, TIMELINE_POLL_MS, link.connected);

  const events = feed.data?.events ?? [];

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
        <Select<string>
          value={monitor}
          onChange={setMonitor}
          options={[
            { value: "", label: "All monitors" },
            ...monitors.map((entry) => ({ value: entry.id, label: entry.title })),
          ]}
        />
        <Select<string>
          value={folded ? "grouped" : "raw"}
          onChange={(next) => setFolded(next === "grouped")}
          options={[
            { value: "grouped", label: "Repeats folded" },
            { value: "raw", label: "Every row" },
          ]}
        />
        <IconButton
          name="refresh"
          label="Re-read the timeline"
          onClick={feed.reload}
          busy={feed.refreshing}
        />
        <span className="ml-auto font-mono text-[10px] text-ink-faint">
          {feed.data ? `${events.length} shown` : ""}
        </span>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {feed.state === "loading" ? (
          <LoadingState what="the timeline" />
        ) : !feed.data ? (
          <div className="p-2">
            <ErrorState
              what={feed.error ?? "The timeline did not load."}
              did="Nothing is listed. An empty timeline would claim nothing was ever observed."
              next={feed.offline ? "Start the core, then retry." : feed.detail || "Retry."}
              offline={feed.offline}
              onRetry={feed.reload}
              compact
            />
          </div>
        ) : events.length === 0 ? (
          <EmptyState
            icon="vision"
            title="Nothing recorded"
            detail={
              monitor
                ? "This monitor has written no observations. Either it has not run, or nothing it watches changed."
                : "No observations are stored. Monitors only write when something changes — and with history switched off they write nothing at all."
            }
          />
        ) : (
          <ul>
            {events.map((event) => (
              <EventRow key={event.id} event={event} />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/**
 * One observation.
 *
 * `clears` is rendered as a resolution rather than as another alert: a row saying "CPU
 * back to normal" carries the opposite meaning to the row that raised it, and colouring
 * both red would make a recovered machine look like a failing one.
 */
function EventRow({ event }: { event: AmbientEvent }) {
  const resolved = Boolean(event.clears);
  const severity = resolved ? "ok" : event.severity;
  const repeats = event.occurrences ?? 1;

  return (
    <li className="flex items-start gap-2 border-b border-line/60 px-3 py-1.5">
      <Icon
        name={resolved ? "check" : severity === "alert" ? "alert" : severity === "notice" ? "info" : "vision"}
        size={11}
        className={[
          "mt-0.5 shrink-0",
          resolved
            ? "text-ok"
            : severity === "alert"
              ? "text-danger"
              : severity === "notice"
                ? "text-warn"
                : "text-ink-faint",
        ].join(" ")}
      />
      <div className="min-w-0 flex-1">
        <p className="break-words text-[11px] leading-relaxed text-ink-soft">{event.summary}</p>
        <p className="font-mono text-[10px] text-ink-faint">
          {event.monitor} · {event.kind} · {relativeTime(event.observed_at)}
          {repeats > 1 && ` · ${repeats}× since ${clockTime(event.first_at ?? event.observed_at)}`}
          {resolved && " · resolved"}
        </p>
      </div>
    </li>
  );
}

/* ------------------------------------------------------------------- history */

/**
 * How long observations live, and how to delete them (§39).
 *
 * `ambient_history_enabled` is the setting that decides whether awareness leaves a
 * record at all — monitors still drive the live UI over the event bus with it off. That
 * distinction is written on screen, because "turn off history" and "stop watching" are
 * different requests and only one of them is this switch.
 */
function HistoryTab({
  overview,
  onChanged,
}: {
  overview: MonitorsOverview;
  onChanged: () => void;
}) {
  const { link } = useNexus();
  // The parent already holds the overview and polls it. Fetching it a second time here
  // would double the request rate and let the two copies disagree mid-poll, so the
  // settings bag and the counts are read from the one the parent already has.
  const { settings, ambient_counts: counts, monitors } = overview;
  const historyOn = boolSetting(settings, "ambient_history_enabled");
  const retention = numSetting(settings, "ambient_retention_days") ?? counts.retention_days;

  const [scope, setScope] = useState("");
  const [confirming, setConfirming] = useState(false);

  const write = useSettingsWriter(
    (values) => `Saved ${Object.keys(values).join(", ")}.`,
    onChanged,
  );

  const forget = useAction(async () => {
    const body = forgetBody(scope);
    if (!body) throw new Error("Choose what to forget first.");
    const result = await api.monitors.forget(body);
    setConfirming(false);
    setScope("");
    onChanged();
    return result.message;
  });

  const byMonitor = Object.entries(counts.by_monitor).sort((a, b) => b[1] - a[1]);
  const byKind = Object.entries(counts.by_kind).sort((a, b) => b[1] - a[1]);

  return (
    <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-2">
      <Section title="What is stored" meta={`${counts.total} observations`}>
        <div className="flex flex-col">
          <DataRow
            label="Alerts among them"
            value={String(counts.alerts)}
            state={counts.alerts > 0 ? "failed" : "ok"}
          />
          <DataRow
            label="Oldest kept"
            value={counts.oldest ? clockTime(counts.oldest) : "nothing is stored"}
            state={counts.oldest ? "idle" : "pending"}
          />
          <DataRow
            label="Newest"
            value={counts.newest ? clockTime(counts.newest) : "nothing is stored"}
            state={counts.newest ? "idle" : "pending"}
          />
          <DataRow
            label="Deleted after"
            value={retention === 0 ? "the end of today" : `${retention} day${retention === 1 ? "" : "s"}`}
            title="Observations are removed this many days after they happened, read or not."
          />
        </div>
        {byMonitor.length > 0 && (
          <div className="flex flex-col border-t border-line/60">
            {byMonitor.map(([id, n]) => (
              <DataRow
                key={id}
                label={monitors.find((entry) => entry.id === id)?.title ?? id}
                value={String(n)}
              />
            ))}
          </div>
        )}
        {byKind.length > 0 && (
          <p className="border-t border-line/60 px-3 py-1.5 font-mono text-[10px] leading-relaxed text-ink-faint">
            {byKind.map(([kind, n]) => `${kind} ${n}`).join(" · ")}
          </p>
        )}
      </Section>

      <div className="chamfer-sm border border-line bg-base px-2.5 py-2">
        <div className="flex items-start gap-3">
          <Toggle
            on={historyOn ?? true}
            onChange={(next) => write.write({ ambient_history_enabled: next })}
            label="Keep a record of observations"
            busy={write.busy}
            disabled={!link.connected || historyOn === undefined}
            reason={
              historyOn === undefined
                ? "The core did not report this setting, so it cannot be changed from here."
                : "The core is not reachable, so it cannot be saved."
            }
          />
          <div className="min-w-0 flex-1">
            <span className="label">Write observations down</span>
            <p className="mt-1 text-[11px] leading-relaxed text-ink-soft">
              With this off the monitors keep running and keep driving the live view, but
              nothing is stored — the timeline stops filling. It does not stop the
              watching; the master switch on the Monitors tab does that.
            </p>
            <p className="mt-1 font-mono text-[10px] text-ink-faint">ambient_history_enabled</p>
          </div>
        </div>
      </div>

      <Section title="Retention">
        <div className="flex items-center gap-2 px-3 py-2">
          <span className="flex-1 text-[11px] leading-relaxed text-ink-soft">
            Delete observations this many days after they happened.
          </span>
          <Select<string>
            value={String(retention)}
            disabled={write.busy || !link.connected}
            onChange={(next) => write.write({ ambient_retention_days: Number(next) })}
            options={RETENTION_DAYS.map((n) => ({
              value: String(n),
              label: n === 0 ? "Today only" : `${n} days`,
            }))}
          />
        </div>
      </Section>

      <ActionResult done={write.done} failed={write.failed} onClear={write.clear} />

      <Section title="Forget observations">
        <p className="border-b border-line/60 px-3 py-1.5 text-[11px] leading-relaxed text-ink-dim">
          This is a real delete with no tombstone — there is nothing to undo from. The
          core refuses an unqualified request, so choose a scope.
        </p>
        <div className="flex flex-wrap items-center gap-2 px-3 py-2">
          <Select<string>
            value={scope}
            onChange={setScope}
            options={[
              { value: "", label: "Choose what to forget" },
              { value: "hour", label: "The last hour" },
              { value: "day", label: "The last 24 hours" },
              ...monitors.map((entry) => ({ value: entry.id, label: `Everything from ${entry.title}` })),
              { value: "everything", label: "Everything, all monitors" },
            ]}
          />
          <Button
            size="sm"
            tone="danger"
            icon="trash"
            onClick={() => setConfirming(true)}
            disabled={!scope || !link.connected}
            reason={!scope ? "Choose a scope first." : "The core is not reachable."}
          >
            Forget
          </Button>
        </div>
        <ActionResult done={forget.done} failed={forget.failed} onClear={forget.clear} />
      </Section>

      <ConfirmDialog
        open={confirming}
        title="Delete observations"
        consequence={forgetConsequence(scope, counts, monitors)}
        confirmLabel="Delete them"
        onConfirm={forget.run}
        onCancel={() => setConfirming(false)}
        busy={forget.busy}
        failed={forget.failed}
      />
    </div>
  );
}

const RETENTION_DAYS = [0, 1, 3, 7, 14, 30, 90] as const;

const WINDOWS: Record<string, { ms: number; label: string }> = {
  hour: { ms: 60 * 60 * 1000, label: "the last hour" },
  day: { ms: 24 * 60 * 60 * 1000, label: "the last 24 hours" },
};

/**
 * Turn the chosen scope into the body the route accepts.
 *
 * `since` deletes rows *at or after* the timestamp — `ambient.forget` filters on
 * `observed_at >= ?`. So it expresses "forget what you have just seen", not "clear out
 * the old stuff", and the options are worded that way. Retention already handles ageing
 * out; there is no route that deletes only the old.
 *
 * `everything: true` is sent only for the explicit "everything" choice, and `null` for an
 * unmade one — the route 400s without one of the three, and returning null is what keeps
 * the button from sending a request that cannot succeed.
 */
function forgetBody(scope: string): { since?: string; monitor?: string; everything?: boolean } | null {
  if (!scope) return null;
  if (scope === "everything") return { everything: true };
  const window = WINDOWS[scope];
  if (window) return { since: new Date(Date.now() - window.ms).toISOString() };
  return { monitor: scope };
}

function forgetConsequence(scope: string, counts: AmbientCounts, monitors: MonitorState[]): string {
  if (scope === "everything") {
    return `All ${counts.total} stored observations are deleted, from every monitor. They are hard-deleted — nothing is kept, nothing syncs, and this cannot be undone.`;
  }
  const window = WINDOWS[scope];
  if (window) {
    return `Every observation recorded in ${window.label} is deleted, from all monitors. Anything older is kept — this deletes the recent, not the stale. It cannot be undone.`;
  }
  const monitor = monitors.find((entry) => entry.id === scope);
  const n = counts.by_monitor[scope] ?? 0;
  return `All ${n} observation${n === 1 ? "" : "s"} from ${monitor?.title ?? scope} are deleted, however old. Other monitors are untouched. This cannot be undone.`;
}
