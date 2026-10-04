/**
 * The voice sphere (§11, §64, §65, §94).
 *
 * Visual priority 1: "The NEXUS Voice Sphere must be the most visually dominant
 * element." It is also the primary state signature — the one place in the app where
 * the user should be able to tell what NEXUS is doing from across the room.
 *
 * Built from SVG and CSS transforms, not a canvas or a 3D library, and that is a
 * requirement rather than a preference. §53: "Avoid heavy 3D rendering,
 * high-frequency particle systems, excessive blur and shadow layers, constant
 * animation, unnecessary canvas rendering." A WebGL sphere on an idle desktop
 * assistant burns a GPU all day to show a state that changes twice a minute.
 *
 * Three honesty rules, each of which the pretty version of this component breaks:
 *
 * 1. **Every state here is a real state.** The eight below map onto what the core
 *    actually reports; there is no "thinking" animation that plays on a timer.
 * 2. **Amplitude is real or absent.** `level` renders as ring displacement only
 *    when the voice subsystem is genuinely reporting input energy. `null` means the
 *    ring holds still — §61 forbids a fake waveform, and a bouncing ring on a dead
 *    microphone is exactly that.
 * 3. **Motion stops when nothing is happening.** READY and OFFLINE are static. A
 *    perpetually pulsing orb tells the user nothing, and §71 warns specifically
 *    about an interface that looks busy forever.
 */

import { useMemo } from "react";

import { Icon } from "./Icon";

export type SphereState =
  | "ready"
  | "listening"
  | "thinking"
  | "speaking"
  | "executing"
  | "awaiting-approval"
  | "error"
  | "offline";

/**
 * The label, colour and motion for each state.
 *
 * `spin` is seconds per revolution of the outer ring; 0 means it does not turn.
 * `pulse` drives the core's breathing. Both are zero for the two resting states.
 */
const LOOK: Record<
  SphereState,
  { label: string; hint: string; colour: string; spin: number; pulse: boolean; ring: number }
> = {
  ready: {
    label: "NEXUS READY",
    hint: "Idle and listening for activation",
    colour: "var(--accent)",
    spin: 0,
    pulse: false,
    ring: 0.55,
  },
  listening: {
    label: "LISTENING",
    hint: "Microphone open, capturing speech",
    colour: "var(--accent-bright)",
    spin: 14,
    pulse: true,
    ring: 1,
  },
  thinking: {
    label: "THINKING",
    hint: "Model is reasoning about the request",
    colour: "var(--color-info)",
    spin: 6,
    pulse: true,
    ring: 0.8,
  },
  speaking: {
    label: "SPEAKING",
    hint: "Reading the response aloud",
    colour: "var(--accent)",
    spin: 10,
    pulse: true,
    ring: 0.9,
  },
  executing: {
    label: "EXECUTING",
    hint: "Running a tool on this machine",
    colour: "var(--color-ok)",
    spin: 3.5,
    pulse: true,
    ring: 0.85,
  },
  "awaiting-approval": {
    label: "ACTION REQUIRES APPROVAL",
    hint: "Waiting for your decision before it proceeds",
    colour: "var(--color-warn)",
    spin: 0,
    pulse: true,
    ring: 1,
  },
  error: {
    label: "SYSTEM ERROR",
    hint: "The last operation failed",
    colour: "var(--color-danger)",
    spin: 0,
    pulse: false,
    ring: 0.7,
  },
  offline: {
    label: "VOICE OFFLINE",
    hint: "No connection to the NEXUS core",
    colour: "var(--color-ink-faint)",
    spin: 0,
    pulse: false,
    ring: 0.4,
  },
};

/** The 12 markers on the outer ring. Fixed positions — not particles. */
const TICKS = Array.from({ length: 12 }, (_, index) => index * 30);

export type VoiceSphereProps = {
  state: SphereState;
  /**
   * Input energy, 0..1, from the voice subsystem. `null` when nothing is
   * measuring — the ring then holds its resting radius rather than inventing one.
   */
  level?: number | null;
  /** Sub-label under the state: the model in use, the tool running, the error. */
  detail?: string;
  size?: number;
  /** Press to start or stop listening. Omitted when voice is unavailable. */
  onActivate?: () => void;
  activateLabel?: string;
};

