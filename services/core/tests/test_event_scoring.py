"""What the scoring layer promises, held to it.

Scoring is arithmetic, which makes it easy to write and easy to write *wrongly* in
a way nothing notices: every threshold is a number that can drift, and a system
whose only symptom is "it stopped mentioning things" produces no error to chase.
So the tests here are mostly about the boundaries rather than the middle -- the
exact points where a battery report becomes an interruption, where an unreliable
detector stops being allowed to act, and where the user being busy silences
something it should not.

Three properties are load-bearing and each has a test that fails if it is lost:

* confidence multiplies (an unsure detector cannot act),
* attention only ever demotes (a guess about the user cannot cause action),
* the rate gate counts what it suppresses (so "29%" is distinguishable from a
  single sample).
"""

from __future__ import annotations

import pytest

from nexus.core.scoring import (
    CRITICAL_URGENCY,
    DISPOSITIONS,
    RULES,
    Attention,
    RateGate,
    Rule,
    prioritise,
    rank,
    rule_for,
    score,
)
from nexus.monitors.base import Observation


def obs(kind: str, **kwargs) -> Observation:
    """An observation with sensible defaults, so each test names only what it means."""
    return Observation(
        kind=kind,
        summary=kwargs.pop("summary", f"{kind} happened"),
        data=kwargs.pop("data", {}),
        severity=kwargs.pop("severity", "notice"),
        dedupe_key=kwargs.pop("dedupe_key", kind),
        clears=kwargs.pop("clears", False),
        **kwargs,
    )


# --------------------------------------------------------------------- rules


def test_the_most_specific_rule_wins():
    assert rule_for("process.crashed") is RULES["process.crashed"]
    # Falls back to the family, not to the default.
    assert rule_for("process.started") is RULES["process"]
    assert rule_for("system.battery.low") is RULES["system.battery"]


def test_an_unknown_kind_gets_the_default_rather_than_vanishing():
    judgement = score(obs("something.nobody.wrote.a.rule.for"))
    # It must still be recorded: a monitor added without a rule should show up in
    # the log, not disappear.
    assert judgement.disposition != "ignore"
    assert rank(judgement.disposition) <= rank("remember")


def test_a_prefix_does_not_match_a_longer_word():
    # "processor.temperature" is not a member of the "process" family. Matching
    # on a bare prefix rather than a segment boundary would score it as one.
    assert rule_for("processor.temperature") is not RULES["process"]


# ---------------------------------------------------------------- confidence


def test_confidence_multiplies_so_an_unsure_detector_cannot_act():
    """A pixel differ that is 45% sure must not reach ``act``, ever.

    This is the property that keeps a guess from becoming an action. If
    confidence were an additive term instead, a high enough importance would
    swamp it and the screen watcher would start doing things about frames of
    video.
    """
    judgement = score(obs("screen.change", severity="alert"))
    assert judgement.score.confidence < 0.5
    assert rank(judgement.disposition) < rank("act")


def test_a_monitor_may_lower_its_confidence_but_not_raise_it():
    lowered = score(obs("system.disk", data={"percent_used": 96, "confidence": 0.2}))
    assert lowered.score.confidence == pytest.approx(0.2)

    # The same payload claiming certainty gets the rule's value, not its own.
    claimed = score(obs("screen.change", data={"confidence": 1.0}))
    assert claimed.score.confidence == pytest.approx(RULES["screen.change"].confidence)


def test_lowering_confidence_lowers_the_disposition():
    """Not just the number -- the decision has to move with it."""
    sure = score(obs("system.disk", data={"percent_used": 96}))
    unsure = score(obs("system.disk", data={"percent_used": 96, "confidence": 0.15}))
    assert rank(unsure.disposition) < rank(sure.disposition)


# ------------------------------------------------------------------- battery


def test_battery_urgency_tracks_what_is_actually_left():
    percentages = [50, 18, 8, 4]
    urgencies = [
        score(obs("system.battery", data={"percent": p, "plugged_in": False})).score.urgency
        for p in percentages
    ]
    assert urgencies == sorted(urgencies), urgencies
    assert urgencies[0] < urgencies[-1]


