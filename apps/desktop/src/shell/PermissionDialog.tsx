/**
 * The permission dialog (§41, §65, §92, §97).
 *
 * This is the only modal in NEXUS, and it is modal because a tool call is *blocked*
 * on it: somewhere in the core a coroutine is awaiting a future that this dialog
 * resolves. Everything about it is shaped by that.
 *
 * What it must show, and why each is not optional:
 *
 * - **The exact consequence, in the core's own words.** `PendingRequest.consequence`
 *   is written per capability by the permission engine. Rewriting it here would mean
 *   the audit log records one sentence and the user was shown another.
 * - **The real parameters.** A dialog that says "NEXUS wants to write a file" without
 *   naming the file is not consent. They are rendered verbatim, as JSON, because a
 *   summarised path is a path the user cannot check.
 * - **Every grant kind the engine offers, and nothing else.** `grant_kinds` comes
 *   from the capability, so a capability that refuses standing grants shows no
 *   "Always" button rather than one that gets rejected.
 * - **Risk as a word, not a colour.** §92. `RISK_WORD` spells it out.
 *
 * Two behaviours that look like missing features and are not:
 *
 * 1. **No Escape-to-dismiss, no backdrop close.** Dismissing would leave the call
 *    blocked with no answer coming, and the user would have "closed a box" while a
 *    tool sat waiting for up to the 180-second timeout. The way out is Deny, which is
 *    also what the timeout does — it fails closed.
 * 2. **`second_confirmation` gets a second, typed step.** For destructive
 *    capabilities the engine flags it, and the dialog asks again with the consequence
 *    restated. A single click is not enough for something irreversible.
 */

import { useEffect, useMemo, useState } from "react";

import { Icon } from "../components/Icon";
import {
  ActionResult,
  Button,
  Dialog,
  Field,
  StatusPill,
  TextInput,
} from "../components/primitives";
import type { PendingRequest, Risk } from "../lib/api";
import { errorSentence, useNexus, usePermissionPrompt } from "../state/nexus";

/** The engine's six choices, in the order they should be read. */
type Choice = "deny" | "deny_always" | "once" | "session" | "always" | "constrained";

const CHOICE_LABEL: Record<Choice, string> = {
  deny: "Deny",
  deny_always: "Never allow this",
  once: "Allow once",
  session: "Allow for this session",
  always: "Always allow",
  constrained: "Allow with limits",
};

const CHOICE_DETAIL: Record<Choice, string> = {
  deny: "This call does not run. NEXUS is told it was refused and continues without it.",
  deny_always: "This call does not run, and NEXUS stops asking for this capability.",
  once: "Runs this one call. The next one asks again.",
  session: "Runs without asking again until the core restarts. Nothing is written to disk.",
  always: "Runs without asking again, saved permanently. Revocable in PERMISSIONS.",
  constrained: "Runs within limits you set. The limits are stored with the grant.",
};

const RISK_WORD: Record<Risk, string> = {
  low: "LOW RISK",
  medium: "MEDIUM RISK",
  high: "HIGH RISK",
  critical: "CRITICAL",
};

const RISK_STATE: Record<Risk, "ok" | "pending" | "waiting" | "failed"> = {
  low: "ok",
  medium: "pending",
  high: "waiting",
  critical: "failed",
};

/** Grant kinds that widen beyond this one call, so they need the extra step. */
const STANDING: Choice[] = ["session", "always", "deny_always"];

/**
 * The four limits the engine actually enforces, and the capability each applies to.
 *
 * Taken from `PermissionEngine._check_constraints`: `max_bytes` is only read for
 * `file.write`, `commands` only for `terminal.execute`, `paths` for any call carrying
 * a `path`, `hosts` for any call carrying a `url`. Offering a limit the engine would
 * ignore would be a control that does nothing (§56), so the editor below shows only
 * the ones that will actually be checked for *this* request.
 */
