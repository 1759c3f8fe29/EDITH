"""Where it is safe to start speaking, checked case by case (§12, §37, §94).

Every test here is a sentence a user would have heard go wrong. The module exists so
that NEXUS starts talking on the first sentence of an answer instead of the last, and
the whole risk of doing that is cutting in a place that makes the audio wrong -- a
segment that ends "the value is three point" and a next one that starts "one four".

Two properties are asserted throughout rather than the exact segment list, because the
segment list is a tuning decision and these two are not:

* **The concatenation of the sources is the input.** Nothing is dropped and nothing is
  duplicated, whatever the chunking. A segmenter that loses a clause is worse than one
  that waits.
* **The same text segments the same way however it arrives.** The model's chunk
  boundaries are arbitrary -- one character or one paragraph -- and they must not change
  what is spoken.
"""
from __future__ import annotations

import pytest

from nexus.ai import speech_stream as ss
from nexus.providers import mistral_audio, nvidia_speech


def run(text: str, chunk: int = 7) -> list[ss.Segment]:
    """Feed ``text`` in fixed-size chunks, the way a token stream arrives."""
    seg = ss.Segmenter()
    out: list[ss.Segment] = []
    for i in range(0, len(text), chunk):
        out.extend(seg.feed(text[i : i + chunk]))
    out.extend(seg.flush())
    return out


def spoken(text: str, chunk: int = 7) -> list[str]:
    return [s.spoken for s in run(text, chunk)]


# ---------------------------------------------------------------- the two invariants


CORPUS = [
    "Yes. It works.",
    "Done. The disk is at 71 percent, which is fine, and the backup finished at four.",
    "Pi is 3.14 and Dr. Chandra agrees. e.g. this must not split early. It ends here.",
    "Here is the fix.\n\n```python\ndef f():\n    return 1\n```\n\nThat is all.",
    "Three things.\n1. Check the disk.\n2. Restart the core.\n3. Read the log.\n",
    "मैंने फ़ाइल पढ़ ली। अब मैं इसे लिख रहा हूँ। यह हो गया।",
    "so " * 130,
    "No terminator at all just a long clause, another clause, and a third one trailing",
    "",
    "   ",
    "?!",
]


def dense(text: str) -> str:
    """The text with all whitespace removed.

    The invariant is about *content*, not layout: the whitespace between two spoken
    segments is deliberately discarded, because it is a gap the synthesiser fills with a
    pause. Comparing dense forms is what distinguishes "the blank line was dropped",
    which is correct, from "a clause was dropped", which is not.
    """
    return "".join(text.split())


@pytest.mark.parametrize("text", CORPUS)
def test_nothing_is_lost_or_duplicated(text: str) -> None:
    """Sources concatenate back to the input. The one thing a segmenter must not do."""
    assert dense("".join(s.source for s in run(text))) == dense(text)
    # And in order -- a reversal would satisfy a set comparison.
    joined = "".join(s.source for s in run(text))
    assert dense(joined) == dense(text)
    for size in (1, 3, 40):
        assert dense("".join(s.source for s in run(text, size))) == dense(text)


@pytest.mark.parametrize("text", CORPUS)
def test_chunking_does_not_change_what_is_spoken(text: str) -> None:
    """A one-character stream and a whole-string stream must say the same thing.

    This is the test that would catch a rule keyed on where the buffer happens to end,
    which is the natural bug in anything that decides on a growing string.
    """
    baseline = spoken(text, chunk=1)
    for size in (2, 3, 5, 17, 500):
        assert spoken(text, chunk=size) == baseline, f"chunk={size}"
    assert [s.spoken for s in ss.segments_of(text)] == baseline


# ------------------------------------------------------------------- time to first word


def test_the_first_sentence_goes_out_before_the_rest_arrives() -> None:
    """The whole point of the module: audio starts on sentence one, not on the last.

    ``feed`` is asserted to have produced something *before* the rest of the answer is
    fed, which is the difference between speaking in under a second and waiting for a
    four-hundred-word answer to finish.
    """
    seg = ss.Segmenter()
    first = seg.feed("The disk is fine. ")
    assert [s.spoken for s in first] == ["The disk is fine."]
    # And it did not wait for a comfortable length: one short sentence is enough.
    assert len(first[0].spoken) < ss.LATER_SEGMENT_MIN


