/**
 * The screens that exist, and only those (§13, §56, §63).
 *
 * §63 names sixteen screens. This list is shorter, and the difference is
 * deliberate rather than unfinished-feeling: §13 says "use only tabs that
 * correspond to actual implemented functionality", and §56 forbids a control that
 * does nothing. A nav entry is a control. Shipping `SOUL`, `DEVICES`,
 * `NETWORK / WORLD VIEW`, `LIVE HEADLINES`, `DAILY BRIEF` and `SKILLS` today would
 * mean six buttons that open a blank panel, because the core exposes no route
 * behind any of them — there is no feed source and no device registry endpoint. A
 * blank panel with a real name on it is worse than an absent name: it reads as
 * broken software rather than as software that has not been built yet.
 *
 * `AUTOMATIONS` used to be on that list and is not any more. It is here because the
 * thing it was waiting for now exists: `nexus.automations` fires due rules on a
 * loop and `/api/automations` reads and writes them. Moving it is not bookkeeping —
 * an entry in `PENDING_SCREENS` is rendered to the user as a statement of fact, and
 * leaving it there would have been this file telling the user NEXUS cannot do
 * something it does.
 *
 * `PENDING_SCREENS` at the bottom keeps the honest record of which ones are
 * missing and what each is waiting for, so this is a documented gap and not a
 * silent omission. They appear in Settings, as text, next to what they need.
 *
 * Kept as plain data with no component imports so the nav rail, the command
 * palette and the router can all read it without a cycle.
 */

import type { IconName } from "../components/Icon";

export type ScreenId =
  | "core"
  | "conversations"
  | "memory"
  | "tasks"
  | "projects"
  | "awareness"
  | "automations"
  | "tools"
  | "models"
  | "permissions"
  | "activity"
  | "voice"
  | "settings";

export type ScreenDef = {
  id: ScreenId;
  /** The uppercase name shown in the rail and the title bar. */
  label: string;
  icon: IconName;
  /** One line, for the command palette and the rail tooltip. */
  purpose: string;
  /**
   * Which section of the rail it sits in. `work` is what the user is doing,
   * `machine` is what NEXUS is doing to the computer, `system` is configuration.
   */
  group: "core" | "work" | "machine" | "system";
};

export const SCREENS: ScreenDef[] = [
  {
    id: "core",
    label: "NEXUS CORE",
    icon: "core",
    purpose: "The voice sphere, live state, and what NEXUS is doing right now.",
    group: "core",
  },
  {
    id: "conversations",
    label: "CONVERSATIONS",
    icon: "conversations",
    purpose: "Every exchange, with the model that answered and the tools it ran.",
    group: "work",
  },
  {
    id: "memory",
    label: "MEMORY",
    icon: "memory",
    purpose: "What NEXUS remembers, what it proposed, and whether it can be found by meaning.",
    group: "work",
  },
  {
    id: "tasks",
    label: "TASKS",
    icon: "tasks",
    purpose: "Multi-step work: the plan, and what actually happened.",
    group: "work",
  },
  {
    id: "projects",
    label: "PROJECTS",
    icon: "projects",
    purpose: "Grouping conversations, tasks and memories by what they are for.",
    group: "work",
  },
  {
    id: "awareness",
    label: "AWARENESS",
    icon: "vision",
    purpose: "What NEXUS is watching, and the timeline of what it noticed.",
    group: "machine",
  },
  {
    id: "automations",
    label: "AUTOMATIONS",
    icon: "automations",
    purpose: "Rules that fire on their own, and whether the scheduler is running at all.",
    group: "machine",
  },
  {
    id: "tools",
    label: "TOOLS",
    icon: "cpu",
    purpose: "Every capability NEXUS has on this machine, and which are unavailable.",
    group: "machine",
  },
  {
    id: "models",
    label: "MODELS",
    icon: "network",
    purpose: "Which model does which job, why, and what happens when one is missing.",
    group: "machine",
  },
  {
    id: "permissions",
    label: "PERMISSIONS",
    icon: "permissions",
    purpose: "Standing grants, and the audited record of everything that ran.",
    group: "machine",
  },
  {
    id: "activity",
    label: "ACTIVITY",
    icon: "activity",
    purpose: "One chronological log of everything NEXUS has done.",
    group: "machine",
  },
  {
    id: "voice",
    label: "VOICE",
    icon: "wave",
    purpose: "Speech in and out: which models, and what works in this shell.",
    group: "system",
  },
  {
    id: "settings",
    label: "SETTINGS",
    icon: "settings",
    purpose: "Configuration, storage, retention, and what is not built yet.",
    group: "system",
  },
];

export const SCREEN_BY_ID: Record<ScreenId, ScreenDef> = Object.fromEntries(
  SCREENS.map((screen) => [screen.id, screen]),
) as Record<ScreenId, ScreenDef>;

/**
 * The §63 screens that are not here, and the exact reason each one is not.
 *
 * Rendered in Settings verbatim. The point is that "NEXUS does not have a Daily
 * Brief screen" and "NEXUS has a Daily Brief screen that shows nothing" are very
 * different claims, and only the first one is true.
 */
export const PENDING_SCREENS: Array<{ label: string; needs: string }> = [
  {
    label: "LIVE HEADLINES",
    needs:
      "A configured news source. §30 forbids invented headlines, and the core has no feed reader — " +
      "so there is nothing truthful to put on this screen yet.",
  },
  {
    label: "DAILY BRIEF",
    needs:
      "A brief generator. It would compose from memory, activity and headlines, and the third of " +
      "those does not exist.",
  },
  {
    label: "DEVICES",
    needs:
      "Device routes. The database has `devices` and `device_capabilities` tables, but nothing " +
      "registers a device and no endpoint reads them.",
  },
  {
    label: "NETWORK / WORLD VIEW",
    needs: "The devices layer above, plus a reachability probe. Nothing to draw a graph from yet.",
  },
  {
    label: "SKILLS",
    needs:
      "A skill registry distinct from the tool registry. Today a skill *is* a tool — see TOOLS, " +
      "which is real.",
  },
  {
    label: "SOUL",
    needs:
      "Personality configuration exists in the core as response modes and is editable in " +
      "SETTINGS. A separate screen would be a second place to change one thing.",
  },
];
