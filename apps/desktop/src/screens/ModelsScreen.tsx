/**
 * MODELS — which model answers, and why (§26, §60).
 *
 * §26 requires the routing to be *visible*. That is only meaningful if the screen asks
 * the same resolver the chat path uses, so the Routing tab calls `/api/models/resolve`,
 * which calls `router.resolve` hypothetically rather than reimplementing the decision.
 * A screen that described its own idea of the routing table would be explaining a
 * system that does not exist.
 *
 * Four things this screen is careful about:
 *
 * 1. **`configured` is not `working`.** A provider is configured when a key is present
 *    in the environment. Whether the key is accepted is only knowable by calling the
 *    provider, which `POST /api/models/verify` does and the overview does not. They are
 *    drawn as two separate columns, and an unverified provider says "not checked" — not
 *    "ready".
 * 2. **Nothing here spends quota on its own.** `verify` and `probe` are POSTs precisely
 *    so a page load cannot bill the user. There is no polling, no probe-on-mount, and
 *    the buttons say what they will cost before they are pressed.
 * 3. **`blocked` and `flaky` stay apart.** A durable refusal removes a model from the
 *    chain; a timeout does not. NIM cold starts have been measured past 250 seconds, so
 *    collapsing the two would silently shrink the bench with nothing on screen to
 *    explain why a model stopped being used.
 * 4. **Rejected candidates are shown with their reasons.** A decision that only names
 *    the winner cannot be checked. `Decision.considered` carries every candidate that
 *    was skipped and what skipped it, so the chain is legible when the answer surprises
 *    you.
 */

import { useRef, useState } from "react";

import { Icon } from "../components/Icon";
import {
  ActionResult,
  Button,
  DataRow,
  EmptyState,
  ErrorState,
  Field,
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
  type JobSpec,
  type ModelSpec,
  type NimSpecialist,
  type ProbeResult,
  type ProbeRow,
  type ProbeStats,
  type ProviderInfo,
  type RouteDecision,
  type Verification,
} from "../lib/api";
import { useAction, useAsync } from "../lib/useAsync";
import { useNexus } from "../state/nexus";

type Tab = "routing" | "providers" | "specialists" | "probes";

const TABS = [
  { value: "routing" as const, label: "Routing" },
  { value: "providers" as const, label: "Providers" },
  { value: "specialists" as const, label: "NIM specialists" },
  { value: "probes" as const, label: "Probes" },
];

