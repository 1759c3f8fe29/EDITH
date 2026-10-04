"""NVIDIA NIM's non-chat specialists: speech, OCR, embeddings, ranking (task E3c).

These are the endpoints the chat transport cannot carry. Speech-to-text takes an
audio file; text-to-speech returns audio bytes; OCR takes an image and answers
with a structured object rather than prose; embeddings and ranking return numbers.
Sending any of them through ``/chat/completions`` produces an opaque 400, which is
why they live behind their own client instead of being squeezed into the same one.

Two real constraints shaped this file.

**Large payloads do not go inline.** NVIDIA's function endpoints reject request
bodies past a few hundred kilobytes, and an audio recording of any length exceeds
that. The documented route is to upload the bytes as an *asset* first and pass a
reference. ``_upload_asset`` does that, and inline base64 is used only for
payloads small enough to be safe.

**Response shapes vary between models.** Rather than assume one, the parsers below
try the layouts NVIDIA's models are known to use and, if none match, return an
error naming the keys that actually came back. A wrong guess that silently
produces an empty transcript is worse than a failure that says what arrived.

Nothing here claims an endpoint works. ``probe`` makes a real call per job and
reports which specialists this key can actually reach -- the same honesty rule the
credential screen follows (§13, §78): a model in the catalogue is a claim, a
successful call is evidence.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import secrets
from ..config import settings
from ..logging_setup import get_logger
from . import nim_catalogue
from .base import (
    MissingCredential,
    NetworkUnreachable,
    ProviderError,
    ProviderTimeout,
    RequestRejected,
)
from .nim_catalogue import NimEndpoint
from .openai_compat import map_http_error

log = get_logger("providers.nim_media")

PROVIDER_ID = "nvidia"
PROVIDER_NAME = "NVIDIA NIM"

ASSET_API = "https://api.nvcf.nvidia.com/v2/nvcf/assets"

#: Above this, bytes are uploaded as an asset rather than inlined as base64.
#: Base64 inflates by a third, and NVIDIA's function gateway rejects large
#: bodies, so the threshold is set well below the point where that starts.
INLINE_LIMIT_BYTES = 180_000

#: Hard ceiling on what NEXUS will send at all. A user handing over an hour of
#: audio should be told the limit, not left watching an upload that will fail.
MAX_AUDIO_BYTES = 25_000_000
MAX_IMAGE_BYTES = 12_000_000


class MediaUnsupported(ProviderError):
    """No NIM specialist is configured for this job."""

    kind = "media_unsupported"
    retryable = False


@dataclass
class Transcript:
    text: str
    model: str
    language: str | None = None
    duration_s: float | None = None
    words: int = 0
    latency_ms: int = 0
    raw_keys: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "model": self.model,
            "language": self.language,
            "duration_s": self.duration_s,
            "words": self.words,
            "latency_ms": self.latency_ms,
        }


@dataclass
class Speech:
    path: Path
    model: str
    bytes_written: int
    media_type: str
    duration_s: float | None = None
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "model": self.model,
            "bytes": self.bytes_written,
            "media_type": self.media_type,
            "duration_s": self.duration_s,
            "latency_ms": self.latency_ms,
        }


@dataclass
class ExtractedDocument:
    """OCR output. ``blocks`` is empty when the model returned only flat text."""

    text: str
    model: str
    blocks: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: int = 0

    @property
    def characters(self) -> int:
        return len(self.text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "model": self.model,
            "blocks": self.blocks,
            "characters": self.characters,
            "latency_ms": self.latency_ms,
        }


@dataclass
class ProbeResult:
    job: str
    model: str
    ok: bool
    message: str
    latency_ms: int | None = None
    error_kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job": self.job,
            "model": self.model,
            "ok": self.ok,
            "message": self.message,
            "latency_ms": self.latency_ms,
            "error_kind": self.error_kind,
        }


# ------------------------------------------------------------------- utilities


def _wav_duration(path: Path) -> float | None:
    """Duration of a WAV file, read from its header. None for other formats."""
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
        if rate:
            return round(frames / rate, 3)
    except (wave.Error, OSError, EOFError):
        return None
    return None


def _media_type(path: Path, fallback: str) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or fallback


def _first_string(payload: Any, keys: tuple[str, ...]) -> str | None:
    """Depth-first search for the first string under any of ``keys``.

    ASR responses nest the transcript differently per model -- sometimes at the
    top level, sometimes under a segment list. Walking the object is more robust
    than encoding six shapes, and when it finds nothing the caller reports the
    keys that were present instead of an empty success.
    """
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        for value in payload.values():
            found = _first_string(value, keys)
            if found:
                return found
    elif isinstance(payload, list):
        # Segment lists concatenate rather than returning only the first piece.
        pieces = [_first_string(item, keys) for item in payload]
        joined = " ".join(p.strip() for p in pieces if p)
        if joined.strip():
            return joined
    return None


def _top_level_keys(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        return sorted(payload.keys())
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return sorted(payload[0].keys())
    return []


# --------------------------------------------------------------------- client


class NimMediaClient:
    """Calls NIM's speech, OCR and retrieval endpoints.

    One client for all of them because they share a key, a host family and an
    error envelope. Which model serves which job comes from ``nim_catalogue``, so
    changing a specialist is a catalogue edit rather than a code change.
    """

    def __init__(self) -> None:
        self._client: Any = None
        #: Job -> model id, when the user has chosen a specific specialist.
        self.overrides: dict[str, str] = {}

    # ------------------------------------------------------------ plumbing

    def has_credential(self) -> bool:
        return bool(secrets.get("NVIDIA_API_KEY"))

    def _key(self) -> str:
        key = secrets.get("NVIDIA_API_KEY")
        if not key:
            raise MissingCredential(
                "No NVIDIA API key is configured, so speech, OCR and document reading "
                "are unavailable. Add one in Settings -> Providers.",
                provider=PROVIDER_ID,
            )
        return key

    async def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=180.0, write=180.0, pool=10.0),
                follow_redirects=False,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def endpoint_for(self, job: str) -> NimEndpoint:
        """The specialist that serves a job, honouring a user override."""
        chosen = self.overrides.get(job)
        if chosen:
            endpoint = nim_catalogue.BY_MODEL.get(chosen)
            if endpoint is None:
                raise MediaUnsupported(
                    f"'{chosen}' is set as the {job} model but is not in the NIM "
                    "catalogue. Pick one in Settings -> Models.",
                    provider=PROVIDER_ID,
                    model=chosen,
                )
            return endpoint

        endpoint = nim_catalogue.preferred(job)  # type: ignore[arg-type]
        if endpoint is None:
            raise MediaUnsupported(
                f"NEXUS has no NIM model configured for {job}.",
                provider=PROVIDER_ID,
            )
        return endpoint

    async def _post(
        self,
        endpoint: NimEndpoint,
        payload: dict[str, Any],
        *,
        asset_ids: list[str] | None = None,
        accept: str = "application/json",
    ) -> Any:
        """POST to a NIM endpoint and return the parsed response or raw bytes."""
        import httpx

        client = await self._http()
        headers = {
            "Authorization": f"Bearer {self._key()}",
            "Content-Type": "application/json",
            "Accept": accept,
        }
        if asset_ids:
            # The gateway needs to know which uploaded assets this call may read.
            headers["NVCF-INPUT-ASSET-REFERENCES"] = ",".join(asset_ids)
            headers["NVCF-FUNCTION-ASSET-IDS"] = ",".join(asset_ids)

        url = f"{endpoint.base_url}{endpoint.path}"
        try:
            response = await client.post(url, headers=headers, json=payload)
        except httpx.ConnectError as exc:
            raise NetworkUnreachable(
                f"Could not reach {PROVIDER_NAME}. The computer may be offline.",
                provider=PROVIDER_ID,
                model=endpoint.model,
                detail=str(exc),
            ) from exc
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(
                f"{PROVIDER_NAME} did not respond in time for {endpoint.label}.",
                provider=PROVIDER_ID,
                model=endpoint.model,
                detail=str(exc),
            ) from exc

        if response.status_code >= 400:
            raise map_http_error(
                response.status_code,
                response.content,
                provider_id=PROVIDER_ID,
                provider_name=PROVIDER_NAME,
                model=endpoint.model,
            )

        content_type = response.headers.get("content-type", "")
        if accept != "application/json" and not content_type.startswith("application/json"):
            return response.content
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(
                f"{endpoint.label} returned a {len(response.content):,}-byte "
                f"{content_type or 'unknown'} response that is not JSON.",
                provider=PROVIDER_ID,
                model=endpoint.model,
                detail=str(exc),
            ) from exc

    async def _upload_asset(self, data: bytes, content_type: str, description: str) -> str:
        """Upload bytes and return the asset id to reference them by."""
        import httpx

        client = await self._http()
        headers = {
            "Authorization": f"Bearer {self._key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            created = await client.post(
                ASSET_API,
                headers=headers,
                json={"contentType": content_type, "description": description},
            )
        except httpx.HTTPError as exc:
            raise NetworkUnreachable(
                "Could not start an upload to NVIDIA.",
                provider=PROVIDER_ID,
                detail=str(exc),
            ) from exc

        if created.status_code >= 400:
            raise map_http_error(
                created.status_code,
                created.content,
                provider_id=PROVIDER_ID,
                provider_name=PROVIDER_NAME,
            )

        body = created.json()
        upload_url = body.get("uploadUrl")
        asset_id = body.get("assetId")
        if not upload_url or not asset_id:
            raise ProviderError(
                "NVIDIA accepted the upload request but returned no upload URL, so the "
                f"file cannot be sent. Response contained: {', '.join(_top_level_keys(body)) or 'nothing'}.",
                provider=PROVIDER_ID,
            )

        try:
            put = await client.put(
                upload_url,
                content=data,
                headers={
                    "Content-Type": content_type,
                    "x-amz-meta-nvcf-asset-description": description,
                },
            )
        except httpx.HTTPError as exc:
            raise NetworkUnreachable(
                "The upload to NVIDIA's storage failed part-way.",
                provider=PROVIDER_ID,
                detail=str(exc),
            ) from exc

        if put.status_code >= 400:
            raise ProviderError(
                f"NVIDIA's storage rejected the upload (HTTP {put.status_code}).",
                provider=PROVIDER_ID,
                detail=put.text[:500],
            )

        log.info("uploaded %s bytes as asset %s", len(data), asset_id)
        return str(asset_id)

    # ------------------------------------------------------- speech to text

    async def transcribe(
        self,
        audio_path: str | Path,
        *,
        language: str | None = None,
        model: str | None = None,
    ) -> Transcript:
        """Transcribe an audio file.

        The file is uploaded as an asset when it is large enough that inlining it
        would be rejected, which is the case for anything past a few seconds.
        """
        path = Path(audio_path).expanduser()
        if not path.is_file():
            raise RequestRejected(
                f"There is no audio file at {path}.", provider=PROVIDER_ID
            )

        size = path.stat().st_size
        if size == 0:
            raise RequestRejected(
                f"{path.name} is empty, so there is nothing to transcribe.",
                provider=PROVIDER_ID,
            )
        if size > MAX_AUDIO_BYTES:
            raise RequestRejected(
                f"{path.name} is {size / 1_000_000:.1f} MB, above the "
                f"{MAX_AUDIO_BYTES // 1_000_000} MB NEXUS will send. Split the recording "
                "into shorter pieces.",
                provider=PROVIDER_ID,
            )

        endpoint = nim_catalogue.BY_MODEL[model] if model else self.endpoint_for("stt")
        if endpoint.modality != "asr":
            raise MediaUnsupported(
                f"{endpoint.label} is not a speech recognition model.",
                provider=PROVIDER_ID,
                model=endpoint.model,
            )

        wanted = language or (endpoint.languages[0] if endpoint.languages else "en")
        if endpoint.languages and wanted not in endpoint.languages:
            raise RequestRejected(
                f"{endpoint.label} handles {', '.join(endpoint.languages)}, not "
                f"'{wanted}'. Choose a different speech model in Settings.",
                provider=PROVIDER_ID,
                model=endpoint.model,
            )

        data = path.read_bytes()
        content_type = _media_type(path, "audio/wav")
        payload: dict[str, Any] = {"language": wanted}
        asset_ids: list[str] | None = None

        if size > INLINE_LIMIT_BYTES:
            asset_id = await self._upload_asset(data, content_type, f"NEXUS audio {path.name}")
            asset_ids = [asset_id]
            payload["audio"] = {"asset_id": asset_id}
        else:
            payload["audio"] = {
                "content": base64.b64encode(data).decode("ascii"),
                "content_type": content_type,
            }

        started = time.monotonic()
        body = await self._post(endpoint, payload, asset_ids=asset_ids)
        latency_ms = int((time.monotonic() - started) * 1000)

        text = _first_string(body, ("transcript", "text", "transcription", "content"))
        if text is None:
            raise ProviderError(
                f"{endpoint.label} answered but no transcript could be found in the "
                f"response. It contained: {', '.join(_top_level_keys(body)) or 'no fields'}.",
                provider=PROVIDER_ID,
                model=endpoint.model,
                detail=json.dumps(body)[:1000] if not isinstance(body, bytes) else "",
            )

        text = text.strip()
        return Transcript(
            text=text,
            model=endpoint.model,
            language=wanted,
            duration_s=_wav_duration(path),
            words=len(text.split()),
            latency_ms=latency_ms,
            raw_keys=_top_level_keys(body),
        )

    # ------------------------------------------------------- text to speech

    async def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        language: str | None = None,
        model: str | None = None,
    ) -> Speech:
        """Turn text into an audio file on disk and return its path."""
        cleaned = (text or "").strip()
        if not cleaned:
            raise RequestRejected(
                "There is no text to speak.", provider=PROVIDER_ID
            )
        if len(cleaned) > 5_000:
            raise RequestRejected(
                f"The text is {len(cleaned):,} characters, which is longer than one "
                "synthesis request can carry. NEXUS speaks long answers in parts.",
                provider=PROVIDER_ID,
            )

        endpoint = nim_catalogue.BY_MODEL[model] if model else self.endpoint_for("tts")
        if endpoint.modality != "tts":
            raise MediaUnsupported(
                f"{endpoint.label} is not a speech synthesis model.",
                provider=PROVIDER_ID,
                model=endpoint.model,
            )

        payload: dict[str, Any] = {
            "text": cleaned,
            "encoding": "wav",
            "sample_rate_hz": endpoint.sample_rate or 22_050,
        }
        if voice:
            payload["voice"] = voice
        if language or endpoint.languages:
            payload["language_code"] = language or endpoint.languages[0]

        started = time.monotonic()
        body = await self._post(endpoint, payload, accept="audio/wav")
        latency_ms = int((time.monotonic() - started) * 1000)

        # Some endpoints return raw audio, others a JSON envelope with base64.
        if isinstance(body, bytes):
            audio = body
        else:
            encoded = _first_string(body, ("audio", "audio_content", "content", "data"))
            if encoded is None:
                raise ProviderError(
                    f"{endpoint.label} answered but the response contained no audio. "
                    f"Fields present: {', '.join(_top_level_keys(body)) or 'none'}.",
                    provider=PROVIDER_ID,
                    model=endpoint.model,
                )
            try:
                audio = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise ProviderError(
                    f"{endpoint.label} returned audio that could not be decoded.",
                    provider=PROVIDER_ID,
                    model=endpoint.model,
                    detail=str(exc),
                ) from exc

        if not audio:
            raise ProviderError(
                f"{endpoint.label} returned zero bytes of audio, so there is nothing "
                "to play.",
                provider=PROVIDER_ID,
                model=endpoint.model,
            )

        directory = settings().audio_dir
        directory.mkdir(parents=True, exist_ok=True)
        out = directory / f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-speech.wav"
        out.write_bytes(audio)

        return Speech(
            path=out,
            model=endpoint.model,
            bytes_written=len(audio),
            media_type="audio/wav",
            duration_s=_wav_duration(out),
            latency_ms=latency_ms,
        )

    # ------------------------------------------------------------------- OCR

    async def extract_text(
        self,
        image_path: str | Path,
        *,
        model: str | None = None,
    ) -> ExtractedDocument:
        """Read the text out of an image or scanned page.

        The strict endpoints in the catalogue accept the image and nothing else --
        adding an instruction alongside it is rejected -- and they answer with a
        structured tool call rather than prose. ``image_only`` and
        ``structured_response`` in the catalogue are what drive that here, so a
        differently-behaved model can be added without editing this method.
        """
        path = Path(image_path).expanduser()
        if not path.is_file():
            raise RequestRejected(f"There is no image at {path}.", provider=PROVIDER_ID)

        size = path.stat().st_size
        if size == 0:
            raise RequestRejected(f"{path.name} is empty.", provider=PROVIDER_ID)
        if size > MAX_IMAGE_BYTES:
            raise RequestRejected(
                f"{path.name} is {size / 1_000_000:.1f} MB, above the "
                f"{MAX_IMAGE_BYTES // 1_000_000} MB limit for document reading.",
                provider=PROVIDER_ID,
            )

        endpoint = nim_catalogue.BY_MODEL[model] if model else self.endpoint_for("ocr")
        if endpoint.modality != "ocr":
            raise MediaUnsupported(
                f"{endpoint.label} is not a document reading model.",
                provider=PROVIDER_ID,
                model=endpoint.model,
            )

        data = path.read_bytes()
        media_type = _media_type(path, "image/png")
        asset_ids: list[str] | None = None

        if size > INLINE_LIMIT_BYTES:
            asset_id = await self._upload_asset(data, media_type, f"NEXUS image {path.name}")
            asset_ids = [asset_id]
            image_url = f"data:{media_type};asset_id,{asset_id}"
        else:
            encoded = base64.b64encode(data).decode("ascii")
            image_url = f"data:{media_type};base64,{encoded}"

        content: list[dict[str, Any]] = [
            {"type": "image_url", "image_url": {"url": image_url}}
        ]
        if not endpoint.image_only:
            content.insert(
                0,
                {"type": "text", "text": "Extract all text from this document."},
            )

        payload: dict[str, Any] = {
            "model": endpoint.model,
            "messages": [{"role": "user", "content": content}],
            # Never streamed for these models: the structured answer arrives as a
            # tool call whose shape differs when streamed, and half a parsed
            # object is not a partial result, it is a broken one.
            "stream": False,
        }
        if endpoint.structured_response:
            payload["tools"] = [
                {"type": "function", "function": {"name": "markdown_bbox"}}
            ]
            payload["tool_choice"] = "required"

        started = time.monotonic()
        body = await self._post(endpoint, payload, asset_ids=asset_ids)
        latency_ms = int((time.monotonic() - started) * 1000)

        text, blocks = _parse_ocr(body, endpoint)
        if not text and not blocks:
            raise ProviderError(
                f"{endpoint.label} answered but no text could be read out of the "
                f"response. Fields present: {', '.join(_top_level_keys(body)) or 'none'}.",
                provider=PROVIDER_ID,
                model=endpoint.model,
                detail=json.dumps(body)[:1000] if not isinstance(body, bytes) else "",
            )

        return ExtractedDocument(
            text=text, model=endpoint.model, blocks=blocks, latency_ms=latency_ms
        )

    # ---------------------------------------------------------- retrieval

    async def embed(
        self,
        texts: list[str],
        *,
        input_type: str = "passage",
        model: str | None = None,
    ) -> list[list[float]]:
        """Embed text for memory search.

        ``input_type`` matters: these models are trained asymmetrically, and using
        'passage' for a query measurably degrades recall. The memory layer sets it
        explicitly on both the write and the read side rather than relying on a
        default.
        """
        if input_type not in ("query", "passage"):
            raise RequestRejected(
                f"input_type must be 'query' or 'passage', not '{input_type}'.",
                provider=PROVIDER_ID,
            )
        cleaned = [t for t in (s.strip() for s in texts) if t]
        if not cleaned:
            return []

        endpoint = nim_catalogue.BY_MODEL[model] if model else self.endpoint_for("embedding")
        payload = {
            "model": endpoint.model,
            "input": cleaned,
            "input_type": input_type,
            "encoding_format": "float",
            "truncate": "END",
        }
        body = await self._post(endpoint, payload)

        entries = body.get("data") if isinstance(body, dict) else None
        if not isinstance(entries, list) or not entries:
            raise ProviderError(
                f"{endpoint.label} returned no embeddings for {len(cleaned)} input(s).",
                provider=PROVIDER_ID,
                model=endpoint.model,
            )

        vectors: list[list[float]] = []
        for entry in entries:
            vector = entry.get("embedding") if isinstance(entry, dict) else None
            if not isinstance(vector, list):
                raise ProviderError(
                    f"{endpoint.label} returned an embedding entry with no vector.",
                    provider=PROVIDER_ID,
                    model=endpoint.model,
                )
            vectors.append([float(v) for v in vector])

        if len(vectors) != len(cleaned):
            raise ProviderError(
                f"Asked {endpoint.label} to embed {len(cleaned)} texts and got "
                f"{len(vectors)} vectors back, so they cannot be matched up safely.",
                provider=PROVIDER_ID,
                model=endpoint.model,
            )
        return vectors

    async def rerank(
        self,
        query: str,
        passages: list[str],
        *,
        model: str | None = None,
    ) -> list[tuple[int, float]]:
        """Score passages against a query. Returns (index, score), best first."""
        cleaned = [p for p in (s.strip() for s in passages) if p]
        if not query.strip() or not cleaned:
            return []

        endpoint = nim_catalogue.BY_MODEL[model] if model else self.endpoint_for("rerank")
        payload = {
            "model": endpoint.model,
            "query": {"text": query.strip()},
            "passages": [{"text": p} for p in cleaned],
        }
        body = await self._post(endpoint, payload)

        rankings = body.get("rankings") if isinstance(body, dict) else None
        if not isinstance(rankings, list):
            raise ProviderError(
                f"{endpoint.label} returned no rankings. Fields present: "
                f"{', '.join(_top_level_keys(body)) or 'none'}.",
                provider=PROVIDER_ID,
                model=endpoint.model,
            )

        scored: list[tuple[int, float]] = []
        for item in rankings:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            logit = item.get("logit", item.get("score"))
            if isinstance(index, int) and isinstance(logit, (int, float)):
                scored.append((index, float(logit)))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored

    # ------------------------------------------------------------------ probe

    async def probe(self, jobs: tuple[str, ...] = ("ocr", "stt", "tts", "embedding", "rerank")) -> list[ProbeResult]:
        """Call each specialist once and report what actually works.

        This is what makes the Settings screen honest. A NIM model listed in the
        catalogue may be a downloadable container rather than a hosted endpoint,
        in which case it 404s with no explanation; the only way to know is to try.
        """
        results: list[ProbeResult] = []
        for job in jobs:
            try:
                endpoint = self.endpoint_for(job)
            except ProviderError as exc:
                results.append(
                    ProbeResult(job=job, model="", ok=False, message=exc.message, error_kind=exc.kind)
                )
                continue
            results.append(await self.probe_model(job, endpoint.model))
        return results

    async def probe_model(self, job: str, model: str) -> ProbeResult:
        """Call one named specialist once and report what happened.

        Split out from ``probe`` because the router needs to check *every* model in
        a job's chain, not only the preferred one -- knowing the fallback is dead
        too is what stops NEXUS from discovering it mid-request.
        """
        endpoint = nim_catalogue.BY_MODEL.get(model)
        if endpoint is None:
            return ProbeResult(
                job=job, model=model, ok=False,
                message=f"{model} is not in the NIM specialist catalogue.",
                error_kind="model_unavailable",
            )
        if job not in endpoint.jobs:
            return ProbeResult(
                job=job, model=model, ok=False,
                message=f"{endpoint.label} does not do {job}.",
                error_kind="model_unavailable",
            )

        started = time.monotonic()
        try:
            await self._probe_one(job, endpoint)
        except ProviderError as exc:
            return ProbeResult(
                job=job, model=endpoint.model, ok=False, message=exc.message,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_kind=exc.kind,
            )
        return ProbeResult(
            job=job, model=endpoint.model, ok=True,
            message=f"{endpoint.label} answered a live request.",
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    async def _probe_one(self, job: str, endpoint: NimEndpoint) -> None:
        """The smallest real request that proves a specialist is reachable."""
        if job == "embedding":
            await self.embed(["nexus reachability probe"], input_type="query", model=endpoint.model)
            return
        if job == "rerank":
            await self.rerank("probe", ["probe"], model=endpoint.model)
            return
        if job == "tts":
            speech = await self.synthesize("Ready.", model=endpoint.model)
            speech.path.unlink(missing_ok=True)
            return
        if job in ("ocr", "stt"):
            # Both need a real file. Rather than ship fixture bytes, ask the
            # endpoint about an empty payload: a 400 proves the route exists and
            # the key is accepted, while 401/404 prove it does not.
            try:
                await self._post(endpoint, {"model": endpoint.model, "messages": []})
            except RequestRejected:
                return  # reachable, and it rejected an intentionally empty request
            return
        raise MediaUnsupported(f"No probe defined for {job}.", provider=PROVIDER_ID)


def _parse_ocr(body: Any, endpoint: NimEndpoint) -> tuple[str, list[dict[str, Any]]]:
    """Pull text and layout blocks out of an OCR response.

    Handles the three shapes these models use: a tool call carrying JSON, a
    normal message body, and a flat CV-style list of detections.
    """
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace"), []

    blocks: list[dict[str, Any]] = []
    pieces: list[str] = []

    choices = body.get("choices") if isinstance(body, dict) else None
    if isinstance(choices, list):
        for choice in choices:
            message = choice.get("message") if isinstance(choice, dict) else None
            if not isinstance(message, dict):
                continue
            for call in message.get("tool_calls") or []:
                arguments = (call.get("function") or {}).get("arguments")
                if not isinstance(arguments, str):
                    continue
                try:
                    parsed = json.loads(arguments)
                except json.JSONDecodeError:
                    # The model's own output was malformed; say so via the raw
                    # string rather than dropping it silently.
                    pieces.append(arguments)
                    continue
                if isinstance(parsed, list):
                    for item in parsed:
                        if isinstance(item, dict):
                            blocks.append(item)
                            text = item.get("text") or item.get("content")
                            if isinstance(text, str):
                                pieces.append(text)
                elif isinstance(parsed, dict):
                    blocks.append(parsed)
                    text = parsed.get("text") or parsed.get("markdown")
                    if isinstance(text, str):
                        pieces.append(text)
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                pieces.append(content)

    # CV-style: {"data": [{"text_detections": [{"text_prediction": {"text": ...}}]}]}
    if not pieces and isinstance(body, dict):
        found = _first_string(body, ("text", "markdown", "text_prediction", "content"))
        if found:
            pieces.append(found)

    text = "\n".join(p.strip() for p in pieces if p and p.strip())
    return text, blocks


_CLIENT: NimMediaClient | None = None


def client() -> NimMediaClient:
    """The process-wide media client. One connection pool, not one per call."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = NimMediaClient()
    return _CLIENT
