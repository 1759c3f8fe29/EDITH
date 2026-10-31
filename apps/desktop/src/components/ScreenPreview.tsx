/**
 * Live screen preview — shows the latest frame from the screen monitor.
 *
 * This component polls the screen monitor's frames endpoint and displays
 * the most recent screenshot, making the "live screening" feature visible.
 */
import { useEffect, useRef, useState } from "react";

import { Icon } from "../components/Icon";
import { Panel } from "../components/primitives";
import { api } from "../lib/api";
import { useNexus } from "../state/nexus";

interface ScreenFrame {
  path: string;
  bytes: number;
  taken_at: string;
}

export function ScreenPreview() {
  const { link } = useNexus();
  const [frames, setFrames] = useState<ScreenFrame[]>([]);
  const [error, setError] = useState<string | null>(null);
  const intervalRef = useRef<number | null>(null);

  const fetchFrames = async () => {
    try {
      const result = await api.monitors.screenFrames();
      if (result.available) {
        setFrames(result.frames);
        setError(null);
      } else {
        setError(result.reason || "Screen monitor not available");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load frames");
    }
  };

  useEffect(() => {
    if (!link.connected) return;

    // Initial fetch
    fetchFrames();

    // Poll every 5 seconds
    intervalRef.current = window.setInterval(fetchFrames, 5_000);

    return () => {
      if (intervalRef.current) {
        window.clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [link.connected]);

  const latestFrame = frames[0];

  if (!latestFrame && error) {
    return (
      <Panel title="LIVE SCREEN" meta="unavailable" className="min-h-0 flex-1 flex flex-col">
        <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-2 p-4">
          <Icon name="monitor-off" size={32} className="text-ink-dim" />
          <p className="text-center text-sm text-ink-soft">{error}</p>
          <p className="text-center text-xs text-ink-faint">
            Enable "Screen watch" in Settings → Awareness to start capturing.
          </p>
        </div>
      </Panel>
    );
  }

  if (!latestFrame) {
    return (
      <Panel title="LIVE SCREEN" meta="waiting for first frame..." className="min-h-0 flex-1 flex flex-col">
        <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-2 p-4">
          <Icon name="monitor" size={32} className="text-ink-dim animate-pulse" />
          <p className="text-center text-sm text-ink-soft">Waiting for first screenshot...</p>
        </div>
      </Panel>
    );
  }

  return (
    <Panel title="LIVE SCREEN" meta={`captured ${latestFrame.taken_at}`} className="min-h-0 flex-1 flex flex-col">
      <div className="flex min-h-0 flex-1 flex-col items-center justify-center p-2">
        <img
          src={`file://${latestFrame.path}`}
          alt="Screen capture"
          className="max-w-full max-h-full object-contain rounded border border-line"
          style={{ imageRendering: "pixelated" }}
        />
      </div>
      <div className="px-3 pb-2 text-[10px] text-ink-faint flex items-center justify-between">
        <span>{frames.length} frame{frames.length === 1 ? "" : "s"} captured</span>
        <span>{(latestFrame.bytes / 1024).toFixed(0)} KB</span>
      </div>
    </Panel>
  );
}