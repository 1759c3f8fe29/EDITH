"""Policy: what a request actually touches, and what must never be touched
(task C2; spec §6, §7, §8, §57, §58).

The engine decides *whether* the user has agreed. This module decides *what they
would be agreeing to* -- it turns raw parameters into a scope string, and it
holds the hard refusals that no grant can override.

Two ideas do most of the work:

``scope_for``
    Collapses a concrete request ("read /home/santosh/Documents/tax/2024.pdf")
    into the thing a person would actually want to grant ("~/Documents"). Grants
    are stored against the scope, so approving one file in a folder does not
    silently approve the whole disk, and approving a folder does not require
    re-approving every file inside it.

``check_hard_limits``
    Refusals that exist regardless of permission state: the system directories,
    credential stores, and NEXUS's own database. §57 requires that a request to
    do something impossible or forbidden gets a clear "no", not an attempt.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any
from urllib.parse import urlparse

from . import capabilities

HOME = Path.home()

# Absolute prefixes that are never writable and never readable through NEXUS,
# no matter what the user has granted. Reading /etc/shadow or writing /usr is not
# a personal-assistant action; it is a way to break the machine.
PROTECTED_PREFIXES: tuple[str, ...] = (
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib32",
    "/lib64",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/sys",
    "/usr",
    "/var",
    "/bin",
)

# Read-only exceptions inside otherwise-protected trees. These are inert,
# genuinely useful for answering questions, and contain no secrets.
#
# Listed as *resolved* paths, because resolve_path() follows symlinks before this
# is consulted. /etc/os-release is a link into /usr/lib on Debian-derived
# systems, so naming only the /etc path would refuse the very file the exception
# exists to permit -- and the refusal would blame system-file protection for what
# is really a stale list. Both spellings are kept so either resolution works.
READABLE_EXCEPTIONS: tuple[str, ...] = (
    "/etc/os-release",
    "/usr/lib/os-release",
    "/etc/timezone",
    "/proc/meminfo",
    "/proc/cpuinfo",
    "/proc/uptime",
    "/proc/loadavg",
    "/sys/class/power_supply",
)

# Credential and key material under the user's own home. The user "owns" these,
# but exfiltrating them to a cloud model is exactly the failure mode §65 exists
# to prevent, so they are refused outright.
PROTECTED_HOME_RELATIVE: tuple[str, ...] = (
    ".ssh",
    ".gnupg",
    ".pki",
    ".aws",
    ".azure",
    ".kube",
    ".docker/config.json",
    ".config/gcloud",
    ".local/share/keyrings",
    ".gnome2/keyrings",
    ".password-store",
    ".mozilla/firefox",  # contains saved logins and cookies
    ".config/google-chrome",
    ".config/chromium",
    ".config/BraveSoftware",
    ".netrc",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
)

# NEXUS's own state. Reading it through file tools would route the permission
# database and conversation history through a model; writing it would let the AI
# edit its own permission grants.
SELF_PROTECTED_RELATIVE: tuple[str, ...] = (
    ".local/share/nexus",
    ".config/nexus",
    ".local/state/nexus",
)

# Files whose names indicate secrets, wherever they live.
SECRET_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\.env(\..+)?$"),
    re.compile(r"^id_(rsa|dsa|ecdsa|ed25519)$"),
    re.compile(r"\.pem$"),
    re.compile(r"\.key$"),
    re.compile(r"\.p12$"),
    re.compile(r"\.pfx$"),
    re.compile(r"^credentials(\.json)?$"),
    re.compile(r"^service-account.*\.json$"),
    re.compile(r"^\.htpasswd$"),
)


@dataclass(frozen=True)
class Refusal:
    """A hard no. ``reason`` is shown to the user verbatim."""

    reason: str
    detail: str = ""


@dataclass(frozen=True)
class ResolvedPath:
    raw: str
    path: Path
    exists: bool
    is_dir: bool
    inside_home: bool
    display: str


# ------------------------------------------------------------------ path logic


def resolve_path(raw: str) -> ResolvedPath:
    """Expand and fully resolve a path.

    ``resolve()`` follows symlinks, which is the point: a symlink in the user's
    home pointing at /etc must be judged as /etc, not as the link's location.
    """
    expanded = Path(raw).expanduser()
    try:
        resolved = expanded.resolve()
    except (OSError, RuntimeError):
        # Broken symlink or a loop; fall back to a lexical resolution so the
        # protected-prefix checks below still get an absolute path to judge.
        resolved = Path(PurePath("/") / expanded).absolute()

    exists = resolved.exists()
    try:
        inside = resolved.is_relative_to(HOME)
    except (AttributeError, ValueError):  # pragma: no cover - py<3.9 safety
        inside = str(resolved).startswith(str(HOME))

    display = f"~/{resolved.relative_to(HOME)}" if inside and resolved != HOME else str(resolved)
    if resolved == HOME:
        display = "~"

    return ResolvedPath(
        raw=raw,
        path=resolved,
        exists=exists,
        is_dir=resolved.is_dir() if exists else False,
        inside_home=inside,
        display=display,
    )


def _under(path: Path, prefix: str) -> bool:
    prefix_path = Path(prefix)
    return path == prefix_path or str(path).startswith(str(prefix_path) + "/")


def path_refusal(raw: str, *, write: bool) -> Refusal | None:
    """Hard limits for file access. Returns None when the path is permissible."""
    resolved = resolve_path(raw)
    path = resolved.path
    text = str(path)

    for rel in SELF_PROTECTED_RELATIVE:
        if _under(path, str(HOME / rel)):
            return Refusal(
                "That location holds NEXUS's own data.",
                f"{resolved.display} contains the permission grants, conversations and "
                "memories NEXUS runs on. It is not reachable through file tools, so the AI "
                "cannot read your history in bulk or alter its own permissions.",
            )

    for rel in PROTECTED_HOME_RELATIVE:
        if _under(path, str(HOME / rel)):
            return Refusal(
                "That location holds credentials or private keys.",
                f"{resolved.display} stores secrets such as keys, tokens or saved logins. "
                "NEXUS refuses to read or modify it so that material is never sent to a "
                "cloud model.",
            )

    if path.name and any(p.search(path.name) for p in SECRET_NAME_PATTERNS):
        return Refusal(
            "That file looks like it holds secrets.",
            f"{resolved.display} matches a credential-file pattern. If you genuinely need "
            "NEXUS to work with it, copy out the specific values you want to share.",
        )

    for prefix in PROTECTED_PREFIXES:
        if not _under(path, prefix):
            continue
        if not write and any(_under(path, allowed) for allowed in READABLE_EXCEPTIONS):
            return None
        verb = "write to" if write else "read"
        return Refusal(
            f"NEXUS will not {verb} system files.",
            f"{text} is part of the operating system. Changing it can break this computer, "
            f"so it is outside what NEXUS can do{'' if write else ' -- even read-only'}.",
        )

    return None


def scope_for_path(raw: str) -> str:
    """The grant scope a path belongs to.

    Inside home, the scope is the first-level directory (``~/Documents``), which
    matches how people think about permission: "yes, my Documents folder". A file
    directly in home gets ``~``; anything outside home is scoped to its own
    parent directory, which is deliberately narrow.
    """
    resolved = resolve_path(raw)
    path = resolved.path

    if resolved.inside_home:
        try:
            relative = path.relative_to(HOME)
        except ValueError:
            return str(path.parent)
        parts = relative.parts
        if not parts:
            return "~"
        return f"~/{parts[0]}"

    return str(path if resolved.is_dir else path.parent)


def path_in_scope(raw: str, scope: str) -> bool:
    """Whether a concrete path falls inside a stored scope."""
    if scope == "*":
        return True
    resolved = resolve_path(raw).path
    scope_path = Path(scope.replace("~", str(HOME), 1)) if scope.startswith("~") else Path(scope)
    try:
        scope_path = scope_path.resolve()
    except (OSError, RuntimeError):
        return False
    return _under(resolved, str(scope_path))


# -------------------------------------------------------------- terminal logic

# Commands and shapes that are refused outright. This is not an attempt at a
# complete sandbox -- it cannot be, and pretending otherwise would be the kind of
# false safety claim §78 forbids. It blocks the specific catastrophic one-liners
# a language model is most likely to produce, and the permission dialog states
# plainly that any accepted command runs with the user's full account rights.
TERMINAL_REFUSALS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\brm\b[^|;&]*\s(-[a-zA-Z]*[rR][a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*[rR])\b[^|;&]*\s+/(\s|$)"),
     "recursive delete of the filesystem root"),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "formatting a filesystem"),
    (re.compile(r"\bdd\b[^|;&]*\bof=/dev/(sd|nvme|vd|hd)"), "writing directly to a disk device"),
    (re.compile(r">\s*/dev/(sd|nvme|vd|hd)\w+"), "writing directly to a disk device"),
    (re.compile(r":\s*\(\s*\)\s*\{.*\}\s*;\s*:"), "a fork bomb"),
    (re.compile(r"\bchmod\b[^|;&]*\s(-R|--recursive)\b[^|;&]*\s/(\s|$)"),
     "changing permissions on the whole filesystem"),
    (re.compile(r"\bchown\b[^|;&]*\s(-R|--recursive)\b[^|;&]*\s/(\s|$)"),
     "changing ownership of the whole filesystem"),
    (re.compile(r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|k|da)?sh\b"),
     "piping a downloaded script straight into a shell"),
    (re.compile(r"\bhistory\s+-c\b"), "erasing shell history"),
    (re.compile(r"\bshred\b"), "unrecoverable file destruction"),
    (re.compile(r"\bcrontab\b[^|;&]*\s-r\b"), "deleting all scheduled jobs"),
    (re.compile(r"\b(iptables|nft)\b[^|;&]*\s(-F|flush)\b"), "flushing firewall rules"),
    (re.compile(r"\bsudo\b"), "running with administrator rights"),
    (re.compile(r"\bsu\b\s"), "switching to another user account"),
    (re.compile(r"\bpkexec\b"), "running with administrator rights"),
)

# Substrings that make a command destructive enough to require the strongest
# confirmation, without being refused outright.
DESTRUCTIVE_HINTS: tuple[str, ...] = (
    "rm ", "rmdir", "mv ", "truncate", "kill", "pkill", "killall",
    "git reset --hard", "git clean", "git push --force", "drop table",
    "docker rm", "docker system prune", "npm publish", "apt remove",
    "apt purge", "systemctl stop", "systemctl disable",
)


def terminal_refusal(command: str) -> Refusal | None:
    text = command.strip()
    if not text:
        return Refusal("Empty command.", "There is nothing to run.")
    for pattern, what in TERMINAL_REFUSALS:
        if pattern.search(text):
            return Refusal(
                f"NEXUS will not run that: it involves {what}.",
                "This is refused regardless of permissions. If you need it, run it yourself "
                "in a terminal where you can see exactly what happens.",
            )
    wipe = _wipes_a_home_directory(text)
    if wipe is not None:
        return wipe
    return _protected_path_in_command(text)


#: Directories whose recursive deletion is refused outright. The home root is
#: obvious; the standard user directories are here because losing one of them is
#: indistinguishable, in consequence, from losing all of them -- and because
#: NEXUS has a trash-aware ``files.delete`` for anything legitimate.
_UNDELETABLE = (HOME,) + tuple(
    HOME / name
    for name in (
        "Documents", "Downloads", "Desktop", "Pictures", "Music", "Videos",
        "Templates", "Public", ".config", ".local", ".cache",
    )
)

_RECURSIVE_RM = re.compile(r"\brm\b(?=[^|;&]*\s-[a-zA-Z]*[rR])")


def _wipes_a_home_directory(command: str) -> Refusal | None:
    """Refuse ``rm -rf`` aimed at the home root or a standard user directory.

    ``rm -rf /`` is already refused above. This covers the far likelier accident:
    a generated path that resolves to ``~`` or ``~/Documents`` instead of the
    intended subdirectory. At the dialog those two look almost identical -- "Run:
    rm -rf ~/Documents" reads like routine tidying -- so this is refused rather
    than confirmed. ``rm -rf ~/projects/foo/node_modules`` is untouched.
    """
    if not _RECURSIVE_RM.search(command):
        return None
    for token in _path_tokens(command):
        resolved = resolve_path(token).path
        if any(resolved == target for target in _UNDELETABLE):
            return Refusal(
                f"NEXUS will not recursively delete {resolve_path(token).display}.",
                "That is your home directory or one of its top-level folders, and the "
                "deletion would not be recoverable. Deleting things inside it is fine, "
                "and the file tools send individual items to the trash.",
            )
    return None


def _path_tokens(command: str) -> list[str]:
    """Arguments that look like filesystem paths, with $HOME expanded.

    ``shlex`` handles the quoting, which matters: ``cat "~/.ssh/id_rsa"`` and
    ``cat ~/.ssh/id_rsa`` must be judged the same way.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        # Unbalanced quotes. Fall back to whitespace so an unparseable command
        # still gets checked rather than silently skipping the check.
        tokens = command.split()

    out: list[str] = []
    for raw in tokens:
        token = raw.strip("'\"")
        for var in ("${HOME}", "$HOME"):
            if token.startswith(var):
                token = str(HOME) + token[len(var) :]
        # Strip shell redirection and separators glued to the front of a path.
        token = token.lstrip("<>|&;()")
        if not token or token.startswith("-"):
            continue
        if token.startswith(("/", "~", "./", "../")) or "/" in token:
            out.append(token)
    return out


