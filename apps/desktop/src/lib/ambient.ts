/**
 * AMBIENT MONITORING — continuous awareness, spoken (§10, §12, L10).
 *
 * This module runs in the desktop app and watches the machine continuously:
 * - System resources (CPU, memory, disk, battery)
 * - Network activity
 * - Active window/app changes
 * - Time-based patterns (meetings, focus blocks)
 *
 * It feeds observations to the proactive engine via the event bus and speaks
 * significant events through TTS. The core's monitors (screen, system, network)
 * are the source of truth; this is the desktop-side companion that adds the
 * voice layer and cross-source correlation the core cannot see (window focus).
 *
 * Three design decisions:
 *
 * 1. **Polling, not pushing.** The core's monitors already poll and publish
 *    `ambient.*` events. This module polls the same endpoints (or uses the
 *    event bus) and correlates locally. A second writer to the core's tables
 *    would race the monitors; reading is safe.
 *
 * 2. **Speak sparingly, log everything.** Every observation is an activity row
 *    (via the core's monitors). Only the *significant* ones — thresholds crossed,
 *    conditions sustained, patterns matched — are spoken. The thresholds are
 *    configurable via settings so the user decides what "significant" means.
 *
 * 3. **No secret state.** All configuration lives in the core's settings table
 *    (`ambient_*` keys). This module reads them on start and on `SETTINGS_CHANGED`.
 *    A screen that edits them is the Settings screen; this module only consumes.
 */

import { useEffect, useRef, useCallback, useState } from "react";
import { api, type SystemSnapshot, type AmbientEvent, SETTINGS_CHANGED } from "./api";

// Re-export AmbientEvent for consumers
export type { AmbientEvent };

// -----------------------------------------------------------------------------
// Types
// -----------------------------------------------------------------------------

/** Configuration keys, matching the core's `ambient.py` settings. */
export const AMBIENT_SETTINGS = {
  /** Master switch for this entire module. */
  ENABLED: "ambient_enabled",
  /** Poll interval in seconds. */
  POLL_INTERVAL: "ambient_poll_interval",
  /** CPU percent threshold to speak. */
  CPU_THRESHOLD: "ambient_cpu_threshold",
  /** Memory percent threshold to speak. */
  MEMORY_THRESHOLD: "ambient_memory_threshold",
  /** Disk percent threshold to speak. */
  DISK_THRESHOLD: "ambient_disk_threshold",
  /** Battery percent threshold to speak (when not charging). */
  BATTERY_THRESHOLD: "ambient_battery_threshold",
  /** Minutes in same app before speaking. */
  APP_DWELL_MINUTES: "ambient_app_dwell_minutes",
  /** Minutes of focus time before speaking. */
  FOCUS_BLOCK_MINUTES: "ambient_focus_block_minutes",
  /** Whether to speak alerts (vs only logging). */
  SPEAK_ALERTS: "ambient_speak_alerts",
  /** Whether to speak info-level observations. */
  SPEAK_INFO: "ambient_speak_info",
} as const;

/** All ambient setting keys as a union. */
export type AmbientSettingKey = (typeof AMBIENT_SETTINGS)[keyof typeof AMBIENT_SETTINGS];

/** Parsed ambient configuration. */
export type AmbientConfig = {
  enabled: boolean;
  pollIntervalSeconds: number;
  cpuThreshold: number;
  memoryThreshold: number;
  diskThreshold: number;
  batteryThreshold: number;
  appDwellMinutes: number;
  focusBlockMinutes: number;
  speakAlerts: boolean;
  speakInfo: boolean;
};

/** Default configuration. */
export const AMBIENT_DEFAULTS: AmbientConfig = {
  enabled: true,
  pollIntervalSeconds: 30,
  cpuThreshold: 85,
  memoryThreshold: 90,
  diskThreshold: 90,
  batteryThreshold: 15,
  appDwellMinutes: 120,
  focusBlockMinutes: 90,
  speakAlerts: true,
  speakInfo: false,
};

