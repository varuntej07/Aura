"""Authenticated dictation endpoints: the desktop's transcription credential,
and the opt-in training-trace ingest."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError

from ..config.settings import settings
from ..lib.logger import logger
from ..services.dictation import fields as F
from ..services.dictation import gcs_audio, store
from ..services.dictation.models import TracePayload
from ..services.request_auth import resolve_user_id_from_request

_DEEPGRAM_GRANT_URL = "https://api.deepgram.com/v1/auth/grant"
# The desktop refreshes ahead of expiry, so this is never blocking a keystroke.
# Bounded anyway: a hung provider must not hold a Cloud Run worker.
_TOKEN_MINT_TIMEOUT_S = 6.0


def validate_trace_id(trace_id: str) -> bool:
    return F.validate_trace_id(trace_id)


def _invalid_trace_id() -> JSONResponse:
    return JSONResponse({"error": "Invalid trace_id."}, status_code=400)


def _is_storage_configuration_error(exc: Exception) -> bool:
    from google.api_core import exceptions as gexc  # type: ignore

    return isinstance(exc, (gexc.NotFound, gexc.Forbidden))


async def handle_put_trace(request: Request, trace_id: str) -> JSONResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not validate_trace_id(trace_id):
        return _invalid_trace_id()
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        return JSONResponse({"error": "Content-Type must be application/json."}, status_code=400)

    content_length = request.headers.get("content-length")
    try:
        if content_length is not None and int(content_length) > F.MAX_METADATA_BYTES:
            return JSONResponse({"error": "Trace metadata too large."}, status_code=413)
    except ValueError:
        return JSONResponse({"error": "Invalid Content-Length."}, status_code=400)

    raw = await request.body()
    if not raw:
        return JSONResponse({"error": "Empty trace metadata."}, status_code=400)
    if len(raw) > F.MAX_METADATA_BYTES:
        return JSONResponse({"error": "Trace metadata too large."}, status_code=413)
    try:
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise ValueError("metadata must be an object")
        payload = TypeAdapter(TracePayload).validate_python(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError):
        # Never echo validation input: the body contains speech and corrected text.
        return JSONResponse({"error": "Invalid trace metadata."}, status_code=422)
    if payload.trace_id is not None and payload.trace_id != trace_id:
        return JSONResponse({"error": "traceId does not match the request path."}, status_code=409)

    try:
        result = await store.put_metadata(uid, trace_id, payload)
    except Exception as exc:
        logger.warn(
            "dictation: metadata persistence failed",
            {"trace_id": trace_id, "error": type(exc).__name__},
        )
        return JSONResponse({"error": "Temporarily unavailable."}, status_code=503)

    if result.status in ("conflict", "deleted"):
        return JSONResponse({"error": "Trace identity conflict."}, status_code=409)
    if result.status == "quota":
        return JSONResponse(
            {
                "error": "Monthly dictation trace quota reached.",
                "remainingThisMonth": 0,
                "resetsAtMs": result.resets_at_ms,
            },
            status_code=429,
        )
    return JSONResponse(
        {
            "ok": True,
            "traceId": trace_id,
            "hasAudio": result.has_audio,
            "remainingThisMonth": result.remaining,
            "resetsAtMs": result.resets_at_ms,
        }
    )


def _inspect_flac(data: bytes) -> tuple[int, int, str, int]:
    import soundfile as sf  # type: ignore

    with sf.SoundFile(io.BytesIO(data)) as audio:
        return int(audio.samplerate), int(audio.channels), str(audio.subtype), int(audio.frames)


async def handle_put_audio(request: Request, trace_id: str) -> JSONResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not validate_trace_id(trace_id):
        return _invalid_trace_id()
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "audio/flac":
        return JSONResponse({"error": "Content-Type must be audio/flac."}, status_code=400)

    digest = request.headers.get("X-Audio-Sha256", "")
    if not F.validate_sha256(digest):
        return JSONResponse({"error": "Missing or invalid X-Audio-Sha256."}, status_code=400)
    try:
        trace = await store.get_trace(uid, trace_id)
    except Exception as exc:
        logger.warn(
            "dictation: audio ownership lookup failed",
            {"trace_id": trace_id, "error": type(exc).__name__},
        )
        return JSONResponse({"error": "Temporarily unavailable."}, status_code=503)
    if trace is None:
        return JSONResponse({"error": "Unknown trace."}, status_code=404)
    if trace.get(F.DELETED_AT) or trace.get(F.DELETION_STATE):
        return JSONResponse({"error": "Trace was deleted."}, status_code=409)
    if trace.get("audioSha256") != digest:
        return JSONResponse(
            {"error": "Audio digest conflicts with trace metadata."},
            status_code=409,
        )

    expected_bytes = trace.get(F.AUDIO_BYTES)
    content_length = request.headers.get("content-length")
    try:
        if content_length is not None and int(content_length) > F.MAX_AUDIO_BYTES:
            return JSONResponse({"error": "Audio too large."}, status_code=413)
    except ValueError:
        return JSONResponse({"error": "Invalid Content-Length."}, status_code=400)

    data = await request.body()
    if not data:
        return JSONResponse({"error": "Empty audio body."}, status_code=400)
    if len(data) > F.MAX_AUDIO_BYTES:
        return JSONResponse({"error": "Audio too large."}, status_code=413)
    if expected_bytes is not None and len(data) != int(expected_bytes):
        return JSONResponse({"error": "Audio size conflicts with trace metadata."}, status_code=409)
    if hashlib.sha256(data).hexdigest() != digest:
        return JSONResponse({"error": "Audio digest mismatch."}, status_code=409)

    try:
        sample_rate, channels, subtype, frames = await asyncio.to_thread(_inspect_flac, data)
    except Exception:
        return JSONResponse({"error": "Invalid FLAC audio."}, status_code=400)
    if sample_rate != 16_000 or channels != 1 or subtype != "PCM_16":
        return JSONResponse({"error": "Audio must be 16 kHz mono 16-bit FLAC."}, status_code=400)
    actual_duration_ms = round(frames * 1_000 / sample_rate)
    expected_duration_ms = int(trace.get("durationMs", -1))
    tolerance_ms = max(250, round(expected_duration_ms * 0.01))
    if abs(actual_duration_ms - expected_duration_ms) > tolerance_ms:
        return JSONResponse(
            {"error": "Audio duration conflicts with trace metadata."},
            status_code=409,
        )

    try:
        immutable = await gcs_audio.create_audio(uid, trace_id, digest, data)
        attached = await store.attach_audio(
            uid,
            trace_id,
            path=immutable.path,
            generation=immutable.generation,
            content_sha256=digest,
            byte_length=len(data),
        )
        if attached.status in ("missing", "deleted", "conflict"):
            await gcs_audio.delete_exact(immutable.path, immutable.generation)
            return JSONResponse({"error": "Trace identity conflict."}, status_code=409)
    except gcs_audio.ImmutableObjectConflict:
        return JSONResponse({"error": "Trace identity conflict."}, status_code=409)
    except Exception as exc:
        log = logger.error if _is_storage_configuration_error(exc) else logger.warn
        log(
            "dictation: audio upload failed",
            {
                "trace_id": trace_id,
                "error": type(exc).__name__,
                "storage_configuration_error": _is_storage_configuration_error(exc),
            },
        )
        return JSONResponse({"error": "Upload temporarily unavailable."}, status_code=503)

    return JSONResponse(
        {
            "ok": True,
            "traceId": trace_id,
            "generation": immutable.generation,
            "reconciled": immutable.reconciled,
        }
    )


async def handle_delete_trace(request: Request, trace_id: str) -> JSONResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not validate_trace_id(trace_id):
        return _invalid_trace_id()

    try:
        target = await store.begin_delete(uid, trace_id)
        if target.status in ("absent", "tombstoned"):
            return JSONResponse({"ok": True})
        path = target.path
        if path is None and target.content_sha256:
            path = gcs_audio.object_path_for(uid, trace_id, target.content_sha256)
        if path:
            generation = target.generation or await gcs_audio.current_generation(path)
            if generation:
                await gcs_audio.delete_exact(path, generation)
        elif target.content_sha256 is None:
            # A valid trace always has a digest. Preserve the pending fence and
            # fail closed rather than claiming deletion while an unknown blob
            # may still exist.
            raise RuntimeError("trace deletion lacks immutable audio identity")
        await store.finish_delete(uid, trace_id)
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.warn(
            "dictation: trace deletion failed",
            {"trace_id": trace_id, "error": type(exc).__name__},
        )
        return JSONResponse({"error": "Deletion temporarily unavailable."}, status_code=503)


async def handle_mint_stt_token(request: Request) -> JSONResponse:
    """POST /dictation/stt-token — a short-lived Deepgram token for the desktop.

    Modelled on handlers/realtime.py, which does the same job for the OpenAI
    Realtime bridge and for the same reason: the desktop needs a direct,
    low-latency socket to a provider, and the permanent API key must not be on
    a client to get one.

    Security: DEEPGRAM_DICTATION_API_KEY never leaves this process. Deepgram's
    /v1/auth/grant exchanges it for a JWT scoped to transcription with a TTL of
    minutes, and that JWT is all the desktop ever holds. The response is
    `no-store` so nothing caches a credential.

    Latency: this is NOT on the press-to-first-word path. The desktop refreshes
    ahead of expiry and keeps a warm token, so a chord press already has one.
    That is the whole reason the TTL is minutes rather than Deepgram's 30s
    default.
    """
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not settings.DEEPGRAM_DICTATION_API_KEY:
        return JSONResponse({"error": "Dictation is unavailable."}, status_code=503)

    import httpx

    try:
        async with httpx.AsyncClient(timeout=_TOKEN_MINT_TIMEOUT_S) as client:
            grant = await client.post(
                _DEEPGRAM_GRANT_URL,
                headers={
                    # A raw API key is presented as `Token`; the JWT this
                    # returns is presented to the WebSocket as `Bearer`.
                    # .strip() is mandatory: a mounted secret carries a trailing
                    # newline, and httpx rejects a header value containing CR/LF
                    # before the request is even sent. Same fix as realtime.py.
                    "Authorization": f"Token {settings.DEEPGRAM_DICTATION_API_KEY.strip()}",
                    "Content-Type": "application/json",
                },
                json={"ttl_seconds": settings.DEEPGRAM_STT_TOKEN_TTL_S},
            )
    except Exception as exc:
        # Log the TYPE only, never the message: an httpx protocol error can
        # echo the offending Authorization header, which carries the real key.
        logger.warn(
            "dictation: stt token mint failed",
            {"error_type": type(exc).__name__},
        )
        return JSONResponse({"error": "Dictation is unavailable."}, status_code=503)

    if grant.status_code != 200:
        # Never echo the provider's body: it can quote the request. The status
        # alone separates a bad key from an outage.
        logger.warn("dictation: stt token mint rejected", {"status": grant.status_code})
        return JSONResponse({"error": "Dictation is unavailable."}, status_code=503)

    try:
        payload = grant.json()
        access_token = payload["access_token"]
        expires_in = int(
            float(payload.get("expires_in") or settings.DEEPGRAM_STT_TOKEN_TTL_S)
        )
    except Exception as exc:
        logger.warn(
            "dictation: stt token response unusable",
            {"error_type": type(exc).__name__},
        )
        return JSONResponse({"error": "Dictation is unavailable."}, status_code=503)

    if not isinstance(access_token, str) or not access_token or expires_in <= 0:
        return JSONResponse({"error": "Dictation is unavailable."}, status_code=503)

    response = JSONResponse(
        {"accessToken": access_token, "expiresInSeconds": expires_in}
    )
    response.headers["Cache-Control"] = "no-store"
    return response


_GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
# The desktop types the raw transcript after 2.5s total, so anything slower
# than this is wasted work for a reply nobody will use. Bounded anyway so a
# hung provider must not hold a Cloud Run worker.
_POLISH_TIMEOUT_S = 2.0
_POLISH_MAX_CHARS = 4000
_POLISH_MAX_BODY_BYTES = 64 * 1024
_POLISH_APP_MAX_CHARS = 64

# Process stems whose Enter key SENDS the message instead of starting a new
# line. The desktop's insert.rs types a newline character as a real VK_RETURN
# press, so an INFERRED line break in one of these posts a half-written
# message before the speaker can stop it. An explicitly spoken "new line" is
# still honoured there, because the speaker asked for it; only the inferred
# list is withheld.
_SEND_ON_ENTER_APPS = frozenset(
    {
        "slack",
        "discord",
        "teams",
        "ms-teams",
        "msteams",
        "whatsapp",
        "telegram",
        "signal",
        "messenger",
        "skype",
        "zoom",
    }
)

# Short and exemplar-led on purpose: gpt-oss-20b degrades on long,
# formatting-dense prompts, and this call has a 2.0s budget. One worked
# example pins numerals, filler removal, and "two sentences are not a list"
# more reliably than a page of rules would.
_POLISH_LIST_RULE_ALLOWED = """Two or more spoken ordinals that each open a separate point become a numbered
            list, one item per line, the ordinal word deleted:
            in: first fix the login bug second update the docs third ship it
            out: 1. Fix the login bug
            2. Update the docs
            3. Ship it
            An ordinal inside ordinary prose is not a list: "the first time I ran it"
            stays as it is.
        """

_POLISH_LIST_RULE_BLOCKED = """Never add a line break the speaker did not ask for: Enter sends the message
            in {app}. Spoken ordinals stay inline, like: First, fix the login bug.
            Second, update the docs.
        """

_POLISH_SYSTEM_PROMPT = """Format a dictated transcript. Output only the formatted text, no preamble or
            surrounding quotation marks.

            Keep the speaker's words and meaning. Never add content or reorder it.
            Fix punctuation, capitalization, and sentence breaks. Remove fillers: um, uh,
            er, "you know", and "like" or "so" used as filler.
            Self-corrections: drop an abandoned attempt only when the speaker signals it,
            with a cue (I mean, sorry, no wait) or by restarting the same phrase almost
            word for word. Keep what they settled on; drop the cue too. Repeated words and
            cut-off fragments collapse the same way.
            in: why don't you upload why don't you update the file
            out: Why don't you update the file
            in: set the timeout to fifty, sorry, fifteen seconds
            out: Set the timeout to 15 seconds
            No cue and no near-verbatim restart means change nothing: two clauses are two
            clauses. Contrast and emphasis are meaning: "make it red, not blue" and "no
            no, don't ship it" stay whole. Never invent a word.
            Write quantities as numerals: ten cards -> 10 cards, five thousand -> 5,000,
            twenty percent -> 20%. Leave number words that are not quantities: one of
            them, no one, at one point.
            Spoken commands: "new line" or "new paragraph" -> line break, "bullet point"
            -> "- ", "all caps that" -> uppercase it, "quote X end quote" -> "X".
            {list_rule}
            Target app is {app}. Match its register through punctuation and casing only.

            in: the recent activity cards are displayed like ten cards at a time i don't
            want to see more than five cards
            out: The recent activity cards are displayed 10 cards at a time. I don't want
            to see more than 5 cards.
        """


async def handle_polish(request: Request) -> JSONResponse:
    """POST /dictation/polish - AI cleanup of a finished dictation transcript.

    The desktop sends {"text": ..., "app": ...} and types whatever comes back
    in {"text": ...}; on any non-200 it types the raw transcript instead, so
    every error here degrades to unformatted dictation, never to lost words.

    Security: GROQ_API_KEY never leaves this process, same posture as
    DEEPGRAM_DICTATION_API_KEY above. Unlike transcription there is no
    provider-side ephemeral token to mint, so the whole call is proxied.

    Privacy: the transcript is speech. It is never logged here at any level
    (the same rule the desktop's dictation module enforces), never stored,
    and never echoed in an error body.

    Latency: this IS on the keyup-to-keystroke path. The desktop gives up at
    2.5s total, so the provider timeout is 2.0s and everything else here is
    arithmetic.
    """
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not settings.GROQ_API_KEY:
        return JSONResponse({"error": "Formatting is unavailable."}, status_code=503)

    raw = await request.body()
    if not raw or len(raw) > _POLISH_MAX_BODY_BYTES:
        return JSONResponse({"error": "Invalid request body."}, status_code=400)
    try:
        decoded = json.loads(raw)
        text = decoded["text"]
        app_name = decoded.get("app")
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, KeyError):
        # Never echo validation input: the body contains speech.
        return JSONResponse({"error": "Invalid request body."}, status_code=400)
    if not isinstance(text, str) or not text.strip():
        return JSONResponse({"error": "Invalid request body."}, status_code=400)
    if len(text) > _POLISH_MAX_CHARS:
        return JSONResponse({"error": "Transcript too long."}, status_code=413)
    if not isinstance(app_name, str) or not app_name.strip():
        app_name = None

    target = (app_name or "a text field")[:_POLISH_APP_MAX_CHARS]
    # Deterministic, not left to the model: whether an inferred line break is
    # safe here is a property of the destination app, and a wrong guess sends
    # the message early.
    list_rule = (
        _POLISH_LIST_RULE_BLOCKED
        if target.lower() in _SEND_ON_ENTER_APPS
        else _POLISH_LIST_RULE_ALLOWED
    )
    system = _POLISH_SYSTEM_PROMPT.replace("{list_rule}", list_rule).replace(
        "{app}", target
    )
    # gpt-oss is a reasoning model and its reasoning tokens count against this
    # cap, so the headroom is bigger than the visible output needs.
    max_tokens = min(len(text) // 3 + 640, 1536)

    import httpx

    try:
        async with httpx.AsyncClient(timeout=_POLISH_TIMEOUT_S) as client:
            completion = await client.post(
                _GROQ_CHAT_URL,
                headers={
                    # .strip() is mandatory: a mounted secret carries a trailing
                    # newline, and httpx rejects a header value containing
                    # CR/LF. Same fix as the Deepgram grant above.
                    "Authorization": f"Bearer {settings.GROQ_API_KEY.strip()}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.GROQ_POLISH_MODEL,
                    "temperature": 0,
                    "max_tokens": max_tokens,
                    # Keep the reasoning burst short: this is mechanical text
                    # cleanup on a 2.0s budget, not a problem to think about.
                    "reasoning_effort": "low",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": text},
                    ],
                },
            )
    except Exception as exc:
        # Type only, never the message: a protocol error can echo the request,
        # which carries both the key header and the transcript.
        logger.warn("dictation: polish call failed", {"error_type": type(exc).__name__})
        return JSONResponse({"error": "Formatting is unavailable."}, status_code=503)

    if completion.status_code != 200:
        # Never echo the provider's body: it can quote the request. The status
        # alone separates a bad key from an outage or a rate limit.
        logger.warn("dictation: polish rejected", {"status": completion.status_code})
        return JSONResponse({"error": "Formatting is unavailable."}, status_code=503)

    try:
        payload = completion.json()
        formatted = payload["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.warn(
            "dictation: polish response unusable",
            {"error_type": type(exc).__name__},
        )
        return JSONResponse({"error": "Formatting is unavailable."}, status_code=503)

    if not isinstance(formatted, str) or not formatted.strip():
        return JSONResponse({"error": "Formatting is unavailable."}, status_code=503)

    response = JSONResponse({"text": formatted})
    response.headers["Cache-Control"] = "no-store"
    return response


async def handle_get_quota(request: Request) -> JSONResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        remaining, resets_at_ms = await store.get_quota(uid)
    except Exception as exc:
        logger.warn("dictation: quota lookup failed", {"error": type(exc).__name__})
        return JSONResponse({"error": "Temporarily unavailable."}, status_code=503)
    return JSONResponse({"remainingThisMonth": remaining, "resetsAtMs": resets_at_ms})
