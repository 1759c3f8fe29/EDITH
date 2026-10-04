"""Which model does which job (task E5; spec §9, §10, §13, §14, §15).

NEXUS does not have "a model". It has a main model and a bench of specialists,
and this file is the list of jobs plus the order in which candidates are tried
for each one.

``deepseek-v4-flash-0731`` is the main model: it heads the conversation chain, so
unless a user pins something else it is what answers when you talk to NEXUS. It is
named once, as ``MAIN_MODEL``, and three other places have to agree with it -- the
router's fallback settings and the database defaults both derive from or are pinned
to it, because a second spelling of "the main model" does not raise when it drifts,
it just stops the Settings screen from doing anything.

It does not head everything, and that is deliberate. Four chains are led by a
specialist because a specialist is genuinely better at that job:

* **expert** is led by ``deepseek-v4-pro-0813``, the heavier of the two V4
  variants. This is the one chain the user chooses rather than the classifier:
  Expert Mode is an explicit "take your time and get this right", and the whole
  point of a mode is that it changes which model answers. Its chain still ends at
  the main model, so an unreachable specialist degrades the answer rather than
  refusing the request -- and the user is told which model actually served them,
  because answering an Expert request from the fast model without saying so is a
  silent substitution.
* **reasoning** is led by ``nemotron-3-ultra-550b-a55b`` -- 550B parameters with
  55B active, the heaviest thing NEXUS can reach. It is slow to first token,
  which is exactly the trade you want when the request is "work this out" and
  exactly the trade you do not want for chat, so it heads reasoning and merely
  backs up conversation.
* **coding and agent work** is led by ``deepseek-v4-pro-0813`` as well, then
  ``kimi-k3``, then the main model.
* **vision** and **video** are led by NVIDIA's VL models, which are stronger at
  this than the main model and free at the point of use.

The rest of the specialists exist for work a chat model cannot do at all:
transcribing audio, speaking, reading a scanned page, embedding text for memory
search.

Every chain ends somewhere reachable, so a specialist being down degrades quality
rather than removing the feature.

Each chain is ordered by *fitness first, then cost*. Every entry is a
"provider:model" key resolvable through ``nexus.providers.find_model`` or, for the
non-chat modalities, through the NIM catalogue. The router filters a chain down to
what is actually reachable at request time; nothing here assumes a key exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Job = Literal[
    "chat",
    "expert",
    "reasoning",
    "coding",
    "fast",
    "vision",
    "ocr",
    "video",
    "stt",
    "tts",
    "embedding",
    "rerank",
]

#: How a job's model is called. ``chat`` goes through a Provider's ``stream``;
#: ``media`` goes through ``nexus.providers.nim_media``. Mixing these up is the
#: single most common way to get an unexplainable 400, so the router branches on
#: it explicitly instead of inferring it from the model name.
Transport = Literal["chat", "media"]

#: The model conversation goes to first.
#:
#: **Re-pointed 2026-10-04.** This named ``deepseek-ai/deepseek-v4-flash-0731``,
#: which NVIDIA retired: a probe of every id in the catalogue that day answered
#: ``410 Gone -- reached its end of life on 2026-08-26`` for it. That made the head
#: of the *chat* chain -- the most-used path in the product -- a guaranteed wasted
#: round trip on a cold install, recovered from only because ``model_probes``
#: records the 410 as durable and the router then skips it. Nine of the twenty-two
#: ids in the catalogue answered the same way; see ``nim_catalogue`` for the sweep.
#:
#: Nemotron 3 Super was measured alive the same day, answering in 1.5 s.
MAIN_MODEL = "nvidia:nvidia/nemotron-3-super-120b-a12b"

#: The heavy model behind the main one. Named separately because it appears in
#: three chains -- it backs up conversation, and it *heads* reasoning -- and a
#: literal repeated three times is a literal that drifts.
BIG_BRAIN = "nvidia:nvidia/nemotron-3-ultra-550b-a55b"

#: What Expert Mode reaches for. Named because it appears in two chains: it heads
#: ``expert`` and it backs up ``reasoning``, since a request heavy enough to have
#: been routed to the reasoning chain is a request the heavier variant also serves
#: well.
#:
#: **Re-pointed 2026-10-04** from ``deepseek-ai/deepseek-v4-pro-0813``, retired by
#: NVIDIA in the same end-of-life batch as the old ``MAIN_MODEL``. Expert Mode now
#: names the heaviest model measured alive on this key: Nemotron 3 Ultra, 550B,
#: which answered in 1.7 s. It is deliberately the same id as ``BIG_BRAIN`` -- two
#: names for one model is honest where inventing a second heavy model would not
#: be, and the names stay separate so that re-pointing one later does not silently
#: move the other.
EXPERT_MODEL = "nvidia:nvidia/nemotron-3-ultra-550b-a55b"


@dataclass(frozen=True)
class JobSpec:
    job: Job
    #: The settings key that overrides this job's model.
    setting: str
    title: str
    description: str
    transport: Transport
    #: Candidate models, best first.
    chain: tuple[str, ...]
    #: True when the job's output is text the user reads, so a failure needs to
    #: be spoken about rather than retried silently.
    user_facing: bool = True
    requires_vision: bool = False
    requires_tools: bool = False
    #: True when the job is an enhancement rather than a requirement. Reranking is
    #: the only one: memory recall orders by vector distance and works without it,
    #: so "no reachable rerank model" must not be reported as a broken install.
    optional: bool = False


JOBS: tuple[JobSpec, ...] = (
    JobSpec(
        job="chat",
        setting="model_chat",
        title="Conversation",
        description="Ordinary talking, questions, and deciding which tool to use.",
        transport="chat",
        chain=(
            MAIN_MODEL,
            BIG_BRAIN,
            # 2026-10-04: ``meta/llama-3.3-70b-instruct`` sat here and answers 410
            # Gone. Replaced with the lightning model, which was measured alive the
            # same day and is a genuinely different size class from the two above
            # it -- a fallback that is only a smaller Nemotron is still a fallback.
            "nvidia:nvidia/nemotron-3.5-lightning-30b-a3b",
            "openai:gpt-4.1-mini",
        ),
        requires_tools=True,
    ),
    JobSpec(
        job="expert",
        setting="model_expert",
        title="Expert Mode",
        description=(
            "The strongest model available, for when the user has explicitly asked "
            "for it: a hard bug, a decision with consequences, work worth waiting for."
        ),
        transport="chat",
        # Led by the strongest model NEXUS can reach: Expert Mode is an explicit
        # "take your time", so latency is the point rather than a cost. It still
        # ends at a lighter model, so a specialist being unreachable degrades the
        # answer instead of refusing the request -- and the user is told which model
        # actually served them, because answering an Expert request from the fast
        # model without saying so is exactly the silent substitution the specs
        # prohibit.
        #
        # 2026-10-04: this chain read four deep and was two deep. It named
        # ``EXPERT_MODEL`` and ``BIG_BRAIN``, which now hold the same id, and then
        # ``nemotron-3-super-120b-a12b`` longhand *and* ``MAIN_MODEL``, which also
        # now hold the same id -- the exact duplication the coding chain's comment
        # below was written about, reintroduced by re-pointing the constants rather
        # than by a stray edit. Spelled with one entry per distinct model, and a
        # cross-provider last resort so a whole-provider outage still has a path.
        chain=(
            EXPERT_MODEL,
            MAIN_MODEL,
            "openai:gpt-4.1",
        ),
        requires_tools=True,
    ),
    JobSpec(
        job="reasoning",
        setting="model_reasoning",
        title="Reasoning and planning",
        description="Breaking a request into steps, working through a hard problem.",
        transport="chat",
        chain=(
            # ``EXPERT_MODEL`` and ``BIG_BRAIN`` now hold the same id, so only one
            # appears: ``_chain`` dedupes, and listing both would read as a
            # two-deep chain that is one deep. 2026-10-04 also removed
            # ``llama-3.3-nemotron-super-49b-v1.5`` from this chain (410 Gone).
            EXPERT_MODEL,
            MAIN_MODEL,
            "nvidia:nvidia/nemotron-3.5-lightning-30b-a3b",
            "openai:gpt-4.1",
        ),
        requires_tools=True,
    ),
    JobSpec(
        job="coding",
        setting="model_coding",
        title="Code and agent work",
        description="Reading, writing and explaining code, and driving a multi-step task.",
        transport="chat",
        # Written with the constants rather than the ids they hold, which is not a
        # style preference: this chain previously named ``deepseek-v4-flash-0731``
        # longhand at position three *and* again as ``MAIN_MODEL`` at position six, so
        # a chain that read seven deep was six deep -- ``_chain`` dedupes into
        # ``keys``, so the repeat was a candidate that could never run. Spelling a
        # constant out next to the constant itself is how that happens.
        chain=(
            # 2026-10-04: four of this chain's six entries were unusable --
            # ``deepseek-v4-pro-0813`` and ``gpt-oss-120b`` answer 410 Gone,
            # ``codestral-22b-instruct-v0.1`` answers 404 Function not found, and
            # ``kimi-k3`` returned no response at all inside 420 s. The coding
            # chain was effectively one model deep behind an OpenAI key. It now
            # names only ids measured alive on this key the same day.
            EXPERT_MODEL,
            MAIN_MODEL,
            "nvidia:nvidia/nemotron-3.5-lightning-30b-a3b",
            "openai:gpt-4.1",
        ),
        requires_tools=True,
    ),
    JobSpec(
        job="fast",
        setting="model_fast",
        title="Quick work",
        description=(
            "Short, latency-sensitive jobs the user never sees as prose: naming a "
            "conversation, classifying a request, deciding whether something is worth "
            "mentioning."
        ),
        transport="chat",
        chain=(
            # Classification is the one job picked by speed and instruction-following
            # rather than raw quality, because this answer is never shown -- it only
            # decides which model *will* be. ``nemotron-3.5-lightning-30b-a3b`` heads
            # it: the catalogue already describes it as "built for latency, used for
            # classifying a request", and it is the only fast candidate that also
            # declares ``instruction_following``, the exact skill labelling a sentence
            # needs. The Mistral small model, which used to head this chain, stays one
            # slot in as a cross-provider fallback, so routing keeps working if NVIDIA
            # is unreachable; gpt-4o-mini remains the key-gated last resort.
            "nvidia:nvidia/nemotron-3.5-lightning-30b-a3b",
            # 2026-10-04: ``nemotron-3-nano-30b-a3b`` and ``meta/llama-3.1-8b-instruct``
            # both answer 410 Gone and were removed. The omni reasoning model takes
            # the NVIDIA second slot: measured alive at 1.9 s, and small enough that
            # it belongs on a chain whose whole purpose is speed.
            "nvidia:nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
            "mistral:mistral-small-latest",
            "openai:gpt-4o-mini",
        ),
        user_facing=False,
    ),
    JobSpec(
        job="vision",
        setting="model_vision",
        title="Seeing",
        description=(
            "Looking at a screenshot, a photograph or a diagram and describing or "
            "reasoning about it."
        ),
        transport="chat",
        # Headed by the strongest NVIDIA vision model that actually answers. The 90B
        # is stronger on paper and was tried at the head, but on this key it hangs:
        # three probes (a 64px image, then a bare 5-token text turn) each ran the full
        # timeout -- 150s, 300s, 90s -- and returned zero bytes, while the 11B answered
        # both correctly in under a second. Listed is not servable, so the 11B leads
        # and the 90B sits last: kept so it self-heals the day NVIDIA restores it, but
        # never preferred over a model that responds. The two nano VLs that used to sit
        # here (nemotron-nano-12b-v2-vl, llama-3.1-nemotron-nano-vl-8b-v1) are gone --
        # absent from the hosted listing since the 2026-08-26 EOL batch -- so they are
        # dropped rather than left as candidates that can only 404. Pixtral and
        # gpt-4.1-mini are the cross-provider fallbacks, real resilience when NVIDIA
        # vision is down entirely.
        chain=(
            "nvidia:meta/llama-3.2-11b-vision-instruct",
            "mistral:pixtral-12b-2409",
            "openai:gpt-4.1-mini",
            "nvidia:meta/llama-3.2-90b-vision-instruct",
        ),
        requires_vision=True,
    ),
    JobSpec(
        job="ocr",
        setting="model_ocr",
        title="Reading documents",
        description=(
            "Pulling the text out of a scan, a PDF page or a photographed receipt, "
            "with its layout preserved."
        ),
        transport="media",
        chain=(
            "nvidia:nvidia/nemotron-parse",
            "nvidia:nvidia/nemoretriever-parse",
        ),
    ),
    JobSpec(
        job="video",
        setting="model_video",
        title="Watching",
        description=(
            "Reasoning about a sequence of frames -- a short screen recording, or the "
            "last few minutes of screen history."
        ),
        # Chat, not media: the frame-sequence models are ordinary chat-completions
        # endpoints that happen to accept several images in one message. Routing
        # them through the media client would send them to a path that does not
        # exist for them.
        transport="chat",
        # Cosmos Reason stays at the head because it is the model asked for and it is
        # purpose-built for physical-world video. This key is not entitled to it: it
        # answers a genuine per-account 404 (application/problem+json with a real
        # function UUID, confirmed twice -- not a wrong path). The probe records that,
        # so the router skips it at zero cost and says why, and the day it is enabled
        # on the NVIDIA account it becomes the head again with no code change.
        #
        # There is no Cosmos 3 or "nano reasoner" variant to route to instead: the
        # hosted listing contains exactly one cosmos id. The nearest real match for
        # "nano + reasoner + physical-world video" is the omni model below, which is
        # multimodal and reasoning-tuned, and which read a four-frame sequence back in
        # the correct order ("red, green, red, green") when tested -- re-verified today,
        # same four frames, same answer.
        #
        # The omni model carries this chain alone, so the tail matters: it answered a
        # 503 "ResourceExhausted: Worker local total request limit reached (670/16)"
        # on first call and only succeeded on retry. That is transient congestion
        # rather than a dead route, but it means a busy minute must fall through to
        # something. It used to fall through to nemotron-nano-12b-v2-vl, which has
        # been absent from the hosted listing since the 2026-08-26 EOL batch -- a
        # fallback that could only 404. The 11B vision model replaces it (verified
        # serving today), with pixtral behind it so a whole-provider outage still has
        # a path. Frame sequences are just several images in one message, so an
        # ordinary vision model reads them.
        chain=(
            # 2026-10-04: ``cosmos-reason2-8b`` headed this chain and answers 404
            # "Function not found" -- deployed by NVIDIA but not enabled for this
            # key, which is the distinction the two 404 shapes exist to make. The
            # omni reasoning model, measured alive, now heads it.
            "nvidia:nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
            "nvidia:meta/llama-3.2-11b-vision-instruct",
            "mistral:pixtral-12b-2409",
        ),
        requires_vision=True,
    ),
    # Speech is NVIDIA's, with Mistral behind it -- and both halves of that are
    # measurements. NIM serves no speech over REST at all, which is what the note
    # here used to say and is still true; what it wrongly concluded was that the
    # surface did not exist. It does, over gRPC at grpc.nvcf.nvidia.com, addressed
    # by NVCF function id (see the speech note in nim_catalogue.ENDPOINTS for how
    # the two were told apart). Parakeet and Magpie head these chains because they
    # are multilingual in a way Voxtral is not: verified by synthesising in five
    # languages -- en-US, hi-IN, es-US, fr-FR, ja-JP -- and transcribing each back
    # to its own words at confidence 1.0. Voxtral stays second: it is a different
    # provider on a different wire, so it survives an NVIDIA outage, a locked
    # keyring, or the riva client not being installed.
    JobSpec(
        job="stt",
        setting="model_stt",
        title="Hearing",
        description="Turning recorded speech into text, in any language.",
        transport="media",
        # Parakeet takes arbitrary BCP-47 tags -- it reports its language set as a
        # pattern rather than a list -- so it is the multilingual head. Voxtral
        # follows as the cross-provider fallback; voxtral-small is deliberately
        # absent, since /audio/transcriptions rejects it as invalid_model and a
        # candidate that can only fail is not a fallback.
        chain=(
            "nvidia:nvidia/parakeet-1-1b-rnnt-multilingual-asr",
            "mistral:voxtral-mini-latest",
        ),
    ),
    JobSpec(
        job="tts",
        setting="model_tts",
        title="Speaking",
        description="Turning NEXUS's reply into audio, in the language it was written in.",
        transport="media",
        # Magpie speaks twelve languages from one voice; Mistral serves exactly one
        # synthesis model, so it is the whole of the fallback rather than a choice
        # among several.
        chain=(
            "nvidia:nvidia/magpie-tts-multilingual",
            "mistral:voxtral-mini-tts-latest",
        ),
    ),
    JobSpec(
        job="embedding",
        setting="model_embedding",
        title="Memory vectors",
        description="Turning memories and questions into vectors so search finds meaning.",
        transport="media",
        chain=(
            "nvidia:nvidia/nemotron-3-embed-1b",
            "nvidia:nvidia/llama-nemotron-embed-1b-v2",
            "nvidia:nvidia/llama-3.2-nv-embedqa-1b-v1",
        ),
        user_facing=False,
    ),
    JobSpec(
        job="rerank",
        setting="model_rerank",
        title="Memory ranking",
        description="Ordering recalled memories by how much they answer the question.",
        transport="media",
        chain=("nvidia:nvidia/rerank-qa-mistral-4b",),
        user_facing=False,
        optional=True,
    ),
)

BY_JOB: dict[str, JobSpec] = {spec.job: spec for spec in JOBS}


def spec(job: str) -> JobSpec:
    try:
        return BY_JOB[job]
    except KeyError:
        raise KeyError(
            f"'{job}' is not a job NEXUS routes. Known jobs: "
            + ", ".join(sorted(BY_JOB))
        ) from None


def split_key(key: str) -> tuple[str, str]:
    """Split "provider:model". Model ids contain slashes and colons are not used
    inside them, so one split from the left is correct and unambiguous."""
    provider_id, _, model_id = key.partition(":")
    return provider_id, model_id


def catalogue() -> list[dict[str, object]]:
    """The job table, for the Settings screen's model assignment panel."""
    return [
        {
            "job": s.job,
            "setting": s.setting,
            "title": s.title,
            "description": s.description,
            "transport": s.transport,
            "default": s.chain[0] if s.chain else "",
            "candidates": list(s.chain),
        }
        for s in JOBS
    ]