/** Internal state for tracking conditions over time. */
type AmbientState = {
  /** Last snapshot, for delta comparison. */
  lastSnapshot: SystemSnapshot | null;
  /** App name → when we first saw it frontmost (ms since epoch). */
  appFirstSeen: Map<string, number>;
  /** App name → last spoken dwell warning (ms since epoch). */
  appLastSpoken: Map<string, number>;
  /** When the current focus block started (ms since epoch). */
  focusBlockStart: number | null;
  /** Whether we've spoken the focus block warning. */
  focusBlockSpoken: boolean;
  /** Last CPU warning spoken (ms since epoch). */
  lastCpuSpoken: number;
  /** Last memory warning spoken (ms since epoch). */
  lastMemorySpoken: number;
  /** Last disk warning spoken (ms since epoch). */
  lastDiskSpoken: number;
  /** Last battery warning spoken (ms since epoch). */
  lastBatterySpoken: number;
  /** Last network change spoken (ms since epoch). */
  lastNetworkSpoken: number;
};

/** A condition that can be spoken. */
type SpeakableCondition =
  | { kind: "cpu_high"; percent: number }
  | { kind: "memory_high"; percent: number }
  | { kind: "disk_high"; mount: string; percent: number }
  | { kind: "battery_low"; percent: number }
  | { kind: "app_dwell"; app: string; minutes: number }
  | { kind: "focus_block"; minutes: number }
  | { kind: "network_change"; from: string; to: string }
  | { kind: "meeting_soon"; title: string; minutes: number };

// -----------------------------------------------------------------------------
// Configuration
// -----------------------------------------------------------------------------

/** Read ambient settings from the core's settings map, with defaults. */
export function ambientConfigOf(stored: Record<string, unknown>): AmbientConfig {
  const asBool = (key: AmbientSettingKey, fallback: boolean): boolean => {
    const value = stored[key];
    return typeof value === "boolean" ? value : fallback;
  };
  const asNumber = (key: AmbientSettingKey, fallback: number): number => {
    const value = stored[key];
    const parsed = typeof value === "number" ? value : typeof value === "string" ? Number(value) : NaN;
    return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
  };

  return {
    enabled: asBool(AMBIENT_SETTINGS.ENABLED, AMBIENT_DEFAULTS.enabled),
    pollIntervalSeconds: asNumber(AMBIENT_SETTINGS.POLL_INTERVAL, AMBIENT_DEFAULTS.pollIntervalSeconds),
    cpuThreshold: asNumber(AMBIENT_SETTINGS.CPU_THRESHOLD, AMBIENT_DEFAULTS.cpuThreshold),
    memoryThreshold: asNumber(AMBIENT_SETTINGS.MEMORY_THRESHOLD, AMBIENT_DEFAULTS.memoryThreshold),
    diskThreshold: asNumber(AMBIENT_SETTINGS.DISK_THRESHOLD, AMBIENT_DEFAULTS.diskThreshold),
    batteryThreshold: asNumber(AMBIENT_SETTINGS.BATTERY_THRESHOLD, AMBIENT_DEFAULTS.batteryThreshold),
    appDwellMinutes: asNumber(AMBIENT_SETTINGS.APP_DWELL_MINUTES, AMBIENT_DEFAULTS.appDwellMinutes),
    focusBlockMinutes: asNumber(AMBIENT_SETTINGS.FOCUS_BLOCK_MINUTES, AMBIENT_DEFAULTS.focusBlockMinutes),
    speakAlerts: asBool(AMBIENT_SETTINGS.SPEAK_ALERTS, AMBIENT_DEFAULTS.speakAlerts),
    speakInfo: asBool(AMBIENT_SETTINGS.SPEAK_INFO, AMBIENT_DEFAULTS.speakInfo),
  };
}

/** Validate that a config is sane (no zero/negative intervals, thresholds in range). */
export function validateConfig(config: AmbientConfig): { ok: boolean; errors: string[] } {
  const errors: string[] = [];
  if (config.pollIntervalSeconds < 5) errors.push("poll interval must be ≥ 5s");
  if (config.pollIntervalSeconds > 3600) errors.push("poll interval must be ≤ 1h");
  if (config.cpuThreshold < 10 || config.cpuThreshold > 100) errors.push("cpu threshold must be 10–100");
  if (config.memoryThreshold < 10 || config.memoryThreshold > 100) errors.push("memory threshold must be 10–100");
  if (config.diskThreshold < 10 || config.diskThreshold > 100) errors.push("disk threshold must be 10–100");
  if (config.batteryThreshold < 1 || config.batteryThreshold > 100) errors.push("battery threshold must be 1–100");
  if (config.appDwellMinutes < 1 || config.appDwellMinutes > 1440) errors.push("app dwell must be 1–1440 minutes");
  if (config.focusBlockMinutes < 1 || config.focusBlockMinutes > 1440) errors.push("focus block must be 1–1440 minutes");
  return { ok: errors.length === 0, errors };
}

