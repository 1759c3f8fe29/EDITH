/**
 * The live turn, rendered (§15, §17, §57, §58).
 *
 * This is the component that makes the assistant inspectable instead of magical. §15
 * requires that the user can see *why* a model was chosen; §17 requires that a tool
 * run shows what it did and whether it was verified; §57 requires that thinking is
 * visible rather than a spinner. All three are the same idea: everything the core
 * reports about a turn is on screen, attributable, and labelled with what it is.
 *
 * What is deliberately not here:
 *
 * - **No markdown renderer.** The core streams plain text and the product does not
 *   promise formatting. Parsing it as markdown would silently eat asterisks out of
 *   real content — a shell command with a glob, a file pattern — and rendering
 *   half-parsed markdown mid-stream flickers as delimiters open and close.
 * - **No collapsed-by-default tool output.** A tool that ran is the most important
 *   thing on the screen; hiding it behind a chevron is how an interface trains its
 *   user not to look.
 * - **No invented "thinking" text.** `reasoning` is shown only when the model actually
 *   sent some. An empty block would imply the model reasoned silently.
 */

import { Icon } from "./Icon";
import { StatusPill, duration } from "./primitives";
import { isConfident, type ContextInfo, type IntentInfo, type RoutingInfo, type ToolRunInfo } from "../lib/chat";
import { turnSentence, type LiveCall, type LiveTurn } from "../state/turn";

export function TurnView({ turn, compact = false }: { turn: LiveTurn; compact?: boolean }) {
  return (
    <div className="flex flex-col gap-3">
      <AskedBlock turn={turn} />

      {turn.adopted && turn.endReason && (
        <p className="chamfer-sm border border-warn/40 bg-warn/8 px-2.5 py-1.5 text-[11px] leading-relaxed text-warn">
          {turn.endReason}
        </p>
      )}

      <PhaseLine turn={turn} />

      {turn.routing && <RoutingBlock routing={turn.routing} intent={turn.intent} />}

      {!compact && turn.context && <ContextBlock context={turn.context} />}

      {turn.calls.length > 0 && (
        <div className="flex flex-col gap-2">
          <span className="label">
            Tools — {turn.calls.filter((entry) => entry.result).length} of {turn.calls.length} finished
          </span>
          {turn.calls.map((entry, index) => (
            <CallBlock key={`${entry.call.id}-${index}`} entry={entry} />
          ))}
        </div>
      )}

      {turn.text && (
        <div className="chamfer-sm border border-accent/30 bg-accent/5 px-2.5 py-2">
          <span className="label label-accent">NEXUS</span>
          <p className="mt-1 whitespace-pre-wrap break-words text-xs leading-relaxed text-ink">
            {turn.text}
          </p>
        </div>
      )}

      {turn.notices.length > 0 && (
        <ul className="flex flex-col gap-1">
          {turn.notices.map((notice, index) => (
            <li
              key={index}
              className="flex items-start gap-1.5 text-[11px] leading-relaxed text-warn"
            >
              <Icon name="info" size={12} className="mt-0.5 shrink-0" />
              {notice}
            </li>
          ))}
        </ul>
      )}

      <SpeechBlock turn={turn} />

      {turn.error && (
        <p className="chamfer-sm border border-danger/40 bg-danger/8 px-2.5 py-1.5 text-[11px] leading-relaxed text-danger">
          {turn.error}
          {turn.errorKind && (
            <span className="ml-1.5 font-mono text-[10px] text-danger/70">({turn.errorKind})</span>
          )}
        </p>
      )}

      {turn.reasoning && (
        <details className="chamfer-sm border border-line bg-base px-2.5 py-2">
          <summary className="flex items-center gap-2 cursor-pointer label">
            <span>Model reasoning (collapsed)</span>
            <Icon name="chevron-down" size={12} className="text-ink-dim" />
          </summary>
          <p className="mt-1 whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-ink-soft">
            {turn.reasoning}
          </p>
        </details>
      )}

      {turn.finished && !compact && <UsageBlock turn={turn} />}
    </div>
  );
}