export function VoiceSphere({
  state,
  level = null,
  detail,
  size = 260,
  onActivate,
  activateLabel,
}: VoiceSphereProps) {
  const look = LOOK[state];

  // Amplitude only displaces the ring when a real number arrived. Clamped, because
  // a provider reporting 1.4 should not blow the ring outside the frame.
  const amplitude = level === null ? 0 : Math.max(0, Math.min(1, level));
  const ringScale = 1 + amplitude * 0.06;

  const ticks = useMemo(
    () =>
      TICKS.map((angle) => {
        const radians = (angle * Math.PI) / 180;
        return {
          angle,
          x1: 50 + Math.cos(radians) * 44,
          y1: 50 + Math.sin(radians) * 44,
          x2: 50 + Math.cos(radians) * 47,
          y2: 50 + Math.sin(radians) * 47,
        };
      }),
    [],
  );

  const core = (
    <div
      className="relative"
      style={{ width: size, height: size, ["--sphere" as string]: look.colour }}
    >
      {/* Corner brackets: the technical framing from the references (§47), four
          absolutely-positioned L shapes rather than a decorative border. */}
      {(
        [
          ["top-0 left-0", "border-t border-l"],
          ["top-0 right-0", "border-t border-r"],
          ["bottom-0 left-0", "border-b border-l"],
          ["bottom-0 right-0", "border-b border-r"],
        ] as const
      ).map(([position, edges]) => (
        <span
          key={position}
          aria-hidden
          className={`absolute h-4 w-4 ${position} ${edges}`}
          style={{ borderColor: look.colour, opacity: 0.5 }}
        />
      ))}

      <svg
        viewBox="0 0 100 100"
        className="h-full w-full"
        style={{ transform: `scale(${ringScale})`, transition: "transform 90ms linear" }}
        aria-hidden
      >
        <defs>
          <radialGradient id="nexus-core" cx="42%" cy="38%" r="62%">
            <stop offset="0%" stopColor={look.colour} stopOpacity="0.85" />
            <stop offset="42%" stopColor={look.colour} stopOpacity="0.22" />
            <stop offset="100%" stopColor="var(--color-void)" stopOpacity="0.95" />
          </radialGradient>
          <radialGradient id="nexus-halo" cx="50%" cy="50%" r="50%">
            <stop offset="60%" stopColor={look.colour} stopOpacity="0" />
            <stop offset="100%" stopColor={look.colour} stopOpacity={0.1 * look.ring} />
          </radialGradient>
        </defs>

        {/* Halo. One gradient, not a stack of blurred shadows (§53). */}
        <circle cx="50" cy="50" r="49" fill="url(#nexus-halo)" />

        {/* Outer ring plus its ticks, turning as one group. Speed encodes the state:
            executing is visibly faster than thinking. */}
        <g
          style={
            look.spin
              ? { animation: `nexus-spin ${look.spin}s linear infinite`, transformOrigin: "50px 50px" }
              : undefined
          }
        >
          <circle
            cx="50"
            cy="50"
            r="46"
            fill="none"
            stroke={look.colour}
            strokeWidth="0.4"
            strokeOpacity={0.35}
            strokeDasharray="1 5"
          />
          {ticks.map((tick) => (
            <line
              key={tick.angle}
              x1={tick.x1}
              y1={tick.y1}
              x2={tick.x2}
              y2={tick.y2}
              stroke={look.colour}
              strokeWidth="0.5"
              strokeOpacity={tick.angle % 90 === 0 ? 0.75 : 0.3}
            />
          ))}
        </g>

        {/* An arc that turns the other way, so the two rings read as a mechanism
            rather than one spinning image. */}
        <g
          style={
            look.spin
              ? {
                  animation: `nexus-spin-reverse ${look.spin * 2.2}s linear infinite`,
                  transformOrigin: "50px 50px",
                }
              : undefined
          }
        >
          <circle
            cx="50"
            cy="50"
            r="39"
            fill="none"
            stroke={look.colour}
            strokeWidth="0.6"
            strokeOpacity={0.45}
            strokeDasharray="28 90"
            strokeLinecap="round"
          />
        </g>

        {/* The core. `pulse` is a slow scale, only while something is happening. */}
        <g
          style={
            look.pulse
              ? { animation: "nexus-breathe 2.6s ease-in-out infinite", transformOrigin: "50px 50px" }
              : undefined
          }
        >
          <circle cx="50" cy="50" r="31" fill="url(#nexus-core)" />
          <circle
            cx="50"
            cy="50"
            r="31"
            fill="none"
            stroke={look.colour}
            strokeWidth="0.5"
            strokeOpacity={0.6}
          />
          {/* Latitude lines: the sphere reads as a sphere from two ellipses, which
              is the whole 3D budget this component spends. */}
          <ellipse
            cx="50"
            cy="50"
            rx="31"
            ry="10"
            fill="none"
            stroke={look.colour}
            strokeWidth="0.3"
            strokeOpacity={0.3}
          />
          <ellipse
            cx="50"
            cy="50"
            rx="12"
            ry="31"
            fill="none"
            stroke={look.colour}
            strokeWidth="0.3"
            strokeOpacity={0.2}
          />
        </g>

        {/* Amplitude ring: present only when a real level is being reported. Its
            radius is the measurement, so a silent room shows a still ring. */}
        {level !== null && (
          <circle
            cx="50"
            cy="50"
            r={33 + amplitude * 9}
            fill="none"
            stroke={look.colour}
            strokeWidth="0.35"
            strokeOpacity={0.25 + amplitude * 0.5}
            style={{ transition: "r 90ms linear, stroke-opacity 90ms linear" }}
          />
        )}
      </svg>

      {/* The state icon at dead centre, so the state is legible without reading. */}
      <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
        <Icon
          name={
            state === "offline"
              ? "mic-off"
              : state === "error"
                ? "alert"
                : state === "awaiting-approval"
                  ? "lock"
                  : state === "executing"
                    ? "cpu"
                    : state === "speaking"
                      ? "wave"
                      : state === "thinking"
                        ? "core"
                        : "mic"
          }
          size={Math.round(size * 0.11)}
          className="opacity-80"
          strokeWidth={1.2}
        />
      </div>
    </div>
  );

  return (
    <div className="flex flex-col items-center gap-3">
      {onActivate ? (
        <button
          type="button"
          onClick={onActivate}
          aria-label={activateLabel ?? "Toggle voice input"}
          className="chamfer-all cursor-pointer transition-transform duration-150 hover:scale-[1.015] focus-visible:outline-2"
        >
          {core}
        </button>
      ) : (
        core
      )}

      {/* The label is the state — text, not colour alone (§92, §55). `aria-live`
          so a screen reader hears the transition rather than missing it. */}
      <div className="flex flex-col items-center gap-1 text-center" role="status" aria-live="polite">
        <p
          className="font-mono text-[13px] font-medium uppercase tracking-[0.22em]"
          style={{ color: look.colour }}
        >
          {look.label}
        </p>
        <p className="max-w-[22rem] text-[11px] text-ink-dim text-balance">{detail || look.hint}</p>
      </div>
    </div>
  );
}

/**
 * Map what the core reports onto a sphere state.
 *
 * Order matters and encodes priority: a pending permission outranks a running turn,
 * because the permission is the thing blocking it, and being disconnected outranks
 * everything, because nothing else on screen can be trusted while it is true.
 */
export function sphereStateFrom(input: {
  connected: boolean;
  pendingApproval: boolean;
  executing: boolean;
  thinking: boolean;
  listening: boolean;
  speaking: boolean;
  failed: boolean;
}): SphereState {
  if (!input.connected) return "offline";
  if (input.pendingApproval) return "awaiting-approval";
  if (input.executing) return "executing";
  if (input.speaking) return "speaking";
  if (input.listening) return "listening";
  if (input.thinking) return "thinking";
  if (input.failed) return "error";
  return "ready";
}
