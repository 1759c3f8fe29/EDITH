"""What an observation is worth (spec doc2 §7, §9, §30, §33; MASTER §22, §30).

Monitors report facts. This decides what each fact is *worth* -- and the whole
point of doing it here, in plain Python, is that the decision is cheap. A system
that asked the reasoning model "is this interesting?" about every CPU sample
would spend its budget deciding not to speak. So the fast path is arithmetic on a
rule table, and the model is only reached for the small set of things this layer
marks ``investigate`` or ``act``.

Five numbers, all in 0..1, because doc2 §7 names five and each one answers a
different question:

* **importance** -- how much this matters if it is true.
* **urgency** -- how much worse it gets by waiting.
* **confidence** -- how much the detector's word is worth. A pixel-difference
  screen watcher is not as sure of "something changed" as ``/proc`` is of a
  battery percentage, and pretending otherwise is how a system ends up acting on
  a guess.
* **relevance** -- whether it bears on what the user is doing right now.
* **actionability** -- whether there is anything to *do*. A disk filling up is
  actionable; a laptop lid closing is not, however important.

They are kept separate rather than collapsed into one score early, because the
disposition rules below need them at different thresholds: something can be
important and useless to act on (``notify``), or unimportant and trivially
fixable (``log``, and fix it if asked).

**Confidence multiplies, it does not add.** A detector that is 30% sure of
something catastrophic must not out-rank a detector that is certain of something
moderate. Multiplication is what makes an unreliable signal produce an
*investigation* rather than an *action* -- which is the correct response to a
maybe.

**A clear is never an action.** When an observation says a condition ended, the
worst it can do is speak. Recovering from a recovery is how a control loop
oscillates.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Literal

from ..logging_setup import get_logger
from ..monitors.base import Observation

log = get_logger("core.scoring")

Disposition = Literal["ignore", "log", "remember", "notify", "investigate", "act"]

#: Ordered from least to most intervention. Used to compare and to demote.
DISPOSITIONS: tuple[Disposition, ...] = (
    "ignore",
    "log",
    "remember",
    "notify",
    "investigate",
    "act",
)

#: What the user is doing. Drives relevance and the interrupt bar (doc2 §9).
Activity = Literal["idle", "away", "typing", "reading", "coding", "watching", "call"]

#: Above this urgency, an event reaches the user whatever they are doing and
#: whatever the cooldown says. Reserved for things that get materially worse in
#: the time it takes to finish a sentence: power about to be lost, a disk with no
#: room left to write. Set deliberately high -- every entry here is a promise
#: that being wrong is worse than interrupting.
CRITICAL_URGENCY = 0.85


def rank(disposition: Disposition) -> int:
    return DISPOSITIONS.index(disposition)


@dataclass(frozen=True)
class Score:
    """The five numbers, before any decision is made from them."""

    importance: float = 0.0
    urgency: float = 0.0
    confidence: float = 1.0
    relevance: float = 1.0
    actionability: float = 0.0

    @property
    def weight(self) -> float:
        """Importance discounted by how much the detector and the moment are worth.

        This is the single number the thresholds compare against. Confidence and
        relevance are multipliers rather than terms so that either one being near
        zero collapses the result: a signal nobody trusts, or one about something
        the user is not doing, should not clear a bar just because the underlying
        condition would be serious if real.
        """
        return self.importance * self.confidence * self.relevance

    def to_dict(self) -> dict[str, float]:
        return {
            "importance": round(self.importance, 3),
            "urgency": round(self.urgency, 3),
            "confidence": round(self.confidence, 3),
            "relevance": round(self.relevance, 3),
            "actionability": round(self.actionability, 3),
            "weight": round(self.weight, 3),
        }


@dataclass(frozen=True)
class Judgement:
    """A score, the disposition it produced, and why."""

    kind: str
    score: Score
    disposition: Disposition
    reasons: tuple[str, ...] = ()
    dedupe_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "disposition": self.disposition,
            "reasons": list(self.reasons),
            "dedupe_key": self.dedupe_key,
            **self.score.to_dict(),
        }


@dataclass(frozen=True)
class Rule:
    """What a class of observation is worth before the moment is considered.

    ``confidence`` belongs to the *detector*, not the event: it says how often
    this kind of observation is right when it fires. An observation may lower it
    (by carrying ``data["confidence"]``) when the monitor knows this particular
    sample was shaky, but it may never raise it -- a monitor cannot vouch for
    itself beyond what its mechanism supports.
    """

    importance: float
    urgency: float
    actionability: float
    confidence: float = 1.0
    #: Worth carrying past this session even when nothing is done about it.
    memorable: bool = False
    #: Activities during which this is background noise. Relevance is cut, not
    #: zeroed: the user watching a video does not make a failing disk irrelevant,
    #: it makes a window title change irrelevant.
    dull_during: tuple[Activity, ...] = ()


#: Keyed by observation kind, longest prefix wins. Every entry is a claim about
#: the world that a test can argue with, which is the point of a table rather
#: than a chain of ifs.
RULES: dict[str, Rule] = {
    # -- power ---------------------------------------------------------------
    # Losing power loses work. Urgency is a function of the percentage, applied
    # in _adjust below; this is the shape, not the final value.
    "system.battery": Rule(
        importance=0.72, urgency=0.55, actionability=0.30, memorable=False
    ),
    # -- storage -------------------------------------------------------------
    # A full disk breaks writes that have already been started, and unlike CPU it
    # does not resolve on its own. Highly actionable: there is always something
    # to delete.
    "system.disk": Rule(
        importance=0.80, urgency=0.60, actionability=0.75, memorable=True
    ),
    # -- load ----------------------------------------------------------------
    # A busy CPU is usually the user's own build. Worth saying once, rarely worth
    # doing anything about, and never worth remembering.
    "system.cpu": Rule(
        importance=0.35, urgency=0.30, actionability=0.25,
        dull_during=("coding", "watching"),
    ),
    "system.memory": Rule(
        importance=0.55, urgency=0.50, actionability=0.45,
        dull_during=("watching",),
    ),
    # -- anticipation --------------------------------------------------------
    # A trend is a claim about the future, so it is scored *below* the alert for
    # the same measurement on purpose. "Memory will be full in ten minutes" is
    # worth one quiet sentence; "memory is full" is worth interrupting for, and
    # inverting that would train the user to distrust the louder of the two.
    #
    # Highly actionable, which is the point of saying it early: ten minutes before
    # the disk fills there is something to close, and afterwards there is damage to
    # undo. Confidence is deliberately below 1.0 -- the detector is a least-squares
    # fit, right about where a line is going only as often as the line holds -- and
    # `core/trends.py` has already refused to emit anything whose R² was too low to
    # argue for, so this number is about the method rather than the sample.
    #
    # Not memorable: a projection that was right stops being interesting the moment
    # the thing it predicted happens, and one that was wrong should not be kept as
    # though it were an observation.
    "system.trend": Rule(
        importance=0.52, urgency=0.35, actionability=0.70, confidence=0.75,
        memorable=False, dull_during=("coding", "watching", "call"),
    ),
    # -- network -------------------------------------------------------------
    # Everything the assistant does over the network stops working, so it is
    # important; almost nothing can be done about it locally, so it is not
    # actionable. This pair is exactly why the two are separate numbers.
    "system.network": Rule(
        importance=0.70, urgency=0.65, actionability=0.20, memorable=False
    ),
    # -- devices -------------------------------------------------------------
    "usb": Rule(importance=0.30, urgency=0.20, actionability=0.15,
                dull_during=("watching", "call")),
    "usb.storage": Rule(importance=0.45, urgency=0.25, actionability=0.40),
    "bluetooth": Rule(importance=0.25, urgency=0.15, actionability=0.20,
                      dull_during=("watching", "call")),
    # A device the user is listening or talking through disappearing mid-call is
    # not the same event as a mouse pairing.
    "bluetooth.audio": Rule(importance=0.50, urgency=0.55, actionability=0.35),
    # A keyboard that just connected can type. Not urgent -- it is almost always
    # the user's own keyboard waking up -- but worth a line in the record, which is
    # why it is memorable and not dulled by what the user is doing (§20).
    "bluetooth.input": Rule(importance=0.42, urgency=0.30, actionability=0.20,
                            memorable=True),
    # The radio being switched off explains every Bluetooth device going quiet at
    # once, so it is worth saying rather than leaving the user to work out why
    # their headphones stopped. Nothing local can act on it: the switch is theirs.
    "bluetooth.radio": Rule(importance=0.45, urgency=0.35, actionability=0.15),
    # -- processes -----------------------------------------------------------
    "process": Rule(importance=0.20, urgency=0.15, actionability=0.10,
                    dull_during=("coding", "watching", "typing", "call")),
    # Something the user started and did not stop. The whole proactive premise of
    # doc2 §8 -- "the crawler stopped unexpectedly" -- lives on this line.
    "process.exited": Rule(
        importance=0.65, urgency=0.55, actionability=0.70, memorable=True
    ),
    "process.crashed": Rule(
        importance=0.82, urgency=0.70, actionability=0.75, memorable=True
    ),
    # -- windows and screen --------------------------------------------------
    # Perception, not news. These exist to update the world model; the user does
    # not need to be told what they just clicked on.
    "window.focus": Rule(importance=0.10, urgency=0.05, actionability=0.05,
                         dull_during=("typing", "coding", "watching", "call")),
    "window.title": Rule(importance=0.08, urgency=0.05, actionability=0.05,
                         dull_during=("typing", "coding", "watching", "call")),
    # A pixel differ cannot tell a dialog from a video frame. Low confidence is
    # the honest encoding of that, and it is what keeps a screen change out of
    # ``act`` no matter how much changed.
    "screen.change": Rule(
        importance=0.30, urgency=0.20, actionability=0.20, confidence=0.45,
        dull_during=("watching", "call"),
    ),
    # -- clipboard -----------------------------------------------------------
    "clipboard.copy": Rule(importance=0.10, urgency=0.05, actionability=0.05,
                           dull_during=("typing", "coding", "watching", "call")),
    # Worth interrupting for: a key on the clipboard can be pasted into a chat
    # box in the next second, and after that it is public.
    "clipboard.credential": Rule(
        importance=0.85, urgency=0.80, actionability=0.60, memorable=True
    ),
    # -- the assistant itself ------------------------------------------------
    # A monitor that stopped is a hole in perception. Nothing else will report
    # it, so it must not be quiet (doc2 §22).
    "monitor.stopped": Rule(
        importance=0.60, urgency=0.45, actionability=0.65, memorable=True
    ),
}

#: Used when no rule matches. Deliberately low but not zero: an unrecognised
#: event is logged, so a monitor added without a rule shows up in the record
#: rather than vanishing.
DEFAULT_RULE = Rule(importance=0.25, urgency=0.20, actionability=0.15, confidence=0.7)

#: Severity is the monitor's own opinion, and it moves the score rather than
#: setting it. A monitor calling something an alert is evidence, not a verdict.
SEVERITY_SCALE: dict[str, float] = {"info": 0.75, "notice": 1.0, "alert": 1.25}

#: How much attention state can suppress something. A floor, not zero: the user
#: being busy must never make a critical event unreportable, only quieter.
MIN_RELEVANCE = 0.25

#: Applied to events that are *not* about what the user is working on, and only
#: when there is a known focus to be off. Discounting the miss rather than
#: boosting the hit is what keeps relevance a 0..1 multiplier: a match restores
#: the full baseline instead of exceeding it, so attention can still only ever
#: lower a disposition.
OFF_TOPIC_RELEVANCE = 0.8


def rule_for(kind: str) -> Rule:
    """The most specific rule for ``kind``.

    ``process.crashed`` matches its own entry; ``process.started`` falls back to
    ``process``; ``mystery.thing`` gets the default. Longest prefix wins so a
    family can be given a baseline and one member an exception.
    """
    best: Rule | None = None
    best_length = -1
    for prefix, rule in RULES.items():
        if kind == prefix or kind.startswith(prefix + "."):
            if len(prefix) > best_length:
                best, best_length = rule, len(prefix)
    return best if best is not None else DEFAULT_RULE


@dataclass
class Attention:
    """What the user is doing, as far as anything can tell (doc2 §9).

    Deliberately a plain record updated by whoever knows. The window monitor sets
    ``activity``; the speech pipeline sets ``speaking``; the task manager sets
    ``focus_topic``. Nothing here infers -- a guess about the user's state that
    silences a real alert would be the worst kind of wrong.
    """

    activity: Activity = "idle"
    #: True while the assistant has the floor. Speaking over itself is the most
    #: obviously broken thing a voice assistant can do.
    speaking: bool = False
    #: What the user or an active task is currently about ("nexus", "crawler").
    #: An observation naming this topic is more relevant than one that does not.
    focus_topic: str = ""
    #: Notifications delivered in the last hour, used to raise the bar as the
    #: hour gets noisier (doc2 §30).
    recent_notifications: int = 0

    def interruptible(self) -> bool:
        """Whether an ordinary notification is welcome right now."""
        return not self.speaking and self.activity not in ("call", "watching")

    def to_dict(self) -> dict[str, Any]:
        return {
            "activity": self.activity,
            "speaking": self.speaking,
            "focus_topic": self.focus_topic,
            "recent_notifications": self.recent_notifications,
        }


def _battery_urgency(data: dict[str, Any], base: float) -> tuple[float, float, list[str]]:
    """Battery importance and urgency scale with what is actually left.

    A drop to 40% and a drop to 5% are the same *kind* of event and nowhere near
    the same event. Without this, either the 40% report interrupts or the 5% one
    does not, and both failures are worse than the arithmetic.
    """
    reasons: list[str] = []
    percent = data.get("percent")
    plugged = data.get("plugged_in")
    if plugged is True:
        # On the charger, the number is a status line, not a warning.
        return 0.20, 0.05, ["on charger"]
    if not isinstance(percent, (int, float)):
        return base, base, reasons
    percent = float(percent)
    if percent <= 5:
        reasons.append("battery critical")
        return 0.95, 0.95, reasons
    if percent <= 10:
        reasons.append("battery very low")
        return 0.88, 0.86, reasons
    if percent <= 20:
        reasons.append("battery low")
        return 0.75, 0.60, reasons
    reasons.append(f"battery at {percent:.0f}%")
    return 0.45, 0.25, reasons


def _disk_urgency(data: dict[str, Any], base: float) -> tuple[float, float, list[str]]:
    """A disk with nothing left is a different event from one that is merely full.

    Below a couple of percent, writes start failing -- which corrupts whatever
    was mid-write. That is worth an interruption in a way that "85% used" is not.
    """
    reasons: list[str] = []
    percent = data.get("percent_used", data.get("percent"))
    if not isinstance(percent, (int, float)):
        return base, base, reasons
    percent = float(percent)
    if percent >= 98:
        reasons.append("disk essentially full")
        return 0.95, 0.90, reasons
    if percent >= 95:
        reasons.append("disk nearly full")
        return 0.88, 0.70, reasons
    if percent >= 90:
        reasons.append("disk filling up")
        return 0.78, 0.50, reasons
    return base, 0.35, reasons


def _network_urgency(data: dict[str, Any], base: float) -> tuple[float, float, list[str]]:
    """Losing the network matters; regaining it is a relief, not an emergency."""
    online = data.get("online")
    if online is False:
        return 0.72, 0.68, ["network down"]
    if online is True:
        return 0.35, 0.20, ["network restored"]
    return base, base, []


def score(
    observation: Observation,
    attention: Attention | None = None,
    *,
    now: float | None = None,
) -> Judgement:
    """Turn one observation into five numbers and a decision.

    Pure and synchronous on purpose: no I/O, no model, no clock beyond the one
    passed in. Everything it needs is in the observation and the attention state,
    which is what makes it cheap enough to run on every sample and testable
    without a running system.
    """
    attention = attention or Attention()
    rule = rule_for(observation.kind)
    reasons: list[str] = []

    importance = rule.importance
    urgency = rule.urgency
    actionability = rule.actionability

    # ---- kind-specific shaping, from the observation's own data -------------
    data = observation.data or {}
    if observation.kind.startswith("system.battery"):
        importance, urgency, extra = _battery_urgency(data, importance)
        reasons += extra
    elif observation.kind.startswith("system.disk"):
        importance, urgency, extra = _disk_urgency(data, importance)
        reasons += extra
    elif observation.kind.startswith("system.network"):
        importance, urgency, extra = _network_urgency(data, importance)
        reasons += extra

    # ---- severity: the monitor's opinion, weighted ---------------------------
    scale = SEVERITY_SCALE.get(observation.severity, 1.0)
    if scale != 1.0:
        reasons.append(f"severity {observation.severity}")
    importance = min(1.0, importance * scale)
    urgency = min(1.0, urgency * scale)

    # ---- confidence: the detector's, lowered by the sample if it says so ----
    confidence = rule.confidence
    claimed = data.get("confidence")
    if isinstance(claimed, (int, float)):
        # A monitor may say it is *less* sure than its mechanism allows, never
        # more. Otherwise "confidence: 1.0" in a payload would launder a guess.
        lowered = max(0.0, min(1.0, float(claimed)))
        if lowered < confidence:
            confidence = lowered
            reasons.append("monitor reported low confidence")

    # ---- relevance: what the user is doing ---------------------------------
    relevance = 1.0
    if attention.activity in rule.dull_during:
        relevance = MIN_RELEVANCE
        reasons.append(f"background noise while {attention.activity}")
    if attention.focus_topic:
        haystack = f"{observation.summary} {observation.dedupe_key} {data}".lower()
        if attention.focus_topic.lower() in haystack:
            relevance = min(1.0, relevance + 0.5)
            reasons.append(f"concerns {attention.focus_topic}")
        else:
            # The first version boosted the match instead, which was a no-op:
            # relevance was already 1.0 for anything not dulled, so `min(1.0, 1.5)`
            # meant an event about the thing the user was working on scored
            # exactly the same as one about anything else. Discounting the miss is
            # the same ordering, expressed in a way the multiplier can carry.
            relevance *= OFF_TOPIC_RELEVANCE
            reasons.append(f"not about {attention.focus_topic}")

    # ---- a clear is good news, and good news is not urgent ------------------
    if observation.clears:
        urgency = min(urgency, 0.25)
        # Zeroing actionability is what enforces "a clear cannot trigger action":
        # ``act`` needs 0.65 and ``investigate`` needs 0.30, so both become
        # unreachable here rather than being clamped afterwards. An earlier
        # version did both, and the clamp in ``decide`` was dead code no mutation
        # could reach -- which is worse than no guard, because it reads like one.
        actionability = 0.0
        reasons.append("condition cleared")

    result = Score(
        importance=round(min(1.0, importance), 4),
        urgency=round(min(1.0, urgency), 4),
        confidence=round(confidence, 4),
        relevance=round(relevance, 4),
        actionability=round(min(1.0, actionability), 4),
    )
    disposition, why = decide(result, rule, attention)
    reasons += why
    return Judgement(
        kind=observation.kind,
        score=result,
        disposition=disposition,
        reasons=tuple(reasons),
        dedupe_key=observation.dedupe_key,
    )


def decide(
    result: Score,
    rule: Rule,
    attention: Attention,
) -> tuple[Disposition, list[str]]:
    """Pick the disposition, then let attention lower it -- never raise it.

    The asymmetry is the whole design. Attention state is a guess about the user;
    a guess is allowed to keep the assistant quiet, and is not allowed to make it
    act. So the thresholds run first on the evidence alone, and the user's
    situation can only demote what came out.
    """
    weight = result.weight
    reasons: list[str] = []

    if result.actionability >= 0.65 and weight >= 0.62 and result.urgency >= 0.55:
        disposition: Disposition = "act"
    elif weight >= 0.45 and result.actionability >= 0.30:
        disposition = "investigate"
    elif weight >= 0.35 or (result.urgency >= 0.65 and weight >= 0.20):
        disposition = "notify"
    elif rule.memorable or weight >= 0.22:
        disposition = "remember"
    elif weight >= 0.04:
        disposition = "log"
    else:
        disposition = "ignore"

    # A clear may speak and may be recorded, but cannot start work: ``score``
    # zeroes its actionability, which puts ``investigate`` and ``act`` out of
    # reach above. Stated here because this is where a reader looks for it.

    critical = result.urgency >= CRITICAL_URGENCY and weight >= 0.5
    if critical:
        reasons.append("critical: bypasses attention")
        return disposition, reasons

    # ---- attention demotions -------------------------------------------------
    if disposition == "notify" and not attention.interruptible():
        # Held back, not thrown away: it is still worth remembering that this
        # happened while the user was unavailable.
        disposition = "remember"
        reasons.append(
            "held: assistant speaking" if attention.speaking
            else f"held: user is {attention.activity}"
        )

    # An hour that has already produced a lot of notifications raises its own
    # bar. Ten true things said in ten minutes is how a user learns to ignore an
    # assistant, at which point the eleventh -- the one that mattered -- is lost
    # too (doc2 §30).
    if disposition == "notify" and attention.recent_notifications >= 5 and weight < 0.6:
        disposition = "remember"
        reasons.append("held: recent notification volume")

    return disposition, reasons


@dataclass
class _Pending:
    """Events held back by the gate, waiting to be said as one thing."""

    count: int = 0
    first_at: float = 0.0
    last: Judgement | None = None
    summary: str = ""


@dataclass
class Aggregate:
    """What the gate releases: one judgement, plus how many it stands for."""

    judgement: Judgement
    summary: str
    suppressed: int = 0
    #: Seconds between the first held event and this release, so a caller can say
    #: "over the last few minutes" honestly rather than guessing.
    span_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "suppressed": self.suppressed,
            "span_seconds": round(self.span_seconds, 1),
            "summary": self.summary,
            **self.judgement.to_dict(),
        }


class RateGate:
    """Stops the assistant saying the same true thing over and over (doc2 §30).

    The failure this exists to prevent is specific and quoted in the spec::

        "Sir, battery is now 31 percent."
        "Sir, battery is now 30 percent."
        "Sir, battery is now 29 percent."

    Three true statements that together are worse than one. So a key that has
    spoken recently goes quiet, the events that arrive during the quiet period
    are *counted rather than dropped*, and the next release carries the newest
    state and how many it stands for -- "battery is getting low, currently 29%".

    Counting rather than dropping is the part that matters. A gate that discarded
    the middle events would make the final message indistinguishable from a
    single sample, and the user could not tell a fast drain from a slow one.

    Critical events bypass the cooldown entirely. If they did not, a battery
    falling from 20% to 4% inside one cooldown window would be silent for exactly
    the period during which speaking was the only useful thing to do.
    """

    def __init__(self, cooldown_seconds: float = 600.0) -> None:
        self.cooldown_seconds = cooldown_seconds
        self._last_spoken: dict[str, float] = {}
        self._pending: dict[str, _Pending] = {}

    def _key(self, judgement: Judgement) -> str:
        # The dedupe key identifies the *condition*; without one, the kind is the
        # next best thing, and is what makes an unkeyed monitor still rate-limit
        # rather than flood.
        return judgement.dedupe_key or judgement.kind

    def offer(
        self,
        judgement: Judgement,
        summary: str = "",
        *,
        now: float | None = None,
    ) -> Aggregate | None:
        """Present a judgement; get back what to say, or None to stay quiet.

        Only ``notify`` and above are gated -- the quieter dispositions are not
        speech, and rate-limiting a log entry would lose history for no benefit.
        """
        if rank(judgement.disposition) < rank("notify"):
            return None

        moment = time.monotonic() if now is None else now
        key = self._key(judgement)
        critical = judgement.score.urgency >= CRITICAL_URGENCY

        last = self._last_spoken.get(key)
        if last is not None and not critical and moment - last < self.cooldown_seconds:
            held = self._pending.setdefault(key, _Pending(first_at=moment))
            held.count += 1
            held.last = judgement
            held.summary = summary
            return None

        held = self._pending.pop(key, None)
        self._last_spoken[key] = moment
        span = 0.0
        if held is not None and held.count:
            span = moment - held.first_at
        return Aggregate(
            judgement=judgement,
            summary=summary,
            suppressed=held.count if held else 0,
            span_seconds=span,
        )

    def due(self, *, now: float | None = None) -> list[Aggregate]:
        """Releases for conditions that were held and whose cooldown has expired.

        Called on a timer by whoever owns the gate. Without this, an event held
        during a cooldown would only ever surface if the same condition happened
        to fire again after the window -- so a condition that stopped repeating
        while still being true would silently never be mentioned.
        """
        moment = time.monotonic() if now is None else now
        out: list[Aggregate] = []
        for key, held in list(self._pending.items()):
            last = self._last_spoken.get(key, 0.0)
            if moment - last < self.cooldown_seconds or held.last is None:
                continue
            self._pending.pop(key, None)
            self._last_spoken[key] = moment
            out.append(
                Aggregate(
                    judgement=held.last,
                    summary=held.summary,
                    suppressed=held.count,
                    span_seconds=moment - held.first_at,
                )
            )
        return out

    def pending_keys(self) -> list[str]:
        return sorted(self._pending)

    def reset(self) -> None:
        self._last_spoken.clear()
        self._pending.clear()


def prioritise(judgements: Iterable[Judgement]) -> list[Judgement]:
    """Order competing events, most pressing first (doc2 §33).

    Urgency leads because the ordering exists to decide what to handle *now*; a
    slightly less important thing that is about to become impossible outranks a
    more important thing that will wait. Weight breaks ties, then actionability,
    so that between two equally pressing events the one that can actually be
    resolved goes first.
    """
    return sorted(
        judgements,
        key=lambda j: (
            -rank(j.disposition),
            -j.score.urgency,
            -j.score.weight,
            -j.score.actionability,
            j.kind,
        ),
    )