def _protected_path_in_command(command: str) -> Refusal | None:
    """Apply the file hard limits to paths named in a shell command.

    Without this the terminal is a second door to everything ``path_refusal``
    guards: the file tools refuse ``~/.ssh/id_rsa``, while ``cat ~/.ssh/id_rsa``
    would return the key through the same permission dialog and put it in a
    model's context. A blocklist with a door next to it is not a blocklist.

    Checked as a read (``write=False``) so the deliberate read-only exceptions --
    ``/proc/meminfo``, ``/etc/os-release`` -- keep working; a path with no read
    exception is refused for either direction anyway.

    **Being run is not being read.** The *first* token names a program the shell
    will execute, and executing an installed binary is not the act this guard
    exists to prevent. Measured on this machine: a command naming
    ``/usr/bin/python3.12`` was refused as "reading a system file", so **NEXUS
    could not run Python at all** -- and the same applied to ``git``, ``npm`` and
    every other tool that lives under ``/usr``. A dev assistant that cannot invoke
    a compiler is not one. So the head token is skipped when it resolves to an
    executable, and everything else in the command is still checked: ``python3
    /etc/shadow`` is refused on the second token, which is the file being read.

    **This is literal-path matching, and it is not proof against obfuscation.**
    A command can reach a protected file through a variable, an encoded string, or
    a symlink created in the same command. It is effective against the case it is
    built for -- a model producing a plainly-written path, whether by mistake or
    because something in its context told it to -- and it is stated here rather
    than overclaimed. The stronger guarantee is that terminal access ships off
    (``terminal_allowed``) and every command is a critical-risk prompt showing the
    exact text that will run.
    """
    tokens = _path_tokens(command)
    for index, token in enumerate(tokens):
        if index == 0 and _is_executable(token):
            # The program being invoked. Checked for executability rather than
            # skipped blindly, so `cat /usr/lib/secret` is still refused: `cat` is
            # a bare name (not a path), and a full path to a *readable data file*
            # under a protected tree is not executable and stays refused.
            continue
        refusal = path_refusal(token, write=False)
        if refusal is not None:
            return Refusal(
                refusal.reason,
                f"{refusal.detail} The same limit applies to shell commands: NEXUS will "
                "not run a command that names that path.",
            )
    return None