def test_a_nearly_empty_battery_is_critical_and_a_half_full_one_is_not():
    empty = score(obs("system.battery", data={"percent": 4, "plugged_in": False}))
    assert empty.score.urgency >= CRITICAL_URGENCY
    half = score(obs("system.battery", data={"percent": 50, "plugged_in": False}))
    assert half.score.urgency < CRITICAL_URGENCY


def test_a_battery_on_the_charger_is_a_status_line_not_a_warning():
    plugged = score(obs("system.battery", data={"percent": 9, "plugged_in": True}))
    unplugged = score(obs("system.battery", data={"percent": 9, "plugged_in": False}))
    assert plugged.score.urgency < unplugged.score.urgency
    assert rank(plugged.disposition) < rank("notify")


def test_a_battery_reading_with_no_percentage_still_scores():
    """A missing field must not crash the fast path or silently zero the event."""
    judgement = score(obs("system.battery", data={}))
    assert judgement.score.importance > 0


# ---------------------------------------------------------------------- disk


def test_a_disk_with_no_room_left_outranks_one_that_is_merely_full():
    tight = score(obs("system.disk", data={"percent_used": 91}))
    gone = score(obs("system.disk", data={"percent_used": 99}))
    assert gone.score.urgency > tight.score.urgency
    assert rank(gone.disposition) >= rank(tight.disposition)


def test_a_full_disk_is_actionable_and_a_dead_network_is_not():
    """The pair that justifies keeping importance and actionability apart."""
    disk = score(obs("system.disk", data={"percent_used": 97}))
    network = score(obs("system.network", data={"online": False}))
    assert network.score.importance > 0.5
    assert network.score.actionability < disk.score.actionability
    assert disk.disposition == "act"
    assert network.disposition != "act"


# ------------------------------------------------------------------ attention


def test_attention_can_silence_a_notification_without_losing_it():
    event = obs("system.cpu", data={"percent": 93}, severity="alert")
    free = score(event, Attention(activity="idle"))
    busy = score(event, Attention(activity="call"))
    assert rank(busy.disposition) < rank(free.disposition)
    # Held, not discarded: it is still worth having happened.
    assert busy.disposition != "ignore"


def test_relevance_alone_can_change_the_decision():
    """Dulling has to reach the disposition, not just sit in the record.

    ``coding`` is an interruptible state, so nothing else in the attention path
    demotes here -- if relevance stopped feeding the weight, this event would
    still be announced while the user was in the middle of the work that made it
    unremarkable. That is the exact failure the multiplier exists to prevent, and
    the "user is in a call" case cannot see it, because the speech hold masks it.
    """
    event = obs("system.cpu", data={"percent": 93}, severity="alert")
    idle = score(event, Attention(activity="idle"))
    coding = score(event, Attention(activity="coding"))
    assert idle.disposition == "notify"
    assert rank(coding.disposition) < rank("notify")
    assert coding.score.weight < idle.score.weight


def test_attention_never_raises_a_disposition():
    """The asymmetry that keeps a guess about the user from causing action.

    Whatever the attention state, the disposition may only be the one the
    evidence produced or something quieter. A version that let ``focus_topic``
    push an event *up* into ``act`` would let a stale guess about what the user
    cares about start work on its own.
    """
    cases = [
        obs("window.focus", data={"app": "Firefox"}),
        obs("system.cpu", data={"percent": 95}, severity="alert"),
        obs("process.crashed", summary="crawler crashed", data={"name": "crawler"}),
        obs("clipboard.credential", severity="alert"),
        obs("screen.change"),
    ]
    states = [
        Attention(),
        Attention(activity="coding"),
        Attention(activity="watching"),
        Attention(speaking=True),
        Attention(focus_topic="crawler"),
        Attention(activity="coding", focus_topic="crawler", recent_notifications=9),
    ]
    for event in cases:
        baseline = rank(score(event, Attention()).disposition)
        for state in states:
            got = score(event, state)
            assert rank(got.disposition) <= baseline, (event.kind, state, got.disposition)