type LimitKind = "paths" | "commands" | "hosts" | "max_bytes";

function limitsFor(request: PendingRequest): LimitKind[] {
  const kinds: LimitKind[] = [];
  if (typeof request.params.path === "string") kinds.push("paths");
  if (request.capability === "terminal.execute") kinds.push("commands");
  if (typeof request.params.url === "string") kinds.push("hosts");
  if (request.capability === "file.write") kinds.push("max_bytes");
  return kinds;
}

/** The program name a command scopes to — `policy.scope_for_command`, client side. */
function programOf(command: string): string {
  const tokens = command.trim().split(/\s+/).filter(Boolean);
  for (const token of tokens) {
    if (/^\w+=/.test(token) && !token.startsWith("-")) continue;
    const parts = token.split("/");
    return parts[parts.length - 1] ?? "";
  }
  return "";
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.toLowerCase();
  } catch {
    return "";
  }
}

function parentOf(path: string): string {
  const trimmed = path.replace(/\/+$/, "");
  const cut = trimmed.lastIndexOf("/");
  return cut > 0 ? trimmed.slice(0, cut) : trimmed || "/";
}

export function PermissionDialog() {
  const request = usePermissionPrompt();
  const { pending, respond, withdraw } = useNexus();

  const [busy, setBusy] = useState<Choice | "withdraw" | "">("");
  const [failed, setFailed] = useState("");
  const [confirming, setConfirming] = useState<Choice | null>(null);
  const [limiting, setLimiting] = useState(false);

  // A new request at the head of the queue is a different question. Any half-made
  // decision about the previous one must not carry over onto it — approving the
  // wrong call is the failure this whole surface exists to prevent.
  const id = request?.id ?? "";
  useEffect(() => {
    setBusy("");
    setFailed("");
    setConfirming(null);
    setLimiting(false);
  }, [id]);

  const limits = useMemo(() => (request ? limitsFor(request) : []), [request]);

  if (!request) return null;

  const answer = async (choice: Choice, constraints?: Record<string, unknown>) => {
    setFailed("");
    setBusy(choice);
    try {
      await respond(request.id, choice, constraints);
    } catch (cause) {
      setFailed(errorSentence(cause));
    } finally {
      setBusy("");
      setConfirming(null);
      setLimiting(false);
    }
  };

  const press = (choice: Choice) => {
    // Denying goes straight through. It is the safe answer, and making the safe
    // answer take more clicks than the dangerous one is exactly backwards — under
    // time pressure the user would learn to click the easy button.
    if (choice === "deny") {
      void answer("deny");
      return;
    }
    // A constrained grant is meaningless without its limits — the engine refuses one
    // with an empty `constraints` dict — so this choice opens the editor instead of
    // answering, and the answer is sent from there.
    if (choice === "constrained") {
      setLimiting(true);
      return;
    }
    // A second step for two reasons, either sufficient: the engine flagged this
    // capability as needing one, or the choice grants beyond this single call.
    const flagged = request.second_confirmation || request.destructive;
    if (flagged || STANDING.includes(choice)) {
      setConfirming(choice);
      return;
    }
    void answer(choice);
  };

  const drop = async () => {
    setFailed("");
    setBusy("withdraw");
    try {
      await withdraw(request.id);
    } catch (cause) {
      setFailed(errorSentence(cause));
    } finally {
      setBusy("");
    }
  };

  const offered = orderChoices(request.grant_kinds, limits.length > 0);
  const queued = pending.length - 1;

  return (
    <Dialog
      open
      // Required by the type, and deliberately not a dismissal: closing without an
      // answer would leave the tool call blocked until the engine's timeout. The only
      // ways out are the buttons, and Deny is the leftmost of them.
      onClose={() => {}}
      dismissable={false}
      title="NEXUS IS ASKING FOR PERMISSION"
      width="md"
      footer={
        <div className="flex w-full items-center gap-2">
          <span className="font-mono text-[10px] text-ink-faint">
            {queued > 0
              ? `${queued} more request${queued === 1 ? "" : "s"} waiting behind this one.`
              : "Nothing else is waiting."}
          </span>
          <div className="ml-auto flex items-center gap-2">
            <Button
              tone="ghost"
              size="sm"
              onClick={drop}
              busy={busy === "withdraw"}
              title="Cancel the request itself, as if it had never been made"
            >
              Withdraw
            </Button>
            <Button
              tone="danger"
              size="sm"
              icon="close"
              onClick={() => press("deny")}
              busy={busy === "deny"}
            >
              Deny
            </Button>
          </div>
        </div>
      }
    >
      <div className="flex flex-col gap-3">
        <header className="flex flex-wrap items-center gap-2">
          <StatusPill state={RISK_STATE[request.risk]} size="sm">
            {RISK_WORD[request.risk]}
          </StatusPill>
          <StatusPill state={request.reversible ? "ok" : "failed"} size="sm">
            {request.reversible ? "REVERSIBLE" : "NOT REVERSIBLE"}
          </StatusPill>
          {request.destructive && (
            <StatusPill state="failed" size="sm">
              DESTRUCTIVE
            </StatusPill>
          )}
          <span className="ml-auto font-mono text-[10px] text-ink-faint">{request.group}</span>
        </header>

        <div>
          <h3 className="text-sm font-medium text-ink">{request.title}</h3>
          <p className="mt-1 text-xs leading-relaxed text-ink-soft">{request.description}</p>
        </div>

        <div className="chamfer-sm border border-line-bright bg-base p-2.5">
          <span className="label">What this does</span>
          <p className="mt-1 text-xs leading-relaxed text-ink">{request.consequence}</p>
        </div>

        <Parameters params={request.params} />

        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
          <Meta label="Capability" value={request.capability} />
          <Meta label="Scope" value={request.scope} />
          {request.task_id && <Meta label="Task" value={request.task_id} />}
          {request.conversation_id && <Meta label="Conversation" value={request.conversation_id} />}
        </dl>

        {limiting ? (
          <Limits
            request={request}
            kinds={limits}
            busy={busy === "constrained"}
            onBack={() => setLimiting(false)}
            onConfirm={(constraints) => void answer("constrained", constraints)}
          />
        ) : confirming ? (
          <SecondStep
            choice={confirming}
            request={request}
            busy={busy === confirming}
            onBack={() => setConfirming(null)}
            onConfirm={() => void answer(confirming)}
          />
        ) : (
          <div className="flex flex-col gap-1.5">
            <span className="label">Your answer</span>
            {offered.map((choice) => (
              <button
                key={choice}
                type="button"
                onClick={() => press(choice)}
                disabled={busy !== ""}
                className="chamfer-sm flex items-start gap-2.5 border border-line-bright bg-raised px-2.5 py-2 text-left transition-colors hover:border-accent hover:bg-accent/8 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Icon
                  name={choice.startsWith("deny") ? "close" : "check"}
                  size={14}
                  className={`mt-0.5 shrink-0 ${choice.startsWith("deny") ? "text-danger" : "text-accent"}`}
                />
                <span className="min-w-0">
                  <span className="block text-xs text-ink">{CHOICE_LABEL[choice]}</span>
                  <span className="block text-[11px] leading-relaxed text-ink-dim">
                    {CHOICE_DETAIL[choice]}
                  </span>
                </span>
              </button>
            ))}
          </div>
        )}

        {failed && <ActionResult done="" failed={failed} onClear={() => setFailed("")} />}

        <p className="font-mono text-[10px] leading-relaxed text-ink-faint">
          If nothing is answered, the core denies this call when it times out. Silence is
          never taken as approval.
        </p>
      </div>
    </Dialog>
  );
}