// -----------------------------------------------------------------------------
// Window/App tracking (desktop-only)
// -----------------------------------------------------------------------------

/**
 * Get the currently focused window's app name.
 * In Tauri, this uses the `window` APIs; in web, it returns null.
 * The actual implementation is in `ambient.desktop.ts` which is loaded
 * conditionally in the desktop build.
 *
 * This is the base implementation; the desktop build will override it
 * via the global registry set up by importing ambient.desktop.ts first.
 */
async function getActiveAppBase(): Promise<string | null> {
  if (typeof window === "undefined") return null;
  return null;
}

function onActiveAppChangeBase(_callback: (app: string | null) => void): () => void {
  return () => {};
}

// -----------------------------------------------------------------------------
// System snapshot helpers
// -----------------------------------------------------------------------------

/** Fetch a fresh system snapshot from the core. */
export async function fetchSystemSnapshot(): Promise<SystemSnapshot | null> {
  try {
    const result = await api.systemStatus();
    return result.data;
  } catch {
    return null;
  }
}

/** Check CPU threshold and return a speakable condition if crossed. */
function checkCpu(snapshot: SystemSnapshot, config: AmbientConfig, state: AmbientState): SpeakableCondition | null {
  const load = snapshot.cpu.load_1m;
  if (load === null) return null;
  // load_1m is per-core; threshold is total percent. Approximate: load / cores * 100.
  const cores = snapshot.cpu.cores ?? 1;
  const percent = Math.round((load / cores) * 100);
  if (percent >= config.cpuThreshold) {
    const now = Date.now();
    if (now - state.lastCpuSpoken > 5 * 60 * 1000) { // Don't repeat within 5 min
      return { kind: "cpu_high", percent };
    }
  }
  return null;
}

/** Check memory threshold. */
function checkMemory(snapshot: SystemSnapshot, config: AmbientConfig, state: AmbientState): SpeakableCondition | null {
  const percent = snapshot.memory.percent_used;
  if (percent >= config.memoryThreshold) {
    const now = Date.now();
    if (now - state.lastMemorySpoken > 5 * 60 * 1000) {
      return { kind: "memory_high", percent };
    }
  }
  return null;
}

/** Check disk thresholds. */
function checkDisk(snapshot: SystemSnapshot, config: AmbientConfig, state: AmbientState): SpeakableCondition[] {
  const results: SpeakableCondition[] = [];
  const now = Date.now();
  for (const disk of snapshot.disks) {
    if (disk.percent_used >= config.diskThreshold) {
      if (now - state.lastDiskSpoken > 10 * 60 * 1000) { // Don't repeat within 10 min
        results.push({ kind: "disk_high", mount: disk.mount, percent: disk.percent_used });
      }
    }
  }
  return results;
}

/** Check battery threshold. */
function checkBattery(snapshot: SystemSnapshot, config: AmbientConfig, state: AmbientState): SpeakableCondition | null {
  if (!snapshot.battery) return null;
  if (snapshot.battery.charging) return null;
  if (snapshot.battery.percent === null) return null;
  if (snapshot.battery.percent <= config.batteryThreshold) {
    const now = Date.now();
    if (now - state.lastBatterySpoken > 10 * 60 * 1000) {
      return { kind: "battery_low", percent: snapshot.battery.percent };
    }
  }
  return null;
}

/** Check if the active app has been frontmost for too long. */
function checkAppDwell(activeApp: string | null, config: AmbientConfig, state: AmbientState): SpeakableCondition | null {
  const now = Date.now();
  if (activeApp) {
    let firstSeen = state.appFirstSeen.get(activeApp);
    if (!firstSeen) {
      firstSeen = now;
      state.appFirstSeen.set(activeApp, firstSeen);
    }
    const minutes = (now - firstSeen) / 60_000;
    if (minutes >= config.appDwellMinutes) {
      const lastSpoken = state.appLastSpoken.get(activeApp) ?? 0;
      if (now - lastSpoken > 30 * 60 * 1000) { // Don't repeat within 30 min
        return { kind: "app_dwell", app: activeApp, minutes: Math.round(minutes) };
      }
    }
  } else {
    // No active app — clear tracking for apps we're not in
    state.appFirstSeen.clear();
  }
  return null;
}

