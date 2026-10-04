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
import { DataRow, Section, StatusPill, duration } from "./primitives";
import type { Preview } from "../lib/api";
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

      {turn.reasoning && (
        <div className="chamfer-sm border border-line bg-base px-2.5 py-2">
          <span className="label">Model reasoning, as sent</span>
          <p className="mt-1 whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-ink-soft">
            {turn.reasoning}
          </p>
        </div>
      )}

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
 *
 * Three facts here are carried by nothing else on the screen.
 *
 * The **voice is named** because a substitution must not read as a choice: the route
 * falls back to the provider's default when the requested voice is unavailable, and that
 * fallback is only visible if the voice actually used is printed.
 *
 * The **counts** are the queue. "6 of 8 sentences with audio" while a turn runs is a
 * real state, and a spinner in its place would be a guess about a fact the core already
 * sent.
 *
 * The **failures are listed one by one**, with the sentence and the provider's reason,
 * because silence where a sentence should have been is indistinguishable from the
 * assistant having nothing to say. That is the §61 failure this whole path exists to
 * avoid, and it cannot be avoided by the audio alone — only by saying so on screen.
 *
 * Code is counted rather than quoted: the segmenter says "a code block" out loud instead
 * of reciting forty seconds of punctuation, and this row is where the user finds out
 * that is what happened to it.
 */
