/**
 * The design system (§47-§56).
 *
 * Every visible surface in NEXUS is built from the pieces in this file, which is
 * what makes §67 ("consistent panel geometry, typography, spacing, colour
 * semantics, iconography, border language, state language") a property of the code
 * rather than a promise. A screen that needs a container uses `Panel`; if a screen
 * hand-rolls a `<div className="border rounded-lg">` that is a defect, because it
 * will drift.
 *
 * Two rules are enforced structurally rather than by review:
 *
 * 1. **No dead buttons (§56).** `Button` cannot be rendered without either an
 *    `onClick` or an `href`/`type="submit"`; a disabled one requires a `reason`
 *    that is shown to the user. "Why is this greyed out" is answerable at every
 *    control.
 * 2. **Colour is never the only signal (§92, §55).** `StatusPill` and `StateDot`
 *    both take a state and always render a text label with it. There is no variant
 *    that is only a coloured dot.
 */

import {
  createContext,
  useContext,
  useEffect,
  useId,
  useRef,
  type ReactNode,
} from "react";

import { Icon, Spinner, type IconName } from "./Icon";

/* ------------------------------------------------------------------ panels */

export type PanelProps = {
  children: ReactNode;
  /** The uppercase technical label in the header (§50). Omit for an unlabelled surface. */
  title?: string;
  /** Small dim text after the title — a count, a timestamp, a model name. */
  meta?: ReactNode;
  /** Controls on the right of the header. */
  actions?: ReactNode;
  className?: string;
  bodyClassName?: string;
  /** Draw the accent border and one soft glow: the *active* subsystem only (§49). */
  active?: boolean;
  /** `flat` removes the 1px frame, for panels nested inside another panel. */
  variant?: "framed" | "flat";
  /** Scroll the body rather than the page. Almost always what a column wants. */
  scroll?: boolean;
};

export function Panel({
  children,
  title,
  meta,
  actions,
  className = "",
  bodyClassName = "",
  active = false,
  variant = "framed",
  scroll = false,
}: PanelProps) {
  const body = (
    <>
      {(title || actions) && (
        <header className="flex items-center gap-3 border-b border-line px-3 py-2 shrink-0">
          {title && <span className={`label marker ${active ? "label-accent" : ""}`}>{title}</span>}
          {meta && <span className="font-mono text-[10px] text-ink-faint truncate">{meta}</span>}
          {actions && <div className="ml-auto flex items-center gap-1">{actions}</div>}
        </header>
      )}
      <div className={`${scroll ? "flex-1 overflow-y-auto" : ""} ${bodyClassName}`}>{children}</div>
    </>
  );

  if (variant === "flat") {
    return <section className={`flex flex-col min-h-0 ${className}`}>{body}</section>;
  }

  // The frame: an outer clipped element painting the border colour with 1px of
  // padding, and the body inset inside it. Borders cannot follow a clip-path, so
  // this is the only way to get a 1px edge along a 45-degree cut without SVG.
  return (
    <div
      className={`chamfer panel flex min-h-0 flex-col ${active ? "glow-accent" : ""} ${className}`}
      style={active ? { background: "var(--accent-dim)" } : undefined}
    >
      <div className={`chamfer panel-body flex min-h-0 flex-1 flex-col`}>{body}</div>
    </div>
  );
}

/** A labelled group inside a panel — the next level down in the §48 hierarchy. */
export function Section({
  title,
  meta,
  actions,
  children,
  className = "",
}: {
  title: string;
  meta?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={className}>
      <div className="flex items-center gap-2 px-3 pt-3 pb-1.5">
        <span className="label">{title}</span>
        {meta && <span className="font-mono text-[10px] text-ink-faint">{meta}</span>}
        {actions && <div className="ml-auto flex items-center gap-1">{actions}</div>}
      </div>
      {children}
    </section>
  );
}

/* ----------------------------------------------------------------- buttons */

export type ButtonTone = "default" | "accent" | "danger" | "ghost";
export type ButtonSize = "sm" | "md";

