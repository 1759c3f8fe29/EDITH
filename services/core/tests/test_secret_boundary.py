"""Credentials must not cross the two boundaries that leave this process (§56).

``redact`` has existed for a long time and was wired into exactly one place: the log
filter. Meanwhile ``terminal.run`` returns its whole stdout and ``files.read`` returns
the whole file, both under ``data``, and ``_payload`` serialised that straight into the
``role="tool"`` message. So "read my .env" or "run env" sent every credential on the
machine to a cloud provider on every subsequent round of the turn -- and invisibly,
because ``ToolRun.to_dict`` does not carry ``data``, so neither the activity feed nor
the stored conversation ever showed what was sent.

These tests are about the boundary, not about the scrubber. ``redact``'s own patterns
are tested elsewhere; what was missing was any test that the scrubber is *called* where
it matters. Two of them also pin decisions that a later simplification would quietly
undo: that the walk happens over the structure rather than over the serialised JSON,
and that it happens before truncation rather than after.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus import logging_setup
from nexus.ai import core
from nexus.api.routes import system as system_routes
from nexus.logging_setup import REDACTED, redact_data
from nexus.tools import registry, runtime

# Shaped like the real thing so the fallback patterns fire, but not real.
OPENAI = "sk-" + "B" * 40
NVIDIA = "nvapi-" + "C" * 40
GOOGLE = "AIza" + "D" * 30


def _sent_to_model(data: dict[str, object]) -> str:
    """What ``_payload`` hands to the model for a tool result."""
    return core._payload(data)


# --------------------------------------------------------- the live leak, both tools

def test_reading_an_env_file_does_not_send_the_keys_to_the_model() -> None:
    """``files.read`` on a credentials file is the exact shape of the leak."""
    body = "\n".join(
        [
            "# NEXUS credentials",
            f"OPENAI_API_KEY={OPENAI}",
            f"NVIDIA_API_KEY={NVIDIA}",
            f"GOOGLE_API_KEY={GOOGLE}",
            "NEXUS_DB_PATH=/home/u/.local/share/nexus/nexus.db",
        ]
    )
    out = _sent_to_model(
        {"ok": True, "ran": True, "data": {"path": "/home/u/EDITH/.env", "content": body}}
    )
    for secret in (OPENAI, NVIDIA, GOOGLE):
        assert secret not in out, "a credential reached the model context"
    assert REDACTED in out, "scrubbed, not merely absent by accident"
    # The non-secret line is what makes the result still worth sending.
    assert "NEXUS_DB_PATH" in out


def test_a_command_that_prints_the_environment_does_not_reach_the_model() -> None:
    """``terminal.run`` carries the command *and* stdout *and* stderr."""
    out = _sent_to_model(
        {
            "ok": True,
            "ran": True,
            "data": {
                "command": f"curl -H 'Authorization: Bearer {OPENAI}' https://api.x/v1",
                "stdout": f"NVIDIA_API_KEY={NVIDIA}\nHOME=/home/u\n",
                "stderr": f"401 unauthorized for key {GOOGLE}",
                "exit_code": 1,
            },
        }
    )
    for secret in (OPENAI, NVIDIA, GOOGLE):
        assert secret not in out
    assert "HOME=/home/u" in out, "the useful part of stdout must survive"


def test_an_error_string_from_a_provider_is_scrubbed_too() -> None:
    """Not just ``data``: a provider's error body can echo the key it rejected."""
    out = _sent_to_model({"ok": False, "ran": True, "error": f"invalid api key {OPENAI}"})
    assert OPENAI not in out


def test_a_registered_credential_with_no_recognisable_shape_is_still_scrubbed() -> None:
    """The two halves of the scrubber are separate, and this is the registered half.

    A key that matches none of the patterns -- a bare hex blob, say -- is only caught
    because the secrets layer registered it. Without this the payload path would rely
    entirely on shape matching, and shape matching cannot know a password.
    """
    opaque = "9f3ca2b71e4d8065af12"
    logging_setup.register_secret(opaque)
    try:
        out = _sent_to_model({"ok": True, "data": {"stdout": f"pass is {opaque} ok"}})
        assert opaque not in out
        assert REDACTED in out
    finally:
        logging_setup.forget_secret(opaque)


# ----------------------------------------------- the two decisions worth pinning

