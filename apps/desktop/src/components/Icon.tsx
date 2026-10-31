/**
 * The icon set (§51).
 *
 * "Use a consistent technical line-icon system. Icons must be minimal, readable,
 * consistent, accessible. Do not use random emoji as primary navigation icons."
 *
 * Hand-drawn on one 24x24 grid with one stroke weight rather than pulled from a
 * package: the whole set is under 40 glyphs, an icon library would be the largest
 * dependency in the app for that, and §69 says not to add frameworks the product
 * does not need.
 *
 * Accessibility (§55): an icon on its own is invisible to a screen reader. This
 * component is `aria-hidden` by default — because in this UI icons sit beside a
 * text label almost everywhere, and announcing both reads the name twice. When an
 * icon *is* the only content of a control, pass `label` and it becomes an
 * `img`-role element with an accessible name. A control with neither is a bug the
 * `IconButton` in primitives.tsx makes impossible.
 */

export type IconName =
  | "core"
  | "memory"
  | "skills"
  | "soul"
  | "settings"
  | "tasks"
  | "projects"
  | "devices"
  | "network"
  | "vision"
  | "automations"
  | "headlines"
  | "brief"
  | "activity"
  | "permissions"
  | "conversations"
  | "mic"
  | "mic-off"
  | "send"
  | "attach"
  | "camera"
  | "stop"
  | "check"
  | "close"
  | "alert"
  | "info"
  | "chevron-right"
  | "chevron-down"
  | "search"
  | "refresh"
  | "trash"
  | "pin"
  | "plus"
  | "edit"
  | "play"
  | "pause"
  | "external"
  | "lock"
  | "unlock"
  | "cpu"
  | "database"
  | "wave"
  | "volume"
  | "spinner"
  | "verified"
  | "unverified"
  | "monitor"
  | "monitor-off"
  | "sparkles"
  | "eye";