def test_a_critical_event_reaches_the_user_whatever_they_are_doing():
    """The bypass is only observable where attention would otherwise bite.

    An event that lands on ``investigate`` is never held in the first place, so
    testing the bypass with one proves nothing. This uses a lost network at
    alert severity, which lands on ``notify`` -- actionability is too low to
    investigate -- with urgency right at the critical bar. Without the bypass a
    call or a noisy hour would swallow it.
    """
    event = obs("system.network", data={"online": False}, severity="alert")
    assert event.severity == "alert"
    baseline = score(event, Attention())
    assert baseline.disposition == "notify"
    assert baseline.score.urgency >= CRITICAL_URGENCY

    for state in (
        Attention(activity="call"),
        Attention(activity="watching"),
        Attention(speaking=True),
        Attention(recent_notifications=50),
    ):
        assert score(event, state).disposition == "notify", state


def test_a_dying_battery_reaches_the_user_whatever_they_are_doing():
    event = obs("system.battery", data={"percent": 3, "plugged_in": False},
                severity="alert")
    for state in (
        Attention(activity="call"),
        Attention(speaking=True),
        Attention(recent_notifications=50),
    ):
        judgement = score(event, state)
        assert rank(judgement.disposition) >= rank("notify"), state


def test_the_assistant_does_not_speak_over_itself_for_ordinary_events():
    """Only ``notify`` is gated -- investigating quietly while speaking is fine.

    The bar is speech, not work. An event that lands on ``investigate`` starts
    the reasoning model looking at something; it does not put words in the user's
    ear, so having the floor is no reason to hold it back.
    """
    event = obs("system.network", data={"online": False}, severity="notice")
    assert score(event, Attention(speaking=False)).disposition == "notify"
    assert rank(score(event, Attention(speaking=True)).disposition) < rank("notify")


def test_a_noisy_hour_raises_its_own_bar():
    # A "back online" note is exactly the kind of true, unpressing thing that
    # should yield when the hour has already been loud.
    event = obs("system.network", data={"online": True}, severity="notice")
    quiet = score(event, Attention(recent_notifications=0))
    noisy = score(event, Attention(recent_notifications=8))
    assert quiet.disposition == "notify"
    assert noisy.disposition != "notify"


def test_a_loud_hour_does_not_silence_a_weighty_event():
    """The volume bar has a weight exemption, or it would bury the one that mattered."""
    event = obs("system.network", data={"online": False}, severity="notice")
    assert score(event, Attention(recent_notifications=8)).disposition == "notify"


def test_a_focus_topic_makes_a_matching_event_more_relevant():
    """The version that boosted the match was a silent no-op.

    Relevance was already 1.0, so ``min(1.0, 1.0 + 0.5)`` changed nothing and an
    event about the user's own crawler scored identically to one about anything
    else. The discrimination has to be visible in the number, or §9's
    "prioritise development-related information" is decoration.
    """
    event = obs("process.exited", summary="crawler exited", data={"name": "crawler"})
    unrelated = score(event, Attention(focus_topic="spreadsheet"))
    matching = score(event, Attention(focus_topic="crawler"))
    assert matching.score.relevance > unrelated.score.relevance
    # And a match is worth no more than having no focus at all -- attention may
    # not raise anything above the evidence.
    assert matching.score.relevance == score(event, Attention()).score.relevance


def test_a_focus_topic_can_rescue_an_event_that_would_be_background_noise():
    event = obs("system.cpu", summary="crawler is using 95% CPU",
                data={"percent": 95}, severity="alert")
    coding = score(event, Attention(activity="coding"))
    watching_it = score(event, Attention(activity="coding", focus_topic="crawler"))
    assert watching_it.score.relevance > coding.score.relevance


def test_window_noise_is_ignorable_while_working_and_still_recorded_when_idle():
    event = obs("window.title", data={"title": "README.md"}, severity="info")
    assert rank(score(event, Attention(activity="coding")).disposition) <= rank("log")


# --------------------------------------------------------------------- clears


def test_a_clear_can_speak_but_can_never_trigger_action():
    """Recovering from a recovery is how a control loop oscillates."""
    problem = obs("system.disk", data={"percent_used": 99}, severity="alert")
    assert score(problem).disposition == "act"

    recovered = obs(
        "system.disk", data={"percent_used": 40}, severity="notice", clears=True
    )
    judgement = score(recovered)
    assert judgement.score.actionability == 0.0
    assert rank(judgement.disposition) <= rank("notify")


