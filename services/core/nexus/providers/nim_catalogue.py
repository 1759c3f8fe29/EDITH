"""NVIDIA NIM model catalogue, organised by job (task E3b; spec §9, §10, §78).

NIM is not one model behind one endpoint -- it is a lot of separately deployed
models, each good at one thing, and several of them do not speak
chat-completions at all. Treating them as interchangeable is how you end up
sending an image to a text-only endpoint and getting an opaque 400 back.

So this file states, per job, which NIM model does it and **how it must be
called**. The ``modality`` and ``endpoint`` fields are load-bearing:

* ``chat`` -- ``/v1/chat/completions`` on ``integrate.api.nvidia.com``.
* ``vision`` -- also chat-completions, but the image goes in the message content.
* ``video`` -- also chat-completions, with several frames in one message. There is
  no streaming-video endpoint here; frames are sampled and sent.
* ``ocr`` -- chat-completions with **image-only** content. Adding a text segment
  is rejected outright, and the answer comes back as a tool call rather than
  prose, so it must not be streamed.
* ``asr`` / ``tts`` -- **not over REST.** Served over gRPC at
  ``grpc.nvcf.nvidia.com``, addressed by function id rather than URL. See the
  speech note below.
* ``embedding`` -- ``/v1/embeddings`` on the hosted host.
* ``rerank`` -- a function-style path on ``ai.api.nvidia.com``, not the hosted host.

**Every id here was checked against the provider's own listing.** A model that
appears on build.nvidia.com is not necessarily callable: many catalogue entries
are downloadable containers you are meant to self-host, and calling those through
the hosted API returns 404 with no hint that this is the reason. So the ids below
were not written from memory -- ``GET {HOSTED_BASE}/models`` was enumerated on
``LISTING_CHECKED`` and each entry carries ``listed``, recording whether the
provider itself named it.

``listed=True`` means the provider enumerates the id, which rules out the
guess-a-plausible-name failure. It does **not** mean a call has succeeded: quota,
region and per-model access can still refuse it, so the Settings screen probes
and reports separately (§78). ``listed=False`` is not an accusation either -- the
rerank endpoint lives on a function-style path that ``/models`` does not enumerate
at all, so for that one the listing is simply silent; a real call is what settled
it, and it answered 200 with correctly ordered rankings.

**Reading a 404 correctly turned out to matter more than reading the listing.**
The two kinds are not the same failure and must not be treated alike:

* ``application/problem+json`` -- ``"Function '<uuid>': Not found for account"``.
  The model exists and is deployed; this key is not entitled to it. Enabling it on
  the provider's site would fix it.
* ``text/plain`` -- ``"404 page not found"``. The gateway has no such route. It is
  byte-identical to what a deliberately invented path returns, so it says nothing
  about entitlement and everything about the URL being wrong.
* ``urn:inference-service:problem-details:not-found`` at the function gateway --
  the route is a real function, but **no HTTP route resolves for it**, because it
  is a gRPC function. This is the one that matters and the one that is easiest to
  misread: it looks like "no such model" and means "right model, wrong protocol".

Four speech endpoints in earlier versions of this file collected the *second* kind
and were recorded as "your account cannot call this model" -- advice that would
have sent the user to enable something that was never reachable by that route.
That much was correct. The conclusion drawn next, that speech therefore had no
surface at all, was not: it was the third kind of 404 wearing the second one's
clothes. Speech is served, over gRPC, and both models are now in ``ENDPOINTS``
with a round trip behind them. See the speech note there.

Six ids that earlier versions of this file used were wrong and have been removed
or corrected. Each would have 404'd on first real use:
``deepseek-ai/deepseek-r1``, ``qwen/qwen2.5-coder-32b-instruct``,
``microsoft/phi-3.5-vision-instruct``, ``nvidia/llama-3.2-nv-embedqa-1b-v2`` (only
``-v1`` exists), ``nvidia/llama-3.2-nv-rerankqa-1b-v2``, and
``nvidia/nv-rerankqa-mistral-4b-v3`` -- the last corrected to
``nvidia/rerank-qa-mistral-4b``, which the reranking endpoint named itself when
asked for the wrong one.

**The listing moved between 2026-08-22 and 2026-09-01.** Re-enumerating it on the
later date returned 83 ids, and seven entries below that carry ``listed=True`` were
not among them: ``nvidia/llama-3.3-nemotron-super-49b-v1.5``,
``meta/llama-3.3-70b-instruct``, ``meta/llama-3.1-8b-instruct``,
``nvidia/nemotron-nano-12b-v2-vl``, ``nvidia/llama-3.1-nemotron-nano-vl-8b-v1``,
``nvidia/llama-nemotron-embed-1b-v2`` and ``nvidia/nemoretriever-parse``. Those flags
are deliberately left as they are rather than flipped: this listing is served against
a key, so absence can mean withdrawn *or* not entitled for this account, and those two
call for opposite fixes. What is certain is that the flags are no longer evidence of
what they claim, so treat those seven as unverified until a probe settles each. All
seven are named by a chain in ``router/jobs.py``, but none heads one; each is a
fallback the chain can skip past to an id the listing does name, which is the whole
reason a chain is a chain.

Context windows are conservative floors chosen for routing decisions, not vendor
guarantees. Routing uses them to reject a model that certainly cannot hold a
prompt; a model may well accept more than is claimed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .base import ModelSpec

Modality = Literal["chat", "vision", "ocr", "asr", "tts", "embedding", "rerank", "video"]

#: The job a model is chosen for. The router maps each of these to one model, and
#: the user can override any of them in Settings.
Job = Literal[
    "chat",            # ordinary conversation -- the main model
    "reasoning",       # multi-step thinking, planning an agent task
    "coding",          # writing or reading code
    "fast",            # short, latency-sensitive replies
    "vision",          # "what is on my screen", photos, diagrams
    "ocr",             # documents, scanned pages, receipts
    "video",           # a sequence of frames
    "stt",             # speech to text
    "tts",             # text to speech
    "embedding",       # memory vectors
    "rerank",          # ordering recalled memories by relevance
]

HOSTED_BASE = "https://integrate.api.nvidia.com/v1"
AI_BASE = "https://ai.api.nvidia.com/v1"

#: Where NVIDIA's speech models actually live. Not a REST host: NVCF publishes
#: 198 public functions and splits them by ``health.protocol`` -- 168 answer HTTP,
#: 30 answer gRPC, and *every* ASR and TTS function is in the gRPC set. Reaching
#: one means a Riva client against this target with the function id in the call
#: metadata, which is why :class:`NimEndpoint` carries a protocol at all.
GRPC_SPEECH_TARGET = "grpc.nvcf.nvidia.com:443"

#: The date ``GET {HOSTED_BASE}/models`` was enumerated to set every ``listed``
#: flag below. Dated because the provider's catalogue moves: a model can be added
#: or withdrawn without notice, and a stale flag asserting "the provider names
#: this" is worse than no flag at all.
LISTING_CHECKED = "2026-08-22"

#: A second, later enumeration of ``GET {HOSTED_BASE}/models``, run when
#: ``deepseek-v4-pro-0813`` was added. Kept *alongside* ``LISTING_CHECKED`` rather
#: than replacing it, because the two dates mean different things: every ``listed``
#: flag below was set on the older date, and bumping that date would assert those
#: flags had been re-verified when seven of them had not. This one records only what
#: the later listing said about the ids checked against it.
LISTING_RECHECKED = "2026-09-01"

_RECHECKED_CAVEAT = (
    f"Named by NVIDIA's hosted model listing as of {LISTING_RECHECKED}, which "
    "returned 83 ids. Being named rules out a wrong id; it does not prove a call "
    "succeeds, since quota and per-model entitlement can still refuse one -- NEXUS "
    "probes each model and reports which are callable with your key."
)

_CATALOGUE_CAVEAT = (
    f"Named by NVIDIA's hosted model listing as of {LISTING_CHECKED}. Entries on "
    "build.nvidia.com that ship as downloadable containers are not in that listing "
    "and return 404 here; NEXUS probes each one and reports which are actually "
    "callable with your key."
)

#: For a model the listing names, that answers neither success nor refusal.
#:
#: Dated and specific on purpose. "Unavailable" would be a guess; what was actually
#: measured on 2026-10-04 was silence -- no response headers in 150 s, no first
#: streamed byte in 420 s, and none of the three error shapes NVIDIA uses to say no
#: (404 wrong-id, 404 not-entitled, 410 retired). A user reading this needs to know
#: it is a scheduling state on their account rather than a broken id, because the
#: two have different fixes.
_UNRESPONSIVE_CAVEAT = (
    "Named by NVIDIA's hosted model listing, but measured 2026-10-04 as returning "
    "no response at all on this key: no headers within 150 s and no first streamed "
    "token within 420 s, with no 404, 410 or 401 to explain it. That is neither a "
    "working model nor a refusal -- it is listed and deployed but not scheduled for "
    "this account. It is therefore in no routing chain, and will only be used if you "
    "pin it; if it starts answering, NEXUS's routing learns to prefer it from the "
    "measured result rather than needing this note changed."
)

_UNLISTED_CAVEAT = (
    "Served from a function-style path on ai.api.nvidia.com, which the hosted "
    "/models listing does not enumerate -- so its absence there says nothing "
    "either way. Only a real call proves this one works."
)


@dataclass(frozen=True)
class NimEndpoint:
    """A NIM model plus the exact way it has to be called."""

    model: str
    modality: Modality
    base_url: str
    path: str
    label: str
    jobs: tuple[Job, ...]
    #: True when the provider's own /models listing names this id (see the module
    #: docstring: proof against a wrong name, not proof of a working call).
    listed: bool = True
    #: True when the endpoint refuses a text segment alongside the image.
    image_only: bool = False
    #: True when the response is a tool call / structured object, not prose.
    structured_response: bool = False
    #: True when the endpoint cannot be streamed.
    no_stream: bool = False
    sample_rate: int | None = None
    languages: tuple[str, ...] = ()
    #: How this endpoint is spoken to. ``http`` means ``base_url + path`` with a
    #: JSON body, which is every chat, OCR and retrieval model here. ``grpc``
    #: means a Riva client against ``base_url`` carrying :attr:`function_id` in
    #: the call metadata, which is the only way NVIDIA serves speech. The two are
    #: not interchangeable and the failure when they are confused is silent-ish:
    #: a gRPC function POSTed to the HTTP gateway answers 404 with the *same*
    #: shape a deleted model gives, which is exactly how speech was written off
    #: here as "no such surface" for as long as it was.
    protocol: Literal["http", "grpc"] = "http"
    #: The NVCF function this endpoint resolves to. Required for gRPC, where
    #: there is no URL path to name the model -- the id *is* the address, passed
    #: as call metadata.
    function_id: str = ""
    #: Named speakers, when the model has any. Empty means the model takes only
    #: its own name as the voice and picks the speaker from the language.
    voices: tuple[str, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        # A gRPC entry without a function id is unreachable: there is no path to
        # fall back on, so the call would fail at request time with a message
        # about metadata rather than about the catalogue. Raising here makes it a
        # startup error in the file that is actually wrong.
        if self.protocol == "grpc" and not self.function_id:
            raise ValueError(
                f"{self.model} is a gRPC endpoint and needs a function_id: there "
                "is no URL to reach it by."
            )

    @property
    def key(self) -> str:
        return f"nvidia:{self.model}"

    @property
    def address(self) -> str:
        """Where this endpoint is, in the form its protocol uses.

        Exists so callers that only want to *display* the endpoint -- the Settings
        screen, a probe result -- do not have to know that concatenating
        ``base_url + path`` produces something meaningless for a gRPC function.
        """
        if self.protocol == "grpc":
            return f"grpc://{self.base_url}#{self.function_id}"
        return f"{self.base_url}{self.path}"


# --------------------------------------------------------------- text and code

CHAT_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec(
        id="nvidia/nemotron-3-ultra-550b-a55b",
        provider="nvidia",
        label="Nemotron 3 Ultra 550B",
        context_window=128_000,
        max_output_tokens=16_384,
        strengths=("reasoning", "instruction_following", "long_context"),
        latency="slow",
        free=True,
        notes=(
            "The heaviest model NEXUS can reach: a 550B mixture-of-experts with 55B "
            "active. Heads the reasoning chain and backs up the main model for "
            "conversation. Slow to first token -- not for anything latency-sensitive."
        ),
    ),
    ModelSpec(
        id="nvidia/nemotron-3-super-120b-a12b",
        provider="nvidia",
        label="Nemotron 3 Super 120B",
        context_window=128_000,
        max_output_tokens=16_384,
        strengths=("reasoning", "instruction_following"),
        latency="medium",
        free=True,
        notes="Most of Ultra's reasoning at a fraction of the latency. "
              + _CATALOGUE_CAVEAT,
    ),
    ModelSpec(
        id="nvidia/llama-3.3-nemotron-super-49b-v1.5",
        provider="nvidia",
        label="Nemotron Super 49B v1.5",
        context_window=128_000,
        max_output_tokens=8_192,
        strengths=("reasoning", "instruction_following"),
        latency="medium",
        free=True,
        notes=_CATALOGUE_CAVEAT,
    ),
    ModelSpec(
        id="moonshotai/kimi-k3",
        provider="nvidia",
        label="Kimi K3",
        context_window=128_000,
        max_output_tokens=16_384,
        strengths=("coding", "reasoning", "instruction_following"),
        latency="medium",
        free=True,
        notes=(
            "Heads the coding and agent chain: strong at multi-step tool use and at "
            "holding a plan across many steps. " + _CATALOGUE_CAVEAT
        ),
    ),
    ModelSpec(
        id="deepseek-ai/deepseek-v4-pro-0813",
        provider="nvidia",
        label="DeepSeek V4 Pro",
        context_window=128_000,
        max_output_tokens=8_192,
        strengths=("coding", "reasoning"),
        # Marked slow deliberately, and not from a measurement: this is the heavier
        # of the two V4 variants, and the latency class is what stops the router
        # picking a model for an urgent request. Guessing "fast" would cost a user
        # their latency budget; guessing "slow" only costs this model a job it was
        # never the right answer for. The flash sibling's own probe took 173s cold.
        latency="slow",
        free=True,
        notes="Heads Expert Mode and the coding chain. The heavier V4 variant: the "
              "choice when a code or reasoning answer is worth waiting for rather "
              "than wanted immediately. " + _RECHECKED_CAVEAT,
    ),
    ModelSpec(
        id="deepseek-ai/deepseek-v4-flash-0731",
        provider="nvidia",
        label="DeepSeek V4 Flash",
        context_window=128_000,
        max_output_tokens=8_192,
        strengths=("coding", "fast", "reasoning"),
        latency="fast",
        free=True,
        notes="Fast coding model; the better choice when a code answer is wanted "
              "immediately rather than perfectly. " + _CATALOGUE_CAVEAT,
    ),
    ModelSpec(
        id="openai/gpt-oss-120b",
        provider="nvidia",
        label="GPT-OSS 120B",
        context_window=128_000,
        max_output_tokens=8_192,
        strengths=("reasoning", "coding"),
        latency="medium",
        free=True,
        notes="Open-weight generalist, used as a third opinion when both the main "
              "model and the Nemotron chain are unreachable. " + _CATALOGUE_CAVEAT,
    ),
    ModelSpec(
        id="mistralai/codestral-22b-instruct-v0.1",
        provider="nvidia",
        label="Codestral 22B",
        context_window=32_000,
        max_output_tokens=4_096,
        strengths=("coding",),
        latency="fast",
        free=True,
        notes="Code-specialised and small enough to answer quickly. "
              + _CATALOGUE_CAVEAT,
    ),
    ModelSpec(
        id="nvidia/nemotron-3.5-lightning-30b-a3b",
        provider="nvidia",
        label="Nemotron 3.5 Lightning 30B",
        context_window=128_000,
        max_output_tokens=4_096,
        strengths=("fast", "cheap", "instruction_following"),
        latency="fast",
        free=True,
        notes="Built for latency. Used for the invisible work -- naming a "
              "conversation, classifying a request. " + _CATALOGUE_CAVEAT,
    ),
    ModelSpec(
        id="nvidia/nemotron-3-nano-30b-a3b",
        provider="nvidia",
        label="Nemotron 3 Nano 30B",
        context_window=128_000,
        max_output_tokens=4_096,
        strengths=("fast", "cheap"),
        latency="fast",
        free=True,
        notes=_CATALOGUE_CAVEAT,
    ),
    ModelSpec(
        id="meta/llama-3.3-70b-instruct",
        provider="nvidia",
        label="Llama 3.3 70B",
        context_window=128_000,
        max_output_tokens=4_096,
        strengths=("instruction_following", "cheap"),
        latency="medium",
        free=True,
        notes=_CATALOGUE_CAVEAT,
    ),
    ModelSpec(
        id="meta/llama-3.1-8b-instruct",
        provider="nvidia",
        label="Llama 3.1 8B",
        context_window=128_000,
        max_output_tokens=4_096,
        strengths=("fast", "cheap"),
        latency="fast",
        free=True,
        notes=_CATALOGUE_CAVEAT,
    ),
    # ------------------------------------------------------------------
    # Listed by NVIDIA, asked for by the user, and **not serving on this
    # key** as of 2026-10-04. Recorded here rather than left out, because
    # "absent from the catalogue" and "present but unreachable" are
    # different facts and only one of them is true.
    #
    # Measured: ``GET /v1/models`` names all three. A non-streaming chat
    # call returns no response headers at all within 150 s; a streaming
    # call returns no first byte within 420 s. That is not a cold start
    # (the slowest alive model on this key answers in 23 s) and it is not
    # a refusal -- there is no 404, no 410 and no 401 to read. It is the
    # third state NVIDIA has: listed, deployed, and not scheduled for this
    # account.
    #
    # They are deliberately in **no chain**. A hanging candidate at a chain
    # head costs the user their whole latency budget to learn nothing, which
    # is worse than a model that is honestly absent. They are here so that
    # (a) the Models screen can name them, (b) the user can pin one the
    # moment the key is entitled, and (c) ``nexus.learning`` will promote
    # them on measured reward if they start answering -- which is exactly
    # the case the bandit was built for.
    #
    # ``latency`` is "slow" on the same principle the retired V4 Pro entry
    # used: guessing "fast" would spend a user's urgency budget on an
    # unknown, and guessing "slow" only costs the model work it was never
    # the right answer for.
    ModelSpec(
        id="z-ai/glm-5.3",
        provider="nvidia",
        label="GLM 5.3",
        context_window=128_000,
        max_output_tokens=16_384,
        strengths=("reasoning", "coding", "instruction_following"),
        latency="slow",
        free=True,
        notes=_UNRESPONSIVE_CAVEAT,
    ),
    ModelSpec(
        id="z-ai/glm-5.3-flash",
        provider="nvidia",
        label="GLM 5.3 Flash",
        context_window=128_000,
        max_output_tokens=16_384,
        strengths=("fast", "reasoning", "coding"),
        latency="slow",
        free=True,
        notes=_UNRESPONSIVE_CAVEAT,
    ),
    ModelSpec(
        id="deepseek-ai/deepseek-v4.1-flash",
        provider="nvidia",
        label="DeepSeek V4.1 Flash",
        context_window=128_000,
        max_output_tokens=8_192,
        strengths=("coding", "fast", "reasoning"),
        latency="slow",
        free=True,
        notes=(
            "The live successor to the retired ``deepseek-v4-flash-0731``. "
            + _UNRESPONSIVE_CAVEAT
        ),
    ),
)

VISION_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec(
        id="nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
        provider="nvidia",
        label="Nemotron 3 Nano Omni 30B Reasoning",
        context_window=128_000,
        max_output_tokens=8_192,
        supports_tools=False,
        supports_vision=True,
        strengths=("video", "vision", "reasoning"),
        latency="medium",
        free=True,
        notes=(
            "Multimodal and reasoning-tuned, and the working head of the video chain. "
            "Chosen after Cosmos Reason turned out to be un-entitled on this key: it "
            "read a four-frame sequence back in the correct order rather than just "
            "describing the last image, which is the property frame-sequence work "
            "actually needs. " + _CATALOGUE_CAVEAT
        ),
    ),
    ModelSpec(
        id="nvidia/nemotron-nano-12b-v2-vl",
        provider="nvidia",
        label="Nemotron Nano 12B VL",
        context_window=128_000,
        max_output_tokens=4_096,
        supports_tools=False,
        supports_vision=True,
        strengths=("vision", "reasoning", "fast"),
        latency="fast",
        free=True,
        notes=(
            "Backs up the vision chain for repeated screen checks: current-generation "
            "VL model, the fast option when the 90B describer is overkill for a "
            "low-stakes look. " + _CATALOGUE_CAVEAT
        ),
    ),
    ModelSpec(
        id="meta/llama-3.2-90b-vision-instruct",
        provider="nvidia",
        label="Llama 3.2 90B Vision",
        context_window=128_000,
        max_output_tokens=4_096,
        supports_tools=False,
        supports_vision=True,
        strengths=("vision", "reasoning"),
        latency="slow",
        free=True,
        notes="Heads the vision chain: the most careful describer of the set, and "
              "the slowest -- the right trade when a wrong read is a wrong action. "
              + _CATALOGUE_CAVEAT,
    ),
    ModelSpec(
        id="nvidia/llama-3.1-nemotron-nano-vl-8b-v1",
        provider="nvidia",
        label="Nemotron Nano VL 8B",
        context_window=128_000,
        max_output_tokens=4_096,
        supports_tools=False,
        supports_vision=True,
        strengths=("vision", "fast", "cheap"),
        latency="fast",
        free=True,
        notes=_CATALOGUE_CAVEAT,
    ),
    ModelSpec(
        id="meta/llama-3.2-11b-vision-instruct",
        provider="nvidia",
        label="Llama 3.2 11B Vision",
        context_window=128_000,
        max_output_tokens=4_096,
        supports_tools=False,
        supports_vision=True,
        strengths=("vision", "fast"),
        latency="medium",
        free=True,
        notes=_CATALOGUE_CAVEAT,
    ),
    ModelSpec(
        id="nvidia/cosmos-reason2-8b",
        provider="nvidia",
        label="Cosmos Reason 2 8B",
        context_window=128_000,
        max_output_tokens=4_096,
        supports_tools=False,
        supports_vision=True,
        strengths=("vision", "reasoning"),
        latency="medium",
        free=True,
        notes=(
            "Physical-world reasoning model, built for reasoning over what happens "
            "across a sequence of frames rather than describing one picture. Heads "
            "the video chain. " + _CATALOGUE_CAVEAT
        ),
    ),
    ModelSpec(
        id="nvidia/vila",
        provider="nvidia",
        label="VILA",
        context_window=128_000,
        max_output_tokens=4_096,
        supports_tools=False,
        supports_vision=True,
        strengths=("vision",),
        latency="medium",
        free=True,
        notes="Multi-frame VLM, kept as the video fallback. " + _CATALOGUE_CAVEAT,
    ),
)


# ------------------------------------------------------- specialised endpoints

ENDPOINTS: tuple[NimEndpoint, ...] = (
    # ---- vision (chat-shaped, image inside the message)
    NimEndpoint(
        model="nvidia/nemotron-nano-12b-v2-vl",
        modality="vision",
        base_url=HOSTED_BASE,
        path="/chat/completions",
        label="Nemotron Nano 12B VL",
        jobs=("vision",),
        listed=False,
        notes="Was the preferred screenshot model. Absent from the hosted listing "
              "since the 2026-08-26 EOL batch, so it is off both the vision and "
              "video chains -- kept here to record that it existed and went away, "
              "not as a candidate.",
    ),
    NimEndpoint(
        model="meta/llama-3.2-90b-vision-instruct",
        modality="vision",
        base_url=HOSTED_BASE,
        path="/chat/completions",
        label="Llama 3.2 90B Vision",
        jobs=("vision",),
        notes="The strongest vision model on paper, and listed -- but on this key it "
              "does not answer: three probes (a 64px image, then a bare 5-token text "
              "turn) each ran the full timeout at 150s, 300s and 90s and returned "
              "zero bytes. Listed is not servable, so it sits last on the vision "
              "chain rather than heading it, ready to self-heal if NVIDIA restores "
              "it. Does not support tool calling.",
    ),
    NimEndpoint(
        model="nvidia/llama-3.1-nemotron-nano-vl-8b-v1",
        modality="vision",
        base_url=HOSTED_BASE,
        path="/chat/completions",
        label="Nemotron Nano VL 8B",
        jobs=("vision",),
        listed=False,
        notes="Delisted in the same 2026-08-26 EOL batch as the 12B VL above, and "
              "dropped from the vision chain for the same reason.",
    ),
    NimEndpoint(
        model="meta/llama-3.2-11b-vision-instruct",
        modality="vision",
        base_url=HOSTED_BASE,
        path="/chat/completions",
        label="Llama 3.2 11B Vision",
        jobs=("vision", "video"),
        notes="Heads the vision chain: the only NVIDIA vision model verified serving "
              "on this key -- it read a two-colour test image correctly in under a "
              "second while the 90B timed out. Also the video chain's fallback, since "
              "a frame sequence is just several images in one message.",
    ),
    # ---- documents and OCR
    NimEndpoint(
        model="nvidia/nemotron-parse",
        modality="ocr",
        base_url=HOSTED_BASE,
        path="/chat/completions",
        label="Nemotron Parse",
        jobs=("ocr",),
        image_only=True,
        structured_response=True,
        no_stream=True,
        notes=(
            "Current-generation document parser, and the head of the OCR chain. The "
            "request must contain the image and nothing else -- adding a text "
            "instruction is rejected with 400 -- and it must not be streamed, "
            "because the result arrives as a structured tool call whose shape "
            "changes when streamed."
        ),
    ),
    NimEndpoint(
        model="nvidia/nemoretriever-parse",
        modality="ocr",
        base_url=HOSTED_BASE,
        path="/chat/completions",
        label="NemoRetriever Parse",
        jobs=("ocr",),
        image_only=True,
        structured_response=True,
        no_stream=True,
        notes="Previous generation of the same idea, kept as the OCR fallback. Same "
              "image-only, no-streaming contract.",
    ),
    # ---- video (frame sequences, also chat-shaped)
    NimEndpoint(
        model="nvidia/cosmos-reason2-8b",
        modality="video",
        base_url=HOSTED_BASE,
        path="/chat/completions",
        label="Cosmos Reason 2 8B",
        jobs=("video",),
        notes=(
            "Reasons about what changes across frames -- what NEXUS needs when asked "
            "what just happened on screen. Frames go in as several images in one "
            "message; there is no true streaming-video endpoint here."
        ),
    ),
    NimEndpoint(
        model="nvidia/vila",
        modality="video",
        base_url=HOSTED_BASE,
        path="/chat/completions",
        label="VILA",
        jobs=("video",),
        notes="Accepts several frames in one request. Video fallback.",
    ),
    # ---- speech to text / text to speech: gRPC, not REST
    #
    # This block used to say speech "does not exist" on NIM, and that was wrong in
    # a way worth recording, because the evidence for it was real. Every REST path
    # (/v1/asr/..., /v1/tts/...) does answer `404 page not found` in text/plain,
    # byte-identical to a made-up path, and the hosted /models listing does contain
    # no ASR or TTS id at all. Both facts are still true. The conclusion drawn from
    # them -- that there was no surface to reach -- was not.
    #
    # What settled it was a controlled experiment at the NVCF function gateway: at
    # one identical path, with one identical key, the HTTP-protocol OCR function
    # answered 422 (route resolves, payload wrong) while Magpie answered 404 and
    # Parakeet 500. The only variable was ``health.protocol``. NVCF lists 198
    # public functions, 168 HTTP and 30 gRPC, and every ASR/TTS one is gRPC. The
    # 404 was the gateway saying "no HTTP route resolves for a gRPC function",
    # which reads exactly like "no such model" and is a completely different fact.
    #
    # Both entries below are verified by a closed round trip, not by being listed:
    # Magpie synthesised a sentence, Parakeet transcribed that exact audio back to
    # the same words, in five languages (hi-IN, es-US, fr-FR, de-DE, ja-JP), each
    # at confidence 1.0.
    NimEndpoint(
        model="nvidia/parakeet-1-1b-rnnt-multilingual-asr",
        modality="asr",
        base_url=GRPC_SPEECH_TARGET,
        path="",
        label="Parakeet RNNT 1.1B Multilingual",
        jobs=("stt",),
        protocol="grpc",
        function_id="71203149-d3b7-4460-8231-1be2543a1fca",
        # Not enumerated on purpose. The server reports its language set as the
        # pattern ``ab|c1...`` rather than a list of tags -- it is a universal
        # multilingual model that accepts arbitrary BCP-47 codes. An explicit
        # tuple here would be read by ``transcribe`` as a whitelist and would
        # start refusing languages the model actually handles.
        languages=(),
        listed=False,
        notes=(
            "Multilingual speech recognition over gRPC. Absent from the hosted "
            "/models listing because that listing only covers the REST surface, "
            "which serves no speech at all -- so its absence there says nothing. "
            "Verified by transcribing Magpie's own output back to the same words "
            "in five languages at confidence 1.0."
        ),
    ),
    NimEndpoint(
        model="nvidia/magpie-tts-multilingual",
        modality="tts",
        base_url=GRPC_SPEECH_TARGET,
        path="",
        label="Magpie TTS Multilingual",
        jobs=("tts",),
        protocol="grpc",
        function_id="877104f7-e885-42b9-8de8-f6e4c6303969",
        # 44100 because that is what the model answered at. Riva returns raw PCM
        # at whatever rate is asked for and the WAV header is written locally, so
        # a mismatch here does not error -- it plays back at the wrong speed.
        sample_rate=44_100,
        languages=(
            "en-US", "es-US", "fr-FR", "de-DE", "zh-CN", "vi-VN",
            "it-IT", "hi-IN", "ja-JP", "ko-KR", "ar-AR", "pt-BR",
        ),
        # The named speakers, and only these. Measured rather than assumed: the
        # dotted form works for EN-US and HI-IN and *fails* for every other
        # language (ES-US.Sofia, FR-FR.Sofia, DE-DE.Sofia and JA-JP.Sofia all
        # answered an RPC error), while the bare model name works for all twelve
        # with the speaker chosen by ``language_code``. So the bare name is the
        # default and these are an en-US/hi-IN extra -- not a list to iterate
        # per language. The uppercase region is load-bearing: the same names
        # lowercased fail, including en-US, which succeeds as EN-US.
        voices=(
            "Magpie-Multilingual",
            "Magpie-Multilingual.EN-US.Sofia",
            "Magpie-Multilingual.EN-US.Ray",
            "Magpie-Multilingual.EN-US.Mia",
            "Magpie-Multilingual.EN-US.Leo",
            "Magpie-Multilingual.HI-IN.Sofia",
        ),
        listed=False,
        notes=(
            "Multilingual speech synthesis over gRPC, twelve languages. Absent "
            "from the hosted /models listing for the same reason Parakeet is: "
            "that listing describes the REST surface, which carries no speech. "
            "Verified in five languages, each transcribed back to its own words."
        ),
    ),
    # ---- retrieval
    NimEndpoint(
        model="nvidia/nemotron-3-embed-1b",
        modality="embedding",
        base_url=HOSTED_BASE,
        path="/embeddings",
        label="Nemotron 3 Embed 1B",
        jobs=("embedding",),
        notes=(
            "Heads the embedding chain. Requires an input_type of 'query' or "
            "'passage'; using the wrong one measurably degrades recall, so NEXUS "
            "sets it explicitly on both sides. Changing embedding model invalidates "
            "every stored vector, which is why memories record which model wrote "
            "them."
        ),
    ),
    NimEndpoint(
        model="nvidia/llama-nemotron-embed-1b-v2",
        modality="embedding",
        base_url=HOSTED_BASE,
        path="/embeddings",
        label="Llama Nemotron Embed 1B v2",
        jobs=("embedding",),
        notes="Embedding fallback. Same input_type contract.",
    ),
    NimEndpoint(
        model="nvidia/llama-3.2-nv-embedqa-1b-v1",
        modality="embedding",
        base_url=HOSTED_BASE,
        path="/embeddings",
        label="Llama 3.2 NV EmbedQA 1B v1",
        jobs=("embedding",),
        notes=(
            "Last embedding fallback. Note the v1: there is no v2 on the hosted "
            "listing, despite the name appearing in older documentation."
        ),
    ),
    # ---- reranking
    #
    # No rerank model appears on the hosted /models listing at all, so this entry
    # is the one place in the file that rests on a documented function path rather
    # than on the provider's own enumeration. Reranking is therefore treated as an
    # optimisation everywhere it is used: memory recall orders by vector distance
    # and works without it, and the router is allowed to find nothing here.
    NimEndpoint(
        model="nvidia/rerank-qa-mistral-4b",
        modality="rerank",
        base_url=AI_BASE,
        path="/retrieval/nvidia/reranking",
        label="Rerank QA Mistral 4B",
        jobs=("rerank",),
        listed=False,
        notes=(
            "Reorders candidate memories against the question before they reach the "
            "main model. Optional by design: recall falls back to vector order if "
            "this is unreachable, so a failure here costs ranking quality and "
            "nothing else. " + _UNLISTED_CAVEAT
        ),
    ),
)


BY_MODEL: dict[str, NimEndpoint] = {e.model: e for e in ENDPOINTS}


def for_job(job: Job) -> list[NimEndpoint]:
    """Endpoints that can do a job, in preference order."""
    return [e for e in ENDPOINTS if job in e.jobs]


def preferred(job: Job) -> NimEndpoint | None:
    candidates = for_job(job)
    return candidates[0] if candidates else None


def by_modality(modality: Modality) -> list[NimEndpoint]:
    return [e for e in ENDPOINTS if e.modality == modality]


def all_models() -> tuple[ModelSpec, ...]:
    return CHAT_MODELS + VISION_MODELS


def unlisted() -> list[NimEndpoint]:
    """Endpoints the provider's own listing does not name.

    Surfaced rather than hidden: these are the ones where a 404 is a real
    possibility and the Settings screen should say so before the user relies on
    speech or reranking working.
    """
    return [e for e in ENDPOINTS if not e.listed]


def catalogue() -> list[dict[str, object]]:
    """Serialisable view for the Settings screen."""
    return [
        {
            "model": e.model,
            "label": e.label,
            "modality": e.modality,
            "jobs": list(e.jobs),
            "endpoint": e.address,
            "protocol": e.protocol,
            "listed": e.listed,
            "listing_checked": LISTING_CHECKED,
            "image_only": e.image_only,
            "structured_response": e.structured_response,
            "streamable": not e.no_stream,
            "sample_rate": e.sample_rate,
            "languages": list(e.languages),
            "voices": list(e.voices),
            "notes": e.notes,
        }
        for e in ENDPOINTS
    ]
