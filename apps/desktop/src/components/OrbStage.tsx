/**
 * The holographic stage around the voice sphere (§94).
 *
 * The reference centres its interface on three stacked layers: an ambient
 * backlight, a projector beam rising from the floor, and foreground particles
 * drifting upward. NEXUS had only the sphere. This adds the stage -- the light
 * around and beneath it -- and deliberately not the 3D character that the
 * reference puts in the middle of it.
 *
 * The backlight and the beam are pure CSS (`.stage-glow`, `.stage-beam` in
 * styles.css) and are static: painted once, never animated, no filter. This
 * component owns only the third layer, the particles, because that is the one
 * that has to be driven from real state rather than drawn.
 *
 * ## The honesty rule this component exists to obey
 *
 * The reference's particles are excited by live speech volume. NEXUS has no
 * such number: `state/voice.tsx` reports `listening` and `speaking` as
 * booleans and nothing measures amplitude. So the choice is between inventing a
 * volume curve -- which is the fake waveform §61 forbids, just drawn as dust
 * instead of a bar -- and treating the two booleans as the whole truth.
 *
 * The two booleans are treated as the whole truth:
 *
 * - Particles rise **only** while `active` is true. Idle is not a slow drift; it
 *   is nothing, and the animation loop is stopped rather than left spinning at
 *   zero speed. A desktop assistant that animates when nothing is happening is
 *   the §71 failure, and it also burns a core all day.
 * - `level` is honoured when something genuinely measures it, and its absence
 *   changes the *speed* of the rise and nothing else. There is no branch that
 *   makes particles appear from a number that was not measured.
 * - `prefers-reduced-motion` removes the layer entirely. Not slowed -- absent.
 *
 * The canvas is DPR-aware and sized to its container, because a canvas drawn at
 * CSS pixels on a 2x display looks like a smear and the reference's look depends
 * on the dust reading as fine points of light.
 */

import { useEffect, useRef } from "react";

/** Below this, a rise reads as flicker rather than drifing dust. */
const MIN_PARTICLES = 10;
/** Above this it stops reading as dust and starts reading as a starfield. */
const MAX_PARTICLES = 48;
const TARGET_SPACING = 28;
const MAX_DPR = 2;

type Particle = {
  x: number;
  y: number;
  speed: number;
  size: number;
  opacity: number;
};

export type OrbStageProps = {
  /**
   * Whether anything real is happening: the microphone is open, or audio is
   * actually being played. False stops the loop dead.
   */
  active: boolean;
  /**
   * Measured input energy, 0..1, or `null` when nothing measures it -- which is
   * the case today. Non-null only changes how fast the dust rises.
   */
  level?: number | null;
  className?: string;
  children: React.ReactNode;
};

export function OrbStage({ active, level = null, className = "", children }: OrbStageProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const particlesRef = useRef<Particle[]>([]);
  /** Smoothed brightness, so a jittery level does not strobe the whole field. */
  const energyRef = useRef(0);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !active) return;

    // Absent under reduced motion, not merely slow. Checked here rather than in
    // CSS because a canvas loop is not something a media query can switch off.
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    // Read the accent off the DOM so the dust follows the theme without this
    // component having to be told about accents, or re-mounted when one changes.
    let accent = "#22d3ee";
    const readAccent = () => {
      const value = getComputedStyle(canvas).getPropertyValue("--accent").trim();
      if (value) accent = value;
    };
    readAccent();

    let width = 0;
    let height = 0;
    let frame = 0;

    const seed = () => {
      const count = Math.max(
        MIN_PARTICLES,
        Math.min(MAX_PARTICLES, Math.floor(width / TARGET_SPACING)),
      );
      particlesRef.current = Array.from({ length: count }, () => ({
        x: Math.random() * width,
        y: Math.random() * height,
        speed: Math.random() * 0.32 + 0.1,
        size: Math.random() * 1.4 + 0.45,
        opacity: Math.random() * 0.55 + 0.18,
      }));
    };

    const resize = () => {
      const dpr = Math.min(MAX_DPR, window.devicePixelRatio || 1);
      width = canvas.clientWidth;
      height = canvas.clientHeight;
      canvas.width = Math.max(1, Math.round(width * dpr));
      canvas.height = Math.max(1, Math.round(height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      seed();
    };
    resize();

    const observer = new ResizeObserver(resize);
    observer.observe(canvas);

    // Theme changes arrive as a different --accent on <html>, which is not a
    // resize; watch the attribute rather than polling the computed style.
    const themeObserver = new MutationObserver(readAccent);
    themeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-accent"],
    });

    const render = () => {
      energyRef.current += ((level === null ? 0.35 : level) - energyRef.current) * 0.06;
      const energy = energyRef.current;

      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = accent;

      for (const particle of particlesRef.current) {
        particle.y -= particle.speed * (1 + energy * 1.8);
        // A slow sideways wander, so the column is not a conveyor belt.
        particle.x += Math.sin(particle.y * 0.014 + particle.size) * 0.35;

        if (particle.y < -4) {
          particle.y = height + Math.random() * 24;
          particle.x = Math.random() * width;
        }

        // Fade out at the top so the dust dissolves instead of vanishing at an
        // edge, and brighten with energy so the field responds to real input.
        const fade = Math.max(0, Math.min(1, particle.y / Math.max(1, height)));
        ctx.globalAlpha = particle.opacity * fade * (0.55 + energy * 0.75);
        ctx.beginPath();
        ctx.arc(particle.x, particle.y, particle.size, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.globalAlpha = 1;

      frame = requestAnimationFrame(render);
    };

    render();

    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      themeObserver.disconnect();
      // Leave the canvas clean: a stopped loop with a stale frame still drawn
      // would freeze the dust mid-air, which reads as a hung animation.
      ctx.clearRect(0, 0, width, height);
    };
  }, [active, level]);

  return (
    <div className={`stage w-full ${className}`}>
      <div aria-hidden className="stage-glow" />
      <div aria-hidden className="stage-beam" />
      <div className="relative z-10 flex w-full items-center justify-center">{children}</div>
      <canvas
        ref={canvasRef}
        aria-hidden
        className="pointer-events-none absolute inset-0 z-20 h-full w-full"
      />
    </div>
  );
}