export function ModelsScreen() {
  const [tab, setTab] = useState<Tab>("routing");
  const overview = useAsync(() => api.models.overview(), []);

  if (overview.state === "loading") {
    return (
      <div className="h-full p-2">
        <Panel title="MODELS" className="min-h-0">
          <LoadingState what="the model configuration" />
        </Panel>
      </div>
    );
  }

  if (!overview.data) {
    return (
      <div className="h-full p-2">
        <Panel title="MODELS" className="min-h-0">
          <div className="p-2">
            <ErrorState
              what={overview.error ?? "The model configuration did not load."}
              did="Nothing is listed. An empty list would say you have no providers configured, which is a different claim."
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
  const configured = data.providers.filter((provider) => provider.configured);

  return (
    <div className="flex h-full min-h-0 flex-col gap-2 p-2">
      <Panel
        title="MODELS"
        meta={`${data.models.available} of ${data.models.total} usable · main ${data.routing.main_model}`}
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
            <span className="font-mono text-[10px] text-ink-faint">
              {configured.length} of {data.providers.length} providers keyed
            </span>
          }
        />

        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          {tab === "routing" && (
            <RoutingTab
              jobs={data.routing.jobs}
              assignments={data.routing.assignments}
              routing={data.routing}
              blocked={data.probes.blocked}
            />
          )}
          {tab === "providers" && (
            <ProvidersTab providers={data.providers} blocked={data.probes.blocked} />
          )}
          {tab === "specialists" && <SpecialistsTab specialists={data.nim_specialists} />}
          {tab === "probes" && (
            <ProbesTab jobs={data.routing.jobs} stats={data.probes.stats} onChanged={overview.reload} />
          )}
        </div>
      </Panel>
    </div>
  );
}

/* ------------------------------------------------------------------ routing */

/**
 * The job table, and the resolver's own answer for one job.
 *
 * Each job is a chain, not a model. The chain is shown in order, the pin (if any) is
 * shown as a pin, and "Ask the router" calls the real resolver — the same function the
 * chat path calls — rather than reading the first candidate and calling that the answer.
 * They differ whenever a candidate is unconfigured or probe-blocked, which is exactly
 * the case the user is trying to understand.
 */
function RoutingTab({
  jobs,
  assignments,
  routing,
  blocked,
}: {
  jobs: JobSpec[];
  assignments: Record<string, string>;
  routing: { main_model: string; router_mode: string; allow_fallback: boolean };
  blocked: Record<string, string>;
}) {
  const [open, setOpen] = useState<string>("");

  return (
    <div className="space-y-2">
      <Section title="How the router is set up">
        <div className="flex flex-col">
          <DataRow
            label="Main model"
            value={routing.main_model}
            title="Heads the conversation chain and is substituted wherever a chain defers to `the main model`."
          />
          <DataRow
            label="Mode"
            value={routing.router_mode}
            state={routing.router_mode === "pinned" ? "pending" : "ok"}
            title="`pinned` puts one model at the head of every chat job. It is still skipped for jobs it cannot do."
          />
          <DataRow
            label="Fallback"
            value={routing.allow_fallback ? "on" : "off"}
            state={routing.allow_fallback ? "ok" : "warn"}
          />
        </div>
        {!routing.allow_fallback && (
          <p className="px-3 py-1.5 text-[11px] leading-relaxed text-warn">
            Fallback is off. If the chosen model fails, NEXUS reports the failure instead
            of trying the next model in the chain.
          </p>
        )}
        {routing.router_mode === "pinned" && (
          <p className="px-3 py-1.5 text-[11px] leading-relaxed text-ink-dim">
            A pin is a preference, not a promise. It goes first for chat jobs and is
            skipped with a reason for anything it cannot do — pinning a text model does
            not make "read this screenshot" fail silently.
          </p>
        )}
      </Section>

      {jobs.map((job) => (
        <JobCard
          key={job.job}
          job={job}
          pinned={assignments[job.job] ?? ""}
          blocked={blocked}
          expanded={open === job.job}
          onToggle={() => setOpen((current) => (current === job.job ? "" : job.job))}
        />
      ))}
    </div>
  );
}

function JobCard({
  job,
  pinned,
  blocked,
  expanded,
  onToggle,
}: {
  job: JobSpec;
  pinned: string;
  blocked: Record<string, string>;
  expanded: boolean;
  onToggle: () => void;
}) {
  const { link } = useNexus();
  const [choice, setChoice] = useState(pinned);
  const [decision, setDecision] = useState<RouteDecision | null>(null);
  const staged = useRef<string>("");

  const pin = useAction(async () => {
    const next = staged.current;
    staged.current = "";
    // An empty string is the stored value for "no override" — the setting exists and
    // defaults to "", so clearing it is a write of "" rather than a delete.
    await api.settings.patch({ [job.setting]: next });
    setDecision(null);
    return next
      ? `${job.title} now goes to ${next} first.`
      : `${job.title} follows its own chain again.`;
  });

  const ask = useAction(async () => {
    const result = await api.models.resolve(job.job);
    setDecision(result.decision);
    return `${result.decision.label} would serve this.`;
  });

  const blockedInChain = job.candidates.filter((key) => blocked[key]);

  return (
    <Section
      title={job.title}
      meta={`${job.candidates.length} in the chain · ${job.transport}`}
    >
      <p className="border-b border-line/60 px-3 py-1.5 text-[11px] leading-relaxed text-ink-soft">
        {job.description}
      </p>
      <div className="flex flex-col">
        <DataRow label="Job id" value={job.job} title="Used by /api/models/resolve?job=" />
        <DataRow
          label="Head of chain"
          value={job.default}
          title="What the job uses when nothing is pinned and everything is reachable."
        />
        <DataRow
          label="Your choice"
          value={pinned || "none — it follows the chain"}
          state={pinned ? "pending" : "idle"}
        />
        {blockedInChain.length > 0 && (
          <DataRow
            label="Routed around"
            value={`${blockedInChain.length} of ${job.candidates.length}`}
            state="failed"
            title={blockedInChain.map((key) => `${key}: ${blocked[key]}`).join("\n")}
          />
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2 px-3 py-2">
        <Button size="sm" tone="ghost" icon={expanded ? "chevron-down" : "chevron-right"} onClick={onToggle}>
          {expanded ? "Hide the chain" : "Show the chain"}
        </Button>
        <Button
          size="sm"
          tone="accent"
          icon="network"
          onClick={ask.run}
          busy={ask.busy}
          disabled={!link.connected}
          reason="The core is not reachable, so the router cannot be asked."
        >
          Ask the router
        </Button>
      </div>
      <ActionResult done={ask.done} failed={ask.failed} onClear={ask.clear} />

      {expanded && (
        <div className="border-t border-line/60">
          <ol>
            {job.candidates.map((key, index) => (
              <li
                key={key}
                className="flex items-baseline gap-2 border-b border-line/60 px-3 py-1.5 last:border-b-0"
              >
                <span className="shrink-0 font-mono text-[10px] text-ink-faint">
                  {String(index + 1).padStart(2, "0")}
                </span>
                <span className="min-w-0 flex-1 break-all font-mono text-[11px] text-ink-soft">
                  {key}
                </span>
                {key === pinned && (
                  <StatusPill state="pending" size="sm">
                    YOUR CHOICE
                  </StatusPill>
                )}
                {blocked[key] && (
                  <span title={blocked[key]} className="shrink-0">
                    <StatusPill state="failed" size="sm">
                      BLOCKED
                    </StatusPill>
                  </span>
                )}
              </li>
            ))}
          </ol>

          <div className="flex flex-wrap items-end gap-2 px-3 py-2">
            <div className="min-w-[220px] flex-1">
              <Field label="Send this job to" htmlFor={`pin-${job.job}`}>
                <Select<string>
                  id={`pin-${job.job}`}
                  value={choice}
                  onChange={setChoice}
                  options={[
                    { value: "", label: "Follow the chain in order" },
                    ...job.candidates.map((key) => ({
                      value: key,
                      label: blocked[key] ? `${key} — blocked` : key,
                    })),
                  ]}
                />
              </Field>
            </div>
            <Button
              size="sm"
              tone="accent"
              icon="check"
              onClick={() => {
                staged.current = choice;
                pin.run();
              }}
              busy={pin.busy}
              disabled={choice === pinned}
              reason="That is already what this job uses."
            >
              Save
            </Button>
          </div>
          <div className="px-3 pb-2">
            <ActionResult done={pin.done} failed={pin.failed} onClear={pin.clear} />
          </div>
        </div>
      )}

      {decision && <DecisionView decision={decision} />}
    </Section>
  );
}

/**
 * The resolver's answer, including what it turned down.
 *
 * `considered` carries every candidate the router looked at with the reason it was
 * skipped, and those reasons are the whole value of this panel: "the key is missing"
 * and "a probe proved this model refuses this account" send the user to two different
 * fixes. A decision that named only the winner would be unfalsifiable.
 */
function DecisionView({ decision }: { decision: RouteDecision }) {
  const skipped = decision.considered.filter((candidate) => candidate.skipped);
  return (
    <div className="chamfer-sm m-2 border border-accent/30 bg-accent/4 px-2.5 py-2">
      <div className="flex items-center gap-2">
        <span className="label label-accent">The router would use</span>
        <span className="ml-auto font-mono text-[10px] text-ink-faint">{decision.transport}</span>
      </div>
      <p className="mt-1 break-all font-mono text-[11px] text-ink">{decision.key}</p>
      <p className="mt-1 text-[11px] leading-relaxed text-ink-soft">{decision.reason}</p>

      {decision.warnings.length > 0 && (
        <ul className="mt-1.5 space-y-1">
          {decision.warnings.map((warning) => (
            <li key={warning} className="flex items-start gap-1.5 text-[11px] leading-relaxed text-warn">
              <Icon name="info" size={11} className="mt-0.5 shrink-0" />
              <span className="min-w-0 flex-1">{warning}</span>
            </li>
          ))}
        </ul>
      )}

      {decision.fallbacks.length > 0 && (
        <p className="mt-1.5 font-mono text-[10px] leading-relaxed text-ink-faint">
          then, if it fails: {decision.fallbacks.map((c) => c.key).join(" → ")}
        </p>
      )}

      {skipped.length > 0 && (
        <div className="mt-2 border-t border-line/60 pt-1.5">
          <span className="label">Passed over</span>
          <ul className="mt-1 space-y-1">
            {skipped.map((candidate) => (
              <li key={candidate.key} className="text-[11px] leading-relaxed">
                <span className="break-all font-mono text-ink-dim">{candidate.key}</span>
                <span className="text-ink-soft"> — {candidate.skipped}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {decision.fallbacks.length === 0 && skipped.length === 0 && (
        <p className="mt-1.5 text-[11px] leading-relaxed text-ink-dim">
          Nothing was passed over and there is no fallback behind this one. If it fails,
          the job fails.
        </p>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------- providers */

/**
 * Providers and their models.
 *
 * `configured` and `verified` are deliberately two different columns. The overview makes
 * no network call, so it can only report whether a key is *present*; whether the key is
 * accepted needs `POST /api/models/verify`, and until that has been pressed the honest
 * answer is "not checked" (§60).
 */
function ProvidersTab({
  providers,
  blocked,
}: {
  providers: ProviderInfo[];
  blocked: Record<string, string>;
}) {
  const { link } = useNexus();
  const [results, setResults] = useState<Record<string, Verification> | null>(null);

  const verify = useAction(async () => {
    const result = await api.models.verify();
    setResults(result.results);
    return `${result.ok} of ${result.checked} providers answered.`;
  });

  return (
    <div className="space-y-2">
      <Section title="Are the keys real?">
        <p className="px-3 py-2 text-[11px] leading-relaxed text-ink-soft">
          Nothing on this screen has contacted a provider. "Keyed" means the credential
          is present in the environment — not that it works. Checking makes one real
          request per provider, which is why it is a button and not something a page load
          does.
        </p>
        <div className="flex flex-wrap items-center gap-2 px-3 pb-2">
          <Button
            size="sm"
            tone="accent"
            icon="verified"
            onClick={verify.run}
            busy={verify.busy}
            disabled={!link.connected}
            reason="The core is not reachable, so it cannot call the providers."
          >
            Check every key now
          </Button>
          <span className="font-mono text-[10px] text-ink-faint">
            spends one request each · NVIDIA cold starts have taken over 100 s
          </span>
        </div>
        <div className="px-3 pb-2">
          <ActionResult done={verify.done} failed={verify.failed} onClear={verify.clear} />
        </div>
      </Section>

      {providers.map((provider) => (
        <ProviderCard
          key={provider.id}
          provider={provider}
          verification={results?.[provider.id] ?? null}
          blocked={blocked}
        />
      ))}
    </div>
  );
}

function ProviderCard({
  provider,
  verification,
  blocked,
}: {
  provider: ProviderInfo;
  verification: Verification | null;
  blocked: Record<string, string>;
}) {
  const [expanded, setExpanded] = useState(false);
  const blockedHere = provider.models.filter((model) => blocked[model.key]).length;

  return (
    <Section
      title={provider.name}
      meta={`${provider.models.length} model${provider.models.length === 1 ? "" : "s"}`}
    >
      <div className="flex flex-col">
        <DataRow
          label="Key present"
          value={provider.configured ? "yes" : "no"}
          state={provider.configured ? "ok" : "failed"}
          title={`Read from ${provider.credential}.`}
        />
        <DataRow
          label="Read from"
          value={provider.credential}
          title="The environment variable. The value itself is never sent to this screen."
        />
        <DataRow
          label="Key checked"
          value={
            verification
              ? verification.ok
                ? `yes — ${verification.models_seen.length} models listed`
                : `no — ${verification.error_kind ?? "it failed"}`
              : "not checked"
          }
          state={verification ? (verification.ok ? "ok" : "failed") : "idle"}
        />
        {verification?.latency_ms != null && (
          <DataRow
            label="Answered in"
            value={duration(verification.latency_ms)}
            title="A slow success and a failure are different things; the number says which one this was."
          />
        )}
        {blockedHere > 0 && (
          <DataRow
            label="Proved unusable"
            value={`${blockedHere} of ${provider.models.length}`}
            state="failed"
            title="A real call was refused durably, so the router skips these."
          />
        )}
      </div>

      {verification && (
        <p
          className={[
            "px-3 py-1.5 text-[11px] leading-relaxed",
            verification.ok ? "text-ok" : "text-danger",
          ].join(" ")}
        >
          {verification.message}
        </p>
      )}

      {!provider.configured && (
        <p className="px-3 py-1.5 text-[11px] leading-relaxed text-ink-dim">
          No key, so every model here is skipped by the router. Set{" "}
          <span className="font-mono">{provider.credential}</span> in{" "}
          <span className="font-mono">.env</span> and restart the core. Keys are issued at{" "}
          <span className="break-all font-mono">{provider.console_url}</span>.
        </p>
      )}

      <div className="px-3 py-2">
        <Button
          size="sm"
          tone="ghost"
          icon={expanded ? "chevron-down" : "chevron-right"}
          onClick={() => setExpanded((current) => !current)}
        >
          {expanded ? "Hide models" : "Show models"}
        </Button>
      </div>

      {expanded && (
        <div className="border-t border-line/60">
          {provider.models.length === 0 ? (
            <p className="px-3 py-2 text-[11px] text-ink-dim">
              This provider is registered but declares no models.
            </p>
          ) : (
            <ul>
              {provider.models.map((model) => (
                <li key={model.key}>
                  <ModelRow
                    model={model}
                    configured={provider.configured}
                    blockedReason={blocked[model.key] ?? ""}
                  />
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </Section>
  );
}

/**
 * One model.
 *
 * A `null` cost is printed as "not declared", never as "$0" — only `free: true` says a
 * model costs nothing, and recommending a paid model as free is the kind of confident
 * fiction §3 forbids.
 */
function ModelRow({
  model,
  configured,
  blockedReason,
}: {
  model: ModelSpec;
  configured: boolean;
  blockedReason: string;
}) {
  const abilities = [
    model.supports_tools ? "tools" : "",
    model.supports_vision ? "vision" : "",
    model.supports_streaming ? "streams" : "",
  ].filter(Boolean);

  return (
    <div className="border-b border-line/60 px-3 py-2 last:border-b-0">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="text-[11px] text-ink">{model.label}</span>
        <span className="font-mono text-[10px] text-ink-faint">{model.latency}</span>
        {model.free && (
          <StatusPill state="ok" size="sm">
            FREE
          </StatusPill>
        )}
        {!configured && (
          <StatusPill state="idle" size="sm">
            NO KEY
          </StatusPill>
        )}
        {blockedReason && (
          <StatusPill state="failed" size="sm">
            BLOCKED
          </StatusPill>
        )}
      </div>
      <p className="mt-0.5 break-all font-mono text-[10px] text-ink-faint">{model.id}</p>
      <p className="mt-1 font-mono text-[10px] text-ink-dim">
        {model.context_window.toLocaleString()} in · {model.max_output_tokens.toLocaleString()} out
        {abilities.length > 0 ? ` · ${abilities.join(", ")}` : ""}
      </p>
      <p className="mt-0.5 font-mono text-[10px] text-ink-dim">
        {model.free
          ? "free at the point of use"
          : model.input_cost_per_million === null && model.output_cost_per_million === null
            ? "price not declared"
            : `$${model.input_cost_per_million ?? "?"} in / $${model.output_cost_per_million ?? "?"} out per million tokens`}
      </p>
      {model.strengths.length > 0 && (
        <p className="mt-1 text-[11px] leading-relaxed text-ink-soft">
          {model.strengths.join(" · ")}
        </p>
      )}
      {model.notes && (
        <p className="mt-1 text-[11px] leading-relaxed text-ink-dim">{model.notes}</p>
      )}
      {blockedReason && (
        <p className="mt-1 text-[11px] leading-relaxed text-danger">{blockedReason}</p>
      )}
    </div>
  );
}

/* -------------------------------------------------------------- specialists */

/**
 * The NIM bench, grouped by modality.
 *
 * `listed: false` means NVIDIA's own `/models` listing does not enumerate the id. That
 * proves nothing about whether the endpoint works — the listing does not enumerate
 * speech or reranking at all — so it is rendered as "not in the listing" with the date
 * the listing was checked, never as "unavailable". Calling it unavailable would bench a
 * working model on the strength of a missing catalogue entry.
 */
function SpecialistsTab({ specialists }: { specialists: NimSpecialist[] }) {
  if (specialists.length === 0) {
    return (
      <EmptyState
        icon="network"
        title="No specialists registered"
        detail="The NIM catalogue is empty, which means the core shipped without it — every non-chat job would have to fall back to the main model."
      />
    );
  }

  const modalities = [...new Set(specialists.map((entry) => entry.modality))];

  return (
    <div className="space-y-2">
      <Section title="What these are">
        <p className="px-3 py-2 text-[11px] leading-relaxed text-ink-soft">
          Each row is a NIM endpoint plus the exact way it has to be called — the
          quirks matter, because a model that refuses a text segment alongside the image
          or cannot be streamed needs a different request, not a retry. "Not in the
          listing" is about NVIDIA's catalogue, not about whether the endpoint answers.
        </p>
      </Section>

      {modalities.map((modality) => {
        const rows = specialists.filter((entry) => entry.modality === modality);
        return (
          <Section key={modality} title={modality} meta={`${rows.length}`}>
            <ul>
              {rows.map((entry) => (
                <li key={entry.model} className="border-b border-line/60 px-3 py-2 last:border-b-0">
                  <div className="flex flex-wrap items-baseline gap-2">
                    <span className="text-[11px] text-ink">{entry.label}</span>
                    {!entry.listed && (
                      <StatusPill state="idle" size="sm">
                        NOT IN THE LISTING
                      </StatusPill>
                    )}
                    {entry.image_only && (
                      <StatusPill state="pending" size="sm">
                        IMAGE ONLY
                      </StatusPill>
                    )}
                    {entry.structured_response && (
                      <StatusPill state="pending" size="sm">
                        STRUCTURED
                      </StatusPill>
                    )}
                    {!entry.streamable && (
                      <StatusPill state="idle" size="sm">
                        NO STREAM
                      </StatusPill>
                    )}
                  </div>
                  <p className="mt-0.5 break-all font-mono text-[10px] text-ink-faint">
                    {entry.model}
                  </p>
                  <p className="mt-0.5 break-all font-mono text-[10px] text-ink-dim">
                    {entry.endpoint}
                  </p>
                  <p className="mt-1 font-mono text-[10px] text-ink-dim">
                    jobs: {entry.jobs.length > 0 ? entry.jobs.join(", ") : "none — it heads no chain"}
                    {entry.sample_rate ? ` · ${entry.sample_rate} Hz` : ""}
                    {entry.languages.length > 0 ? ` · ${entry.languages.join(", ")}` : ""}
                  </p>
                  {!entry.listed && (
                    <p className="mt-1 text-[11px] leading-relaxed text-ink-dim">
                      NVIDIA's model listing did not name this id when it was checked
                      ({entry.listing_checked}). The listing does not enumerate every
                      endpoint, so this is not evidence either way — only a real call is.
                    </p>
                  )}
                  {entry.notes && (
                    <p className="mt-1 text-[11px] leading-relaxed text-ink-soft">{entry.notes}</p>
                  )}
                </li>
              ))}
            </ul>
          </Section>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------- probes */

/**
 * What a real call proved, and the button that proves it again.
 *
 * The two failure kinds are kept apart everywhere on this tab, because only one of them
 * changes routing. A durable refusal (404 for this account, a rejected key) takes the
 * model out of the chain; a timeout leaves it in, since a NIM cold start has been
 * measured past 250 seconds and permanently benching a slow model would shrink the
 * bench with nothing on screen to explain it.
 *
 * Probing every job is the most expensive request in the API — one call per model per
 * chain, fallbacks included — so it is never automatic and the job filter exists to
 * make a narrower run possible.
 */
function ProbesTab({
  jobs,
  stats,
  onChanged,
}: {
  jobs: JobSpec[];
  stats: ProbeStats;
  onChanged: () => void;
}) {
  const { link } = useNexus();
  const [job, setJob] = useState("");
  const [run, setRun] = useState<{ results: ProbeResult[]; summary: string } | null>(null);
  const stored = useAsync(() => api.models.probes(), []);
  const stagedJobs = useRef<string[] | undefined>(undefined);
  const stagedKey = useRef<string | undefined>(undefined);

  const probe = useAction(async () => {
    const only = stagedJobs.current;
    stagedJobs.current = undefined;
    const result = await api.models.probe(only);
    setRun({ results: result.results, summary: result.summary });
    stored.reload();
    onChanged();
    return `${result.summary} Took ${duration(result.duration_ms)}.`;
  });

  const clear = useAction(async () => {
    const key = stagedKey.current;
    stagedKey.current = undefined;
    const result = await api.models.clearProbes(key);
    setRun(null);
    stored.reload();
    onChanged();
    return key
      ? `Forgot what was measured about ${key}.`
      : `Forgot ${result.cleared} stored verdict${result.cleared === 1 ? "" : "s"}.`;
  });

  return (
    <div className="space-y-2">
      <Section title="What is remembered" meta={stats.last_checked ? relativeTime(stats.last_checked) : "never run"}>
        <div className="flex flex-col">
          <DataRow label="Models measured" value={String(stats.total)} />
          <DataRow label="Answered" value={String(stats.callable)} state="ok" />
          <DataRow
            label="Blocked"
            value={String(stats.blocked)}
            state={stats.blocked > 0 ? "failed" : "idle"}
            title="A durable refusal. These are skipped by the router."
          />
          <DataRow
            label="Flaky"
            value={String(stats.flaky)}
            state={stats.flaky > 0 ? "warn" : "idle"}
            title="Timed out or failed transiently. Still in the chain — a slow model is not a broken one."
          />
          {stats.last_checked && (
            <DataRow label="Last measured" value={clockTime(stats.last_checked)} />
          )}
        </div>
        {stats.total === 0 && (
          <p className="px-3 py-1.5 text-[11px] leading-relaxed text-ink-dim">
            Nothing has been probed. The router still works — it just has no evidence to
            skip a model with, so it will try each one in order and find out at request
            time.
          </p>
        )}
      </Section>

      <Section title="Measure it again">
        <p className="px-3 py-2 text-[11px] leading-relaxed text-ink-soft">
          This calls every model in the selected chains once, fallbacks included. It is
          the most expensive request NEXUS makes, and it is never triggered by opening
          this screen.
        </p>
        <div className="flex flex-wrap items-end gap-2 px-3 pb-2">
          <div className="min-w-[220px] flex-1">
            <Field label="Which chains" htmlFor="probe-job">
              <Select<string>
                id="probe-job"
                value={job}
                onChange={setJob}
                options={[
                  { value: "", label: "Every job" },
                  ...jobs.map((entry) => ({ value: entry.job, label: entry.title })),
                ]}
              />
            </Field>
          </div>
          <Button
            size="sm"
            tone="accent"
            icon="play"
            onClick={() => {
              stagedJobs.current = job ? [job] : undefined;
              probe.run();
            }}
            busy={probe.busy}
            disabled={!link.connected}
            reason="The core is not reachable, so nothing can be called."
          >
            {job ? "Probe this chain" : "Probe everything"}
          </Button>
          <Button
            size="sm"
            tone="danger"
            icon="trash"
            onClick={() => {
              stagedKey.current = undefined;
              clear.run();
            }}
            busy={clear.busy}
          >
            Forget the results
          </Button>
        </div>
        <div className="px-3 pb-2">
          <ActionResult done={probe.done} failed={probe.failed} onClear={probe.clear} />
          <ActionResult done={clear.done} failed={clear.failed} onClear={clear.clear} />
        </div>
        <p className="px-3 pb-2 text-[11px] leading-relaxed text-ink-dim">
          Forgetting is useful after adding a key: a model blocked for "no credential"
          stays blocked until something measures it again.
        </p>
      </Section>

      {run && (
        <Section title="This run" meta={`${run.results.length} called`}>
          <p className="border-b border-line/60 px-3 py-1.5 text-[11px] leading-relaxed text-ink-soft">
            {run.summary}
          </p>
          <ul>
            {run.results.map((result) => (
              <li
                key={`${result.job}-${result.key}`}
                className="border-b border-line/60 px-3 py-1.5 last:border-b-0"
              >
                <div className="flex flex-wrap items-baseline gap-2">
                  <StatusPill state={result.ok ? "ok" : "failed"} size="sm">
                    {result.ok ? "ANSWERED" : (result.error_kind ?? "FAILED")}
                  </StatusPill>
                  <span className="min-w-0 flex-1 break-all font-mono text-[11px] text-ink-soft">
                    {result.key}
                  </span>
                  <span className="font-mono text-[10px] text-ink-faint">
                    {result.latency_ms != null ? duration(result.latency_ms) : "no timing"}
                  </span>
                </div>
                <p className="mt-0.5 font-mono text-[10px] text-ink-dim">
                  {result.job} · {result.transport}
                  {result.stored === false ? " · not recorded" : ""}
                </p>
                {result.detail && !result.ok && (
                  <p className="mt-0.5 break-words text-[11px] leading-relaxed text-danger">
                    {result.detail}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </Section>
      )}

      <StoredProbes state={stored} onForget={(key) => { stagedKey.current = key; clear.run(); }} />
    </div>
  );
}

/** Every stored verdict, newest first, with the row that caused it. */
function StoredProbes({
  state,
  onForget,
}: {
  state: ReturnType<typeof useAsync<{ probes: ProbeRow[]; stats: ProbeStats; blocked: Record<string, string> }>>;
  onForget: (key: string) => void;
}) {
  if (state.state === "loading") {
    return (
      <Section title="Every measurement">
        <LoadingState what="stored probe results" />
      </Section>
    );
  }
  if (!state.data) {
    return (
      <Section title="Every measurement">
        <div className="p-2">
          <ErrorState
            what={state.error ?? "The stored probe results did not load."}
            did="Nothing is listed. An empty list would say nothing has ever been measured."
            next={state.offline ? "Start the core, then retry." : state.detail || "Retry."}
            offline={state.offline}
            onRetry={state.reload}
            compact
          />
        </div>
      </Section>
    );
  }

  const rows = state.data.probes;
  if (rows.length === 0) return null;

  return (
    <Section title="Every measurement" meta={`${rows.length}`}>
      <ul>
        {rows.map((row) => (
          <li
            key={row.key}
            className="flex items-start gap-2 border-b border-line/60 px-3 py-1.5 last:border-b-0"
          >
            <Icon
              name={row.ok ? "check" : row.durable ? "alert" : "info"}
              size={11}
              className={[
                "mt-1 shrink-0",
                row.ok ? "text-ok" : row.durable ? "text-danger" : "text-warn",
              ].join(" ")}
            />
            <div className="min-w-0 flex-1">
              <p className="break-all font-mono text-[11px] text-ink-soft">{row.key}</p>
              <p className="mt-0.5 font-mono text-[10px] text-ink-dim">
                {row.ok
                  ? "answered"
                  : row.durable
                    ? `blocked — ${row.error_kind ?? "refused"}`
                    : `flaky — ${row.error_kind ?? "failed once"}`}
                {" · "}
                {row.latency_ms != null ? duration(row.latency_ms) : "no timing"} ·{" "}
                {clockTime(row.checked_at)}
              </p>
              {row.detail && !row.ok && (
                <p className="mt-0.5 break-words text-[11px] leading-relaxed text-ink-soft">
                  {row.detail}
                </p>
              )}
              {!row.ok && !row.durable && (
                <p className="mt-0.5 text-[11px] leading-relaxed text-ink-dim">
                  Still in the chain. A transient failure does not bench a model.
                </p>
              )}
            </div>
            <Button size="sm" tone="ghost" icon="refresh" onClick={() => onForget(row.key)}>
              Forget
            </Button>
          </li>
        ))}
      </ul>
    </Section>
  );
}
