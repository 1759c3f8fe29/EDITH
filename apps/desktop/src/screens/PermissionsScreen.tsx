/**
 * PERMISSIONS - the leash, and the log that proves it held (§43, §45, §46, §97).
 *
 * The screen is shaped by one thing the API deliberately cannot do: **there is no route
 * that grants a capability.** A grant is only ever the answer to a request the engine
 * itself raised, so a compromised page cannot authorise anything. Everything here is
 * therefore read, revoke, or answer - never "allow this in advance", because that button
 * would need an endpoint that does not and should not exist.
 *
 * What follows from that, and what the screen commits to:
 *
 * - **Revocation is always available.** Taking permission away must never require a
 *   prompt to hang off (§45), which is why the revoke buttons work with nothing pending.
 * - **Four states, not two.** A capability can be implicitly allowed, stored-allowed,
 *   session-allowed, stored-*denied*, or unanswered. A denied capability is not the same
 *   as an un-granted one - one will never prompt again - so they are drawn apart.
 * - **`expired` comes from the core.** The engine computes it against its own clock and
 *   fails closed on an unparseable date. Comparing `expires_at` to the browser clock
 *   would let this screen show a live grant the engine will refuse.
 * - **Dismissing is not denying.** Withdrawing a request writes no deny grant, so the
 *   same action can be asked about again. The two buttons say which is which.
 * - **The log is the other half of the leash (§46).** Permission decides whether
 *   something may happen; the invocation list records that it did. Both halves are here.
 */

import { useState } from "react";

import { Icon } from "../components/Icon";
import {
  ActionResult,
  Button,
  ConfirmDialog,
  DataRow,
  EmptyState,
  ErrorState,
  LoadingState,
  Panel,
  Section,
  Select,
  StatusPill,
  Tabs,
  clockTime,
  duration,
  relativeTime,
} from "../components/primitives";
import {
  api,
  type Capability,
  type Grant,
  type Invocation,
  type PendingRequest,
  type Risk,
  type SessionGrant,
} from "../lib/api";
import { useAction, useAsync } from "../lib/useAsync";
import { useNexus } from "../state/nexus";

/** Risk maps onto the shared state vocabulary so the colours mean the same thing here. */
const RISK_STATE: Record<Risk, "ok" | "pending" | "warn" | "failed"> = {
  low: "ok",
  medium: "pending",
  high: "warn",
  critical: "failed",
};

type Tab = "capabilities" | "grants" | "log";

const TABS = [
  { value: "capabilities" as const, label: "Capabilities" },
  { value: "grants" as const, label: "What is allowed" },
  { value: "log" as const, label: "What ran" },
];

/** How a grant kind reads to someone who did not write the engine. */
const KIND_WORDS: Record<string, string> = {
  deny: "refused - NEXUS will not ask again",
  once: "allowed once, already used",
  session: "allowed until the core restarts",
  always: "allowed until you revoke it",
  constrained: "allowed within limits you set",
};

export function PermissionsScreen() {
  const { pending } = useNexus();
  const [tab, setTab] = useState<Tab>("capabilities");
  const overview = useAsync(() => api.permissions.overview(), []);

  if (overview.state === "loading") {
    return (
      <div className="h-full p-2">
        <Panel title="PERMISSIONS" className="min-h-0">
          <LoadingState what="the permission catalogue" />
        </Panel>
      </div>
    );
  }

  if (!overview.data) {
    return (
      <div className="h-full p-2">
        <Panel title="PERMISSIONS" className="min-h-0">
          <div className="p-2">
            <ErrorState
              what={overview.error ?? "The permission catalogue did not load."}
              did="Nothing is listed. An empty list would say nothing is allowed, and this screen cannot claim that without reading the grants."
              next={overview.offline ? "Start the core, then retry." : overview.detail || "Retry."}
              offline={overview.offline}
              onRetry={overview.reload}
            />
          </div>
        </Panel>
      </div>
    );
  }

  const data = overview.data;
  const byCapability = data.overview.grants_by_capability;
  const sessionKeys = new Set(
    data.session_grants.map((grant) => `${grant.capability} ${grant.scope}`),
  );
  const live = data.grants.filter((grant) => !grant.expired && grant.grant_kind !== "deny");
  const denied = data.grants.filter((grant) => grant.grant_kind === "deny");

  return (
    <div className="flex h-full min-h-0 flex-col gap-2 p-2">
      <Panel
        title="PERMISSIONS"
        meta={`${live.length} allowed · ${data.session_grants.length} this session · ${denied.length} refused`}
        className="min-h-0 flex-1"
        bodyClassName="flex min-h-0 flex-1 flex-col"
        actions={
          <Button
            size="sm"
            tone="ghost"
            icon="refresh"
            onClick={overview.reload}
            busy={overview.refreshing}
          >
            Re-read
          </Button>
        }
      >
        <Tabs<Tab>
          tabs={TABS}
          value={tab}
          onChange={setTab}
          meta={
            pending.length > 0 ? (
              <StatusPill state="pending" size="sm" pulse>
                {pending.length} WAITING ON YOU
              </StatusPill>
            ) : undefined
          }
        />

        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          {pending.length > 0 && <PendingQueue pending={pending} />}

          {tab === "capabilities" && (
            <CapabilitiesTab
              groups={data.groups}
              byCapability={byCapability}
              sessionKeys={sessionKeys}
              onChanged={overview.reload}
            />
          )}
          {tab === "grants" && (
            <GrantsTab
              grants={data.grants}
              sessionGrants={data.session_grants}
              onChanged={overview.reload}
            />
          )}
          {tab === "log" && <LogTab />}
        </div>
      </Panel>
    </div>
  );
}

