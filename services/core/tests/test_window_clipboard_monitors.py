"""The window and clipboard monitors, with the programs they shell out to stubbed.

These two monitors are the pair that reads *content* -- window titles and whatever
is on the clipboard -- which makes them the two where a bug is a privacy bug
rather than a missed notification. Neither had a test.

Nothing here needs a desktop. Both monitors funnel every subprocess through one
module-level helper (``clipboard_monitor._read_clipboard``,
``window_monitor._run``), so a test replaces that helper and drives the real
``sample()``, the real classifier, the real history buffer and the real debouncer.
The fixtures are not invented: the xprop and xdotool output below is what this
machine printed, including the part that looks like a bug and is not --
``xdotool getwindowclassname`` does not exist on this xdotool, so the app name has
to come from ``/proc``.

The assertion this file exists for is the §56 one: a credential on the clipboard
must be *counted*, never *kept*. A monitor that stores nothing would pass that
trivially, so every withholding test here is paired with a control proving
ordinary text does reach ``data["content"]`` -- otherwise the no-leak assertion is
measuring an empty code path.

The last two tests check the fixtures against the live system where there is one,
and skip where there is not.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import shutil
import time
from typing import Any, Callable

import pytest

from nexus.db.repos import app_settings
from nexus.monitors import supervisor
from nexus.monitors import clipboard_monitor, window_monitor
from nexus.monitors.base import Debouncer, MonitorUnavailable
from nexus.monitors.clipboard_monitor import (
    HISTORY_SIZE,
    MAX_KEPT_CHARS,
    _READERS,
    ClipboardMonitor,
    looks_like_credential,
)
from nexus.monitors.window_monitor import (
    MIN_SESSION_SECONDS,
    WindowMonitor,
    _via_xdotool,
    _via_xprop,
)


def sync(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


# ===========================================================================
# (a) looks_like_credential -- a pure function, tested as one
# ===========================================================================
#
# It returns a *category string*, not a bool, and the empty string means "not a
# credential". Both directions are bugs: a miss writes a password into the
# database (§81), and an over-match turns a user's ordinary note into
# "something that looks like a password" with the content thrown away. The
# function's docstring says it is deliberately biased towards the second, which
# is a reason to pin the first-order false positives down, not to stop checking
# them.


#: One real-looking example per named pattern, with the category each must produce.
#: Values are structurally valid and semantically fake.
CREDENTIAL_SHAPES: tuple[tuple[str, str], ...] = (
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaA\n", "a private key"),
    ("-----BEGIN CERTIFICATE-----\nMIIDdzCCAl8\n", "a certificate"),
    ("AKIAIOSFODNN7EXAMPLE", "an AWS access key"),
    ("ASIAIOSFODNN7EXAMPLE", "an AWS access key"),
    ("ghp_aB3dE5fG7hI9jK1lM3nO", "a GitHub token"),
    ("sk-live-3xampl3K3yD0N0tL0g1t", "an OpenAI-style API key"),
    ("AIzaSyA7bC9dE1fG3hI5jK7lM9nO1pQ3rS5tU7v", "a Google API key"),
    ("xoxb-1234567890-0987654321-aBcDeF", "a Slack token"),
    ("nvapi-7bC9dE1fG3hI5jK7lM9nO1pQ3rS5tU7vW", "an NVIDIA API key"),
    ("aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW", "a 32-character API key"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP",
     "a JSON web token"),
    ("postgres://nexus:hunter2@db.internal:5432/app",
     "a connection string with a password"),
    ("PASSWORD=hunter2-s3cr3t", "an environment variable holding a secret"),
    ("api_key: 7bC9dE1fG3hI5jK", "an environment variable holding a secret"),
)


#: Things a person copies all day that must stay copyable. Every one of these
#: coming back non-empty means the user's notes, paths and code get silently
#: emptied out of their own clipboard history.
ORDINARY_CONTENT: tuple[str, ...] = (
    "The quick brown fox jumps over the lazy dog.",
    "Meeting moved to 3pm in the small conference room on the second floor.",
    "Remember to buy milk, eggs and bread on the way home tonight.",
    "task-force",
    "The task-force met on Tuesday to review the budget.",
    "SELECT id, name FROM users WHERE created_at > now() - interval '7 days';",
    "def add(a, b):\n    return a + b\n",
    "https://example.com/some/very/long/path/that/keeps/going/onwards",
    "/home/santosh/EDITH/services/core/nexus/monitors/clipboard_monitor.py",
    "~/Documents/notes/the-one-about-the-thing.md",
    "TOKEN",
    "deadbeefdeadbeefdeadbeefdeadbeef12345678",
)


@pytest.mark.parametrize("text,category", CREDENTIAL_SHAPES)
def test_each_named_credential_shape_is_recognised_as_itself(text: str, category: str):
    """One example per pattern, and the category has to be the right one.

    Asserting only "non-empty" would pass with every pattern collapsed into one,
    and the category is what the user reads in place of their content -- being
    told "a certificate was copied" when it was an AWS key is a wrong answer, not
    a vague one. Written as a table so a pattern added without an example here is
    visible as a pattern with no test.
    """
    assert looks_like_credential(text) == category


def test_every_pattern_in_the_module_has_an_example_in_this_file():
    """A pattern nobody wrote an example for is a pattern nobody tested.

    The table above is only as good as its coverage of ``_CREDENTIAL_PATTERNS``,
    and the natural way to add a shape is to add the regex and move on. This is
    the check that notices.
    """
    declared = {category for category, _pattern in clipboard_monitor._CREDENTIAL_PATTERNS}
    covered = {category for _text, category in CREDENTIAL_SHAPES}
    assert declared == covered, (
        f"patterns with no example: {sorted(declared - covered)}; "
        f"examples for patterns that no longer exist: {sorted(covered - declared)}"
    )


def test_a_generated_password_matching_no_pattern_is_caught_by_entropy():
    """The password-manager case: a secret that is not any vendor's key format.

    Every pattern above keys off a prefix. A password generated by Bitwarden has
    no prefix, so the entropy branch is the only thing between it and the
    database -- and it is the branch most likely to be deleted as a false-positive
    machine.
    """
    assert looks_like_credential("Tr0ub4dor&3-correct-horse-battery") == (
        "something that looks like a password or token"
    )
    assert looks_like_credential("CorrectHorseBatteryStaple2026!") == (
        "something that looks like a password or token"
    )


@pytest.mark.parametrize("text", ORDINARY_CONTENT)
def test_ordinary_content_is_not_mistaken_for_a_credential(text: str):
    """Over-matching empties the user's clipboard history into category labels.

    This is the cost side of "deliberately biased towards false positives": the
    bias is acceptable for a git SHA, not for a sentence. ``TOKEN`` on its own is
    in here because the environment-variable pattern needs an actual assignment --
    the word alone is a word.
    """
    assert looks_like_credential(text) == "", (
        f"{text[:48]!r} is ordinary content and would be withheld as a secret"
    )


def test_the_word_boundary_in_the_sk_pattern_is_what_saves_hyphenated_prose():
    """``\\bsk-`` versus ``sk-``: an English phrase contains an OpenAI key shape.

    "task-force-infrastructure-review" has ``sk-`` inside it at ``ta|sk-``, with
    twenty-five more word characters after it -- exactly what
    ``sk-[A-Za-z0-9_\\-]{20,}`` matches. The leading ``\\b`` is the only thing that
    refuses it, and this test holds both halves so that dropping the anchor fails
    here instead of quietly relabelling a sentence as an API key.
    """
    sentence = "Reassigned the task-force-infrastructure-review item to Priya this morning."
    assert looks_like_credential(sentence) == ""

    unanchored = clipboard_monitor.re.compile(r"sk-[A-Za-z0-9_\-]{20,}\b")
    assert unanchored.search(sentence) is not None, (
        "the premise of this test is that the pattern without \\b matches this "
        "sentence; if it no longer does, the test proves nothing about the anchor"
    )
    anchored = clipboard_monitor.re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")
    assert anchored.search(sentence) is None


def test_a_credential_glued_into_a_longer_word_is_not_claimed_as_that_vendors_key():
    """The other edge of the same anchor, so its cost is recorded too.

    ``\\b`` means a key with a letter stuck to its front is not reported as an AWS
    key. That is the deliberate trade: the anchor is kept for the prose above and
    paid for here. ``XAKIAIOSFODNN7EXAMPLEX`` is low-entropy enough to miss the
    fallback as well, so it is withheld by nothing -- which is what the anchor
    costs, written down rather than discovered later.
    """
    assert looks_like_credential("XAKIAIOSFODNN7EXAMPLEX") == ""


def test_a_git_sha_stays_below_the_entropy_floor():
    """Forty hex characters is the single most-copied non-secret on a dev machine.

    Hex has about 3.1 bits per character against a 3.5 floor, which is the whole
    reason the floor is where it is. Lower it and every commit a user copies
    becomes "something that looks like a password".
    """
    sha = "deadbeefdeadbeefdeadbeefdeadbeef12345678"
    assert looks_like_credential(sha) == ""
    assert clipboard_monitor._entropy(sha) < clipboard_monitor._ENTROPY_BITS_PER_CHAR


def test_a_token_shorter_than_the_minimum_is_not_a_credential():
    """Below 24 characters the entropy test cannot tell a word from a key.

    ``Tr0ub4dor&3`` is a password and this returns "" for it. That is the
    documented floor, not an accident, and pinning it means a change to
    ``_ENTROPY_MIN_LENGTH`` is a decision somebody makes on purpose.
    """
    assert len("Tr0ub4dor&3") < clipboard_monitor._ENTROPY_MIN_LENGTH
    assert looks_like_credential("Tr0ub4dor&3") == ""


def test_a_long_url_or_path_is_excluded_from_the_entropy_branch():
    """A URL is a single unbroken high-entropy token, and it is not a secret.

    Without the prefix exclusions, copying a link -- the most common clipboard
    operation there is -- would report "a password was copied" and throw the link
    away. The trailing-dot rule is the same guard for filenames.
    """
    assert looks_like_credential("https://example.com/articles/2026/the-long-one") == ""
    assert looks_like_credential("/var/log/nexus/monitors/clipboard/history.log") == ""
    assert looks_like_credential("~/projects/edith/services/core/pyproject.toml") == ""
    assert looks_like_credential("aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW.pem") == ""


def test_nothing_copied_is_not_a_credential():
    """``sample()`` calls this before the empty-content check in some orders."""
    assert looks_like_credential("") == ""
    assert looks_like_credential("   \n\t  ") == ""


def test_the_returned_category_never_contains_any_of_the_input():
    """The category is a label. If it quotes the secret, §56 is broken at the root.

    Every caller treats the return value as safe to put in a summary, a log line
    and an observation, so a pattern changed to interpolate what it matched would
    leak everywhere at once. Checked against every shape in the table, in windows
    of six characters so a partial echo is caught too.
    """
    for text, _category in CREDENTIAL_SHAPES:
        category = looks_like_credential(text)
        assert category
        secret = text.strip()
        for start in range(len(secret) - 5):
            window = secret[start:start + 6]
            if window.isspace():
                continue
            assert window not in category, (
                f"the category {category!r} echoes {window!r} from the input"
            )


# ===========================================================================
# (b) ClipboardMonitor, through the real sample() with the reader stubbed
# ===========================================================================


class FakeClipboard:
    """Stands in for ``_read_clipboard``: the same ``(text, error)`` contract.

    ``text=None`` with an error is how the real helper reports a reader that
    failed, which the monitor must treat differently from an empty clipboard.
    """

    def __init__(self, text: str | None = "", error: str = "") -> None:
        self.text = text
        self.error = error
        self.reads = 0

    def __call__(self, argv: list[str]) -> tuple[str | None, str]:
        self.reads += 1
        return self.text, self.error

    def copy(self, text: str) -> None:
        self.text, self.error = text, ""

    def fail(self, detail: str) -> None:
        self.text, self.error = None, detail


@pytest.fixture
def clipboard(monkeypatch: pytest.MonkeyPatch) -> FakeClipboard:
    """Replace the one module-level helper every clipboard read goes through."""
    fake = FakeClipboard()
    monkeypatch.setattr(clipboard_monitor, "_read_clipboard", fake)
    return fake


@pytest.fixture
def clip_monitor(clipboard: FakeClipboard, monkeypatch: pytest.MonkeyPatch) -> ClipboardMonitor:
    """A monitor that has resolved a reader, without caring what is installed.

    ``check_available`` is what sets ``_binary`` and ``_argv``, and the error
    message in ``sample`` names ``_binary``, so going through it rather than
    poking the attributes keeps that path real.
    """
    monkeypatch.setattr(clipboard_monitor.shutil, "which",
                        lambda name: "/usr/bin/xclip" if name == "xclip" else None)
    monitor = ClipboardMonitor()
    monitor.check_available()
    assert monitor._binary == "xclip"
    return monitor


#: Structurally a real OpenAI-style key, semantically nonsense. Used as the
#: needle: this exact string must not appear anywhere downstream.
SECRET = "sk-live-3xampl3K3yD0N0tL0g1tEver0987654321"


def leaks(blob: str, secret: str, window: int = 10) -> str:
    """Any ``window``-character run of ``secret`` present in ``blob``, else "".

    Searching for the whole secret would pass against truncated content, which is
    exactly the mistake ``_entry`` documents ("the first eight characters of a
    password are still eight characters of a password").
    """
    for start in range(len(secret) - window + 1):
        piece = secret[start:start + window]
        if piece in blob:
            return piece
    return ""


@sync
async def test_a_credential_is_counted_and_not_stored(clip_monitor: ClipboardMonitor):
    """The §56 boundary, through the real sample path.

    The observation must carry the shape -- category, length, ``stored: False`` --
    and no part of the value, in no field. Serialising the whole observation and
    hunting for any ten-character run of the secret is the only check that covers
    the summary, the dedupe key and every data field at once, including ones added
    later.
    """
    clip_monitor._history.clear()
    clipboard = clipboard_monitor._read_clipboard
    clipboard.copy(SECRET)

    observations = await clip_monitor.sample()
    assert len(observations) == 1
    event = observations[0]

    assert event.kind == "clipboard.credential"
    assert event.severity == "notice"
    assert event.data["stored"] is False
    assert event.data["category"] == "an OpenAI-style API key"
    assert event.data["length"] == len(SECRET)
    assert "content" not in event.data
    assert "preview" not in event.data

    blob = json.dumps(event.to_dict())
    assert leaks(blob, SECRET) == "", (
        f"the observation carries {leaks(blob, SECRET)!r} from the secret"
    )
    assert clip_monitor.snapshot()["credentials_skipped"] >= 1


@sync
async def test_ordinary_text_does_reach_the_observation(clip_monitor: ClipboardMonitor):
    """The control for the test above, and it is not optional.

    If ``sample()`` never put content in ``data`` at all -- a refactor, a bug, a
    field renamed -- the no-leak assertion would pass on an empty code path and
    keep passing forever. This proves the channel the secret must not travel
    through is open and carrying things.
    """
    clip_monitor._history.clear()
    note = "Remember to renew the domain before the fourteenth."
    clipboard_monitor._read_clipboard.copy(note)

    observations = await clip_monitor.sample()
    assert len(observations) == 1
    event = observations[0]
    assert event.kind == "clipboard.copy"
    assert event.data["kind"] == "text"
    assert event.data["content"] == note, (
        "ordinary content must be stored, or the credential test proves nothing"
    )
    assert note in json.dumps(event.to_dict())


@sync
async def test_the_withheld_credential_is_absent_even_when_content_is_asked_for(
    clip_monitor: ClipboardMonitor,
):
    """``history(include_content=True)`` is the one call that exists to return text.

    Withholding at the observation and keeping the value in the buffer behind it
    would be a boundary that holds for one reader and not for the tool that asks
    properly. ``latest()`` defaults to ``include_content=True``, so it is the most
    likely accidental path to a secret and gets checked explicitly.
    """
    clip_monitor._history.clear()
    clipboard_monitor._read_clipboard.copy(SECRET)
    await clip_monitor.sample()

    stored = clip_monitor.history(include_content=True)
    assert len(stored) == 1
    assert stored[0]["kind"] == "credential"
    assert stored[0]["content"] == ""
    assert stored[0]["preview"] == ""
    assert stored[0]["withheld_reason"] == "an OpenAI-style API key"
    assert stored[0]["length"] == len(SECRET)

    latest = clip_monitor.latest()
    assert latest is not None and latest["content"] == ""

    blob = json.dumps([stored, latest, clip_monitor.snapshot()])
    assert leaks(blob, SECRET) == "", (
        f"the history buffer carries {leaks(blob, SECRET)!r} from the secret"
    )


@sync
async def test_a_credential_inside_a_larger_paste_withholds_the_whole_paste(
    clip_monitor: ClipboardMonitor,
):
    """A config file with one secret line in it is the realistic copy.

    Any single match withholds everything, which loses the surrounding text --
    the right direction to fail. Keeping "the parts that are not secret" would
    mean a redactor, and a redactor that is wrong once has published a key.
    """
    clip_monitor._history.clear()
    paste = (
        "# staging\n"
        "HOST=db.internal\n"
        f"DATABASE_PASSWORD={SECRET}\n"
        "PORT=5432\n"
    )
    clipboard_monitor._read_clipboard.copy(paste)

    event = (await clip_monitor.sample())[0]
    assert event.kind == "clipboard.credential"
    assert leaks(json.dumps(event.to_dict()), SECRET) == ""
    # Not even the innocent lines, because deciding which they are is the bug.
    assert "db.internal" not in json.dumps(event.to_dict())


@sync
async def test_the_same_clipboard_content_is_not_reported_twice(
    clip_monitor: ClipboardMonitor,
):
    """A three-second poll over an unchanged clipboard is the normal case.

    Without the hash compare this monitor reports the same copy twenty times a
    minute, and the clipboard history fills with one entry repeated. The reader
    is still called every tick -- suppression happens after the read, not instead
    of it -- so the read count is checked too.
    """
    clipboard = clipboard_monitor._read_clipboard
    clip_monitor._history.clear()
    clipboard.copy("https://example.com/the-one-link")

    assert len(await clip_monitor.sample()) == 1
    assert await clip_monitor.sample() == []
    assert await clip_monitor.sample() == []
    assert clipboard.reads == 3
    assert len(clip_monitor._history) == 1


@sync
async def test_copying_something_else_and_coming_back_is_two_more_copies(
    clip_monitor: ClipboardMonitor,
):
    """Dedupe is against the *previous* read, not against everything ever seen.

    Copying A, then B, then A again is three deliberate acts, and a user asking
    "what did I copy" wants the last one to be A. A monitor remembering every
    hash would report the second A as a repeat and leave the history saying B.
    """
    clipboard = clipboard_monitor._read_clipboard
    clip_monitor._history.clear()

    clipboard.copy("alpha one two three")
    assert len(await clip_monitor.sample()) == 1
    clipboard.copy("bravo four five six")
    assert len(await clip_monitor.sample()) == 1
    clipboard.copy("alpha one two three")
    assert len(await clip_monitor.sample()) == 1

    assert clip_monitor.latest()["content"] == "alpha one two three"


@sync
async def test_an_empty_clipboard_is_not_a_copy(clip_monitor: ClipboardMonitor):
    """Clearing the clipboard is not something the user copied.

    Some readers report an empty selection as a successful read of "" and others
    as whitespace. Either arriving as a ``clipboard.copy`` observation would put
    blank entries in the history every time a program cleared the buffer.
    """
    clipboard = clipboard_monitor._read_clipboard
    clip_monitor._history.clear()

    clipboard.copy("")
    assert await clip_monitor.sample() == []
    clipboard.copy("   \n\t ")
    assert await clip_monitor.sample() == []
    assert len(clip_monitor._history) == 0


@sync
async def test_oversized_content_keeps_the_first_line_and_drops_the_body(
    clip_monitor: ClipboardMonitor,
):
    """A four-megabyte CSV paste is a memory leak wearing a feature's clothes.

    The cap has to drop the *content* while keeping enough to recognise the entry,
    and it has to say so -- an entry with no content and no reason reads as a bug
    in the monitor rather than a deliberate limit.
    """
    clip_monitor._history.clear()
    huge = "First line of a very long note\n" + ("lorem ipsum dolor sit amet " * 400)
    assert len(huge) > MAX_KEPT_CHARS
    clipboard_monitor._read_clipboard.copy(huge)

    event = (await clip_monitor.sample())[0]
    assert event.kind == "clipboard.copy"
    assert event.data["content"] == ""
    assert event.data["preview"].startswith("First line of a very long note")
    assert str(MAX_KEPT_CHARS) in event.data["withheld_reason"].replace(",", "")
    assert event.data["length"] == len(huge)


@sync
async def test_a_reader_that_fails_raises_rather_than_looking_like_an_empty_clipboard(
    clip_monitor: ClipboardMonitor,
):
    """A broken reader must be an error, not silence.

    ``_read_clipboard`` returns ``None`` for a failure and ``""`` for an empty
    clipboard, and collapsing the two would make a dead ``xclip`` indistinguishable
    from a user who copied nothing -- the supervisor would never see an error, so
    the monitor would run forever reporting nothing. The message names the binary
    because that is what the user has to go and fix.
    """
    clipboard_monitor._read_clipboard.fail("Error: Can't open display: (null)")
    with pytest.raises(RuntimeError) as raised:
        await clip_monitor.sample()
    assert "xclip" in str(raised.value)
    assert "Can't open display" in str(raised.value)


@sync
async def test_priming_at_start_means_the_existing_clipboard_is_not_a_fresh_copy(
    clip_monitor: ClipboardMonitor,
):
    """Whatever was on the clipboard before watching began was not just copied.

    Without ``on_start`` priming, switching this monitor on reports the contents
    of the buffer as a copy that happened now -- and if that happens to be a
    password, NEXUS announces a credential the user did not touch.
    """
    clip_monitor._history.clear()
    clipboard_monitor._read_clipboard.copy("something from before")

    await clip_monitor.on_start()
    assert await clip_monitor.sample() == []
    assert len(clip_monitor._history) == 0

    clipboard_monitor._read_clipboard.copy("something copied now")
    assert len(await clip_monitor.sample()) == 1


@sync
async def test_the_history_buffer_is_bounded(clip_monitor: ClipboardMonitor):
    """An unbounded history is a process that grows for as long as it runs."""
    clip_monitor._history.clear()
    clipboard = clipboard_monitor._read_clipboard
    for index in range(HISTORY_SIZE + 10):
        clipboard.copy(f"entry number {index} of the test")
        await clip_monitor.sample()
    assert len(clip_monitor._history) == HISTORY_SIZE
    newest = clip_monitor.history(limit=5, include_content=True)[0]
    assert newest["content"].startswith(f"entry number {HISTORY_SIZE + 9}")


def test_no_clipboard_program_at_all_says_which_one_to_install(
    monkeypatch: pytest.MonkeyPatch,
):
    """On a machine with no clipboard tool this monitor cannot work, and must say so.

    ``MonitorUnavailable`` rather than an error per tick is what stops the
    supervisor retrying forever (§78), and the message is the only place the user
    learns that installing one package fixes it. A bare "unavailable" sends them
    to the logs.
    """
    monkeypatch.setattr(clipboard_monitor.shutil, "which", lambda name: None)
    with pytest.raises(MonitorUnavailable) as raised:
        ClipboardMonitor().check_available()
    message = str(raised.value)
    assert "xclip" in message
    assert "wl-clipboard" in message


def test_the_wayland_reader_is_preferred_when_it_exists(monkeypatch: pytest.MonkeyPatch):
    """``wl-paste`` first, because the X11 tools read nothing under Wayland.

    Both can be installed at once, and picking ``xclip`` on a Wayland session
    gives a monitor that is "available" and permanently sees an empty clipboard --
    the failure that looks like a user who never copies anything.
    """
    present = {"wl-paste", "xclip", "xsel"}
    monkeypatch.setattr(clipboard_monitor.shutil, "which",
                        lambda name: f"/usr/bin/{name}" if name in present else None)
    monitor = ClipboardMonitor()
    monitor.check_available()
    assert monitor._binary == "wl-paste"
    assert monitor._argv == ["wl-paste", "--no-newline"]
    assert _READERS[0][0] == "wl-paste", "the preference order is the thing being tested"

    present.discard("wl-paste")
    second = ClipboardMonitor()
    second.check_available()
    assert second._binary == "xclip"


# ===========================================================================
# (c) WindowMonitor, through the real sample() with the focus reader stubbed
# ===========================================================================


def focus(window_id: str, title: str, app: str = "firefox", pid: int | None = 4242):
    """One focus reading, in the shape both readers return."""
    return window_monitor._Focus(window_id=window_id, title=title, app=app, pid=pid)


class FakeFocus:
    """Stands in for the resolved ``_reader``: returns whatever the test last set."""

    def __init__(self) -> None:
        self.current = None
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return self.current

    def set(self, value) -> None:
        self.current = value


@pytest.fixture
def win() -> tuple[WindowMonitor, FakeFocus]:
    """A monitor with its reader already resolved to a stub.

    ``sample()`` calls ``self._reader()`` and nothing else touches the desktop, so
    this is the whole seam. ``check_available`` is tested separately against
    patched ``shutil.which`` -- going through it here would make every sampling
    test depend on this machine having xdotool and an X session.
    """
    monitor = WindowMonitor()
    reader = FakeFocus()
    monitor._reader = reader
    return monitor, reader


@sync
async def test_a_focus_change_is_reported_once_and_then_not_again(
    win: tuple[WindowMonitor, FakeFocus],
):
    """The first thing this monitor must get right, and the easiest to get wrong.

    A five-second poll over an unchanged desktop is the normal state of a
    machine. Reporting the focused window every tick would make the timeline one
    entry per poll and bury every real switch, which is this monitor's documented
    worst failure mode -- "not missing an event, reporting the same one sixty
    times".
    """
    monitor, reader = win
    reader.set(focus("0x4200004", "notes.md - Editor", app="Editor"))

    observations = await monitor.sample()
    assert len(observations) == 1
    assert observations[0].kind == "window.focus"
    assert observations[0].summary == "Focused Editor"
    assert observations[0].data["from"] is None
    assert observations[0].data["to"]["app"] == "Editor"

    assert await monitor.sample() == []
    assert await monitor.sample() == []
    assert reader.reads == 3, "suppression happens after the read, not instead of it"


@sync
async def test_switching_windows_reports_both_sides_with_a_real_duration(
    win: tuple[WindowMonitor, FakeFocus],
):
    """"Switched to X after 5m in Y" is the sentence the timeline is built from.

    The closed session's duration has to come from the clock rather than from the
    number of samples, or the answer to "how long was I in there" is a count of
    polls. Backdating ``started_monotonic`` is how a test moves five minutes
    without patching the clock the event loop also reads.
    """
    monitor, reader = win
    reader.set(focus("0x1", "notes.md", app="Editor"))
    await monitor.sample()
    monitor._current.started_monotonic = time.monotonic() - 300.0

    reader.set(focus("0x2", "Inbox", app="Thunderbird"))
    observations = await monitor.sample()

    assert len(observations) == 1
    event = observations[0]
    assert event.kind == "window.focus"
    assert event.summary == "Switched to Thunderbird after 5m in Editor"
    assert event.data["from"]["app"] == "Editor"
    assert event.data["to"]["app"] == "Thunderbird"
    assert 299 <= event.data["from"]["seconds"] <= 302
    assert event.dedupe_key == "focus:0x2"


@sync
async def test_the_same_window_with_a_new_title_is_a_title_change_not_a_new_session(
    win: tuple[WindowMonitor, FakeFocus],
):
    """Switching browser tabs must not look like switching applications.

    A browser keeps one window id across every tab. Treating a new title as a new
    focus session would end and restart the session on every tab change, so the
    usage total for the browser becomes a list of ninety-second fragments and the
    timeline is unreadable. The observation kind is what tells them apart
    downstream.
    """
    monitor, reader = win
    reader.set(focus("0x4200004", "Tab one - Chrome", app="Google-chrome"))
    await monitor.sample()
    started = monitor._current.started_monotonic

    reader.set(focus("0x4200004", "Tab two - Chrome", app="Google-chrome"))
    observations = await monitor.sample()

    assert len(observations) == 1
    event = observations[0]
    assert event.kind == "window.title"
    assert event.data["previous_title"] == "Tab one - Chrome"
    assert event.data["title"] == "Tab two - Chrome"
    assert monitor._current is not None
    assert monitor._current.started_monotonic == started, (
        "a tab change restarted the focus session, so the duration is now wrong"
    )
    assert len(monitor.timeline()) == 1


@sync
async def test_nothing_focused_ends_the_session_instead_of_accruing_time(
    win: tuple[WindowMonitor, FakeFocus],
):
    """A locked screen or an empty desktop is not time spent in the last app.

    ``_reader`` returning None is what a screen lock looks like. Leaving the
    session open would charge the whole lunch break to whatever was focused
    beforehand, which is the one number the usage report exists to give.
    """
    monitor, reader = win
    reader.set(focus("0x1", "notes.md", app="Editor"))
    await monitor.sample()
    monitor._current.started_monotonic = time.monotonic() - 120.0

    reader.set(None)
    observations = await monitor.sample()
    assert len(observations) == 1
    assert observations[0].summary == "Left Editor after 2m"
    assert observations[0].data["to"] is None
    assert observations[0].dedupe_key == "focus:none"
    assert monitor.current() is None

    # And nothing further, because there is no session left to close.
    assert await monitor.sample() == []


@sync
async def test_alt_tabbing_between_two_windows_is_two_conditions_not_twenty_events(
    win: tuple[WindowMonitor, FakeFocus],
):
    """Through the real debouncer, which is what decides whether the user hears this.

    The dedupe key names the window being switched *to*, so bouncing between an
    editor and a browser produces two keys however many times it happens. One
    shared key would suppress the second window entirely; a key including the
    moment would report every bounce.
    """
    monitor, reader = win
    debouncer = Debouncer(min_repeat_seconds=900)

    reader.set(focus("0x1", "notes.md", app="Editor"))
    assert [debouncer.should_report(o) for o in await monitor.sample()] == [True]
    reader.set(focus("0x2", "Inbox", app="Thunderbird"))
    assert [debouncer.should_report(o) for o in await monitor.sample()] == [True]
    reader.set(focus("0x1", "notes.md", app="Editor"))
    back = await monitor.sample()
    assert [o.dedupe_key for o in back] == ["focus:0x1"]
    assert [debouncer.should_report(o) for o in back] == [False], (
        "returning to a window already reported is the same situation, not news"
    )
    assert debouncer.active_keys() == ["focus:0x1", "focus:0x2"]


@sync
async def test_a_pass_through_window_is_counted_in_totals_but_not_in_the_timeline(
    win: tuple[WindowMonitor, FakeFocus],
):
    """Alt-tabbing *across* a window on the way somewhere else is not working in it.

    A timeline with a 0.3-second entry for every window between here and there is
    noise that hides the three sessions that mattered. The totals still count it,
    because time spent is time spent -- dropping it from both would make the usage
    numbers quietly not add up to the day.
    """
    monitor, reader = win
    reader.set(focus("0x1", "notes.md", app="Editor"))
    await monitor.sample()
    monitor._current.started_monotonic = time.monotonic() - 0.3
    assert 0.3 < MIN_SESSION_SECONDS

    reader.set(focus("0x2", "Inbox", app="Thunderbird"))
    await monitor.sample()

    assert [entry["app"] for entry in monitor.timeline()] == ["Thunderbird"], (
        "the pass-through window is in the timeline"
    )
    totals = {entry["app"]: entry["seconds"] for entry in monitor.usage()}
    assert "Editor" in totals and totals["Editor"] >= 0.2, (
        "the pass-through was dropped from the totals as well, so the day is short"
    )


@sync
async def test_stopping_closes_the_open_session(win: tuple[WindowMonitor, FakeFocus]):
    """Otherwise the last session of the day has no end and no duration.

    ``timeline()`` reports the open session with ``ongoing: True`` and a duration
    measured from now, so a monitor stopped without closing leaves an entry that
    keeps growing against a clock nobody is watching.
    """
    monitor, reader = win
    reader.set(focus("0x1", "notes.md", app="Editor"))
    await monitor.sample()
    monitor._current.started_monotonic = time.monotonic() - 60.0

    await monitor.on_stop()
    assert monitor.current() is None
    assert len(monitor.timeline()) == 1
    entry = monitor.timeline()[0]
    assert entry["ended_at"] is not None
    assert entry.get("ongoing") is not True
    assert 59 <= entry["seconds"] <= 62


def test_neither_xdotool_nor_xprop_installed_says_which_one_to_install(
    monkeypatch: pytest.MonkeyPatch,
):
    """Both readers are external programs, and a bare refusal is unactionable.

    This is the check that has to happen *before* the Wayland check, because a
    machine with neither program also has no DISPLAY half the time and would
    otherwise be told it is on Wayland -- sending the user to look at their
    compositor instead of installing a package.
    """
    monkeypatch.setattr(window_monitor.shutil, "which", lambda name: None)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")

    with pytest.raises(MonitorUnavailable) as raised:
        WindowMonitor().check_available()
    message = str(raised.value)
    assert "xdotool" in message
    assert "xprop" in message


def test_a_wayland_session_refuses_rather_than_reporting_an_empty_timeline(
    monkeypatch: pytest.MonkeyPatch,
):
    """xdotool installed under Wayland is the worst case: available and blind.

    It runs, exits zero, and reports no active window forever, so the timeline is
    an empty afternoon rather than an error -- a wrong answer presented as a fact
    (§78). Refusing at startup is the only way the user finds out.
    """
    monkeypatch.setattr(window_monitor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("DISPLAY", ":0")

    with pytest.raises(MonitorUnavailable) as raised:
        WindowMonitor().check_available()
    assert "Wayland" in str(raised.value)

    # And with no DISPLAY at all -- a service started outside a session.
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("DISPLAY", raising=False)
    with pytest.raises(MonitorUnavailable):
        WindowMonitor().check_available()


def test_xdotool_is_preferred_and_xprop_is_the_fallback(monkeypatch: pytest.MonkeyPatch):
    """Which reader got picked decides which parser runs, so it is worth pinning.

    One ``xdotool`` call per property beats four ``xprop`` invocations plus a
    regex over their output, and the fallback exists because xdotool is not
    installed by default anywhere.
    """
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")

    monkeypatch.setattr(window_monitor.shutil, "which",
                        lambda name: f"/usr/bin/{name}" if name == "xdotool" else None)
    both = WindowMonitor()
    both.check_available()
    assert both._reader is _via_xdotool

    monkeypatch.setattr(window_monitor.shutil, "which",
                        lambda name: f"/usr/bin/{name}" if name == "xprop" else None)
    only_xprop = WindowMonitor()
    only_xprop.check_available()
    assert only_xprop._reader is _via_xprop


# ------------------------------------------------- the readers, on recorded output

#: Verbatim from ``xprop -id 0x4200004 _NET_WM_NAME WM_NAME WM_CLASS _NET_WM_PID``
#: on this machine, emoji and all. The title is content, which is the point: it is
#: also the shape that breaks a naive parse.
XPROP_PROPS = (
    '_NET_WM_NAME(UTF8_STRING) = "(20) The impossible ice cream \U0001f62dm '
    '#magictrick #magic #ytshorts - YouTube - Google Chrome"\n'
    'WM_NAME(UTF8_STRING) = "(20) The impossible ice cream \U0001f62dm '
    '#magictrick #magic #ytshorts - YouTube - Google Chrome"\n'
    'WM_CLASS(STRING) = "google-chrome", "Google-chrome"\n'
    "_NET_WM_PID(CARDINAL) = 3321\n"
)
XPROP_ROOT = "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x4200004\n"


def fake_run(monkeypatch: pytest.MonkeyPatch, answers: dict[str, str | None]):
    """Replace ``window_monitor._run``, keyed by the first distinguishing argument.

    The real helper returns None for any non-zero exit, which is how a missing
    subcommand and a vanished window both arrive, so None is a first-class answer
    here rather than an error.
    """
    calls: list[list[str]] = []

    def run(command: list[str], timeout: float = 3.0) -> str | None:
        calls.append(command)
        for key, value in answers.items():
            if key in command:
                return value
        return None

    monkeypatch.setattr(window_monitor, "_run", run)
    return calls


def test_the_xprop_reader_parses_the_output_this_machine_produces(
    monkeypatch: pytest.MonkeyPatch,
):
    """Four properties out of one text blob, including a title full of punctuation.

    ``WM_CLASS`` is a pair -- instance then class -- and the class is the one a
    human recognises, so taking the first would label Chrome "google-chrome" and
    Firefox "Navigator". The title regex has to survive quotes, hashes and an
    emoji, because window titles are whatever the user happens to be looking at.
    """
    fake_run(monkeypatch, {"-root": XPROP_ROOT, "-id": XPROP_PROPS})

    found = _via_xprop()
    assert found is not None
    assert found.window_id == "0x4200004"
    assert found.app == "Google-chrome"
    assert found.pid == 3321
    assert found.title.startswith("(20) The impossible ice cream")
    assert found.title.endswith("YouTube - Google Chrome")


def test_an_unfocused_desktop_reads_as_no_window_rather_than_window_zero(
    monkeypatch: pytest.MonkeyPatch,
):
    """``0x0`` is what X reports with nothing focused, and it is not a window.

    Passing it on would create a focus session for a window that does not exist,
    with whatever title ``xprop -id 0x0`` prints, and the usage report would show
    time spent in "unknown".
    """
    fake_run(monkeypatch, {"-root": "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x0\n"})
    assert _via_xprop() is None

    # And a root query that failed outright is also nothing, not a crash.
    fake_run(monkeypatch, {})
    assert _via_xprop() is None


def test_xdotool_without_getwindowclassname_falls_back_to_the_process_name(
    monkeypatch: pytest.MonkeyPatch,
):
    """Measured on this machine: ``xdotool getwindowclassname`` does not exist here.

    It prints "Unknown command" and exits non-zero, so ``_run`` returns None and
    the app name is empty. Without the ``/proc`` fallback every window on this
    machine would be reported as "unknown", and the usage report would be one row
    saying the user spent the day in nothing.
    """
    fake_run(monkeypatch, {
        "getactivewindow": "69206020\n",
        "getwindowname": "notes.md - Editor\n",
        "getwindowclassname": None,          # the real behaviour here
        "getwindowpid": "3321\n",
    })
    monkeypatch.setattr(window_monitor, "_app_from_pid",
                        lambda pid: "chrome" if pid == 3321 else None)

    found = _via_xdotool()
    assert found is not None
    assert found.window_id == "69206020"
    assert found.pid == 3321
    assert found.app == "chrome", "the /proc fallback is not wired to the empty class"

    # With no pid either, the app is named rather than left blank.
    fake_run(monkeypatch, {
        "getactivewindow": "69206020\n",
        "getwindowname": "notes.md\n",
    })
    assert _via_xdotool().app == "unknown"


def test_no_active_window_from_xdotool_is_nothing_focused(
    monkeypatch: pytest.MonkeyPatch,
):
    """``xdotool getactivewindow`` exits non-zero when nothing has focus."""
    fake_run(monkeypatch, {})
    assert _via_xdotool() is None
    fake_run(monkeypatch, {"getactivewindow": "  \n"})
    assert _via_xdotool() is None


# ===========================================================================
# Both monitors: registered, switchable, and off until switched on
# ===========================================================================


@pytest.mark.parametrize("monitor_id,cls", [("window", WindowMonitor),
                                            ("clipboard", ClipboardMonitor)])
def test_each_monitor_is_registered_and_switchable(monitor_id: str, cls: type):
    """A monitor nobody registered is a watcher that silently never runs.

    Every other test in this file drives the class directly, which is exactly the
    shape that stays green when the supervisor has never heard of it. And the
    setting names have to exist in ``DEFAULTS``, or the Awareness screen writes a
    key nothing reads -- a toggle that does nothing, which is worse than a missing
    one because the user believes it.
    """
    registered = supervisor().get(monitor_id)
    assert isinstance(registered, cls)

    monitor = cls()
    assert monitor.enabled_setting == f"monitor_{monitor_id}_enabled"
    assert monitor.interval_setting == f"monitor_{monitor_id}_interval_seconds"
    assert monitor.enabled_setting in app_settings.DEFAULTS
    assert monitor.interval_setting in app_settings.DEFAULTS
    assert app_settings.DEFAULTS[monitor.enabled_setting] is False, (
        "a monitor that reads content must be off until the user switches it on"
    )
    assert monitor.enabled() is False

    app_settings.set(monitor.enabled_setting, True)
    try:
        assert monitor.enabled() is True
    finally:
        # ``reset`` removes the row so the default applies again; a stored False is
        # the same value but a different state, and settings are global.
        app_settings.reset(monitor.enabled_setting)
    assert monitor.enabled() is False


def test_neither_monitor_writes_what_it_sees_to_the_activity_log():
    """Both read content, and both fire constantly. Activity is not the place.

    A focus change every five seconds and a copy every few would fill the
    Activity log with a transcript of the user's afternoon -- titles and clipboard
    text at rest, in a table built for "what did NEXUS do", which is a retention
    decision nobody made.
    """
    assert WindowMonitor.record_to_activity is False
    assert ClipboardMonitor.record_to_activity is False


# ===================================================== against the real machine


@pytest.mark.skipif(
    not (shutil.which("xprop") and os.environ.get("DISPLAY")),
    reason="no xprop or no X display on this machine",
)
def test_xprop_still_prints_the_shape_this_module_parses():
    """Everything above is recorded output. This is the part that can go stale.

    If xprop renamed a property or changed its quoting, every test in this file
    would still pass and the monitor would report "unknown" forever. So: make the
    real calls and require that the live output parses into a focus reading.
    """
    root = window_monitor._run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
    if not root or not window_monitor._HEX_ID.search(root):
        pytest.skip("no _NET_ACTIVE_WINDOW on this display")
    assert "_NET_ACTIVE_WINDOW" in root, "the root property was renamed"

    found = _via_xprop()
    if found is None:
        pytest.skip("nothing is focused on this display right now")
    assert found.window_id.startswith("0x")
    assert found.app and found.app != "unknown", (
        "neither WM_CLASS nor /proc produced an app name from the live window"
    )


@pytest.mark.skipif(
    not any(shutil.which(binary) for binary, _argv, _wayland in _READERS),
    reason="no clipboard reader installed on this machine",
)
def test_the_installed_clipboard_reader_answers_the_argv_this_module_sends():
    """The argv lists are recorded flags, and a flag renamed is a silent failure.

    ``_read_clipboard`` turns any non-zero exit with no stderr into "" -- an empty
    clipboard -- so a reader rejecting its own arguments would read as a user who
    never copies anything. This runs the real command and requires that it either
    returns text or fails loudly, never that it quietly returns nothing while
    complaining.
    """
    binary, argv, _wayland = next(
        (b, a, w) for b, a, w in _READERS if shutil.which(b)
    )
    text, error = clipboard_monitor._read_clipboard(argv)
    assert not (text == "" and "usage" in error.lower()), (
        f"{binary} did not understand {argv!r}"
    )
    if text is None:
        # A real failure (no display, no Wayland socket) is an honest answer.
        assert error, "the reader failed with no reason, which sample() cannot report"
    else:
        assert isinstance(text, str)
