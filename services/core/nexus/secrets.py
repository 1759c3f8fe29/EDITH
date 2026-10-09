"""Credential storage (task A8).

Spec §13 wants API credentials held by a secure mechanism and §65 forbids them
appearing in logs, prompts, or diagnostics. Credentials live in the OS keyring.

A repo-root ``.env`` is honoured as a *development* source only, and the fact
that a value came from there rather than the keyring is reported truthfully so
the Settings screen can offer to migrate it (§78: detect, never pretend).
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Literal

from .config import REPO_ROOT
from .logging_setup import get_logger, register_secret, forget_secret

log = get_logger("secrets")

SERVICE = "nexus"

Source = Literal["keyring", "env", "missing"]

# Credential names NEXUS knows how to use. Listed so the Settings screen can
# render a row per provider without discovering names by accident.
KNOWN_CREDENTIALS: tuple[str, ...] = (
    "NVIDIA_API_KEY",
    "MISTRAL_API_KEY",
    "GEMINI_API_KEY",
    # Rotation pools. Each of these may hold several keys from different accounts
    # -- ``OPENROUTER_API_KEY`` plus ``OPENROUTER_API_KEY_1..N`` -- which the
    # provider layer spreads requests across so one account's rate limit is not
    # the ceiling for the whole install. Read through ``pool()``, never by name.
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    # The gateway-style spelling of the Anthropic credential. Listed as its own
    # name rather than folded into the one above because they are used
    # differently: one is a direct Anthropic key, the other a token for an
    # endpoint set by ANTHROPIC_BASE_URL, and a user who has the second and sees
    # only the first reported would think nothing was configured.
    "ANTHROPIC_AUTH_TOKEN",
    "SERPAPI_API_KEY",
)

#: Names in ``.env`` that are configuration, not credentials. They resolve through
#: the same file for convenience, but must never be handed to the log redactor:
#: redacting a base URL turns every diagnostic that mentions the endpoint into
#: ``***``, which is precisely when the user needs to see it.
NON_SECRET_NAMES: frozenset[str] = frozenset({"ANTHROPIC_BASE_URL"})



@dataclass(frozen=True)
class CredentialStatus:
    """What the UI is allowed to know about a credential: never its value."""

    name: str
    present: bool
    source: Source
    hint: str | None  # last 4 characters, enough to tell two keys apart


#: Name read during the startup probe. Never stored, so a miss is the expected
#: result -- the probe is testing whether the store answers at all, not what is in
#: it.
_PROBE = "__nexus_keyring_probe__"

#: How long a single keyring call may take before we stop waiting for it. Every
#: one of them crosses D-Bus into another process, so "slow" and "never" look the
#: same from here until a clock says otherwise.
_KEYRING_TIMEOUT_S = 5.0


class _KeyringTimeout(Exception):
    """The keyring did not answer in time. Never leaves this module."""


def _bounded(fn: Any, *args: Any, timeout: float = _KEYRING_TIMEOUT_S) -> Any:
    """Run one keyring call on a throwaway thread and stop waiting after ``timeout``.

    A locked Secret Service collection does not fail. ``keyring`` asks it to
    unlock, the daemon opens a password prompt, and the D-Bus call blocks until
    somebody answers it. When nobody can -- a service start, a headless session, a
    terminal whose graphical session it cannot draw into -- the wait has no end,
    and because the backend was built while :mod:`nexus.secrets` was still being
    imported it took the entire core with it: uvicorn never bound a port, no log
    line was written, and the process sat in ``select()`` looking like a hang with
    no cause. Guarding only against exceptions cannot see that, which is exactly
    how it survived -- the code here used to assume "locked" meant "raises".

    The thread is a daemon and is deliberately abandoned when the clock runs out:
    it is parked inside another process's reply, so there is nothing to cancel.
    Callers disable the backend on timeout, so a second call is never made.
    """
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["value"] = fn(*args)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=work, name="nexus-keyring", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise _KeyringTimeout(f"no answer in {timeout:g}s")
    error = box.get("error")
    if error is not None:
        raise error
    return box.get("value")


def _locked_reason(backend: object) -> str:
    """Why this backend must not be touched, or ``""`` when it can be.

    Asked once, before the first read, and only of the Secret Service backend --
    that is the one that unlocks on demand. ``is_locked`` reads a property and
    never asks the daemon to unlock anything, so this answers the question without
    putting a password prompt on the user's screen on behalf of a background
    process they did not start.
    """
    if type(backend).__module__ != "keyring.backends.SecretService":
        return ""
    alias = str(getattr(backend, "preferred_collection", "") or "default")
    alias = alias.rsplit("/", 1)[-1] or "default"

    def check() -> bool:
        import secretstorage

        connection = secretstorage.dbus_init()
        return secretstorage.get_collection_by_alias(connection, alias).is_locked()

    try:
        locked = bool(_bounded(check))
    except _KeyringTimeout as exc:
        return f"the Secret Service did not answer ({exc})"
    except Exception:
        # Could not tell: no secretstorage, no collection under that alias, a bus
        # that replied with something else. Not grounds for refusing the backend --
        # the bounded probe below is the fallback, and it cannot hang either.
        log.debug("could not determine whether the keyring is locked", exc_info=True)
        return ""
    if not locked:
        return ""
    return (
        f"the '{alias}' collection is locked; unlocking it needs a password typed into "
        "this session, and NEXUS will not open that prompt by itself"
    )


class _KeyringBackend:
    """Thin wrapper that tolerates the keyring being unavailable *or* unresponsive.

    XFCE sessions do not always run a Secret Service daemon, and when one does run
    its login collection is often still locked. Both mean "no keys from here", and
    both must be said out loud rather than silently losing the user's key -- but
    they fail differently: the first raises, the second blocks. Every call below is
    wrapped by :func:`_bounded` for that reason.
    """

    def __init__(self) -> None:
        self.available = False
        self.name = "unavailable"
        #: Why the keyring stopped being usable, if it did. Reported to the UI so
        #: the Credentials screen can say where keys are actually coming from
        #: instead of implying they are in the OS keyring when they are not.
        self.disabled_reason = ""
        self._kr: Any = None
        try:
            import keyring
            from keyring.backends.fail import Keyring as FailKeyring

            backend = keyring.get_keyring()
            if isinstance(backend, FailKeyring):
                self.name = "none (no Secret Service running)"
            else:
                self.name = type(backend).__name__
                # Ask before touching it, because touching it is what blocks.
                self.disabled_reason = _locked_reason(backend)
                if self.disabled_reason:
                    log.warning(
                        "keyring present but unusable (%s); reading credentials from .env instead",
                        self.disabled_reason,
                    )
                else:
                    self._kr = keyring
                    self.available = True
        except Exception as exc:  # pragma: no cover - depends on host session
            self.name = f"unavailable ({exc.__class__.__name__})"
            self.disabled_reason = f"{exc.__class__.__name__}: {exc}"
        # Probe once, now, rather than discovering on the first real read. A
        # backend can exist, be unlocked, and still be unusable. Finding that out
        # here means backend_status() is already truthful the first time the UI
        # asks, instead of reporting the OS keyring and then quietly changing its
        # mind mid-session.
        if self.available:
            self.get(_PROBE)
        log.info("keyring backend resolved", extra={"backend": self.name})

    def _disable(self, name: str, exc: BaseException) -> None:
        """Stand down for the rest of the process, and say why once.

        A locked or unresponsive keyring is not a per-read failure, it is a
        standing condition: the collection will not unlock because we asked again.
        Warning on every read would bury a real problem under identical
        tracebacks, and each retry costs a D-Bus round trip -- or, on timeout, five
        seconds of the user's time. So this happens once and everything falls
        through to the .env path.
        """
        self.available = False
        self._kr = None
        self.disabled_reason = f"{exc.__class__.__name__}: {exc}"
        log.warning(
            "keyring became unusable (%s); reading credentials from .env instead",
            self.disabled_reason,
        )
        log.debug("keyring call failed for %s", name, exc_info=True)

    def get(self, name: str) -> str | None:
        if not self.available or self._kr is None:
            return None
        try:
            value = _bounded(self._kr.get_password, SERVICE, name)
        except Exception as exc:
            self._disable(name, exc)
            return None
        return value if isinstance(value, str) else None

    def set(self, name: str, value: str) -> bool:
        if not self.available or self._kr is None:
            return False
        try:
            _bounded(self._kr.set_password, SERVICE, name, value)
            return True
        except Exception as exc:
            self._disable(name, exc)
            return False

    def delete(self, name: str) -> bool:
        if not self.available or self._kr is None:
            return False
        try:
            _bounded(self._kr.delete_password, SERVICE, name)
            return True
        except Exception as exc:
            self._disable(name, exc)
            return False


_backend_instance: _KeyringBackend | None = None
_backend_lock = threading.Lock()


def _keyring() -> _KeyringBackend:
    """The one backend, built on first use rather than on import.

    Import time is the wrong moment to talk to another process: an import cannot
    report a problem, cannot be timed out by its caller, and here runs before
    logging is configured. Resolving lazily means a slow or hostile Secret Service
    costs the first credential read, not the ability to start.
    """
    global _backend_instance
    with _backend_lock:
        if _backend_instance is None:
            _backend_instance = _KeyringBackend()
        return _backend_instance
_env_cache: dict[str, str] | None = None


def _env_values() -> dict[str, str]:
    """Parse the repo-root .env once. Development convenience only."""
    global _env_cache
    if _env_cache is not None:
        return _env_cache
    values: dict[str, str] = {}
    path = REPO_ROOT / ".env"
    if path.is_file():
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip("'\"")
            if value:
                values[key.strip()] = value
    _env_cache = values
    for name, value in values.items():
        if name not in NON_SECRET_NAMES:
            register_secret(value)
    return values


def env_value(name: str, *, prefer_file: bool = False) -> str | None:
    """Read a non-secret setting from ``.env`` or the environment.

    Separate from :func:`get` because :func:`get` registers whatever it returns
    with the log redactor, which is right for a key and wrong for an endpoint:
    a redacted base URL makes "could not reach ***" the least useful possible
    error message. Nothing here is registered, so callers must only use it for
    values in :data:`NON_SECRET_NAMES`.

    ``prefer_file`` inverts the usual precedence and exists for names that other
    programs also read. ``ANTHROPIC_BASE_URL`` is the clear case: it is the
    standard name, so any Anthropic client in the same shell exports it, and
    inheriting that means NEXUS quietly using someone else's endpoint. When the
    project has declared a value in its own ``.env``, that is the answer.
    """
    if prefer_file:
        return _env_values().get(name) or os.environ.get(name)
    return os.environ.get(name) or _env_values().get(name)



def backend_status() -> dict[str, object]:
    """Reported to the UI so the user knows where their keys actually live."""
    backend = _keyring()
    env_present = (REPO_ROOT / ".env").is_file()
    if backend.available:
        where = f"the OS keyring ({backend.name})"
    elif env_present:
        where = "the project's .env file, which is not encrypted"
    else:
        where = "nowhere -- no key store is usable"
    return {
        "keyring_available": backend.available,
        "keyring_backend": backend.name,
        "keyring_disabled_reason": backend.disabled_reason,
        "env_file_present": env_present,
        # One sentence the Credentials screen can show verbatim. A user who thinks
        # their keys are in the OS keyring when they are sitting in a plain text
        # file has been misled about something that matters (spec 65).
        "storage_summary": f"Keys are read from {where}.",
    }


def get(name: str) -> str | None:
    """Resolve a credential: keyring first, then .env."""
    value = _keyring().get(name)
    if not value:
        value = _env_values().get(name) or os.environ.get(name)
    register_secret(value)
    return value


def pool(name: str) -> list[str]:
    """Every key configured for one provider, in a stable order.

    Providers with several accounts -- OpenRouter and Groq here -- are declared as
    ``NAME`` and/or ``NAME_1``, ``NAME_2``, ... so a user with a hundred accounts
    does not have to invent a naming scheme. The unsuffixed name sorts first, so
    a single-key install behaves exactly as before and there is no special case
    for "only one key".

    The values are gathered but *not* returned as a set: order matters, because the
    caller rotates through the list and a set would make the rotation order change
    between processes -- which turns "spread the load" into "hit a random account
    and hope".

    Keyring entries are looked up for each name, so a key stored properly is
    preferred over the same key in the plain-text ``.env``, matching ``get()``.
    """
    found: list[tuple[int, str, str]] = []

    def consider(env_name: str, rank: int) -> None:
        value = _keyring().get(env_name) or _env_values().get(env_name) \
            or os.environ.get(env_name)
        if value:
            register_secret(value)
            found.append((rank, env_name, value.strip()))

    consider(name, 0)
    for index in range(1, 101):
        consider(f"{name}_{index}", index)

    found.sort(key=lambda item: item[0])
    return [value for _rank, _n, value in found]


def source_of(name: str) -> Source:
    if _keyring().get(name):
        return "keyring"
    if _env_values().get(name) or os.environ.get(name):
        return "env"
    return "missing"


def set(name: str, value: str) -> bool:  # noqa: A001 - deliberate API shape
    """Store a credential. Returns False when no secure backend accepted it."""
    value = value.strip()
    if not value:
        raise ValueError("credential value must not be empty")
    register_secret(value)
    stored = _keyring().set(name, value)
    if not stored:
        log.error(
            "no secure store accepted the credential; refusing to persist it in plaintext",
            extra={"credential": name},
        )
    return stored


def delete(name: str) -> bool:
    """Remove a credential from the keyring.

    A value present only in ``.env`` cannot be deleted from here -- that file is
    the user's, so we report the failure instead of pretending (§79).
    """
    backend = _keyring()
    forget_secret(backend.get(name))
    return backend.delete(name)


def status(names: tuple[str, ...] = KNOWN_CREDENTIALS) -> list[CredentialStatus]:
    out: list[CredentialStatus] = []
    for name in names:
        src = source_of(name)
        value = get(name) if src != "missing" else None
        out.append(
            CredentialStatus(
                name=name,
                present=bool(value),
                source=src,
                hint=value[-4:] if value else None,
            )
        )
    return out


def prime_redaction() -> int:
    """Register every known credential with the log redactor at startup."""
    count = 0
    for name in KNOWN_CREDENTIALS:
        if get(name):
            count += 1
    return count
