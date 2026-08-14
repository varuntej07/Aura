"""Authenticated dictation endpoints: the desktop's transcription credential,
and the opt-in training-trace ingest."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

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
        payload = TracePayload.model_validate(decoded)
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

    expected_bytes = int(trace.get("audioBytes", -1))
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
    if len(data) != expected_bytes:
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
