"""The capability catalogue (task C1; spec §5, §6, §35).

A capability is the unit the user grants or denies. Tools declare which one they
need; nothing reaches the OS without a capability that the permission engine has
approved.

Design rules:

* **Closed set.** Adding a power means adding an entry here, so the Permissions
  screen can never be out of sync with what the system can actually do.
* **Read and write are separate.** ``file.read`` and ``file.write`` are distinct
  capabilities because granting "look at my documents" must not imply
  "rewrite my documents".
* **Prompt-worthiness is declared too.** A handful of reads describe the machine
  rather than the person (``system.info``, ``app.list``) and carry
  ``implicit=True``, which means the engine allows them without a dialog. The
  test is not "is this low risk" -- it is "could a dialog here ever be answered
  no". Everything that touches the user's content, or has an effect they would
  notice, asks.
* **Risk is declared, not inferred.** The dialog wording, the default grant
  kinds offered, and whether a second confirmation is required all derive from
  ``risk``, so a high-risk action cannot accidentally present a low-risk prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Risk = Literal["low", "medium", "high", "critical"]

# Which grant kinds the dialog may offer, by risk. Note that 'always' is not
# offered for critical actions: a standing grant to delete files or run shutdown
# is not something we let a single click establish (§6, §8).
GRANT_KINDS_BY_RISK: dict[Risk, tuple[str, ...]] = {
    "low": ("once", "session", "always"),
    "medium": ("once", "session", "always"),
    "high": ("once", "session"),
    "critical": ("once",),
}


@dataclass(frozen=True)
class Capability:
    name: str
    group: str
    title: str
    description: str
    risk: Risk
    reversible: bool
    # A human-readable statement of what granting this actually allows. Shown
    # verbatim in the permission dialog -- §6 requires the user understand the
    # real consequence, not a vague category.
    consequence: str
    scoped: bool = False  # True when grants can be narrowed (e.g. to a directory)
    # Allowed without a prompt. Reserved for reads that answer a question about
    # the *machine* rather than about the *person* using it -- information any
    # local process can already obtain and that reveals nothing about what the
    # user is doing. It is a narrow exemption, not a convenience: a dialog for
    # "how much disk is free" teaches people to click yes without reading, which
    # costs more safety than it buys (§8). A user can still deny these explicitly
    # from the Permissions screen, and that denial wins.
    implicit: bool = False


CAPABILITIES: tuple[Capability, ...] = (
    # ---------------------------------------------------------- applications
    Capability(
        name="app.list",
        group="Applications",
        title="See installed applications",
        description="Read which applications are installed on this computer.",
        risk="low",
        reversible=True,
        consequence=(
            "NEXUS can see which applications are installed. This is the same "
            "list your desktop menu shows, and says nothing about what you are "
            "doing with them."
        ),
        implicit=True,
    ),
    Capability(
        # Deliberately separate from app.list. Which applications exist is public
        # system metadata; which windows are open, and what their titles say, is a
        # running account of what the user is working on -- often the single most
        # revealing text on the machine. Granting "read my launcher menu" must not
        # imply "watch what I am doing", for the same reason file.read does not
        # imply file.write.
        name="window.list",
        group="Applications",
        title="See what you have open",
        description="Read the list of open windows and their titles.",
        risk="medium",
        reversible=True,
        consequence=(
            "NEXUS can see every open window's title, which usually reveals the "
            "documents, sites and conversations you have open."
        ),
    ),
    Capability(
        name="app.launch",
        group="Applications",
        title="Open applications",
        description="Start an installed application.",
        risk="low",
        reversible=True,
        consequence="NEXUS can start applications on this computer.",
        scoped=True,
    ),
    Capability(
        name="app.focus",
        group="Applications",
        title="Switch windows",
        description="Raise or focus an existing window.",
        risk="low",
        reversible=True,
        consequence="NEXUS can bring windows to the front, changing what you are looking at.",
    ),
    Capability(
        name="app.close",
        group="Applications",
        title="Close applications",
        description="Ask an application to quit.",
        risk="medium",
        reversible=False,
        consequence="NEXUS can close applications. Unsaved work in them may be lost.",
        scoped=True,
    ),
    # ----------------------------------------------------------------- files
    Capability(
        name="file.list",
        group="Files",
        title="Browse folders",
        description="List the contents of a directory.",
        risk="low",
        reversible=True,
        consequence="NEXUS can see the names of your files and folders in the granted location.",
        scoped=True,
    ),
    Capability(
        name="file.read",
        group="Files",
        title="Read files",
        description="Read the contents of a file.",
        risk="medium",
        reversible=True,
        consequence="NEXUS can read the full contents of files in the granted location, and that content may be sent to a cloud AI provider to answer your request.",
        scoped=True,
    ),
    Capability(
        name="file.write",
        group="Files",
        title="Create and edit files",
        description="Write a new file or modify an existing one.",
        risk="high",
        reversible=False,
        consequence="NEXUS can create files and overwrite the contents of existing ones in the granted location.",
        scoped=True,
    ),
    Capability(
        name="file.move",
        group="Files",
        title="Move and rename files",
        description="Move or rename a file or folder.",
        risk="high",
        reversible=False,
        consequence="NEXUS can move and rename your files, which changes where you find them.",
        scoped=True,
    ),
    Capability(
        name="file.delete",
        group="Files",
        title="Delete files",
        description="Send a file to trash, or delete it permanently.",
        risk="critical",
        reversible=False,
        consequence="NEXUS can delete your files. Deletion goes to the trash where possible, but may be permanent.",
        scoped=True,
    ),
    # -------------------------------------------------------------- terminal
    Capability(
        name="terminal.execute",
        group="Terminal",
        title="Run terminal commands",
        description="Execute a shell command on this computer.",
        risk="critical",
        reversible=False,
        consequence="NEXUS can run commands with your user account's full permissions. A command can read, change or destroy anything your account can reach.",
        scoped=True,
    ),
    # --------------------------------------------------------------- browser
    Capability(
        name="browser.open",
        group="Browser",
        title="Open web pages",
        description="Open a URL in your default browser.",
        risk="low",
        reversible=True,
        consequence="NEXUS can open web pages in your browser.",
        scoped=True,
    ),
    Capability(
        name="browser.search",
        group="Browser",
        title="Search the web",
        description="Run a web search and read the results.",
        risk="medium",
        reversible=True,
        consequence="NEXUS can send search queries to a third-party search provider and read the results.",
    ),
    Capability(
        name="net.fetch",
        group="Browser",
        title="Fetch web content",
        description="Download and read the content of a web page or API.",
        risk="medium",
        reversible=True,
        consequence="NEXUS can request content from addresses on the internet and read what comes back.",
        scoped=True,
    ),
    # Separate from browser.open and net.fetch because those are one-shot and
    # blind: they ask the desktop for a page, or download one, and then know
    # nothing about it. This one holds a live, stateful session that can type
    # into fields and press buttons, which is a different power with a different
    # consequence, and folding it into either of the others would hand it over
    # on a consent the user gave for something smaller.
    #
    # ``scoped=False`` is deliberate. Scoping it to a hostname would look
    # reassuring and be false: the scope for a ``browser.navigate`` is the
    # address it was given, but a following ``browser.click`` on a link leaves
    # that host and nothing re-derives a scope for it -- the grant would name a
    # domain the session is no longer on. High risk already means the grant
    # kinds are ("once", "session"), so the wide scope is time-bounded rather
    # than permanent, which is the honest shape for a power this broad.
    Capability(
        name="browser.drive",
        group="Browser",
        title="Control a web browser",
        description=(
            "Open pages in a browser NEXUS controls, read what they render, and click "
            "and type on them."
        ),
        risk="high",
        reversible=False,
        consequence=(
            "NEXUS can read and change the contents of web pages, including submitting "
            "forms and pressing buttons that spend money, send messages or delete "
            "things. It uses its own browser with no logins of yours in it, so it "
            "cannot act as you on sites you are signed in to -- but a site that needs "
            "no login can still be acted on."
        ),
        scoped=False,
    ),
    # ---------------------------------------------------------------- system
    Capability(
        name="system.info",
        group="System",
        title="Read system status",
        description="Read battery, CPU, memory, disk and network status.",
        risk="low",
        reversible=True,
        consequence=(
            "NEXUS can read this computer's hardware and resource status: battery, "
            "processor load, free memory and disk space."
        ),
        # Machine state, not personal state. Prompting for "how much disk is free"
        # before answering a question about disk space is a dialog whose only
        # possible answer is yes.
        implicit=True,
    ),
    Capability(
        name="system.volume",
        group="System",
        title="Change volume",
        description="Adjust or mute the system audio volume.",
        risk="low",
        reversible=True,
        consequence="NEXUS can change how loud this computer is.",
    ),
    Capability(
        name="system.brightness",
        group="System",
        title="Change screen brightness",
        description="Adjust display brightness.",
        risk="low",
        reversible=True,
        consequence="NEXUS can change your screen brightness.",
    ),
    Capability(
        name="system.power",
        group="System",
        title="Lock, sleep, restart or shut down",
        description="Trigger a session or power state change.",
        risk="critical",
        reversible=False,
        consequence="NEXUS can lock, suspend, restart or shut down this computer. Unsaved work in any application may be lost.",
        scoped=True,
    ),
    Capability(
        name="system.processes",
        group="System",
        title="See what is running",
        description="List running processes with their memory and CPU use.",
        # Not implicit, and not low, for the same reason `window.list` is not:
        # a process list is not just machine state. Every entry carries the
        # command line the program was started with, and those name the files,
        # sites and accounts being worked on. `system.info` is a battery
        # percentage; this is a description of the day.
        risk="medium",
        reversible=True,
        consequence=(
            "NEXUS can see every program running on this computer, including the command "
            "line each one was started with -- which often names the files, sites and "
            "accounts you are working with."
        ),
    ),
    Capability(
        name="system.kill_process",
        group="System",
        title="End a running process",
        description="Send a running process a signal that stops it.",
        risk="high",
        reversible=False,
        consequence=(
            "NEXUS can end a running program. Anything unsaved in it is lost, and a "
            "background service ended this way stays down until something starts it again."
        ),
    ),
    Capability(
        name="system.process.signal",
        group="System",
        title="Pause, resume or reprioritize a process",
        description="Freeze a running process, unfreeze one, or change how much processor time it is given.",
        # Not marked reversible even though freezing (SIGSTOP) is undone by resuming
        # (SIGCONT): a process frozen while it holds a lock or an open connection can
        # hang whatever is waiting on it in the meantime, and *lowering* a process's
        # priority cannot be raised back without administrator rights. Claiming these
        # effects are cleanly undoable would be the over-statement the dialog exists to
        # avoid. Distinct from system.kill_process: this does not end the program.
        risk="high",
        reversible=False,
        consequence=(
            "NEXUS can freeze a running program so it stops using the processor, resume "
            "one that was frozen, or change how much processor time a program is given. A "
            "frozen program does nothing at all until it is resumed, which can make "
            "anything waiting on it stop responding too."
        ),
    ),
    Capability(
        name="system.service.read",
        group="System",
        title="See background services",
        description="Read which system services exist and whether they are running.",
        risk="low",
        reversible=True,
        consequence=(
            "NEXUS can see which background services this computer has and whether each "
            "is running. This is machine state any local account can already read with "
            "systemctl, and says nothing about what you are doing."
        ),
        implicit=True,
    ),
    Capability(
        name="system.packages.read",
        group="System",
        title="See installed packages",
        description="Read which software packages are installed and search the repositories.",
        risk="low",
        reversible=True,
        consequence=(
            "NEXUS can see which software packages are installed and search for available "
            "ones. This is the same list dpkg and apt show, not what you are doing with "
            "that software."
        ),
        implicit=True,
    ),
    Capability(
        name="system.cron.read",
        group="System",
        title="See scheduled jobs",
        description="Read this account's scheduled (cron) jobs.",
        # Not implicit: a crontab is the user's *own* automation configuration, not
        # machine metadata. Entries name the scripts and commands that run on a
        # schedule and can embed credentials -- the kind of personal detail that
        # should ask before being read, for the same reason file.read asks.
        risk="low",
        reversible=True,
        consequence=(
            "NEXUS can read this account's scheduled jobs. Entries name the scripts and "
            "commands that run automatically, and may contain credentials."
        ),
    ),
    Capability(
        name="clipboard.read",
        group="System",
        title="Read the clipboard",
        description="Read the current clipboard contents.",
        risk="medium",
        reversible=True,
        consequence="NEXUS can read whatever you last copied, which may include passwords.",
    ),
    Capability(
        name="clipboard.write",
        group="System",
        title="Set the clipboard",
        description="Replace the clipboard contents.",
        risk="low",
        reversible=False,
        consequence="NEXUS can replace what is on your clipboard, discarding what was there.",
    ),
    Capability(
        name="notify.send",
        group="System",
        title="Send desktop notifications",
        description="Post a desktop notification.",
        risk="low",
        reversible=True,
        consequence="NEXUS can show notifications on your desktop.",
    ),
    # ---------------------------------------------------------------- screen
    Capability(
        name="screen.capture",
        group="Screen",
        title="Capture the screen",
        description="Take a screenshot of a window or the whole screen.",
        risk="high",
        reversible=True,
        consequence="NEXUS can capture an image of your screen, including anything visible on it, and that image may be sent to a cloud AI provider to answer your request.",
        scoped=True,
    ),
    # ----------------------------------------------------------------- input
    # Synthesised input is the most powerful capability NEXUS has, because it can
    # reach anything the user can reach: a keystroke is indistinguishable from a
    # real one to every application on the machine. The consequence text says
    # exactly that, and none of these are grantable as "always" (they are high
    # risk, so RISK_GRANT_KINDS offers only once/session).
    Capability(
        name="input.type",
        group="Input",
        title="Type on your keyboard",
        description="Send text to whatever window has focus.",
        risk="high",
        reversible=False,
        consequence="NEXUS can type into whatever window is focused, exactly as if you typed it. It cannot tell a password field from any other field, and typed text cannot be taken back.",
    ),
    Capability(
        name="input.key",
        group="Input",
        title="Press keys and shortcuts",
        description="Send key presses and keyboard shortcuts to the focused window.",
        risk="high",
        reversible=False,
        consequence="NEXUS can press any key or shortcut, including ones that save, delete, close or submit. The effect depends entirely on what is focused at the time.",
    ),
    Capability(
        name="input.pointer",
        group="Input",
        title="Move and click the mouse",
        description="Move the pointer, click, and scroll.",
        risk="high",
        reversible=False,
        consequence="NEXUS can move your pointer and click anywhere on screen, including buttons that confirm or delete things. It clicks a coordinate, not a button it can read.",
    ),
    # ---------------------------------------------------------------- windows
    Capability(
        name="window.manage",
        group="Applications",
        title="Move and resize windows",
        description="Move, resize, minimise, maximise or move windows between workspaces.",
        risk="low",
        reversible=True,
        consequence="NEXUS can rearrange your windows. Nothing is lost, but what you are looking at will change.",
    ),
    # ----------------------------------------------------------------- media
    Capability(
        name="media.control",
        group="System",
        title="Control playback",
        description="Play, pause, skip or stop media players.",
        risk="low",
        reversible=True,
        consequence="NEXUS can pause or skip whatever is playing.",
    ),
    # ---------------------------------------------------------------- memory
    Capability(
        name="memory.write",
        group="Memory",
        title="Remember things",
        description="Store a new long-term memory.",
        risk="low",
        reversible=True,
        consequence="NEXUS can save facts about you to its long-term memory, where they will be used in future conversations.",
    ),
    Capability(
        name="memory.read",
        group="Memory",
        title="Read remembered things",
        description="Read the long-term memories NEXUS has stored.",
        risk="low",
        reversible=True,
        consequence="NEXUS can read the facts it has stored about you in long-term memory. This is your own memory, not anyone else's.",
        implicit=True,
    ),
    Capability(
        name="memory.delete",
        group="Memory",
        title="Forget things",
        description="Permanently delete stored memories.",
        risk="high",
        reversible=False,
        consequence="NEXUS can permanently delete stored memories. Deleted memories cannot be recovered.",
    ),
    # -- automations ---------------------------------------------------------
    # The system-control audit's gap 3: automations were reachable only through
    # the UI, so "every evening at seven, remind me to study" could be *discussed*
    # by the assistant and not *done* by it. These two capabilities close that.
    #
    # The risk split is the point. Reading the list is low -- it is the user's own
    # schedule, and refusing to show it while happily creating entries in it would
    # be the wrong way round. Creating or changing one is **medium**, deliberately
    # higher than `memory.write`, because an automation is consent that *fires
    # later*: a memory written wrongly is a sentence the user can correct next time
    # they see it, while an automation written wrongly acts on its own schedule
    # whether or not anyone is watching.
    #
    # What stops this becoming a hole in the permission engine is that an automation
    # cannot grant powers its author did not hold. `capability_scope` records what
    # the rule was armed with, and the engine re-checks it at every firing -- so a
    # rule the model creates carries the model's scope, and a rule edited to need
    # more power than the stored scope is disarmed and labelled rather than
    # inheriting the trust the narrower version earned (schema v4).
    # The audit recorded this observation kind as not existing at all. Reading other
    # applications' notifications is among the most personal things on a desktop --
    # codes, messages, medical results -- so it is medium rather than low, and every
    # monitor that reads content defaults off (§56).
    Capability(
        name="notifications.read",
        group="Awareness",
        title="Read notifications",
        description="Watch the notifications other applications show you.",
        risk="medium",
        reversible=True,
        consequence=(
            "NEXUS can read the notifications other applications show you, including "
            "their text. Anything that looks like a password or key is counted but "
            "never stored."
        ),
    ),
    Capability(
        name="context.read",
        group="Awareness",
        title="Read application context",
        description="Get contextual information about the current application and recent activity.",
        risk="low",
        reversible=True,
        consequence=(
            "NEXUS can read your recent window activity and application usage to provide "
            "contextual assistance. This is derived from the window monitor you already "
            "enabled."
        ),
    ),
    Capability(
        name="automation.read",
        group="Automations",
        title="See your automations",
        description="List the scheduled rules and when they last ran.",
        risk="low",
        reversible=True,
        consequence=(
            "NEXUS can see the automations you have set up, their schedules, and "
            "whether their recent runs succeeded."
        ),
    ),
    Capability(
        name="automation.write",
        group="Automations",
        title="Create and change automations",
        description="Create, enable, disable or delete a scheduled rule.",
        risk="medium",
        reversible=True,
        consequence=(
            "NEXUS can create rules that run on a schedule without asking again at "
            "the time they fire, and can disable or delete rules you already have. "
            "A rule can never do more than you have allowed NEXUS to do."
        ),
    ),
)


BY_NAME: dict[str, Capability] = {c.name: c for c in CAPABILITIES}

GROUPS: tuple[str, ...] = tuple(
    dict.fromkeys(c.group for c in CAPABILITIES)  # preserves declaration order
)


def get(name: str) -> Capability:
    """Look up a capability.

    Raises rather than returning a permissive default: an unknown capability is a
    programming error, and guessing would mean executing something the user was
    never asked about (§5).
    """
    try:
        return BY_NAME[name]
    except KeyError:
        raise KeyError(
            f"unknown capability {name!r}; register it in nexus.permissions.capabilities"
        ) from None


def exists(name: str) -> bool:
    return name in BY_NAME


def by_group() -> dict[str, list[Capability]]:
    grouped: dict[str, list[Capability]] = {group: [] for group in GROUPS}
    for cap in CAPABILITIES:
        grouped[cap.group].append(cap)
    return grouped


def offered_grant_kinds(name: str) -> tuple[str, ...]:
    return GRANT_KINDS_BY_RISK[get(name).risk]


def requires_second_confirmation(name: str) -> bool:
    """Critical, irreversible actions get an explicit second step (§8, §62)."""
    cap = get(name)
    return cap.risk == "critical" and not cap.reversible


def to_dict(cap: Capability) -> dict[str, object]:
    return {
        "name": cap.name,
        "group": cap.group,
        "title": cap.title,
        "description": cap.description,
        "risk": cap.risk,
        "reversible": cap.reversible,
        "consequence": cap.consequence,
        "scoped": cap.scoped,
        # Surfaced so the Permissions screen can show these as already-allowed
        # with the reason, instead of listing them as though a prompt were coming.
        "implicit": cap.implicit,
        "grant_kinds": GRANT_KINDS_BY_RISK[cap.risk],
        "second_confirmation": requires_second_confirmation(cap.name),
    }


def is_implicit(name: str) -> bool:
    """Whether this capability is allowed without asking. See ``Capability``."""
    return get(name).implicit


def catalogue() -> list[dict[str, object]]:
    """Serialisable catalogue for the Permissions screen (§43)."""
    return [to_dict(c) for c in CAPABILITIES]
