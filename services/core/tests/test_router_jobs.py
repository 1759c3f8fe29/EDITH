"""The job table's shape: every chain entry must be able to be chosen.

This file exists because of a real bug that reached the tree. A stray comma turned two
module constants into tuples::

    MAIN_MODEL = "nvidia/deepseek-ai/deepseek-v4-flash-0731","mistral:mistral-large-latest"

``MAIN_MODEL`` and ``BIG_BRAIN`` appear in the chat, expert, reasoning and coding chains
-- every conversational job in the product -- so ``resolve("chat")`` raised
``AttributeError: 'tuple' object has no attribute 'partition'`` on the live path, not only
under pytest. It was found by accident, thirteen tests deep in ``test_agent_loop.py``,
because the agent's planner happens to build a system prompt that names its model. The
router itself had no tests at all, which is why the most central table in the codebase
could be syntactically broken and only report it sideways.

Two quieter casualties came from the same line, and neither would ever have raised:

* ``_chain`` substitutes the user's configured main model with
  ``key == jobs.MAIN_MODEL``. A tuple never equals a string, so "use the main model I
  chose in Settings" silently stopped applying to every chain that defers to it.
* ``GET /api/models/jobs`` returns ``"main_model": jobs.MAIN_MODEL``, so the UI was
  served a JSON array where it expects a string.

The tests here are deliberately about the *table*, not about routing outcomes. They are
cheap, they need no network and no credentials, and any one of them fails the instant a
chain entry stops being a thing the router can choose.

**Credentials are excluded on purpose.** "No NVIDIA key is configured" is a real runtime
state that the router reports and recovers from; it must not fail a test. What these
tests refuse is the other thing entirely: an entry that can *never* be chosen no matter
which keys you hold. That is the failure mode with no symptom -- an invented model id
sits in a chain looking exactly like a safety net, is skipped silently on every request,
and the chain is one model shorter than it reads. The tuple bug shipped alongside exactly
such an id: ``deepseek-ai/deepseek-v4-pro-0813``, which exists in no catalogue.
"""
from __future__ import annotations

import pytest

from nexus import providers
from nexus.router import jobs

#: Every ``(job, key)`` pair in the table, so a failure names the entry rather than
#: reporting that "some chain somewhere" is wrong.
ENTRIES = [(spec.job, key) for spec in jobs.JOBS for key in spec.chain]

IDS = [f"{job}:{key}" for job, key in ENTRIES]


def can_be_chosen(job: str, key: str) -> str | None:
    """Why the router could never choose this entry, or None if it could.

    Mirrors ``Router._why_not`` with the credential and capability checks removed --
    those depend on which keys are installed and on what the request contains. What is
    left is the part that is a property of the table alone: does this key name a model
    the router has any way to reach for this job.
    """
    spec = jobs.spec(job)
    provider_id, model_id = jobs.split_key(key)
    if not provider_id or not model_id:
        return f"{key!r} is not a 'provider:model' key."

    if spec.transport == "chat":
        if providers.get(provider_id) is None:
            return f"there is no provider called {provider_id!r}"
        if providers.find_model(key) is None:
            return f"{provider_id} does not list {model_id!r}"
        return None

    # Media is a request *shape*, not a vendor: speech is Mistral's and OCR, embedding
    # and rerank are NVIDIA's, so there are two catalogues to check rather than one.
    if provider_id == "mistral":
        served = providers.mistral_audio.serves(job)
        if model_id not in served:
            return f"mistral audio does not use {model_id!r} for {job} (it uses {served!r})"
        return None

    endpoint = providers.nim_catalogue.BY_MODEL.get(model_id)
    if endpoint is None:
        return f"{model_id!r} is not in the NIM specialist catalogue"
    if job not in endpoint.jobs:
        return f"{model_id!r} is catalogued for {endpoint.jobs!r}, not {job}"
    return None


# ------------------------------------------------------------------ the entries


@pytest.mark.parametrize(("job", "key"), ENTRIES, ids=IDS)
def test_every_chain_entry_is_a_string(job: str, key: object) -> None:
    """The tuple bug, pinned directly.

    Checked separately from resolvability because it is the failure that *crashes* rather
    than degrades, and because the assertion has to run before anything calls
    ``split_key`` on the value -- an ``AttributeError`` inside a helper is a far worse
    error message than this.
    """
    assert isinstance(key, str), (
        f"the {job} chain contains {key!r}, which is not a 'provider:model' string. "
        "A stray comma after a chain constant makes it a tuple, and every job that "
        "names that constant then raises inside the router."
    )