/**
 * The engine's grant kinds, in escalation order.
 *
 * Filtered twice. First against what this capability actually offers, rather than
 * shown wholesale: a capability with no standing grants must not show "Always allow",
 * because the engine would reject it and the user would have been offered something
 * imaginary. Second against `canConstrain` — "Allow with limits" is dropped when none
 * of the four limits the engine enforces applies to this call, since a constrained
 * grant with nothing in it is refused by `respond` ("A constrained grant needs the
 * limits it should apply").
 *
 * `deny` is excluded because it is a footer button, always present.
 */
function orderChoices(kinds: string[], canConstrain: boolean): Choice[] {
  const order: Choice[] = ["once", "constrained", "session", "always", "deny_always"];
  return order.filter((choice) => {
    if (!kinds.includes(choice)) return false;
    if (choice === "constrained") return canConstrain;
    return true;
  });
}

function SecondStep({
  choice,
  request,
  busy,
  onBack,
  onConfirm,
}: {
  choice: Choice;
  request: PendingRequest;
  busy: boolean;
  onBack: () => void;
  onConfirm: () => void;
}) {
  const widens = STANDING.includes(choice);
  return (
    <div className="chamfer-sm border border-warn/50 bg-warn/8 p-2.5">
      <span className="label text-warn">Confirm</span>
      <p className="mt-1 text-xs leading-relaxed text-ink">
        {CHOICE_LABEL[choice]}: {request.consequence}
      </p>
      {widens && (
        <p className="mt-1.5 text-[11px] leading-relaxed text-ink-soft">
          {choice === "session"
            ? `This applies to every later ${request.capability} call on ${request.scope} until the core restarts, without asking again.`
            : choice === "always"
              ? `This is stored on disk and applies to every later ${request.capability} call on ${request.scope}. You can revoke it in PERMISSIONS.`
              : `NEXUS will stop asking for ${request.capability} and will refuse it silently from now on.`}
        </p>
      )}
      {!request.reversible && (
        <p className="mt-1.5 text-[11px] leading-relaxed text-danger">
          This cannot be undone once it runs.
        </p>
      )}
      <div className="mt-2.5 flex items-center gap-2">
        <Button size="sm" tone="ghost" onClick={onBack} disabled={busy} reason="Answering.">
          Back
        </Button>
        <Button
          size="sm"
          tone={choice.startsWith("deny") ? "danger" : "accent"}
          onClick={onConfirm}
          busy={busy}
        >
          {CHOICE_LABEL[choice]}
        </Button>
      </div>
    </div>
  );
}

