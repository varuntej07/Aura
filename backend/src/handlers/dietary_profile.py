"""
GET  /nutrition/profile -> fetch user's dietary profile
POST /nutrition/profile -> create or update user's dietary profile
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from ..lib.logger import logger
from ..services.firebase import admin_firestore
from ..services.request_auth import resolve_user_id_from_event

_ALLOWED_FIELDS = {
    "age",
    "gender",
    "height_cm",
    "weight_kg",
    "goal",
    "activity_level",
    "workout_min_per_day",
    "restrictions",
    "allergies",
    "fat_pct",
}


def _json(status: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload),
    }


def _profile_ref(user_id: str):
    return (
        admin_firestore()
        .collection("users")
        .document(user_id)
        .collection("dietary_profile")
        .document("data")
    )


async def handle_get_dietary_profile(event: dict[str, Any]) -> dict[str, Any]:
    user_id = resolve_user_id_from_event(event)
    if not user_id:
        return _json(401, {"error": "Unauthorized"})

    try:
        doc = await asyncio.to_thread(_profile_ref(user_id).get)
        if not doc.exists:
            return _json(200, {"profile": None})
        return _json(200, {"profile": doc.to_dict()})
    except Exception as exc:
        logger.error("Get dietary profile failed", {"error": str(exc), "user_id": user_id})
        return _json(500, {"error": "Internal server error"})


async def handle_save_dietary_profile(event: dict[str, Any]) -> dict[str, Any]:
    try:
        body: dict[str, Any] = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _json(400, {"error": "Invalid JSON body"})

    user_id = resolve_user_id_from_event(event, body=body)
    if not user_id:
        return _json(401, {"error": "Unauthorized"})

    profile = body.get("profile")
    if not isinstance(profile, dict) or not profile:
        return _json(400, {"error": "profile object is required"})

    sanitized = {k: v for k, v in profile.items() if k in _ALLOWED_FIELDS}
    if not sanitized:
        return _json(400, {"error": "No valid profile fields provided"})

    sanitized["updated_at"] = datetime.now(UTC).isoformat()

    try:
        ref = _profile_ref(user_id)
        await asyncio.to_thread(lambda: ref.set(sanitized, merge=True))
        logger.info("Dietary profile saved", {"user_id": user_id, "fields": list(sanitized.keys())})
        return _json(200, {"profile": sanitized})
    except Exception as exc:
        logger.error("Save dietary profile failed", {"error": str(exc), "user_id": user_id})
        return _json(500, {"error": "Internal server error"})
