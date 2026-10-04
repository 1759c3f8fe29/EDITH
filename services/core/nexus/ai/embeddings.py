"""Semantic recall: memory that matches meaning, not spelling (tasks F6, B5-B6).

Keyword search finds a memory when the user reuses their own words. It fails the
moment they paraphrase -- "what did I say about the editor" will not find "I use
Neovim for everything". That gap is the difference between a memory store and a
memory, so both search paths run and their results merge.

Three facts govern this module, and each one is a real failure that shaped it:

* **Embedding is a network call and can be unavailable.** When it is, search
  degrades to keyword-only and *says so*. A search that silently drops half its
  strategy looks identical to a search that found nothing, which would teach the
  user their memory is empty when it is only unreachable.

* **The write and read sides use different ``input_type`` values.** These models
  are trained asymmetrically; embedding a query as a passage measurably degrades
  recall. Both sides are set explicitly here rather than relying on a default.

* **A stored vector belongs to the model that produced it.** Change embedding
  models and the old vectors do not become wrong, they become incomparable --
  :func:`nexus.db.repos.memories.cosine` returns 0.0 on a dimension mismatch, so
  those rows silently never match. :func:`status` reports the mismatch instead of
  leaving the user with a memory that has quietly stopped answering.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..db.repos import app_settings, memories
from ..logging_setup import get_logger
from ..providers import nim_media
from ..providers.base import ProviderError
from ..router import NoModelAvailable, resolve

log = get_logger("ai.embeddings")

#: Cosine floor for a semantic hit. Chosen by measurement rather than taste: with
#: ``nemotron-3-embed-1b``, unrelated personal notes score around 0.1-0.2, and
#: genuine paraphrases score above 0.4. A threshold under ~0.25 starts returning
#: everything, which is worse than returning nothing because it looks like recall.
SEMANTIC_THRESHOLD = 0.30

#: How many memories to embed in one request. The endpoint accepts batches, and a
#: backfill of a few hundred memories as individual calls would take minutes of
#: round trips for work that fits in a handful.
BATCH = 32


class EmbeddingUnavailable(RuntimeError):
    """No embedding model can be reached, with a sentence explaining why.

    Deliberately distinct from :class:`ProviderError`: callers that can degrade to
    keyword search need to tell "semantic search is off right now" apart from
    "this specific request failed", and only the first is a reason to change what
    the UI says about memory.
    """


# ----------------------------------------------------------------- embedding


async def embed(texts: list[str], *, input_type: str) -> tuple[list[list[float]], str]:
    """Embed texts, returning the vectors and the model that produced them.

    The model id comes back with the vectors because it has to be stored beside
    them -- a vector without its model is unusable the moment the model changes.
    """
    cleaned = [t for t in (s.strip() for s in texts) if t]
    if not cleaned:
        return [], ""
    try:
        decision = resolve("embedding")
    except NoModelAvailable as exc:
        raise EmbeddingUnavailable(str(exc)) from exc

    vectors = await nim_media.client().embed(
        cleaned, input_type=input_type, model=decision.model_id
    )
    return vectors, decision.model_id


def _text_of(memory: dict[str, Any]) -> str:
    """What actually gets embedded for a memory.

    Title and body together, because the title alone is often the only place the
    subject is named ("Neovim" in the title, "I use it for everything" in the
    body) and embedding the body alone loses it.
    """
    title = str(memory.get("title") or "").strip()
    body = str(memory.get("body") or "").strip()
    return f"{title}\n{body}".strip() if title else body


# --------------------------------------------------------------- indexing


async def index(memory_id: str) -> bool:
    """Embed one memory and store its vector. False when there is nothing to embed.

    Called after a memory is created or approved. Failure is logged and swallowed
    rather than raised: an unreachable embedding endpoint must not stop the user
    from saving what they asked to save. The memory is still there, still found by
    keyword, and :func:`backfill` picks it up later.
    """
    memory = memories.get(memory_id)
    if memory is None:
        return False
    text = _text_of(memory)
    if not text:
        return False
    try:
        vectors, model = await embed([text], input_type="passage")
    except (EmbeddingUnavailable, ProviderError) as exc:
        log.info("memory %s left unembedded: %s", memory_id, exc)
        return False
    if not vectors:
        return False
    memories.set_vector(memory_id, vectors[0], model)
    return True


async def backfill(*, limit: int = 500) -> dict[str, Any]:
    """Embed every approved memory that has no usable vector.

    Runs at startup and after an embedding model change. Reports counts rather
    than raising, because "142 of 150 embedded, the rest timed out" is a state the
    user can act on and an exception is not.
    """
    pending = memories.unembedded(limit=limit)
    if not pending:
        return {"embedded": 0, "failed": 0, "pending": 0, "model": ""}

    embedded = 0
    failed = 0
    model_used = ""
    for start in range(0, len(pending), BATCH):
        chunk = pending[start : start + BATCH]
        texts = [_text_of(m) for m in chunk]
        pairs = [(m, t) for m, t in zip(chunk, texts) if t]
        if not pairs:
            continue
        try:
            vectors, model = await embed([t for _, t in pairs], input_type="passage")
        except (EmbeddingUnavailable, ProviderError) as exc:
            # Stop the whole backfill rather than hammering a provider that is
            # down: the remaining count is reported so the caller knows work is
            # left, and the next run resumes from exactly here.
            log.info("backfill stopped after %d: %s", embedded, exc)
            failed += len(pending) - embedded
            break
        model_used = model or model_used
        for (memory, _), vector in zip(pairs, vectors):
            memories.set_vector(memory["id"], vector, model)
            embedded += 1

    return {
        "embedded": embedded,
        "failed": failed,
        "pending": max(0, len(pending) - embedded),
        "model": model_used,
    }


# ------------------------------------------------------- background scheduling

#: Strong references to in-flight indexing tasks. Required, not tidiness:
#: ``asyncio`` holds only a *weak* reference to a task, so a fire-and-forget
#: ``create_task`` whose result nobody awaits can be garbage collected mid-flight
#: and the embedding simply never happens -- with no error, because the coroutine
#: was cancelled rather than raised. Holding the reference until the task is done
#: is the difference between "indexed in the background" being true and being a
#: sentence in a docstring.
_IN_FLIGHT: set[asyncio.Task[Any]] = set()


def _track(task: asyncio.Task[Any]) -> None:
    _IN_FLIGHT.add(task)
    task.add_done_callback(_IN_FLIGHT.discard)


async def _index_tracked(memory_id: str) -> bool:
    """``index`` with its own task registered, whichever door scheduled it.

    Registering from *inside* the coroutine means both scheduling paths below --
    a task created on this loop, and one submitted from a worker thread -- end up
    tracked the same way, so :func:`drain` has one set to wait on rather than two
    kinds of future to reconcile.
    """
    task = asyncio.current_task()
    if task is not None:
        _track(task)
    return await index(memory_id)


def schedule_index(memory_id: str) -> str:
    """Embed a memory in the background. Returns "" when scheduled, else why not.

    The write is already committed by the time this is called, so indexing must
    never be in the request's critical path: an embedding call against a cold
    endpoint has been measured here at 50-110 s, and a user clicking "approve"
    should not wait on that to find out their memory was saved. The caller reports
    the returned sentence, because a memory that is stored but not yet searchable
    by meaning is a real state and the honest thing is to name it (§60).

    Two scheduling paths, because callers arrive from two places and only one of
    them has a running loop. FastAPI runs a ``def`` route in a worker thread, so
    ``get_running_loop()`` raises there -- the first version of this function
    therefore returned "it will be indexed on the next startup backfill" for every
    single HTTP approval, which is a true sentence describing a feature that never
    ran in a long-lived session. The bus's loop is used from a thread instead, the
    same mechanism the tool handlers already use.
    """
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.create_task(_index_tracked(memory_id))
        return ""

    from ..core.events import bus

    thread_loop = bus.loop
    if thread_loop is None:
        return "It will be indexed for semantic search on the next startup backfill."
    try:
        asyncio.run_coroutine_threadsafe(_index_tracked(memory_id), thread_loop)
    except RuntimeError as exc:  # loop shutting down
        log.info("could not schedule embedding for %s: %s", memory_id, exc)
        return "Semantic indexing could not be started; keyword search still finds it."
    return ""


def schedule_backfill(*, limit: int = 500) -> str:
    """Run :func:`backfill` in the background, once, without blocking the caller.

    Called at startup. It deliberately does not block the app from serving: the
    UI's first request must not queue behind an embedding round trip, and a core
    that answers "still indexing 140 memories" is more useful than one that has
    not finished starting. Returns "" when scheduled.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return "No event loop is running, so no backfill was started."

    async def _run() -> None:
        pending = memories.unembedded(limit=1)
        if not pending:
            log.info("embedding backfill not needed: every approved memory has a vector")
            return
        result = await backfill(limit=limit)
        # Logged at info with real counts rather than declared successful: a
        # backfill that embedded 0 of 6 because the endpoint was unreachable and
        # one that embedded 6 of 6 must not read the same in the log.
        log.info(
            "embedding backfill finished",
            extra={k: v for k, v in result.items()},
        )

    _track(loop.create_task(_run()))
    return ""