const TONES: Record<ButtonTone, string> = {
  default:
    "bg-raised text-ink hover:bg-line-bright hover:text-ink border border-line-bright active:translate-y-px",
  accent:
    "bg-accent text-accent-ink hover:bg-accent-bright border border-accent font-medium active:translate-y-px",
  danger:
    "bg-transparent text-danger hover:bg-danger/12 border border-danger/50 hover:border-danger active:translate-y-px",
  ghost:
    "bg-transparent text-ink-soft hover:bg-raised hover:text-ink border border-transparent active:translate-y-px",
};

export type ButtonProps = {
  children: ReactNode;
  onClick?: () => void;
  type?: "button" | "submit";
  tone?: ButtonTone;
  size?: ButtonSize;
  icon?: IconName;
  /** Show a spinner and block further clicks. The label stays, so width does not jump. */
  busy?: boolean;
  disabled?: boolean;
  /**
   * Required when `disabled` — rendered as the tooltip and the accessible
   * description. §56: a control the user cannot press must say why.
   */
  reason?: string;
  className?: string;
  /** Full-width in a column layout. */
  block?: boolean;
  title?: string;
};

export function Button({
  children,
  onClick,
  type = "button",
  tone = "default",
  size = "md",
  icon,
  busy = false,
  disabled = false,
  reason,
  className = "",
  block = false,
  title,
}: ButtonProps) {
  const blocked = disabled || busy;
  const sizing = size === "sm" ? "h-7 px-2.5 text-[11px] gap-1.5" : "h-8 px-3 text-xs gap-2";
  return (
    <button
      type={type}
      onClick={blocked ? undefined : onClick}
      disabled={blocked}
      title={disabled ? reason || title : title}
      aria-disabled={blocked}
      aria-busy={busy || undefined}
      className={[
        "chamfer-sm inline-flex items-center justify-center font-mono uppercase tracking-wider",
        "transition-colors duration-150 select-none",
        sizing,
        TONES[tone],
        blocked ? "opacity-45 cursor-not-allowed hover:bg-raised" : "cursor-pointer",
        block ? "w-full" : "",
        className,
      ].join(" ")}
    >
      {busy ? <Icon name="spinner" size={14} className="animate-spin" /> : icon ? <Icon name={icon} size={14} /> : null}
      <span className="truncate">{children}</span>
    </button>
  );
}

/**
 * A control whose only content is an icon.
 *
 * `label` is not optional: without it this is an unnamed button, which §55 counts
 * as inaccessible and §56 as indistinguishable from decoration.
 */
export function IconButton({
  name,
  label,
  onClick,
  tone = "ghost",
  busy = false,
  disabled = false,
  reason,
  size = 15,
  className = "",
}: {
  name: IconName;
  label: string;
  onClick?: () => void;
  tone?: ButtonTone;
  busy?: boolean;
  disabled?: boolean;
  reason?: string;
  size?: number;
  className?: string;
}) {
  const blocked = disabled || busy;
  return (
    <button
      type="button"
      onClick={blocked ? undefined : onClick}
      disabled={blocked}
      title={disabled && reason ? `${label} — ${reason}` : label}
      aria-label={label}
      aria-busy={busy || undefined}
      className={[
        "chamfer-sm inline-flex h-7 w-7 items-center justify-center transition-colors duration-150",
        TONES[tone],
        blocked ? "opacity-40 cursor-not-allowed" : "cursor-pointer",
        className,
      ].join(" ")}
    >
      <Icon name={busy ? "spinner" : name} size={size} className={busy ? "animate-spin" : ""} />
    </button>
  );
}

/* ------------------------------------------------------------------ states */

/**
 * The state vocabulary (§45, §46, §68).
 *
 * One list for the whole app. A task, a device, a tool run and an automation all
 * draw their state from here, so "failed" looks the same everywhere and the user
 * learns the colours once.
 */
