/**
 * The status bar (§34, §60, §92).
 *
 * One line, always visible, saying three things the user must never have to guess:
 * whether the core is reachable, whether anything was missed while it was not, and
 * what is running on the machine right now.
 *
 * The gap notice is the part worth defending. When the event socket reconnects past
 * the core's replay buffer, some events are simply gone — and the activity feed has a
 * hole in it that no refetch will fill, because those events were never rows. §60
 * forbids presenting that as normal, so it is stated, and it stays stated until the
 * user dismisses it: `link.acknowledgeGap()` is the only thing that clears it, on
 * purpose (a reconnect does not make the hole untrue).
 */

import { Icon } from "../components/Icon";
import { IconButton, StatusPill } from "../components/primitives";
import { linkSentence, useNexus } from "../state/nexus";
import { turnSentence, useTurn } from "../state/turn";

export function StatusBar() {
  const { link, reconnect, dismissGap, runningTools, startup } = useNexus();
  const { turn, running } = useTurn();

  const tools = startup.state === "ready" ? startup.data.tools : null;

  return (
    <footer className="flex shrink-0 items-center gap-3 border-t border-line bg-base px-3 py-1.5">
      <StatusPill state={link.connected ? "ok" : "offline"} size="sm" pulse={!link.connected}>
        {link.connected ? "CORE ONLINE" : "CORE OFFLINE"}
      </StatusPill>

      <span className="min-w-0 truncate font-mono text-[10px] text-ink-dim" title={linkSentence(link)}>
        {linkSentence(link)}
      </span>

      {!link.connected && (
        <IconButton name="refresh" label="Reconnect to the core now" onClick={reconnect} size={14} />
      )}

      {link.gap && (
        <span className="flex items-center gap-1.5 font-mono text-[10px] text-warn">
          <Icon name="alert" size={12} />
          Some events were missed — the log below them may be incomplete.
          <IconButton name="close" label="Dismiss the missed-events notice" onClick={dismissGap} size={12} />
        </span>
      )}

      <div className="ml-auto flex items-center gap-3">
        {runningTools.length > 0 && (
          <span
            className="flex items-center gap-1.5 font-mono text-[10px] text-accent"
            title={runningTools.map((item) => item.description || item.title).join("\n")}
          >
            <Icon name="cpu" size={12} />
            {runningTools.length === 1
              ? runningTools[0]?.title
              : `${runningTools.length} tools running`}
          </span>
        )}

        {running && turn && (
          <span className="font-mono text-[10px] text-ink-soft">{turnSentence(turn)}</span>
        )}

        {tools && (
          <span
            className="font-mono text-[10px] text-ink-faint"
            title={
              tools.unavailable.length > 0
                ? tools.unavailable.map((item) => `${item.name}: ${item.reason}`).join("\n")
                : "Every registered tool reports itself available."
            }
          >
            {tools.available}/{tools.total} TOOLS
          </span>
        )}

        <span className="font-mono text-[10px] text-ink-faint" title="Last event sequence number seen">
          SEQ {link.lastSeq}
        </span>
      </div>
    </footer>
  );
}