def _is_executable(token: str) -> bool:
    """Whether this token names a program that exists and can be run.

    Resolves through PATH for a bare name (``python3``) and checks the file
    directly for a path (``/usr/bin/python3.12``). ``os.access(X_OK)`` is the
    question that matters: a non-executable file under ``/usr`` is data, and
    reading it is exactly what the guard should still refuse.
    """
    if not token or token.startswith("-"):
        return False
    candidate = token if os.sep in token else shutil.which(token)
    if not candidate:
        return False
    try:
        path = Path(candidate).expanduser()
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def command_is_destructive(command: str) -> bool:
    lowered = command.lower()
    return any(hint in lowered for hint in DESTRUCTIVE_HINTS)


def scope_for_command(command: str) -> str:
    """Scope a terminal grant to the program being run, not the whole shell.

    Granting "always" for ``git status`` should not also grant ``rm``. Using the
    executable name as the scope means a standing grant covers one program.
    """
    text = command.strip()
    try:
        tokens = shlex.split(text)
    except ValueError:
        return "*"
    if not tokens:
        return "*"
    # Skip leading VAR=value assignments.
    for token in tokens:
        if "=" in token and not token.startswith("-") and re.match(r"^\w+=", token):
            continue
        return Path(token).name
    return "*"


# ------------------------------------------------------------------- URL logic

