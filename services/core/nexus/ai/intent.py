"""What kind of request this is, which decides which model answers (spec §5, §13).

The behaviour specs list eight kinds of request, and the reason to classify them
is not tidiness -- it is that they need different machinery. "What is a container"
needs a fast chat model and no tools. "Why is my build failing" needs the
reasoning model and a large context. "What is on my screen" needs vision and a
capture before a single word may be said about it. Sending all eight to the same
model means either paying reasoning-model latency for a definition, or handing a
diagnosis to a model that cannot hold the problem.

Classification here is deliberately **cheap and syntactic** -- keyword and shape
rules over the user's sentence, no model call. That choice has one honest
consequence: it is sometimes wrong. So it is built to be wrong in the safe
direction.

* It never *grants* anything. A sentence classified as an action still passes
  through the permission engine; misreading "delete" in a question about deleting
  cannot delete anything.
* It never *withholds* tools from a turn that might need them. When the signal is
  weak the answer is :data:`Intent.CONVERSATION` with tools still attached, so a
  request the rules failed to recognise as actionable can still act. The model is
  a better classifier than these rules; its job is to decide *whether* to call a
  tool, and this module's job is only to make sure it can and that a sensible
  model is holding the request.
* The only thing a wrong guess costs is a suboptimal model or a wasted retrieval,
  and :meth:`Classification.confident` tells the caller when to distrust it.

The one class that is more than a routing hint is :data:`Intent.MEMORY_WRITE`.
"Remember that I use Neovim" is not a question to answer, it is an instruction to
store something, and answering it conversationally would produce a warm reply and
no memory -- exactly the "fake memory" the specs prohibit.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from enum import Enum

from ..logging_setup import get_logger
from ..providers.base import ChatRequest, Message, ProviderError
from ..router import NoModelAvailable, router

log = get_logger("ai.intent")


class Intent(str, Enum):
    """The eight request kinds from §5. ``str`` so it serialises without a cast."""

    #: A question answerable from knowledge or conversation. No tool required.
    CONVERSATION = "conversation"
    #: One concrete thing to do on this computer now: open, close, play, type.
    ACTION = "action"
    #: A goal that needs several dependent steps and survives a failure midway.
    #: Routed to the agent loop with a task record, not answered in one reply.
    MULTI_STEP = "multi_step"
    #: A question about this machine's live state: CPU, disk, processes, windows.
    #: Must be answered from a tool result, never from memory of a past answer.
    INSPECTION = "inspection"
    #: Needs to see something -- the screen, a window, an image the user attached.
    VISUAL = "visual"
    #: An instruction to remember, forget, or recall something.
    MEMORY_WRITE = "memory_write"
    #: Creating or changing a rule that fires later without being asked.
    AUTOMATION = "automation"
    #: Hard technical work: debugging, code, analysis, "why is this happening".
    REASONING = "reasoning"


#: Which router job serves each intent. This is the whole point of the module.
#:
#: ``MULTI_STEP`` goes to ``coding`` rather than ``reasoning`` because in practice
#: multi-step desktop work is tool-sequencing, and the coding models are the ones
#: trained to emit long correct chains of structured calls. ``MEMORY_WRITE`` and
#: ``AUTOMATION`` go to ``fast``: both are short, structured, and mostly about
#: extracting fields from a sentence, which the main model does no better and
#: several seconds slower.
JOB_FOR: dict[Intent, str] = {
    Intent.CONVERSATION: "chat",
    Intent.ACTION: "chat",
    Intent.MULTI_STEP: "coding",
    Intent.INSPECTION: "chat",
    Intent.VISUAL: "vision",
    Intent.MEMORY_WRITE: "fast",
    Intent.AUTOMATION: "fast",
    Intent.REASONING: "reasoning",
}

#: Intents that must not be answered without a tool result behind the answer.
#: The caller uses this to keep tools attached even in a pinned-model turn: an
#: inspection answered from the model's own guess is a fabricated system value,
#: which is the prohibition the specs state most bluntly (§10, §40).
#:
#: ``MEMORY_WRITE`` is in here for a sharper reason. "Remember that I use Neovim"
#: reaching a model with no ``memory.remember`` tool produces a warm confirmation
#: and stores nothing -- the fake memory of §78. The only thing that can actually
#: write a memory is the tool, so a memory turn without tools is a turn that
#: cannot do what it is about to claim it did.
#:
#: ``REASONING`` is in here too: "why is my build failing" is answerable only by
#: reading the actual error, and a diagnosis produced without looking is a guess
#: dressed as a finding.
NEEDS_TOOLS: frozenset[Intent] = frozenset(
    {Intent.ACTION, Intent.MULTI_STEP, Intent.INSPECTION, Intent.VISUAL,
     Intent.AUTOMATION, Intent.MEMORY_WRITE, Intent.REASONING}
)


@dataclass(slots=True)
class Classification:
    """What the request looks like, and how much to trust that reading."""

    intent: Intent
    job: str
    #: 0.0-1.0. Weak scores are honest: they mean "no rule fired clearly", and
    #: the caller responds by keeping tools available rather than by guessing
    #: harder.
    score: float
    #: The phrases that decided it, for the routing-transparency panel (§32) and
    #: for debugging a misroute without re-running the request.
    signals: list[str] = field(default_factory=list)
    needs_vision: bool = False
    needs_tools: bool = True

    @property
    def confident(self) -> bool:
        return self.score >= 0.6

    def to_dict(self) -> dict[str, object]:
        return {
            "intent": self.intent.value,
            "job": self.job,
            "score": round(self.score, 2),
            "signals": self.signals,
            "needs_vision": self.needs_vision,
            "needs_tools": self.needs_tools,
        }

    def with_signal(self, note: str) -> Classification:
        """A copy carrying one more explanation, changing nothing else.

        Used when something was considered and rejected -- a model was consulted
        and disagreed weakly, or answered unusably. The reading is unchanged, but
        the fact that it was questioned belongs in the record: without it a
        misroute looks like a rule that fired wrongly rather than a genuinely
        ambiguous sentence.
        """
        return replace(self, signals=self.signals + [note])


# ------------------------------------------------------------------- patterns
#
# Written as word-boundary regexes rather than substring checks, because
# substrings misfire in ways that change the model: "screenshot" contains "shot",
# "remember" contains "member", and "important" contains "port". Each list is
# ordered roughly by how decisive the phrase is.

_MEMORY = (
    r"\bremember (?:that|this|i|my|to)\b",
    r"\b(?:don'?t|do not|stop) remember(?:ing)?\b",
    r"\bforget (?:that|this|what|about|everything|my)\b",
    r"\bkeep (?:in mind|a note|track) (?:that|of)\b",
    r"\bmake a note\b",
    r"\bwhat do you (?:remember|know) about (?:me|my)\b",
    r"\b(?:my|our) (?:preference|preferences) (?:is|are)\b",
    r"\bfrom now on\b",
)

_AUTOMATION = (
    r"\bevery (?:day|morning|evening|night|hour|week|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday|\d+\s*(?:min|minute|hour)s?)\b",
    r"\bwhen(?:ever)? (?:i|my|the) \w+ (?:opens?|closes?|starts?|connects?|"
    r"disconnects?|changes?|drops?|reaches?)\b",
    r"\b(?:schedule|automate|automatically)\b",
    r"\bat \d{1,2}(?::\d{2})?\s*(?:am|pm)\b.*\b(?:remind|open|run|start|play)\b",
    r"\bset (?:up )?(?:a )?(?:reminder|automation|routine|rule)\b",
    r"\bremind me\b",
)

_VISUAL = (
    r"\b(?:on|in) my screen\b",
    r"\bwhat (?:am i|do you see|is this|does this say)\b",
    r"\b(?:look at|see|read) (?:this|my|the) (?:screen|image|picture|photo|"
    r"screenshot|window|tab)\b",
    r"\bscreenshot\b",
    r"\b(?:describe|analyse|analyze) (?:this|the) (?:image|picture|photo|screen)\b",
    r"\bwhat'?s (?:on|in) (?:the|my) (?:screen|display)\b",
    r"\bread the (?:text|error|dialog|message) (?:on|in)\b",
)

_INSPECTION = (
    r"\b(?:how much|what'?s using|what is using) (?:my )?(?:ram|memory|cpu|disk|"
    r"space|battery|bandwidth)\b",
    r"\bis \w+ (?:running|open|installed|connected|playing)\b",
    r"\bwhat(?:'s| is| are)? (?:running|open|installed|playing|connected)\b",
    r"\b(?:cpu|memory|ram|disk|battery|network|wifi|bluetooth) (?:usage|"
    r"status|level|state)\b",
    r"\bhow many \w+ (?:are )?(?:running|open|installed)\b",
    r"\b(?:list|show me) (?:my |the )?(?:processes|windows|apps|applications|"
    r"devices|files|downloads)\b",
    r"\bwhich (?:app|application|process|window|tab)\b",
    r"\bwhat time is it\b",
)

_ACTION = (
    r"^(?:please )?(?:open|close|launch|start|stop|quit|kill|play|pause|resume|"
    r"skip|mute|unmute|type|click|copy|paste|delete|remove|move|rename|create|"
    r"make|save|download|install|uninstall|restart|reboot|shutdown|lock|"
    r"minimi[sz]e|maximi[sz]e|focus|switch|send|search|google|volume|"
    r"screenshot)\b",
    r"\b(?:can you|could you|please|i want you to|i need you to) "
    r"(?:open|close|launch|start|stop|play|pause|type|click|copy|delete|move|"
    r"rename|create|save|download|install|send|search|switch|set)\b",
    r"\bturn (?:on|off|up|down) (?:the |my )?\w+",
    r"\bset (?:the |my )?(?:volume|brightness|wallpaper|timer)\b",
)

_MULTI_STEP = (
    r"\band then\b.*\band\b",
    r"\bafter (?:that|you|it)\b.*\b(?:open|close|run|send|save|check)\b",
    r"\b(?:set (?:up|me up)|configure|scaffold|bootstrap|migrate|refactor|"
    r"clean up|organi[sz]e|back ?up)\b",
    r"\bfor (?:each|every) (?:file|folder|tab|window|project|image)\b",
    r"\b(?:step by step|one by one)\b",
    r"\bthen\b.*\bthen\b",
)

_REASONING = (
    r"\bwhy (?:is|isn'?t|does|doesn'?t|did|didn'?t|won'?t|can'?t|are|am)\b",
    r"\b(?:debug|diagnose|troubleshoot|root cause)\b",
    r"\b(?:not working|doesn'?t work|broken|failing|fails|crashed|crashes|"
    r"hangs|stuck|slow|timing out|times out)\b",
    r"\b(?:error|exception|traceback|stack ?trace|segfault|panic)\b",
    r"\b(?:compare|trade[- ]?offs?|pros and cons|which (?:approach|option|way)|"
    r"best way|should i)\b",
    r"\b(?:design|architect|plan out|think through|reason about)\b",
    r"\b(?:review|refactor|optimi[sz]e) (?:this|my|the) (?:code|function|"
    r"query|schema|design)\b",
    r"```",
)

#: (patterns, intent, weight). Order matters: the first list whose pattern
#: matches with the highest weight wins, and ties break toward the earlier entry.
#:
#: Memory and automation sit first because they are *instructions about NEXUS
#: itself* -- "remind me every morning to check the build" contains action verbs
#: and would otherwise be executed once, immediately, instead of being scheduled.
#: Visual comes before inspection because "what's on my screen" is both, and
#: answering it without an image is the fabrication the specs forbid.
_RULES: tuple[tuple[tuple[str, ...], Intent, float], ...] = (
    (_MEMORY, Intent.MEMORY_WRITE, 0.9),
    (_AUTOMATION, Intent.AUTOMATION, 0.85),
    (_VISUAL, Intent.VISUAL, 0.8),
    (_MULTI_STEP, Intent.MULTI_STEP, 0.7),
    (_REASONING, Intent.REASONING, 0.7),
    (_INSPECTION, Intent.INSPECTION, 0.75),
    (_ACTION, Intent.ACTION, 0.75),
)

_COMPILED: tuple[tuple[tuple[re.Pattern[str], ...], Intent, float], ...] = tuple(
    (tuple(re.compile(p, re.IGNORECASE) for p in patterns), intent, weight)
    for patterns, intent, weight in _RULES
)

#: A request long enough that it is almost certainly a problem statement rather
#: than a command. Measured against real use: desktop commands are short ("open
#: spotify", "close all chrome tabs"), and anything past this is context being
#: pasted in for analysis.
_LONG_REQUEST_WORDS = 60


def classify(
    text: str,
    *,
    has_attachments: bool = False,
    continuing_task: bool = False,
) -> Classification:
    """Read a request and decide which capability should serve it.

    ``has_attachments`` overrides everything else, because an attached image is
    not a hint about intent -- it is a hard requirement that the model be able to
    look at it. A text-only model handed an image either ignores it silently or
    describes what such an image usually contains, and both are the prohibited
    "claiming to see without visual input".
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return Classification(Intent.CONVERSATION, "chat", 0.0, ["empty request"],
                              needs_tools=False)

    if has_attachments:
        return Classification(
            Intent.VISUAL, JOB_FOR[Intent.VISUAL], 1.0,
            ["an image is attached, so the model must be able to see"],
            needs_vision=True,
        )

    best: tuple[Intent, float, list[str]] | None = None
    for patterns, intent, weight in _COMPILED:
        hits = [p.pattern for p in patterns if p.search(cleaned)]
        if not hits:
            continue
        # Two independent phrases pointing the same way is real evidence, not
        # twice the evidence -- hence a small bonus rather than a doubled score.
        score = min(0.95, weight + 0.05 * (len(hits) - 1))
        if best is None or score > best[1]:
            best = (intent, score, hits[:3])

    words = len(cleaned.split())
    if best is None:
        # Nothing matched. A long request with no command shape is a problem being
        # described, which the reasoning model handles better; a short one is
        # conversation. Both keep tools, because these rules missing an actionable
        # request is likelier than the user asking for nothing.
        if words >= _LONG_REQUEST_WORDS:
            return Classification(
                Intent.REASONING, JOB_FOR[Intent.REASONING], 0.45,
                [f"long request ({words} words) with no command phrasing"],
            )
        return Classification(Intent.CONVERSATION, "chat", 0.35,
                              ["no rule matched; treating as conversation"])

    intent, score, signals = best

    # A one-line "open spotify" and a three-paragraph request that happens to
    # contain "create" are not the same job. Length is the tiebreaker the keyword
    # rules cannot see.
    if intent in (Intent.ACTION, Intent.CONVERSATION) and words >= _LONG_REQUEST_WORDS:
        intent, score = Intent.REASONING, 0.5
        signals = signals + [f"reclassified: {words} words is a problem, not a command"]

    if continuing_task:
        # Mid-task turns inherit the task's machinery. Re-classifying "yes, go
        # ahead" as conversation would drop the tools halfway through a plan and
        # strand the task at whichever step needed approval.
        if intent in (Intent.CONVERSATION, Intent.MEMORY_WRITE):
            intent, score = Intent.MULTI_STEP, 0.6
            signals = signals + ["continuing an active task"]

    return Classification(
        intent=intent,
        job=JOB_FOR[intent],
        score=score,
        signals=signals,
        needs_vision=intent is Intent.VISUAL,
        needs_tools=intent in NEEDS_TOOLS or score < 0.6,
    )


