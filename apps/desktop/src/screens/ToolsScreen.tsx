/**
 * TOOLS — the catalogue, and running one by hand (§44, §45, §65, §66).
 *
 * Everything NEXUS can do to this computer is a registered tool, and this screen is the
 * place a person can read that list and drive it directly. Four things shape it:
 *
 * - **Unavailable tools are listed, not hidden.** The route includes them with the
 *   reason attached, because "NEXUS cannot do this" and "NEXUS needs xdotool installed"
 *   have different answers and only one of them has a fix. A hidden tool teaches the
 *   user the capability does not exist.
 * - **Preview and run are the same code path.** `runtime.preview` resolves the same
 *   tool, validates against the same schema and evaluates the same capability, so what
 *   the preview says is what execution will attempt. That is the only reason showing a
 *   preview is worth anything (§44).
 * - **The permission engine is not optional here.** `/api/tools/execute` hardcodes
 *   `require_permission=True`; there is no flag for a caller to turn it off. So a run
 *   from this screen can block on a dialog, and the button says so before it is pressed.
 * - **`verified: null` is not `false`.** A tool with no verifier returns null, and the
 *   catalogue's `verified_by_default` is what lets that be rendered as "this tool has no
 *   check" rather than as a check that failed.
 *
 * The parameter form is generated from each tool's own JSON schema — the same schema the
 * model is handed. A hand-written form per tool would drift from the validator, and the
 * first symptom would be a 400 the user cannot act on.
 */

import { useMemo, useRef, useState } from "react";

import { Icon } from "../components/Icon";
import {
  ActionResult,
  Button,
  DataRow,
  EmptyState,
  ErrorState,
  Field,
  IconButton,
  ListRow,
  LoadingState,
  Panel,
  Section,
  Select,
  StatusPill,
  TextArea,
  TextInput,
  Toggle,
  UnavailableState,
  clockTime,
  duration,
  relativeTime,
} from "../components/primitives";
import { api, type Invocation, type Preview, type Tool, type ToolResult } from "../lib/api";
import { useAction, useAsync } from "../lib/useAsync";
import { useNexus } from "../state/nexus";

const RISK_STATE = {
  low: "ok",
  medium: "pending",
  high: "warn",
  critical: "failed",
} as const;

type Availability = "all" | "available" | "blocked";