BLOCKED_URL_SCHEMES = ("file", "ftp", "gopher", "data", "javascript", "about", "chrome")

PRIVATE_HOST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^localhost$", re.IGNORECASE),
    re.compile(r"^127\."),
    re.compile(r"^0\.0\.0\.0$"),
    re.compile(r"^10\."),
    re.compile(r"^192\.168\."),
    re.compile(r"^172\.(1[6-9]|2\d|3[01])\."),
    re.compile(r"^169\.254\."),
    re.compile(r"^\[?::1\]?$"),
    re.compile(r"\.local$", re.IGNORECASE),
    re.compile(r"^metadata\.google\.internal$", re.IGNORECASE),
)


def url_refusal(raw: str) -> Refusal | None:
    parsed = urlparse(raw.strip())
    scheme = (parsed.scheme or "").lower()

    if not scheme:
        return Refusal("That is not a complete web address.", f"{raw!r} has no scheme (https://).")
    if scheme in BLOCKED_URL_SCHEMES:
        return Refusal(
            f"NEXUS will not open {scheme}: addresses.",
            "Only http and https addresses are allowed, so a web request cannot be turned "
            "into a way to read local files.",
        )
    if scheme not in ("http", "https"):
        return Refusal(f"Unsupported address type: {scheme}:", "Only http and https are allowed.")

    host = (parsed.hostname or "").strip()
    if not host:
        return Refusal("That address has no host.", f"{raw!r} does not name a server to contact.")
    for pattern in PRIVATE_HOST_PATTERNS:
        if pattern.search(host):
            return Refusal(
                "NEXUS will not fetch addresses on your private network.",
                f"{host} is a local or private address. Fetching it would let a web request "
                "reach services running on this machine or your home network.",
            )
    return None