/** Stroked paths, drawn on a 24x24 box. `fill` is never used — one visual language. */
const PATHS: Record<IconName, string> = {
  // A ring with an inner core: the sphere, reduced to two strokes.
  core: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 5.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7Z",
  memory: "M4 6h11l5 5v7H4V6Zm0 5h16M9 6v5m5-5v5",
  skills: "M12 3l2.6 5.6 6 .8-4.4 4.2 1.1 6-5.3-3-5.3 3 1.1-6L3.4 9.4l6-.8L12 3Z",
  soul: "M12 20s-7-4.3-7-9a4 4 0 0 1 7-2.6A4 4 0 0 1 19 11c0 4.7-7 9-7 9Z",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM4 12h2m12 0h2M12 4v2m0 12v2M6.3 6.3l1.4 1.4m8.6 8.6 1.4 1.4M17.7 6.3l-1.4 1.4M7.7 16.3l-1.4 1.4",
  tasks: "M4 7h3l1.5 2L11 5m0 2h9M4 14h3l1.5 2L11 12m0 2h9",
  projects: "M4 7a1 1 0 0 1 1-1h4l2 2h8a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7Z",
  devices: "M3 6h13v9H3V6Zm0 12h13M18 9h3v10h-3V9Z",
  network: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm-9 9h18M12 3c2.5 2.4 3.8 5.4 3.8 9S14.5 18.6 12 21c-2.5-2.4-3.8-5.4-3.8-9S9.5 5.4 12 3Z",
  vision: "M2.5 12S6 6.5 12 6.5 21.5 12 21.5 12S18 17.5 12 17.5 2.5 12 2.5 12Zm9.5 2.6a2.6 2.6 0 1 0 0-5.2 2.6 2.6 0 0 0 0 5.2Z",
  automations: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 4.5V12l3.2 2",
  headlines: "M4 5h16v14H4V5Zm2 3h7m-7 3h7m-7 3h5m5-6h3v3h-3V8Z",
  brief: "M6 4h9l3 3v13H6V4Zm3 8h6m-6 4h6m-6-8h3",
  activity: "M3 12h3l2.5-6 3 12 2.5-6h3.5",
  permissions: "M12 3l7 3v6c0 4.2-2.9 7.6-7 9-4.1-1.4-7-4.8-7-9V6l7-3Zm0 6v3m0 3h.01",
  conversations: "M4 5h12v9H8l-4 3V5Zm5 12h7l4 3V9h-3",
  mic: "M12 4a3 3 0 0 1 3 3v4a3 3 0 0 1-6 0V7a3 3 0 0 1 3-3Zm-5 7a5 5 0 0 0 10 0M12 16v4m-3 0h6",
  "mic-off": "M9 7a3 3 0 0 1 6 0v3M9 11v.5a3 3 0 0 0 4.6 2.5M7 11a5 5 0 0 0 7.6 4.3M12 18v2m-3 0h6M4 4l16 16",
  send: "M4 12l16-8-6 16-2.5-6L4 12Z",
  attach: "M15 7l-6.4 6.4a3 3 0 0 0 4.2 4.2L19 12a5 5 0 0 0-7-7l-6 6a7 7 0 0 0 9.9 9.9",
  camera: "M4 8h3l1.5-2h7L17 8h3v11H4V8Zm8 8.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z",
  stop: "M7 7h10v10H7V7Z",
  check: "M5 13l4.5 4.5L19 7",
  close: "M6 6l12 12M18 6 6 18",
  alert: "M12 4l9 16H3l9-16Zm0 5v5m0 3h.01",
  info: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 4.5h.01M12 11v6",
  "chevron-right": "M9 5l7 7-7 7",
  "chevron-down": "M5 9l7 7 7-7",
  search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14Zm5 12 4 4",
  refresh: "M20 6v5h-5M4 18v-5h5m-4.5-2A8 8 0 0 1 18 7.5M19.5 13A8 8 0 0 1 6 16.5",
  trash: "M5 7h14M9 7V5h6v2m-7 0 1 13h6l1-13M11 11v6m2-6v6",
  pin: "M12 3v7m0 0-4 4h8l-4-4Zm0 4v14",
  plus: "M12 5v14M5 12h14",
  edit: "M4 20h4L20 8l-4-4L4 16v4Zm11-13 3 3",
  play: "M8 5l11 7-11 7V5Z",
  pause: "M9 5v14M15 5v14",
  external: "M14 4h6v6m0-6L11 13M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5",
  lock: "M6 11h12v9H6v-9Zm3 0V8a3 3 0 0 1 6 0v3m-3 4v2",
  unlock: "M6 11h12v9H6v-9Zm3 0V8a3 3 0 0 1 5.8-1M12 15v2",
  cpu: "M8 8h8v8H8V8ZM5 9V6h3M19 9V6h-3M5 15v3h3m11-3v3h-3",
  database: "M12 3c4.4 0 8 1.1 8 2.5S16.4 8 12 8 4 6.9 4 5.5 7.6 3 12 3Zm8 2.5v13c0 1.4-3.6 2.5-8 2.5s-8-1.1-8-2.5v-13m16 6.5c0 1.4-3.6 2.5-8 2.5s-8-1.1-8-2.5",
  wave: "M4 12h2m2-4v8m3-11v14m3-10v6m3-8v10m3-6v2",
  volume: "M4 10h3l4-3.5v11L7 14H4v-4Zm11-1a4 4 0 0 1 0 6m2.5-8.5a7 7 0 0 1 0 11",
  // Three quarters of a ring: the rotation comes from the caller's class.
  spinner: "M12 3a9 9 0 0 1 9 9",
  verified: "M12 3l7 3v6c0 4.2-2.9 7.6-7 9-4.1-1.4-7-4.8-7-9V6l7-3Zm-3 8.5 2.5 2.5L16 9.5",
  unverified: "M12 3l7 3v6c0 4.2-2.9 7.6-7 9-4.1-1.4-7-4.8-7-9V6l7-3ZM9.5 9.5l5 5m0-5-5 5",
  // A screen with a stand
  monitor: "M4 3h16a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Zm0 2v13h16V5H4ZM9 20h6v2H9v-2Z",
  // Screen with slash
  "monitor-off": "M4 3h16a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Zm0 2v13h16V5H4ZM9 20h6v2H9v-2ZM2 2l20 20",
  // Sparkles for proactive AI
  sparkles: "M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z",
  // Eye icon
  eye: "M2.5 12S6 6.5 12 6.5 21.5 12 21.5 12S18 17.5 12 17.5 2.5 12 2.5 12Zm9.5 2.6a2.6 2.6 0 1 0 0-5.2 2.6 2.6 0 0 0 0 5.2Z",
};

export type IconProps = {
  name: IconName;
  /** Pixel size of the square box. 16 for inline, 18 in controls, 22 in nav. */
  size?: number;
  className?: string;
  /**
   * Accessible name. Set this only when the icon is the sole content of a control —
   * otherwise the adjacent text already names it and this would duplicate.
   */
  label?: string;
  strokeWidth?: number;
};

export function Icon({ name, size = 18, className = "", label, strokeWidth = 1.5 }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden={label ? undefined : true}
      role={label ? "img" : undefined}
      aria-label={label}
      focusable="false"
    >
      <path d={PATHS[name]} />
    </svg>
  );
}

/**
 * A spinner that says what it is waiting for.
 *
 * `aria-live="polite"` rather than silence: §55 requires error and status
 * announcements, and a screen-reader user given a spinning shape with no name
 * cannot tell a loading screen from a broken one.
 */
export function Spinner({ label = "Working", size = 16 }: { label?: string; size?: number }) {
  return (
    <span className="inline-flex items-center gap-2" role="status" aria-live="polite">
      <Icon name="spinner" size={size} className="animate-spin" />
      <span className="sr-only">{label}</span>
    </span>
  );
}