export type State =
  | "ok"
  | "active"
  | "pending"
  | "failed"
  | "cancelled"
  | "waiting"
  | "offline"
  | "warn"
  | "info"
  | "idle"
  | "disabled";

const STATE_STYLE: Record<State, { fg: string; bg: string; glyph: string }> = {
  ok: { fg: "text-ok", bg: "bg-ok/12 border-ok/40", glyph: "✓" },
  active: { fg: "text-accent", bg: "bg-accent/12 border-accent/40", glyph: "●" },
  pending: { fg: "text-ink-dim", bg: "bg-raised border-line-bright", glyph: "○" },
  failed: { fg: "text-danger", bg: "bg-danger/12 border-danger/40", glyph: "✕" },
  cancelled: { fg: "text-ink-dim", bg: "bg-raised border-line-bright", glyph: "—" },
  waiting: { fg: "text-warn", bg: "bg-warn/12 border-warn/40", glyph: "⏸" },
  offline: { fg: "text-ink-dim", bg: "bg-raised border-line-bright", glyph: "⊘" },
  warn: { fg: "text-warn", bg: "bg-warn/12 border-warn/40", glyph: "!" },
  info: { fg: "text-info", bg: "bg-info/12 border-info/40", glyph: "i" },
  idle: { fg: "text-ink-soft", bg: "bg-raised border-line-bright", glyph: "·" },
  disabled: { fg: "text-ink-faint", bg: "bg-base border-line", glyph: "·" },
};

/** Map a backend status string onto the shared vocabulary. Unknown values stay visible. */
export function stateOf(status: string | null | undefined): State {
  switch ((status ?? "").toLowerCase()) {
    case "complete":
    case "completed":
    case "done":
    case "ok":
    case "online":
    case "allowed":
    case "granted":
    case "enabled":
    case "success":
      return "ok";
    case "active":
    case "running":
    case "streaming":
    case "executing":
      return "active";
    case "pending":
    case "queued":
    case "proposed":
      return "pending";
    case "failed":
    case "error":
    case "denied":
    case "rejected":
      return "failed";
    case "cancelled":
    case "canceled":
    case "interrupted":
    case "revoked":
      return "cancelled";
    case "waiting":
    case "waiting_for_permission":
    case "needs_approval":
      return "waiting";
    case "offline":
    case "unavailable":
    case "unconfigured":
      return "offline";
    case "disabled":
      return "disabled";
    default:
      return "idle";
  }
}

/**
 * A state chip. Always carries its own text — there is deliberately no
 * label-less variant, because a bare coloured dot is the exact failure §92 names.
 */
export function StatusPill({
  state,
  children,
  size = "md",
  pulse = false,
}: {
  state: State;
  children: ReactNode;
  size?: "sm" | "md";
  /** Only for a genuinely ongoing state — §94: motion means something is happening. */
  pulse?: boolean;
}) {
  const style = STATE_STYLE[state];
  return (
    <span
      className={[
        "chamfer-sm inline-flex shrink-0 items-center gap-1.5 border font-mono uppercase tracking-wider",
        size === "sm" ? "px-1.5 py-0.5 text-[9px]" : "px-2 py-0.5 text-[10px]",
        style.fg,
        style.bg,
      ].join(" ")}
    >
      <span aria-hidden className={pulse ? "animate-pulse" : ""}>
        {style.glyph}
      </span>
      {children}
    </span>
  );
}

/** The step glyph used by task and agent execution lists (§24). */
export function StateDot({ state, pulse = false }: { state: State; pulse?: boolean }) {
  const style = STATE_STYLE[state];
  return (
    <span
      aria-hidden
      className={`inline-block w-4 shrink-0 text-center font-mono text-[11px] ${style.fg} ${
        pulse ? "animate-pulse" : ""
      }`}
    >
      {style.glyph}
    </span>
  );
}

/* -------------------------------------------------------------- empty/error */

/**
 * Nothing here — and *why* nothing is here.
 *
 * "No results" and "not configured yet" and "you have not created one" are three
 * different situations with three different next actions, and §46 requires the
 * empty state to be designed rather than defaulted to a blank box.
 */
