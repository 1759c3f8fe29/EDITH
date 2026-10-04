"""Why a step failed, and whether trying again could possibly help (§13, §14).

The behaviour spec asks for two things that are usually conflated. First, classify
the failure -- transient, permission-related, missing dependency, wrong target,
unavailable capability, network, state, configuration, tool, or genuinely unclear.
Second, choose a *deliberate* recovery: "Never blindly repeat the exact same
failing action indefinitely."

Those are separate because most failures must **not** be retried, and the reason
differs each time. A denied permission is an answer, not an obstacle -- retrying
it is nagging the user with the same dialog. A missing program will not install
itself between two attempts. A wrong path is still wrong the second time. Only a
small set of failures are the kind a second attempt can fix at all, and this
module exists so that a decision to retry carries a stated reason instead of being
a loop with a counter.

Three rules hold the whole thing together.

* **Structured facts beat text.** A timeout, a denial the engine reported, and an
  unavailable tool are passed in as arguments, not matched out of a sentence.
  Pattern-matching an error string is a heuristic, and it is used only for what
  arrives as prose from someone else's program.
* **A non-idempotent tool is never retried automatically**, whatever the class of
  failure. If running it twice is not the same as running it once, then a failure
  that might have partially applied is not something to repeat without a human
  deciding -- ``Tool.idempotent`` is the flag that says which is which, and this
  is the code that finally reads it.
* **Unclear is its own answer.** When nothing matches, the class is ``ambiguous``
  and the recovery is to inspect rather than to retry. Guessing a class in order
  to justify a retry is how "intelligent retries" becomes a spin loop.

Pure by design: no database, no bus, no I/O. It takes strings and flags and
returns a verdict, which is what makes it testable without putting a machine into
a particular broken state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: The closed vocabulary from §13. Closed because a classifier that can invent a
#: new class cannot be reasoned about -- the caller has a branch per class, and an
#: unexpected value would fall through it to a default that quietly retries.
KINDS: tuple[str, ...] = (
    "transient",
    "network",
    "timeout",
    "permission",
    "missing_dependency",
    "unavailable",
    "wrong_target",
    "state",
    "configuration",
    "tool",
    "ambiguous",
)

#: Classes where a second identical attempt can plausibly succeed on its own.
#: Everything else needs something to *change* first -- a different target, a
#: granted permission, an installed program, a re-plan -- and repeating the call
#: is not that change.
_RETRYABLE: frozenset[str] = frozenset({"transient", "network", "timeout"})

#: One initial attempt plus at most two retries. Bounded here rather than by the
#: caller so "never loop endlessly" is a property of the policy, not of whoever
#: happens to be calling it.
MAX_ATTEMPTS = 3

#: Backoff before the next attempt, indexed by the attempt just finished. Short,
#: because a task is something the user is watching: a thirty-second wait to retry
#: a DNS lookup reads as a hang, and the honest move there is to report and let
#: them decide.
_DELAYS: tuple[float, ...] = (0.8, 2.4)

# Ordered specific-first: the first pattern that matches decides, so a narrow
# phrase must come before a general one. "already in use" is state, not a wrong
# target, even though it also describes something that could not be used.
_PATTERNS: tuple[tuple[str, str], ...] = (
    # Permission. "You declined." is the permission engine's own wording (C6).
    (
        "permission",
        r"you declined|denied by|permission denied|not authoris|not authoriz|"
        r"operation not permitted|\beacces\b|\beperm\b|requires root|must be root|"
        r"needs sudo|are not allowed|refused by policy|no answer, so nothing was done",
    ),
    # A program that is not there. Distinct from "unavailable": this names a
    # dependency the user could install.
    (
        "missing_dependency",
        r"command not found|not installed|no such command|executable file not found|"
        r"is not on \$?path|install .* first",
    ),
    # The capability itself is off or absent on this machine. Deliberately narrow:
    # the bare word "unavailable" cannot be used here, because "503 Service
    # Unavailable" is a busy server -- transient, retryable -- and matching it as a
    # missing capability would refuse every retry of a rate-limited API. The real
    # signal for this class is ``unavailable_reason``, which arrives as a fact.
    (
        "unavailable",
        r"not available (?:on|here|in this)|is unavailable on this|no backend|"
        r"not implemented|not supported on this|disabled in settings|switched off",
    ),
    # Credentials and configuration. 401 belongs here, not in permission: the
    # user's own authorisation is fine, the key is wrong.
    (
        "configuration",
        r"\b401\b|unauthorized|invalid api key|missing api key|no api key|"
        r"not configured|no model available|misconfigur",
    ),
    # Rate limits and explicit try-again signals.
    (
        "transient",
        r"\b429\b|\b503\b|rate limit|too many requests|temporarily unavailable|"
        r"please try again|try again later|overloaded|server is busy|"
        r"resource temporarily unavailable",
    ),
    # The network, named specifically enough to be actionable.
    (
        "network",
        r"name or service not known|temporary failure in name resolution|"
        r"connection refused|connection reset|network is unreachable|no route to host|"
        r"could not resolve|\bdns\b|\bssl\b|certificate verify|\b502\b|\b504\b|"
        r"failed to establish a new connection",
    ),
    ("timeout", r"timed out|timeout|deadline exceeded"),
    # The world is in a state that blocks this action. Not retryable: it needs a
    # re-plan (inspect what holds the port, decide whether it is what you wanted).
    (
        "state",
        r"already running|already exists|already in use|address already in use|"
        r"resource busy|device or resource busy|is locked|another instance|"
        r"is already open|nothing to do",
    ),
    # The target does not exist. Retrying the same path cannot fix it.
    (
        "wrong_target",
        r"no such file or directory|no such process|no such window|no such tab|"
        r"does not exist|not found|could not find|no matching|unknown application",
    ),
    # Our own call was wrong: schema, unknown tool, bad parameters.
    (
        "tool",
        r"does not match|invalid parameter|unknown tool|schema|required property|"
        r"must be one of|expected .* got",
    ),
)

_COMPILED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (kind, re.compile(pattern, re.IGNORECASE)) for kind, pattern in _PATTERNS
)

#: What a recovery would have to change, per class. Written for two readers: the
#: user, who sees it on the failed step, and the planner, which is handed it when
#: it re-plans around a failure. Both need to know what would be different next
#: time -- "retry" is not a plan, "install ffmpeg" is.
_HINTS: dict[str, str] = {
    "transient": "the service was busy; the same call may work shortly",
    "network": "the network or the remote host was unreachable",
    "timeout": "it did not finish in time; the same call may work, or the target is stuck",
    "permission": "it needs authorisation that was not given, so nothing but the user can change it",
    "missing_dependency": "a program this needs is not installed on this computer",
    "unavailable": "this capability is not available here, so no attempt can succeed",
    "wrong_target": "the thing it was told to act on does not exist; find the real target first",
    "state": "the system is already in a state that blocks this; inspect it and re-plan",
    "configuration": "a credential or setting is wrong; fix that before trying again",
    "tool": "the call itself was malformed; correct the parameters, do not repeat them",
    "ambiguous": "the failure does not say what went wrong; inspect before acting again",
}


@dataclass(frozen=True)
class Failure:
    """One classified failure. ``evidence`` is what led to the class."""

    kind: str
    hint: str
    evidence: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "hint": self.hint, "evidence": self.evidence}


@dataclass(frozen=True)
class Recovery:
    """The decision. ``why`` is stated whether or not a retry happens."""

    retry: bool
    delay_s: float
    why: str


def classify(
    error: str,
    *,
    timed_out: bool = False,
    denied: bool = False,
    unavailable_reason: str | None = None,
) -> Failure:
    """Classify a failure from what is known about it.

    The three keyword arguments are facts the runtime already established, and
    they win over the text: a step killed by our own timeout is a ``timeout`` even
    if the tool's last words were about a file, and a denial reported by the
    permission engine is a ``permission`` failure even when the tool dressed it up
    as something else.
    """
    text = (error or "").strip()
    if denied:
        return Failure(
            "permission", _HINTS["permission"], text or "the permission engine denied it"
        )
    if unavailable_reason:
        return Failure("unavailable", _HINTS["unavailable"], unavailable_reason)
    if timed_out:
        return Failure("timeout", _HINTS["timeout"], text or "it did not finish in time")
    if not text:
        # A failure with no error text is not a class -- it is a gap in whatever
        # reported it, and saying "ambiguous" is the honest reading.
        return Failure("ambiguous", _HINTS["ambiguous"], "the tool did not say why it failed")
    for kind, pattern in _COMPILED:
        if pattern.search(text):
            return Failure(kind, _HINTS[kind], text)
    return Failure("ambiguous", _HINTS["ambiguous"], text)


def _article(kind: str) -> str:
    """"a transient failure", "an unavailable one". The reason is read by a user."""
    return "an" if kind[:1] in "aeiou" else "a"


def plan(kind: str, *, idempotent: bool, attempts: int) -> Recovery:
    """Whether to try again, given the class, the tool, and how many attempts ran.

    ``attempts`` counts attempts already made, so the first call after one failure
    passes 1. The refusals are stated rather than silent: the caller records ``why``
    on the step, which is what makes a decision *not* to retry reviewable.
    """
    if kind not in KINDS:
        # An unknown class must not fall through to a retry. Treated as a refusal,
        # and the caller can see that it was.
        return Recovery(
            False, 0.0, f"{kind!r} is not a failure class this policy knows, so it does not retry"
        )
    if attempts >= MAX_ATTEMPTS:
        return Recovery(False, 0.0, f"already tried {attempts} times, which is the limit")
    if kind not in _RETRYABLE:
        return Recovery(
            False,
            0.0,
            f"{_article(kind)} {kind} failure is not fixed by repeating the call -- {_HINTS[kind]}",
        )
    if not idempotent:
        return Recovery(
            False,
            0.0,
            "this tool is not idempotent, so a failure that may have partly applied "
            "is not repeated automatically",
        )
    delay = _DELAYS[min(attempts - 1, len(_DELAYS) - 1)]
    return Recovery(
        True, delay, f"{_article(kind)} {kind} failure can clear on its own; retrying after {delay}s"
    )