def scope_for_url(raw: str) -> str:
    parsed = urlparse(raw.strip())
    return (parsed.hostname or "*").lower()


# ------------------------------------------------------- request scope routing

# How each capability derives its scope from tool parameters. A capability
# missing from this map is scoped '*' -- it is not scopable, which is why
# ``Capability.scoped`` exists to say so in the UI.
SCOPE_SOURCES: dict[str, tuple[str, str]] = {
    "file.list": ("path", "path"),
    "file.read": ("path", "path"),
    "file.write": ("path", "path"),
    "file.move": ("source", "path"),
    "file.delete": ("path", "path"),
    "terminal.execute": ("command", "command"),
    "browser.open": ("url", "url"),
    "net.fetch": ("url", "url"),
    "app.launch": ("app", "literal"),
    "app.close": ("app", "literal"),
    "screen.capture": ("target", "literal"),
    "system.power": ("action", "literal"),
}


def scope_for(capability: str, params: dict[str, Any]) -> str:
    """Derive the grant scope for a concrete request."""
    source = SCOPE_SOURCES.get(capability)
    if source is None:
        return "*"
    key, kind = source
    value = params.get(key)
    if value is None or value == "":
        return "*"
    text = str(value)
    if kind == "path":
        return scope_for_path(text)
    if kind == "command":
        return scope_for_command(text)
    if kind == "url":
        return scope_for_url(text)
    return text


def check_hard_limits(capability: str, params: dict[str, Any]) -> Refusal | None:
    """Refusals that no permission grant can override (§57)."""
    if capability in ("file.read", "file.list"):
        target = params.get("path")
        return path_refusal(str(target), write=False) if target else None

    if capability in ("file.write", "file.delete"):
        target = params.get("path")
        return path_refusal(str(target), write=True) if target else None

    if capability == "file.move":
        for key in ("source", "destination"):
            target = params.get(key)
            if target:
                refusal = path_refusal(str(target), write=True)
                if refusal:
                    return refusal
        return None

    if capability == "terminal.execute":
        command = params.get("command")
        return terminal_refusal(str(command)) if command else None

    if capability in ("browser.open", "net.fetch"):
        url = params.get("url")
        return url_refusal(str(url)) if url else None

    return None