@pytest.mark.parametrize(("job", "key"), ENTRIES, ids=IDS)
def test_every_chain_entry_can_be_chosen(job: str, key: str) -> None:
    """No entry may be permanently unreachable, credentials aside.

    An unreachable entry has no symptom: ``_candidate`` accepts a key that resolves to
    nothing, ``_why_not`` skips it with a reason nobody reads, and the chain the docstring
    describes as six models deep is really five. The chain is the product's whole answer
    to "a specialist is down", so an entry that was never up is a hole in it.
    """
    problem = can_be_chosen(job, key)
    assert problem is None, f"the {job} chain lists {key!r} but {problem}"


# -------------------------------------------------------------- the constants


@pytest.mark.parametrize("name", ["MAIN_MODEL", "BIG_BRAIN", "EXPERT_MODEL"])
def test_the_shared_constants_are_single_keys(name: str) -> None:
    """The three constants that appear in more than one chain.

    Covered by the per-entry tests as well, and not redundantly: those iterate the
    chains, so a constant that stopped being referenced anywhere would stop being
    checked at exactly the moment it stopped being obviously right.
    """
    value = getattr(jobs, name)
    assert isinstance(value, str), f"jobs.{name} is {value!r}, not a single key"
    provider_id, model_id = jobs.split_key(value)
    assert provider_id and model_id, f"jobs.{name} is not a 'provider:model' key"


def test_the_main_model_is_the_one_the_router_defaults_to() -> None:
    """``_chain`` substitutes the configured main model by comparing against MAIN_MODEL.

    The comparison is ``key == jobs.MAIN_MODEL``, so the two spellings of "the main
    model" -- this constant and the ``main_provider``/``main_model`` settings defaults --
    have to agree exactly. When they drift, nothing raises: the substitution simply stops
    happening, the chain keeps its hard-coded head, and changing the main model in
    Settings alters the label on the Settings screen and nothing else.
    """
    from nexus import router

    assert router.main_key(router._FALLBACK_SETTINGS) == jobs.MAIN_MODEL


# ------------------------------------------------------------------- the table


def test_every_job_has_a_chain() -> None:
    """An empty chain is a job that always raises NoModelAvailable."""
    for spec in jobs.JOBS:
        assert spec.chain, f"the {spec.job} job has no candidates at all"


def test_no_chain_repeats_a_model() -> None:
    """``_chain`` dedupes into ``keys``, so a repeat is a candidate that never runs.

    Harmless at runtime and misleading to read: a chain written with five entries where
    two are the same is a four-deep chain claiming to be five. It usually means a
    constant was expanded by hand next to the constant itself.
    """
    for spec in jobs.JOBS:
        seen = [key for key in spec.chain if isinstance(key, str)]
        duplicates = {key for key in seen if seen.count(key) > 1}
        assert not duplicates, f"the {spec.job} chain lists {duplicates} more than once"


def test_every_job_has_its_own_settings_key() -> None:
    """Two jobs sharing a setting means choosing a model for one silently changes the other."""
    settings = [spec.setting for spec in jobs.JOBS]
    duplicates = {key for key in settings if settings.count(key) > 1}
    assert not duplicates, f"these settings keys are used by more than one job: {duplicates}"


def test_the_catalogue_the_settings_screen_reads_is_serialisable() -> None:
    """``jobs.catalogue()`` is JSON for the model-assignment panel.

    ``default`` is ``chain[0]``, so the tuple bug put a list where the UI reads a string
    -- the same shape of break as ``main_model`` on ``/api/models/jobs``, and equally
    silent. A UI cannot render a two-element array as a model name; it renders nothing,
    or the word "undefined", and the panel looks empty rather than broken.
    """
    import json

    rows = jobs.catalogue()
    assert len(rows) == len(jobs.JOBS)
    for row in rows:
        assert isinstance(row["default"], str) and row["default"], row
        assert all(isinstance(key, str) for key in row["candidates"]), row
    # Serialises, and nothing in it is a tuple pretending to be a string.
    json.loads(json.dumps(rows))


