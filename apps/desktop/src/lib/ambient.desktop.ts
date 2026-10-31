/**
 * DESKTOP AMBIENT IMPLEMENTATION — Tauri window tracking.
 *
 * This file is only imported in the desktop build (via Vite's conditional import
 * or by the entry point). It provides the real `getActiveApp` and
 * `onActiveAppChange` implementations using Tauri's window APIs.
 *
 * In the web build, the base `ambient.ts` provides no-op implementations.
 */

import { getAllWindows } from "@tauri-apps/api/window";
import { listen } from "@tauri-apps/api/event";

/** Cache of the last known active app, for synchronous access. */
let activeAppCache: string | null = null;
const appChangeListeners = new Set<(app: string | null) => void>();

/** Notify all listeners of an app change. */
function notifyAppChange(app: string | null): void {
  activeAppCache = app;
  for (const listener of appChangeListeners) {
    try {
      listener(app);
    } catch (error) {
      console.warn("[ambient.desktop] listener error:", error);
    }
  }
}

/**
 * Get the currently focused window's app name.
 * Uses Tauri's `window.all()` to enumerate windows and find the focused one.
 */
export async function getActiveApp(): Promise<string | null> {
  try {
    // Get all windows in the current Tauri app
    const windows = await getAllWindows();
    for (const window of windows) {
      const isFocused = await window.isFocused();
      if (isFocused) {
        // Get the window's title, which typically contains the app name
        const title = await window.title();
        if (title) {
          // Extract app name from title (e.g., "filename — VS Code" -> "VS Code")
          const appName = extractAppName(title);
          return appName;
        }
      }
    }
  } catch (error) {
    console.warn("[ambient.desktop] getActiveApp failed:", error);
  }
  return null;
}

/**
 * Subscribe to active window changes.
 * Listens to Tauri's `tauri://focus` and `tauri://blur` events, and also
 * polls as a fallback since focus events can be unreliable.
 */
export function onActiveAppChange(callback: (app: string | null) => void): () => void {
  appChangeListeners.add(callback);

  // Immediately call with current cached value
  callback(activeAppCache);

  // Start polling as primary mechanism (Tauri focus events are not always reliable)
  let pollTimer: number | null = null;
  let lastApp: string | null = activeAppCache;

  const poll = async () => {
    const app = await getActiveApp();
    if (app !== lastApp) {
      lastApp = app;
      notifyAppChange(app);
    }
  };

  // Initial poll
  poll();

  // Poll every 2 seconds
  pollTimer = window.setInterval(poll, 2000);

  // Also listen to Tauri focus events as a supplement
  let unlistenFocus: (() => void) | null = null;
  let unlistenBlur: (() => void) | null = null;

  listen("tauri://focus", () => {
    // Window gained focus, poll immediately
    poll();
  }).then((unlisten) => {
    unlistenFocus = unlisten;
  });

  listen("tauri://blur", () => {
    // Window lost focus, poll immediately
    poll();
  }).then((unlisten) => {
    unlistenBlur = unlisten;
  });

  // Return unsubscribe function
  return () => {
    appChangeListeners.delete(callback);
    if (pollTimer !== null) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
    if (unlistenFocus) unlistenFocus();
    if (unlistenBlur) unlistenBlur();
    // If no more listeners, clear cache
    if (appChangeListeners.size === 0) {
      activeAppCache = null;
    }
  };
}

/**
 * Extract a clean app name from a window title.
 * Examples:
 *   "main.rs - project - VS Code" -> "VS Code"
 *   "Terminal — bash" -> "Terminal"
 *   "Firefox" -> "Firefox"
 *   "Settings" -> "Settings"
 */
function extractAppName(title: string | null | undefined): string {
  if (!title) return "Unknown";
  // Common patterns: " — ", " - ", " | ", " :: "
  const separators = [" — ", " - ", " | ", " :: "];
  for (const sep of separators) {
    const parts = title.split(sep);
    if (parts.length > 1) {
      // Usually the last part is the app name
      const lastPart = parts[parts.length - 1];
      if (lastPart) {
        const candidate = lastPart.trim();
        if (candidate && candidate.length < 50) {
          return candidate;
        }
      }
    }
  }
  // Fallback: use the whole title if it's short enough
  if (title.length < 50) return title.trim();
  // Last resort: first 50 chars
  return title.slice(0, 50).trim();
}

// Make the functions available globally for the base ambient.ts to override
// This is a build-time replacement pattern; in practice, the desktop entry point
// imports this file and re-exports the functions.
if (typeof window !== "undefined") {
  (window as any).__AMBIENT_DESKTOP__ = {
    getActiveApp,
    onActiveAppChange,
  };
}

// Also export for direct import
export { notifyAppChange, activeAppCache };

// Register overrides with the base module
import { registerAmbientDesktopOverrides } from "./ambient";
registerAmbientDesktopOverrides(getActiveApp, onActiveAppChange);