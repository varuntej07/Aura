"""Authenticated dictation endpoints: the desktop's transcription credential,
and the opt-in training-trace ingest."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError

from ..config.settings import settings
from ..lib.logger import logger
from ..prompts import (
    DICTATION_POLISH_LIST_RULE_ALLOWED,
    DICTATION_POLISH_LIST_RULE_BLOCKED,
    DICTATION_POLISH_SYSTEM_PROMPT,
)
from ..services import audio_validation
from ..services.analytics.llm_telemetry import bind_llm_user
from ..services.dictation import fields as F
from ..services.model_provider import get_model_provider
from ..services.dictation import gcs_audio, store
from ..services.dictation.models import TracePayload
from ..services.stt import deepgram_grant
from .request_guards import no_store_json, require_json, require_user


def _invalid_trace_id() -> JSONResponse:
    return JSONResponse({"error": "Invalid trace_id."}, status_code=400)


def _is_storage_configuration_error(exc: Exception) -> bool:
    from google.api_core import exceptions as gexc  # type: ignore

    return isinstance(exc, (gexc.NotFound, gexc.Forbidden))


async def handle_put_trace(request: Request, trace_id: str) -> JSONResponse:
    uid = require_user(request)
    if not F.validate_trace_id(trace_id):
        return _invalid_trace_id()
    require_json(request)

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


# Dictation's own duration-tolerance floor: clips are <=2 minutes, so a 2s
# floor (the meetings value) would mask real truncation; 250ms keeps the 1%
# rule meaningful at this scale.
_DURATION_TOLERANCE_FLOOR_MS = 250


async def handle_put_audio(request: Request, trace_id: str) -> JSONResponse:
    uid = require_user(request)
    if not F.validate_trace_id(trace_id):
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
    if trace.get(F.AUDIO_SHA256) != digest:
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

    # Shared full-decode validation (services/audio_validation.py): unlike the
    # previous header-only soundfile probe this also rejects a truncated FLAC,
    # matching the strictness the meetings path already enforced. The
    # channel/rate/subtype policy stays dictation's own.
    try:
        stream = await asyncio.to_thread(
            audio_validation.decode_flac_info,
            data,
            max_duration_ms=F.MAX_DURATION_MS
            + audio_validation.duration_tolerance_ms(
                F.MAX_DURATION_MS, floor_ms=_DURATION_TOLERANCE_FLOOR_MS
            ),
        )
    except audio_validation.AudioValidationError:
        return JSONResponse({"error": "Invalid FLAC audio."}, status_code=400)
    if (
        stream.sample_rate_hz != 16_000
        or stream.channel_count != 1
        or stream.subtype != "PCM_16"
    ):
        return JSONResponse({"error": "Audio must be 16 kHz mono 16-bit FLAC."}, status_code=400)
    actual_duration_ms = stream.duration_ms
    expected_duration_ms = int(trace.get(F.DURATION_MS, -1))
    tolerance_ms = audio_validation.duration_tolerance_ms(
        expected_duration_ms, floor_ms=_DURATION_TOLERANCE_FLOOR_MS
    )
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
    uid = require_user(request)
    if not F.validate_trace_id(trace_id):
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

    Security: DEEPGRAM_DICTATION_API_KEY never leaves this process
    (services/stt/deepgram_grant.py owns the exchange). The response is
    `no-store` so nothing caches a credential.

    Latency: this is NOT on the press-to-first-word path. The desktop refreshes
    ahead of expiry and keeps a warm token, so a chord press already has one.
    That is the whole reason the TTL is minutes rather than Deepgram's 30s
    default.
    """
    uid = require_user(request)

    grant = await deepgram_grant.mint_grant(
        ttl_seconds=settings.DEEPGRAM_STT_TOKEN_TTL_S,
        caller="dictation",
    )
    if grant is None:
        return JSONResponse({"error": "Dictation is unavailable."}, status_code=503)
    access_token, expires_in = grant

    return no_store_json({"accessToken": access_token, "expiresInSeconds": expires_in})


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

# Process stems where Enter is known to start a new line, not send, so an
# inferred numbered list is safe. Anything in NEITHER set takes the blocked
# branch: losing an inferred list in an unrecognised app is recoverable, a
# half-written message posted to an unrecognised chat app is not. Both sets
# key on the destination app - a structural fact - never on what was said.
_INFERRED_LIST_SAFE_APPS = frozenset(
    {
        "notepad",
        "wordpad",
        "winword",
        "word",
        "code",
        "cursor",
        "notion",
        "obsidian",
        "onenote",
    }
)