def test_the_walk_is_over_the_leaves_and_not_the_serialised_json() -> None:
    """Scrubbing ``json.dumps(...)`` output would be safe and useless.

    Inside JSON a newline is the two characters backslash-n, so the ``KEY=value``
    pattern's value run -- which stops at whitespace -- does not stop at the end of the
    line. One ``API_KEY=`` near the top of a long stdout would swallow every line after
    it. This test fails if someone "simplifies" ``redact_data`` into a single
    ``redact(json.dumps(data))``.
    """
    stdout = f"API_KEY={OPENAI}\nPATH=/usr/local/bin\nUSER=santosh\nSHELL=/bin/bash\n"
    out = _sent_to_model({"ok": True, "data": {"stdout": stdout}})
    assert OPENAI not in out
    for survivor in ("/usr/local/bin", "USER", "santosh", "/bin/bash"):
        assert survivor in out, f"{survivor!r} was collateral damage of the redaction"


def test_a_key_straddling_the_truncation_limit_leaves_no_fragment() -> None:
    """Redact first, then truncate -- the other order leaves the head of the key.

    A fragment short enough to fall under the pattern's minimum length is no longer
    recognisable as a key, so a cut-then-scrub implementation cannot catch it. The
    surviving prefix here is ``sk-`` plus ten characters, and the pattern needs
    sixteen, so this test distinguishes the two orders rather than merely asserting
    that truncation happens.
    """
    keep = core.MAX_TOOL_RESULT_CHARS - 200

    def raw(pre: str, post: str) -> str:
        return json.dumps({"ok": True, "data": {"content": pre + OPENAI + post}}, default=str)

    overhead = raw("", "").index(OPENAI)
    # The filler ends in a space, and that is load-bearing rather than cosmetic. The
    # shape patterns are anchored with ``\b``, so a key glued directly to a preceding
    # word character is not matched at all -- writing this test with a bare run of
    # ``x`` made it fail for that reason instead of the ordering reason, which would
    # have been a mutant killed for the wrong reason and worth nothing. See
    # ``test_a_shape_matched_key_needs_a_boundary_before_it`` for that limitation,
    # pinned deliberately.
    pre = "x" * (keep - 14 - overhead) + " "
    post = "y" * 500
    unredacted = raw(pre, post)

    # The construction is asserted, not assumed: hand-placed offsets are wrong more
    # often than not, and a test whose premise silently stopped holding proves nothing.
    assert unredacted.index(OPENAI) == keep - 13, "the key is not where this test needs it"
    assert unredacted.index(OPENAI) + len(OPENAI) > keep, "the key does not straddle the cut"
    assert len(unredacted) > core.MAX_TOOL_RESULT_CHARS, "truncation would not even fire"

    out = core._payload({"ok": True, "data": {"content": pre + OPENAI + post}})
    assert OPENAI[:13] not in out, "the head of the key survived the cut"
    assert REDACTED in out


# ------------------------------------------------------------- shape is preserved

@pytest.mark.parametrize(
    "value",
    [0, 1, -3, 4.5, True, False, None],
    ids=["zero", "one", "negative", "float", "true", "false", "none"],
)
def test_a_non_string_scalar_passes_through_unchanged(value: object) -> None:
    """Not stringified. ``exit_code: 0`` must not become ``exit_code: "0"``.

    ``0`` and ``False`` are here on purpose: a falsy-check instead of a type-check is
    the natural way to write this wrong, and it would turn a successful exit code into
    a string the model then reasons about as text.
    """
    out = redact_data({"k": value})
    assert out == {"k": value}
    assert type(out["k"]) is type(value)


def test_the_structure_survives_the_walk() -> None:
    nested = {
        "a": {"b": [{"c": f"key {OPENAI}"}, 2, None]},
        "list": ["plain", f"{NVIDIA}"],
        "n": 7,
    }
    out = redact_data(nested)
    assert out["n"] == 7
    assert out["a"]["b"][1] == 2 and out["a"]["b"][2] is None
    assert OPENAI not in out["a"]["b"][0]["c"] and REDACTED in out["a"]["b"][0]["c"]
    assert out["list"][0] == "plain"
    assert NVIDIA not in out["list"][1]


def test_a_tuple_becomes_a_list_and_keeps_its_items() -> None:
    """Documented: both consumers serialise to JSON, where the two are the same."""
    out = redact_data({"t": ("plain", 1, f"{OPENAI}")})
    assert isinstance(out["t"], list)
    assert out["t"][0] == "plain" and out["t"][1] == 1
    assert OPENAI not in out["t"][2]


