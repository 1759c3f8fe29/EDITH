"""Model surface: what is configured, what each job resolves to, and proving it.

§26 requires the routing to be *visible* -- the user should be able to see which
model answered and why. That is only possible if the same resolver the chat path
uses can be asked hypothetically, so ``/resolve`` calls ``router.resolve`` rather
than reimplementing the decision. If the two ever disagreed, the explanation
screen would be describing a routing table that does not exist, which §81 (no
fake progress) and §3 (no confident fiction) both forbid.

``/verify`` is the only route in the whole API that makes a network call on
purpose. It is a POST for that reason: verification costs real requests against
real providers, and a GET would let a page refresh spend the user's quota.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from ... import providers
from ...providers import nim_catalogue
from ...logging_setup import get_logger
from ...router import NoModelAvailable, jobs, resolve as resolve_job

router = APIRouter(prefix="/api/models", tags=["models"])
log = get_logger("api.models")


@router.get("")
def overview() -> dict[str, Any]:
    """Everything the Models screen shows, with no network calls.

    Offline this still returns the full picture: which providers have keys, which
    models exist, what each job is assigned to. The one thing it cannot tell you
    is whether a key still *works* -- that needs ``/verify``, and pretending
    otherwise would be the fake-status failure §60 warns about.
    """
    from ...router import catalogue as router_catalogue

    return {
        **providers.snapshot(),
        "routing": router_catalogue(),
        "configured": [p.id for p in providers.configured()],
        "models": {
            "total": len(providers.all_models()),
            "available": len(providers.available_models()),
        },
        # Included here rather than left to a second request because a Models screen
        # that lists a model without saying "your key cannot call this one" is
        # showing a bench that does not exist. Still no network use: these are
        # remembered results from the last probe.
        "probes": _probe_summary(),
    }


def _probe_summary() -> dict[str, Any]:
    """Last probe outcome per model, or empty when nothing has been measured yet.

    Swallows database errors on purpose: the Models screen must still render if the
    probe table cannot be read, and "nothing is known to be blocked" is the same
    state as a fresh install.
    """
    try:
        from ...db.repos import model_probes

        return {"stats": model_probes.stats(), "blocked": model_probes.unusable()}
    except Exception:  # noqa: BLE001 -- see docstring
        log.debug("could not read probe results for the models overview", exc_info=True)
        return {"stats": {"total": 0}, "blocked": {}}


@router.get("/jobs")
def job_catalogue() -> dict[str, Any]:
    """The jobs the router knows about, and what each one needs from a model."""
    return {"jobs": jobs.catalogue(), "main_model": jobs.MAIN_MODEL}


@router.get("/resolve")
def resolve(
    job: str = "chat",
    needs_vision: bool = False,
    min_context_tokens: int = 0,
) -> dict[str, Any]:
    """Which model would handle this job right now, and every model it passed over.

    Returns the rejected candidates with their reasons, not just the winner. "Why
    did it use that model?" is only answerable if the near-misses are visible
    (§26).
    """
    try:
        decision = resolve_job(job, needs_vision=needs_vision, min_context_tokens=min_context_tokens)
    except NoModelAvailable as exc:
        # 409, not 500: nothing is broken. The configuration genuinely has no
        # model that can do this, and the exception text says which requirement
        # each candidate failed.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"There is no job called '{job}'.") from exc
    return {"decision": decision.to_dict()}


@router.get("/nim")
def nim(modality: str | None = None) -> dict[str, Any]:
    """The NIM specialist catalogue, optionally for one modality."""
    entries = nim_catalogue.catalogue()
    if modality:
        wanted = {m.model_id for m in nim_catalogue.by_modality(modality)}  # type: ignore[arg-type]
        entries = [e for e in entries if e.get("model_id") in wanted]
        if not entries:
            raise HTTPException(
                status_code=404,
                detail=f"No NIM specialist is registered for '{modality}'.",
            )
    return {"specialists": entries}


@router.post("/verify")
async def verify() -> dict[str, Any]:
    """Actually call every configured provider and report what happened.

    Slow by nature -- NVIDIA's free tier cold-starts a model and has been measured
    taking over 100 seconds to answer a five-token request, while its model listing
    answers in under a second. Both numbers come back in the result, because a slow
    success and a failure are completely different things and a spinner cannot tell
    the user which one it is looking at.
    """
    results = await providers.verify_all()
    return {
        "results": {pid: result.to_dict() for pid, result in results.items()},
        "checked": len(results),
        "ok": sum(1 for r in results.values() if r.ok),
    }


@router.post("/embed")
async def embed(
    text: str = Body(..., embed=True),
    input_type: str = Body("query", embed=True),
) -> dict[str, Any]:
    """Embed one string, so the Memory screen can offer semantic search.

    Separate from ``/api/memories/search`` on purpose: that route is text-only and
    always works, this one needs a provider and can fail. Merging them would make
    a search box that sometimes returns nothing for reasons the user cannot see.

    ``input_type`` defaults to ``query`` rather than ``passage`` because the only
    caller here is a search box. These models are trained asymmetrically, and
    embedding a query as a passage silently degrades recall -- results would just
    be quietly worse, with nothing to indicate why.
    """
    if not text.strip():
        raise HTTPException(status_code=400, detail="Nothing to embed.")
    if input_type not in ("query", "passage"):
        raise HTTPException(status_code=400, detail="input_type must be 'query' or 'passage'.")
    try:
        decision = resolve_job("embedding")
    except NoModelAvailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    from ...providers.base import ProviderError
    from ...providers.nim_media import client as nim_client

    try:
        vectors = await nim_client().embed([text], input_type=input_type, model=decision.model_id)
    except ProviderError as exc:
        # 502: the request was fine, the upstream model was not. Distinguished from
        # 409 above (nothing configured) because only one of the two is fixable by
        # the user changing a setting.
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not vectors:
        raise HTTPException(status_code=502, detail="The embedding model returned no vector.")
    return {
        "model": decision.key,
        "input_type": input_type,
        "dimensions": len(vectors[0]),
        "vector": vectors[0],
    }


@router.get("/probes")
def probe_results() -> dict[str, Any]:
    """What a real call has already proved about each model, with no network use.

    Read-only counterpart to ``POST /probe``: the Settings screen needs to show the
    last measurement on load without spending quota to redraw a list.
    """
    from ...db.repos import model_probes

    return {
        "probes": model_probes.all_probes(),
        "stats": model_probes.stats(),
        "blocked": model_probes.unusable(),
    }


@router.post("/probe")
async def run_probe(
    jobs_filter: list[str] | None = Body(None, embed=True, alias="jobs"),
) -> dict[str, Any]:
    """Call every routable model once and record which ones this key can use.

    A POST, and never automatic, because this is the most expensive route in the
    API: it contacts every model in every chain. It probes *fallbacks* too, not
    just the model each job prefers -- a fallback is only ever reached when the
    primary has already failed, so finding out then that it is also dead breaks the
    feature under exactly the conditions it exists for.

    Failures are split rather than counted together. Only a durable refusal (404
    for this account, a rejected key) removes a model from routing; a timeout
    leaves it in the chain, because NIM cold starts have been measured past 250
    seconds and permanently benching a slow model would quietly shrink the bench
    with nothing on screen to explain why.
    """
    from ...router import probe as probe_mod

    only = tuple(j for j in (jobs_filter or []) if j in jobs.BY_JOB) or None
    if jobs_filter and not only:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown job(s). Valid jobs: {', '.join(sorted(jobs.BY_JOB))}.",
        )
    return await probe_mod.probe_all(only_jobs=only)


@router.delete("/probes")
def clear_probes(key: str | None = None) -> dict[str, Any]:
    """Forget probe verdicts so the next probe measures again.

    Exists because a stored 404 goes stale the moment the user enables the model on
    the provider's site. Without this the app would stay permanently wrong about
    something the user had just fixed.
    """
    from ...db.repos import model_probes

    removed = model_probes.clear(key)
    return {"cleared": removed, "key": key}
