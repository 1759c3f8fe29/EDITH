/**
 * NEXUS CORE — the three-zone home screen (§64, §49, §11).
 *
 * §64 fixes the geometry: intelligence and system state on the left, the voice sphere
 * dominant in the centre, the console on the right. §49 fixes the emphasis — exactly
 * one panel carries the accent frame at a time, and it is whichever subsystem is
 * actually doing something.
 *
 * Two absences on this screen are deliberate and are the reason it can be trusted:
 *
 * - **LIVE HEADLINES and DAILY INTELLIGENCE are not here.** The reference layout has
 *   them; the core has no feed reader and no brief generator, so there is nothing
 *   truthful to put in either panel. They are listed in Settings under what is not
 *   built and what each needs — see `PENDING_SCREENS`.
 * - **The sphere never claims LISTENING or SPEAKING.** The microphone belongs to the
 *   instruction bar, which shows its own recording state; and `/api/voice/speak`
 *   returns a filesystem path that no route serves, so nothing in a browser can play
 *   it. A sphere pulsing SPEAKING over silence is the §61 failure exactly.
 */

import { useEffect, useState } from "react";

import { Icon } from "../components/Icon";
import { OrbStage } from "../components/OrbStage";
import { TurnView } from "../components/TurnView";
import { VoiceSphere, sphereStateFrom } from "../components/VoiceSphere";
import { ScreenPreview } from "../components/ScreenPreview";
import {
  Button,
  DataRow,
  EmptyState,
  ErrorState,
  IconButton,
  ListRow,
  LoadingState,
  Panel,
  Section,
  StatusPill,
  StateDot,
  Tabs,
  relativeTime,
  stateOf,
} from "../components/primitives";
import { api, type SystemSnapshot } from "../lib/api";
import { useAsync, usePoll, type AsyncResult } from "../lib/useAsync";
import { useNexus } from "../state/nexus";
import { turnSentence, useTurn } from "../state/turn";
import { useVoice } from "../state/voice";

/** Slow on purpose: this runs a real tool on the machine and writes an audit row. */
const SYSTEM_POLL_MS = 15_000;

type ConsoleTab = "voice" | "ai" | "tasks" | "activity" | "system";