def _process_stem(app: str) -> str:
    """Reduce a client-supplied destination ("Slack", "slack.exe",
    "C:\\...\\Slack.exe", "Slack | general") to a lowercase process stem for
    the lookups above. Nothing verifies the desktop's `app` format, so this
    accepts the obvious variants instead of trusting one; a stem that still
    matches nothing fails safe via the blocked branch."""
    tail = re.split(r"[\\/]", app.strip().lower())[-1]
    if tail.endswith(".exe"):
        tail = tail[: -len(".exe")]
    return re.split(r"[^a-z0-9._-]", tail, maxsplit=1)[0].strip("._-")

# The polish prompts live in prompts.py (every prompt in one home), aliased to
# their historical names; byte-stability hash-verified at move time.
_POLISH_LIST_RULE_ALLOWED = DICTATION_POLISH_LIST_RULE_ALLOWED
_POLISH_LIST_RULE_BLOCKED = DICTATION_POLISH_LIST_RULE_BLOCKED
_POLISH_SYSTEM_PROMPT = DICTATION_POLISH_SYSTEM_PROMPT


async def handle_polish(request: Request) -> JSONResponse:
    """POST /dictation/polish - AI cleanup of a finished dictation transcript.

    The desktop sends {"text": ..., "app": ...} and types whatever comes back
    in {"text": ...}; on any non-200 it types the raw transcript instead, so
    every error here degrades to unformatted dictation, never to lost words.

    The model call rides ModelProvider.polish() - the single LLM entry point -
    so it is traced and priced into the per-user cost ledger; it stays
    single-attempt with the same 2.0s hard budget as the httpx call it
    replaced. GROQ_API_KEY never leaves this process, same posture as
    DEEPGRAM_DICTATION_API_KEY above; unlike transcription there is no
    provider-side ephemeral token to mint, so the whole call is proxied.

    Privacy: the transcript is speech. It is never logged here at any level
    (the same rule the desktop's dictation module enforces), never stored,
    and never echoed in an error body.

    Latency: this IS on the keyup-to-keystroke path. The desktop gives up at
    2.5s total, so the provider timeout is 2.0s and everything else here is
    arithmetic.
    """
    uid = require_user(request)
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
    # the message early. Only a KNOWN-safe destination gets inferred lists;
    # send-on-Enter apps and every unrecognised destination take the blocked
    # branch, so a format drift in the client's `app` string can no longer
    # silently select the branch that posts a half-written message.
    stem = _process_stem(app_name) if app_name else ""
    list_rule = (
        _POLISH_LIST_RULE_ALLOWED
        if stem in _INFERRED_LIST_SAFE_APPS and stem not in _SEND_ON_ENTER_APPS
        else _POLISH_LIST_RULE_BLOCKED
    )
    system = _POLISH_SYSTEM_PROMPT.replace("{list_rule}", list_rule).replace(
        "{app}", target
    )
    # gpt-oss is a reasoning model and its reasoning tokens count against this
    # cap, so the headroom is bigger than the visible output needs.
    max_tokens = min(len(text) // 3 + 640, 1536)

    try:
        # bind_llm_user makes the per-user cost ledger fire for this call -
        # the exact spend the old handler-local httpx call kept invisible.
        with bind_llm_user(uid):
            formatted = await get_model_provider().polish(
                text,
                system=system,
                temperature=0,
                max_output_tokens=max_tokens,
                # Keep the reasoning burst short: this is mechanical text
                # cleanup on a 2.0s budget, not a problem to think about.
                reasoning_effort="low",
                timeout_s=_POLISH_TIMEOUT_S,
                caller="dictation_polish",
            )
    except Exception as exc:
        # Type only, never the message: a protocol error can echo the request,
        # which carries both the key header and the transcript.
        logger.warn("dictation: polish call failed", {"error_type": type(exc).__name__})
        return JSONResponse({"error": "Formatting is unavailable."}, status_code=503)

    if not formatted.strip():
        return JSONResponse({"error": "Formatting is unavailable."}, status_code=503)

    return no_store_json({"text": formatted})


async def handle_get_quota(request: Request) -> JSONResponse:
    uid = require_user(request)
    try:
        remaining, resets_at_ms = await store.get_quota(uid)
    except Exception as exc:
        logger.warn("dictation: quota lookup failed", {"error": type(exc).__name__})
        return JSONResponse({"error": "Temporarily unavailable."}, status_code=503)
    return JSONResponse({"remainingThisMonth": remaining, "resetsAtMs": resets_at_ms})
