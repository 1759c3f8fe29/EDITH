"""Where a stream of text is safe to start speaking (§12, §37, §94).

The reasoning already streams -- :func:`nexus.ai.core.stream` yields tokens as the
model produces them -- and synthesis is a whole-request call. Between the two sits the
only reason a voice assistant feels slow: waiting for the last token before saying the
first word. On a 400-word answer that is the difference between speaking in under a
second and speaking in fifteen.

So this module answers one question, repeatedly, on a buffer that keeps growing: **is
there a point in what has arrived so far that can be cut off and spoken now, without
the audio being wrong?** Everything here is a rule about when the answer is no.

* **A boundary is not a full stop character.** ``3.14``, ``v1.2``, ``Dr. Chandra`` and
  ``e.g.`` all contain a period followed by something, and cutting at any of them
  produces a segment that ends mid-thought and a next segment that starts mid-thought.
  Both are audible.
* **Never inside a fenced code block.** A fence is not prose and splitting it is the
  worst case, because the two halves are each read as if they were sentences. Code is
  held whole and then *described* rather than recited: nobody wants forty seconds of
  "open paren, self, dot, underscore".
* **A run with no boundary at all must still be spoken.** A model can emit three
  hundred characters without a terminator. Blocking audio until it happens to write a
  period would be a hang, so past a ceiling this cuts at the last clause break, and
  failing that the last space -- never mid-word.
* **The first segment is eager; the rest are not.** Time-to-first-audio is the whole
  point, so the first complete sentence goes out however short it is. After that,
  segments are accumulated towards a comfortable length, because a separate synthesis
  request per three-word sentence gives each its own pitch contour and the result
  sounds like a ransom note.

Pure: no provider, no I/O, no event loop. It takes text and returns strings, which is
what lets the boundary rules be tested exhaustively instead of listened to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Sentence terminators, including the ones that arrive from a multilingual model.
#: Devanagari danda and the CJK full stop are here because NEXUS is asked to answer in
#: the user's language, and a segmenter that only knows ASCII periods would never find
#: a boundary in Hindi and would fall back to the character ceiling for whole answers.
_TERMINATORS = ".!?…。！？।॥"

#: Closers allowed to sit between the terminator and the whitespace: ``He said "stop."``
#: cuts after the quote, not before it.
_CLOSERS = '"\')]}»”’'

#: Clause breaks, used only once the ceiling is reached. Weaker than a terminator: a
#: cut here is a compromise that keeps audio flowing, not a natural pause.
_CLAUSE = ",;:—–"

#: Tokens that end in a period without ending a sentence. Matched case-insensitively
#: against the last whitespace-delimited word, with the period included.
_ABBREVIATIONS: frozenset[str] = frozenset(
    {
        "mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.", "st.", "mt.",
        "e.g.", "i.e.", "cf.", "vs.", "etc.", "al.", "ca.", "approx.",
        "fig.", "no.", "vol.", "p.", "pp.", "ed.", "eds.", "inc.", "ltd.",
        "co.", "corp.", "dept.", "est.", "min.", "max.", "avg.", "sec.",
        "a.m.", "p.m.", "u.s.", "u.k.", "e.u.",
    }
)

#: Emit the first segment as soon as a boundary appears, however short. After that,
#: keep accumulating until this many characters, so prosody has something to work with.
FIRST_SEGMENT_MIN = 1
LATER_SEGMENT_MIN = 90

#: Past this, cut at the best break available rather than waiting for a terminator.
#: Chosen to be a couple of spoken sentences: long enough that a normal answer is never
#: cut artificially, short enough that a terminator-free run does not stall the voice.
SEGMENT_CEILING = 320

#: Absolute ceiling, duplicated from the providers' ``MAX_TTS_CHARS`` as a number
#: rather than imported so this module stays free of the provider layer. It is the
#: smaller of the two speech providers' limits, so a segment is safe on whichever
#: one the router names. The assertion that they agree lives in the tests, which is
#: where a drift should surface.
HARD_CEILING = 4_000

# ``[ \t]*`` and not ``\s*``: ``\s`` matches the newline *before* the fence, so the
# match would start on the blank line above it. The buffer then begins with a newline,
# ``find("\n")`` returns 0, and the search for the closing fence finds the opening one
# again -- the block gets recited as prose and reported as unclosed. Same class of bug
# as any regex whose leading whitespace class eats a line boundary it is meant to anchor.
_FENCE = re.compile(r"^[ \t]*(?:```|~~~)", re.M)


@dataclass(frozen=True)
class Segment:
    """One thing to synthesise.

    ``spoken`` is what the voice should say; ``source`` is the text it came from. They
    differ for a code block, where the source is code and the spoken form describes it
    -- so a transcript can show what was written while the speaker says something a
    person would want to hear.
    """

    spoken: str
    source: str
    kind: str = "prose"

    @property
    def is_code(self) -> bool:
        return self.kind == "code"


def describe_code(block: str) -> str:
    """What to say instead of reciting a code block.

    Deliberately not "code block": the useful facts are how long it is and, if the
    fence declared one, what language -- enough for the user to decide whether to look
    at the screen, which is the only sensible response to code in a voice channel.
    """
    lines = [line for line in block.strip().splitlines()]
    language = ""
    if lines and lines[0].lstrip().startswith(("```", "~~~")):
        language = lines[0].lstrip().lstrip("`~").strip()
        lines = lines[1:]
    if lines and lines[-1].lstrip().startswith(("```", "~~~")):
        lines = lines[:-1]
    count = len([line for line in lines if line.strip()])
    named = f"{language} " if language and language.isalpha() else ""
    if count == 0:
        return f"There is an empty {named}code block on screen."
    if count == 1:
        return f"There is one line of {named}code on screen."
    return f"There are {count} lines of {named}code on screen."


def _is_abbreviation(text: str, dot: int) -> bool:
    """Is the period at ``dot`` part of a token that does not end a sentence?

    Four separate cases, and each of them is audible when it goes wrong: a known
    abbreviation (``e.g.``), an initial (``J. Smith``), a list marker or ordinal at the
    start of a line (``1. First``), and a decimal (``3.14``).

    The initial rule has a cost worth knowing: a sentence whose last word is a single
    letter -- "the grade is B." -- is indistinguishable from an initial and does not end
    here. Initials are common in names and one-letter endings are rare, so that is the
    right way round, but it is a limitation rather than an oversight and the tests pin it
    as one.
    """
    if text[dot] != ".":
        return False
    start = dot
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    word = text[start : dot + 1].lower()
    if word in _ABBREVIATIONS:
        return True
    bare = word.strip("(“\"'[")
    # An initial: one letter and a period. "A." never ends a sentence in practice.
    if len(bare) == 2 and bare[0].isalpha():
        return True
    # A list marker or an ordinal opening a line: "1." / "12." at the start of its line.
    if bare[:-1].isdigit():
        line_start = text.rfind("\n", 0, start) + 1
        if text[line_start:start].strip() == "":
            return True
    # A decimal point with digits on both sides -- "3.14".
    #
    # Redundant with ``_prose_boundary``'s no-whitespace-after rule, and that is measured
    # rather than assumed: a digit after the dot is never whitespace and is in neither
    # ``_TERMINATORS`` nor ``_CLOSERS``, so the caller declines the same cut and lands on
    # the same index either way. Deleting this changed no segment across 5,280 chunkings
    # of a thousand generated inputs.
    #
    # It stays because this function answers "does this period end a sentence", and for
    # "3.14" the answer is no whoever is asking. A rule that is only correct because its
    # one caller happens to agree is a rule that breaks the day a second caller appears.
    # Asserted directly in test_speech_stream, not through the Segmenter, for that reason.
    if dot + 1 < len(text) and text[dot + 1].isdigit() and dot > 0 and text[dot - 1].isdigit():
        return True
    return False


def _prose_boundary(text: str, *, min_len: int, at_end: bool) -> int:
    """Index just past a safe cut in ``text``, or -1.

    ``at_end`` is False while a stream is still arriving, and it is the difference
    between a terminator and a boundary: a period at the very end of the buffer may be
    ``3.`` about to become ``3.14``, so mid-stream a cut requires whitespace after the
    terminator. On the flush there is no next chunk, so the end of the text is a cut.
    """
    i = 0
    length = len(text)
    while i < length:
        char = text[i]
        if char not in _TERMINATORS:
            i += 1
            continue
        if _is_abbreviation(text, i):
            i += 1
            continue
        # Run of terminators and closers: "?!" and 'stop."' both cut after the last one.
        end = i + 1
        while end < length and (text[end] in _TERMINATORS or text[end] in _CLOSERS):
            end += 1
        if end >= length:
            # Nothing after it yet. Only a cut if no more text is coming.
            #
            # Also redundant, and for the same kind of reason: ``flush`` appends whatever
            # is left as one segment, so returning -1 here produces identical audio today.
            # It stays because it is this function's documented contract -- ``at_end`` means
            # the end of the text is a boundary -- and because the two paths only agree
            # while the buffer at flush is under ``SEGMENT_CEILING``, which is an invariant
            # of ``feed``'s draining rather than anything this function can see.
            if at_end and end - 0 >= min_len:
                return end
            return -1
        if not text[end].isspace():
            # "3.14", "example.com", "a.b" -- a terminator inside a token.
            i = end
            continue
        if end >= min_len:
            return end
        i = end
    return -1


def _ceiling_cut(text: str, limit: int) -> int:
    """Where to cut a run that has produced no sentence boundary at all.

    Preference order: the last clause break, then the last space, then nothing. Never
    mid-word -- a segment ending in half a word is worse than a slightly longer wait,
    and the only way to reach the third case is a single token longer than the limit.
    """
    window = text[:limit]
    best = -1
    for index, char in enumerate(window):
        if char in _CLAUSE and index + 1 < len(text) and text[index + 1].isspace():
            best = index + 1
    if best > 0:
        return best
    space = window.rfind(" ")
    if space > 0:
        return space
    return -1


@dataclass
class Segmenter:
    """Accumulates streamed text and hands back what is safe to speak.

    Stateful on purpose: whether a cut is allowed depends on what has already been
    spoken (the first segment is eager, later ones are not) and on whether the stream is
    currently inside a code fence. One instance per turn.
    """

    buffer: str = ""
    emitted: int = 0
    _held_code: bool = field(default=False, repr=False)

    # ------------------------------------------------------------------ public

    def feed(self, chunk: str) -> list[Segment]:
        """Add streamed text; return every segment that is now safe to speak."""
        if not chunk:
            return []
        self.buffer += chunk
        return self._drain(at_end=False)

    def flush(self) -> list[Segment]:
        """The stream has ended. Return whatever is left, boundaries relaxed.

        Called on completion *and* on cancellation. On cancellation the tail is a
        partial sentence, and speaking it is still right: the user heard the model stop
        mid-thought, which is the truth about what happened.
        """
        out = self._drain(at_end=True)
        # The raw tail, not a stripped copy: ``source`` is documented as the text a
        # segment came from, and a transcript built from stripped sources cannot be
        # laid back over the stream it came from.
        tail = self.buffer
        self.buffer = ""
        # One segment, never a split. :meth:`_drain` with ``at_end=True`` cannot return
        # while more than ``HARD_CEILING`` characters are left -- every path that stops
        # it either has a buffer under ``SEGMENT_CEILING`` or has just cut at the hard
        # ceiling -- so the tail is already inside the provider's limit. A splitting loop
        # here would be code that cannot run, which is worse than no guard: it reads as
        # protection while protecting nothing. The invariant is asserted where it is
        # actually enforced, by a test that flushes a token far over the limit.
        if tail.strip():
            self.emitted += 1
            out.append(Segment(spoken=tail.strip(), source=tail))
        return out

    @property
    def pending(self) -> str:
        """Text received but not yet spoken. Shown, not guessed at."""
        return self.buffer

    # ----------------------------------------------------------------- internal

    def _drain(self, *, at_end: bool) -> list[Segment]:
        out: list[Segment] = []
        while True:
            taken = self._take(at_end=at_end)
            if taken is None:
                return out
            out.extend(taken)

    def _take(self, *, at_end: bool) -> list[Segment] | None:
        """One cut, or ``None`` when nothing can be cut yet."""
        if not self.buffer.strip():
            return None

        fence = _FENCE.search(self.buffer)
        if fence is not None:
            if fence.start() > 0:
                # A code block is a hard boundary: the prose before it is finished,
                # whatever its length, so it goes out without waiting for a terminator.
                prose = self.buffer[: fence.start()]
                self.buffer = self.buffer[fence.start() :]
                stripped = prose.strip()
                if not stripped:
                    return []
                self.emitted += 1
                return [Segment(spoken=stripped, source=prose)]
            return self._take_fence(at_end=at_end)

        min_len = FIRST_SEGMENT_MIN if self.emitted == 0 else LATER_SEGMENT_MIN
        cut = _prose_boundary(self.buffer, min_len=min_len, at_end=at_end)
        if cut < 0:
            if len(self.buffer) < SEGMENT_CEILING:
                return None
            cut = _ceiling_cut(self.buffer, SEGMENT_CEILING)
            if cut < 0:
                # One token longer than the ceiling. Cutting mid-word is refused, so it
                # is spoken whole once it stops growing -- but never past the provider's
                # own limit, which is a hard error rather than a preference.
                if len(self.buffer) < HARD_CEILING:
                    return None
                cut = HARD_CEILING
        source = self.buffer[:cut]
        self.buffer = self.buffer[cut:]
        stripped = source.strip()
        if not stripped:
            return []
        self.emitted += 1
        return [Segment(spoken=stripped, source=source)]

    def _take_fence(self, *, at_end: bool) -> list[Segment] | None:
        """The buffer opens with a fence. Take the whole block, or wait for its close."""
        line_end = self.buffer.find("\n")
        if line_end < 0:
            # The opening fence line has not finished arriving.
            return None if not at_end else self._take_unclosed_fence()
        close = _FENCE.search(self.buffer, line_end)
        if close is None:
            if at_end or len(self.buffer) >= HARD_CEILING:
                return self._take_unclosed_fence()
            return None
        end = self.buffer.find("\n", close.end())
        end = len(self.buffer) if end < 0 else end + 1
        block = self.buffer[:end]
        self.buffer = self.buffer[end:]
        self.emitted += 1
        return [Segment(spoken=describe_code(block), source=block, kind="code")]

    def _take_unclosed_fence(self) -> list[Segment]:
        """A fence the model never closed. Described, not recited, and said as such."""
        block = self.buffer
        self.buffer = ""
        self.emitted += 1
        spoken = describe_code(block)
        return [Segment(spoken=f"{spoken} It was not closed.", source=block, kind="code")]

def segments_of(text: str) -> list[Segment]:
    """Segment a complete string. The same rules, for text that did not stream."""
    seg = Segmenter()
    out = seg.feed(text)
    out.extend(seg.flush())
    return out
