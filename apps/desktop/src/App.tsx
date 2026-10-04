/**
 * The shell (§49, §50, §60, §64).
 *
 * A title bar naming where you are, a rail naming everywhere you can go, one screen,
 * the instruction bar, and one status line. The permission dialog sits above all of
 * it because it is the only thing in the product that must be answered before
 * anything else can proceed.
 *
 * Three things this file is responsible for and nothing else is:
 *
 * 1. **The screen router.** A `switch` over `ScreenId` rather than a lookup table, so
 *    the compiler's exhaustiveness check is the thing that guarantees every screen in
 *    `SCREENS` has a component. Add an id to the union and this file stops compiling
 *    until it is handled — which is how the rail and the router are kept from
 *    drifting apart. The `ReactElement` return annotation on `Screen` is what makes
 *    that true rather than merely intended: without it the inferred return type
 *    quietly widens to include `undefined`, `undefined` is a perfectly valid
 *    `ReactNode`, and an unhandled id compiles into a nav entry that opens a blank
 *    panel — the exact §56 failure `state/screens.ts` exists to prevent. Checked by
 *    deleting a case and confirming `tsc` refuses.
 *
 * 2. **Which conversation the instruction bar continues.** Typing while looking at a
 *    conversation continues *that* conversation; typing anywhere else continues the
 *    turn already in flight, or starts a new one. Guessing differently would append
 *    the user's next message to a thread they had navigated away from.
 *
 * 3. **Keeping every panel's failure local.** Each screen is wrapped in
 *    `PanelBoundary`, so a bug in one screen costs that screen rather than the window.
 */

import type { ReactElement } from "react";

import { Icon } from "./components/Icon";
import { ACCENTS, IconButton, type Accent } from "./components/primitives";
import { CommandBar } from "./shell/CommandBar";
import { NavRail } from "./shell/NavRail";
import { PanelBoundary } from "./shell/PanelBoundary";
import { PermissionDialog } from "./shell/PermissionDialog";
import { StatusBar } from "./shell/StatusBar";
import { ActivityScreen } from "./screens/ActivityScreen";
import { AutomationsScreen } from "./screens/AutomationsScreen";
import { AwarenessScreen } from "./screens/AwarenessScreen";
import { ConversationsScreen } from "./screens/ConversationsScreen";
import { CoreScreen } from "./screens/CoreScreen";
import { MemoryScreen } from "./screens/MemoryScreen";
import { ModelsScreen } from "./screens/ModelsScreen";
import { PermissionsScreen } from "./screens/PermissionsScreen";
import { ProjectsScreen } from "./screens/ProjectsScreen";
import { SettingsScreen } from "./screens/SettingsScreen";
import { TasksScreen } from "./screens/TasksScreen";
import { ToolsScreen } from "./screens/ToolsScreen";
import { VoiceScreen } from "./screens/VoiceScreen";
import { useNexus } from "./state/nexus";
import { SCREEN_BY_ID } from "./state/screens";

export function App() {
  const { screen, focus, accent, setAccent, startup } = useNexus();
  const here = SCREEN_BY_ID[screen];

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-void text-ink">
      <header className="flex shrink-0 items-center gap-3 border-b border-line bg-base px-3 py-2">
        <span className="flex items-center gap-2">
          <Icon name="core" size={16} className="text-accent" />
          <span className="font-mono text-xs font-medium tracking-[0.2em] text-ink">NEXUS</span>
        </span>

        <span aria-hidden className="h-4 w-px bg-line-bright" />

        <span className="label label-accent">{here.label}</span>
        <span className="min-w-0 truncate text-[11px] text-ink-dim">{here.purpose}</span>

        <div className="ml-auto flex items-center gap-2">
          {startup.state === "ready" && (
            <span
              className="font-mono text-[10px] text-ink-faint"
              title={`Display server: ${startup.data.session_type}. Some tools depend on it.`}
            >
              {startup.data.session_type.toUpperCase()}
            </span>
          )}
          <AccentPicker accent={accent} onPick={setAccent} />
          <IconButton
            name="refresh"
            label="Reload the startup snapshot"
            onClick={startup.reload}
            busy={startup.refreshing}
          />
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        <div className="shrink-0 border-r border-line bg-base">
          <NavRail />
        </div>
        <main className="min-w-0 flex-1 overflow-hidden">
          <PanelBoundary what={here.label}>
            <Screen />
          </PanelBoundary>
        </main>
      </div>

      <CommandBar conversationId={screen === "conversations" ? focus : null} />
      <StatusBar />
      <PermissionDialog />
    </div>
  );
}

/**
 * The router.
 *
 * Deliberately exhaustive with no `default` branch, and annotated `ReactElement` so
 * that a missing case is a compile error ("lacks ending return statement") rather
 * than a blank screen at runtime. The annotation is the mechanism; the missing
 * `default` is what stops a new screen from being silently absorbed by a fallback.
 */
function Screen(): ReactElement {
  const { screen, focus } = useNexus();
  switch (screen) {
    case "core":
      return <CoreScreen />;
    case "conversations":
      return <ConversationsScreen focus={focus} />;
    case "memory":
      return <MemoryScreen />;
    case "tasks":
      return <TasksScreen focus={focus} />;
    case "projects":
      return <ProjectsScreen focus={focus} />;
    case "awareness":
      return <AwarenessScreen />;
    case "automations":
      return <AutomationsScreen focus={focus} />;
    case "tools":
      return <ToolsScreen />;
    case "models":
      return <ModelsScreen />;
    case "permissions":
      return <PermissionsScreen />;
    case "activity":
      return <ActivityScreen />;
    case "voice":
      return <VoiceScreen />;
    case "settings":
      return <SettingsScreen />;
  }
}

/**
 * The accent switch (§52).
 *
 * Four swatches rather than a dropdown, because the choice *is* the colour and a menu
 * of colour names is a worse way to pick one. Each carries its name as an accessible
 * label, so the control is not colour-only (§92).
 *
 * The swatch colours are literals here, and that is not laziness. `--accent` is scoped
 * to `:root[data-accent=…]` in styles.css, so `var(--accent)` inside any child element
 * resolves to *the current theme* — all four swatches would paint the same colour and
 * the picker would show one option four times. Four literals matching the `@theme`
 * block is the only way to show four colours simultaneously without duplicating the
 * whole token set onto a second selector.
 */
const SWATCH: Record<Accent, string> = {
  cyan: "#22d3ee",
  crimson: "#ff3b4e",
  amber: "#ffa726",
  violet: "#a78bfa",
};

function AccentPicker({ accent, onPick }: { accent: Accent; onPick: (next: Accent) => void }) {
  return (
    <div role="group" aria-label="Accent colour" className="flex items-center gap-1">
      {ACCENTS.map((option) => (
        <button
          key={option}
          type="button"
          onClick={() => onPick(option)}
          aria-label={`${option} accent`}
          aria-pressed={option === accent}
          title={`${option} accent`}
          className={[
            "h-3.5 w-3.5 border transition-transform",
            option === accent ? "scale-110 border-ink" : "border-line-bright hover:scale-105",
          ].join(" ")}
          style={{ background: SWATCH[option] }}
        />
      ))}
    </div>
  );
}