def describe_request(capability: str, params: dict[str, Any]) -> str:
    """One plain sentence naming the concrete effect, for the dialog (§6).

    Built from the parameters, never from model-supplied prose, so the dialog
    cannot describe something other than what will run.
    """
    if capability == "terminal.execute":
        return f"Run: {str(params.get('command', '')).strip()}"
    if capability in ("file.read", "file.list", "file.write", "file.delete"):
        verb = {
            "file.read": "Read",
            "file.list": "List the contents of",
            "file.write": "Write to",
            "file.delete": "Delete",
        }[capability]
        return f"{verb} {resolve_path(str(params.get('path', ''))).display}"
    if capability == "file.move":
        src = resolve_path(str(params.get("source", ""))).display
        dst = resolve_path(str(params.get("destination", ""))).display
        return f"Move {src} to {dst}"
    if capability in ("browser.open", "net.fetch"):
        return f"Open {params.get('url', '')}"
    if capability in ("app.launch", "app.close", "app.focus"):
        verb = {"app.launch": "Open", "app.close": "Close", "app.focus": "Switch to"}[capability]
        return f"{verb} {params.get('app', params.get('window', ''))}"
    if capability == "system.power":
        return f"{str(params.get('action', '')).capitalize()} this computer"
    if capability == "screen.capture":
        target = params.get("target", "screen")
        return f"Capture {'the whole screen' if target in ('screen', 'full') else target}"
    if capability in ("input.type", "input.key"):
        return _describe_input(capability, params)
    if capability == "input.pointer":
        action = str(params.get("action", "move")).lower()
        at = ""
        if params.get("x") is not None and params.get("y") is not None:
            at = f" at {params['x']},{params['y']}"
        return f"{'Click' if action == 'click' else action.capitalize()} the mouse{at}"
    if capability == "window.manage":
        return f"{str(params.get('action', 'rearrange')).capitalize()} the current window"
    if capability == "media.control":
        return f"{str(params.get('action', 'control')).capitalize()} media playback"
    if capability == "system.volume":
        if str(params.get("action", "")).lower() == "mute":
            return "Mute the audio"
        level = params.get("level")
        return "Set the volume" + (f" to {level}%" if level is not None else "")
    if capability == "system.brightness":
        level = params.get("level")
        return "Set the screen brightness" + (f" to {level}%" if level is not None else "")
    if capability == "clipboard.write":
        return "Replace what is on your clipboard"
    if capability == "notify.send":
        title = str(params.get("title", "")).strip()
        return f"Show you a notification{': ' + title if title else ''}"
    if capability == "memory.write":
        subject = str(params.get("title") or params.get("body", "")).strip()
        return f"Remember: {subject[:80]}" if subject else "Save a new memory"
    if capability == "memory.delete":
        return "Delete a stored memory"
    if capability == "browser.search":
        return f"Search the web for: {str(params.get('query', '')).strip()}"

    # No template for this capability. Fall back to the catalogue's own sentence
    # rather than to the identifier: a dialog that says "app.list" tells the user
    # nothing they can consent to, which is the opaque prompt §6 exists to
    # prevent. Parameters are deliberately not appended -- "kind=x, limit=3" is
    # not a consequence, and any capability whose effect depends on its parameters
    # belongs in the list above, named.
    return capabilities.get(capability).description.rstrip(".")


def _describe_input(capability: str, params: dict[str, Any]) -> str:
    """Synthetic keyboard input, quoted so the user sees exactly what will be sent."""
    if capability == "input.key":
        keys = params.get("keys") or params.get("key") or ""
        if isinstance(keys, (list, tuple)):
            keys = "+".join(str(k) for k in keys)
        return f"Press {keys} in whatever window is focused"
    text = str(params.get("text", ""))
    # Truncated, because a page of pasted text must not push the dialog's buttons
    # off screen -- but the beginning is shown verbatim so consent is informed.
    shown = text if len(text) <= 60 else text[:57] + "..."
    return f'Type "{shown}" into whatever window is focused'