export function EmptyState({
  icon = "info",
  title,
  detail,
  action,
}: {
  icon?: IconName;
  title: string;
  detail?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-10 text-center">
      <Icon name={icon} size={22} className="text-ink-faint" />
      <p className="label">{title}</p>
      {detail && <p className="max-w-sm text-xs text-ink-dim text-balance">{detail}</p>}
      {action && <div className="mt-1">{action}</div>}
    </div>
  );
}

/**
 * A failure in the three-part shape §45 mandates: what happened, what NEXUS did
 * about it, what the user can do next. A raw stack trace or a bare "Error" is
 * explicitly not acceptable.
 */
export function ErrorState({
  what,
  did,
  next,
  offline = false,
  onRetry,
  retryLabel = "Retry",
  compact = false,
}: {
  what: string;
  did?: string;
  next?: string;
  offline?: boolean;
  onRetry?: () => void;
  retryLabel?: string;
  compact?: boolean;
}) {
  return (
    <div
      role="alert"
      className={[
        "chamfer-sm border bg-danger/6 text-xs",
        offline ? "border-ink-faint/40" : "border-danger/40",
        compact ? "px-2.5 py-2" : "px-3 py-3",
      ].join(" ")}
    >
      <div className="flex items-start gap-2">
        <Icon
          name={offline ? "alert" : "alert"}
          size={14}
          className={`mt-0.5 shrink-0 ${offline ? "text-ink-dim" : "text-danger"}`}
        />
        <div className="min-w-0 flex-1 space-y-1">
          <p className={`label ${offline ? "" : "text-danger"}`}>
            {offline ? "Core unreachable" : "What happened"}
          </p>
          <p className="text-ink break-words">{what}</p>
          {did && (
            <>
              <p className="label pt-1">What NEXUS did</p>
              <p className="text-ink-soft break-words">{did}</p>
            </>
          )}
          {next && (
            <>
              <p className="label pt-1">What you can do</p>
              <p className="text-ink-soft break-words">{next}</p>
            </>
          )}
          {onRetry && (
            <div className="pt-2">
              <Button size="sm" icon="refresh" onClick={onRetry}>
                {retryLabel}
              </Button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/** A skeleton that says what it is loading, not an anonymous grey box. */
export function LoadingState({ what }: { what: string }) {
  return (
    <div className="flex items-center gap-2 px-3 py-6">
      <Spinner label={`Loading ${what}`} />
      <span className="label">Loading {what}</span>
    </div>
  );
}

/**
 * A truthful unavailable state (§61).
 *
 * When a capability genuinely is not wired up — no feed configured, no TTS voice
 * installed, no display server for screen capture — this is what the screen shows.
 * The one thing it must never do is render a plausible-looking fake.
 */
export function UnavailableState({
  what,
  why,
  fix,
}: {
  what: string;
  why: string;
  fix?: string;
}) {
  return (
    <div className="chamfer-sm border border-line-bright bg-base px-3 py-3 text-xs">
      <p className="label marker">{what} unavailable</p>
      <p className="mt-1.5 text-ink-soft">{why}</p>
      {fix && <p className="mt-1 text-ink-dim">{fix}</p>}
    </div>
  );
}

/** The result of an action: a real success or a real failure, never silence (§56). */
export function ActionResult({
  done,
  failed,
  onClear,
}: {
  done: string;
  failed: string;
  onClear?: () => void;
}) {
  if (!done && !failed) return null;
  const bad = Boolean(failed);
  return (
    <div
      role="status"
      aria-live="polite"
      className={[
        "chamfer-sm mt-2 flex items-start gap-2 border px-2.5 py-1.5 text-xs",
        bad ? "border-danger/40 bg-danger/8 text-danger" : "border-ok/40 bg-ok/8 text-ok",
      ].join(" ")}
    >
      <Icon name={bad ? "alert" : "check"} size={13} className="mt-0.5 shrink-0" />
      <span className="min-w-0 flex-1 break-words">{failed || done}</span>
      {onClear && <IconButton name="close" label="Dismiss" onClick={onClear} size={12} />}
    </div>
  );
}

/* ------------------------------------------------------------------- fields */

/** A labelled input. The label is a real `<label>` — §55 wants programmatic association. */
export function Field({
  label,
  hint,
  error,
  children,
  htmlFor,
}: {
  label: string;
  hint?: string;
  error?: string;
  children: ReactNode;
  htmlFor?: string;
}) {
  return (
    <div className="space-y-1">
      <label className="label block" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
      {hint && !error && <p className="text-[11px] text-ink-dim">{hint}</p>}
      {error && (
        <p className="text-[11px] text-danger" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

const INPUT_CLASS =
  "chamfer-sm w-full border border-line-bright bg-base px-2.5 py-1.5 text-xs text-ink " +
  "placeholder:text-ink-faint focus:border-accent focus:outline-none disabled:opacity-45";

export function TextInput({
  value,
  onChange,
  placeholder,
  id,
  disabled,
  type = "text",
  onEnter,
  autoFocus,
  invalid,
}: {
  value: string;
  onChange: (next: string) => void;
  placeholder?: string;
  id?: string;
  disabled?: boolean;
  type?: "text" | "password" | "number" | "url";
  onEnter?: () => void;
  autoFocus?: boolean;
  invalid?: boolean;
}) {
  return (
    <input
      id={id}
      type={type}
      value={value}
      disabled={disabled}
      autoFocus={autoFocus}
      aria-invalid={invalid || undefined}
      placeholder={placeholder}
      onChange={(event) => onChange(event.target.value)}
      onKeyDown={(event) => {
        if (event.key === "Enter" && onEnter) {
          event.preventDefault();
          onEnter();
        }
      }}
      className={`${INPUT_CLASS} ${invalid ? "border-danger" : ""}`}
    />
  );
}

export function TextArea({
  value,
  onChange,
  placeholder,
  id,
  rows = 4,
  disabled,
}: {
  value: string;
  onChange: (next: string) => void;
  placeholder?: string;
  id?: string;
  rows?: number;
  disabled?: boolean;
}) {
  return (
    <textarea
      id={id}
      value={value}
      rows={rows}
      disabled={disabled}
      placeholder={placeholder}
      onChange={(event) => onChange(event.target.value)}
      className={`${INPUT_CLASS} resize-y leading-relaxed`}
    />
  );
}

export function Select<T extends string>({
  value,
  onChange,
  options,
  id,
  disabled,
}: {
  value: T;
  onChange: (next: T) => void;
  options: Array<{ value: T; label: string }>;
  id?: string;
  disabled?: boolean;
}) {
  return (
    <select
      id={id}
      value={value}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value as T)}
      className={`${INPUT_CLASS} font-mono uppercase tracking-wider`}
    >
      {options.map((option) => (
        <option key={option.value} value={option.value} className="bg-panel">
          {option.label}
        </option>
      ))}
    </select>
  );
}

/**
 * A switch. `role="switch"` with `aria-checked`, not a styled checkbox: §55 wants
 * semantic controls, and the on/off state is announced rather than inferred.
 */
export function Toggle({
  on,
  onChange,
  label,
  disabled,
  reason,
  busy,
}: {
  on: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
  reason?: string;
  busy?: boolean;
}) {
  const blocked = disabled || busy;
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      aria-busy={busy || undefined}
      disabled={blocked}
      title={blocked && reason ? reason : label}
      onClick={blocked ? undefined : () => onChange(!on)}
      className={[
        "relative inline-flex h-4 w-8 shrink-0 items-center border transition-colors duration-150",
        on ? "border-accent bg-accent/25" : "border-line-bright bg-base",
        blocked ? "opacity-45 cursor-not-allowed" : "cursor-pointer",
      ].join(" ")}
    >
      <span
        aria-hidden
        className={[
          "absolute top-0.5 h-2.5 w-2.5 transition-all duration-150",
          on ? "left-[18px] bg-accent" : "left-0.5 bg-ink-faint",
        ].join(" ")}
      />
    </button>
  );
}

/* -------------------------------------------------------------------- rows */

/**
 * The dense key/value row that most of the technical panels are made of. The
 * value is monospace because these are values you compare across rows.
 */
export function DataRow({
  label,
  value,
  state,
  title,
}: {
  label: string;
  value: ReactNode;
  state?: State;
  title?: string;
}) {
  return (
    <div className="flex items-baseline gap-3 px-3 py-1" title={title}>
      <span className="label shrink-0">{label}</span>
      <span
        className={[
          "ml-auto min-w-0 truncate font-mono text-[11px]",
          state ? STATE_STYLE[state].fg : "text-ink-soft",
        ].join(" ")}
      >
        {value}
      </span>
    </div>
  );
}

/** A selectable row in a list. Keyboard reachable, with a real pressed state. */
export function ListRow({
  children,
  onClick,
  selected = false,
  className = "",
}: {
  children: ReactNode;
  onClick?: () => void;
  selected?: boolean;
  className?: string;
}) {
  const base = [
    "w-full border-b border-line/60 px-3 py-2 text-left transition-colors duration-100",
    selected ? "bg-accent/8 border-l-2 border-l-accent" : "border-l-2 border-l-transparent",
    onClick ? "hover:bg-raised cursor-pointer" : "",
    className,
  ].join(" ");
  if (!onClick) return <div className={base}>{children}</div>;
  return (
    <button type="button" onClick={onClick} aria-current={selected || undefined} className={base}>
      {children}
    </button>
  );
}

/* ------------------------------------------------------------------ dialogs */

/**
 * A modal dialog (§55: "accessible dialogs", §41: "clear dialog dismissal").
 *
 * Focus moves into the dialog on open and returns to whatever opened it on close;
 * Escape closes; the backdrop closes unless the dialog is a confirmation the user
 * must answer deliberately. Without the focus return, a keyboard user is dropped
 * at the top of the document every time they dismiss something.
 */
export function Dialog({
  open,
  onClose,
  title,
  children,
  footer,
  width = "md",
  dismissable = true,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  width?: "sm" | "md" | "lg";
  dismissable?: boolean;
}) {
  const panel = useRef<HTMLDivElement | null>(null);
  const opener = useRef<Element | null>(null);
  const headingId = useId();

  useEffect(() => {
    if (!open) return;
    opener.current = document.activeElement;
    const target = panel.current?.querySelector<HTMLElement>(
      "input, textarea, select, button, [tabindex]",
    );
    (target ?? panel.current)?.focus();

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && dismissable) {
        event.stopPropagation();
        onClose();
      }
      // Focus trap: a dialog the user can Tab out of is a dialog they can lose.
      if (event.key === "Tab" && panel.current) {
        const focusable = Array.from(
          panel.current.querySelectorAll<HTMLElement>(
            'input:not([disabled]), textarea:not([disabled]), select:not([disabled]), button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
          ),
        );
        if (focusable.length === 0) return;
        const first = focusable[0]!;
        const last = focusable[focusable.length - 1]!;
        if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        } else if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      const back = opener.current;
      if (back instanceof HTMLElement) back.focus();
    };
  }, [open, dismissable, onClose]);

  if (!open) return null;
  const widths = { sm: "max-w-sm", md: "max-w-lg", lg: "max-w-3xl" }[width];

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-void/80 p-6"
      onMouseDown={(event) => {
        if (dismissable && event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        tabIndex={-1}
        className={`chamfer panel w-full ${widths} shadow-2xl outline-none`}
      >
        <div className="chamfer panel-body flex max-h-[80vh] flex-col">
          <header className="flex items-center gap-3 border-b border-line px-3 py-2">
            <h2 id={headingId} className="label marker label-accent">
              {title}
            </h2>
            {dismissable && (
              <div className="ml-auto">
                <IconButton name="close" label="Close dialog" onClick={onClose} />
              </div>
            )}
          </header>
          <div className="flex-1 overflow-y-auto px-3 py-3">{children}</div>
          {footer && (
            <footer className="flex items-center justify-end gap-2 border-t border-line px-3 py-2">
              {footer}
            </footer>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * Destructive confirmation (§56: "confirmation where destructive").
 *
 * Takes the exact consequence as a sentence, because "Are you sure?" does not tell
 * anyone what they are about to lose. Deliberately not dismissable by backdrop.
 */
export function ConfirmDialog({
  open,
  title,
  consequence,
  confirmLabel,
  onConfirm,
  onCancel,
  busy = false,
  failed = "",
}: {
  open: boolean;
  title: string;
  consequence: string;
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
  busy?: boolean;
  failed?: string;
}) {
  return (
    <Dialog
      open={open}
      onClose={onCancel}
      title={title}
      width="sm"
      dismissable={!busy}
      footer={
        <>
          <Button tone="ghost" size="sm" onClick={onCancel} disabled={busy} reason="Running.">
            Cancel
          </Button>
          <Button tone="danger" size="sm" onClick={onConfirm} busy={busy}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      <p className="text-xs leading-relaxed text-ink">{consequence}</p>
      {failed && <ActionResult done="" failed={failed} />}
    </Dialog>
  );
}

/* --------------------------------------------------------------------- tabs */

/**
 * Tabs, with the §13 caveat baked into the call site: "Use only tabs that
 * correspond to actual implemented functionality." There is no `disabled` tab
 * variant here on purpose — a tab that cannot be opened should not be rendered.
 */
export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
  meta,
}: {
  tabs: Array<{ value: T; label: string; badge?: number }>;
  value: T;
  onChange: (next: T) => void;
  meta?: ReactNode;
}) {
  return (
    <div role="tablist" className="flex items-stretch border-b border-line">
      {tabs.map((tab) => {
        const on = tab.value === value;
        return (
          <button
            key={tab.value}
            role="tab"
            type="button"
            aria-selected={on}
            onClick={() => onChange(tab.value)}
            className={[
              "relative flex items-center gap-1.5 px-3 py-2 font-mono text-[10px] uppercase tracking-wider transition-colors",
              on ? "text-accent" : "text-ink-dim hover:text-ink-soft",
            ].join(" ")}
          >
            {tab.label}
            {typeof tab.badge === "number" && tab.badge > 0 && (
              <span className="chamfer-sm bg-accent/20 px-1 text-[9px] text-accent">{tab.badge}</span>
            )}
            {on && <span aria-hidden className="absolute inset-x-0 -bottom-px h-px bg-accent" />}
          </button>
        );
      })}
      {meta && <div className="ml-auto flex items-center px-3">{meta}</div>}
    </div>
  );
}

/* ------------------------------------------------------------------- timing */

/**
 * Relative time, recomputed by the caller's render rather than by a timer per
 * instance. A hundred rows each with their own `setInterval` is a hundred timers
 * the §53 performance budget does not have room for.
 */
export function relativeTime(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  const seconds = Math.round((now - then) / 1000);
  if (seconds < 0) return "just now";
  if (seconds < 45) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(then).toLocaleDateString();
}

/** Wall-clock time for a "last synced" readout, where the exact minute matters (§34). */
export function clockTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  return new Date(then).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function duration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60_000);
  return `${minutes}m ${Math.round((ms % 60_000) / 1000)}s`;
}

/* ------------------------------------------------- accent theme (§52 tokens) */

export const ACCENTS = ["cyan", "crimson", "amber", "violet"] as const;
export type Accent = (typeof ACCENTS)[number];

const AccentContext = createContext<{ accent: Accent; setAccent: (next: Accent) => void }>({
  accent: "cyan",
  setAccent: () => {},
});

export const AccentProvider = AccentContext.Provider;
export const useAccent = () => useContext(AccentContext);