async def drain(*, timeout: float = 5.0) -> int:
    """Wait briefly for in-flight indexing at shutdown. Returns how many were left.

    Bounded on purpose: a shutdown that hangs on a slow provider is worse than one
    unembedded memory, which the next startup backfill picks up anyway.
    """
    if not _IN_FLIGHT:
        return 0
    pending = set(_IN_FLIGHT)
    done, still_running = await asyncio.wait(pending, timeout=timeout)
    for task in still_running:
        task.cancel()
    return len(still_running)


# ------------------------------------------------------------------ search


async def search(
    query: str,
    *,
    limit: int = 8,
    threshold: float = SEMANTIC_THRESHOLD,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Find memories relevant to a query, by meaning and by keyword.

    Returns the merged hits plus how they were found, because the caller needs the
    second part: the context assembler labels retrieved memories with their
    provenance, and the Memory screen shows "keyword only -- semantic search is
    unavailable" rather than an unexplained thin result.
    """
    query = (query or "").strip()
    if not query:
        return {"items": [], "semantic": False, "semantic_ran": False, "notice": "",
                "keyword_hits": 0, "semantic_hits": 0}

    keyword = memories.search_text(query, limit=limit * 2)
    for item in keyword:
        item["match"] = "keyword"

    semantic: list[dict[str, Any]] = []
    # Whether the semantic half *ran*, which is a different fact from whether it
    # found anything. Reporting one as the other was a real bug here: a query with
    # no semantic match returned ``semantic: False`` with no notice, so the tool
    # handler told the model "keyword only" about a search where meaning matching
    # had worked perfectly and correctly matched nothing. That is the same
    # confusion this module's docstring warns about, inverted -- an available
    # capability reported as missing instead of the reverse.
    ran = False
    notice = ""
    enabled = bool(app_settings.get("semantic_search_enabled", True))
    if not enabled:
        notice = "Semantic search is turned off in Settings, so this is keyword-only."
    else:
        try:
            vectors, model = await embed([query], input_type="query")
            if vectors:
                ran = True
                semantic = memories.search_semantic(
                    vectors[0], limit=limit * 2, threshold=threshold
                )
                for item in semantic:
                    item["match"] = "semantic"
                stale = memories.vector_models()
                if stale and model and model not in stale:
                    # Every stored vector came from a different model, so cosine
                    # scores 0.0 against all of them and semantic search returns
                    # nothing at all. Silence here would read as "no memories
                    # match", which is a lie about why.
                    notice = (
                        f"Stored embeddings came from {', '.join(sorted(stale))} but "
                        f"queries now use {model}. They cannot be compared, so "
                        "semantic search will stay empty until memories are "
                        "re-embedded."
                    )
        except (EmbeddingUnavailable, ProviderError) as exc:
            ran = False
            notice = f"Keyword-only: semantic search is unavailable. {exc}"

    if ran and not semantic and not notice and not memories.coverage()["embedded"]:
        # The query embedded fine and matched nothing because there is nothing to
        # match: no memory has a vector. "Nothing relevant" and "nothing indexed"
        # are different answers and only the second is actionable.
        notice = (
            "Semantic search ran but no memory has been embedded yet, so only "
            "keyword matching could contribute. Run an embedding backfill."
        )

    merged = _merge(semantic, keyword, limit=limit, project_id=project_id)
    return {
        "items": merged,
        # "Was this a semantic search?" -- true when the meaning half ran, whether
        # or not it matched. Callers use it to decide whether to say "keyword only",
        # and a correct empty semantic result is not keyword-only.
        "semantic": ran,
        "semantic_ran": ran,
        "semantic_hits": len(semantic),
        "keyword_hits": len(keyword),
        "notice": notice,
    }


def _merge(
    semantic: list[dict[str, Any]],
    keyword: list[dict[str, Any]],
    *,
    limit: int,
    project_id: str | None,
) -> list[dict[str, Any]]:
    """Interleave the two result sets, semantic first, without duplicates.

    Not a weighted score across both: FTS bm25 and cosine are not on a common
    scale, and inventing one would produce a confident ordering with no meaning
    behind it. Alternating preserves each ranking's own judgement and puts a
    memory found by both at the top, which is the signal that actually matters.
    """
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    both = {m["id"] for m in semantic} & {m["id"] for m in keyword}

    for item in semantic:
        if item["id"] in both:
            item["match"] = "both"

    for source in (
        [m for m in semantic if m["id"] in both],
        semantic,
        keyword,
    ):
        for item in source:
            mid = item["id"]
            if mid in seen:
                continue
            if project_id and item.get("project_id") not in (None, "", project_id):
                # Project context stays isolated unless explicitly widened (§20).
                continue
            seen.add(mid)
            ordered.append(item)
            if len(ordered) >= limit:
                return ordered
    return ordered


# ------------------------------------------------------------------ status


def status() -> dict[str, Any]:
    """What the Memory screen shows about recall, with no network call.

    Answers "is my memory actually searchable" from stored state alone, so opening
    Settings never waits on a provider.
    """
    cover = memories.coverage()
    models = memories.vector_models()
    model_id = ""
    try:
        decision = resolve("embedding")
        model = decision.key
        model_id = decision.model_id
        available = True
        reason = ""
    except NoModelAvailable as exc:
        model = ""
        available = False
        reason = str(exc)

    mismatch = bool(models) and bool(model_id) and model_id not in models
    return {
        "enabled": bool(app_settings.get("semantic_search_enabled", True)),
        "available": available,
        "reason": reason,
        "model": model,
        "approved": cover.get("approved", 0),
        "embedded": cover.get("embedded", 0),
        "unembedded": max(0, cover.get("approved", 0) - cover.get("embedded", 0)),
        "vector_models": sorted(models),
        "model_mismatch": mismatch,
        "threshold": SEMANTIC_THRESHOLD,
    }


__all__ = [
    "BATCH",
    "SEMANTIC_THRESHOLD",
    "EmbeddingUnavailable",
    "backfill",
    "drain",
    "embed",
    "index",
    "schedule_backfill",
    "schedule_index",
    "search",
    "status",
]