def test_later_segments_are_accumulated_rather_than_dribbled() -> None:
    """After the first, short sentences are merged, because prosody needs a phrase.

    A separate synthesis request per three-word sentence gives each its own pitch
    contour, and the result sounds like a ransom note. So this asserts the *absence* of
    a segment boundary that the naive rule would have produced.
    """
    seg = ss.Segmenter()
    assert [s.spoken for s in seg.feed("Ready. ")] == ["Ready."]
    assert seg.feed("Yes. ") == []
    assert seg.feed("No. ") == []
    tail = seg.flush()
    assert [s.spoken for s in tail] == ["Yes. No."]


# --------------------------------------------------------------------- the cut rules


@pytest.mark.parametrize(
    "text, must_not_start",
    [
        ("The value is 3.14 exactly and nothing else matters here at all right now.", "14"),
        ("Ask Dr. Chandra about it before you touch anything else on that machine.", "Chandra"),
        ("Use it e.g. like this, which is the shortest form that still reads well.", "like"),
        ("Read it at example.com later on, once the rest of this has finished up.", "com"),
        ("Signed by J. Robert Oppenheimer, according to the letter in the archive.", "Robert"),
        ("Version 2.1 shipped and the notes are in the file you asked me to open.", "1"),
    ],
)
def test_a_period_inside_a_token_is_not_a_sentence(text: str, must_not_start: str) -> None:
    """Six ways a period lies. Each one, cut wrongly, is audible.

    The assertion is on what a *later* segment starts with, because that is the audible
    half: a listener notices "one four exactly" much faster than a slightly short first
    segment.
    """
    for segment in spoken(text)[1:]:
        assert not segment.startswith(must_not_start), text


def test_a_numbered_list_marker_is_not_a_boundary() -> None:
    text = "Do this.\n1. Open the file.\n2. Read it.\n"
    for segment in spoken(text):
        assert segment not in {"1.", "2."}
        assert not segment.startswith("Open the file")


def test_a_closing_quote_stays_with_its_sentence() -> None:
    text = 'She said "stop." Then the machine stopped, and nothing else happened after that.'
    first = spoken(text)[0]
    assert first == 'She said "stop."'


@pytest.mark.parametrize("terminator", [".", "!", "?", "…", "।", "。", "！"])
def test_every_terminator_is_a_boundary(terminator: str) -> None:
    """Including the non-ASCII ones. NEXUS answers in the user's language (§37).

    Without the Devanagari danda and the CJK stop, a Hindi or Japanese answer finds no
    boundary at all and falls back to the character ceiling -- so the voice waits, and
    then cuts mid-clause, for entire languages.
    """
    out = spoken(f"First one{terminator} And then a much longer second part follows here.")
    assert out[0] == f"First one{terminator}"


# ----------------------------------------------------------------------- the ceiling


def test_a_run_with_no_terminator_still_gets_spoken() -> None:
    """A model can write three hundred characters without a full stop.

    Waiting for one would be a hang with no error, which is the worst kind. Past the
    ceiling this cuts at the best break available.
    """
    text = "and then it kept going " * 20
    out = run(text)
    assert len(out) > 1
    assert all(len(s.source) <= ss.SEGMENT_CEILING for s in out[:-1])


def test_the_ceiling_never_cuts_mid_word() -> None:
    text = "alpha bravo charlie delta echo foxtrot golf hotel india juliett " * 8
    for segment in run(text):
        assert not segment.spoken.endswith(("alph", "brav", "charli", "delt"))
        # Every segment begins and ends on a whole word from the input.
        assert segment.spoken.split()[0] in text.split()
        assert segment.spoken.split()[-1] in text.split()


def test_one_token_longer_than_the_ceiling_is_not_chopped() -> None:
    """A URL or a base64 blob has no space in it. Cutting it would be gibberish."""
    blob = "x" * (ss.SEGMENT_CEILING + 50)
    out = run(blob)
    assert [s.spoken for s in out] == [blob]