/**
 * What was asked, and — when it was spoken — what the machine believed it heard (§61).
 *
 * The transcript is the single most important thing on a voice screen, and it is shown
 * with the model that produced it. A misheard word is what explains an answer that
 * otherwise looks like the model being stupid, and "voice input is broken" and
 * "Voxtral misheard one word" are different problems with different remedies.
 *
 * `heard` and `prompt` are usually the same string, because the reducer fills an empty
 * prompt from the transcript. They differ when text and audio were sent together, and
 * then both are shown rather than one being chosen: which of the two the model answered
 * is not this component's guess to make.
 */
function AskedBlock({ turn }: { turn: LiveTurn }) {
  const heard = turn.heard.trim();
  const asked = turn.prompt.trim();
  const said = asked || heard;
  if (!said) return null;
  // Only when there is a typed prompt *and* a different transcript is there a second
  // thing to show. Otherwise the one string is the whole of what was asked.
  const also = heard && asked && heard !== asked ? heard : "";
  const spoken = heard !== "";
  return (
    <div className="chamfer-sm border border-line-bright bg-raised px-2.5 py-2">
      <div className="flex items-center gap-1.5">
        {spoken && <Icon name="mic" size={12} className="shrink-0 text-ink-dim" />}
        <span className="label">{spoken && !also ? "NEXUS heard" : "You asked"}</span>
        {spoken && turn.heardBy && (
          <span className="font-mono text-[10px] text-ink-faint">by {turn.heardBy}</span>
        )}
      </div>
      <p className="mt-1 whitespace-pre-wrap break-words text-xs leading-relaxed text-ink">
        {said}
      </p>
      {also && (
        <div className="mt-1.5 border-t border-line pt-1.5">
          <div className="flex items-center gap-1.5">
            <Icon name="mic" size={12} className="shrink-0 text-ink-dim" />
            <span className="label">And heard</span>
          </div>
          <p className="mt-1 whitespace-pre-wrap break-words text-xs leading-relaxed text-ink-soft">
            {also}
          </p>
        </div>
      )}
    </div>
  );
}

function PhaseLine({ turn }: { turn: LiveTurn }) {
  const state =
    turn.phase === "complete"
      ? turn.error
        ? "pending"
        : "ok"
      : turn.phase === "failed"
        ? "failed"
        : turn.phase === "interrupted"
          ? "cancelled"
          : turn.phase === "dropped"
            ? "waiting"
            : "active";
  return (
    <div className="flex items-center gap-2">
      <StatusPill state={state} size="sm" pulse={state === "active"}>
        {turn.phase.toUpperCase()}
      </StatusPill>
      <span className="min-w-0 truncate text-[11px] text-ink-soft">{turnSentence(turn)}</span>
    </div>
  );
}

/**
 * The spoken half of the answer: which voice said it, how much of it has audio, and
 * every sentence that has none (§30, §57, §61).
 */