export function ToolsScreen() {
  const { link } = useNexus();
  const [query, setQuery] = useState("");
  const [availability, setAvailability] = useState<Availability>("all");
  const [selected, setSelected] = useState<string | null>(null);

  const list = useAsync(() => api.tools.list(), []);
  const tools = list.data?.tools ?? [];

  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return tools.filter((tool) => {
      if (availability === "available" && !tool.available) return false;
      if (availability === "blocked" && tool.available) return false;
      if (!needle) return true;
      return (
        tool.name.toLowerCase().includes(needle) ||
        tool.title.toLowerCase().includes(needle) ||
        tool.description.toLowerCase().includes(needle) ||
        tool.capability.toLowerCase().includes(needle)
      );
    });
  }, [tools, query, availability]);

  // Grouped by capability group, which is how PERMISSIONS groups the same things — a
  // user who allowed "files" there should find the file tools together here.
  const groups = useMemo(() => {
    const map = new Map<string, Tool[]>();
    for (const tool of shown) {
      const key = tool.group || "other";
      const bucket = map.get(key);
      if (bucket) bucket.push(tool);
      else map.set(key, [tool]);
    }
    return [...map.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [shown]);

  const current = tools.find((tool) => tool.name === selected) ?? null;

  return (
    <div className="grid h-full min-h-0 grid-cols-1 gap-2 p-2 lg:grid-cols-[minmax(260px,360px)_1fr]">
      <Panel
        title="TOOLS"
        meta={list.data ? `${list.data.available} of ${list.data.total} usable` : undefined}
        className="min-h-0"
        bodyClassName="flex min-h-0 flex-1 flex-col"
        actions={
          <IconButton
            name="refresh"
            label="Re-read the catalogue"
            onClick={list.reload}
            busy={list.refreshing}
          />
        }
      >
        <div className="space-y-2 border-b border-line px-3 py-2">
          <TextInput value={query} onChange={setQuery} placeholder="Search tools" />
          <Select<Availability>
            value={availability}
            onChange={setAvailability}
            options={[
              { value: "all", label: "All tools" },
              { value: "available", label: "Can run here" },
              { value: "blocked", label: "Missing a program" },
            ]}
          />
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {list.state === "loading" ? (
            <LoadingState what="the tool catalogue" />
          ) : !list.data ? (
            <div className="p-2">
              <ErrorState
                what={list.error ?? "The tool catalogue did not load."}
                did="Nothing is listed. An empty catalogue would say NEXUS can do nothing, which is a claim only the core can make."
                next={list.offline ? "Start the core, then retry." : list.detail || "Retry."}
                offline={list.offline}
                onRetry={list.reload}
                compact
              />
            </div>
          ) : shown.length === 0 ? (
            <EmptyState
              icon="search"
              title="No tool matches"
              detail={
                availability === "blocked"
                  ? "Every registered tool can run on this machine — nothing is missing a program."
                  : "Nothing matches that search. The catalogue is searched by name, title, description and capability."
              }
            />
          ) : (
            groups.map(([group, entries]) => (
              <div key={group}>
                <p className="sticky top-0 z-10 border-y border-line bg-panel px-3 py-1 font-mono text-[10px] uppercase tracking-wider text-ink-dim">
                  {group} · {entries.length}
                </p>
                <ul>
                  {entries.map((tool) => (
                    <li key={tool.name}>
                      <ListRow selected={tool.name === selected} onClick={() => setSelected(tool.name)}>
                        <div className="flex items-center gap-1.5">
                          <p className="min-w-0 flex-1 truncate font-mono text-[11px] text-ink">
                            {tool.name}
                          </p>
                          {!tool.available && (
                            <Icon name="lock" size={10} className="shrink-0 text-warn" />
                          )}
                          <StatusPill state={RISK_STATE[tool.risk]} size="sm">
                            {tool.risk}
                          </StatusPill>
                        </div>
                        <p className="mt-0.5 truncate text-[11px] text-ink-soft">{tool.title}</p>
                      </ListRow>
                    </li>
                  ))}
                </ul>
              </div>
            ))
          )}
        </div>
      </Panel>

      {current ? (
        <ToolDetail key={current.name} tool={current} connected={link.connected} />
      ) : (
        <Panel title="TOOL" className="min-h-0">
          <EmptyState
            icon="cpu"
            title="No tool selected"
            detail="Pick one on the left to read what it does, see what it would do with your parameters, and run it. Every run goes through the permission engine — the same one the model goes through."
          />
        </Panel>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------- schemas */

/**
 * The subset of JSON Schema `registry.validate` actually enforces.
 *
 * Written from the validator rather than from the spec: it understands `string`,
 * `number`, `integer`, `boolean`, `array` (with `items`) and `object`, plus `enum`,
 * `default`, the two length bounds, the two numeric bounds, `pattern` and `minItems`.
 * Rendering a control for anything else would promise validation that does not happen.
 */
type FieldSpec = {
  type?: string;
  description?: string;
  enum?: Array<string | number>;
  default?: unknown;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  minItems?: number;
  pattern?: string;
  items?: FieldSpec;
};

type ObjectSchema = {
  properties: Record<string, FieldSpec>;
  required: string[];
  additionalProperties: boolean;
};

function readSchema(parameters: Record<string, unknown>): ObjectSchema {
  const properties = (parameters.properties as Record<string, FieldSpec> | undefined) ?? {};
  const required = Array.isArray(parameters.required) ? (parameters.required as string[]) : [];
  return {
    properties,
    required,
    additionalProperties: parameters.additionalProperties === true,
  };
}

/** What a field's editor holds. Kept as text so a half-typed number is not destroyed. */
type Draft = Record<string, string | boolean>;

function initialDraft(schema: ObjectSchema): Draft {
  const draft: Draft = {};
  for (const [key, spec] of Object.entries(schema.properties)) {
    if (spec.type === "boolean") {
      draft[key] = spec.default === true;
      continue;
    }
    if (spec.default !== undefined && spec.default !== null) {
      draft[key] = Array.isArray(spec.default) ? spec.default.join("\n") : String(spec.default);
      continue;
    }
    draft[key] = "";
  }
  return draft;
}

/**
 * Turn the draft into the params body.
 *
 * An empty optional field is **omitted**, not sent as `""`. The validator treats a
 * missing key as "apply the default" and an empty string as a missing *required* value,
 * so sending empties would both discard defaults and turn optional fields into errors.
 * A required field left empty is reported here rather than shipped to earn a 400.
 */
function buildParams(
  schema: ObjectSchema,
  draft: Draft,
): { params: Record<string, unknown>; missing: string[]; invalid: string } {
  const params: Record<string, unknown> = {};
  const missing: string[] = [];

  for (const [key, spec] of Object.entries(schema.properties)) {
    const raw = draft[key];
    const isRequired = schema.required.includes(key);

    if (spec.type === "boolean") {
      params[key] = raw === true;
      continue;
    }
    const text = typeof raw === "string" ? raw.trim() : "";
    if (!text) {
      if (isRequired) missing.push(key);
      continue;
    }
    if (spec.type === "integer" || spec.type === "number") {
      const value = spec.type === "integer" ? Number.parseInt(text, 10) : Number(text);
      if (!Number.isFinite(value)) return { params, missing, invalid: `${key} is not a number.` };
      params[key] = value;
      continue;
    }
    if (spec.type === "array") {
      // One item per line: the validator wants a list, and a comma is a legitimate
      // character inside a path or a shell argument.
      params[key] = text
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean);
      continue;
    }
    if (spec.type === "object") {
      try {
        const parsed: unknown = JSON.parse(text);
        if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
          return { params, missing, invalid: `${key} must be a JSON object.` };
        }
        params[key] = parsed;
      } catch {
        return { params, missing, invalid: `${key} is not valid JSON.` };
      }
      continue;
    }
    params[key] = text;
  }

  return { params, missing, invalid: "" };
}

/** The constraints the validator will apply, said out loud under the control. */
function specHint(spec: FieldSpec, required: boolean): string {
  const parts: string[] = [required ? "required" : "optional"];
  if (spec.type && spec.type !== "string") parts.push(spec.type);
  if (spec.minimum !== undefined || spec.maximum !== undefined) {
    parts.push(
      spec.minimum !== undefined && spec.maximum !== undefined
        ? `${spec.minimum}–${spec.maximum}`
        : spec.minimum !== undefined
          ? `at least ${spec.minimum}`
          : `at most ${spec.maximum}`,
    );
  }
  if (spec.minLength !== undefined) parts.push(`min ${spec.minLength} chars`);
  if (spec.maxLength !== undefined) parts.push(`max ${spec.maxLength} chars`);
  if (spec.minItems !== undefined) parts.push(`at least ${spec.minItems} item(s)`);
  if (spec.pattern) parts.push("must match a pattern");
  if (spec.default !== undefined && spec.default !== null && spec.type !== "boolean") {
    parts.push(`defaults to ${JSON.stringify(spec.default)}`);
  }
  const suffix = spec.description ? ` — ${spec.description}` : "";
  return parts.join(" · ") + suffix;
}

/* -------------------------------------------------------------------- detail */

/**
 * One tool: what it is, what it would do, and what it did.
 *
 * Keyed on the tool name by the parent, so switching tools remounts this and the draft,
 * the preview and the last result all reset together. Carrying a previous tool's result
 * into a new tool's panel would be the worst kind of stale — a success message under the
 * wrong name.
 */
function ToolDetail({ tool, connected }: { tool: Tool; connected: boolean }) {
  const schema = useMemo(() => readSchema(tool.parameters), [tool]);
  const [draft, setDraft] = useState<Draft>(() => initialDraft(schema));
  const [preview, setPreview] = useState<Preview | null>(null);
  const [result, setResult] = useState<ToolResult | null>(null);
  const staged = useRef<Record<string, unknown>>({});

  const fields = Object.entries(schema.properties);

  /** Shared by both buttons: the same params, validated the same way, once. */
  function stage(): string {
    const { params, missing, invalid } = buildParams(schema, draft);
    if (invalid) return invalid;
    if (missing.length > 0) {
      return `Fill in ${missing.join(", ")} — the tool requires ${missing.length === 1 ? "it" : "them"}.`;
    }
    staged.current = params;
    return "";
  }

  const runPreview = useAction(async () => {
    const problem = stage();
    if (problem) throw new Error(problem);
    setResult(null);
    const next = await api.tools.preview(tool.name, staged.current);
    setPreview(next);
    return next.will_prompt
      ? "Previewed. Running this will ask for permission first."
      : "Previewed. No permission prompt will appear — the reason is below.";
  });

  const runTool = useAction(async () => {
    const problem = stage();
    if (problem) throw new Error(problem);
    const next = await api.tools.execute(tool.name, staged.current);
    setResult(next);
    if (!next.ok) {
      // A tool that ran and failed is a 200 with `ok: false`. Reported as a failure
      // here so it cannot be mistaken for a successful run with an empty result.
      throw new Error(next.error || next.summary || "The tool ran and failed without saying why.");
    }
    return next.summary || "It ran and reported no summary.";
  });

  return (
    <Panel
      title={tool.name.toUpperCase()}
      meta={`${tool.group} · ${tool.risk} risk`}
      className="min-h-0"
      bodyClassName="flex min-h-0 flex-1 flex-col"
      actions={
        <>
          <Button
            size="sm"
            tone="ghost"
            icon="search"
            onClick={runPreview.run}
            busy={runPreview.busy}
            disabled={!connected || !tool.available}
            reason={
              !tool.available
                ? "It cannot run on this machine, so there is nothing to preview."
                : "The core is not reachable."
            }
          >
            Preview
          </Button>
          <Button
            size="sm"
            tone={tool.read_only ? "accent" : "danger"}
            icon="play"
            onClick={runTool.run}
            busy={runTool.busy}
            disabled={!connected || !tool.available}
            reason={
              !tool.available
                ? `It cannot run here: ${tool.unavailable_reason}`
                : "The core is not reachable."
            }
          >
            Run
          </Button>
        </>
      }
    >
      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-2">
        <div className="chamfer-sm border border-line-bright bg-raised px-2.5 py-2">
          <p className="text-xs text-ink">{tool.title}</p>
          <p className="mt-1 text-[11px] leading-relaxed text-ink-soft">{tool.description}</p>
        </div>

        {!tool.available && (
          <UnavailableState
            what={tool.name}
            why={tool.unavailable_reason ?? "The core did not say why."}
            fix={`Install what it needs (${tool.requires.join(", ") || "unspecified"}) and re-read the catalogue. Nothing here is disabled to be tidy — the tool genuinely cannot execute.`}
          />
        )}

        <Section title="What it needs">
          <div className="flex flex-col">
            <DataRow
              label="Capability"
              value={tool.capability}
              title="The permission this tool asks for. PERMISSIONS is where it is granted or denied."
            />
            <DataRow label="Risk" value={tool.risk} state={RISK_STATE[tool.risk]} />
            <DataRow
              label="Reversible"
              value={tool.reversible ? "yes" : "no — it cannot be undone"}
              state={tool.reversible ? "ok" : "warn"}
            />
            <DataRow
              label="Reads only"
              value={tool.read_only ? "yes, it changes nothing" : "no, it changes something"}
              state={tool.read_only ? "ok" : "pending"}
            />
            <DataRow
              label="Safe to repeat"
              value={tool.idempotent ? "yes" : "no — running it twice is not the same as once"}
              state={tool.idempotent ? "ok" : "warn"}
              title="Agent mode uses this to decide whether a retry is safe."
            />
            <DataRow
              label="Result is checked"
              value={
                tool.verified_by_default
                  ? "yes, it has a verifier"
                  : "no verifier — its result reports verified as null, not false"
              }
              state={tool.verified_by_default ? "ok" : "idle"}
            />
            {tool.requires.length > 0 && (
              <DataRow
                label="Needs installed"
                value={tool.requires.join(", ")}
                state={tool.available ? "ok" : "failed"}
                title="An entry with | is satisfied by any one of the alternatives."
              />
            )}
          </div>
        </Section>

        <Section
          title="Parameters"
          meta={fields.length === 0 ? "none" : `${fields.length} · ${schema.required.length} required`}
        >
          {fields.length === 0 ? (
            <p className="px-3 py-2 text-[11px] leading-relaxed text-ink-soft">
              This tool takes no parameters. Running it needs no input.
            </p>
          ) : (
            <div className="space-y-2 px-3 py-2">
              {fields.map(([key, spec]) => (
                <SchemaField
                  key={key}
                  name={key}
                  spec={spec}
                  required={schema.required.includes(key)}
                  value={draft[key] ?? ""}
                  onChange={(next) => setDraft((current) => ({ ...current, [key]: next }))}
                  disabled={!tool.available}
                />
              ))}
              {schema.additionalProperties && (
                <p className="text-[11px] leading-relaxed text-ink-dim">
                  This tool also accepts parameters not listed in its schema. They cannot
                  be entered here — only the declared ones are.
                </p>
              )}
            </div>
          )}
        </Section>

        <ActionResult done={runPreview.done} failed={runPreview.failed} onClear={runPreview.clear} />
        {preview && <PreviewPanel preview={preview} />}

        <ActionResult done={runTool.done} failed={runTool.failed} onClear={runTool.clear} />
        {result && <ResultPanel result={result} tool={tool} />}

        <History tool={tool.name} />
      </div>
    </Panel>
  );
}

/** One control, chosen by the schema's declared type — never a text box for everything. */
function SchemaField({
  name,
  spec,
  required,
  value,
  onChange,
  disabled,
}: {
  name: string;
  spec: FieldSpec;
  required: boolean;
  value: string | boolean;
  onChange: (next: string | boolean) => void;
  disabled: boolean;
}) {
  const id = `param-${name}`;
  const hint = specHint(spec, required);

  if (spec.type === "boolean") {
    return (
      <div className="flex items-start gap-3">
        <Toggle
          on={value === true}
          onChange={onChange}
          label={name}
          disabled={disabled}
          reason="This tool cannot run on this machine."
        />
        <div className="min-w-0 flex-1">
          <span className="font-mono text-[11px] text-ink">{name}</span>
          <p className="mt-0.5 text-[10px] leading-relaxed text-ink-faint">{hint}</p>
        </div>
      </div>
    );
  }

  // A closed list in the schema becomes a closed list on screen. Typing a value the
  // validator will reject with "must be one of" is a mistake the form can prevent.
  if (spec.enum && spec.enum.length > 0) {
    return (
      <Field label={name} htmlFor={id} hint={hint}>
        <Select<string>
          id={id}
          value={typeof value === "string" ? value : ""}
          onChange={onChange}
          disabled={disabled}
          options={[
            ...(required ? [] : [{ value: "", label: "(leave unset)" }]),
            ...spec.enum.map((option) => ({ value: String(option), label: String(option) })),
          ]}
        />
      </Field>
    );
  }

  if (spec.type === "array" || spec.type === "object") {
    return (
      <Field
        label={name}
        htmlFor={id}
        hint={spec.type === "array" ? `${hint} · one item per line` : `${hint} · JSON object`}
      >
        <TextArea
          id={id}
          value={typeof value === "string" ? value : ""}
          onChange={onChange}
          rows={3}
          disabled={disabled}
          placeholder={spec.type === "array" ? "one\nper\nline" : '{"key": "value"}'}
        />
      </Field>
    );
  }

  const long = (spec.maxLength ?? 0) > 200;
  return (
    <Field label={name} htmlFor={id} hint={hint}>
      {long ? (
        <TextArea
          id={id}
          value={typeof value === "string" ? value : ""}
          onChange={onChange}
          rows={3}
          disabled={disabled}
        />
      ) : (
        <TextInput
          id={id}
          value={typeof value === "string" ? value : ""}
          onChange={onChange}
          disabled={disabled}
          placeholder={spec.type === "integer" || spec.type === "number" ? "a number" : ""}
        />
      )}
    </Field>
  );
}

/**
 * What the run would do, from the preview.
 *
 * `permission.source` is shown next to the outcome because an allow without a prompt
 * needs an explanation: "you allowed this for the session" and "it only reads this
 * machine's own state" are both silent allows, and only one of them is a decision the
 * user made.
 */
function PreviewPanel({ preview }: { preview: Preview }) {
  const { permission } = preview;
  return (
    <Section title="What running it would do" meta={preview.title}>
      <div className="border-b border-line/60 px-3 py-2">
        <span className="label label-accent">Effect</span>
        <p className="mt-1 whitespace-pre-wrap break-words text-[11px] leading-relaxed text-ink">
          {preview.effect || "The core described no specific effect for these parameters."}
        </p>
      </div>
      <div className="flex flex-col">
        <DataRow
          label="Permission decision"
          value={permission.outcome}
          state={
            permission.outcome === "allow" ? "ok" : permission.outcome === "deny" ? "failed" : "pending"
          }
        />
        <DataRow
          label="Because"
          value={permission.reason || "no reason given"}
          title={permission.detail || undefined}
        />
        <DataRow
          label="Decided by"
          value={permission.source || "not stated"}
          title="hard_limit, stored, session, user, timeout, disabled or implicit."
        />
        <DataRow label="Scope" value={permission.scope || "*"} />
        <DataRow
          label="You will be asked"
          value={preview.will_prompt ? "yes, a dialog will open" : "no"}
          state={preview.will_prompt ? "pending" : "idle"}
        />
        <DataRow
          label="Consequence"
          value={preview.consequence || "not described"}
          state={preview.reversible ? "idle" : "warn"}
        />
        <DataRow
          label="Result can be checked"
          value={preview.verifiable ? "yes" : "no verifier exists for this tool"}
          state={preview.verifiable ? "ok" : "idle"}
        />
      </div>
      {permission.outcome === "deny" && (
        <p className="border-t border-line/60 px-3 py-1.5 text-[11px] leading-relaxed text-danger">
          Running it will be refused before anything happens. Change the decision in
          PERMISSIONS if it should be allowed.
        </p>
      )}
    </Section>
  );
}

/**
 * What it actually did.
 *
 * The verification pill is tri-state and reads from the tool's own `verified_by_default`
 * when the result is null: "this tool has no verifier" is a fact about the tool, and
 * printing it as UNVERIFIED would imply a check ran and came back empty-handed.
 */
function ResultPanel({ result, tool }: { result: ToolResult; tool: Tool }) {
  return (
    <Section
      title={result.ok ? "It ran" : "It ran and failed"}
      meta={result.duration_ms === null ? undefined : duration(result.duration_ms)}
    >
      <div
        className={[
          "border-b border-line/60 px-3 py-2",
          result.ok ? "bg-ok/6" : "bg-danger/8",
        ].join(" ")}
      >
        <div className="flex flex-wrap items-center gap-1.5">
          <StatusPill state={result.ok ? "ok" : "failed"} size="sm">
            {result.ok ? "SUCCEEDED" : "FAILED"}
          </StatusPill>
          <StatusPill
            state={result.verified === null ? "idle" : result.verified ? "ok" : "pending"}
            size="sm"
          >
            {result.verified === null
              ? tool.verified_by_default
                ? "NOT CHECKED"
                : "NO VERIFIER"
              : result.verified
                ? "VERIFIED"
                : "UNVERIFIED"}
          </StatusPill>
        </div>
        <p className="mt-1.5 whitespace-pre-wrap break-words text-[11px] leading-relaxed text-ink">
          {result.summary || "It reported no summary."}
        </p>
        {result.error && (
          <p className="mt-1 whitespace-pre-wrap break-words text-[11px] leading-relaxed text-danger">
            {result.error}
          </p>
        )}
        {result.verification && (
          <p className="mt-1 font-mono text-[10px] leading-relaxed text-ink-faint">
            {result.verification}
          </p>
        )}
      </div>
      {result.data !== null && result.data !== undefined && (
        <details className="px-3 py-2">
          <summary className="cursor-pointer font-mono text-[10px] uppercase tracking-wider text-ink-dim hover:text-accent">
            What it returned
          </summary>
          <pre className="mt-1.5 max-h-64 overflow-auto whitespace-pre-wrap break-words bg-base p-2 font-mono text-[10px] leading-relaxed text-ink-soft">
            {JSON.stringify(result.data, null, 2)}
          </pre>
        </details>
      )}
    </Section>
  );
}

/**
 * Previous runs of this tool, from the invocation log.
 *
 * Read from `/api/permissions/invocations?tool=`, which is the record the permission
 * engine writes — not a client-side list. So it survives a reload and includes runs the
 * model made, which is the more interesting half.
 */
function History({ tool }: { tool: string }) {
  const runs = useAsync(() => api.permissions.invocations(25, tool), [tool]);
  const invocations = runs.data?.invocations ?? [];

  return (
    <Section title="Previous runs" meta={runs.data ? `${invocations.length}` : undefined}>
      {runs.state === "loading" ? (
        <LoadingState what="this tool's history" />
      ) : !runs.data ? (
        <p className="px-3 py-2 text-[11px] leading-relaxed text-warn">
          {runs.error ?? "The run history did not load."} Nothing is listed rather than an
          empty list, which would say this tool has never run.
        </p>
      ) : invocations.length === 0 ? (
        <p className="px-3 py-2 text-[11px] leading-relaxed text-ink-soft">
          This tool has never run — not from here and not from a conversation.
        </p>
      ) : (
        <ul>
          {invocations.map((run) => (
            <InvocationRow key={run.id} run={run} />
          ))}
        </ul>
      )}
    </Section>
  );
}

function InvocationRow({ run }: { run: Invocation }) {
  const ok = run.ok === 1;
  return (
    <li className="flex items-start gap-2 border-b border-line/60 px-3 py-1.5">
      <Icon
        name={ok ? "check" : "alert"}
        size={11}
        className={`mt-0.5 shrink-0 ${ok ? "text-ok" : "text-danger"}`}
      />
      <div className="min-w-0 flex-1">
        <p className="font-mono text-[10px] text-ink-faint">
          {relativeTime(run.created_at)} · {clockTime(run.created_at)}
          {run.duration_ms !== null && ` · ${duration(run.duration_ms)}`}
          {run.verified === null ? " · no verifier" : run.verified === 1 ? " · verified" : " · unverified"}
          {run.task_id ? " · from a task" : ""}
        </p>
        {run.error && (
          <p className="mt-0.5 break-words text-[11px] leading-relaxed text-danger">{run.error}</p>
        )}
        {Object.keys(run.params).length > 0 && (
          <p className="mt-0.5 break-all font-mono text-[10px] leading-relaxed text-ink-dim">
            {JSON.stringify(run.params)}
          </p>
        )}
      </div>
    </li>
  );
}
