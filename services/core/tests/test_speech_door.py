"""The one speech door: the decision names the provider, the provider named is called.

``nexus.providers.speech`` exists because ``voice_routes`` and ``voice_stream`` used
to call Mistral directly and then report the router's answer alongside audio that had
not come from it -- the silent substitution the specs prohibit. The module's whole
value is that dispatch cannot disagree with the answer it gives about itself, so these
tests hold it to exactly that:

* ``provider_for`` is the dispatch table: unknown ids raise, known ids are returned,
  and the same table answers "does this provider do speech" (``has_credential``) and
  "what is its default voice" (``default_voice``). One dict, so those cannot drift.
* ``voice_for`` carries the precedence -- caller, then ``tts_voice`` setting, then the
  provider's own default -- plus the rule the tests elsewhere cannot cover: a stored
  voice belonging to a *different* provider is dropped rather than sent. A user who
  picked ``en_paul_neutral`` while Mistral served speech, then had routing move to
  NVIDIA, would otherwise have every synthesis refused with "not a voice Magpie has"
  -- a stored preference breaking a working feature after a change they never made.
* ``transcribe`` and ``synthesize`` dispatch to the module the decision named. These
  run against the real dispatch table with the provider modules' ``client()`` stubbed,
  so what is tested is the *routing* through the door, not the gRPC/HTTP clients.

The two provider modules -- ``nvidia_speech``, ``mistral_audio`` -- are not stubbed
out entirely: ``_PROVIDERS`` is patched per test with a tiny fake module, because the
claim under test is about the door's behaviour over whatever sits behind it, and
patching the table is also how the door proves a *wrong* provider was never touched.
"""
from __future__ import annotations

import asyncio
from types import ModuleType
from typing import Any

import pytest

from nexus.providers import speech
from nexus.providers.base import ProviderError
from nexus.providers.nim_media import Speech, Transcript


class FakeProvider(ModuleType):
    """A provider module as the door sees one: constants, a client, a result."""

    def __init__(self, name: str, *, default_voice: str = "", max_chars: int = 0) -> None:
        super().__init__(name)
        self.DEFAULT_VOICE = default_voice
        self.MAX_TTS_CHARS = max_chars
        self.credential = True
        self.transcribes: list[dict[str, Any]] = []
        self.synthesises: list[dict[str, Any]] = []

    def has_credential(self) -> bool:
        return self.credential

    def client(self) -> "FakeProvider":
        return self

    async def transcribe(self, audio: Any, *, model: str, language: str | None = None) -> Transcript:
        self.transcribes.append({"model": model, "language": language})
        return Transcript(text="stub heard", model=model, language=language)

    async def synthesize(
        self, text: str, *, model: str, voice: str | None = None, language: str | None = None
    ) -> Speech:
        self.synthesises.append(
            {"text": text, "model": model, "voice": voice, "language": language}
        )
        return Speech(path=None, model=model, bytes_written=1, media_type="audio/wav")

    async def voices(self) -> list[dict[str, Any]]:
        return [{"slug": self.DEFAULT_VOICE, "name": "Stub", "gender": None, "languages": [], "tags": []}]


def patch_table(monkeypatch: pytest.MonkeyPatch, **providers: FakeProvider) -> None:
    monkeypatch.setattr(speech, "_PROVIDERS", providers)


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    """A fake provider under the door's dispatch key.

    The real table maps ``nvidia`` -> ``nvidia_speech`` and ``mistral`` ->
    ``mistral_audio``; the tests patch in fakes because the claims are about the
    door's behaviour over whatever sits behind it, and because a fake is how a test
    proves a *wrong* provider was never touched. Patching the whole table (rather
    than one entry) keeps the fakes from leaking onto the other key: an entry left
    real would mean a wrong-dispatch test failing on a live network call rather
    than on the assertion.
    """
    module = FakeProvider("fake_nvidia", default_voice="Magpie-Multilingual", max_chars=2_000)
    patch_table(monkeypatch, nvidia=module)
    return module


@pytest.fixture
def nvidia(table: FakeProvider) -> FakeProvider:
    return table


@pytest.fixture
def mistral(monkeypatch: pytest.MonkeyPatch, table: FakeProvider) -> FakeProvider:
    """The second provider, added to the same table the ``nvidia`` fixture built.

    The two fixtures exist because the interesting tests need both providers present
    at once -- proving one was called is only meaningful if the other was reachable
    and was not.
    """
    module = FakeProvider("fake_mistral", default_voice="en_paul_neutral", max_chars=4_000)
    patch_table(monkeypatch, nvidia=table, mistral=module)
    return module


# ---------------------------------------------------------------------- the dispatch


def test_a_known_provider_is_returned(nvidia: FakeProvider) -> None:
    assert speech.provider_for("nvidia") is nvidia


def test_an_unknown_provider_is_refused_by_name() -> None:
    with pytest.raises(ProviderError) as caught:
        speech.provider_for("acme")
    assert "acme" in str(caught.value)
    assert "does not provide speech" in str(caught.value)


def test_the_same_table_answers_who_can_speak(monkeypatch: pytest.MonkeyPatch) -> None:
    """``has_credential`` reads the same dict as dispatch, so they cannot disagree.

    An unknown id answers ``False`` rather than raising: the settings screen asks this
    about every provider id it knows, including ones the speech table has never heard
    of, and a raise there would turn a harmless question into a broken panel.
    """
    module = FakeProvider("fake_nvidia")
    module.credential = False
    patch_table(monkeypatch, nvidia=module)

    assert speech.has_credential("nvidia") is False
    module.credential = True
    assert speech.has_credential("nvidia") is True
    assert speech.has_credential("acme") is False


