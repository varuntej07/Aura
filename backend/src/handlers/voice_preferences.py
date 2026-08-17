"""Authenticated account-wide Buddy voice preference endpoints."""

from __future__ import annotations

import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse
from google.cloud import firestore as fs

from ..agent.voice.voice_catalog import CATALOG, DEFAULT_VOICE_SLUG, resolve_voice
from ..lib.logger import logger
from ..services.entitlement import EntitlementUnavailableError, get_user_effective_tier
from ..services.firebase import admin_firestore
from ..services.request_auth import resolve_user_id_from_request


def _user_ref(user_id: str):
    return admin_firestore().collection("users").document(user_id)


def _read_stored_voice(user_id: str) -> str:
    data = _user_ref(user_id).get().to_dict() or {}
    settings = data.get("settings") or {}
    return str(settings.get("tts_voice_id") or "").strip()


async def _response_for(user_id: str, stored_voice_id: str) -> JSONResponse:
    try:
        tier = await get_user_effective_tier(user_id)
    except EntitlementUnavailableError:
        # Match the voice worker's fail-open treatment of an unavailable tier
        # read. The worker repeats the authoritative check when a session starts.
        tier = "unknown"
    voice, fallback_reason = resolve_voice(stored_voice_id, tier)
    return JSONResponse({
        "voice_id": voice.slug,
        "stored_voice_id": stored_voice_id,
        "fallback_reason": fallback_reason,
        "default_voice_id": DEFAULT_VOICE_SLUG,
    })


async def handle_get_voice_preferences(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        stored_voice_id = await asyncio.to_thread(_read_stored_voice, user_id)
        return await _response_for(user_id, stored_voice_id)
    except Exception as exc:
        logger.warn("voice preferences: read failed", {
            "user_id": user_id,
            "error_type": type(exc).__name__,
        })
        return JSONResponse({"error": "Temporarily unavailable."}, status_code=503)


async def handle_update_voice_preferences(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

    voice_id = str(body.get("voice_id") or "").strip().lower() if isinstance(body, dict) else ""
    selected = next((voice for voice in CATALOG if voice.slug == voice_id), None)
    if selected is None:
        return JSONResponse({"error": "Unknown voice."}, status_code=400)

    if selected.paid_only:
        try:
            tier = await get_user_effective_tier(user_id)
        except EntitlementUnavailableError:
            return JSONResponse({"error": "Temporarily unavailable."}, status_code=503)
        resolved, fallback_reason = resolve_voice(voice_id, tier)
        if fallback_reason:
            return JSONResponse(
                {"error": "This voice requires a paid plan.", "code": fallback_reason},
                status_code=403,
            )
        voice_id = resolved.slug

    try:
        await asyncio.to_thread(
            _user_ref(user_id).update,
            {
                "settings.tts_voice_id": voice_id,
                "settings.tts_voice_updated_at": fs.SERVER_TIMESTAMP,
            },
        )
    except Exception as exc:
        logger.warn("voice preferences: update failed", {
            "user_id": user_id,
            "voice_id": voice_id,
            "error_type": type(exc).__name__,
        })
        return JSONResponse({"error": "Temporarily unavailable."}, status_code=503)

    logger.info("voice preferences: updated", {
        "user_id": user_id,
        "voice_id": voice_id,
    })
    return await _response_for(user_id, voice_id)
