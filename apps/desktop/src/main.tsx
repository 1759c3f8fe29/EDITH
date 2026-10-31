/**
 * The entry point.
 *
 * Three things happen here and nothing else: the stylesheet is imported (so Tailwind
 * and the token layer exist before the first paint), the providers are nested in
 * dependency order, and the root is mounted.
 *
 * Provider order is not arbitrary. `NexusProvider` owns the accent and the link, and
 * `TurnProvider` reads neither — but the permission dialog lives inside the shell and
 * needs both, so the shell has to be inside both. `VoiceProvider` goes innermost
 * because it watches the turn: speaking an answer requires knowing a turn finished,
 * so it has to be able to read `useTurn`. `StrictMode` is on deliberately:
 * it double-invokes effects in development, which is exactly how a subscription that
 * fails to clean up gets caught, and every effect in this app returns an unsubscribe.
 *
 * No error boundary wraps the tree at this level, and that is a decision. A thrown
 * render error here would mean the shell itself is broken; catching it would replace
 * a stack trace with a friendly box and lose the only diagnostic. Boundaries belong
 * around individual panels, where the rest of the app is still usable — see
 * `PanelBoundary` in the shell.
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// The three faces the token layer names. Bundled rather than linked from Google
// Fonts (which is what the reference does) because this ships as a desktop app
// that must render correctly with no network -- under file:// a webfont link is
// simply a font that never arrives, and the app falls back to DejaVu in silence.
// Before these imports the declared stack named Inter and JetBrains Mono while
// neither was installed on this machine, so every "monospace label" in the
// design was rendering in DejaVu Sans Mono.
import "@fontsource-variable/inter";
import "@fontsource/space-grotesk/500.css";
import "@fontsource/space-grotesk/600.css";
import "@fontsource/space-grotesk/700.css";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/500.css";
import "@fontsource/jetbrains-mono/600.css";

import { App } from "./App";
import { NexusProvider } from "./state/nexus";
import { TurnProvider } from "./state/turn";
import { VoiceProvider } from "./state/voice";
// Initialize desktop ambient monitoring (must be before ambient.ts is used)
import "./lib/ambient.desktop";
import "./styles.css";

const host = document.getElementById("root");
if (!host) {
  // index.html ships `<div id="root">`, so this cannot happen from a normal load —
  // it means the document was replaced. Said out loud rather than swallowed, because
  // a silent no-op here looks exactly like a blank white window with no explanation.
  throw new Error("NEXUS could not start: no #root element in the document.");
}

createRoot(host).render(
  <StrictMode>
    <NexusProvider>
      <TurnProvider>
        <VoiceProvider>
          <App />
        </VoiceProvider>
      </TurnProvider>
    </NexusProvider>
  </StrictMode>,
);