export function CoreScreen() {
  const { link, pending, runningTools, activity, go } = useNexus();
  const { turn, running, executing } = useTurn();
  const voice = useVoice();
  const [tab, setTab] = useState<ConsoleTab>("ai");

  const status = useAsync(() => api.systemStatus(), []);
  usePoll(status.reload, SYSTEM_POLL_MS, link.connected);

  // Start live screen capture on app launch
  useEffect(() => {
    if (link.connected) {
      api.settings.patch({
        monitor_screen_enabled: true,
        monitor_screen_describe: true,
      }).catch(() => {}); // Silent - screen monitoring is best-effort
    }
    return () => {
      // Optionally stop on unmount - but we want it to persist
    };
  }, [link.connected]);

  const sphere = sphereStateFrom({
    connected: link.connected,
    pendingApproval: pending.length > 0,
    executing,
    thinking: running && !executing,
    // Both were hard-coded false while nothing in the app could report them. They are
    // real facts now: a microphone this window holds open, and audio it is playing.
    listening: voice.listening,
    speaking: voice.speaking,
    failed: turn?.phase === "failed" || turn?.phase === "dropped",
  });

  return (
    <div className="grid h-full min-h-0 grid-cols-1 gap-2 overflow-y-auto p-2 xl:grid-cols-[minmax(240px,1fr)_minmax(360px,1.4fr)_minmax(300px,1.2fr)] xl:overflow-hidden">
      {/* ---------------------------------------------------------- left zone */}
      <div className="flex min-h-0 flex-col gap-2">
        <IntelligencePanel />
        <SystemPanel status={status} />
      </div>

      {/* -------------------------------------------------------- centre zone */}
      <div className="flex min-h-0 flex-col gap-2">
        {/* Voice sphere - top half */}
        <Panel
          title="NEXUS VOICE"
          meta={link.connected ? "core online" : "core unreachable"}
          active={running || pending.length > 0}
          className="min-h-0 flex-1"
          bodyClassName="flex min-h-0 flex-1 flex-col items-center justify-center gap-4 px-3 py-4"
          scroll
        >
          {/* The holographic stage: backlight, beam and dust beneath the sphere.
              `active` is only true when something real is happening — the mic is
              open or audio is playing — so the dust never drifts over an idle
              machine (§71), and no amplitude is invented (§61). */}
          <OrbStage
            active={voice.listening || voice.speaking}
            className="flex min-h-0 flex-1 items-center justify-center"
          >
            <VoiceSphere
              state={sphere}
              detail={
              pending.length > 0
                ? `${pending.length} action${pending.length === 1 ? "" : "s"} waiting for your decision`
                : voice.listening
                  ? "Listening. Press the microphone again to stop."
                  : voice.speaking
                    ? voice.spoken
                    : turn
                      ? turnSentence(turn)
                      : link.connected
                        ? voice.voiceEnabled
                          ? "Speak or type an instruction below."
                          : "Type or dictate an instruction below."
                        : link.reason
              }
              size={230}
            />
          </OrbStage>

          {runningTools.length > 0 && (
            <ul className="w-full max-w-sm space-y-1">
              {runningTools.map((item) => (
                <li
                  key={`${item.tool}-${item.startedAt}`}
                  className="chamfer-sm flex items-center gap-2 border border-ok/40 bg-ok/8 px-2 py-1"
                >
                  <Icon name="cpu" size={12} className="shrink-0 animate-pulse text-ok" />
                  <span className="min-w-0 flex-1 truncate text-[11px] text-ink-soft">
                    {item.description || item.title}
                  </span>
                  <span className="shrink-0 font-mono text-[10px] text-ink-faint">
                    {Math.max(0, Math.round((Date.now() - item.startedAt) / 1000))}s
                  </span>
                </li>
              ))}
            </ul>
          )}

          {turn?.text && (
            <p className="max-h-40 w-full max-w-lg overflow-y-auto whitespace-pre-wrap break-words px-2 text-center text-xs leading-relaxed text-ink">
              {turn.text}
            </p>
          )}

          {!link.connected && (
            <p className="max-w-sm px-2 text-center text-[11px] leading-relaxed text-ink-dim">
              Nothing on this screen can be trusted while the core is unreachable, so the
              panels around it are showing their last known values, labelled as stale.
            </p>
          )}
        </Panel>

        {/* Live screen preview - bottom half */}
        <ScreenPreview />
      </div>

      {/* --------------------------------------------------------- right zone */}
      <Panel
        title="NEXUS CONSOLE"
        className="min-h-0"
        bodyClassName="flex min-h-0 flex-1 flex-col"
        actions={
          <IconButton
            name="external"
            label="Open the full activity log"
            onClick={() => go("activity")}
          />
        }
      >
        <Tabs<ConsoleTab>
          value={tab}
          onChange={setTab}
          tabs={[
            { value: "voice", label: "Voice" },
            { value: "ai", label: "AI" },
            { value: "tasks", label: "Tasks" },
            { value: "activity", label: "Activity", badge: activity.length },
            { value: "system", label: "System" },
          ]}
        />
        <div className="min-h-0 flex-1 overflow-y-auto">
          {tab === "voice" && <VoiceTab />}
          {tab === "ai" && <AiTab />}
          {tab === "tasks" && <TasksTab />}
          {tab === "activity" && <ActivityTab />}
          {tab === "system" && <SystemTab />}
        </div>
      </Panel>
    </div>
  );
}

/**
 * NEXUS INTELLIGENCE — what the core is, from the one snapshot it publishes.
 *
 * Everything here comes out of `GET /api/startup`. `storage.ok` is given its own row
 * because a degraded database is the one condition under which every other number on
 * this screen is suspect, and it must not be summarised away into a green dot.
 *
 * Cleaned up: removed "Unavailable tools", "Watching" sections that showed stale/noisy data.
 * Only shows actionable, verified state.
 */
