/**
 * ACTIVITY - the record of what NEXUS did (§36, §44, §46).
 *
 * This is the trust surface. If something is not here, the user has no way to know it
 * happened, so the screen's only job is to show the rows faithfully and never to
 * summarise them into something more comfortable.
 *
 * Four decisions worth stating:
 *
 * - **Filters are built from the closed `KINDS` tuple**, not from whatever strings the
 *   log happens to contain. A filter list derived from existing rows would silently lose
 *   a kind the moment the log was pruned, and the user would never learn that "browser"
 *   was a thing NEXUS records.
 * - **Paging is keyset, on `created_at`.** Rows arrive constantly; an offset-based page 2
 *   would skip whatever landed while page 1 was being read. "Older" therefore means
 *   `before=<the oldest row on screen>`, and pages are appended rather than replaced.
 * - **The counts are all-time and unfiltered.** `counts_by_kind` ignores the filter, so
 *   they are labelled as totals in the log rather than as counts of what is shown - two
 *   numbers that would otherwise contradict each other on the same screen.
 * - **New rows are not spliced in.** The live feed in the shell already streams events;
 *   this screen re-reads on demand instead, because a list that reorders under a reader
 *   who is scrolling through an audit trail is worse than one that says "there are newer
 *   rows, re-read".
 */

import { useState } from "react";

import { Icon } from "../components/Icon";
import {
  Button,
  EmptyState,
  ErrorState,
  LoadingState,
  Panel,
  Section,
  Select,
  StatusPill,
  clockTime,
  relativeTime,
} from "../components/primitives";
import { api, type ActivityRow } from "../lib/api";
import type { IconName } from "../components/Icon";
import { useAsync } from "../lib/useAsync";
import { useNexus } from "../state/nexus";

const PAGE = 100;

/**
 * The kinds, exactly as `db/repos/activity.py:KINDS` declares them, with an icon and a
 * plain-English label each. Kept in the same order as the tuple so the two lists can be
 * compared by eye when one of them changes.
 */
const KINDS: Array<{ kind: string; label: string; icon: IconName }> = [
  { kind: "ai", label: "Model calls", icon: "network" },
  { kind: "memory", label: "Memory", icon: "memory" },
  { kind: "tool", label: "Tools", icon: "skills" },
  { kind: "application", label: "Apps", icon: "external" },
  { kind: "file", label: "Files", icon: "database" },
  { kind: "browser", label: "Browser", icon: "external" },
  { kind: "terminal", label: "Terminal", icon: "cpu" },
  { kind: "screen", label: "Screen", icon: "vision" },
  { kind: "permission", label: "Permissions", icon: "permissions" },
  { kind: "task", label: "Tasks", icon: "tasks" },
  { kind: "device", label: "Devices", icon: "devices" },
  { kind: "automation", label: "Automations", icon: "automations" },
  { kind: "system", label: "System", icon: "core" },
  { kind: "error", label: "Failures", icon: "alert" },
];

const LEVELS = [
  { value: "", label: "Every level" },
  { value: "error", label: "Failures only" },
  { value: "warning", label: "Warnings and worse" },
  { value: "success", label: "Successes only" },
  { value: "info", label: "Plain info only" },
] as const;

/** `warning` widens to include `error`, because "show me problems" means both. */
function levelParam(level: string): string {
  if (!level) return "";
  if (level === "warning") return "warning,error";
  return level;
}

const LEVEL_ICON: Record<string, IconName> = {
  error: "alert",
  warning: "info",
  success: "check",
  info: "chevron-right",
};

const LEVEL_COLOUR: Record<string, string> = {
  error: "text-danger",
  warning: "text-warn",
  success: "text-ok",
  info: "text-ink-faint",
};

