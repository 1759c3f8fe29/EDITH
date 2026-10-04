/**
 * The navigation rail (§13, §49, §56, §63).
 *
 * Reads `SCREENS` and renders exactly what is in it — so the rail cannot drift from
 * the router, and a screen that has no route behind it cannot appear here by
 * accident. Grouped, because "what I am doing", "what NEXUS is doing to this
 * machine", and "configuration" are genuinely different kinds of destination and a
 * flat list of twelve makes the user re-read all twelve every time.
 *
 * Two details that matter more than they look:
 *
 * - **The active entry carries the accent and a bar, not just a colour.** §92: colour
 *   never means anything on its own. The left bar and `aria-current` are the other
 *   two channels.
 * - **Badges are real counts or absent.** The permission badge is `pending.length`;
 *   when nothing is pending there is no badge at all rather than a `0`, which reads
 *   as a control with a value.
 */

import { Icon } from "../components/Icon";
import { useNexus } from "../state/nexus";
import { SCREENS, type ScreenDef, type ScreenId } from "../state/screens";

const GROUP_LABEL: Record<ScreenDef["group"], string> = {
  core: "",
  work: "Work",
  machine: "Machine",
  system: "System",
};

const ORDER: Array<ScreenDef["group"]> = ["core", "work", "machine", "system"];

export function NavRail() {
  const { screen, go, pending, runningTools } = useNexus();

  return (
    <nav aria-label="NEXUS sections" className="flex w-[184px] shrink-0 flex-col gap-4 py-3">
      {ORDER.map((group) => {
        const entries = SCREENS.filter((item) => item.group === group);
        if (entries.length === 0) return null;
        return (
          <div key={group} className="flex flex-col">
            {GROUP_LABEL[group] && (
              <span className="label px-3 pb-1.5 text-ink-faint">{GROUP_LABEL[group]}</span>
            )}
            {entries.map((entry) => (
              <RailItem
                key={entry.id}
                entry={entry}
                active={entry.id === screen}
                badge={badgeFor(entry.id, pending.length, runningTools.length)}
                onSelect={() => go(entry.id)}
              />
            ))}
          </div>
        );
      })}
    </nav>
  );
}

function badgeFor(id: ScreenId, pendingCount: number, runningCount: number): number {
  if (id === "permissions") return pendingCount;
  if (id === "tools") return runningCount;
  return 0;
}

function RailItem({
  entry,
  active,
  badge,
  onSelect,
}: {
  entry: ScreenDef;
  active: boolean;
  badge: number;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-current={active ? "page" : undefined}
      title={entry.purpose}
      className={[
        "group flex items-center gap-2.5 border-l-2 py-2 pl-2.5 pr-2 text-left transition-colors duration-100",
        active
          ? "border-l-accent bg-accent/8 text-accent"
          : "border-l-transparent text-ink-dim hover:bg-raised hover:text-ink-soft",
      ].join(" ")}
    >
      <Icon name={entry.icon} size={15} className="shrink-0" />
      <span className="label truncate text-current">{entry.label}</span>
      {badge > 0 && (
        <span className="chamfer-sm ml-auto shrink-0 bg-accent/20 px-1 font-mono text-[9px] text-accent">
          {badge}
        </span>
      )}
    </button>
  );
}