/* ------------------------------------------------------------------ pending */

/**
 * Requests waiting for an answer, listed rather than only shown in the dialog.
 *
 * The modal answers one at a time, oldest first, because stacking modals makes it easy
 * to approve the wrong thing. That is the right dialog behaviour and the wrong list
 * behaviour: a user who wants to know what is blocked needs to see all of it. Each row
 * is holding a real tool call open, so the waiting time is real and is shown.
 */
function PendingQueue({ pending }: { pending: PendingRequest[] }) {
  const { respond, withdraw } = useNexus();
  const [busy, setBusy] = useState("");
  const [failed, setFailed] = useState("");

  async function answer(id: string, choice: string) {
    setBusy(`${id}:${choice}`);
    setFailed("");
    try {
      await respond(id, choice);
    } catch (error) {
      setFailed(error instanceof Error ? error.message : "That answer was not recorded.");
    } finally {
      setBusy("");
    }
  }

  async function dismiss(id: string) {
    setBusy(`${id}:withdraw`);
    setFailed("");
    try {
      await withdraw(id);
    } catch (error) {
      setFailed(error instanceof Error ? error.message : "The request could not be withdrawn.");
    } finally {
      setBusy("");
    }
  }

  return (
    <Section title="Waiting on you" meta={`${pending.length}`}>
      <p className="border-b border-line/60 px-3 py-1.5 text-[11px] leading-relaxed text-ink-soft">
        Each of these is a tool call held open, waiting. The dialog asks about the oldest
        one; this list is all of them.
      </p>
      <ul>
        {pending.map((request) => (
          <li key={request.id} className="border-b border-line/60 px-3 py-2 last:border-b-0">
            <div className="flex flex-wrap items-baseline gap-2">
              <StatusPill state={RISK_STATE[request.risk]} size="sm">
                {request.risk}
              </StatusPill>
              <span className="text-[11px] text-ink">{request.title}</span>
              {request.destructive && (
                <StatusPill state="failed" size="sm">
                  DESTRUCTIVE
                </StatusPill>
              )}
              {!request.reversible && (
                <StatusPill state="warn" size="sm">
                  CANNOT BE UNDONE
                </StatusPill>
              )}
              <span className="ml-auto font-mono text-[10px] text-ink-faint">
                asked {relativeTime(request.created_at)}
              </span>
            </div>
            <p className="mt-1 break-words text-[11px] leading-relaxed text-ink-soft">
              {request.description}
            </p>
            <p className="mt-1 text-[11px] leading-relaxed text-ink-dim">{request.consequence}</p>
            <p className="mt-1 font-mono text-[10px] text-ink-faint">
              {request.capability} · scope {request.scope || "*"}
              {request.task_id ? " · from a task" : ""}
            </p>
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
              {request.grant_kinds.map((kind) => (
                <Button
                  key={kind}
                  size="sm"
                  tone={kind === "always" ? "default" : "accent"}
                  onClick={() => void answer(request.id, kind)}
                  busy={busy === `${request.id}:${kind}`}
                >
                  {kind === "once"
                    ? "Allow once"
                    : kind === "session"
                      ? "Allow this session"
                      : "Always allow"}
                </Button>
              ))}
              <Button
                size="sm"
                tone="danger"
                onClick={() => void answer(request.id, "deny")}
                busy={busy === `${request.id}:deny`}
              >
                Deny
              </Button>
              <Button
                size="sm"
                tone="ghost"
                onClick={() => void dismiss(request.id)}
                busy={busy === `${request.id}:withdraw`}
                title="Withdraws the request without recording a refusal, so it can be asked again."
              >
                Withdraw
              </Button>
            </div>
            {request.second_confirmation && (
              <p className="mt-1 text-[11px] leading-relaxed text-warn">
                This one is critical and cannot be undone, so allowing it here still asks
                for a second confirmation before it runs.
              </p>
            )}
          </li>
        ))}
      </ul>
      <div className="px-3 py-1.5">
        <ActionResult done="" failed={failed} onClear={() => setFailed("")} />
      </div>
    </Section>
  );
}

