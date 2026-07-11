"""GET /entitlement: the one entitlement read every client calls on launch.

Serves the account's subscription state plus a usage summary and the in-app
purchase steering config. On the first-ever call for a uid it stamps the
server-side 45-day trial into users/{uid}/entitlement/current, which makes the
backend the single trial authority (the old client-side stamping was trivially
extendable by anyone with the Firestore SDK, and desktop users had no doc at all).

The stamp uses Firestore create() (atomic create-if-absent): two concurrent
first calls, or a race against a legacy mobile build's own client write, always
converge on exactly one doc and never overwrite an existing one.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import Request
from fastapi.responses import JSONResponse

from ..config.settings import settings
from ..lib.logger import logger
from ..services.entitlement import (
    FREE_TIER_DAILY_CHAT_LIMIT,
    FREE_TIER_DAILY_OUTBOUND_DRAFT_LIMIT,
    FREE_TIER_DAILY_VOICE_SECONDS,
    FREE_TIER_DAILY_WEB_SURF_LIMIT,
    EntitlementUnavailableError,
    ensure_entitlement_doc,
    normalize_status,
    resolve_effective_tier,
)
from ..services.geo import resolve_request_country
from ..services.request_auth import resolve_user_id_from_request

# Usage doc id -> (response key, counter field, daily limit).
_USAGE_COUNTERS: dict[str, tuple[str, str, int]] = {
    "daily_chat": ("chat", "count", FREE_TIER_DAILY_CHAT_LIMIT),
    "daily_web_surf": ("web_surf", "count", FREE_TIER_DAILY_WEB_SURF_LIMIT),
    "daily_outbound_draft": ("drafts", "count", FREE_TIER_DAILY_OUTBOUND_DRAFT_LIMIT),
    "daily_voice": ("voice_seconds", "seconds", FREE_TIER_DAILY_VOICE_SECONDS),
}


def _iso_or_none(value) -> str | None:
    """Firestore timestamps -> ISO 8601 strings; anything else -> None."""
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=UTC)
        return aware.isoformat()
    return None


def _read_usage_doc(uid: str, doc_id: str) -> dict:
    """One usage counter read, in a worker thread. Raises on Firestore failure."""
    from ..services.firebase import admin_firestore

    snap = (
        admin_firestore()
        .collection("users")
        .document(uid)
        .collection("usage")
        .document(doc_id)
        .get()
    )
    return snap.to_dict() or {}


async def _usage_summary(uid: str) -> dict:
    """All four daily counters as {used, limit}. A counter whose stored date is
    not today (UTC) has rolled over and counts as 0. A single failed read yields
    null for that entry; the summary never blocks or fails the response."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    doc_ids = list(_USAGE_COUNTERS.keys())
    results = await asyncio.gather(
        *(asyncio.to_thread(_read_usage_doc, uid, doc_id) for doc_id in doc_ids),
        return_exceptions=True,
    )

    summary: dict = {"date": today}
    for doc_id, result in zip(doc_ids, results):
        key, field, limit = _USAGE_COUNTERS[doc_id]
        if isinstance(result, BaseException):
            logger.warn("entitlement: usage read failed", {
                "user_id": uid, "counter": doc_id, "error": str(result),
            })
            summary[key] = None
            continue
        used = int(result.get(field, 0)) if result.get("date") == today else 0
        summary[key] = {"used": used, "limit": limit}
    return summary


async def handle_get_entitlement(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        data = await ensure_entitlement_doc(user_id)
    except EntitlementUnavailableError:
        # Explicit stale signal: clients fall back to their cached copy
        # (then degrade to free), never to a silently granted tier.
        return JSONResponse({"error": "entitlement_unavailable"}, status_code=503)
    except Exception as exc:
        logger.error("entitlement: trial stamp failed", {
            "user_id": user_id, "error": str(exc),
        })
        return JSONResponse({"error": "entitlement_unavailable"}, status_code=503)

    usage = await _usage_summary(user_id)

    return JSONResponse({
        "tier": data.get("tier", "free"),
        "status": normalize_status(data),
        "effective_tier": resolve_effective_tier(data),
        "trial_end_date": _iso_or_none(data.get("trial_end_date")),
        "expires_at": _iso_or_none(data.get("expires_at")),
        "cancel_at_period_end": bool(data.get("cancel_at_period_end", False)),
        "usage": usage,
        "steering": settings.steering_config,
        "country": resolve_request_country(request),
    })