# ------------------------------------------------------- model-assisted reading
#
# The rules above are fast and free, and they are wrong often enough to matter.
# A model reads a sentence better than a regex does -- it knows that "can you see
# what's eating my battery" is an inspection and not a request to look at the
# screen, and no keyword list will ever know that.
#
# So the two are layered rather than swapped: the rules run every turn and
# produce a prior, and a model is consulted only when that prior is weak. That
# ordering is the whole design.
#
# * Most turns never pay for it. "open spotify" scores 0.75 and goes straight
#   through; the model is asked about the sentences that actually confused the
#   rules.
# * The latency lands where it is already justified. A request the rules could
#   not read is a request the user will wait a moment for.
# * A failure costs nothing. If the model is unreachable, times out, or answers
#   with something that is not one of the eight labels, the prior stands. Intent
#   is a routing hint; degrading it slows a turn down, it never breaks one.
#
# The model's answer wins on disagreement, because that is the point of asking.
# What it may not do is change ``needs_vision``: an attached image is a hard
# requirement for a model that can see, and no classification may route around it.

#: Below this the rules are admitting they guessed, which is when a model is worth
#: the round trip. Matches :attr:`Classification.confident` deliberately -- one
#: threshold, so "unconfident" means the same thing everywhere.
MODEL_MIN_SCORE = 0.6