function IntelligencePanel() {
  const { startup, link } = useNexus();

  if (startup.state === "loading") {
    return (
      <Panel title="NEXUS INTELLIGENCE">
        <LoadingState what="the core snapshot" />
      </Panel>
    );
  }

  if (startup.state === "error" && !startup.data) {
    return (
      <Panel title="NEXUS INTELLIGENCE">
        <div className="p-2">
          <ErrorState
            what={startup.error}
            did="Nothing — this is the first thing the window asks for, so there is no earlier snapshot to fall back to."
            next={
              startup.offline
                ? "Start the core with: python -m nexus.api"
                : startup.detail || "Retry, then check the core's own log."
            }
            offline={startup.offline}
            onRetry={startup.reload}
            compact
          />
        </div>
      </Panel>
    );
  }

  const data = startup.data;
  if (!data) return null;
  const health = data.health;
  const stale = startup.state === "error";

  return (
    <Panel
      title="NEXUS INTELLIGENCE"
      meta={stale ? "last known — refresh failed" : undefined}
      actions={
        <IconButton
          name="refresh"
          label="Re-read the core snapshot"
          onClick={startup.reload}
          busy={startup.refreshing}
        />
      }
    >
      <div className="flex flex-col pb-2">
        <DataRow
          label="Core"
          value={health.status === "ok" ? "operational" : "degraded"}
          state={health.status === "ok" ? "ok" : "warn"}
        />
        <DataRow
          label="Storage"
          value={health.storage.ok ? `sqlite · schema ${health.storage.schema_version ?? "?"}` : "unavailable"}
          state={health.storage.ok ? "ok" : "failed"}
          title={`${health.storage.detail} — ${health.storage.path}`}
        />
        {health.startup_error ? (
          <DataRow
            label="At startup"
            value="storage failed — background work is off"
            state="failed"
            title={`${health.startup_error} — the monitors, the memory sweep and the embedding backfill were not started, and only restarting the core retries them.`}
          />
        ) : null}
        <DataRow label="Uptime" value={durationFromSeconds(health.uptime_seconds)} />
        <DataRow label="Platform" value={health.platform} />
        <DataRow label="Display" value={data.session_type || "unknown"} />
        <DataRow label="Process" value={`pid ${health.pid}`} />
        <DataRow
          label="Tools ready"
          value={`${data.tools.available} / ${data.tools.total}`}
          state={data.tools.available === data.tools.total ? "ok" : "warn"}
          title={`${data.tools.available} tools available out of ${data.tools.total} registered`}
        />
        <DataRow
          label="Event link"
          value={link.connected ? `live · seq ${link.lastSeq}` : "no event stream"}
          state={link.connected ? "ok" : "offline"}
        />
      </div>
    </Panel>
  );
}

/**
 * SYSTEM STATUS — the machine, measured (§26, §61).
 *
 * This calls the real `system.status` tool, which reads `/proc` and `/sys`. Two
 * consequences are visible in the markup: the poll is slow (each call writes a
 * `tool_invocations` audit row), and `battery === null` renders a sentence saying this
 * machine has no battery rather than a 0% bar.
 */