/** Check for sustained focus block (no app switches). */
function checkFocusBlock(activeApp: string | null, config: AmbientConfig, state: AmbientState): SpeakableCondition | null {
  const now = Date.now();
  if (activeApp) {
    if (state.focusBlockStart === null) {
      state.focusBlockStart = now;
      state.focusBlockSpoken = false;
    }
    const minutes = (now - state.focusBlockStart) / 60_000;
    if (minutes >= config.focusBlockMinutes && !state.focusBlockSpoken) {
      state.focusBlockSpoken = true;
      return { kind: "focus_block", minutes: Math.round(minutes) };
    }
  } else {
    state.focusBlockStart = null;
    state.focusBlockSpoken = false;
  }
  return null;
}

/** Check for network interface changes. */
function checkNetwork(snapshot: SystemSnapshot, state: AmbientState): SpeakableCondition | null {
  const now = Date.now();
  const activeInterfaces = snapshot.network.interfaces
    .filter((i) => i.state === "up")
    .map((i) => i.name)
    .sort()
    .join(",");
  // In a real implementation, we'd store the last active interface string in state
  // For now, only speak on first detection of a change after startup
  if (state.lastNetworkSpoken === 0 && activeInterfaces) {
    state.lastNetworkSpoken = now;
    return { kind: "network_change", from: "none", to: activeInterfaces };
  }
  return null;
}

/** Check calendar for upcoming meetings (placeholder — needs calendar integration). */
function checkMeetings(_config: AmbientConfig, _state: AmbientState): SpeakableCondition | null {
  // TODO: Integrate with calendar (CalDAV, Google, Outlook) when available.
  // For now, returns null. The shape is here so the type checks.
  return null;
}

// -----------------------------------------------------------------------------
// Speech generation
// -----------------------------------------------------------------------------

/** Turn a speakable condition into a natural language sentence. */
function conditionToSpeech(condition: SpeakableCondition): string {
  switch (condition.kind) {
    case "cpu_high":
      return `Your CPU usage is at ${condition.percent} percent.`;
    case "memory_high":
      return `Memory usage is at ${condition.percent} percent.`;
    case "disk_high":
      return `Disk ${condition.mount} is at ${condition.percent} percent full.`;
    case "battery_low":
      return `Battery is at ${condition.percent} percent.`;
    case "app_dwell":
      return `You've been in ${condition.app} for ${condition.minutes} minutes.`;
    case "focus_block":
      return `You've been focused for ${condition.minutes} minutes. Consider a break.`;
    case "network_change":
      return `Network changed: now on ${condition.to}.`;
    case "meeting_soon":
      return `Meeting "${condition.title}" starting in ${condition.minutes} minutes.`;
  }
}

/** Speak a condition if TTS is enabled and the config allows it. */
async function maybeSpeak(
  condition: SpeakableCondition,
  config: AmbientConfig,
  speakFn: (text: string) => Promise<void>
): Promise<boolean> {
  const isAlert = ["cpu_high", "memory_high", "disk_high", "battery_low", "meeting_soon"].includes(condition.kind);
  const shouldSpeak = isAlert ? config.speakAlerts : config.speakInfo;
  if (!shouldSpeak) return false;
  const text = conditionToSpeech(condition);
  try {
    await speakFn(text);
    return true;
  } catch {
    return false;
  }
}

// -----------------------------------------------------------------------------
// Main ambient monitor class
// -----------------------------------------------------------------------------

/** The ambient monitor, created once and started/stopped with the app. */
export class AmbientMonitor {
  private config: AmbientConfig;
  private state: AmbientState;
  private timer: number | null = null;
  private stopping = false;
  private speakFn: (text: string) => Promise<void>;
  private onObservation: (event: AmbientEvent) => void;
  private unsubscribeAppChange: (() => void) | null = null;

  constructor(
    initialConfig: AmbientConfig,
    speakFn: (text: string) => Promise<void>,
    onObservation: (event: AmbientEvent) => void
  ) {
    this.config = initialConfig;
    this.speakFn = speakFn;
    this.onObservation = onObservation;
    this.state = {
      lastSnapshot: null,
      appFirstSeen: new Map(),
      appLastSpoken: new Map(),
      focusBlockStart: null,
      focusBlockSpoken: false,
      lastCpuSpoken: 0,
      lastMemorySpoken: 0,
      lastDiskSpoken: 0,
      lastBatterySpoken: 0,
      lastNetworkSpoken: 0,
    };
  }

