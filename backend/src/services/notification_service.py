"""
Centralized FCM notification service.

Usage anywhere in the backend:

    from ..services.notification_service import send_notification

    result = await send_notification(
        user_id,
        title="Buddy Reminder",
        body="Time to complete your rental application.",
        data={"reminder_id": "abc123"},
        notification_type="reminder",
        priority="high",
        collapse_key="reminder_abc123",
        apns_category="BUDDY_REMINDER",
    )

``send_notification`` is an async function — all blocking Firestore and
Firebase Admin SDK calls are dispatched to a thread pool via
``asyncio.to_thread`` so the event loop is never stalled.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from firebase_admin import messaging

from ..lib.logger import logger
from . import notification_ledger
from .fcm_token_registry import (
    get_user_tokens,
    is_permanently_invalid_token_error,
    remove_invalid_tokens,
)
from .firebase import admin_messaging

# Android notification channel created by the Flutter app on first launch.
_ANDROID_CHANNEL_ID = "aura_default"


@dataclass
class NotificationResult:
    """Result of a ``send_notification`` call."""

    tokens_targeted: int
    """Total number of device tokens the message was sent to."""

    success_count: int
    """Tokens that FCM accepted."""

    failure_count: int
    """Tokens that FCM rejected (includes invalid tokens)."""

    invalid_tokens: list[str] = field(default_factory=list)
    """Tokens that were permanently invalid and have been auto-deleted."""

    notification_id: str = ""
    """Stable identity shared by all delivery channels for this notification."""

    desktop_queued_count: int = 0
    """Desktop outbox rows accepted for delivery."""

    channel_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Per-channel outcomes used by the unified notification ledger."""

    @property
    def delivered(self) -> bool:
        """True if at least one selected channel accepted the notification."""
        return self.success_count > 0 or self.desktop_queued_count > 0