def test_the_hard_ceiling_matches_the_provider_limit() -> None:
    """The number is duplicated to keep this module free of the provider layer.

    Duplicated, so it can drift; this is the test that notices. A segment over the
    provider's limit is a 413 the user hears as silence.

    Checked against *both* speech providers rather than one: the ceiling guards the
    turn's segmentation, and the routed provider can be either. It is set to the
    smaller of the two so a segment is safe on whichever one serves it -- matching
    only one would let the module drift past the other's limit the day routing
    changes, which is a failure that starts working only on the fallback.
    """
    assert ss.HARD_CEILING == min(
        mistral_audio.MAX_TTS_CHARS, nvidia_speech.MAX_TTS_CHARS
    )


def test_no_segment_exceeds_the_hard_ceiling() -> None:
    text = "word " * 3000
    for segment in run(text, chunk=997):
        assert len(segment.source) <= ss.HARD_CEILING


# ---------------------------------------------------------------------- code blocks


def test_a_code_block_is_described_and_not_recited() -> None:
    """Nobody wants forty seconds of "open paren, self, dot, underscore"."""
    out = run("Here is the fix.\n\n```python\ndef f():\n    return 1\n```\n\nThat is all.")
    kinds = [s.kind for s in out]
    assert kinds == ["prose", "code", "prose"]
    code = out[1]
    assert "2 lines" in code.spoken
    assert "python" in code.spoken
    # The source is kept whole so a transcript can still show the code.
    assert "def f():" in code.source
    assert "def f():" not in code.spoken


def test_a_code_block_is_never_split_in_half() -> None:
    """The worst case: two halves of code, each read as if it were a sentence.

    The fence contains sentence terminators (``return 1.5;``) precisely so that a
    segmenter which forgot about fences would cut inside it.
    """
    text = "Look.\n\n```c\nfloat a = 1.5;\nreturn a. // not a sentence\n```\n\nDone.\n"
    out = run(text, chunk=3)
    code = [s for s in out if s.is_code]
    assert len(code) == 1
    assert code[0].source.count("```") == 2


def test_an_unclosed_fence_is_still_spoken_and_says_so() -> None:
    """A stream that ends mid-fence must not swallow the rest of the answer."""
    out = run("Look:\n```js\nconst a = 1;\nconst b = 2;\n")
    assert [s.kind for s in out] == ["prose", "code"]
    assert "not closed" in out[1].spoken


def test_inline_backticks_are_not_a_fence() -> None:
    """``Run `ls` now`` is prose. A fence is a line that starts with three of them."""
    text = "Run `ls -la` now and then check `df -h` afterwards, both of which are safe."
    out = run(text)
    assert all(s.kind == "prose" for s in out)
    assert "`ls -la`" in out[0].source


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_both_fence_markers_are_recognised(fence: str) -> None:
    out = run(f"See:\n{fence}\nplain text\n{fence}\nend.")
    assert [s.kind for s in out] == ["prose", "code", "prose"]


def test_an_indented_fence_is_a_fence() -> None:
    """Markdown allows up to three spaces. A regex anchored on ``^```` alone misses it."""
    out = run("Note.\n\n  ```\n  x = 1\n  ```\n\nDone.")
    assert [s.kind for s in out] == ["prose", "code", "prose"]


def test_the_fence_pattern_does_not_swallow_the_preceding_newline() -> None:
    """The bug this pattern was written wrong for, kept as a test.

    With ``\\s*`` instead of ``[ \\t]*`` the match starts on the blank line *above* the
    fence. The buffer then begins with a newline, the search for the closing fence finds
    the opening one again, and the block is recited as prose and reported as unclosed --
    all three symptoms at once, from one character class.
    """
    match = ss._FENCE.search("text\n\n```py\n")
    assert match is not None
    assert match.group(0) == "```"
    assert "text\n\n```py\n"[match.start()] == "`"


# --------------------------------------------------------------------------- flush


def test_a_cancelled_turn_speaks_its_partial_sentence() -> None:
    """§21: stop means stop, and what was already said is what happened.

    The tail is a half sentence. Speaking it is honest -- the user heard the model stop
    mid-thought, which is true -- and dropping it would leave the last thing on screen
    unspoken.
    """
    seg = ss.Segmenter()
    seg.feed("I checked the disk. It is at seventy one per")
    tail = seg.flush()
    assert [s.spoken for s in tail] == ["It is at seventy one per"]
    assert seg.pending == ""


def test_flush_on_an_empty_stream_says_nothing() -> None:
    seg = ss.Segmenter()
    assert seg.feed("") == []
    assert seg.flush() == []