/* ------------------------------------------------------------- capabilities */

/**
 * Every capability NEXUS has, grouped as the user reads them.
 *
 * Unanswered is the honest default and is drawn as "it will ask", not as "denied".
 * `implicit` capabilities are shown as already-allowed with the reason, because listing
 * them as though a prompt were coming would be a promise the engine will not keep.
 */
function CapabilitiesTab({
  groups,
  byCapability,
  sessionKeys,
  onChanged,
}: {
  groups: Array<{ name: string; capabilities: Capability[] }>;
  byCapability: Record<string, Grant[]>;
  sessionKeys: Set<string>;
  onChanged: () => void;
}) {
  const [risk, setRisk] = useState<"all" | Risk>("all");

  const shown = groups
    .map((group) => ({
      name: group.name,
      capabilities: group.capabilities.filter((cap) => risk === "all" || cap.risk === risk),
    }))
    .filter((group) => group.capabilities.length > 0);

  const total = groups.reduce((sum, group) => sum + group.capabilities.length, 0);

  return (
    <div className="space-y-2">
      <Section title="What NEXUS can be asked to do" meta={`${total} capabilities`}>
        <p className="px-3 py-2 text-[11px] leading-relaxed text-ink-soft">
          Nothing here can be allowed in advance. There is no route that writes a grant
          without a request behind it, on purpose: a page that could mint its own
          permission would not be a leash. What you can do from here is take permission
          away, which never needs a prompt to hang off.
        </p>
        <div className="flex items-center gap-2 px-3 pb-2">
          <div className="min-w-[180px]">
            <Select<"all" | Risk>
              value={risk}
              onChange={setRisk}
              options={[
                { value: "all", label: "Every risk level" },
                { value: "low", label: "Low risk" },
                { value: "medium", label: "Medium risk" },
                { value: "high", label: "High risk" },
                { value: "critical", label: "Critical" },
              ]}
            />
          </div>
        </div>
      </Section>

      {shown.length === 0 ? (
        <EmptyState
          icon="permissions"
          title="Nothing at that risk level"
          detail="No capability is registered with this risk. Try another level."
        />
      ) : (
        shown.map((group) => (
          <Section key={group.name} title={group.name} meta={`${group.capabilities.length}`}>
            <ul>
              {group.capabilities.map((cap) => (
                <li key={cap.name}>
                  <CapabilityRow
                    capability={cap}
                    grants={byCapability[cap.name] ?? []}
                    sessionScopes={[...sessionKeys]
                      .filter((key) => key.startsWith(`${cap.name} `))
                      .map((key) => key.slice(cap.name.length + 1))}
                    onChanged={onChanged}
                  />
                </li>
              ))}
            </ul>
          </Section>
        ))
      )}
    </div>
  );
}

