"""Terminal execution (task D4; spec §36, §58, §60, §79).

This is the most dangerous tool in the system and it is written to be honest
about that rather than to feel safe.

What it really does: runs the command through ``bash -c`` as the logged-in user,
in a new process group, with output streamed to the event bus as it arrives. What
it does **not** do: sandbox anything. There is no container, no seccomp filter,
no restricted user. §78 forbids claiming a safety property that does not exist,
so the permission dialog for ``terminal.execute`` says outright that an accepted
command has the user's full account rights.

The protections that do exist are real and narrow:

* ``policy.terminal_refusal`` rejects the specific catastrophic shapes a language
  model is most likely to emit, before any permission prompt appears.
* Every run happens in its own process group, so a timeout kills the whole tree
  rather than orphaning children.
* Output is bounded, and truncation is reported instead of hidden.
* Exit codes are reported verbatim -- a non-zero exit is a failure, never
  smoothed over (§79).
"""

from __future__ import annotations

import os
import selectors
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from ..config import settings
from ..core.events import bus
from ..logging_setup import get_logger
from ..permissions import policy
from .registry import ToolResult, tool

log = get_logger("tools.terminal")

# Beyond this, output stops being information and starts being a denial of
# service on the UI and on the model's context window.
MAX_OUTPUT_CHARS = 120_000
MAX_LINE_CHARS = 4_000

# Commands still running, so a user can stop one (§17, §79).
_RUNNING: dict[str, subprocess.Popen[bytes]] = {}


def _shell() -> str:
    return shutil.which("bash") or "/bin/sh"


def _clean_env() -> dict[str, str]:
    """Inherit the user's environment, minus interactivity and colour noise."""
    env = dict(os.environ)
    env["TERM"] = "dumb"
    env["NO_COLOR"] = "1"
    env["CLICOLOR"] = "0"
    # Stop pagers from blocking forever waiting for a keypress that never comes.
    env["PAGER"] = "cat"
    env["GIT_PAGER"] = "cat"
    env["SYSTEMD_PAGER"] = "cat"
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _resolve_cwd(raw: str | None) -> tuple[Path | None, str | None]:
    if not raw:
        return Path.home(), None
    resolved = policy.resolve_path(str(raw))
    if not resolved.exists:
        return None, f"The folder {resolved.display} does not exist, so the command has nowhere to run."
    if not resolved.is_dir:
        return None, f"{resolved.display} is a file, not a folder."
    refusal = policy.path_refusal(str(raw), write=False)
    if refusal is not None:
        return None, refusal.reason
    return resolved.path, None


def _stream(
    proc: subprocess.Popen[bytes],
    run_id: str,
    deadline: float,
    task_id: str | None,
) -> tuple[str, str, bool, bool]:
    """Read both pipes until exit or deadline.

    Returns (stdout, stderr, truncated, timed_out). Reading both descriptors with
    a selector keeps the real interleaving of a command's progress while still
    telling the two streams apart, which matters when a command succeeds but
    warns.
    """
    assert proc.stdout is not None and proc.stderr is not None
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, "stdout")
    sel.register(proc.stderr, selectors.EVENT_READ, "stderr")

    chunks: dict[str, list[str]] = {"stdout": [], "stderr": []}
    total = 0
    truncated = False
    timed_out = False
    pending: dict[str, str] = {"stdout": "", "stderr": ""}

    while sel.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break

        for key, _ in sel.select(timeout=min(remaining, 0.5)):
            stream_name = key.data
            raw = key.fileobj.read1(8192)  # type: ignore[union-attr]
            if not raw:
                sel.unregister(key.fileobj)
                if pending[stream_name]:
                    _emit(run_id, stream_name, pending[stream_name], task_id)
                    pending[stream_name] = ""
                continue

            text = raw.decode("utf-8", errors="replace")
            if total < MAX_OUTPUT_CHARS:
                room = MAX_OUTPUT_CHARS - total
                kept = text[:room]
                chunks[stream_name].append(kept)
                total += len(kept)
                if len(kept) < len(text):
                    truncated = True
            else:
                truncated = True

            # Emit whole lines as they complete so the UI shows live progress
            # instead of a spinner that unblocks all at once (§17).
            pending[stream_name] += text
            while "\n" in pending[stream_name]:
                line, _, pending[stream_name] = pending[stream_name].partition("\n")
                _emit(run_id, stream_name, line, task_id)

        if proc.poll() is not None and not sel.get_map():
            break

    sel.close()
    return "".join(chunks["stdout"]), "".join(chunks["stderr"]), truncated, timed_out


def _emit(run_id: str, stream: str, line: str, task_id: str | None) -> None:
    if not line:
        return
    if len(line) > MAX_LINE_CHARS:
        line = line[:MAX_LINE_CHARS] + f" … ({len(line)} characters in this line)"
    bus.publish_threadsafe(
        "terminal.output",
        {"run_id": run_id, "stream": stream, "line": line, "task_id": task_id},
    )


