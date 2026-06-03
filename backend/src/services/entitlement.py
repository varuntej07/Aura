"""
Entitlement checks for metered features.

Free tier: 25 chat messages per UTC calendar day.
Trial users (free tier within trial window) get pro access.
Paid users are never gated.

All Firestore reads run in asyncio.to_thread() so the event loop stays unblocked.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from ..lib.logger import logger

FREE_TIER_DAILY_CHAT_LIMIT = 25
FREE_TIER_DAILY_WEB_SURF_LIMIT = 10


async def get_user_effective_tier(uid: str) -> str:
    """
    Returns 'free', 'starter', or 'pro'.

    A free-tier user still within their trial window is returned as 'pro'
    so they are never gated during the reverse-trial period.

    Returns 'pro' permissively when the entitlement doc is absent — new users
    should never be blocked before their Firestore write completes.
    """
    from ..services.firebase import admin_firestore

    def _fetch() -> dict:
        try:
            db = admin_firestore()
            snap = (
                db.collection("users")
                .document(uid)
                .collection("entitlement")
                .document("current")
                .get()
            )
            return snap.to_dict() or {}
        except Exception as exc:
            logger.warn("entitlement: Firestore read failed, defaulting to pro", {
                "user_id": uid,
                "error": str(exc),
            })
            return {}

    data = await asyncio.to_thread(_fetch)
    if not data:
        return "pro"

    tier: str = data.get("tier", "free")
    trial_end = data.get("trial_end_date")

    if tier == "free" and trial_end is not None:
        try:
            end_dt = trial_end.replace(tzinfo=UTC) if trial_end.tzinfo is None else trial_end
            if datetime.now(UTC) < end_dt:
                return "pro"
        except Exception:
            pass

    return tier


async def check_and_increment_daily_chat_usage(uid: str) -> tuple[bool, int]:
    """
    Atomically checks then increments the UTC-day chat counter for a free-tier user.

    Returns (allowed, count_after_this_message).
    The counter resets automatically each UTC calendar day.

    Falls back to (True, 0) if Firestore is unavailable; 
    infra failures should never block the user's chat. Log and allow.
    """
    from google.cloud import firestore as gcloud_firestore

    from ..services.firebase import admin_firestore

    today = datetime.now(UTC).strftime("%Y-%m-%d")

    def _run() -> tuple[bool, int]:
        db = admin_firestore()
        usage_ref = (
            db.collection("users")
            .document(uid)
            .collection("usage")
            .document("daily_chat")
        )
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> tuple[bool, int]:
            snap = usage_ref.get(transaction=txn)
            data = snap.to_dict() or {}

            if data.get("date") != today:
                txn.set(usage_ref, {"date": today, "count": 1})
                return True, 1

            count: int = data.get("count", 0)
            if count >= FREE_TIER_DAILY_CHAT_LIMIT:
                return False, count

            new_count = count + 1
            txn.update(usage_ref, {"count": new_count})
            return True, new_count

        return _execute(transaction)

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:
        logger.warn("entitlement: usage increment failed, allowing request", {
            "user_id": uid,
            "error": str(exc),
        })
        return True, 0


async def check_and_increment_daily_web_surf_usage(uid: str) -> tuple[bool, int]:
    """
    Atomically checks then increments the UTC-day web_surf counter for a free-tier user.

    Returns (allowed, count_after_this_call).
    Counter resets each UTC calendar day. Stored at users/{uid}/usage/daily_web_surf.

    Falls back to (True, 0) if Firestore is unavailable — infra failures should not
    block a user's request. Log and allow.
    """
    from google.cloud import firestore as gcloud_firestore

    from ..services.firebase import admin_firestore

    today = datetime.now(UTC).strftime("%Y-%m-%d")

    def _run() -> tuple[bool, int]:
        db = admin_firestore()
        usage_ref = (
            db.collection("users")
            .document(uid)
            .collection("usage")
            .document("daily_web_surf")
        )
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> tuple[bool, int]:
            snap = usage_ref.get(transaction=txn)
            data = snap.to_dict() or {}

            if data.get("date") != today:
                txn.set(usage_ref, {"date": today, "count": 1})
                return True, 1

            count: int = data.get("count", 0)
            if count >= FREE_TIER_DAILY_WEB_SURF_LIMIT:
                return False, count

            new_count = count + 1
            txn.update(usage_ref, {"count": new_count})
            return True, new_count

        return _execute(transaction)

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:
        logger.warn("entitlement: web_surf usage increment failed, allowing request", {
            "user_id": uid,
            "error": str(exc),
        })
        return True, 0
