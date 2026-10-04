/**
 * A boundary around one panel (§46, §60).
 *
 * The reason this is per-panel and not per-app: a screen is a dozen independent
 * reads, and one of them throwing should cost the user that panel, not the window.
 * A single top-level boundary turns a bug in the model catalogue into "NEXUS is
 * broken", which is both less true and less useful than "MODELS could not be drawn".
 *
 * It reports the real error text rather than a friendly substitute. §60 forbids
 * misleading states, and "something went wrong" is misleading by omission — the user
 * cannot act on it and neither can anyone reading a screenshot of it.
 *
 * Class component because that is the only form React gives for `componentDidCatch`;
 * there is no hook equivalent.
 */

import { Component, type ErrorInfo, type ReactNode } from "react";

import { Button } from "../components/primitives";

type Props = { children: ReactNode; what: string };
type State = { failed: string; where: string };

export class PanelBoundary extends Component<Props, State> {
  override state: State = { failed: "", where: "" };

  static getDerivedStateFromError(cause: unknown): State {
    return {
      failed: cause instanceof Error ? cause.message : String(cause),
      where: "",
    };
  }

  override componentDidCatch(cause: unknown, info: ErrorInfo): void {
    // Kept in state rather than only logged: the component stack names the panel
    // that failed, and that is the one piece of information a bug report needs.
    this.setState({
      failed: cause instanceof Error ? cause.message : String(cause),
      where: (info.componentStack ?? "").trim().split("\n")[0]?.trim() ?? "",
    });
    console.error(`[nexus] ${this.props.what} failed to render`, cause, info.componentStack);
  }

  private retry = () => this.setState({ failed: "", where: "" });

  override render(): ReactNode {
    const { failed, where } = this.state;
    if (!failed) return this.props.children;
    return (
      <div className="flex h-full flex-col items-start justify-center gap-2 p-4">
        <span className="label text-danger">{this.props.what} failed to draw</span>
        <p className="font-mono text-[11px] leading-relaxed text-ink-soft">{failed}</p>
        {where && <p className="font-mono text-[10px] text-ink-faint">{where}</p>}
        <p className="text-[11px] leading-relaxed text-ink-dim">
          This is a bug in the interface, not in the core — nothing on the machine was
          affected. The rest of NEXUS still works.
        </p>
        <Button size="sm" tone="ghost" icon="refresh" onClick={this.retry}>
          Try drawing it again
        </Button>
      </div>
    );
  }
}