async def send_notification(
    user_id: str,
    *,
    title: str,
    body: str,
    data: dict[str, str] | None = None,
    notification_type: str = "general",
    priority: Literal["high", "normal"] = "high",
    collapse_key: str | None = None,
    badge: int | None = None,
    sound: str = "default",
    apns_category: str | None = None,
    data_only: bool = False,
    dedup_key: str = "",
    decision: notification_ledger.NotificationDecision | None = None,
    notification_id: str | None = None,
    record_ledger: bool = True,
) -> NotificationResult:
    """Send an FCM push notification to all registered devices for a user.

    Automatically cleans up any permanently-invalid tokens that FCM
    reports back so stale tokens never accumulate.

    Args:
        user_id:           Firestore user document ID.
        title:             Notification title shown on the device.
        body:              Notification body text.
        data:              Extra string key-value pairs delivered to the app
                           (on top of the built-in ``notification_type`` /
                           ``user_id`` fields).  All values must be strings.
        notification_type: Client-side routing key (e.g. ``"reminder"``,
                           ``"calendar_event"``, ``"chat"``, ``"general"``).
                           Delivered in the FCM data payload so the Flutter
                           app can navigate to the right screen on tap.
        priority:          ``"high"`` for time-sensitive alerts (wakes the
                           device), ``"normal"`` for background sync.
        collapse_key:      Replaces a pending notification with the same key.
                           Use ``f"reminder_{reminder_id}"`` to prevent
                           duplicate reminder banners.
        badge:             iOS app badge count.  ``None`` leaves the badge
                           unchanged.
        sound:             Notification sound name.  Defaults to
                           ``"default"`` (system sound).
        apns_category:     iOS interactive notification category (enables
                           action buttons defined in the app).
        data_only:         When True, omit the Android display ``notification``
                           block so the message is delivered straight to the
                           app's background handler, which builds the rich
                           notification (action-button suggestion chips) itself
                           from the data payload. iOS still shows an alert via
                           the APNS ``aps.alert`` so it is never silent there.
        decision:          Optional learning-substrate metadata (score, scoring
                           components, framer relevance reason / prompt version)
                           for LLM-framed proactive sends. Persisted on the
                           notification ledger row; deterministic paths omit it.

    Returns:
        ``NotificationResult`` with delivery counts and a list of invalid
        tokens that were auto-deleted from Firestore.
    """

    # One logical id can be supplied by the channel router so mobile and desktop
    # share the same audit identity. Direct mobile callers keep UUID behavior.
    notification_id = notification_id or str(uuid.uuid4())

    # 1. Fetch registered tokens
    token_docs: list[dict[str, Any]] = await asyncio.to_thread(
        get_user_tokens, user_id
    )

    if not token_docs:
        logger.info("send_notification: no registered tokens found, skipping user", {
            "user_id": user_id,
            "title": title,
            "notification_type": notification_type,
        })
        return NotificationResult(
            tokens_targeted=0,
            success_count=0,
            failure_count=0,
            notification_id=notification_id,
        )

    token_strings: list[str] = [doc["token"] for doc in token_docs]

    # 2. Build FCM data payload
    payload: dict[str, str] = {
        "notification_type": notification_type,
        "user_id": user_id,
    }
    if data:
        payload.update(data)

    # Every notification gets a stable id, carried in the payload so the client
    # can report taps/dismissals against it (the signal engine already supplies
    # one; generate it for the other paths). Also the ledger doc id.
    notification_id = payload.get("notification_id") or notification_id
    payload["notification_id"] = notification_id

    # 3. Build platform-specific message
    apns_headers: dict[str, str] = {
        "apns-priority": "10" if priority == "high" else "5",
    }
    if collapse_key:
        apns_headers["apns-collapse-id"] = collapse_key

    # In data-only mode the Android side carries no display notification — the
    # app's background handler renders it (with interactive suggestion chips) —
    # but iOS cannot build a notification from a data push, so the alert is
    # carried in the APNS payload to keep iOS from going silent.
    android_notification = (
        None
        if data_only
        else messaging.AndroidNotification(sound=sound, channel_id=_ANDROID_CHANNEL_ID)
    )
    aps_alert = messaging.ApsAlert(title=title, body=body) if data_only else None

    message = messaging.MulticastMessage(
        tokens=token_strings,
        notification=None if data_only else messaging.Notification(title=title, body=body),
        data=payload,
        android=messaging.AndroidConfig(
            priority="high" if priority == "high" else "normal",
            collapse_key=collapse_key,
            notification=android_notification,
        ),
        apns=messaging.APNSConfig(
            headers=apns_headers,
            payload=messaging.APNSPayload(
                aps=messaging.Aps(
                    alert=aps_alert,
                    sound=sound,
                    badge=badge,
                    category=apns_category,
                    content_available=True,
                ),
            ),
        ),
    )

    logger.info("send_notification: sending notification..", {
        "user_id": user_id,
        "notification_type": notification_type,
        "title": title,
        "token_count": len(token_strings),
        "priority": priority,
        "collapse_key": collapse_key,
    })

    # Record one ledger row per attempted send (success or failure). This is the
    # single choke point through which every notification path flows, so writing
    # here covers all of them. Fire-and-forget: record_send swallows its own
    # errors and must never break or delay delivery.
    data_in = data or {}

    async def _record_to_ledger(
        *, delivered: bool, tokens_targeted: int, success_count: int, failure_count: int
    ) -> None:
        if not record_ledger:
            return
        await notification_ledger.record_send(
            user_id,
            notification_id=notification_id,
            notification_type=notification_type,
            origin=str(data_in.get("notification_origin", notification_type)),
            title=title,
            body=body,
            url=str(data_in.get("url", "")),
            content_id=str(data_in.get("content_id", "")),
            source=str(data_in.get("source", "")),
            category=str(data_in.get("category", "")),
            content_kind=str(data_in.get("content_kind", "")),
            dedup_key=dedup_key,
            delivered=delivered,
            tokens_targeted=tokens_targeted,
            success_count=success_count,
            failure_count=failure_count,
            decision=decision,
        )

    # 4. Send via FCM
    try:
        batch_response: messaging.BatchResponse = await asyncio.to_thread(
            admin_messaging().send_each_for_multicast, message
        )
    except Exception as exc:
        logger.error("send_notification: FCM send failed", {
            "user_id": user_id,
            "notification_type": notification_type,
            "token_count": len(token_strings),
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
        await _record_to_ledger(
            delivered=False,
            tokens_targeted=len(token_strings),
            success_count=0,
            failure_count=len(token_strings),
        )
        return NotificationResult(
            tokens_targeted=len(token_strings),
            success_count=0,
            failure_count=len(token_strings),
            notification_id=notification_id,
        )

    # 5. Collect invalid tokens from FCM response
    invalid: list[str] = []
    for idx, response in enumerate(batch_response.responses):
        if response.success:
            continue
        exc = response.exception
        error_code = ""
        if exc is not None:
            # firebase_admin wraps errors; code is in exc.cause or exc.code
            error_code = (
                getattr(exc, "code", "")
                or getattr(getattr(exc, "cause", None), "error_code", "")
                or ""
            )
            if isinstance(error_code, str):
                # Normalise: "messaging/registration-token-not-registered"
                error_code = error_code.split("/")[-1].lower()

        is_invalid = is_permanently_invalid_token_error(exc)

        logger.warn("send_notification: token delivery failed", {
            "user_id": user_id,
            "token_preview": token_strings[idx][:20],
            "error_code": error_code,
            "error": str(exc),
            "token_removed": is_invalid,
        })

        if is_invalid:
            invalid.append(token_strings[idx])

    # 6. Auto-delete permanently invalid tokens
    if invalid:
        await asyncio.to_thread(remove_invalid_tokens, user_id, invalid)

    result = NotificationResult(
        tokens_targeted=len(token_strings),
        success_count=batch_response.success_count,
        failure_count=batch_response.failure_count,
        invalid_tokens=invalid,
        notification_id=notification_id,
    )

    logger.info("send_notification: complete", {
        "user_id": user_id,
        "notification_type": notification_type,
        "tokens_targeted": result.tokens_targeted,
        "success_count": result.success_count,
        "failure_count": result.failure_count,
        "invalid_removed": len(invalid),
    })

    await _record_to_ledger(
        delivered=result.delivered,
        tokens_targeted=result.tokens_targeted,
        success_count=result.success_count,
        failure_count=result.failure_count,
    )

    return result
