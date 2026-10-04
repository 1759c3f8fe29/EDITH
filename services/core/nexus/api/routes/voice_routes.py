"""Voice: hearing the user and answering out loud (spec §12, §37, §94).

NEXUS is voice-first, so these three routes are the product's front door rather
than an accessory.

Speech runs on NVIDIA's Parakeet and Magpie, with Mistral Voxtral behind them,
and the router owns that choice: these routes ask ``resolve('stt')`` /
``resolve('tts')`` and then call whichever provider came back, through
``providers.speech``. They name no model and no vendor, so the bench can change
without this file being touched -- and, more importantly, the model reported to
the user is always the one that actually ran.

NVIDIA's speech is reached over gRPC rather than HTTP. That is worth saying here
because this docstring used to say the opposite -- that NIM served no ASR or TTS
at all -- which was a wrong conclusion drawn from real evidence: the REST paths
do 404, and the hosted model listing does name no speech id. Both are still true.
Speech simply lives on a different wire, at ``grpc.nvcf.nvidia.com``, addressed
by function id. See ``providers/nvidia_speech.py`` for how the two were told
apart, and ``providers/nim_catalogue.py`` for the three shapes of 404 involved.

Both directions are multilingual, verified rather than advertised: five languages
synthesised and transcribed back to their own words.

``/transcribe`` and ``/speak`` both POST because both spend the user's quota on a
real network call. ``/voices`` is a GET: it reads a list and costs nothing.

``/converse`` is the voice-first path itself, and the reason the other two are not
enough. Recording, then transcribing, then thinking, then speaking, as four requests,
serialises four waits the user sits through -- and the last of them is the whole answer
before the first word of it. This route runs the same turn as ``/api/chat`` but streams
audio per sentence as the sentences appear, so speaking starts while the model is still
writing. See :mod:`nexus.ai.voice_stream` for the ordering guarantees and
:mod:`nexus.ai.speech_stream` for where a sentence is allowed to be cut.

What it is *not*, and will not claim to be: frame-level streaming ASR. The user's audio
is transcribed in one call. Parakeet does have a streaming recogniser, and reaching it
is a real possibility now that the gRPC path exists -- but it is not what this route
does today, and the ``heard`` event says so by carrying the transcript the rest of the
turn was built from. So the hearing half is per-utterance while the thinking and
speaking halves are per-token and per-sentence.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from .. import sse, turns
from ...ai import persona, voice_stream
from ...config import settings
from ...db.repos import app_settings
from ...logging_setup import get_logger
from ...providers import speech as speech_provider
from ...providers.base import ProviderError
from ...router import NoModelAvailable, resolve as resolve_job

router = APIRouter(prefix="/api/voice", tags=["voice"])
log = get_logger("api.voice")

#: Refuse an upload larger than this before reading it into memory. The provider
#: has its own ceiling, but finding out after a 25 MB upload wastes the user's
#: bandwidth to learn something knowable up front.
MAX_UPLOAD_BYTES = 25_000_000


def _resolved(job: str) -> Any:
    try:
        return resolve_job(job)
    except NoModelAvailable as exc:
        # 409 rather than 500: nothing is broken, there is simply no model for this
        # job with the keys currently configured, and that is fixable in Settings.
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _audio_path(name: str) -> Path:
    """Resolve a synthesis filename inside the audio cache, or refuse.

    The name arrives from a URL, so it is checked rather than trusted: no path
    separators, no ``..``, a ``.wav`` suffix, and -- after resolving symlinks -- the
    file must still sit directly inside ``audio_dir``. A route that serves "whatever
    path you name" from a directory the UI knows would be a file-read capability with
    no permission check in front of it, which is exactly what the permission engine
    exists to prevent.

    The separator check is **deliberately redundant** and is not to be "simplified"
    away. Deleting it lets nothing escape -- measured, not assumed: every traversal
    shape (``../../../etc/passwd.wav``, ``/etc/shadow.wav``, ``sub/dir.wav``,
    ``....//x.wav``) is still refused by the resolved-parent check below, and the only
    behaviour that changes is a 410 instead of a 400 for a separator-containing name
    that resolves to the same absent file. So it survives mutation on purpose, recorded
    here rather than pinned by a contrived test. It stays because it fails earlier and
    more explicitly, and because a traversal boundary whose *sole* defence is one
    ``resolve()`` comparison is one careless refactor away from being no defence.
    """
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        raise HTTPException(status_code=400, detail="That is not an audio file name.")
    if not name.endswith(".wav"):
        raise HTTPException(status_code=400, detail="Only synthesised .wav files are served here.")
    directory = settings().audio_dir.resolve()
    candidate = (directory / name).resolve()
    if candidate.parent != directory:
        raise HTTPException(status_code=400, detail="That is not an audio file name.")
    if not candidate.is_file():
        # 410 rather than 404: the file was real and was cleaned up, which is a
        # different thing for the caller than a name that never existed.
        raise HTTPException(
            status_code=410,
            detail="That speech file is no longer on disk. Ask for it to be spoken again.",
        )
    return candidate


@router.get("/voices")
def voices_offline() -> dict[str, Any]:
    """The default voice and how many are known, without a network call.

    Deliberately separate from ``/voices/live``: a settings panel opening should not
    depend on a provider being reachable, and the live list is only needed when the
    user actually opens the voice picker.

    The default is the routed provider's own, not a constant. The two providers'
    voice names have nothing in common -- a Mistral slug handed to NVIDIA is a
    rejected request, not a different accent -- so answering with the wrong one
    would put a name in the UI that cannot be used.
    """
    try:
        decision = resolve_job("tts")
    except NoModelAvailable as exc:
        return {"default": "", "configured": False, "unavailable": str(exc)}
    return {
        "default": speech_provider.default_voice(decision.provider_id),
        "configured": speech_provider.has_credential(decision.provider_id),
        "routed": decision.key,
    }


@router.get("/voices/live")
async def voices_live() -> dict[str, Any]:
    """Read the real voice list for whichever provider is serving speech.

    Not hard-coded because it is not one list: Mistral's is per-account and read
    over the network (cloned voices appear there too), while NVIDIA's is the set
    of named speakers its deployment actually answers to. Asking the routed
    provider is what keeps the picker from offering voices that do not exist.
    """
    decision = _resolved("tts")
    if not speech_provider.has_credential(decision.provider_id):
        raise HTTPException(
            status_code=409,
            detail=f"No API key is configured for {decision.provider_id}, so voice "
                   "is unavailable.",
        )
    try:
        items = await speech_provider.voices(decision.provider_id, decision.model_id)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "voices": items,
        "count": len(items),
        "default": speech_provider.default_voice(decision.provider_id),
        "routed": decision.key,
    }


@router.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    language: str | None = Form(None),
) -> dict[str, Any]:
    """Turn an uploaded recording into text.

    Returns the model that heard it alongside the text, because §26 requires the
    routing to be visible: when a transcript is wrong the user needs to know which
    model produced it, not just that "voice" failed.

    The language is the caller's if it sent one, otherwise the ``input_language``
    setting. Passing it matters: the model guesses otherwise, and a guess is what
    turns an English sentence into transliterated nonsense.
    """
    decision = _resolved("stt")
    spoken = (language or "").strip() or str(app_settings.get("input_language", "") or "").strip()
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The recording is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"The recording is {len(data) / 1_000_000:.1f} MB, over the "
                   f"{MAX_UPLOAD_BYTES / 1_000_000:.0f} MB limit. Record shorter takes.",
        )

    try:
        transcript = await speech_provider.transcribe(
            data,
            provider_id=decision.provider_id,
            model=decision.model_id,
            language=spoken or None,
        )
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not transcript.text:
        # A successful call that heard nothing is not an error -- silence, or a muted
        # microphone, produces exactly this. Saying so beats an empty string the UI
        # would render as a blank message.
        return {
            **transcript.to_dict(),
            "routed": decision.key,
            "empty": True,
            "message": "No speech was found in that recording.",
        }
    return {**transcript.to_dict(), "routed": decision.key, "empty": False}


@router.post("/speak")
async def speak(
    text: str = Body(..., embed=True),
    voice: str | None = Body(None, embed=True),
    language: str | None = Body(None, embed=True),
) -> dict[str, Any]:
    """Speak text, returning a path to a playable WAV.

    The response carries both a filesystem ``path`` and a ``url``. The path is for
    the desktop shell, which can hand it to the system player; the url is for the web
    view, which cannot open ``file://`` from an http origin and so needs
    ``/api/voice/audio/{name}``. Returning a location rather than the bytes means the
    same synthesis can be replayed without paying for it twice.

    The voice is the caller's if it named one, otherwise the ``tts_voice`` setting,
    otherwise the provider's own default -- so choosing a voice in Settings actually
    changes what the user hears. A stored voice belonging to a *different* provider
    is ignored rather than sent: see ``providers.speech.voice_for``.

    The language is the caller's, else ``output_language``, else the provider's
    default. It is what makes synthesis multilingual: Magpie speaks twelve
    languages from one voice, chosen by this parameter.
    """
    if not text.strip():
        raise HTTPException(status_code=400, detail="There is no text to speak.")
    decision = _resolved("tts")
    # The limit belongs to whichever provider was routed to, and the two differ.
    # Asked rather than hard-coded: this route used to check Mistral's constant
    # unconditionally, which would refuse text NVIDIA would have spoken.
    limit = speech_provider.max_chars(decision.provider_id)
    if limit and len(text) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"That is {len(text):,} characters; one synthesis request carries "
                   f"{limit:,}. Speak it in parts.",
        )
    chosen = speech_provider.voice_for(
        decision.provider_id, voice, str(app_settings.get("tts_voice", "") or "")
    )
    spoken = (language or "").strip() or str(
        app_settings.get("output_language", "") or ""
    ).strip()
    try:
        speech = await speech_provider.synthesize(
            text,
            provider_id=decision.provider_id,
            model=decision.model_id,
            voice=chosen,
            language=spoken or None,
        )
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    body = speech.to_dict()
    name = Path(str(body.get("path") or "")).name
    return {
        **body,
        "url": f"/api/voice/audio/{name}" if name else None,
        "routed": decision.key,
        "voice": chosen or speech_provider.default_voice(decision.provider_id),
        "language": spoken or None,
    }


@router.get("/audio/{name}")
def audio(name: str) -> FileResponse:
    """Serve one synthesised WAV so the web view can play it.

    Only files this core wrote into ``audio_dir`` are reachable, and only by name --
    see :func:`_audio_path`. ``no-store`` because the file may be cleaned up at any
    time and a cached 200 for a file that is gone is worse than a fresh 410.
    """
    path = _audio_path(name)
    return FileResponse(
        path,
        media_type="audio/wav",
        filename=name,
        headers={"Cache-Control": "no-store"},
    )


# ------------------------------------------------------- one spoken turn, streamed

#: Strong references to running spoken turns. Required rather than tidy: ``asyncio`` keeps
#: only a weak reference to a task, so a turn whose result nobody awaits can be garbage
#: collected mid-flight -- the audio stops, the database write in ``AiCore.stream``'s
#: ``finally`` never happens, and nothing raises to say so.
_TURNS: set[asyncio.Task[None]] = set()

#: Base64 inflates by four bytes per three, plus padding. Checked against the encoded
#: length so an over-sized recording is refused before it is decoded into memory -- the
#: same reason :data:`MAX_UPLOAD_BYTES` is checked before reading the multipart body.
MAX_AUDIO_BASE64 = MAX_UPLOAD_BYTES * 4 // 3 + 8


def _decode_audio(value: str) -> bytes:
    """Decode a base64 recording from a JSON body, or refuse with a reason.

    JSON rather than multipart because this route's response is a stream: a client that
    posts a form and reads server-sent events off the same request works, but every proxy
    and fetch wrapper in the path has to agree about it, and the recording is small enough
    that base64's third is cheaper than that argument.

    There is no "decoded to nothing" branch, and that is not an omission. With
    ``validate=True`` the only input that decodes to zero bytes is the empty string, and
    the caller has already refused an empty ``audio`` field -- ``"="``, ``" "`` and every
    other short candidate raise :class:`binascii.Error` and come back as "not valid
    base64" instead. A guard here would be a branch no request can reach, which reads as
    protection while protecting nothing. The emptiness that *can* happen -- an uploaded
    file with no bytes in it -- is checked in :func:`transcribe`, where it is reachable.
    """
    if len(value) > MAX_AUDIO_BASE64:
        raise HTTPException(
            status_code=413,
            detail=f"That recording is over the {MAX_UPLOAD_BYTES / 1_000_000:.0f} MB "
                   "limit. Record shorter takes.",
        )
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail=f"The recording is not valid base64: {exc}"
        ) from exc


async def _hear(data: bytes, language: str | None) -> tuple[str, dict[str, Any]]:
    """Transcribe the recording, returning the text and the event that names the model.

    One call, not a stream. This is the honest ceiling for the route as it stands:
    Parakeet has a streaming recogniser reachable over the same gRPC channel, but
    wiring it is a separate piece of work, and the ``heard`` event exists so the
    user can see the words the answer was built from either way. A wrong answer to
    a misheard question is otherwise indistinguishable from a wrong answer.
    """
    decision = _resolved("stt")
    spoken = (language or "").strip() or str(app_settings.get("input_language", "") or "").strip()
    try:
        transcript = await speech_provider.transcribe(
            data,
            provider_id=decision.provider_id,
            model=decision.model_id,
            language=spoken or None,
        )
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    text = (transcript.text or "").strip()
    if not text:
        # Nothing was said. Answering anyway would mean inventing the question.
        raise HTTPException(status_code=400, detail="No speech was found in that recording.")
    return text, {"type": "heard", "text": text, "routed": decision.key}


async def _drive(
    queue: "asyncio.Queue[Any]",
    kwargs: dict[str, Any],
    stop_speaking: asyncio.Event,
) -> None:
    """Run the spoken turn, putting every event on the queue.

    ``put_nowait`` on an unbounded queue, deliberately: the queue is per-turn and a turn is
    bounded by the model's output, so it cannot grow without end -- while a bounded queue
    would make a closed browser tab block a running tool call.
    """
    task = asyncio.current_task()
    if task is not None:
        _TURNS.add(task)
        task.add_done_callback(_TURNS.discard)
    try:
        async for event in voice_stream.stream(**kwargs, stop_speaking=stop_speaking):
            queue.put_nowait(event)
    except Exception as exc:  # noqa: BLE001 -- the net under a turn that should not raise
        log.exception("spoken turn driver failed")
        queue.put_nowait(
            {"type": "error", "error": f"The turn ended unexpectedly: {exc}", "kind": "internal"}
        )
    finally:
        queue.put_nowait(sse.END)


@router.post("/converse")
async def converse(
    message: str | None = Body(None, embed=True),
    audio: str | None = Body(None, embed=True),
    language: str | None = Body(None, embed=True),
    conversation_id: str | None = Body(None, embed=True),
    project_id: str | None = Body(None, embed=True),
    mode: str | None = Body(None, embed=True),
    voice: str | None = Body(None, embed=True),
    speak: bool = Body(True, embed=True),
) -> StreamingResponse:
    """Hear, think and speak in one request, streamed as it happens.

    Send ``message`` for typed input or ``audio`` (base64) for a recording; with audio the
    first event is ``heard``, carrying the transcript the rest of the turn was built from.

    Events are :meth:`nexus.ai.core.AiCore.stream`'s, plus ``start`` and ``heard`` from
    this route and ``voice``, ``segment``, ``speech`` and ``speech_error`` from
    :mod:`nexus.ai.voice_stream`. ``speech`` events arrive in segment order, each with a
    ``path`` for the desktop shell and a ``url`` for the web view, and the first of them
    lands while the model is still writing -- which is the entire point of the route.

    **Disconnecting stops the voice, not the turn.** They are different intentions and each
    gets what it deserves: the answer is already being written to the conversation and
    finishes, because losing it would be losing work the user asked for; the synthesis
    stops, because audio nobody is listening to is only a bill. Stopping the *turn* is
    ``POST /api/chat/cancel`` with this conversation id, which is the same cooperative
    checkpoint the typed path uses.
    """
    typed = (message or "").strip()
    if not typed and not audio:
        raise HTTPException(
            status_code=400,
            detail="There is nothing to answer: send a message or a recording.",
        )
    if mode is not None and mode not in persona.MODES:
        raise HTTPException(
            status_code=400,
            detail=f"'{mode}' is not a response mode. Choose one of: "
            + ", ".join(sorted(persona.MODES)),
        )

    heard: dict[str, Any] | None = None
    if audio:
        # Before the conversation is resolved, so a recording of silence does not leave an
        # empty conversation behind for the user to tidy up.
        typed, heard = await _hear(_decode_audio(audio), language)

    conversation_id = turns.resolve_conversation(
        conversation_id, first_message=typed, project_id=project_id
    )
    turns.refuse_if_busy(conversation_id)

    queue: asyncio.Queue[Any] = asyncio.Queue()
    if heard is not None:
        queue.put_nowait(heard)

    # Set by the response generator when it stops for any reason, including the client
    # going away. The turn watches it and stops synthesising; it does not stop thinking.
    gone = asyncio.Event()
    asyncio.create_task(
        _drive(
            queue,
            {
                "request": typed,
                "conversation_id": conversation_id,
                "project_id": project_id,
                "voice": voice,
                "speak": speak,
                "mode": mode,
            },
            gone,
        ),
        name=f"voice-turn:{conversation_id}",
    )

    return StreamingResponse(
        sse.drain(
            queue,
            first={"type": "start", "conversation_id": conversation_id, "spoken": speak},
            on_close=gone,
        ),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache, no-transform",
            "connection": "keep-alive",
            # nginx and friends buffer streamed responses by default, which would turn
            # sentence-by-sentence audio back into one block at the end.
            "x-accel-buffering": "no",
            "x-nexus-conversation": conversation_id,
        },
    )