  /** Update configuration (e.g., from settings change). */
  setConfig(config: AmbientConfig): void {
    const wasEnabled = this.config.enabled;
    this.config = config;
    if (wasEnabled && !config.enabled) this.stop();
    if (!wasEnabled && config.enabled) this.start();
    if (config.enabled) this.reschedule();
  }

  /** Start the monitoring loop. */
  start(): void {
    if (this.stopping) return;
    this.stopping = false;
    this.tick();
    // Subscribe to active app changes (desktop only)
    this.unsubscribeAppChange = onActiveAppChange((app) => this.handleAppChange(app));
  }

  /** Stop the monitoring loop. */
  stop(): void {
    this.stopping = true;
    if (this.timer !== null) {
      window.clearTimeout(this.timer);
      this.timer = null;
    }
    if (this.unsubscribeAppChange) {
      this.unsubscribeAppChange();
      this.unsubscribeAppChange = null;
    }
  }

  /** Handle active app change. */
  private async handleAppChange(activeApp: string | null): Promise<void> {
    if (!this.config.enabled) return;
    // Reset focus block tracking on app change
    this.state.focusBlockStart = null;
    this.state.focusBlockSpoken = false;
    // Check app dwell for the new app
    const condition = checkAppDwell(activeApp, this.config, this.state);
    if (condition) {
      await this.emitAndSpeak(condition);
    }
  }

  /** One monitoring tick. */
  private async tick(): Promise<void> {
    if (this.stopping || !this.config.enabled) return;

    try {
      const snapshot = await fetchSystemSnapshot();
      if (snapshot) {
        await this.evaluateSnapshot(snapshot);
        this.state.lastSnapshot = snapshot;
      }
    } catch (error) {
      console.warn("[ambient] tick failed:", error);
    }

    if (!this.stopping) {
      this.reschedule();
    }
  }

  /** Schedule the next tick. */
  private reschedule(): void {
    if (this.timer !== null) window.clearTimeout(this.timer);
    this.timer = window.setTimeout(
      () => this.tick(),
      this.config.pollIntervalSeconds * 1000
    );
  }

  /** Evaluate a snapshot and emit/speak any conditions. */
  private async evaluateSnapshot(snapshot: SystemSnapshot): Promise<void> {
    const conditions: SpeakableCondition[] = [];

    // System resource checks
    const cpu = checkCpu(snapshot, this.config, this.state);
    if (cpu) conditions.push(cpu);

    const memory = checkMemory(snapshot, this.config, this.state);
    if (memory) conditions.push(memory);

    const disks = checkDisk(snapshot, this.config, this.state);
    conditions.push(...disks);

    const battery = checkBattery(snapshot, this.config, this.state);
    if (battery) conditions.push(battery);

    // Network check
    const network = checkNetwork(snapshot, this.state);
    if (network) conditions.push(network);

    // App dwell (uses current active app)
    const activeApp = await getActiveApp();
    const dwell = checkAppDwell(activeApp, this.config, this.state);
    if (dwell) conditions.push(dwell);

    // Focus block
    const focus = checkFocusBlock(activeApp, this.config, this.state);
    if (focus) conditions.push(focus);

    // Meetings (placeholder)
    const meeting = checkMeetings(this.config, this.state);
    if (meeting) conditions.push(meeting);

    // Emit and speak each condition
    for (const condition of conditions) {
      await this.emitAndSpeak(condition);
    }
  }

  /** Emit an observation to the core and optionally speak it. */
  private async emitAndSpeak(condition: SpeakableCondition): Promise<void> {
    const now = Date.now();
    const isoNow = new Date(now).toISOString();

    // Build ambient event
    const event: AmbientEvent = {
      id: crypto.randomUUID(),
      monitor: "ambient",
      kind: condition.kind,
      summary: conditionToSpeech(condition),
      severity: ["cpu_high", "memory_high", "disk_high", "battery_low", "meeting_soon"].includes(condition.kind)
        ? "alert"
        : "notice",
      observed_at: isoNow,
      data: condition,
    };

    // Emit to observers (the core will also receive this via the event bus if we publish)
    this.onObservation(event);

    // Update last-spoken timestamps
    switch (condition.kind) {
      case "cpu_high":
        this.state.lastCpuSpoken = now;
        break;
      case "memory_high":
        this.state.lastMemorySpoken = now;
        break;
      case "disk_high":
        this.state.lastDiskSpoken = now;
        break;
      case "battery_low":
        this.state.lastBatterySpoken = now;
        break;
      case "app_dwell":
        this.state.appLastSpoken.set(condition.app, now);
        break;
      case "network_change":
        this.state.lastNetworkSpoken = now;
        break;
    }

    // Speak if enabled
    await maybeSpeak(condition, this.config, this.speakFn);
  }
}