#: Cap on how much of a long request goes to the classifier. The shape of a request
#: is visible in its opening -- a command is short and starts with a verb, a
#: problem statement does not -- and sending twelve pasted stack frames to decide
#: one label wastes the budget the reasoning model will need.
_CLASSIFIER_MAX_CHARS = 1_200

_CLASSIFIER_INSTRUCTIONS = """\
You are a request classifier inside a desktop assistant. Read the user's message
and return the single label that best describes what they want.

conversation  -- a question answerable from knowledge, or ordinary talk.
action        -- one concrete thing to do on this computer now (open, close, play,
                 type, send, set).
multi_step    -- a goal needing several dependent steps to reach.
inspection    -- a question about this computer's current state (memory, disk,
                 processes, windows, what is running, battery, network).
visual        -- it requires looking at the screen or at an image.
memory_write  -- an instruction to remember, forget, or recall something about the
                 user.
automation    -- a rule that should fire later without being asked (schedules,
                 reminders, "whenever X happens").
reasoning     -- hard technical work: debugging, diagnosis, code review, design,
                 "why is this happening", comparing options.

Judge by intent, not by vocabulary. "Can you see what is draining my battery" is
inspection, not visual -- the user wants a measurement, not a screenshot.
"Remind me to check the build" is automation, not memory_write. "My laptop is
slow" is reasoning, not conversation.

Reply with JSON and nothing else:
{"intent": "<one label>", "confidence": <0.0-1.0>, "why": "<a few words>"}

Confidence is how sure you are, not how urgent the request is. If the message is
genuinely ambiguous, say so with a low number rather than picking confidently.\
"""

