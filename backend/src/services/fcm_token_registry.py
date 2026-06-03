"""
FCM Token Registry - per-device token storage in Firestore.

Firestore path: users/{uid}/fcm_tokens/{token}

Each document:
  token: str - FCM registration token (same as doc ID)
  platform: str - "android" | "ios" | "web"
  registered_at: str - ISO UTC datetime, refreshed on every upsert
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..lib.logger import logger
from .firebase import admin_firestore

_SUBCOLLECTION = "fcm_tokens"

# Field names for documents in users/{uid}/fcm_tokens/{token}.
# SINGLE SOURCE OF TRUTH: the writer (register_token) and every reader reference these constants, 
# so the field names can never drift.
FIELD_TOKEN = "token"
FIELD_PLATFORM = "platform"
FIELD_REGISTERED_AT = "registered_at"

# FCM error codes that indicate a token is permanently invalid.
INVALID_TOKEN_CODES = frozenset({
    "registration-token-not-registered",
    "invalid-registration-token",
    "invalid-argument",
})


def _tokens_ref(user_id: str):
    return (
        admin_firestore()
        .collection("users")
        .document(user_id)
        .collection(_SUBCOLLECTION)
    )


def register_token(user_id: str, token: str, platform: str) -> None:
    """Upsert an FCM token for a user device.

    Uses the token string as the document ID so registering the same
    token twice is a no-op (just updates registered_at).
    """
    now = datetime.now(UTC).isoformat()
    ref = _tokens_ref(user_id).document(token)
    doc = ref.get()

    if doc.exists:
        ref.update({FIELD_PLATFORM: platform, FIELD_REGISTERED_AT: now})
        logger.debug("FCM token updated", {
            "user_id": user_id,
            "platform": platform,
            "token_preview": token[:20],
        })
    else:
        ref.set({
            FIELD_TOKEN: token,
            FIELD_PLATFORM: platform,
            FIELD_REGISTERED_AT: now,
        })
        logger.info("FCM token registered", {
            "user_id": user_id,
            "platform": platform,
            "token_preview": token[:20],
        })


def get_user_tokens(user_id: str) -> list[dict[str, Any]]:
    """Return all FCM token documents for a user."""
    docs = _tokens_ref(user_id).stream()
    tokens = [doc.to_dict() for doc in docs if doc.exists and doc.to_dict()]
    logger.debug("FCM tokens fetched", {
        "user_id": user_id,
        "token_count": len(tokens),
    })
    return tokens


def list_active_user_ids(inactivity_days: int = 7) -> list[str]:
    """Distinct uids whose FCM token was (re)registered within ``inactivity_days``.

    ``register_token`` refreshes ``FIELD_REGISTERED_AT`` on every upsert, and the
    client re-registers on each app launch and on token refresh, so this field
    tracks recency well enough to act as an "active recently" signal.

    Synchronous — the Firestore Admin SDK is blocking. Async callers wrap this in
    ``asyncio.to_thread`` (see ``feature_store`` and ``orchestrator``). 
    Keeping the one query here means writer and readers share a single field contract.
    """
    from datetime import timedelta

    from google.cloud.firestore_v1.base_query import FieldFilter

    cutoff = (datetime.now(UTC) - timedelta(days=inactivity_days)).isoformat()
    docs = (
        admin_firestore()
        .collection_group(_SUBCOLLECTION)
        .where(filter=FieldFilter(FIELD_REGISTERED_AT, ">=", cutoff))
        .stream()
    )
    user_ids: list[str] = []
    seen: set[str] = set()
    for doc in docs:
        # Path: users/{uid}/fcm_tokens/{token}
        parts = doc.reference.path.split("/")
        if len(parts) >= 2:
            uid = parts[1]
            if uid not in seen:
                seen.add(uid)
                user_ids.append(uid)
    return user_ids


def any_token_registered() -> bool:
    """Cheap existence probe (limit 1) to tell "no tokens at all" apart from
    "tokens exist but none recent" when the active-user list comes back empty.
    Lets the scoring loop warn loudly instead of failing silently."""
    docs = list(admin_firestore().collection_group(_SUBCOLLECTION).limit(1).stream())
    return len(docs) > 0


def remove_invalid_tokens(user_id: str, tokens: list[str]) -> None:
    """Delete tokens that FCM reported as permanently invalid.

    Called automatically by ``notification_service.send_notification``
    whenever FCM returns an error code in ``INVALID_TOKEN_CODES``.
    """
    if not tokens:
        return
    ref = _tokens_ref(user_id)
    for token in tokens:
        ref.document(token).delete()
    logger.info("Invalid FCM tokens removed", {
        "user_id": user_id,
        "removed_count": len(tokens),
        "token_previews": [t[:20] for t in tokens],
    })