export function ActivityScreen() {
  const { link } = useNexus();
  const [kind, setKind] = useState("");
  const [level, setLevel] = useState("");
  /** Pages fetched after the first one, in order. Kept apart so a filter change drops them. */
  const [older, setOlder] = useState<ActivityRow[]>([]);
  const [paging, setPaging] = useState(false);
  const [pageError, setPageError] = useState("");
  const [exhausted, setExhausted] = useState(false);

  const head = useAsync(
    () => api.activity({ limit: PAGE, kinds: kind, levels: levelParam(level) }),
    [kind, level],
  );

  // A filter change re-runs the query above, so the accumulated pages belong to a
  // different question and are dropped rather than left on screen under a new heading.
  const filterKey = `${kind}|${level}`;
  const [lastKey, setLastKey] = useState(filterKey);
  if (lastKey !== filterKey) {
    setLastKey(filterKey);
    setOlder([]);
    setExhausted(false);
    setPageError("");
  }

  const rows = head.data ? [...head.data.activity, ...older] : [];
  const oldest = rows.length > 0 ? rows[rows.length - 1]!.created_at : "";

  async function loadOlder() {
    if (!oldest) return;
    setPaging(true);
    setPageError("");
    try {
      const result = await api.activity({
        limit: PAGE,
        kinds: kind,
        levels: levelParam(level),
        before: oldest,
      });
      if (result.activity.length === 0) setExhausted(true);
      else setOlder((current) => [...current, ...result.activity]);
    } catch (error) {
      setPageError(
        error instanceof Error ? error.message : "The older rows could not be fetched.",
      );
    } finally {
      setPaging(false);
    }
  }

  function reset() {
    setOlder([]);
    setExhausted(false);
    setPageError("");
    head.reload();
  }

  const counts = head.data?.counts ?? {};
  const total = Object.values(counts).reduce((sum, n) => sum + n, 0);

  return (
    <div className="grid h-full min-h-0 grid-cols-1 gap-2 p-2 lg:grid-cols-[minmax(200px,260px)_1fr]">
      <Panel title="FILTER" className="min-h-0" bodyClassName="flex min-h-0 flex-1 flex-col">
        <div className="min-h-0 flex-1 overflow-y-auto">
          <Section title="Level">
            <div className="px-3 py-2">
              <Select<string>
                value={level}
                onChange={setLevel}
                options={LEVELS.map((entry) => ({ value: entry.value, label: entry.label }))}
              />
            </div>
          </Section>

          <Section title="Kind" meta={total > 0 ? `${total} logged` : undefined}>
            <ul>
              <li>
                <KindRow
                  label="Everything"
                  icon="activity"
                  count={total}
                  selected={kind === ""}
                  onClick={() => setKind("")}
                />
              </li>
              {KINDS.map((entry) => (
                <li key={entry.kind}>
                  <KindRow
                    label={entry.label}
                    icon={entry.icon}
                    count={counts[entry.kind] ?? 0}
                    selected={kind === entry.kind}
                    onClick={() => setKind(entry.kind)}
                  />
                </li>
              ))}
            </ul>
            <p className="px-3 py-1.5 text-[11px] leading-relaxed text-ink-dim">
              These counts are every row in the log, not the number shown. A kind with
              zero has never been recorded - the filter is still listed, because the set
              of things NEXUS can record does not depend on what it happens to have done.
            </p>
          </Section>
        </div>
      </Panel>

      <Panel
        title="ACTIVITY"
        meta={rows.length > 0 ? `${rows.length} shown` : undefined}
        className="min-h-0"
        bodyClassName="flex min-h-0 flex-1 flex-col"
        actions={
          <Button
            size="sm"
            tone="ghost"
            icon="refresh"
            onClick={reset}
            busy={head.refreshing || paging}
            disabled={!link.connected}
            reason="The core is not reachable, so the log cannot be re-read."
          >
            Re-read
          </Button>
        }
      >
        <div className="min-h-0 flex-1 overflow-y-auto">
          {head.state === "loading" ? (
            <LoadingState what="the activity log" />
          ) : !head.data ? (
            <div className="p-2">
              <ErrorState
                what={head.error ?? "The activity log did not load."}
                did="Nothing is listed. An empty log would say NEXUS has done nothing, which is a claim this screen cannot make while the read failed."
                next={head.offline ? "Start the core, then retry." : head.detail || "Retry."}
                offline={head.offline}
                onRetry={head.reload}
                compact
              />
            </div>
          ) : rows.length === 0 ? (
            <EmptyState
              icon="activity"
              title={kind || level ? "Nothing matches this filter" : "Nothing recorded yet"}
              detail={
                kind || level
                  ? "The log is not empty - nothing in it matches. Widen the filter on the left."
                  : "Every model call, tool run, permission decision and task transition is written here as it happens. Nothing has happened yet."
              }
            />
          ) : (
            <>
              <ul>
                {rows.map((row) => (
                  <li key={row.id}>
                    <Entry row={row} />
                  </li>
                ))}
              </ul>
              <div className="border-t border-line px-3 py-2">
                {exhausted ? (
                  <p className="text-[11px] leading-relaxed text-ink-dim">
                    That is the whole log for this filter - {rows.length} row
                    {rows.length === 1 ? "" : "s"}. Older rows are pruned by the retention
                    setting, not hidden here.
                  </p>
                ) : (
                  <Button
                    size="sm"
                    tone="ghost"
                    icon="chevron-down"
                    onClick={() => void loadOlder()}
                    busy={paging}
                    disabled={!link.connected}
                    reason="The core is not reachable."
                  >
                    Older than {clockTime(oldest)}
                  </Button>
                )}
                {pageError && (
                  <p className="mt-1 text-[11px] leading-relaxed text-danger">
                    {pageError} The rows already on screen are unaffected.
                  </p>
                )}
              </div>
            </>
          )}
        </div>
      </Panel>
    </div>
  );
}