function CapabilityRow({
  capability,
  grants,
  sessionScopes,
  onChanged,
}: {
  capability: Capability;
  grants: Grant[];
  sessionScopes: string[];
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const denied = grants.find((grant) => grant.grant_kind === "deny");
  const allowed = grants.filter((grant) => grant.grant_kind !== "deny" && !grant.expired);
  const expired = grants.filter((grant) => grant.grant_kind !== "deny" && grant.expired);

  const revoke = useAction(async () => {
    // Every scope, one call each: `revoke` is per (capability, scope) and there is no
    // wildcard form. Doing it in a loop is honest about that; a single call to `*` would
    // silently leave the scoped grants in place.
    const scopes = [...new Set([...grants.map((g) => g.scope), ...sessionScopes])];
    let removed = 0;
    for (const scope of scopes) {
      const result = await api.permissions.revoke(capability.name, scope);
      if (result.ok) removed += 1;
    }
    onChanged();
    return removed > 0
      ? `${capability.title} is no longer allowed (${removed} grant${removed === 1 ? "" : "s"} removed).`
      : "There was nothing stored to remove.";
  });

  return (
    <div className="border-b border-line/60 px-3 py-2 last:border-b-0">
      <div className="flex flex-wrap items-baseline gap-2">
        <StatusPill state={RISK_STATE[capability.risk]} size="sm">
          {capability.risk}
        </StatusPill>
        <span className="text-[11px] text-ink">{capability.title}</span>
        {capability.implicit ? (
          <StatusPill state="ok" size="sm">
            ALWAYS ALLOWED
          </StatusPill>
        ) : denied ? (
          <StatusPill state="failed" size="sm">
            REFUSED
          </StatusPill>
        ) : allowed.length > 0 ? (
          <StatusPill state="ok" size="sm">
            {allowed.length === 1 ? KIND_WORDS[allowed[0]!.grant_kind]?.toUpperCase() : "ALLOWED"}
          </StatusPill>
        ) : sessionScopes.length > 0 ? (
          <StatusPill state="ok" size="sm">
            THIS SESSION
          </StatusPill>
        ) : (
          <StatusPill state="idle" size="sm">
            IT WILL ASK
          </StatusPill>
        )}
        {!capability.reversible && (
          <StatusPill state="warn" size="sm">
            NOT REVERSIBLE
          </StatusPill>
        )}
        <button
          type="button"
          onClick={() => setOpen((current) => !current)}
          className="ml-auto flex shrink-0 items-center gap-1 font-mono text-[10px] uppercase tracking-wider text-ink-dim hover:text-ink-soft"
        >
          <Icon name={open ? "chevron-down" : "chevron-right"} size={10} />
          {open ? "less" : "detail"}
        </button>
      </div>
      <p className="mt-1 break-words text-[11px] leading-relaxed text-ink-soft">
        {capability.description}
      </p>

      {open && (
        <div className="mt-1.5 space-y-1.5 border-t border-line/60 pt-1.5">
          <p className="text-[11px] leading-relaxed text-ink-dim">{capability.consequence}</p>
          <div className="flex flex-col">
            <DataRow label="Name" value={capability.name} />
            <DataRow
              label="Can be granted"
              value={capability.grant_kinds.join(", ")}
              title="Higher risk offers fewer kinds. A critical capability can only ever be allowed once."
            />
            <DataRow
              label="Scoped"
              value={capability.scoped ? "yes - per target" : "no - all or nothing"}
              title="A scoped capability can be allowed for one path or app rather than everywhere."
            />
            {capability.second_confirmation && (
              <DataRow
                label="Second step"
                value="yes"
                state="warn"
                title="Critical and irreversible, so allowing it still asks again before it runs."
              />
            )}
          </div>

          {capability.implicit && (
            <p className="text-[11px] leading-relaxed text-ok">
              This is allowed without asking. It reads this machine's own state and
              changes nothing, so a prompt would be noise that trains you to click yes.
            </p>
          )}

          {denied && (
            <p className="text-[11px] leading-relaxed text-danger">
              You refused this{denied.scope !== "*" ? ` for ${denied.scope}` : ""}{" "}
              {relativeTime(denied.granted_at)}. NEXUS will not ask again until the
              refusal is removed.
            </p>
          )}

          {allowed.map((grant) => (
            <p key={grant.id} className="text-[11px] leading-relaxed text-ink-soft">
              {KIND_WORDS[grant.grant_kind] ?? grant.grant_kind}
              {grant.scope !== "*" ? ` · only for ${grant.scope}` : ""}
              {grant.expires_at ? ` · until ${clockTime(grant.expires_at)}` : ""}
            </p>
          ))}

          {expired.length > 0 && (
            <p className="text-[11px] leading-relaxed text-ink-dim">
              {expired.length} grant{expired.length === 1 ? "" : "s"} here{" "}
              {expired.length === 1 ? "has" : "have"} expired. The row is kept until it is
              revoked, but the engine treats it as gone.
            </p>
          )}

          {sessionScopes.length > 0 && (
            <p className="text-[11px] leading-relaxed text-ink-soft">
              Allowed for this session{" "}
              {sessionScopes.some((scope) => scope !== "*")
                ? `(${sessionScopes.join(", ")})`
                : ""}
              . Session grants are held in memory and disappear when the core restarts.
            </p>
          )}

          {(grants.length > 0 || sessionScopes.length > 0) && (
            <div>
              <Button size="sm" tone="danger" icon="lock" onClick={revoke.run} busy={revoke.busy}>
                {denied ? "Remove the refusal" : "Revoke"}
              </Button>
              <ActionResult done={revoke.done} failed={revoke.failed} onClear={revoke.clear} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------- grants */

/**
 * Only what is actually allowed, plus the two blunt instruments.
 *
 * Session grants are listed separately from stored ones because they are a different
 * kind of fact: they live in the process and vanish on restart (§7). Merging them would
 * make "allowed" look more permanent than it is, and would make "clear session" look
 * like it does nothing.
 */
function GrantsTab({
  grants,
  sessionGrants,
  onChanged,
}: {
  grants: Grant[];
  sessionGrants: SessionGrant[];
  onChanged: () => void;
}) {
  const [confirmAll, setConfirmAll] = useState(false);
  const stored = grants.filter((grant) => grant.grant_kind !== "deny");
  const denied = grants.filter((grant) => grant.grant_kind === "deny");

  const revokeAll = useAction(async () => {
    const result = await api.permissions.revokeAll();
    setConfirmAll(false);
    onChanged();
    return `${result.message} (${result.counts.stored ?? 0} stored, ${result.counts.session ?? 0} session.)`;
  });

  const clearSession = useAction(async () => {
    const result = await api.permissions.clearSession();
    onChanged();
    return result.message;
  });

  return (
    <div className="space-y-2">
      <Section title="Stored" meta={`${stored.length}`}>
        {stored.length === 0 ? (
          <p className="px-3 py-2 text-[11px] leading-relaxed text-ink-soft">
            Nothing is stored. NEXUS will ask before it does anything that needs
            permission, which is the state it ships in.
          </p>
        ) : (
          <ul>
            {stored.map((grant) => (
              <li key={grant.id}>
                <GrantRow grant={grant} onChanged={onChanged} />
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="This session only" meta={`${sessionGrants.length}`}>
        {sessionGrants.length === 0 ? (
          <p className="px-3 py-2 text-[11px] leading-relaxed text-ink-soft">
            No session permissions are active.
          </p>
        ) : (
          <>
            <p className="border-b border-line/60 px-3 py-1.5 text-[11px] leading-relaxed text-ink-dim">
              Held in memory by the running core. Restarting it drops all of these
              without you doing anything.
            </p>
            <div className="flex flex-col">
              {sessionGrants.map((grant) => (
                <DataRow
                  key={`${grant.capability} ${grant.scope}`}
                  label={grant.capability}
                  value={grant.scope === "*" ? "everywhere" : grant.scope}
                  state="ok"
                />
              ))}
            </div>
          </>
        )}
        <div className="px-3 py-2">
          <Button
            size="sm"
            tone="default"
            icon="unlock"
            onClick={clearSession.run}
            busy={clearSession.busy}
            disabled={sessionGrants.length === 0}
            reason="There are no session permissions to drop."
          >
            Drop session permissions
          </Button>
          <ActionResult
            done={clearSession.done}
            failed={clearSession.failed}
            onClear={clearSession.clear}
          />
        </div>
      </Section>

      {denied.length > 0 && (
        <Section title="Refused" meta={`${denied.length}`}>
          <p className="border-b border-line/60 px-3 py-1.5 text-[11px] leading-relaxed text-ink-soft">
            These were denied with "do not ask again". Until a refusal is removed, NEXUS
            will not raise a prompt for it - the action simply fails with the reason.
          </p>
          <ul>
            {denied.map((grant) => (
              <li key={grant.id}>
                <GrantRow grant={grant} onChanged={onChanged} />
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section title="Withdraw everything">
        <p className="px-3 py-2 text-[11px] leading-relaxed text-ink-soft">
          Removes every stored grant and every session grant in one go. Nothing breaks -
          NEXUS asks again the next time it needs something.
        </p>
        <div className="px-3 pb-2">
          <Button
            size="sm"
            tone="danger"
            icon="lock"
            onClick={() => setConfirmAll(true)}
            disabled={grants.length === 0 && sessionGrants.length === 0}
            reason="Nothing is granted, so there is nothing to withdraw."
          >
            Withdraw every permission
          </Button>
          <ActionResult done={revokeAll.done} failed={revokeAll.failed} onClear={revokeAll.clear} />
        </div>
      </Section>

      <ConfirmDialog
        open={confirmAll}
        title="Withdraw every permission"
        consequence={`${grants.length} stored grant${grants.length === 1 ? "" : "s"} and ${sessionGrants.length} session grant${sessionGrants.length === 1 ? "" : "s"} will be removed, refusals included. NEXUS will ask again the next time it needs any of them. Anything running right now that is waiting on a permission is unaffected.`}
        confirmLabel="Withdraw everything"
        onConfirm={revokeAll.run}
        onCancel={() => setConfirmAll(false)}
        busy={revokeAll.busy}
        failed={revokeAll.failed}
      />
    </div>
  );
}

function GrantRow({ grant, onChanged }: { grant: Grant; onChanged: () => void }) {
  const revoke = useAction(async () => {
    const result = await api.permissions.revoke(grant.capability, grant.scope);
    onChanged();
    return result.message;
  });

  const constraints = Object.entries(grant.constraints);

  return (
    <div className="border-b border-line/60 px-3 py-2 last:border-b-0">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="min-w-0 flex-1 break-all font-mono text-[11px] text-ink">
          {grant.capability}
        </span>
        {grant.expired && (
          <StatusPill state="idle" size="sm">
            EXPIRED
          </StatusPill>
        )}
        <Button size="sm" tone="ghost" icon="trash" onClick={revoke.run} busy={revoke.busy}>
          Revoke
        </Button>
      </div>
      <p className="mt-0.5 font-mono text-[10px] text-ink-dim">
        {KIND_WORDS[grant.grant_kind] ?? grant.grant_kind} ·{" "}
        {grant.scope === "*" ? "everywhere" : grant.scope} · granted{" "}
        {relativeTime(grant.granted_at)}
        {grant.expires_at ? ` · expires ${clockTime(grant.expires_at)}` : ""}
      </p>
      {constraints.length > 0 && (
        <p className="mt-0.5 font-mono text-[10px] leading-relaxed text-ink-soft">
          limits: {constraints.map(([key, value]) => `${key}=${JSON.stringify(value)}`).join(" · ")}
        </p>
      )}
      {grant.expired && (
        <p className="mt-0.5 text-[11px] leading-relaxed text-ink-dim">
          The engine treats this as gone and will ask again. The row stays until it is
          revoked.
        </p>
      )}
      <ActionResult done={revoke.done} failed={revoke.failed} onClear={revoke.clear} />
    </div>
  );
}

/* ---------------------------------------------------------------------- log */

/**
 * The audit trail (§46).
 *
 * `ok` and `verified` are separate columns and stay separate here. A tool that ran
 * successfully and a tool whose effect was checked afterwards are different claims, and
 * `verified: null` means no verifier exists rather than a check that failed - so the
 * rates are printed as two numbers over the same total instead of one blended score.
 */
function LogTab() {
  const [limit, setLimit] = useState("100");
  const log = useAsync(() => api.permissions.invocations(Number(limit)), [limit]);

  if (log.state === "loading") {
    return (
      <Section title="What ran">
        <LoadingState what="the invocation log" />
      </Section>
    );
  }

  if (!log.data) {
    return (
      <Section title="What ran">
        <div className="p-2">
          <ErrorState
            what={log.error ?? "The invocation log did not load."}
            did="Nothing is listed. An empty log would say no tool has ever run, which is a different claim."
            next={log.offline ? "Start the core, then retry." : log.detail || "Retry."}
            offline={log.offline}
            onRetry={log.reload}
            compact
          />
        </div>
      </Section>
    );
  }

  const { invocations, stats } = log.data;

  return (
    <div className="space-y-2">
      <Section title="Rates" meta={stats.total === 0 ? "nothing yet" : `${stats.total} calls`}>
        {stats.total === 0 ? (
          <p className="px-3 py-2 text-[11px] leading-relaxed text-ink-soft">
            No tool has run yet, so there is nothing to average. A rate over zero calls
            would be a made-up number.
          </p>
        ) : (
          <div className="flex flex-col">
            <DataRow
              label="Succeeded"
              value={`${stats.succeeded} of ${stats.total}`}
              state={stats.succeeded === stats.total ? "ok" : "warn"}
            />
            <DataRow
              label="Verified afterwards"
              value={`${stats.verified} of ${stats.total}`}
              state="idle"
              title="Only counts rows where a verifier ran and confirmed. A tool with no verifier is not counted as a failure."
            />
            <DataRow
              label="Average time"
              value={stats.avg_ms === null ? "not recorded" : duration(stats.avg_ms)}
            />
          </div>
        )}
      </Section>

      <Section
        title="Every call"
        meta={`${invocations.length} shown`}
      >
        <div className="flex items-center gap-2 border-b border-line/60 px-3 py-2">
          <div className="min-w-[160px]">
            <Select<string>
              value={limit}
              onChange={setLimit}
              options={[
                { value: "25", label: "Last 25" },
                { value: "100", label: "Last 100" },
                { value: "500", label: "Last 500" },
              ]}
            />
          </div>
          <Button size="sm" tone="ghost" icon="refresh" onClick={log.reload} busy={log.refreshing}>
            Re-read
          </Button>
        </div>
        {invocations.length === 0 ? (
          <p className="px-3 py-2 text-[11px] leading-relaxed text-ink-soft">
            Nothing has run. Every tool call NEXUS makes is written here, so this list
            being empty means none have been made.
          </p>
        ) : (
          <ul>
            {invocations.map((row) => (
              <li key={row.id}>
                <InvocationRow invocation={row} />
              </li>
            ))}
          </ul>
        )}
      </Section>
    </div>
  );
}

function InvocationRow({ invocation }: { invocation: Invocation }) {
  const [open, setOpen] = useState(false);
  const params = Object.entries(invocation.params);
  const ok = invocation.ok === 1;

  return (
    <div className="border-b border-line/60 px-3 py-1.5 last:border-b-0">
      <div className="flex flex-wrap items-baseline gap-2">
        <Icon
          name={ok ? "check" : "alert"}
          size={11}
          className={`shrink-0 ${ok ? "text-ok" : "text-danger"}`}
        />
        <span className="min-w-0 flex-1 break-all font-mono text-[11px] text-ink-soft">
          {invocation.tool}
        </span>
        {invocation.verified === null ? (
          <StatusPill state="idle" size="sm">
            NO VERIFIER
          </StatusPill>
        ) : invocation.verified === 1 ? (
          <StatusPill state="ok" size="sm">
            VERIFIED
          </StatusPill>
        ) : (
          <StatusPill state="pending" size="sm">
            UNVERIFIED
          </StatusPill>
        )}
        <span className="font-mono text-[10px] text-ink-faint">
          {invocation.duration_ms === null ? "no timing" : duration(invocation.duration_ms)}
        </span>
        <span className="font-mono text-[10px] text-ink-faint">
          {clockTime(invocation.created_at)}
        </span>
      </div>
      {invocation.error && (
        <p className="mt-0.5 break-words text-[11px] leading-relaxed text-danger">
          {invocation.error}
        </p>
      )}
      <div className="mt-0.5 flex items-center gap-2">
        {invocation.capability && (
          <span className="font-mono text-[10px] text-ink-dim">{invocation.capability}</span>
        )}
        {invocation.task_id && (
          <span className="font-mono text-[10px] text-ink-dim">from a task</span>
        )}
        {params.length > 0 && (
          <button
            type="button"
            onClick={() => setOpen((current) => !current)}
            className="ml-auto flex shrink-0 items-center gap-1 font-mono text-[10px] uppercase tracking-wider text-ink-dim hover:text-ink-soft"
          >
            <Icon name={open ? "chevron-down" : "chevron-right"} size={10} />
            {open ? "hide what it was given" : "what it was given"}
          </button>
        )}
      </div>
      {open && params.length > 0 && (
        <pre className="chamfer-sm mt-1 max-h-48 overflow-auto border border-line bg-base px-2 py-1.5 font-mono text-[10px] leading-relaxed text-ink-soft">
          {JSON.stringify(invocation.params, null, 2)}
        </pre>
      )}
    </div>
  );
}