def test_a_clear_is_not_urgent():
    judgement = score(
        obs("system.battery", data={"percent": 4, "plugged_in": False}, clears=True)
    )
    assert judgement.score.urgency <= 0.25


# ------------------------------------------------------------------- severity


def test_severity_moves_the_score_without_setting_it():
    """A monitor calling something an alert is evidence, not a verdict.

    Two kinds at the same severity must still score differently, or the rule
    table has stopped mattering.
    """
    noise = score(obs("window.title", severity="alert"))
    real = score(obs("clipboard.credential", severity="alert"))
    assert real.score.weight > noise.score.weight
    assert rank(real.disposition) > rank(noise.disposition)


def test_raising_severity_raises_the_weight_for_one_kind():
    low = score(obs("system.cpu", severity="info"))
    high = score(obs("system.cpu", severity="alert"))
    assert high.score.weight > low.score.weight


# ------------------------------------------------------------------ rate gate


def test_the_gate_counts_what_it_suppresses_rather_than_dropping_it():
    """"29%" has to be distinguishable from a single sample.

    A gate that discarded the held events would make the release identical to a
    one-off reading, and the user could not tell a fast drain from a slow one.
    """
    gate = RateGate(cooldown_seconds=600)
    first = score(obs("system.memory", data={"percent": 88}, severity="alert"))
    assert gate.offer(first, "memory is high", now=0.0) is not None

    for step in range(1, 5):
        assert gate.offer(first, f"memory sample {step}", now=float(step)) is None

    release = gate.offer(first, "memory still high", now=700.0)
    assert release is not None
    assert release.suppressed == 4
    assert release.summary == "memory still high"
    assert release.span_seconds == pytest.approx(699.0)


def test_a_critical_event_bypasses_the_cooldown():
    """Otherwise a battery falling 20% -> 4% inside one window is silent.

    Which is exactly the period during which speaking is the only useful thing to
    do, so a gate without this exception fails precisely when it matters.
    """
    gate = RateGate(cooldown_seconds=600)
    warning = score(obs("system.battery", data={"percent": 19, "plugged_in": False}))
    assert gate.offer(warning, "battery low", now=0.0) is not None

    critical = score(obs("system.battery", data={"percent": 4, "plugged_in": False}))
    assert critical.score.urgency >= CRITICAL_URGENCY
    assert gate.offer(critical, "battery critical", now=30.0) is not None


def test_the_gate_only_holds_speech():
    """Rate-limiting a log entry would lose history for no benefit."""
    gate = RateGate(cooldown_seconds=600)
    quiet = score(obs("window.focus", data={"app": "Firefox"}))
    assert rank(quiet.disposition) < rank("notify")
    assert gate.offer(quiet, "focus changed", now=0.0) is None
    assert gate.pending_keys() == []


def test_different_conditions_do_not_share_a_cooldown():
    gate = RateGate(cooldown_seconds=600)
    disk = score(obs("system.disk", data={"percent_used": 96}, dedupe_key="disk:/"))
    memory = score(obs("system.memory", data={"percent": 90}, severity="alert",
                       dedupe_key="memory"))
    assert gate.offer(disk, "disk", now=0.0) is not None
    assert gate.offer(memory, "memory", now=1.0) is not None


def test_a_held_condition_that_stops_repeating_is_still_released():
    """Without ``due``, a held event only surfaces if the condition fires again.

    So a problem that stayed true but stopped generating samples would be
    silently never mentioned -- the gate would have eaten the only report.
    """
    gate = RateGate(cooldown_seconds=600)
    event = score(obs("system.memory", data={"percent": 91}, severity="alert"))
    gate.offer(event, "memory high", now=0.0)
    gate.offer(event, "memory still high", now=10.0)
    assert gate.pending_keys() == ["system.memory"]

    assert gate.due(now=100.0) == []          # cooldown has not expired yet
    released = gate.due(now=800.0)
    assert len(released) == 1
    assert released[0].suppressed == 1
    assert gate.pending_keys() == []