def test_the_settings_defaults_name_the_same_main_model() -> None:
    """The third spelling of "the main model" has to agree with the other two.

    ``MAIN_MODEL`` lives in the job table, ``_FALLBACK_SETTINGS`` lives in the router,
    and ``app_settings.DEFAULTS`` lives in the database layer -- and the last one is the
    one that actually reaches a fresh install, because ``DEFAULTS`` supplies every key
    the ``settings`` table has no row for. So on a first run the router does not read its
    own fallback at all; it reads these, and they win.

    That is why the disagreement this pins was not cosmetic. The database layer defaulted
    to ``mistral:mistral-large-latest`` while the job table's ``MAIN_MODEL`` was a NVIDIA
    DeepSeek id, and ``_chain`` substitutes the configured main model by testing
    ``key == jobs.MAIN_MODEL``. Two different strings never match, so the substitution
    never fired: every chain kept its hard-coded head, and choosing a main model in
    Settings changed the label on the Settings screen and nothing else. Nothing raised,
    nothing logged, and no test failed -- the only visible symptom was a feature quietly
    not working.

    ``app_settings`` spells the value out instead of importing it, deliberately: the
    router consumes settings, so a settings module that imports the router would point
    the dependency backwards. This test is what pays for that choice, so it is not
    redundant with the constant living in one place -- it is the *reason* the constant is
    allowed to be written twice.
    """
    from nexus.db.repos import app_settings

    expected_provider, expected_model = jobs.split_key(jobs.MAIN_MODEL)
    assert app_settings.DEFAULTS["main_provider"] == expected_provider, (
        "app_settings.DEFAULTS['main_provider'] disagrees with jobs.MAIN_MODEL, so a "
        "fresh install routes to a different model than the job table names"
    )
    assert app_settings.DEFAULTS["main_model"] == expected_model, (
        "app_settings.DEFAULTS['main_model'] disagrees with jobs.MAIN_MODEL, so "
        "_chain's `key == jobs.MAIN_MODEL` substitution silently never fires"
    )


def test_a_fresh_install_routes_to_the_main_model_the_table_names() -> None:
    """The same agreement, asserted through the function the router actually calls.

    Checked separately from the ``DEFAULTS`` comparison above because it exercises the
    path rather than the pair of literals: ``main_key`` reads whatever ``get_all()``
    returns, which is ``DEFAULTS`` merged with any stored rows. A future change that
    stopped merging defaults, or renamed a key on one side only, would leave the two
    literals in perfect agreement and still hand the router the wrong model.
    """
    from nexus import router
    from nexus.db.repos import app_settings

    settings = dict(app_settings.DEFAULTS)
    assert router.main_key(settings) == jobs.MAIN_MODEL


def test_a_cleared_main_model_setting_falls_back_to_the_job_table() -> None:
    """An empty string is a stored value, not a missing key, and it has its own path.

    ``main_key`` reads ``config.get("main_provider") or <default>``, so the default is
    reached two ways: the key is absent (a pre-migration database) or the key is present
    and empty (a user who cleared the field in Settings). Every other test here passes a
    fully populated config, so the ``or`` branch never executes in them -- which means a
    wrong literal on that side of the ``or`` would have been invisible. This test exists
    because writing a deliberate mutation of that branch found nothing to fail.
    """
    from nexus import router

    assert router.main_key({"main_provider": "", "main_model": ""}) == jobs.MAIN_MODEL
    assert router.main_key({}) == jobs.MAIN_MODEL


def test_the_route_job_is_headed_by_a_fast_instruction_following_model() -> None:
    """Every request pays this model before the user sees an answer, so it must be cheap.

    ``intent.refine`` calls ``resolve("fast")`` when the syntactic classifier is unsure,
    and ``memory_extract`` calls the same job to judge whether a sentence is worth
    remembering. In both cases the model's answer is never shown -- it only decides
    which model the user *does* see, or whether a memory is written. So the head of this
    chain has two hard requirements and no room to trade either away:

    * it must be ``latency="fast"`` -- a slow model here taxes every request, not just
      the ones routed to it;
    * it must declare ``instruction_following`` -- labelling a sentence is an
      instruction, and a fast model that cannot follow one gets fast at the wrong answer.

    Both are asserted against the catalogue's own declaration rather than re-stated here,
    so the test pins the *property* the route job needs instead of one model id. The head
    is ``nemotron-3.5-lightning-30b-a3b``, the model the catalogue already describes as
    "built for latency, used for classifying a request". The two failure modes this test
    refuses are the neighbours it actually has: ``nemotron-3-nano-30b-a3b`` is fast but
    declares no instruction-following, and ``BIG_BRAIN`` follows instructions but is
    marked slow. A route job headed by either passes today's per-entry tests unchanged.
    """
    spec = jobs.spec("fast")
    head = spec.chain[0]
    resolved = providers.find_model(head)
    assert resolved is not None, f"the route head {head!r} resolves to nothing"
    model = resolved[1]
    assert model.latency == "fast", (
        f"the route head {head!r} is marked {model.latency!r}, taxing every request"
    )
    assert "instruction_following" in model.strengths, (
        f"the route head {head!r} cannot follow a classification instruction"
    )