function SystemPanel({
  status,
}: {
  status: AsyncResult<{ data: SystemSnapshot; summary: string; verified: boolean | null }>;
}) {
  if (status.state === "loading") {
    return (
      <Panel title="SYSTEM STATUS" className="min-h-0" scroll>
        <LoadingState what="the machine's state" />
      </Panel>
    );
  }

  if (!status.data) {
    return (
      <Panel title="SYSTEM STATUS" className="min-h-0" scroll>
        <div className="p-2">
          <ErrorState
            what={status.error ?? "The system status tool did not return."}
            did="Nothing was read from /proc, so no figure below would be real."
            next={status.offline ? "Start the core, then retry." : status.detail || "Retry."}
            offline={status.offline}
            onRetry={status.reload}
            compact
          />
        </div>
      </Panel>
    );
  }

  const system = status.data.data;
  const stale = status.state === "error";
  const memoryState = system.memory.percent_used > 90 ? "warn" : "ok";

  return (
    <Panel
      title="SYSTEM STATUS"
      meta={stale ? "stale — last read failed" : system.uptime_human}
      className="min-h-0 flex-1"
      scroll
      actions={
        <IconButton
          name="refresh"
          label="Read the machine's state again"
          onClick={status.reload}
          busy={status.refreshing}
        />
      }
    >
      <div className="flex flex-col pb-2">
        <DataRow label="Host" value={system.host} />
        <DataRow label="OS" value={system.os} title={`kernel ${system.kernel}`} />
        <DataRow
          label="CPU"
          value={system.cpu.cores === null ? system.cpu.model : `${system.cpu.cores} cores`}
          title={system.cpu.model}
        />
        <DataRow
          label="Load"
          value={
            system.cpu.load_1m === null
              ? "not reported"
              : [system.cpu.load_1m, system.cpu.load_5m, system.cpu.load_15m]
                  .map((value) => (value === null ? "—" : value.toFixed(2)))
                  .join("  ")
          }
          title="1, 5 and 15 minute load averages from /proc/loadavg."
        />
        <DataRow
          label="Memory"
          value={`${system.memory.available_human} free of ${system.memory.total_human}`}
          state={memoryState}
        />
        <Meter percent={system.memory.percent_used} label="memory used" />

        {system.disks.map((disk) => (
          <div key={disk.mount}>
            <DataRow
              label={disk.mount === "/" ? "Disk /" : `Disk ${disk.mount}`}
              value={`${disk.free_human} free of ${disk.total_human}`}
              state={disk.percent_used > 90 ? "warn" : "ok"}
            />
            <Meter percent={disk.percent_used} label={`${disk.mount} used`} />
          </div>
        ))}

        <DataRow
          label="Battery"
          value={
            system.battery === null
              ? "none — this machine runs on mains only"
              : `${system.battery.percent ?? "?"}% · ${system.battery.status}${
                  system.battery.time_remaining ? ` · ${system.battery.time_remaining} left` : ""
                }`
          }
          state={
            system.battery === null
              ? "idle"
              : system.battery.charging
                ? "ok"
                : (system.battery.percent ?? 100) < 20
                  ? "warn"
                  : "info"
          }
        />

        <DataRow
          label="Network"
          value={system.network.has_default_route ? "default route present" : "no default route"}
          state={system.network.has_default_route ? "ok" : "warn"}
          title={system.network.note}
        />
        {system.network.interfaces.length > 0 && (
          <div className="px-3 pb-1 pt-0.5">
            <ul className="flex flex-wrap gap-1">
              {system.network.interfaces.map((iface) => (
                <li key={iface.name}>
                  <StatusPill state={iface.state === "up" ? "ok" : "idle"} size="sm">
                    {iface.name} {iface.state}
                  </StatusPill>
                </li>
              ))}
            </ul>
          </div>
        )}

        <p className="px-3 pt-1 text-[10px] leading-relaxed text-ink-faint">
          {system.network.note}
        </p>
      </div>
    </Panel>
  );
}

/** A bar whose only input is a real percentage. No animation, no gradient (§53). */
function Meter({ percent, label }: { percent: number; label: string }) {
  const width = Math.max(0, Math.min(100, percent));
  const tone = width > 90 ? "bg-danger" : width > 75 ? "bg-warn" : "bg-accent";
  return (
    <div className="px-3 pb-1.5">
      <div
        role="meter"
        aria-label={label}
        aria-valuenow={Math.round(width)}
        aria-valuemin={0}
        aria-valuemax={100}
        className="h-1 w-full bg-line"
      >
        <div className={`h-full ${tone}`} style={{ width: `${width}%` }} />
      </div>
    </div>
  );
}

/**
 * The VOICE tab: what speech actually does in this shell.
 *
 * Both halves are real and both are stated as settings rather than as capabilities,
 * because that is what they are: transcription happens on every recording, and whether
 * a *spoken* transcript is sent or merely inserted is `voice_enabled`; whether an
 * answer is read aloud is `tts_enabled`. A tab that said "speech works" without saying
 * what is currently switched on would leave the user unable to explain what they just
 * saw happen.
 */