def test_an_event_with_no_dedupe_key_still_rate_limits():
    gate = RateGate(cooldown_seconds=600)
    event = score(obs("system.memory", data={"percent": 90}, severity="alert",
                      dedupe_key=""))
    assert gate.offer(event, "memory", now=0.0) is not None
    assert gate.offer(event, "memory", now=5.0) is None


def test_two_unkeyed_kinds_do_not_share_one_cooldown():
    """Falling back to the kind is what makes the fallback useful.

    Falling back to a constant would also "rate limit" -- and would put every
    unkeyed monitor in the system behind a single ten-minute gate, so the first
    event of the hour would silence all the others. The previous test cannot see
    the difference, because one condition offered twice looks the same either way.
    """
    gate = RateGate(cooldown_seconds=600)
    memory = score(obs("system.memory", data={"percent": 90}, severity="alert",
                       dedupe_key=""))
    network = score(obs("system.network", data={"online": False}, dedupe_key=""))
    assert gate.offer(memory, "memory", now=0.0) is not None
    assert gate.offer(network, "network", now=1.0) is not None


# ---------------------------------------------------------------- prioritise


def test_the_most_pressing_event_sorts_first():
    battery = score(obs("system.battery", data={"percent": 3, "plugged_in": False},
                        severity="alert"))
    cleanup = score(obs("system.cpu", data={"percent": 91}, severity="notice"))
    focus = score(obs("window.focus", data={"app": "Firefox"}))
    ordered = prioritise([focus, cleanup, battery])
    assert ordered[0] is battery
    assert ordered[-1] is focus


def test_urgency_outranks_weight_within_one_disposition():
    """The tie-break that makes the ordering about *now* rather than about size.

    Disposition alone settles most pairs, so a sort key that had lost urgency
    entirely would still pass the test above. These two both land on ``notify``:
    a possible network loss the monitor is unsure about, and a confirmed
    recovery. The uncertain one weighs less and matters sooner, and the point of
    leading with urgency is that it goes first anyway.
    """
    unsure_loss = score(
        obs("system.network", data={"online": False, "confidence": 0.45},
            dedupe_key="net:down")
    )
    restored = score(
        obs("system.network", data={"online": True}, dedupe_key="net:up")
    )
    assert unsure_loss.disposition == restored.disposition == "notify"
    assert unsure_loss.score.urgency > restored.score.urgency
    assert unsure_loss.score.weight < restored.score.weight
    assert prioritise([restored, unsure_loss])[0] is unsure_loss


def test_ordering_is_stable_for_identical_events():
    a = score(obs("system.cpu", data={"percent": 91}))
    b = score(obs("system.cpu", data={"percent": 91}))
    assert prioritise([a, b]) == [a, b] or prioritise([a, b]) == [b, a]
    # The point is that it does not raise and does not lose an entry.
    assert len(prioritise([a, b])) == 2


# ------------------------------------------------------------------ structure


def test_every_rule_is_in_range():
    """A rule above 1.0 would quietly break every threshold below it."""
    for kind, rule in RULES.items():
        for name in ("importance", "urgency", "actionability", "confidence"):
            value = getattr(rule, name)
            assert 0.0 <= value <= 1.0, f"{kind}.{name} = {value}"


def test_dispositions_are_ordered_from_quiet_to_loud():
    assert DISPOSITIONS[0] == "ignore"
    assert DISPOSITIONS[-1] == "act"
    assert rank("log") < rank("notify") < rank("act")


def test_a_judgement_serialises_with_its_reasons():
    judgement = score(obs("system.disk", data={"percent_used": 99}))
    payload = judgement.to_dict()
    assert payload["disposition"] == "act"
    assert payload["kind"] == "system.disk"
    assert payload["reasons"], "a decision with no stated reason is not reviewable"
    assert 0.0 <= payload["weight"] <= 1.0


def test_scoring_reads_nothing_and_is_reproducible():
    """Pure: same input, same answer, no clock and no I/O."""
    event = obs("system.disk", data={"percent_used": 93})
    first = score(event, Attention(activity="coding"))
    second = score(event, Attention(activity="coding"))
    assert first == second