def test_the_default_voice_comes_from_the_named_provider(
    nvidia: FakeProvider, mistral: FakeProvider
) -> None:
    """Two providers whose defaults share nothing, asked separately.

    This is why the default is per-provider rather than a constant: Mistral's is a
    slug, NVIDIA's is a model name, and using one with the other is a refused request
    rather than a different accent.
    """
    assert speech.default_voice("nvidia") == "Magpie-Multilingual"
    assert speech.default_voice("mistral") == "en_paul_neutral"
    assert speech.default_voice("acme") == ""


def test_max_chars_is_the_named_providers_own(nvidia: FakeProvider, mistral: FakeProvider) -> None:
    """Asked rather than assumed: the two ceilings differ, and zero means "let the
    provider decide" rather than "refuse everything"."""
    assert speech.max_chars("nvidia") == 2_000
    assert speech.max_chars("mistral") == 4_000
    assert speech.max_chars("acme") == 0


# ------------------------------------------------------------------------ the calls


def test_transcribe_goes_to_the_named_provider(nvidia: FakeProvider) -> None:
    transcript = asyncio.run(
        speech.transcribe(
            b"audio", provider_id="nvidia", model="parakeet", language="hi-IN"
        )
    )
    assert transcript.text == "stub heard"
    assert nvidia.transcribes == [{"model": "parakeet", "language": "hi-IN"}]


def test_synthesize_goes_to_the_named_provider(mistral: FakeProvider) -> None:
    result = asyncio.run(
        speech.synthesize(
            "hello", provider_id="mistral", model="voxtral", voice="en_paul_neutral"
        )
    )
    assert result is not None
    assert mistral.synthesises == [
        {"text": "hello", "model": "voxtral", "voice": "en_paul_neutral", "language": None}
    ]


def test_synthesis_reaches_only_the_named_provider(
    nvidia: FakeProvider, mistral: FakeProvider
) -> None:
    """The substitution rule, asserted as silence on the other side.

    Calling with ``nvidia`` must leave Mistral's module untouched -- this is the exact
    inversion of the old behaviour, where the router said one provider and the code
    called another.
    """
    asyncio.run(speech.synthesize("hello", provider_id="nvidia", model="magpie", voice=None))
    assert nvidia.synthesises and not mistral.synthesises


def test_a_call_for_a_provider_that_is_not_in_the_table_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_table(monkeypatch)
    with pytest.raises(ProviderError):
        asyncio.run(speech.synthesize("hello", provider_id="acme", model="x", voice=None))


# ------------------------------------------------------------------ the voice choice


@pytest.mark.parametrize(
    ("provider", "requested", "configured", "expected", "why"),
    [
        # The caller's choice wins for either provider, whichever provider the voice
        # belongs to -- the UI passes it through, and a picker offering Mistral slugs
        # while NVIDIA serves is a panel nobody can make work.
        ("nvidia", "Magpie-Multilingual.EN-US.Sofia", "Magpie-Multilingual", "Magpie-Multilingual.EN-US.Sofia", "the caller's choice wins"),
        ("mistral", "en_kim_calm", "en_paul_neutral", "en_kim_calm", "the caller's choice wins on the other provider too"),
        # The setting is used when the caller is silent -- but only if it belongs to
        # the provider being asked. A stored preference from a routing change the
        # user never made must not break synthesis.
        ("nvidia", None, "Magpie-Multilingual", "Magpie-Multilingual", "the matching setting is used"),
        ("mistral", None, "en_paul_neutral", "en_paul_neutral", "the matching setting is used on the other provider too"),
        ("nvidia", None, "en_paul_neutral", None, "a setting from another provider is dropped, not sent"),
        ("mistral", None, "Magpie-Multilingual", None, "and the other way round"),
        # Nothing chosen anywhere: the provider picks its own default.
        ("nvidia", None, "", None, "no choice at all"),
        ("mistral", None, None, None, "the setting was never written"),
        # An unrecognisable setting is *sent*, not dropped: the door only drops a
        # voice it can positively identify as another provider's. Dropping anything
        # unrecognisable would silently ignore the setting of a provider whose voice
        # naming changed -- and the picker is the thing that guarantees the setting
        # holds a real voice, not this predicate.
        ("nvidia", None, "Voice", "Voice", "a voice the door cannot attribute is passed through"),
    ],
)
def test_voice_for(
    provider: str,
    requested: str | None,
    configured: str | None,
    expected: str | None,
    why: str,
) -> None:
    assert speech.voice_for(provider, requested, configured) == expected, why


def test_voice_ownership_is_shape_not_a_lookup() -> None:
    """Mistral's voice list is per-account and read live, so ownership is inferred from
    the name's shape rather than membership: a lowercase slug with an underscore is
    Mistral's, a ``Magpie-`` prefix is NVIDIA's. A voice that is neither is not sent
    to either -- ``_owner_of`` answers '' and the setting is dropped."""
    assert speech._owner_of("Magpie-Multilingual") == "nvidia"
    assert speech._owner_of("en_paul_neutral") == "mistral"
    assert speech._owner_of("Sofia") == ""


# ----------------------------------------------------------------------- the voices


def test_voices_are_the_named_providers_list(mistral: FakeProvider) -> None:
    items = asyncio.run(speech.voices("mistral", "voxtral"))
    assert items == [{"slug": "en_paul_neutral", "name": "Stub", "gender": None, "languages": [], "tags": []}]