def test_whitespace_only_is_not_a_segment() -> None:
    assert run("   \n\n  ") == []


def test_pending_is_what_has_not_been_spoken() -> None:
    seg = ss.Segmenter()
    seg.feed("First one. And a second that is not finished")
    assert seg.pending.strip() == "And a second that is not finished"


def test_a_clause_break_inside_a_number_is_not_a_cut() -> None:
    """The ceiling cut needs whitespace after the comma, not just a comma.

    A terminator-free run is cut at its last clause break, and "1,234,567" contains two
    of them. Cutting there gives a segment ending "one million two hundred thirty four"
    and a next one starting "five hundred sixty seven" -- audibly a different number,
    with nothing on screen to explain it. So a clause break only counts when a space
    follows it, and here that leaves the last ordinary space as the cut.
    """
    number = "1,234,567"
    text = "so " * 100 + number + " and it goes on for a while yet"
    out = run(text, chunk=331)
    assert any(number in segment.spoken for segment in out), [s.spoken for s in out]


def test_one_unbreakable_token_is_cut_at_the_provider_limit_while_streaming() -> None:
    """A 4 000-character token with no space in it must still be spoken.

    ``_ceiling_cut`` refuses to cut mid-word, which is right for a long word and wrong
    for a base64 blob: waiting for a space that will never arrive is a hang, and past
    the provider's own limit the request would fail anyway. So the hard ceiling is a
    cut, taken mid-stream rather than at the flush -- checked before ``flush()`` here,
    because a segment that only appears once the stream ends is a segment that was not
    streamed.
    """
    seg = ss.Segmenter()
    out = seg.feed("x" * (ss.HARD_CEILING + 50))
    assert len(out) == 1
    assert len(out[0].source) == ss.HARD_CEILING
    assert len(seg.pending) == 50


def test_nothing_over_the_provider_limit_survives_the_flush() -> None:
    """The invariant the flush relies on, asserted where it is enforced.

    ``flush()`` appends its tail as one segment and does not split it, which is only
    correct because draining at the end cannot leave more than the hard ceiling behind.
    A space-free run of three times the limit is the case that would break it.
    """
    text = "x" * (ss.HARD_CEILING * 3 + 17)
    out = run(text, chunk=1_000)
    assert "".join(segment.source for segment in out) == text
    for segment in out:
        assert len(segment.source) <= ss.HARD_CEILING


# ------------------------------------------------- the two pure helpers, directly

# Everything above drives ``Segmenter``, which is the right level for almost all of it.
# It is the wrong level for these two functions, and a mutation run is what showed it:
# breaking ``_is_abbreviation``'s decimal rule and ``_prose_boundary``'s end-of-text rule
# changed no segment anywhere, across 5,280 chunkings of a thousand generated inputs,
# because in both cases the caller declines the same cut for a second reason.
#
# That makes them redundant, not dead -- they execute constantly -- and redundancy is
# not a licence to leave them unasserted. Each is a documented pure function; a caller
# that happens to agree with it today is not a test of what it promises. So these tests
# call them, and they are the only tests here that a reader should not translate into a
# sentence the user hears.


@pytest.mark.parametrize(
    ("text", "dot", "expected", "why"),
    [
        # A known abbreviation.
        ("Use e.g. this one", 7, True, "e.g. is in the list"),
        ("See Dr. Chandra", 6, True, "dr. is in the list"),
        ("It is fine.", 10, False, "an ordinary word ending a sentence"),
        # An initial.
        ("From J. Smith", 6, True, "a single letter and a period"),
        ('He said "A. Then"', 10, True, "an initial inside a quote"),
        ("Go to ab. Then", 8, False, "two letters is a word, not an initial"),
        # The cost of the initial rule, recorded rather than pretended away: a sentence
        # whose last word is a single letter does not end here. "Grade B." runs into the
        # next sentence. It is the right trade -- initials are common in names and
        # one-letter sentence endings are rare -- but it is a real limitation, not a bug
        # to be found later, so it is pinned as the behaviour it is.
        ("The grade is B. Well done", 14, True, "indistinguishable from an initial"),
        # A list marker or ordinal, at the start of its line only.
        ("1. First item", 1, True, "a list marker opening the text"),
        ("Done.\n12. Second", 8, True, "a list marker opening its line"),
        ("  3. Indented", 3, True, "leading whitespace is still a line start"),
        ("I counted 42. That is fine", 12, False, "mid-line, so it ends the sentence"),
        # A decimal.
        ("It is 3.14 exactly", 7, True, "digits on both sides"),
        ("It is 3. 14 apart", 7, False, "a space after the dot is not a decimal"),
        ("Chapter 3.4 is next", 9, True, "digits on both sides again"),
    ],
)
def test_is_abbreviation_case_by_case(text: str, dot: int, expected: bool, why: str) -> None:
    """The four documented cases, each asserted where it is decided.

    ``_prose_boundary`` consults this before anything else, so a wrong answer here is a
    sentence spoken with the wrong shape -- either two run together or one cut in half.
    """
    assert text[dot] == ".", f"the test itself points at {text[dot]!r}, not a period"
    assert ss._is_abbreviation(text, dot) is expected, why