// -----------------------------------------------------------------------------
// React integration hook
// -----------------------------------------------------------------------------

/**
 * Hook to run the ambient monitor in a React app.
 * Returns the current config and a function to update it.
 */
export function useAmbientMonitor(
  getSpeakFn: () => (text: string) => Promise<void>,
  onObservation: (event: AmbientEvent) => void
): { config: AmbientConfig; setConfig: (config: AmbientConfig) => void; reload: () => void } {
  const monitorRef = useRef<AmbientMonitor | null>(null);
  const configRef = useRef<AmbientConfig>(AMBIENT_DEFAULTS);
  const [, forceUpdate] = useState(0);

  // Initialize monitor on mount
  useEffect(() => {
    const speakFn = getSpeakFn();
    const monitor = new AmbientMonitor(configRef.current, speakFn, onObservation);
    monitorRef.current = monitor;

    // Load initial config from settings
    api.settings.get().then((result) => {
      const config = ambientConfigOf(result.settings ?? {});
      configRef.current = config;
      monitor.setConfig(config);
      forceUpdate((n) => n + 1);
    }).catch(() => {
      // Use defaults
      monitor.setConfig(AMBIENT_DEFAULTS);
    });

    // Listen for settings changes
    const onSettingsChange = () => {
      api.settings.get().then((result) => {
        const config = ambientConfigOf(result.settings ?? {});
        configRef.current = config;
        monitor.setConfig(config);
        forceUpdate((n) => n + 1);
      });
    };
    window.addEventListener(SETTINGS_CHANGED, onSettingsChange);

    return () => {
      monitor.stop();
      window.removeEventListener(SETTINGS_CHANGED, onSettingsChange);
    };
  }, [getSpeakFn, onObservation]);

  const setConfig = useCallback((config: AmbientConfig) => {
    configRef.current = config;
    monitorRef.current?.setConfig(config);
    forceUpdate((n) => n + 1);
  }, []);

  const reload = useCallback(() => {
    monitorRef.current?.stop();
    const speakFn = getSpeakFn();
    const monitor = new AmbientMonitor(configRef.current, speakFn, onObservation);
    monitorRef.current = monitor;
    api.settings.get().then((result) => {
      const config = ambientConfigOf(result.settings ?? {});
      configRef.current = config;
      monitor.setConfig(config);
    });
  }, [getSpeakFn, onObservation]);

  return { config: configRef.current, setConfig, reload };
}

// -----------------------------------------------------------------------------
// Desktop-specific implementation (loaded only in desktop build)
// -----------------------------------------------------------------------------

/**
 * This section is replaced at build time in the desktop build.
 * In the web build, the base implementations above (returning null/no-op) are used.
 *
 * The desktop implementation uses Tauri's window APIs to track the active app.
 * See `ambient.desktop.ts` for the actual implementation.
 */

// Export a marker so importers can detect if they're in desktop or web
export const IS_DESKTOP = typeof window !== "undefined" && (window as any).__TAURI__ !== undefined;

// Getter functions that use the desktop override when available
let getActiveAppOverride: (() => Promise<string | null>) | null = null;
let onActiveAppChangeOverride: ((callback: (app: string | null) => void) => () => void) | null = null;

/** Register desktop overrides (called by ambient.desktop.ts when loaded). */
export function registerAmbientDesktopOverrides(
  getActiveApp: () => Promise<string | null>,
  onActiveAppChange: (callback: (app: string | null) => void) => () => void
): void {
  getActiveAppOverride = getActiveApp;
  onActiveAppChangeOverride = onActiveAppChange;
}

/** Get the currently focused window's app name. */
export async function getActiveApp(): Promise<string | null> {
  if (getActiveAppOverride) return getActiveAppOverride();
  return getActiveAppBase();
}

/** Subscribe to active window changes. Returns unsubscribe. */
export function onActiveAppChange(callback: (app: string | null) => void): () => void {
  if (onActiveAppChangeOverride) return onActiveAppChangeOverride(callback);
  return onActiveAppChangeBase(callback);
}