function SpeechBlock({ turn }: { turn: LiveTurn }) {
  const { segments, speech, speechErrors, voice } = turn;
  if (!voice && segments.length === 0 && speech.length === 0 && speechErrors.length === 0) {
    // A typed, silent turn. An empty "Spoken" section would imply speech was attempted.
    return null;
  }
  const described = segments.filter((segment) => segment.source !== null);
  return (
    <Section
      title="Spoken"
      meta={
        segments.length > 0
          ? `${speech.length} of ${segments.length} sentence${segments.length === 1 ? "" : "s"} with audio`
          : undefined
      }
    >
      <div className="flex flex-col">
        <SpeechRows voice={voice} described={described.length} />
        {speechErrors.length > 0 && (
          <div className="px-3 py-1">
            <span className="label text-warn">Not spoken</span>
            <ul className="mt-0.5 flex flex-col gap-1">
              {speechErrors.map((failed) => (
                <li key={failed.index} className="text-[11px] leading-relaxed text-warn">
                  <span className="font-mono text-[10px] text-warn/70">#{failed.index + 1}</span>{" "}
                  {failed.text.trim() || "an empty sentence"} — {failed.error}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </Section>
  );
}

/** The two rows above the failures, split out only to keep `SpeechBlock` readable. */
function SpeechRows({
  voice,
  described,
}: {
  voice: LiveTurn["voice"];
  described: number;
}) {
  return (
    <>
      {voice ? (
        <DataRow
          label="Voice"
          value={`${voice.voice || "the provider default"} · ${voice.model}`}
          state="ok"
          title="The voice that actually spoke, which is not always the one that was asked for."
        />
      ) : (
        <p className="px-3 py-1 text-[11px] leading-relaxed text-ink-dim">
          The core named no voice for this turn, so the answer is on screen and not out
          loud.
        </p>
      )}
      {described > 0 && (
        <DataRow
          label="Described"
          value={`${described} code block${described === 1 ? "" : "s"} named rather than read out`}
          title="Reciting code aloud is unusable, so the voice says what it is and the text stays on screen."
        />
      )}
    </>
  );
}

/**
 * Why this model (§15, §26).
 *
 * `considered` and `fallbacks` are shown because "the router picked Nemotron" is not an
 * answer to "why" — the answer is which candidates it had, in what order, and what it
 * would fall back to. `warnings` is where the router says a preferred model was
 * unavailable, and hiding that would present a substitution as a choice (§30).
 */
function RoutingBlock({ routing, intent }: { routing: RoutingInfo; intent: IntentInfo | null }) {
  return (
    <Section title="Routing" meta={`${routing.job} · ${routing.transport}`}>
      <div className="flex flex-col">
        <DataRow label="Model" value={routing.label} state="ok" />
        <DataRow label="Provider" value={`${routing.provider} · ${routing.model}`} />
        <DataRow label="Because" value={routing.reason} />
        {intent && (
          <>
            <DataRow label="Read as" value={`${intent.intent} → ${intent.job}`} />
            <DataRow
              label="Confidence"
              value={
                isConfident(intent)
                  ? `${intent.score.toFixed(2)} — confident`
                  : `${intent.score.toFixed(2)} — not confident, so tools stayed attached`
              }
              state={isConfident(intent) ? "ok" : "pending"}
            />
            {intent.signals.length > 0 && (
              <DataRow label="Signals" value={intent.signals.join(", ")} />
            )}
          </>
        )}
        {routing.considered.length > 0 && (
          <DataRow
            label="Considered"
            value={routing.considered.map((item) => item.key).join(" → ")}
            title="In preference order. The first reachable one answers."
          />
        )}
        {routing.fallbacks.length > 0 && (
          <DataRow
            label="Fallbacks"
            value={routing.fallbacks.map((item) => item.key).join(" → ")}
            title="Used only if the chosen model fails."
          />
        )}
        {routing.warnings.map((warning, index) => (
          <p key={index} className="px-3 py-1 text-[11px] leading-relaxed text-warn">
            {warning}
          </p>
        ))}
      </div>
    </Section>
  );
}

/**
 * What went into the prompt, and what did not fit (§45).
 *
 * `dropped` is the reason this block exists. A model that answered without the memory
 * it needed answered differently, and the user is the only one who can tell whether
 * that mattered — so what was shed is stated rather than being an internal detail.
 */
function ContextBlock({ context }: { context: Partial<ContextInfo> }) {
  const blocks = context.blocks ?? [];
  const tokens = context.tokens;
  const budget = context.budget;
  // An unstarted turn has `context: {}` — the assembler had not run. Rendering a
  // "0 tokens of 0" row would claim it assembled an empty context, which is different.
  if (blocks.length === 0 && tokens === undefined) return null;
  return (
    <Section
      title="Context"
      meta={
        tokens !== undefined && budget !== undefined
          ? `${tokens.toLocaleString()} of ${budget.toLocaleString()} tokens`
          : undefined
      }
    >
      <div className="flex flex-col">
        {blocks.map((block, index) => (
          <DataRow
            key={`${block.kind}-${index}`}
            label={block.heading || block.kind}
            value={`${block.tokens.toLocaleString()} tokens`}
          />
        ))}
        {context.turns !== undefined && (
          <DataRow label="History" value={`${context.turns} earlier turns`} />
        )}
        {context.memories !== undefined && (
          <DataRow label="Memories" value={`${context.memories} recalled`} />
        )}
        {(context.dropped ?? []).length > 0 && (
          <div className="px-3 py-1">
            <span className="label text-warn">Did not fit</span>
            <ul className="mt-0.5 flex flex-col gap-0.5">
              {(context.dropped ?? []).map((item, index) => (
                <li key={index} className="font-mono text-[11px] leading-relaxed text-warn">
                  {item}
                </li>
              ))}
            </ul>
          </div>
        )}
        {(context.notices ?? []).map((notice, index) => (
          <p key={index} className="px-3 py-1 text-[11px] leading-relaxed text-ink-dim">
            {notice}
          </p>
        ))}
      </div>
    </Section>
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

      {preview && <PreviewLine preview={preview} />}

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

/** What the permission layer said about the call before it ran. */
function PreviewLine({ preview }: { preview: Preview }) {
  return (
    <p className="mt-1 font-mono text-[10px] leading-relaxed text-ink-faint">
      {preview.read_only ? "Read-only. " : ""}
      {preview.effect}
      {preview.permission.outcome && ` · permission: ${preview.permission.outcome}`}
      {preview.permission.reason && ` (${preview.permission.reason})`}
    </p>
  );
}

/** Tokens and latency, from the record rather than measured here (§26, §45). */
function UsageBlock({ turn }: { turn: LiveTurn }) {
  const finished = turn.finished;
  if (!finished) return null;
  const usage = Object.entries(finished.usage);
  return (
    <Section title="Cost" meta={`${finished.rounds} round${finished.rounds === 1 ? "" : "s"}`}>
      <div className="flex flex-col">
        <DataRow label="Latency" value={duration(finished.latency_ms)} />
        {usage.map(([key, value]) => (
          <DataRow key={key} label={key.replace(/_/g, " ")} value={value.toLocaleString()} />
        ))}
        {finished.message_id && <DataRow label="Message" value={finished.message_id} />}
      </div>
    </Section>
  );
}