function SpeechBlock({ turn }: { turn: LiveTurn }) {
  const { segments, speech, speechErrors, voice } = turn;
  if (!voice && segments.length === 0 && speech.length === 0 && speechErrors.length === 0) {
    return null;
  }
  const described = segments.filter((segment) => segment.source !== null).length;
  return (
    <div className="chamfer-sm border border-line bg-base px-2.5 py-2">
      <span className="label">Spoken</span>
      <div className="mt-1 flex flex-col gap-1 text-[11px] leading-relaxed text-ink-soft">
        {voice && (
          <span>Voice: {voice.voice || "the provider default"} · {voice.model}</span>
        )}
        {segments.length > 0 && (
          <span>{speech.length} of {segments.length} sentence{segments.length === 1 ? "" : "s"} with audio</span>
        )}
        {described > 0 && (
          <span>{described} code block{described === 1 ? "" : "s"} named rather than read out</span>
        )}
        {speechErrors.length > 0 && (
          <div>
            <span className="text-warn">Not spoken</span>
            <ul className="mt-0.5 flex flex-col gap-1">
              {speechErrors.map((failed) => (
                <li key={failed.index} className="text-warn">
                  <span className="font-mono text-[10px] text-warn/70">#{failed.index + 1}</span>{" "}
                  {failed.text.trim() || "an empty sentence"} — {failed.error}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * Why this model (§15, §26).
 */
function RoutingBlock({ routing, intent }: { routing: RoutingInfo; intent: IntentInfo | null }) {
  return (
    <details className="chamfer-sm border border-line bg-base px-2.5 py-2">
      <summary className="flex items-center gap-2 cursor-pointer label">
        <span>Routing ({routing.job})</span>
        <Icon name="chevron-down" size={12} className="text-ink-dim" />
      </summary>
      <div className="mt-2 flex flex-col gap-1.5 text-[11px] leading-relaxed text-ink-soft">
        <div><span className="font-mono">Model:</span> {routing.label}</div>
        <div><span className="font-mono">Provider:</span> {routing.provider} · {routing.model}</div>
        <div><span className="font-mono">Because:</span> {routing.reason}</div>
        {intent && (
          <>
            <div><span className="font-mono">Read as:</span> {intent.intent} → {intent.job}</div>
            <div className={isConfident(intent) ? "text-ok" : "text-warn"}>
              <span className="font-mono">Confidence:</span> {intent.score.toFixed(2)} —
              {isConfident(intent) ? "confident" : "not confident, so tools stayed attached"}
            </div>
            {intent.signals.length > 0 && (
              <div><span className="font-mono">Signals:</span> {intent.signals.join(", ")}</div>
            )}
          </>
        )}
        {routing.considered.length > 0 && (
          <div className="text-ink-dim"><span className="font-mono">Considered:</span> {routing.considered.map((item) => item.key).join(" → ")}</div>
        )}
        {routing.fallbacks.length > 0 && (
          <div className="text-ink-dim"><span className="font-mono">Fallbacks:</span> {routing.fallbacks.map((item) => item.key).join(" → ")}</div>
        )}
        {routing.warnings.map((warning, index) => (
          <p key={index} className="text-warn">{warning}</p>
        ))}
      </div>
    </details>
  );
}

/**
 * What went into the prompt, and what did not fit (§45).
 */
function ContextBlock({ context }: { context: Partial<ContextInfo> }) {
  const blocks = context.blocks ?? [];
  const tokens = context.tokens;
  const budget = context.budget;
  if (blocks.length === 0 && tokens === undefined) return null;
  return (
    <details className="chamfer-sm border border-line bg-base px-2.5 py-2">
      <summary className="flex items-center gap-2 cursor-pointer label">
        <span>Context</span>
        {tokens !== undefined && budget !== undefined && (
          <span className="text-ink-dim font-mono text-[10px]">{tokens.toLocaleString()} of {budget.toLocaleString()} tokens</span>
        )}
        <Icon name="chevron-down" size={12} className="text-ink-dim" />
      </summary>
      <div className="mt-2 flex flex-col gap-1.5 text-[11px] leading-relaxed text-ink-soft">
        {blocks.map((block, index) => (
          <div key={`${block.kind}-${index}`}><span className="font-mono">{block.heading || block.kind}:</span> {block.tokens.toLocaleString()} tokens</div>
        ))}
        {context.turns !== undefined && (
          <div><span className="font-mono">History:</span> {context.turns} earlier turns</div>
        )}
        {context.memories !== undefined && (
          <div><span className="font-mono">Memories:</span> {context.memories} recalled</div>
        )}
        {(context.dropped ?? []).length > 0 && (
          <div>
            <span className="text-warn font-mono">Did not fit:</span>
            <ul className="mt-0.5 flex flex-col gap-0.5">
              {(context.dropped ?? []).map((item, index) => (
                <li key={index} className="text-warn font-mono text-[11px]">{item}</li>
              ))}
            </ul>
          </div>
        )}
        {(context.notices ?? []).map((notice, index) => (
          <p key={index} className="text-ink-dim">{notice}</p>
        ))}
      </div>
    </details>
  );
}

/**
 * One tool call (§17, §18).
 *
 * The verification tri-state is the fussy part: `verified === null` means this tool has
 * no verifier, which is not the same as "not verified" and must not be shown as a
 * failure. §18's whole point is that "I did it" and "I checked that I did it" are
 * different claims, so an unverifiable tool says so in those words.
 */
function CallBlock({ entry }: { entry: LiveCall }) {
  const { call, preview, result } = entry;
  const running = !result;
  return (
    <div
      className={[
        "chamfer-sm border px-2.5 py-2",
        running
          ? "border-accent/40 bg-accent/5"
          : result.ok
            ? "border-line-bright bg-raised"
            : "border-danger/40 bg-danger/8",
      ].join(" ")}
    >
      <div className="flex items-center gap-2">
        <Icon
          name={running ? "spinner" : result.ok ? "check" : "close"}
          size={13}
          className={[
            "shrink-0",
            running ? "animate-spin text-accent" : result.ok ? "text-ok" : "text-danger",
          ].join(" ")}
        />
        <span className="font-mono text-[11px] text-ink">{call.name}</span>
        {result && <Verification result={result} />}
        <span className="ml-auto shrink-0 font-mono text-[10px] text-ink-faint">
          {running ? "running" : duration(result.duration_ms)}
        </span>
      </div>

      {call.parse_error && (
        <p className="mt-1 text-[11px] leading-relaxed text-danger">
          This call never ran: {call.parse_error}
        </p>
      )}

      {Object.keys(call.arguments).length > 0 && (
        <pre className="mt-1 max-h-28 overflow-auto whitespace-pre-wrap break-all font-mono text-[10px] leading-relaxed text-ink-dim">
          {JSON.stringify(call.arguments, null, 2)}
        </pre>
      )}

      {call.raw_arguments && call.parse_error && (
        <div className="mt-1">
          <span className="label">As the model sent it</span>
          <pre className="mt-0.5 max-h-24 overflow-auto whitespace-pre-wrap break-all font-mono text-[10px] text-ink-faint">
            {call.raw_arguments}
          </pre>
        </div>
      )}

      {preview && (
        <div className="mt-1 flex flex-col gap-0.5 text-[11px] leading-relaxed text-ink-soft">
          <span className="label label-accent">Effect</span>
          <p className="whitespace-pre-wrap break-words">{preview.effect || "The core described no specific effect for these parameters."}</p>
          <div className="flex flex-col gap-0.5 border-t border-line/60 pt-1">
            <span><span className="font-mono">Permission:</span> {preview.permission.outcome} — {preview.permission.reason || "no reason given"}</span>
            <span><span className="font-mono">Scope:</span> {preview.permission.scope || "*"}</span>
            <span><span className="font-mono">Will prompt:</span> {preview.will_prompt ? "yes" : "no"}</span>
            <span className={preview.reversible ? "text-ok" : "text-warn"}><span className="font-mono">Reversible:</span> {preview.reversible ? "yes" : "no"}</span>
            <span className={preview.verifiable ? "text-ok" : "text-idle"}><span className="font-mono">Verifiable:</span> {preview.verifiable ? "yes" : "no verifier exists"}</span>
          </div>
        </div>
      )}

      {result && (
        <>
          <p className="mt-1 whitespace-pre-wrap break-words text-[11px] leading-relaxed text-ink-soft">
            {result.summary || "It reported nothing."}
          </p>
          {result.error && (
            <p className="mt-1 text-[11px] leading-relaxed text-danger">{result.error}</p>
          )}
          {result.rejected && (
            <p className="mt-1 text-[11px] leading-relaxed text-warn">
              Did not run: {result.rejected}
            </p>
          )}
        </>
      )}
    </div>
  );
}

function Verification({ result }: { result: ToolRunInfo }) {
  if (result.verified === null) {
    return (
      <StatusPill state="idle" size="sm">
        NO VERIFIER
      </StatusPill>
    );
  }
  return (
    <StatusPill state={result.verified ? "ok" : "pending"} size="sm">
      {result.verified ? "VERIFIED" : "UNVERIFIED"}
    </StatusPill>
  );
}

/** Tokens and latency, from the record rather than measured here (§26, §45). */
function UsageBlock({ turn }: { turn: LiveTurn }) {
  const finished = turn.finished;
  if (!finished) return null;
  const usage = Object.entries(finished.usage);
  return (
    <div className="chamfer-sm border border-line bg-base px-2.5 py-2">
      <span className="label">Cost</span>
      <div className="mt-1 flex flex-col gap-0.5 text-[11px] leading-relaxed text-ink-soft">
        <span>Latency: {duration(finished.latency_ms)}</span>
        {usage.map(([key, value]) => (
          <span key={key}>{key.replace(/_/g, " ")}: {value.toLocaleString()}</span>
        ))}
        {finished.message_id && <span>Message: {finished.message_id}</span>}
      </div>
    </div>
  );
}