/**
 * The limits editor for a constrained grant (§7).
 *
 * Prefilled from the call itself — the folder the file is in, the program the command
 * runs, the host the URL points at — because those are the limits the user almost
 * always means, and typing a path by hand is where a typo becomes a grant that covers
 * more than intended. Everything is editable and everything is shown, so the widening
 * is visible before it is stored.
 *
 * Only the four keys `PermissionEngine._check_constraints` reads are ever sent. A
 * fifth would be stored on the grant and then silently never enforced, which would be
 * a limit the user believes in and the engine does not (§78).
 */
function Limits({
  request,
  kinds,
  busy,
  onBack,
  onConfirm,
}: {
  request: PendingRequest;
  kinds: LimitKind[];
  busy: boolean;
  onBack: () => void;
  onConfirm: (constraints: Record<string, unknown>) => void;
}) {
  const path = typeof request.params.path === "string" ? request.params.path : "";
  const command = typeof request.params.command === "string" ? request.params.command : "";
  const url = typeof request.params.url === "string" ? request.params.url : "";

  const [paths, setPaths] = useState(() => (path ? parentOf(path) : ""));
  const [commands, setCommands] = useState(() => programOf(command));
  const [hosts, setHosts] = useState(() => hostOf(url));
  const [maxBytes, setMaxBytes] = useState("");

  const constraints: Record<string, unknown> = {};
  if (kinds.includes("paths") && paths.trim()) {
    constraints.paths = paths
      .split(",")
      .map((entry) => entry.trim())
      .filter(Boolean);
  }
  if (kinds.includes("commands") && commands.trim()) {
    constraints.commands = commands
      .split(",")
      .map((entry) => entry.trim())
      .filter(Boolean);
  }
  if (kinds.includes("hosts") && hosts.trim()) {
    constraints.hosts = hosts
      .split(",")
      .map((entry) => entry.trim().toLowerCase())
      .filter(Boolean);
  }
  if (kinds.includes("max_bytes")) {
    const parsed = Number.parseInt(maxBytes, 10);
    if (Number.isFinite(parsed) && parsed > 0) constraints.max_bytes = parsed;
  }

  const empty = Object.keys(constraints).length === 0;

  return (
    <div className="chamfer-sm border border-accent/50 bg-accent/6 p-2.5">
      <span className="label label-accent">Limits this grant will enforce</span>
      <p className="mt-1 text-[11px] leading-relaxed text-ink-soft">
        The grant is stored with these and every later call is checked against them. A
        call outside them is refused without asking you again.
      </p>

      <div className="mt-2.5 flex flex-col gap-2.5">
        {kinds.includes("paths") && (
          <Field
            label="Folders"
            hint={`Comma separated. Anything inside these is covered. This call touches ${path}`}
          >
            <TextInput value={paths} onChange={setPaths} placeholder="/home/you/projects" />
          </Field>
        )}
        {kinds.includes("commands") && (
          <Field
            label="Programs"
            hint={
              command
                ? `Comma separated program names, not full commands. This call runs: ${command}`
                : "Comma separated program names, not full commands."
            }
          >
            <TextInput value={commands} onChange={setCommands} placeholder="git, ls" />
          </Field>
        )}
        {kinds.includes("hosts") && (
          <Field label="Websites" hint={`Comma separated hostnames. This call reaches ${hostOf(url) || url}`}>
            <TextInput value={hosts} onChange={setHosts} placeholder="example.com" />
          </Field>
        )}
        {kinds.includes("max_bytes") && (
          <Field
            label="Largest write"
            hint="In bytes. Leave empty for no size limit — the other limits still apply."
          >
            <TextInput type="number" value={maxBytes} onChange={setMaxBytes} placeholder="65536" />
          </Field>
        )}
      </div>

      <div className="mt-2.5 flex items-center gap-2">
        <Button size="sm" tone="ghost" onClick={onBack} disabled={busy} reason="Answering.">
          Back
        </Button>
        <Button
          size="sm"
          tone="accent"
          onClick={() => onConfirm(constraints)}
          busy={busy}
          disabled={empty}
          reason="Fill in at least one limit — the core refuses a constrained grant with nothing in it."
        >
          Allow with these limits
        </Button>
      </div>
    </div>
  );
}

/**
 * The call's arguments, verbatim.
 *
 * Pretty-printed JSON rather than a friendly summary: this is the part the user is
 * actually consenting to, and any transformation is a chance to show something other
 * than what will run. The engine has already redacted what should not be displayed —
 * doing it again here would be second-guessing the layer that knows.
 */
function Parameters({ params }: { params: Record<string, unknown> }) {
  const keys = Object.keys(params);
  if (keys.length === 0) {
    return (
      <div className="chamfer-sm border border-line bg-base p-2.5">
        <span className="label">Parameters</span>
        <p className="mt-1 font-mono text-[11px] text-ink-dim">
          None. This call takes no arguments.
        </p>
      </div>
    );
  }
  return (
    <div className="chamfer-sm border border-line bg-base p-2.5">
      <span className="label">Parameters — exactly what will run</span>
      <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed text-ink">
        {JSON.stringify(params, null, 2)}
      </pre>
    </div>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="label">{label}</dt>
      <dd className="min-w-0 truncate font-mono text-[11px] text-ink-soft">{value}</dd>
    </>
  );
}
