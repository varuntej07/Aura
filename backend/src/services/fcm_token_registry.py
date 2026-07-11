"""
FCM Token Registry - per-device token storage in Firestore.

Firestore path: users/{uid}/fcm_tokens/{token}

Each document:
  token: str - FCM registration token (same as doc ID)
  platform: str - "android" | "ios" | "web"
  registered_at: str - ISO UTC datetime, refreshed on every upsert
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from firebase_admin import exceptions, messaging

from ..lib.logger import logger
from .firebase import admin_firestore

_SUBCOLLECTION = "fcm_tokens"

# list_active_user_ids is independently re-queried by ~6 different call sites at
# cadences from every minute to hourly (proactive drain, briefing tick, signal
# tick, hourly tick-emit, reengagement, daily plan fan-out) for a value that only
# changes when a device registers/re-registers a token — nowhere near every
# minute. An in-process TTL cache turns ~1700 redundant collection_group scans/day
# into a refresh every few minutes, with zero effect on correctness: no caller
# needs sub-minute-exact membership. Keyed by inactivity_days so a caller using a
# non-default window can never collide with the default-window cache.
_ACTIVE_USERS_CACHE_TTL_SECONDS = 180
_active_users_cache: dict[int, tuple[list[str], float]] = {}

# Field names for documents in users/{uid}/fcm_tokens/{token}.
# SINGLE SOURCE OF TRUTH: the writer (register_token) and every reader reference these constants,
# so the field names can never drift.
FIELD_TOKEN = "token"
FIELD_PLATFORM = "platform"
FIELD_REGISTERED_AT = "registered_at"

# Exception types raised by ``send_each_for_multicast`` that mean a token is
# permanently invalid and must be deleted. This is the primary, version-stable
# detection path — checking the exception class avoids depending on the exact
# string code the SDK happens to expose.
INVALID_TOKEN_EXCEPTIONS = (
    messaging.UnregisteredError,      # token expired / app uninstalled (canonical NOT_FOUND)
    messaging.SenderIdMismatchError,  # token belongs to a different FCM sender
    exceptions.InvalidArgumentError,  # malformed / unparseable token
)

# FCM error codes that indicate a token is permanently invalid. Used only as a
# fallback for SDK paths that don't raise one of INVALID_TOKEN_EXCEPTIONS.
# Codes are normalised to lowercase-hyphenated before comparison, so both the
# canonical codes ("NOT_FOUND" -> "not-found") and the messaging-style codes
# ("messaging/registration-token-not-registered") land here.
INVALID_TOKEN_CODES = frozenset({
    "registration-token-not-registered",
    "invalid-registration-token",
    "invalid-argument",
    "not-found",
    "sender-id-mismatch",
})


def is_permanently_invalid_token_error(exc: BaseException | None) -> bool:
    """True if an FCM send exception means the device token should be deleted.

    Checks the exception type first (stable across SDK versions), then falls
    back to matching the normalised error code so a future SDK change can't
    silently reopen the stale-token leak.
    """
    if exc is None:
        return False
    if isinstance(exc, INVALID_TOKEN_EXCEPTIONS):
        return True
    code = (
        getattr(exc, "code", "")
        or getattr(getattr(exc, "cause", None), "error_code", "")
        or ""
    )
    if isinstance(code, str):
        code = code.split("/")[-1].lower().replace("_", "-")
    return code in INVALID_TOKEN_CODES


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


def _query_active_user_ids(inactivity_days: int) -> list[str]:
    """The raw collection_group query, uncached. Split out so the cache wrapper
    below can call it directly on a miss or a forced refresh."""
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


def list_active_user_ids(inactivity_days: int = 7, *, force_refresh: bool = False) -> list[str]:
    """Distinct uids whose FCM token was (re)registered within ``inactivity_days``.

    ``register_token`` refreshes ``FIELD_REGISTERED_AT`` on every upsert, and the
    client re-registers on each app launch and on token refresh, so this field
    tracks recency well enough to act as an "active recently" signal.

    Cached in-process for ``_ACTIVE_USERS_CACHE_TTL_SECONDS``: this same query is
    independently re-run by several callers on cadences from every minute to
    hourly, for a value that changes on the timescale of app launches, not
    minutes. ``force_refresh=True`` bypasses the cache (used by the once-a-day
    plan fan-out, where freshness matters more than the negligible read cost of a
    once-a-day call). On a query failure, serves the last cached value (however
    recent — it was populated at most a few minutes ago) and logs loudly rather
    than letting one transient Firestore blip zero out the audience for every
    subsystem this tick; only raises if there is no prior cached value to fall
    back on (matches the pre-cache behavior on a cold start).

    Synchronous — the Firestore Admin SDK is blocking. Async callers wrap this in
    ``asyncio.to_thread`` (see ``feature_store`` and ``orchestrator``).
    Keeping the one query here means writer and readers share a single field contract.
    """
    now = time.monotonic()

    if not force_refresh:
        cached = _active_users_cache.get(inactivity_days)
        if cached is not None and (now - cached[1]) < _ACTIVE_USERS_CACHE_TTL_SECONDS:
            return cached[0]

    try:
        user_ids = _query_active_user_ids(inactivity_days)
    except Exception as exc:
        cached = _active_users_cache.get(inactivity_days)
        if cached is not None:
            logger.error(
                "fcm_token_registry.list_active_user_ids: refresh failed, serving stale cache",
                {"error": str(exc), "cache_age_seconds": round(now - cached[1], 1)},
            )
            return cached[0]
        logger.error(
            "fcm_token_registry.list_active_user_ids: refresh failed, no cached value to fall back on",
            {"error": str(exc)},
        )
        raise

    _active_users_cache[inactivity_days] = (user_ids, now)
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