function KindRow({
  label,
  icon,
  count,
  selected,
  onClick,
}: {
  label: string;
  icon: IconName;
  count: number;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={selected}
      className={[
        "flex w-full items-center gap-2 border-b border-line/60 px-3 py-1.5 text-left transition-colors",
        selected ? "bg-accent/10 text-accent" : "text-ink-soft hover:bg-raised",
      ].join(" ")}
    >
      <Icon name={icon} size={12} className="shrink-0" />
      <span className="min-w-0 flex-1 truncate text-[11px]">{label}</span>
      <span className="shrink-0 font-mono text-[10px] text-ink-faint">{count}</span>
    </button>
  );
}

/**
 * One row.
 *
 * `detail` is opaque by design - each kind writes its own shape - so it is offered as
 * JSON behind a disclosure rather than guessed at with a per-kind renderer that would
 * quietly drop fields it did not recognise. The summary is what was written; it is shown
 * unedited.
 */
function Entry({ row }: { row: ActivityRow }) {
  const [open, setOpen] = useState(false);
  const detail = row.detail ?? {};
  const hasDetail = Object.keys(detail).length > 0;
  const { go } = useNexus();

  return (
    <div className="border-b border-line/60 px-3 py-1.5">
      <div className="flex items-start gap-2">
        <Icon
          name={LEVEL_ICON[row.level] ?? "chevron-right"}
          size={11}
          className={`mt-0.5 shrink-0 ${LEVEL_COLOUR[row.level] ?? "text-ink-faint"}`}
        />
        <div className="min-w-0 flex-1">
          <p className="break-words text-[11px] leading-relaxed text-ink-soft">{row.summary}</p>
          <div className="mt-0.5 flex flex-wrap items-center gap-2">
            <span className="font-mono text-[10px] text-ink-faint">{row.kind}</span>
            <span className="font-mono text-[10px] text-ink-faint">
              {clockTime(row.created_at)}
            </span>
            <span className="font-mono text-[10px] text-ink-dim">
              {relativeTime(row.created_at)}
            </span>
            {row.level === "error" && (
              <StatusPill state="failed" size="sm">
                FAILED
              </StatusPill>
            )}
            {row.task_id && (
              <button
                type="button"
                onClick={() => go("tasks", row.task_id)}
                className="font-mono text-[10px] text-accent hover:underline"
              >
                open the task
              </button>
            )}
            {row.conversation_id && (
              <button
                type="button"
                onClick={() => go("conversations", row.conversation_id ?? null)}
                className="font-mono text-[10px] text-accent hover:underline"
              >
                open the conversation
              </button>
            )}
            {hasDetail && (
              <button
                type="button"
                onClick={() => setOpen((current) => !current)}
                className="ml-auto flex shrink-0 items-center gap-1 font-mono text-[10px] uppercase tracking-wider text-ink-dim hover:text-ink-soft"
              >
                <Icon name={open ? "chevron-down" : "chevron-right"} size={10} />
                {open ? "less" : "detail"}
              </button>
            )}
          </div>
          {open && hasDetail && (
            <pre className="chamfer-sm mt-1 max-h-56 overflow-auto border border-line bg-base px-2 py-1.5 font-mono text-[10px] leading-relaxed text-ink-soft">
              {JSON.stringify(detail, null, 2)}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}