function VoiceTab() {
  const voices = useAsync(() => api.voice.voices(), []);
  const voice = useVoice();
  const recordable =
    typeof window !== "undefined" &&
    typeof window.MediaRecorder !== "undefined" &&
    Boolean(navigator.mediaDevices?.getUserMedia);
  // Hands-free needs a different capability from push-to-talk: Web Audio, to measure
  // loudness locally. Checked separately so a shell that has one and not the other
  // says which is missing.
  const tappable =
    typeof window !== "undefined" &&
    Boolean(
      window.AudioContext ??
        (window as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext,
    ) &&
    Boolean(navigator.mediaDevices?.getUserMedia);
  const { go } = useNexus();

  return (
    <div className="flex flex-col pb-3">
      <Section title="Speech in">
        <div className="flex flex-col gap-1">
          <DataRow
            label="Capture"
            value={recordable ? "available" : "unavailable"}
            state={recordable ? "ok" : "offline"}
            title={
              recordable
                ? "MediaRecorder and getUserMedia are both present."
                : "This shell has no MediaRecorder, so nothing can capture audio."
            }
          />
          <DataRow
            label="Language"
            value={voice.inputLanguage || "auto-detect"}
            state={voice.inputLanguage ? "ok" : "pending"}
            title="The input_language setting, sent with every recording."
          />
          <DataRow
            label="Spoken words"
            value={voice.voiceEnabled ? "sent as instruction" : "inserted for editing"}
            state={voice.voiceEnabled ? "ok" : "idle"}
            title="voice_enabled. On means a transcript goes; off means it waits in the box."
          />
          <DataRow
            label="Hands-free"
            value={
              !voice.wakeWordEnabled
                ? "off"
                : tappable
                  ? `armed — say "${voice.wakeWord}"`
                  : "on, but no Web Audio"
            }
            state={voice.wakeWordEnabled ? (tappable ? "ok" : "failed") : "idle"}
            title="wake_word_enabled. While armed the bar shows what it heard and ignored."
          />
        </div>
      </Section>

      <Section title="Speech out">
        <div className="flex flex-col gap-1">
          {voices.state === "loading" ? (
            <LoadingState what="the voice configuration" />
          ) : voices.data ? (
            <>
              <DataRow
                label="Configured"
                value={voices.data.configured ? "yes" : "no provider key"}
                state={voices.data.configured ? "ok" : "offline"}
              />
              <DataRow label="Default voice" value={voices.data.default || "—"} />
            </>
          ) : (
            <DataRow label="Configured" value={voices.error ?? "unknown"} state="failed" />
          )}
          <DataRow
            label="Answers"
            value={voice.ttsEnabled ? "spoken + printed" : "printed only"}
            state={voice.ttsEnabled ? "ok" : "idle"}
            title="tts_enabled. On means every completed answer is synthesised and played."
          />
          <DataRow
            label="Voice used"
            value={voice.ttsVoice || `${voices.data?.default || "provider default"}`}
            title="tts_voice. Empty means whatever the provider defaults to."
          />
          <div className="px-3 pt-1">
            <Button size="sm" icon="wave" onClick={() => go("voice")}>
              Open Voice
            </Button>
          </div>
        </div>
      </Section>
    </div>
  );
}

/** The AI tab: the turn in flight, or the last one, with nothing invented between. */
function AiTab() {
  const { turn, running, clear } = useTurn();
  if (!turn) {
    return (
      <EmptyState
        icon="core"
        title="Nothing running"
        detail="Type or dictate an instruction below. Routing, reasoning, every tool call and the token cost all appear here as they happen."
      />
    );
  }
  return (
    <div className="p-2 min-h-0 overflow-y-auto">
      <TurnView turn={turn} compact />
      {!running && (
        <div className="mt-2">
          <Button size="sm" tone="ghost" icon="close" onClick={clear}>
            Clear
          </Button>
        </div>
      )}
    </div>
  );
}

/** The TASKS tab: the most recent multi-step work, from `GET /api/tasks`. */
function TasksTab() {
  const { go, link } = useNexus();
  const tasks = useAsync(() => api.tasks.list({ limit: 8 }), []);
  usePoll(tasks.reload, 20_000, link.connected);

  if (tasks.state === "loading") return <LoadingState what="tasks" />;
  if (!tasks.data) {
    return (
      <div className="p-2">
        <ErrorState
          what={tasks.error ?? "The task list did not load."}
          did="Nothing is shown, because an empty list here would read as 'you have no tasks'."
          next={tasks.offline ? "Start the core, then retry." : tasks.detail || "Retry."}
          offline={tasks.offline}
          onRetry={tasks.reload}
          compact
        />
      </div>
    );
  }
  if (tasks.data.tasks.length === 0) {
    return (
      <EmptyState
        icon="tasks"
        title="No tasks yet"
        detail="Ask for something multi-step and it will appear here with its plan."
      />
    );
  }

  return (
    <ul className="space-y-1">
      {tasks.data.tasks.map((task) => (
        <li key={task.id}>
          <ListRow onClick={() => go("tasks", task.id)}>
            <div className="flex items-start gap-2">
              <StateDot state={stateOf(task.status)} pulse={task.status === "running"} />
              <div className="min-w-0 flex-1">
                <p className="truncate text-xs text-ink">{task.objective}</p>
                <p className="mt-0.5 font-mono text-[10px] text-ink-faint">
                  {task.status.replace(/_/g, " ")}
                  {task.step_count !== undefined &&
                    ` · ${task.done_count ?? 0}/${task.step_count} steps`}
                  {` · ${relativeTime(task.updated_at)}`}
                </p>
              </div>
            </div>
          </ListRow>
        </li>
      ))}
    </ul>
  );
}

/**
 * The ACTIVITY tab: the shared feed, which is the database and not the event bus.
 *
 * `activity.record()` does not publish, so there is no event to render directly — bus
 * traffic is only a signal to re-read `/api/activity`. What is on screen is therefore
 * what was actually stored.
 */
function ActivityTab() {
  const { activity, activityError, activityOffline, activityLoading, refreshActivity, go } =
    useNexus();

  if (activityLoading && activity.length === 0) return <LoadingState what="the activity log" />;

  if (activityError && activity.length === 0) {
    return (
      <div className="p-2">
        <ErrorState
          what={activityError}
          did="The log is not shown rather than shown empty — those are different facts."
          next={activityOffline ? "Start the core, then retry." : "Retry."}
          offline={activityOffline}
          onRetry={refreshActivity}
          compact
        />
      </div>
    );
  }

  if (activity.length === 0) {
    return (
      <EmptyState
        icon="activity"
        title="Nothing recorded yet"
        detail="Every tool run, permission decision and model call is written here as it happens."
      />
    );
  }

  return (
    <>
      {activityError && (
        <p className="border-b border-warn/30 bg-warn/8 px-3 py-1 text-[10px] text-warn">
          Showing the last known log — the refresh failed: {activityError}
        </p>
      )}
      <ul>
        {activity.slice(0, 40).map((row) => (
          <li key={row.id}>
            <ListRow onClick={row.task_id ? () => go("tasks", row.task_id) : undefined}>
              <div className="flex items-start gap-2">
                <StateDot state={levelState(row.level)} />
                <div className="min-w-0 flex-1">
                  <p className="break-words text-[11px] leading-relaxed text-ink-soft">
                    {row.summary}
                  </p>
                  <p className="mt-0.5 font-mono text-[10px] text-ink-faint">
                    {row.kind} · {relativeTime(row.created_at)}
                  </p>
                </div>
              </div>
            </ListRow>
          </li>
        ))}
      </ul>
    </>
  );
}

/** The four activity levels, mapped onto the shared state vocabulary. */
function levelState(level: string) {
  return level === "error"
    ? "failed"
    : level === "warning"
      ? "warn"
      : level === "success"
        ? "ok"
        : "info";
}

/** Uptime in seconds, as words. `duration()` takes milliseconds and reads oddly here. */
function durationFromSeconds(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}


/**
 * SYSTEM tab — live machine state from the system.status tool.
 * Clean chatbotui-style console with important info only.
 */
function SystemTab() {
  const { link } = useNexus();
  const status = useAsync(() => api.systemStatus(), []);
  // Poll less frequently than intelligence panel since this is a secondary view
  usePoll(status.reload, 30_000, link.connected);

  if (status.state === "loading") {
    return <LoadingState what="system status" />;
  }

  if (!status.data) {
    return (
      <div className="p-2">
        <ErrorState
          what={status.error ?? "The system status tool did not return."}
          did="Nothing was read from /proc, so no figure below would be real."
          next={status.offline ? "Start the core, then retry." : status.detail || "Retry."}
          offline={status.offline}
          onRetry={status.reload}
          compact
        />
      </div>
    );
  }

  const system = status.data.data;
  const stale = status.state === "error";
  const memoryState = system.memory.percent_used > 90 ? "warn" : "ok";

  return (
    <div className="p-2 space-y-3">
      <Section title="Machine" meta={stale ? "stale" : system.uptime_human}>
        <DataRow label="Host" value={system.host} />
        <DataRow label="OS" value={system.os} title={`kernel ${system.kernel}`} />
        <DataRow
          label="CPU"
          value={system.cpu.cores === null ? system.cpu.model : `${system.cpu.cores} cores`}
          title={system.cpu.model}
        />
        <DataRow
          label="Load"
          value={
            system.cpu.load_1m === null
              ? "not reported"
              : [system.cpu.load_1m, system.cpu.load_5m, system.cpu.load_15m]
                  .map((value) => (value === null ? "—" : value.toFixed(2)))
                  .join("  ")
          }
          title="1, 5 and 15 minute load averages from /proc/loadavg."
        />
        <DataRow
          label="Memory"
          value={`${system.memory.available_human} free of ${system.memory.total_human}`}
          state={memoryState}
        />
        <Meter percent={system.memory.percent_used} label="memory used" />
      </Section>

      <Section title="Disks">
        {system.disks.map((disk) => (
          <div key={disk.mount}>
            <DataRow
              label={disk.mount === "/" ? "Disk /" : `Disk ${disk.mount}`}
              value={`${disk.free_human} free of ${disk.total_human}`}
              state={disk.percent_used > 90 ? "warn" : "ok"}
            />
            <Meter percent={disk.percent_used} label={`${disk.mount} used`} />
          </div>
        ))}
      </Section>

      <Section title="Battery">
        <DataRow
          label="Battery"
          value={
            system.battery === null
              ? "none — this machine runs on mains only"
              : `${system.battery.percent ?? "?"}% · ${system.battery.status}${
                  system.battery.time_remaining ? ` · ${system.battery.time_remaining} left` : ""
                }`
          }
          state={
            system.battery === null
              ? "idle"
              : system.battery.charging
                ? "ok"
                : (system.battery.percent ?? 100) < 20
                  ? "warn"
                  : "info"
          }
        />
      </Section>

      <Section title="Network">
        <DataRow
          label="Default route"
          value={system.network.has_default_route ? "present" : "missing"}
          state={system.network.has_default_route ? "ok" : "warn"}
          title={system.network.note}
        />
        {system.network.interfaces.length > 0 && (
          <div className="px-3 pb-1 pt-0.5">
            <ul className="flex flex-wrap gap-1">
              {system.network.interfaces.map((iface) => (
                <li key={iface.name}>
                  <StatusPill state={iface.state === "up" ? "ok" : "idle"} size="sm">
                    {iface.name} {iface.state}
                  </StatusPill>
                </li>
              ))}
            </ul>
          </div>
        )}
        <p className="px-3 pt-1 text-[10px] leading-relaxed text-ink-faint">
          {system.network.note}
        </p>
      </Section>
    </div>
  );
}