def _terminate(proc: subprocess.Popen[bytes]) -> str:
    """SIGTERM the whole group, then SIGKILL what survives."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return "already gone"
    try:
        proc.wait(timeout=3)
        return "stopped with SIGTERM"
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=3)
        return "force-killed with SIGKILL"
    except (ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired):
        return "could not be killed"


@tool(
    "terminal.run",
    title="Run a terminal command",
    description=(
        "Run a shell command on this computer and return its output and exit "
        "code. The command runs with your user account's full permissions. Use "
        "this only when no dedicated tool can do the job, and prefer the "
        "narrowest command that answers the question."
    ),
    capability="terminal.execute",
    idempotent=False,
    read_only=False,
    # The runtime's own timeout must sit *above* the largest command timeout this
    # tool accepts. If the outer wait_for fired first, the runtime would report a
    # timeout while this function was still shepherding a live process -- and
    # nobody would ever kill it.
    timeout_s=630.0,
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "minLength": 1,
                "maxLength": 8000,
                "description": "The shell command to run.",
            },
            "cwd": {
                "type": "string",
                "description": "Folder to run in. Defaults to your home folder.",
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": 600,
                "description": "How long to wait before stopping the command.",
            },
        },
        "required": ["command"],
    },
)
def terminal_run(params: dict[str, Any]) -> ToolResult:
    command = str(params["command"]).strip()

    # Checked again here, not only in the permission engine: this function is the
    # last point before a shell exists, and a refusal must not depend on the
    # caller having gone through the engine.
    refusal = policy.terminal_refusal(command)
    if refusal is not None:
        return ToolResult(ok=False, error=f"{refusal.reason} {refusal.detail}".strip(),
                          summary=refusal.reason)

    cwd, problem = _resolve_cwd(params.get("cwd"))
    if problem is not None:
        return ToolResult(ok=False, error=problem, summary=problem)

    timeout = float(params.get("timeout_seconds") or settings().terminal_timeout_s)
    run_id = uuid.uuid4().hex[:12]

    bus.publish_threadsafe(
        "terminal.started",
        {"run_id": run_id, "command": command, "cwd": str(cwd), "timeout_seconds": timeout},
    )

    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            [_shell(), "-c", command],
            cwd=str(cwd),
            env=_clean_env(),
            stdin=subprocess.DEVNULL,  # a command that waits for input would hang forever
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,  # own process group, so a timeout kills the tree
        )
    except (OSError, ValueError) as exc:
        return ToolResult(ok=False, error=f"Could not start the command: {exc}")

    _RUNNING[run_id] = proc
    try:
        stdout, stderr, truncated, timed_out = _stream(
            proc, run_id, started + timeout, None
        )
        kill_note = ""
        if timed_out:
            kill_note = _terminate(proc)
            exit_code = proc.returncode
        else:
            try:
                exit_code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Pipes closed but the process lingers: usually a daemonised child.
                kill_note = _terminate(proc)
                exit_code = proc.returncode
    finally:
        _RUNNING.pop(run_id, None)

    duration_ms = int((time.monotonic() - started) * 1000)
    data = {
        "run_id": run_id,
        "command": command,
        "cwd": str(cwd),
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": truncated,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
    }
    bus.publish_threadsafe(
        "terminal.finished",
        {"run_id": run_id, "exit_code": exit_code, "timed_out": timed_out,
         "duration_ms": duration_ms},
    )

    if timed_out:
        note = f" The command was {kill_note}." if kill_note else ""
        return ToolResult(
            ok=False,
            data=data,
            error=(
                f"The command was still running after {timeout:.0f} seconds, so NEXUS stopped it.{note} "
                "Anything it had already changed stays changed."
            ),
            summary=f"Timed out after {timeout:.0f}s: {command}",
        )

    if exit_code != 0:
        # The command's own report of failure is the truth. Do not soften it.
        detail = (stderr or stdout).strip().splitlines()
        first = detail[-1] if detail else "no output"
        return ToolResult(
            ok=False,
            data=data,
            error=f"The command exited with code {exit_code}. Last output: {first}",
            summary=f"Failed (exit {exit_code}): {command}",
        )

    line_count = stdout.count("\n") + (1 if stdout and not stdout.endswith("\n") else 0)
    summary = f"Ran: {command}"
    if truncated:
        summary += f" (output truncated at {MAX_OUTPUT_CHARS:,} characters)"
    return ToolResult(
        ok=True,
        data=data,
        summary=summary,
        # The exit code *is* the verification for a shell command; there is no
        # independent check to run, and pretending otherwise would be false.
        verified=True,
        verification=f"The command exited 0 after {duration_ms} ms with {line_count} line(s) of output.",
    )


def running() -> list[dict[str, Any]]:
    return [{"run_id": rid, "pid": p.pid} for rid, p in _RUNNING.items() if p.poll() is None]


def cancel(run_id: str) -> dict[str, Any]:
    """Stop a running command (§17: the user can always interrupt)."""
    proc = _RUNNING.get(run_id)
    if proc is None:
        return {"stopped": False, "reason": "That command is not running any more."}
    note = _terminate(proc)
    # Drop it from the registry now rather than waiting for run()'s finally to
    # notice the pipes closed. run() pops with a default too, so a double-pop is
    # a no-op; the point is that a cancelled command must not linger in _RUNNING
    # where a following cancel_all() would "stop" the corpse a second time and
    # report a count of commands that were already gone.
    _RUNNING.pop(run_id, None)
    bus.publish_threadsafe("terminal.cancelled", {"run_id": run_id, "detail": note})
    return {"stopped": True, "detail": note}


def cancel_all() -> int:
    stopped = 0
    for run_id in list(_RUNNING):
        if cancel(run_id)["stopped"]:
            stopped += 1
    return stopped
