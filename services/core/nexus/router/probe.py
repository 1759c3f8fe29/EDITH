"""Find out which models this key can actually call (spec §13, §78).

The router's job table names models. It cannot know whether they *work*: a
provider will happily list a model and then answer 404 "not found for account"
when your key asks for it. The model is real and deployed -- just not enabled
here -- and nothing in the listing says so.

This module asks. It walks every candidate in every job chain, makes the smallest
real request that proves reachability, and records the answer in ``model_probes``
so the router can skip what is known to be dead instead of paying for the same
failed round trip on every request.

Two decisions worth stating:

* **Every candidate, not just the head of each chain.** Knowing the preferred
  model works tells you nothing about the fallback, and the fallback only ever
  gets used at the worst possible moment -- when the primary has already failed.
  Discovering *then* that it is also dead is how a feature turns out to be broken
  under exactly the conditions it exists for.

* **A probe that fails is not the same as a model that is broken.** A cold NIM
  model has been measured past 110 seconds to first token, so the budget here is
  generous and timeouts are recorded as transient. Only the provider saying "no"
  in a way that will not change (404, rejected key) is allowed to bench a model.
  ``model_probes`` enforces that split; this module just reports honestly.

The probe is a real call, so it costs quota and time. It is never automatic --
Settings triggers it, and it says how many models it is about to contact.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .. import providers
from ..db.repos import model_probes
from ..providers.base import ChatRequest, Message, ProviderError
from . import jobs

log = logging.getLogger(__name__)

#: Enough tokens for a model to say one word, and no more. Reasoning models spend
#: their budget thinking before they emit any content, so a probe that demanded
#: real prose would call them broken; reachability is what is being measured, and
#: the first event of any kind proves it.
_PROBE_MAX_TOKENS = 16

#: A cold model can take over 100 seconds to answer. This is deliberately far
#: beyond that: a probe that times out early records a false negative, and a false
#: negative here is worse than a slow probe because it is remembered.
_PROBE_TIMEOUT_S = 300.0

_PROMPT = "Reply with the single word: ready"


async def _probe_chat(key: str, provider_id: str, model_id: str) -> dict[str, Any]:
    """Prove a chat model answers, through the same code path real requests use.

    Deliberately goes through ``provider.stream`` rather than a hand-rolled POST:
    a probe that exercised a different path could pass while real traffic failed,
    which is the one outcome that would make this whole module worse than nothing.
    The stream is abandoned after the first event -- reachability is established by
    then, and generating the rest would spend quota to learn nothing.
    """
    provider = providers.get(provider_id)
    if provider is None:
        return {"ok": False, "error_kind": "model_unavailable",
                "detail": f"There is no provider called '{provider_id}'."}
    if not provider.has_credential():
        return {"ok": False, "error_kind": "missing_credential",
                "detail": f"No {provider.name} API key is configured."}

    request = ChatRequest(
        messages=[Message(role="user", content=_PROMPT)],
        model=model_id,
        max_output_tokens=_PROBE_MAX_TOKENS,
        temperature=0.0,
    )

    started = time.monotonic()
    stream = provider.stream(request)
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_S):
            async for event in stream:
                if event.type in ("text", "reasoning", "tool_call", "done"):
                    return {
                        "ok": True,
                        "latency_ms": int((time.monotonic() - started) * 1000),
                        "detail": f"Answered with a {event.type} event.",
                    }
        # The stream ended without producing anything. Reachable, but not usable
        # for the job, and worth distinguishing from a refusal.
        return {"ok": False, "error_kind": "stream_interrupted",
                "latency_ms": int((time.monotonic() - started) * 1000),
                "detail": "The model accepted the request but produced no output."}
    except ProviderError as exc:
        return {"ok": False, "error_kind": exc.kind, "detail": exc.message,
                "latency_ms": int((time.monotonic() - started) * 1000)}
    except TimeoutError:
        return {"ok": False, "error_kind": "timeout",
                "latency_ms": int((time.monotonic() - started) * 1000),
                "detail": f"No answer within {int(_PROBE_TIMEOUT_S)}s. Recorded as a "
                          "one-off, not as a broken model."}
    except Exception as exc:  # noqa: BLE001 -- a probe must never take the app down
        log.warning("probe of %s raised", key, exc_info=True)
        return {"ok": False, "error_kind": "provider_error", "detail": str(exc)}
    finally:
        # Abandoning an async generator mid-iteration leaves the HTTP response
        # open until garbage collection. Closing it explicitly returns the
        # connection to the pool now, which matters when probing a dozen models.
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001
                pass


async def _probe_media(job: str, model_id: str, provider_id: str = "nvidia") -> dict[str, Any]:
    """Prove a media specialist is reachable, through its own provider's client.

    Dispatches on the provider because "media" is a request *shape* -- audio or
    image bytes instead of chat messages -- not a vendor, and not one wire either.
    Three clients sit behind it: Mistral's HTTP audio API, NIM's HTTP endpoints
    for OCR and retrieval, and NIM's gRPC speech functions. An earlier version
    checked one catalogue and assumed the answer covered everything, which would
    now reject every speech model as "not in the NIM catalogue"; the same assumption
    with the media client would send the gRPC speech functions to the HTTP gateway
    and bench working models as unreachable.

    Speech is decided by protocol, not by job: OCR and retrieval are HTTP no matter
    who serves them, and a gRPC speech candidate must be probed by the speech client
    or not at all. ``providers.speech.provider_for`` is not used here because the
    probe runs before any routing decision exists to honour -- it is one of the
    places that *informs* the decision -- so the table is read directly.
    """
    endpoint = providers.nim_catalogue.BY_MODEL.get(model_id)
    is_speech = endpoint is not None and endpoint.protocol == "grpc"

    if provider_id == "mistral" or is_speech:
        if provider_id == "mistral":
            client: Any = providers.mistral_audio.client()
            missing = "No Mistral API key is configured."
        else:
            client = providers.nvidia_speech.client()
            missing = "No NVIDIA API key is configured."
        if not client.has_credential():
            return {"ok": False, "error_kind": "missing_credential", "detail": missing}
    else:
        client = providers.nim_media.client()
        if not client.has_credential():
            return {"ok": False, "error_kind": "missing_credential", "detail": "No NVIDIA API key is configured."}
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_S):
            result = await client.probe_model(job, model_id)
    except TimeoutError:
        return {"ok": False, "error_kind": "timeout",
                "detail": f"No answer within {int(_PROBE_TIMEOUT_S)}s."}
    except Exception as exc:  # noqa: BLE001
        log.warning("media probe of %s raised", model_id, exc_info=True)
        return {"ok": False, "error_kind": "provider_error", "detail": str(exc)}
    return {
        "ok": result.ok,
        "error_kind": result.error_kind,
        "detail": result.message,
        "latency_ms": result.latency_ms,
    }


def targets(only_jobs: tuple[str, ...] | None = None) -> list[dict[str, str]]:
    """Every distinct model the router could choose, with how to reach it.

    Deduplicated by key: several jobs share a model, and probing the same model
    four times would spend four times the quota to learn one fact. The first job
    that names it wins, because the probe shape depends on the transport and the
    transport is a property of the job.
    """
    seen: dict[str, dict[str, str]] = {}
    for spec in jobs.JOBS:
        if only_jobs and spec.job not in only_jobs:
            continue
        for key in spec.chain:
            if key in seen:
                continue
            provider_id, model_id = jobs.split_key(key)
            seen[key] = {
                "key": key,
                "provider": provider_id,
                "model": model_id,
                "transport": spec.transport,
                "job": spec.job,
            }
    return list(seen.values())


async def probe_all(
    *,
    only_jobs: tuple[str, ...] | None = None,
    store: bool = True,
    concurrency: int = 4,
) -> dict[str, Any]:
    """Probe every routable model and record what this key can call.

    ``concurrency`` is small on purpose. Firing a dozen simultaneous requests at a
    free tier is a good way to be rate limited, and a rate limit would be recorded
    against a model that was fine -- turning the measurement into the thing that
    corrupted it. Four at a time keeps the wall clock reasonable without
    manufacturing failures.
    """
    plan = targets(only_jobs)
    gate = asyncio.Semaphore(max(1, concurrency))

    async def one(target: dict[str, str]) -> dict[str, Any]:
        async with gate:
            if target["transport"] == "media":
                outcome = await _probe_media(
                    target["job"], target["model"], target["provider"]
                )
            else:
                outcome = await _probe_chat(
                    target["key"], target["provider"], target["model"]
                )
        row = {**target, **outcome}
        if store:
            try:
                model_probes.record(
                    target["key"],
                    provider=target["provider"],
                    model=target["model"],
                    transport=target["transport"],
                    ok=bool(outcome.get("ok")),
                    error_kind=outcome.get("error_kind"),
                    detail=str(outcome.get("detail") or ""),
                    latency_ms=outcome.get("latency_ms"),
                )
            except Exception:  # noqa: BLE001 -- a failed write must not lose the result
                log.warning("could not record probe for %s", target["key"], exc_info=True)
                row["stored"] = False
            else:
                row["stored"] = True
        return row

    started = time.monotonic()
    results = await asyncio.gather(*(one(t) for t in plan))

    callable_now = [r for r in results if r.get("ok")]
    blocked = [r for r in results if not r.get("ok") and model_probes.is_durable(r.get("error_kind"))]
    flaky = [r for r in results if not r.get("ok") and not model_probes.is_durable(r.get("error_kind"))]

    return {
        "checked": len(results),
        "callable": len(callable_now),
        "blocked": len(blocked),
        "flaky": len(flaky),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "results": results,
        # Spelled out rather than left for the caller to infer: "blocked" means
        # routed around from now on, "flaky" means still in the chain.
        "summary": (
            f"{len(callable_now)} of {len(results)} models answered. "
            f"{len(blocked)} cannot be used with this key and will be skipped; "
            f"{len(flaky)} failed once and stay in the chain."
        ),
    }