def test_dictionary_keys_are_left_alone_so_no_entry_is_merged_away() -> None:
    """Two keys that would redact to the same string must stay two keys.

    Redacting keys looks safer and loses data: both of these collapse to
    ``[redacted]`` and one entry silently overwrites the other, inside a function whose
    job is to remove only the secret parts.

    The keys are *bare* credentials rather than ``f"first-{OPENAI}"`` and
    ``f"second-{NVIDIA}"``, and that is the test rather than a detail. A distinguishing
    prefix sits outside the redacted span, so prefixed keys come back as
    ``first-[redacted]`` and ``second-[redacted]`` -- still two entries, so the dict
    never collapses and a key-redacting mutant survives. This test was written that way
    first and proved nothing: the collapse has to be *constructible* for the count
    assertion to mean anything.

    Leaving keys unscrubbed is safe because the keys of a tool payload are field names
    this codebase writes -- ``stdout``, ``content``, ``command`` -- never user data.
    """
    out = redact_data({OPENAI: 1, NVIDIA: 2})
    assert len(out) == 2, "an entry was lost to key redaction"
    assert sorted(out.values()) == [1, 2]
    assert set(out) == {OPENAI, NVIDIA}, "a key was redacted"


def test_a_pathologically_deep_payload_terminates_and_still_scrubs() -> None:
    """The depth cap is a stack guard, and must not become a way past the scrubber."""
    deep: object = f"bottom {OPENAI}"
    for _ in range(60):
        deep = {"down": deep}
    out = json.dumps(redact_data(deep), default=str)
    assert OPENAI not in out
    assert REDACTED in out


def test_a_shape_matched_key_needs_a_boundary_before_it() -> None:
    """A recorded limitation of the *fallback* half, not a defect to be "fixed".

    ``_PATTERNS`` anchors each shape with ``\b``, so ``sk-...`` glued to a preceding
    word character is missed. Dropping the anchor would be worse than the gap: ``sk-``
    occurs inside ordinary words -- ``task-force``, ``risk-register`` -- so an unanchored
    pattern turns any hyphenated word followed by sixteen characters into a redaction,
    and a scrubber that eats normal output gets switched off.

    What makes the gap acceptable is that it applies only to *foreign* keys of a
    recognised shape. NEXUS's own credentials go through ``register_secret``, whose
    match is a plain substring replace with no boundary condition -- so they are caught
    however they are surrounded. This test pins both halves of that trade.
    """
    glued = f"prefix{OPENAI}"
    assert OPENAI in redact_data({"s": glued})["s"], (
        "if this now redacts, check the pattern did not lose its \\b anchor"
    )

    logging_setup.register_secret(OPENAI)
    try:
        out = redact_data({"s": glued})["s"]
        assert OPENAI not in out, "a registered secret must be caught regardless of adjacency"
    finally:
        logging_setup.forget_secret(OPENAI)


# --------------------------------------------- the second boundary: a user-facing route

def test_the_system_status_route_scrubs_what_it_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§56's other named channel: "tool output shown to the user".

    ``system.status`` carries no credential today -- host, OS, uptime, CPU, memory,
    disks and interfaces, and no process command lines, which is where a
    ``--password=`` would show up. So the scrub on that route is defence in depth
    rather than a fix for a live leak.

    That is exactly why it needs a test. A guard whose payload never contains the
    thing it guards against is invisible: deleting it changes no observable
    behaviour, so it survives review, and it survives mutation too -- which would
    record a passing suite as evidence for a boundary that no longer exists. The
    payload below is therefore the field this route does *not* have yet. The test
    pins the boundary rather than today's data, so the day the payload grows a field
    that quotes a command, the redaction is already here and already covered.
    """

    async def fake_execute(name: str, params: dict | None = None, **kwargs: object):
        assert name == "system.status", "the route must still ask for system.status"
        return registry.ToolResult(
            ok=True,
            data={"host": "nexus-box", "boot_command": f"nexusd --api-key={OPENAI}"},
            summary="Read the machine state.",
            verified=True,
        )

    monkeypatch.setattr(runtime, "execute", fake_execute)

    # The real router on a bare app, so no lifespan work runs for a route test.
    app = FastAPI()
    app.include_router(system_routes.router)
    with TestClient(app) as http:
        response = http.get("/api/system/status")

    assert response.status_code == 200
    assert OPENAI not in response.text, "a credential reached a user-facing route"
    assert REDACTED in response.text
    assert "nexus-box" in response.text, "the useful part of the payload must survive"
    # The envelope around ``data`` is not part of the scrub and must arrive intact.
    body = response.json()
    assert body["summary"] == "Read the machine state."
    assert body["verified"] is True