_VALID_LABELS = {i.value: i for i in Intent}


def _parse_label(text: str) -> tuple[Intent, float, str] | None:
    """Read the classifier's JSON. Returns None on anything unexpected.

    Deliberately strict about the label and forgiving about the wrapper: models
    add ```json fences and a line of preamble, which is cosmetic, but a label
    outside the eight is a real disagreement about the vocabulary and must not be
    coerced into the nearest match.
    """
    body = (text or "").strip()
    if not body:
        return None
    if "```" in body:
        # Take the fenced block's contents, whichever fence style was used.
        parts = body.split("```")
        body = parts[1] if len(parts) > 1 else body
        if body.lstrip().startswith("json"):
            body = body.lstrip()[4:]
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(body[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None

    label = str(parsed.get("intent") or "").strip().lower()
    intent = _VALID_LABELS.get(label)
    if intent is None:
        return None
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    why = str(parsed.get("why") or "").strip()[:80]
    return intent, max(0.0, min(1.0, confidence)), why


async def refine(
    prior: Classification,
    text: str,
    *,
    has_attachments: bool = False,
) -> Classification:
    """Ask a fast model what the request is, when the rules were not sure.

    Returns ``prior`` unchanged whenever asking is pointless or fails: an image is
    attached (already decided), the prior is confident, the request is empty, no
    fast model is reachable, or the answer was not one of the eight labels.
    """
    if has_attachments or prior.needs_vision:
        return prior
    cleaned = (text or "").strip()
    if not cleaned:
        return prior

    try:
        decision = router().resolve("fast")
    except NoModelAvailable as exc:
        log.info("intent refinement skipped: %s", exc)
        return prior

    # A focused instruction rather than the full NEXUS persona, which
    # memory_extract does use. The difference is deliberate: extraction needs to
    # know who NEXUS is in order to judge what is worth remembering, while
    # labelling a sentence does not, and the persona's several thousand tokens of
    # identity and tool contract would only bias a classifier toward "action".
    payload = ChatRequest(
        messages=[Message(role="user", content=cleaned[:_CLASSIFIER_MAX_CHARS])],
        model=decision.model_id,
        system=_CLASSIFIER_INSTRUCTIONS,
        temperature=0.0,
        max_output_tokens=120,
    )

    collected: list[str] = []
    try:
        async for event in router().stream(payload, job="fast", decision=decision):
            if event.type == "text" and event.text:
                collected.append(event.text)
    except ProviderError as exc:
        log.info("intent refinement failed: %s", exc.message)
        return prior
    except Exception:  # noqa: BLE001 -- a hint must never take down a turn
        log.warning("intent refinement raised", exc_info=True)
        return prior

    read = _parse_label("".join(collected))
    if read is None:
        return prior.with_signal("a model was asked and did not return a usable label")

    intent, confidence, why = read
    detail = f"{decision.model_id} read this as {intent.value}"
    if why:
        detail += f" ({why})"

    if intent is prior.intent:
        # Agreement between two independent readings is real evidence. Raised to
        # the model's own confidence when that is higher, but never past the point
        # where the caller would stop attaching tools to a borderline request.
        return Classification(
            intent=prior.intent,
            job=prior.job,
            score=max(prior.score, min(confidence, 0.9)),
            signals=prior.signals + [f"{detail}, agreeing with the rules"],
            needs_vision=prior.needs_vision,
            needs_tools=prior.needs_tools,
        )

    if confidence < 0.4:
        # It disagrees but is not sure either. Two weak readings do not make a
        # decision; keep the prior and record that the question is open, so a
        # misroute is debuggable afterwards.
        return prior.with_signal(f"{detail}, but with low confidence -- kept the rules' reading")

    return Classification(
        intent=intent,
        job=JOB_FOR[intent],
        score=confidence,
        # Appended, not prepended: signals read oldest to newest everywhere else in
        # this module, and the transparency panel shows them in order, so the
        # override belongs after the rule it overrode rather than in front of it.
        signals=prior.signals[:2] + [f"{detail}, overriding the rules' {prior.intent.value}"],
        needs_vision=intent is Intent.VISUAL,
        # Same rule as ``classify``: tools stay attached when the reading is weak,
        # because a missed actionable request is worse than an unused schema list.
        needs_tools=intent in NEEDS_TOOLS or confidence < MODEL_MIN_SCORE,
    )


__all__ = [
    "Classification",
    "Intent",
    "JOB_FOR",
    "MODEL_MIN_SCORE",
    "NEEDS_TOOLS",
    "classify",
    "refine",
]

