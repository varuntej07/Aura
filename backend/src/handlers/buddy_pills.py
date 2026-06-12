"""
POST /chat/buddy-pills/refresh - regenerates the main Buddy chat suggestion pills.

Fired fire-and-forget by the Flutter client when the user leaves the app after a
real session (a text message OR a voice session). Regenerating at that moment
means the pills are ready and fresh the next time the user opens the empty Buddy
chat and because it keys off "did anything this session", a voice-only session
is covered just the same as text.

Grounding: the user's last 10 chat queries + their UserAura interest subjects.
The interest read is consent-gated (users/{uid}.aura_consent_granted) exactly like
every other behavioural read — a consent-less user still gets query-grounded pills.

Pills are merged into agent_suggestion_pills/{uid} under the "buddy" key (the daily
job owns the other agent sets), with a buddy_generated_at stamp. Never raises into
the request: any failure returns 200 {"refreshed": false} and the client keeps its
cached/fallback pills.
"""

from __future__ import annotations

import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services.daily_notification.suggestion_pills_agent import SuggestionPillsAgent
from ..services.firebase import admin_firestore
from ..services.model_provider import ModelProvider
from ..services.request_auth import resolve_user_id_from_request
from ..services.user_aura_schema import top_interest_subjects

# Module-level singletons — one ModelProvider/agent reused across requests.
_models: ModelProvider | None = None
_pills_agent: SuggestionPillsAgent | None = None


def _get_pills_agent() -> SuggestionPillsAgent:
    global _models, _pills_agent
    if _models is None:
        _models = ModelProvider()
    if _pills_agent is None:
        _pills_agent = SuggestionPillsAgent(_models)
    return _pills_agent


async def handle_refresh_buddy_pills(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        recent_queries, interest_subjects = await asyncio.gather(
            asyncio.to_thread(_fetch_last_10_queries, user_id),
            asyncio.to_thread(_fetch_interest_subjects_if_consented, user_id),
        )
        pills = await _get_pills_agent().generate_buddy_pills(
            user_id, recent_queries, interest_subjects
        )
        logger.info("buddy_pills: refresh complete", {
            "user_id": user_id,
            "count": len(pills),
            "had_interests": bool(interest_subjects),
        })
        return JSONResponse({"refreshed": bool(pills)}, status_code=200)
    except Exception as exc:
        # Never fail the client over a pill refresh — it keeps its cached pills.
        logger.warn("buddy_pills: refresh failed", {"user_id": user_id, "error": str(exc)})
        return JSONResponse({"refreshed": False}, status_code=200)


def _fetch_last_10_queries(user_id: str) -> list[dict]:
    """Most recent 10 logged user inputs (users/{uid}/queries), newest first.
    Mirrors the daily orchestrator's fetch so buddy pills are grounded the same way."""
    try:
        db = admin_firestore()
        docs = (
            db.collection("users").document(user_id)
            .collection("queries")
            .order_by("timestamp", direction="DESCENDING")
            .limit(10)
            .stream()
        )
        return [{"id": d.id, **d.to_dict()} for d in docs]
    except Exception as exc:
        logger.warn("buddy_pills: queries fetch failed", {"user_id": user_id, "error": str(exc)})
        return []


def _fetch_interest_subjects_if_consented(user_id: str) -> list[str]:
    """The user's top UserAura interest subjects, only if Aura consent is granted.
    Returns [] without consent or on any error — pills then fall back to queries only."""
    try:
        db = admin_firestore()
        user_snap = db.collection("users").document(user_id).get()
        consent = (
            bool((user_snap.to_dict() or {}).get("aura_consent_granted", False))
            if user_snap.exists
            else False
        )
        if not consent:
            return []
        aura_snap = db.collection("UserAura").document(user_id).get()
        profile = (aura_snap.to_dict() or {}) if aura_snap.exists else {}
        return top_interest_subjects(profile, k=5)
    except Exception as exc:
        logger.warn("buddy_pills: interest fetch failed", {"user_id": user_id, "error": str(exc)})
        return []
