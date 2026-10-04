/**
 * MEMORY — what NEXUS remembers, and whether it can find it (§19, §20, §21, §22).
 *
 * The distinction this screen exists to make visible: memory is not conversation
 * history. A memory is an extracted, approved fact with its own row, its own category
 * and — if embedding is available — its own vector. §21 requires that the *reason*
 * something is remembered is inspectable, and §22 requires that "searchable by
 * meaning" is a state the user can check rather than assume.
 *
 * Four tabs, each backed by a real route:
 *
 * - **STORED** — `GET /api/memories`.
 * - **PROPOSED** — `GET /api/memories/pending`. The sweep proposes; a human approves.
 *   Nothing is remembered without that step, which is why this tab is separate.
 * - **RECALL** — `GET /api/memories/recall`, the same call the model makes. It reports
 *   `semantic_ran` and `semantic_hits` separately, and so does this screen: "meaning
 *   matching found nothing" and "meaning matching never ran" are different answers.
 * - **INDEX** — `GET /api/memories/embeddings` and the sweep's own status, because
 *   `unembedded > 0` means some memories can only be found by exact words.
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
  Field,
  IconButton,
  LoadingState,
  Panel,
  Section,
  Select,
  StatusPill,
  Tabs,
  TextArea,
  TextInput,
  relativeTime,
} from "../components/primitives";
import { api, type Memory, type Recall } from "../lib/api";
import { useAction, useAsync, type AsyncResult } from "../lib/useAsync";
import { useNexus } from "../state/nexus";

type MemoryTab = "stored" | "proposed" | "recall" | "index";

export function MemoryScreen() {
  const [tab, setTab] = useState<MemoryTab>("stored");
  const pending = useAsync(() => api.memories.pending(), []);

  return (
    <div className="flex h-full min-h-0 flex-col p-2">
      <Panel title="MEMORY" className="min-h-0" bodyClassName="flex min-h-0 flex-1 flex-col">
        <Tabs<MemoryTab>
          value={tab}
          onChange={setTab}
          tabs={[
            { value: "stored", label: "Stored" },
            { value: "proposed", label: "Proposed", badge: pending.data?.count ?? 0 },
            { value: "recall", label: "Recall" },
            { value: "index", label: "Index" },
          ]}
        />
        <div className="min-h-0 flex-1 overflow-y-auto">
          {tab === "stored" && <StoredTab />}
          {tab === "proposed" && <ProposedTab state={pending} />}
          {tab === "recall" && <RecallTab />}
          {tab === "index" && <IndexTab />}
        </div>
      </Panel>
    </div>
  );
}

/** STORED: the approved memories, with keyword search and a real create form. */
function StoredTab() {
  const { link } = useNexus();
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [composing, setComposing] = useState(false);

  // Two different response shapes behind one list: `/search` returns `{memories,
  // mode}`, the plain list returns `{memories, pending_count, coverage}`. Naming the
  // common shape explicitly is what lets one `useAsync` hold both — and `mode` stays
  // optional, so the "matched by" line only appears when the route actually said how
  // it matched rather than being filled in with a guess.
  const list = useAsync<{ memories: Memory[]; mode?: string }>(
    () => (submitted ? api.memories.search(submitted) : api.memories.list(false)),
    [submitted],
  );

  const memories = list.data?.memories ?? [];
  const mode = list.data?.mode ?? "";

  return (
    <div className="flex flex-col">
      <div className="flex items-center gap-2 border-b border-line px-3 py-2">
        <div className="flex-1">
          <TextInput
            value={query}
            onChange={setQuery}
            placeholder="Search stored memories by word"
            onEnter={() => setSubmitted(query.trim())}
          />
        </div>
        <Button size="sm" icon="search" onClick={() => setSubmitted(query.trim())}>
          Search
        </Button>
        {submitted && (
          <Button
            size="sm"
            tone="ghost"
            onClick={() => {
              setQuery("");
              setSubmitted("");
            }}
          >
            Clear
          </Button>
        )}
        <Button
          size="sm"
          tone="accent"
          icon="plus"
          onClick={() => setComposing(true)}
          disabled={!link.connected}
          reason="The core is not reachable, so it cannot store one."
        >
          Add
        </Button>
      </div>

      {submitted && mode && (
        <p className="border-b border-line/60 px-3 py-1 font-mono text-[10px] text-ink-faint">
          Matched by {mode}. This is word matching — the RECALL tab is the one that uses
          meaning.
        </p>
      )}

      {composing && <Composer onClose={() => setComposing(false)} onSaved={list.reload} />}

      {list.state === "loading" ? (
        <LoadingState what="memories" />
      ) : !list.data ? (
        <div className="p-2">
          <ErrorState
            what={list.error ?? "Memories did not load."}
            did="Nothing is listed, because an empty list would say NEXUS remembers nothing."
            next={list.offline ? "Start the core, then retry." : list.detail || "Retry."}
            offline={list.offline}
            onRetry={list.reload}
            compact
          />
        </div>
      ) : memories.length === 0 ? (
        <EmptyState
          icon="memory"
          title={submitted ? "No memory matched those words" : "Nothing remembered yet"}
          detail={
            submitted
              ? "Try the RECALL tab — it also matches by meaning, so it finds memories that use different words."
              : "Memories are extracted from conversations and proposed for your approval, or you can write one yourself."
          }
        />
      ) : (
        <ul>
          {memories.map((memory) => (
            <MemoryRow key={memory.id} memory={memory} onChanged={list.reload} />
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * One memory.
 *
 * `vector_model` is shown because it is the answer to "can this be found by meaning".
 * A memory with no vector is still stored and still recalled by keyword — saying
 * nothing about it would let the user assume semantic search covers everything.
 *
 * `score`/`via` are optional because only RECALL has them. A stored-list row that
 * printed "score 0.000" would be inventing a ranking that no route produced.
 */
function MemoryRow({
  memory,
  onChanged,
  score,
  via,
}: {
  memory: Memory;
  onChanged: () => void;
  score?: number;
  via?: string;
}) {
  const [confirmDelete, setConfirmDelete] = useState(false);

  const pin = useAction(async () => {
    await api.memories.update(memory.id, { pinned: !memory.pinned });
    onChanged();
    return memory.pinned ? "Unpinned." : "Pinned — it is now preferred during recall.";
  });

  const approve = useAction(async () => {
    const result = await api.memories.approve(memory.id);
    onChanged();
    return `Approved. Indexing: ${result.indexing}.`;
  });

  const remove = useAction(async () => {
    const result = await api.memories.remove(memory.id);
    setConfirmDelete(false);
    onChanged();
    // The route reports whether it re-read the row to confirm the delete. An
    // unverified delete is still reported as unverified rather than as done.
    return result.verified ? result.message : `${result.message} — not verified.`;
  });

  return (
    <li className="border-b border-line/60 px-3 py-2">
      <div className="flex items-start gap-2">
        <Icon
          name={memory.pinned ? "pin" : "memory"}
          size={13}
          className={`mt-0.5 shrink-0 ${memory.pinned ? "text-accent" : "text-ink-faint"}`}
        />
        <div className="min-w-0 flex-1">
          <p className="break-words text-xs font-medium text-ink">{memory.title}</p>
          <p className="mt-1 whitespace-pre-wrap break-words text-[11px] leading-relaxed text-ink-soft">
            {memory.body}
          </p>
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            <StatusPill state="idle" size="sm">
              {memory.category}
            </StatusPill>
            {memory.approved ? (
              <StatusPill state="ok" size="sm">
                APPROVED
              </StatusPill>
            ) : (
              <StatusPill state="pending" size="sm">
                AWAITING YOUR APPROVAL
              </StatusPill>
            )}
            <StatusPill state={memory.vector_model ? "ok" : "warn"} size="sm">
              {memory.vector_model ? "FINDABLE BY MEANING" : "WORDS ONLY"}
            </StatusPill>
            {score !== undefined && (
              <span
                className="font-mono text-[10px] text-ink-faint"
                title={via ? `Matched by ${via}` : "Higher is closer."}
              >
                score {score.toFixed(3)}
                {via ? ` · ${via}` : ""}
              </span>
            )}
            <span className="font-mono text-[10px] text-ink-faint">
              {relativeTime(memory.updated_at)}
            </span>
          </div>
          <ActionResult done={pin.done} failed={pin.failed} onClear={pin.clear} />
          <ActionResult done={approve.done} failed={approve.failed} onClear={approve.clear} />
          <ActionResult done={remove.done} failed={remove.failed} onClear={remove.clear} />
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {!memory.approved && (
            <IconButton
              name="check"
              label="Approve this memory so NEXUS may use it"
              onClick={approve.run}
              busy={approve.busy}
            />
          )}
          <IconButton
            name="pin"
            label={memory.pinned ? "Unpin this memory" : "Pin this memory"}
            onClick={pin.run}
            busy={pin.busy}
          />
          <IconButton
            name="trash"
            label="Forget this memory"
            tone="danger"
            onClick={() => setConfirmDelete(true)}
          />
        </div>
      </div>

      <ConfirmDialog
        open={confirmDelete}
        title="Forget this memory"
        consequence={`"${memory.title}" is deleted from the local database along with its vector. NEXUS stops using it immediately, and this cannot be undone.`}
        confirmLabel="Forget it"
        onConfirm={remove.run}
        onCancel={() => setConfirmDelete(false)}
        busy={remove.busy}
        failed={remove.failed}
      />
    </li>
  );
}

/**
 * PROPOSED: what the sweep extracted, waiting on a human (§20).
 *
 * This tab is the approval gate made literal. The sweep reads finished conversations
 * with a small model and writes candidates with `approved = 0`; recall only returns
 * approved rows, so nothing here is in play yet. Rejection is deletion — there is no
 * "reject" route, and inventing a third state (dismissed-but-kept) would claim a
 * column the database does not have.
 *
 * The list is passed in rather than loaded here: the tab strip shows the same count as
 * its badge, and two independent fetches could disagree with each other on screen.
 */
function ProposedTab({ state }: { state: AsyncResult<{ memories: Memory[]; count: number }> }) {
  const memories = state.data?.memories ?? [];

  return (
    <div className="flex flex-col">
      <p className="border-b border-line px-3 py-2 text-[11px] leading-relaxed text-ink-soft">
        A small model reads finished conversations and proposes facts worth keeping.
        Nothing here is used until you approve it — recall only reads approved rows.
        Rejecting one deletes it.
      </p>

      {state.state === "loading" ? (
        <LoadingState what="proposed memories" />
      ) : !state.data ? (
        <div className="p-2">
          <ErrorState
            what={state.error ?? "The proposal queue did not load."}
            did="Nothing is listed. An empty queue would say the sweep found nothing to propose."
            next={state.offline ? "Start the core, then retry." : state.detail || "Retry."}
            offline={state.offline}
            onRetry={state.reload}
            compact
          />
        </div>
      ) : memories.length === 0 ? (
        <EmptyState
          icon="check"
          title="Nothing waiting"
          detail="Either the sweep has not run since your last conversation, or it found nothing worth remembering. INDEX shows when it last ran and what it examined."
        />
      ) : (
        <ul>
          {memories.map((memory) => (
            <MemoryRow key={memory.id} memory={memory} onChanged={state.reload} />
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * RECALL: the model's own lookup, run by hand (§22).
 *
 * This calls `/api/memories/recall` — the same route the assistant calls when it
 * assembles context — so what appears here is what the model would have been given.
 * It can involve an embedding provider, which is why it is a button rather than a
 * search-as-you-type: each keystroke would be a paid remote call.
 */
function RecallTab() {
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState("");

  // `null` is the un-asked state, distinct from a recall that returned no items.
  const recall = useAsync(
    async () => (submitted ? await api.memories.recall(submitted, 8) : null),
    [submitted],
  );

  return (
    <div className="flex flex-col">
      <div className="flex items-center gap-2 border-b border-line px-3 py-2">
        <div className="flex-1">
          <TextInput
            value={query}
            onChange={setQuery}
            placeholder="Ask the way you would ask NEXUS"
            onEnter={() => setSubmitted(query.trim())}
          />
        </div>
        <Button
          size="sm"
          tone="accent"
          icon="search"
          onClick={() => setSubmitted(query.trim())}
          busy={Boolean(submitted) && recall.state === "loading"}
        >
          Recall
        </Button>
      </div>

      {!submitted ? (
        <EmptyState
          icon="search"
          title="Nothing recalled yet"
          detail="This runs the same lookup the model runs mid-turn: meaning first, words as well. It reports which of the two actually found something, so you can tell a bad match from a missing index."
        />
      ) : recall.state === "loading" ? (
        <LoadingState what="recall — this may call the embedding provider" />
      ) : !recall.data ? (
        <div className="p-2">
          <ErrorState
            what={recall.error ?? "The recall did not complete."}
            did="No results are shown. Showing keyword hits alone would look like a complete answer."
            next={recall.offline ? "Start the core, then retry." : recall.detail || "Retry."}
            offline={recall.offline}
            onRetry={recall.reload}
            compact
          />
        </div>
      ) : (
        <div className="flex flex-col">
          <div className="p-2">
            <RecallSummary recall={recall.data} />
          </div>
          {recall.data.items.length === 0 ? (
            <EmptyState
              icon="memory"
              title="No memory was returned"
              detail={
                recall.data.semantic_ran
                  ? "Meaning matching ran and nothing was close enough to the threshold, and no memory contained these words either."
                  : "Only word matching ran, and no memory contains these words. A memory that says the same thing differently would not be found — INDEX explains why meaning matching did not run."
              }
            />
          ) : (
            <ul>
              {recall.data.items.map((item) => (
                <MemoryRow
                  key={item.id}
                  memory={item}
                  onChanged={recall.reload}
                  score={item.score}
                  via={item.via}
                />
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * How the recall matched (§22, §30).
 *
 * `semantic_ran` and `semantic_hits` are rendered as two rows because they answer two
 * questions. "Meaning matching ran, nothing was relevant" is a fact about the query;
 * "meaning matching never ran" is a fact about the machine, and it means every result
 * on screen is a word match that could be missing paraphrases.
 */
function RecallSummary({ recall }: { recall: Recall }) {
  return (
    <Section title="How it matched" meta={`${recall.items.length} returned`}>
      <div className="flex flex-col">
        <DataRow
          label="Meaning matching"
          value={
            recall.semantic_ran
              ? `ran · ${recall.semantic_hits} hit${recall.semantic_hits === 1 ? "" : "s"}`
              : "did not run"
          }
          state={recall.semantic_ran ? (recall.semantic_hits > 0 ? "ok" : "pending") : "warn"}
          title={
            recall.semantic_ran
              ? "The query was embedded and compared against stored vectors."
              : "No vector comparison happened for this query."
          }
        />
        <DataRow
          label="Word matching"
          value={`${recall.keyword_hits} hit${recall.keyword_hits === 1 ? "" : "s"}`}
          state={recall.keyword_hits > 0 ? "ok" : "idle"}
        />
        <DataRow
          label="Semantic search"
          value={recall.semantic ? "configured and available" : "not available"}
          state={recall.semantic ? "ok" : "warn"}
        />
        {recall.notice && (
          <p className="px-3 py-1 text-[11px] leading-relaxed text-warn">{recall.notice}</p>
        )}
        {!recall.semantic_ran && (
          <p className="px-3 py-1 text-[11px] leading-relaxed text-warn">
            These are word matches only. A memory that records the same fact in
            different words was not considered — the INDEX tab says what is missing.
          </p>
        )}
      </div>
    </Section>
  );
}

/**
 * INDEX: whether "searchable by meaning" is actually true right now (§22, §30).
 *
 * Two independent facts, two panels. The embedding side answers "can a memory be found
 * by meaning at all" — `unembedded > 0` means some rows can only be found by the user's
 * exact words, and `model_mismatch` means stored vectors came from a different model
 * than the one now configured, which makes their distances incomparable. The sweep side
 * answers "is anything still being proposed", including `stopped_reason`, because a
 * sweep that quietly stopped looks exactly like a sweep that found nothing.
 */
function IndexTab() {
  const { link } = useNexus();
  const embeddings = useAsync(() => api.memories.embeddings(), []);
  const sweep = useAsync(() => api.memories.sweep(), []);

  const backfill = useAction(async () => {
    const result = await api.memories.backfill();
    embeddings.reload();
    const { embedded, failed, pending } = result.backfill;
    return `Embedded ${embedded}. Failed ${failed}. Still pending ${pending}.`;
  });

  const runSweep = useAction(async () => {
    const result = await api.memories.sweepRun();
    sweep.reload();
    if (result.result.error) {
      throw new Error(`The sweep ran and failed: ${result.result.error}`);
    }
    return `${result.message} Examined ${result.result.examined}, proposed ${result.result.proposed}.`;
  });

  const index = embeddings.data?.embeddings;
  const status = sweep.data?.sweep;

  return (
    <div className="flex flex-col gap-2 p-2">
      {embeddings.state === "loading" ? (
        <LoadingState what="the embedding index" />
      ) : !index ? (
        <ErrorState
          what={embeddings.error ?? "The embedding index did not load."}
          did="Nothing is claimed about semantic search, because claiming it works is the failure that matters here."
          next={embeddings.offline ? "Start the core, then retry." : embeddings.detail || "Retry."}
          offline={embeddings.offline}
          onRetry={embeddings.reload}
          compact
        />
      ) : (
        <Section
          title="Meaning index"
          meta={`${index.embedded} of ${index.approved} embedded`}
          actions={
            <Button
              size="sm"
              icon="refresh"
              onClick={backfill.run}
              busy={backfill.busy}
              disabled={!link.connected || !index.available || index.unembedded === 0}
              reason={
                !link.connected
                  ? "The core is not reachable."
                  : !index.available
                    ? `Embedding is unavailable: ${index.reason || "no reason given"}.`
                    : "Every approved memory already has a vector."
              }
            >
              Embed the rest
            </Button>
          }
        >
          <div className="flex flex-col">
            <DataRow
              label="Semantic search"
              value={index.enabled ? (index.available ? "on" : "on, but not usable") : "off"}
              state={index.enabled ? (index.available ? "ok" : "failed") : "disabled"}
            />
            {!index.available && index.reason && (
              <p className="px-3 py-1 text-[11px] leading-relaxed text-warn">{index.reason}</p>
            )}
            <DataRow label="Model" value={index.model || "none configured"} />
            <DataRow
              label="Not embedded"
              value={
                index.unembedded === 0
                  ? "none — every approved memory has a vector"
                  : `${index.unembedded} findable by exact words only`
              }
              state={index.unembedded === 0 ? "ok" : "warn"}
            />
            {index.model_mismatch && (
              <p className="px-3 py-1 text-[11px] leading-relaxed text-warn">
                Stored vectors came from more than one model
                {index.vector_models.length > 0 ? ` (${index.vector_models.join(", ")})` : ""}.
                Distances from different models are not comparable, so recall ranking is
                unreliable until these are re-embedded.
              </p>
            )}
            <DataRow
              label="Match threshold"
              value={index.threshold.toFixed(2)}
              title="A memory below this similarity is not returned by meaning matching."
            />
          </div>
          <ActionResult done={backfill.done} failed={backfill.failed} onClear={backfill.clear} />
        </Section>
      )}

      {sweep.state === "loading" ? (
        <LoadingState what="the extraction sweep" />
      ) : !status ? (
        <ErrorState
          what={sweep.error ?? "The sweep status did not load."}
          did="Nothing is shown about extraction. A silent panel would imply it is running."
          next={sweep.offline ? "Start the core, then retry." : sweep.detail || "Retry."}
          offline={sweep.offline}
          onRetry={sweep.reload}
          compact
        />
      ) : (
        <Section
          title="Extraction sweep"
          meta={`${status.runs} run${status.runs === 1 ? "" : "s"}`}
          actions={
            <Button
              size="sm"
              icon="play"
              onClick={runSweep.run}
              busy={runSweep.busy}
              disabled={!link.connected || status.running}
              reason={
                !link.connected ? "The core is not reachable." : "A sweep is running right now."
              }
            >
              Run it now
            </Button>
          }
        >
          <div className="flex flex-col">
            <DataRow
              label="State"
              value={
                status.running
                  ? "running now"
                  : status.enabled
                    ? `idle · every ${Math.round(status.interval_seconds / 60)} min`
                    : "off"
              }
              state={status.running ? "active" : status.enabled ? "ok" : "disabled"}
            />
            {status.stopped_reason && (
              <p className="px-3 py-1 text-[11px] leading-relaxed text-warn">
                It stopped on its own: {status.stopped_reason}. Nothing new is being
                proposed until it is started again.
              </p>
            )}
            <DataRow
              label="Waiting to be read"
              value={
                status.backlog === 0
                  ? "nothing — every finished conversation has been examined"
                  : `${status.backlog} conversation${status.backlog === 1 ? "" : "s"}`
              }
              state={status.backlog === 0 ? "ok" : "pending"}
            />
            <DataRow
              label="Batch"
              value={`${status.batch_size} per run, after ${status.grace_seconds}s of quiet`}
              title="A conversation is only read once it has been silent this long, so a live thread is not extracted mid-sentence."
            />
            {status.last ? (
              <DataRow
                label="Last run"
                value={
                  status.last.error
                    ? `failed: ${status.last.error}`
                    : `examined ${status.last.examined}, proposed ${status.last.proposed}${
                        status.last.finished_at ? ` · ${relativeTime(status.last.finished_at)}` : ""
                      }`
                }
                state={status.last.error ? "failed" : "ok"}
              />
            ) : (
              <DataRow label="Last run" value="it has not run yet" state="idle" />
            )}
            {status.cursor && (
              <DataRow
                label="Read up to"
                value={status.cursor}
                title="Conversations updated after this point have not been examined."
              />
            )}
          </div>
          <ActionResult done={runSweep.done} failed={runSweep.failed} onClear={runSweep.clear} />
        </Section>
      )}
    </div>
  );
}

/**
 * The eight real categories, from `db/repos/memories.py:CATEGORIES`.
 *
 * A free-text field would be worse than useless here: the repository silently rewrites
 * an unrecognised category to "general", so a typo would be accepted, stored as
 * something else, and never reported. A closed list cannot make that mistake.
 */
const CATEGORIES = [
  "general",
  "preference",
  "project",
  "person",
  "task",
  "file",
  "device",
  "goal",
] as const;

type Category = (typeof CATEGORIES)[number];

/**
 * Writing a memory by hand.
 *
 * The route is explicit that a memory the user writes is approved on creation, while
 * the model's door (`memories.propose`) is not — so this form says so instead of
 * leaving the user to guess whether their own note needs approving. `indexing` is
 * echoed verbatim because it is the only report of whether a vector was actually
 * written; a green "Saved" that hid a failed embedding would be the exact lie this
 * screen exists to prevent.
 */
function Composer({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [category, setCategory] = useState<Category>("general");

  const save = useAction(async () => {
    const cleanTitle = title.trim();
    const cleanBody = body.trim();
    // The same rule the route enforces, checked here so the failure is immediate
    // rather than a 400 round-trip.
    if (!cleanTitle || !cleanBody) {
      throw new Error("A memory needs both a title and something to remember.");
    }
    const result = await api.memories.create(cleanTitle, cleanBody, category);
    onSaved();
    setTitle("");
    setBody("");
    return `Stored and approved. Indexing: ${result.indexing}.`;
  });

  return (
    <div className="flex flex-col gap-2 border-b border-line bg-raised/40 px-3 py-2.5">
      <div className="flex items-center justify-between">
        <span className="label">Write a memory</span>
        <IconButton name="close" label="Close without saving" onClick={onClose} />
      </div>

      <Field label="Title" htmlFor="memory-title" hint="What this is about, in a few words.">
        <TextInput
          id="memory-title"
          value={title}
          onChange={setTitle}
          autoFocus
          placeholder="Prefers dark themes"
        />
      </Field>

      <Field
        label="What to remember"
        htmlFor="memory-body"
        hint="Write it as a fact. This exact text is what goes into the model's context."
      >
        <TextArea
          id="memory-body"
          value={body}
          onChange={setBody}
          rows={3}
          placeholder="Always use the cyan accent and avoid light backgrounds."
        />
      </Field>

      <Field
        label="Category"
        htmlFor="memory-category"
        hint="One of eight fixed categories. Anything else would be silently stored as general."
      >
        <Select<Category>
          id="memory-category"
          value={category}
          onChange={setCategory}
          options={CATEGORIES.map((value) => ({ value, label: value }))}
        />
      </Field>

      <p className="text-[11px] leading-relaxed text-ink-dim">
        A memory you write yourself is approved immediately — it does not wait in
        PROPOSED. Only the ones the model proposes need approving.
      </p>

      <div className="flex items-center gap-2">
        <Button size="sm" tone="accent" icon="check" onClick={save.run} busy={save.busy}>
          Remember it
        </Button>
        <Button size="sm" tone="ghost" onClick={onClose}>
          Cancel
        </Button>
      </div>

      <ActionResult done={save.done} failed={save.failed} onClear={save.clear} />
    </div>
  );
}
