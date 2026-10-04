"""The model router (tasks E5-E7; spec §9, §10, §13, §14, §15, §79).

Given a job, this picks the model that does it, says why in plain English, and
falls back when a provider fails. Three rules shape the whole file.

**The main model wins unless there is a reason.** ``deepseek-v4-flash-0731`` heads
the conversation chain; the heavier specialists lead their own jobs
(``nemotron-3-ultra-550b`` for reasoning, ``deepseek-v4-pro-0813`` for coding and expert). A different model is only chosen
when the main one cannot do the work (an image to read, audio to transcribe), when
the user pinned something else, or when a provider is unreachable -- and in every one
of those cases the reason is recorded and shown.

**Fallback is honest.** Falling back before a single token has been emitted is
invisible and fine. Falling back *after* text has reached the user is not: the
result would be two half-answers spliced together, presented as one. So a stream
that breaks mid-answer surfaces the partial text and the error rather than
quietly restarting on another provider (§79).

**A decision is explainable.** Every ``Decision`` carries the candidates that were
considered, the ones that were skipped, and why. §15 requires the user be able to
ask "why this model" and get a real answer, which is only possible if the router
keeps its reasoning instead of just its conclusion.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from .. import providers
from ..logging_setup import get_logger
from ..providers.base import (
    ChatRequest,
    MissingCredential,
    ModelSpec,
    Provider,
    ProviderError,
    RateLimited,
    StreamEvent,
    StreamInterrupted,
)
from . import jobs
from .jobs import Job, JobSpec, Transport

log = get_logger("router")


class NoModelAvailable(ProviderError):
    """Nothing can do this job with the keys that are configured."""

    kind = "no_model_available"
    retryable = False
    fall_back = False


@dataclass
class Candidate:
    key: str
    provider_id: str
    model_id: str
    spec: ModelSpec | None
    #: Filled in when this candidate was rejected, with the reason.
    skipped: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "provider": self.provider_id,
            "model": self.model_id,
            "label": self.spec.label if self.spec else self.model_id,
            "skipped": self.skipped,
        }


@dataclass
class Decision:
    """Which model was chosen, and why (§15)."""

    job: Job
    transport: Transport
    provider_id: str
    model_id: str
    spec: ModelSpec | None
    reason: str
    considered: list[Candidate] = field(default_factory=list)
    #: Models to try if this one fails, in order.
    fallbacks: list[Candidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.provider_id}:{self.model_id}"

    @property
    def label(self) -> str:
        return self.spec.label if self.spec else self.model_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "job": self.job,
            "transport": self.transport,
            "key": self.key,
            "provider": self.provider_id,
            "model": self.model_id,
            "label": self.label,
            "reason": self.reason,
            "considered": [c.to_dict() for c in self.considered],
            "fallbacks": [c.to_dict() for c in self.fallbacks],
            "warnings": self.warnings,
        }


# ------------------------------------------------------------------- settings

#: Settings the router reads. Kept here so a missing database degrades to sane
#: behaviour rather than taking the whole service down: a first run that has not
#: migrated yet still routes to the main model.
#: Derived from ``jobs.MAIN_MODEL`` rather than spelled out, because these two are
#: not merely similar defaults -- they have to be the *same* model or a feature
#: stops working silently. ``_chain`` substitutes the user's configured main model
#: into a chain by testing ``key == jobs.MAIN_MODEL``; when the default disagrees
#: with the constant, that test never matches, the substitution never fires, every
#: chain keeps its hard-coded head, and "use the main model I chose in Settings"
#: becomes a label on the Settings screen that changes nothing. Nothing raises.
_MAIN_PROVIDER_DEFAULT, _MAIN_MODEL_DEFAULT = jobs.split_key(jobs.MAIN_MODEL)

_FALLBACK_SETTINGS: dict[str, Any] = {
    "router_mode": "auto",
    "pinned_provider": "",
    "pinned_model": "",
    "allow_fallback": True,
    "main_provider": _MAIN_PROVIDER_DEFAULT,
    "main_model": _MAIN_MODEL_DEFAULT,
}


def _settings() -> dict[str, Any]:
    try:
        from ..db.repos import app_settings

        return app_settings.get_all()
    except Exception:  # pragma: no cover - pre-migration or DB unavailable
        log.debug("router settings unavailable, using defaults", exc_info=True)
        return dict(_FALLBACK_SETTINGS)


def main_key(config: dict[str, Any] | None = None) -> str:
    """The configured main model, as a "provider:model" key."""
    config = config if config is not None else _settings()
    provider_id = str(config.get("main_provider") or _MAIN_PROVIDER_DEFAULT)
    model_id = str(config.get("main_model") or _MAIN_MODEL_DEFAULT)
    return f"{provider_id}:{model_id}"


# --------------------------------------------------------------------- router


class Router:
    def __init__(self) -> None:
        self._last: Decision | None = None

    # ------------------------------------------------------------- resolving

    def resolve(
        self,
        job: Job | str,
        *,
        needs_vision: bool = False,
        needs_tools: bool | None = None,
        min_context_tokens: int = 0,
    ) -> Decision:
        """Choose a model for a job, or explain why none can be used."""
        job_spec = jobs.spec(str(job))
        config = _settings()

        requires_tools = job_spec.requires_tools if needs_tools is None else needs_tools
        requires_vision = needs_vision or job_spec.requires_vision

        chain = self._chain(job_spec, config, requires_vision=requires_vision)
        # Read once per resolve, not once per candidate: this is a database query,
        # and a chain is up to six models long.
        blocked = self._blocked_models()
        considered: list[Candidate] = []
        chosen: Candidate | None = None
        reason = ""

        for candidate in chain:
            skip = self._why_not(
                candidate,
                job_spec,
                requires_tools=requires_tools,
                requires_vision=requires_vision,
                min_context_tokens=min_context_tokens,
                blocked=blocked,
            )
            if skip:
                candidate.skipped = skip
                considered.append(candidate)
                continue
            considered.append(candidate)
            if chosen is None:
                chosen = candidate
                reason = self._reason(candidate, job_spec, config, considered)

        if chosen is None:
            raise self._nothing_available(job_spec, considered)

        # Everything in `eligible` has already cleared `_why_not`, so the learner
        # is choosing between options that are all legitimately usable. It cannot
        # admit a model the checks rejected -- only prefer one survivor over
        # another -- which is what makes letting it decide safe (§15, §38).
        eligible = [c for c in considered if c.skipped is None]
        learned = ""
        if len(eligible) > 1:
            from ..learning import outcomes as learning

            preferred, learned = learning.prefer(job_spec.job, eligible)
            if learned and preferred and preferred[0].key != chosen.key:
                chosen = preferred[0]
                eligible = preferred
                # The reason the user reads must say the order came from
                # measurement, not from the chain: a swap the user cannot account
                # for is the silent substitution jobs.py forbids.
                reason = learned
            else:
                learned = ""

        fallbacks = [
            c for c in eligible if c.key != chosen.key
        ] if config.get("allow_fallback", True) else []

        decision = Decision(
            job=job_spec.job,
            transport=job_spec.transport,
            provider_id=chosen.provider_id,
            model_id=chosen.model_id,
            spec=chosen.spec,
            reason=reason,
            considered=considered,
            fallbacks=fallbacks,
            warnings=self._warnings(chosen, job_spec, config),
        )
        self._last = decision
        log.info("routed %s -> %s (%s)", job_spec.job, decision.key, reason)
        return decision

    def resolve_for_request(self, request: ChatRequest, *, job: Job | str = "chat") -> Decision:
        """Route a concrete chat request, accounting for what it contains."""
        return self.resolve(
            job,
            needs_vision=request.needs_vision(),
            needs_tools=bool(request.tools),
            # A request that already needs 40k tokens cannot go to a 32k model, and
            # finding that out from the provider costs a round trip and a
            # confusing error.
            min_context_tokens=request.approximate_input_tokens()
            + (request.max_output_tokens or 1_024),
        )

    @property
    def last_decision(self) -> Decision | None:
        return self._last

    # --------------------------------------------------------------- chaining

    def _chain(
        self,
        job_spec: JobSpec,
        config: dict[str, Any],
        *,
        requires_vision: bool,
    ) -> list[Candidate]:
        """Build the ordered candidate list for a job.

        Order of authority: a pin beats a per-job override, which beats the job's
        own chain. The main model is substituted in wherever the chain refers to
        it, so changing the main model in Settings changes every job that defers
        to it rather than only the label on the Settings screen.
        """
        keys: list[str] = []

        pinned = ""
        if str(config.get("router_mode") or "auto") == "pinned":
            provider_id = str(config.get("pinned_provider") or "")
            model_id = str(config.get("pinned_model") or "")
            if provider_id and model_id:
                pinned = f"{provider_id}:{model_id}"
        # A pin is a preference, not a promise the model can do every job. It goes
        # first and is skipped with a reason if it cannot -- pinning a text model
        # must not make "read this screenshot" fail silently.
        if pinned and job_spec.transport == "chat":
            keys.append(pinned)

        override = str(config.get(job_spec.setting) or "")
        if override and override not in keys:
            keys.append(override)

        configured_main = main_key(config)
        for key in job_spec.chain:
            resolved = configured_main if key == jobs.MAIN_MODEL else key
            if resolved not in keys:
                keys.append(resolved)

        # For a job that needs vision, the main model is only usable if it can see.
        # Rather than special-case that here, _why_not rejects it with a reason,
        # which keeps the explanation in one place.
        return [self._candidate(key) for key in keys]

    def _candidate(self, key: str) -> Candidate:
        provider_id, model_id = jobs.split_key(key)
        found = providers.find_model(key)
        return Candidate(
            key=key,
            provider_id=provider_id,
            model_id=model_id,
            spec=found[1] if found else None,
        )

    # -------------------------------------------------------------- filtering

    def _blocked_models(self) -> dict[str, str]:
        """Models a real call has proved this key cannot use.

        A provider listing a model does not mean your key can call it: NVIDIA
        lists ``cosmos-reason2-8b`` and answers 404 "not found for account" when
        this project's key asks for it. Once measured, that is remembered, and the
        model is skipped here rather than costing a failed round trip on every
        request that routes to it.

        Only durable failures reach this dict -- a timeout or a rate limit never
        removes a model from the bench. A database that cannot be read must not
        take the whole router down with it, so a failure here means "nothing is
        known to be blocked", which is the same state as a fresh install.
        """
        try:
            from ..db.repos import model_probes

            return model_probes.unusable()
        except Exception:  # noqa: BLE001 -- see docstring
            log.debug("could not read model probe results", exc_info=True)
            return {}

    def _why_not(
        self,
        candidate: Candidate,
        job_spec: JobSpec,
        *,
        requires_tools: bool,
        requires_vision: bool,
        min_context_tokens: int,
        blocked: dict[str, str] | None = None,
    ) -> str | None:
        """Why this candidate cannot serve the job, or None if it can."""
        # Checked before anything else, and for both transports: a model this key
        # cannot call is unusable no matter what its spec claims it supports.
        if blocked and candidate.key in blocked:
            return blocked[candidate.key]

        if job_spec.transport == "media":
            return self._why_not_media(candidate, job_spec)

        provider = providers.get(candidate.provider_id)
        if provider is None:
            return f"There is no provider called '{candidate.provider_id}'."
        if candidate.spec is None:
            return (
                f"{provider.name} does not list '{candidate.model_id}', so NEXUS "
                "cannot tell what it supports."
            )
        if not provider.has_credential():
            return f"No {provider.name} API key is configured."
        if requires_vision and not candidate.spec.supports_vision:
            return f"{candidate.spec.label} cannot look at images."
        if requires_tools and not candidate.spec.supports_tools:
            return f"{candidate.spec.label} cannot call tools."
        if min_context_tokens and min_context_tokens > candidate.spec.context_window:
            return (
                f"This needs about {min_context_tokens:,} tokens of context and "
                f"{candidate.spec.label} holds {candidate.spec.context_window:,}."
            )
        return None

    def _why_not_media(self, candidate: Candidate, job_spec: JobSpec) -> str | None:
        """Media-transport eligibility, which is provider-specific.

        "Media" describes a request *shape* -- audio or image bytes rather than
        chat messages -- not a vendor, and not one wire either. Three clients sit
        behind it: Mistral's HTTP audio API, NIM's HTTP endpoints for OCR and
        retrieval, and NIM's gRPC speech functions. An earlier version checked one
        catalogue and assumed the answer covered everything, which would now
        reject every speech model as "not in the NIM catalogue".
        """
        if candidate.provider_id == "mistral":
            served = providers.mistral_audio.serves(job_spec.job)
            if candidate.model_id not in served:
                return (
                    f"Mistral audio does not use '{candidate.model_id}' for "
                    f"{job_spec.job}."
                )
            if not providers.mistral_audio.has_credential():
                return "No Mistral API key is configured, so voice is unavailable."
            return None

        endpoint = providers.nim_catalogue.BY_MODEL.get(candidate.model_id)
        if endpoint is None:
            return f"{candidate.model_id} is not in the NIM specialist catalogue."
        if job_spec.job not in endpoint.jobs:
            return f"{endpoint.label} does not do {job_spec.job}."

        if endpoint.protocol == "grpc":
            # Speech. A different client and a different failure: the key is the
            # same one, but the riva package is an optional dependency, so it can
            # be absent on a working install. Saying which of the two is missing
            # is the difference between a fix and a guess.
            if not providers.nvidia_speech.has_credential():
                return "No NVIDIA API key is configured."
            if not providers.nvidia_speech.client().available():
                return (
                    f"{endpoint.label} needs the 'nvidia-riva-client' package, "
                    "which is not installed."
                )
            return None

        if not providers.nim_media.client().has_credential():
            return "No NVIDIA API key is configured."
        return None

    def _reason(
        self,
        candidate: Candidate,
        job_spec: JobSpec,
        config: dict[str, Any],
        considered: list[Candidate],
    ) -> str:
        """Plain-English justification, written for the user, not the log."""
        label = candidate.spec.label if candidate.spec else candidate.model_id
        skipped = [c for c in considered if c.skipped and c.key != candidate.key]

        if candidate.key == main_key(config):
            base = f"{label} is your main model."
        elif str(config.get(job_spec.setting) or "") == candidate.key:
            base = f"You chose {label} for {job_spec.title.lower()}."
        elif (
            str(config.get("router_mode") or "") == "pinned"
            and f"{config.get('pinned_provider')}:{config.get('pinned_model')}" == candidate.key
        ):
            base = f"{label} is pinned in Settings, so everything goes to it."
        elif job_spec.transport == "media":
            base = f"{label} is the specialist for {job_spec.title.lower()}."
        else:
            base = f"{label} is the best available fit for {job_spec.title.lower()}."

        if skipped:
            first = skipped[0]
            base += f" ({first.skipped})"
        return base

    def _warnings(
        self, candidate: Candidate, job_spec: JobSpec, config: dict[str, Any]
    ) -> list[str]:
        notes: list[str] = []
        if not config.get("allow_fallback", True):
            notes.append(
                "Fallback is turned off, so if this model fails NEXUS will report the "
                "failure rather than trying another one."
            )
        if candidate.spec and candidate.spec.notes and candidate.spec.free:
            notes.append(candidate.spec.notes)
        if candidate.provider_id == "nvidia":
            # Measured 2026-08-22: a cold free-tier NIM model took 53 s once and
            # 110 s another time to return five tokens, with a valid key both
            # times. Without saying so, the first request through this provider is
            # indistinguishable from a hang -- and a user who assumes NEXUS froze
            # will kill it a few seconds before the answer arrives.
            notes.append(
                "NVIDIA's free tier starts the model on first use, so a first "
                "request can take up to two minutes to begin answering. It is not "
                "stuck; later requests are fast."
            )
        return notes

    def _nothing_available(
        self, job_spec: JobSpec, considered: list[Candidate]
    ) -> ProviderError:
        """Turn an empty chain into an error that says what would fix it (§80)."""
        missing_keys = sorted(
            {
                c.provider_id
                for c in considered
                if c.skipped and "API key is configured" in c.skipped
            }
        )
        details = "; ".join(
            f"{c.spec.label if c.spec else c.model_id}: {c.skipped}"
            for c in considered
            if c.skipped
        )

        if missing_keys and len(missing_keys) == len({c.provider_id for c in considered}):
            names = [
                (providers.get(p).name if providers.get(p) else p) for p in missing_keys
            ]
            return MissingCredential(
                f"NEXUS cannot {job_spec.title.lower()} yet: no API key is configured "
                f"for {' or '.join(names)}. Add one in Settings -> Providers.",
                detail=details,
            )
        return NoModelAvailable(
            f"No configured model can handle {job_spec.title.lower()}. {details}",
            detail=details,
        )

    # -------------------------------------------------------------- streaming

    async def stream(
        self,
        request: ChatRequest,
        *,
        job: Job | str = "chat",
        decision: Decision | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a response, falling back between providers while it is still safe.

        "Still safe" means no text has reached the caller yet. Once it has, a
        different model continuing the same answer would produce a splice the user
        has no way to detect, so the partial text and the error are surfaced
        instead (§79).
        """
        chosen = decision or self.resolve_for_request(request, job=job)
        config = _settings()
        allow_fallback = bool(config.get("allow_fallback", True))

        attempts: list[Candidate] = [
            Candidate(
                key=chosen.key,
                provider_id=chosen.provider_id,
                model_id=chosen.model_id,
                spec=chosen.spec,
            )
        ]
        if allow_fallback:
            attempts.extend(chosen.fallbacks)

        errors: list[ProviderError] = []

        for index, candidate in enumerate(attempts):
            provider = providers.get(candidate.provider_id)
            if provider is None:  # pragma: no cover - filtered during resolve
                continue

            attempt_request = ChatRequest(
                messages=request.messages,
                model=candidate.model_id,
                system=request.system,
                tools=request.tools,
                temperature=request.temperature,
                max_output_tokens=request.max_output_tokens,
                tool_choice=request.tool_choice,
                stop=request.stop,
            )

            emitted_text = False
            first_token_ms: int | None = None
            started = time.monotonic()
            try:
                async for event in self._attempt(provider, attempt_request):
                    if event.type == "text" and event.text:
                        if not emitted_text:
                            first_token_ms = int((time.monotonic() - started) * 1000)
                        emitted_text = True
                    if not event.model:
                        event.model = candidate.key
                    yield event
                self._learn(chosen.job, candidate, ok=True, started=started,
                            first_token_ms=first_token_ms)
                return
            except ProviderError as exc:
                errors.append(exc)
                self._learn(chosen.job, candidate, ok=False, started=started,
                            first_token_ms=first_token_ms, error_kind=exc.kind)
                if emitted_text:
                    partial = getattr(exc, "partial_text", "")
                    raise StreamInterrupted(
                        f"{exc.message} The reply above stopped part-way; NEXUS did not "
                        "switch models mid-answer because that would join two different "
                        "replies together.",
                        partial_text=partial,
                        provider=candidate.provider_id,
                        model=candidate.model_id,
                        detail=exc.detail,
                    ) from exc

                remaining = attempts[index + 1 :]
                if not exc.fall_back or not remaining or not allow_fallback:
                    raise self._final_error(exc, errors) from exc
                log.warning(
                    "falling back from %s after %s: %s",
                    candidate.key,
                    exc.kind,
                    exc.message,
                )
                continue

        raise NoModelAvailable(
            "Every model NEXUS tried failed. " + " ".join(e.message for e in errors[:3]),
            detail="; ".join(f"{e.provider}: {e.kind}" for e in errors),
        )

    def _learn(
        self,
        job: Job,
        candidate: Candidate,
        *,
        ok: bool,
        started: float,
        first_token_ms: int | None = None,
        error_kind: str | None = None,
    ) -> None:
        """Hand one call's result to the learner. Cannot raise into a stream.

        Called from ``stream()`` on both the success and the ProviderError path,
        and deliberately *not* on cancellation: a user who stops a reply has not
        told us anything about the model, and recording that as a failure would
        teach the router to avoid whichever model the user interrupts most.
        """
        from ..learning import outcomes as learning

        learning.record(
            job=str(job),
            key=candidate.key,
            provider=candidate.provider_id,
            model=candidate.model_id,
            ok=ok,
            latency_ms=int((time.monotonic() - started) * 1000),
            first_token_ms=first_token_ms,
            error_kind=error_kind,
        )

    async def _attempt(
        self, provider: Provider, request: ChatRequest
    ) -> AsyncIterator[StreamEvent]:
        """One provider attempt, with a single short retry for rate limits.

        A rate limit with a short ``retry_after`` is worth waiting out: switching
        providers mid-conversation changes the voice of the reply for no reason.
        A long one is not, and falls through to the caller's fallback logic.
        """
        try:
            async for event in provider.stream(request):
                yield event
            return
        except RateLimited as exc:
            wait = exc.retry_after
            if wait is None or wait > 5.0:
                raise
            log.info("rate limited by %s, waiting %.1fs", provider.id, wait)
            await asyncio.sleep(wait)

        async for event in provider.stream(request):
            yield event

    def _final_error(
        self, last: ProviderError, errors: list[ProviderError]
    ) -> ProviderError:
        """Collapse a fallback chain into one message the user can act on.

        Six provider errors in a row is not six problems. When they are all
        network failures it is one -- the computer is offline -- and saying that
        once is more useful than listing them.
        """
        if len(errors) > 1 and len({e.kind for e in errors}) == 1:
            if last.kind == "network_unreachable":
                return type(last)(
                    "NEXUS cannot reach any model provider. This computer appears to be "
                    "offline.",
                    provider=last.provider,
                    detail="; ".join(e.message for e in errors),
                )
            if last.kind == "missing_credential":
                return MissingCredential(
                    "No provider has a working API key yet. Add one in "
                    "Settings -> Providers.",
                    detail="; ".join(e.message for e in errors),
                )
        return last


_ROUTER: Router | None = None


def router() -> Router:
    global _ROUTER
    if _ROUTER is None:
        _ROUTER = Router()
    return _ROUTER


def resolve(job: Job | str, **kwargs: Any) -> Decision:
    return router().resolve(job, **kwargs)


def catalogue() -> dict[str, Any]:
    """Jobs, their assignments and the main model, for the Settings screen."""
    config = _settings()
    return {
        "main_model": main_key(config),
        "router_mode": config.get("router_mode", "auto"),
        "allow_fallback": bool(config.get("allow_fallback", True)),
        "jobs": jobs.catalogue(),
        "assignments": {
            spec.job: str(config.get(spec.setting) or "") for spec in jobs.JOBS
        },
    }


__all__ = [
    "Candidate",
    "Decision",
    "Job",
    "NoModelAvailable",
    "Router",
    "catalogue",
    "jobs",
    "main_key",
    "resolve",
    "router",
]
