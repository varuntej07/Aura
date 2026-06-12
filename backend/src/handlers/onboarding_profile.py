"""
POST /onboarding/profile - seed a new user's declared interests into UserAura.

The Flutter onboarding screen writes the declarative fields (gender,
onboarding_interests, locale, language) onto users/{uid} itself, then calls this
endpoint so the SERVER seeds those declared interests into the behavioural profile
(UserAura/{uid}.interests) with origin="onboarding". That gives the signal engine a
real starting direction on day one instead of waiting for chat to accumulate.

GDPR gate: seeding the behavioural profile only happens when the user has granted
Aura consent (users/{uid}.aura_consent_granted), exactly like the chat extractor.
The declared onboarding_interests list on users/{uid} is the client's own write and
is unaffected — the scoring loop's allow-list reads it directly — so a consent-less
user is never profiled but still gets relevant content from their declared list.

Body: { "interests": ["sports", "technology_computing", ...] }   # taxonomy slugs
Returns 200 { "seeded": bool, "categories": int } (or "reason" when not seeded).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services.firebase import admin_firestore
from ..services.request_auth import resolve_user_id_from_request
from ..services.signal_engine.content_category_map import ONBOARDABLE_CATEGORIES
from ..services.user_aura_schema import seed_onboarding_interests

# Only producible/onboardable slugs are accepted, a declared interest no source
# can satisfy would silently never surface, so it is dropped at the door.
_ALLOWED_SLUGS = frozenset(ONBOARDABLE_CATEGORIES)
_MAX_INTERESTS = 12


async def handle_onboarding_profile(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

    raw = body.get("interests") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        return JSONResponse({"error": "Field 'interests' must be a list."}, status_code=400)

    # Validate against the producible set; drop anything off-list (never error,
    # an old client sending an unknown slug should degrade, not fail onboarding).
    slugs: list[str] = []
    for s in raw[:_MAX_INTERESTS]:
        slug = str(s or "").strip()
        if slug in _ALLOWED_SLUGS and slug not in slugs:
            slugs.append(slug)

    seeded, categories = await asyncio.to_thread(_seed_if_consented, user_id, slugs)

    logger.info("onboarding_profile: seed result", {
        "user_id": user_id,
        "requested": len(raw),
        "accepted": len(slugs),
        "seeded": seeded,
        "categories": categories,
    })
    if not seeded:
        return JSONResponse({"seeded": False, "reason": "consent_not_granted"}, status_code=200)
    return JSONResponse({"seeded": True, "categories": categories}, status_code=200)


def _seed_if_consented(user_id: str, slugs: list[str]) -> tuple[bool, int]:
    """Read consent, seed UserAura.interests if granted. Returns (seeded, count).

    Runs in a thread (firebase-admin is sync). Never raises — a Firestore error
    returns (False, 0) so onboarding always completes on the client."""
    try:
        db = admin_firestore()
        user_snap = db.collection("users").document(user_id).get()
        consent = bool((user_snap.to_dict() or {}).get("aura_consent_granted", False)) if user_snap.exists else False
        if not consent or not slugs:
            return False, 0

        aura_ref = db.collection("UserAura").document(user_id)
        aura = (aura_ref.get().to_dict() or {}) if aura_ref else {}
        interests = aura.get("interests")
        if not isinstance(interests, dict):
            interests = {}

        seed_onboarding_interests(interests, slugs, datetime.now(UTC))
        # merge=True so we add interest categories without clobbering anything the
        # fire-and-forget chat extractor may have written concurrently.
        aura_ref.set(
            {"interests": interests, "last_updated": datetime.now(UTC).isoformat()},
            merge=True,
        )
        return True, len(interests)
    except Exception as exc:
        logger.warn("onboarding_profile: seed failed", {"user_id": user_id, "error": str(exc)})
        return False, 0