def test_a_sentence_ending_in_a_number_is_still_a_sentence() -> None:
    """The list-marker rule is positional, and dropping the position is audible.

    Without the line-start check every ``digits.`` reads as a list marker, so "I counted
    42." never ends a sentence: it runs into the next one, and the first segment -- the
    one the whole module exists to deliver early -- arrives late or not at all. Asserted
    end to end as well as on the helper because that is the failure a user would notice.
    """
    assert spoken("I counted 42. That is all fine and well here.") == [
        "I counted 42.",
        "That is all fine and well here.",
    ]
    # And the positional rule still holds where it should: a real list does not split.
    assert ss._is_abbreviation("1. First\n2. Second", 1) is True


@pytest.mark.parametrize("chunk", [1, 3, 7, 5_000])
def test_the_end_of_the_text_is_a_boundary_only_on_the_flush(chunk: int) -> None:
    """``at_end`` is the whole difference between a terminator and a boundary.

    Mid-stream a trailing "3." may be about to become "3.14", so it is not a cut. On the
    flush there is no next chunk and it is. Both directions are asserted: a function that
    returned a boundary either way would cut mid-number on every streamed decimal, and one
    that returned -1 either way would leave the last sentence to the tail path -- which
    produces the same audio today and is a different contract from the one documented.
    """
    text = "It is done."
    assert ss._prose_boundary(text, min_len=1, at_end=False) == -1
    assert ss._prose_boundary(text, min_len=1, at_end=True) == len(text)
    # Below ``min_len`` it is not a cut even at the end, so a later short sentence is
    # accumulated rather than dribbled out on its own.
    assert ss._prose_boundary(text, min_len=90, at_end=True) == -1
    # Whatever the helper says, the text still segments the same way through the object.
    assert spoken(text, chunk) == ["It is done."]


def test_a_terminator_inside_a_token_is_never_a_boundary() -> None:
    """The other half of the rule the decimal check duplicates.

    ``example.com`` and ``3.14`` are declined for two independent reasons -- the
    abbreviation rules and the no-whitespace-after rule -- and both are asserted, because
    "some other branch happens to catch this" is how a rule stops being checked.
    """
    for text in ("Go to example.com now", "It is 3.14 exactly", "Read a.b.c here"):
        assert ss._prose_boundary(text, min_len=1, at_end=False) == -1, text


# -------------------------------------------------------------------- describe_code


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        ("```py\n```", "There is an empty py code block on screen."),
        ("```py\nx = 1\n```", "There is one line of py code on screen."),
        ("```py\nx = 1\ny = 2\n```", "There are 2 lines of py code on screen."),
        # Blank lines inside a block are not lines of code.
        ("```py\nx = 1\n\n\n```", "There is one line of py code on screen."),
        # No language, and a tilde fence.
        ("~~~\nx = 1\ny = 2\n~~~", "There are 2 lines of code on screen."),
        # A language that is not a word is not read out as one.
        ("```c++\nint x;\n```", "There is one line of code on screen."),
    ],
)
def test_describe_code_says_how_much_code_there_is(block: str, expected: str) -> None:
    """The sentence a user hears in place of a code block, including its grammar.

    "There are 1 lines of py code on screen" is the kind of wrong that makes a voice sound
    like a machine reading a template, and the singular branch is one character away from
    it. Counting is checked at 0, 1 and 2 because those are the three shapes of the
    sentence, and blank lines are checked because a block padded with them is common.
    """
    assert ss.describe_code(block) == expected
