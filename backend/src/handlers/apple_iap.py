"""Apple In-App Purchase routes: client transaction intake and Apple's
server notifications.

Thin handlers over `services/apple_iap.py`, deliberately shaped like
`handlers/billing.py` so the two purchase paths fail the same way.

Auth differs by route, and neither uses a shared secret:

  * `POST /billing/apple/transaction` is a Firebase-auth route. The caller's uid
    is the account the purchase attaches to, and the posted JWS is what proves a
    purchase actually happened.
  * `POST /billing/apple/notifications` carries no Firebase identity. Apple's
    JWS signature, verified against the pinned Apple root certificate, IS the
    auth.

Non-2xx means Apple redelivers, so anything we could not apply must not be
acked away.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import Request
from fastapi.responses import JSONResponse

from ..config.settings import settings
from ..lib.logger import logger
from ..services.apple_iap import (
    AppleNotConfiguredError,
    AppleVerificationError,
    activating_write,
    apply_apple_event,
    entitlement_write_for_notification,
    link_subscription_to_uid,
    uid_for_subscription,
    verify_notification,
    verify_transaction,
)
from ..services.billing import _send_entitlement_updated
from ..services.request_auth import resolve_user_id_from_request


async def handle_apple_transaction(request: Request) -> JSONResponse:
    """Intake for a transaction JWS the app just received from StoreKit.

    Called after a purchase or a restore, and again on entitlement refresh. It
    is what maps a StoreKit subscription to a Firebase account: Apple's own
    notifications identify only the subscription, so without this the renewals
    that follow cannot be applied to anybody.
    """
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if not settings.apple_iap_configured:
        # Never trust a payload we cannot fully verify. In production the
        # numeric app id is part of what Apple's verifier checks.
        logger.error("apple_iap: transaction posted but Apple IAP is not configured")
        return JSONResponse({"error": "billing_not_configured"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    signed_transaction = str(body.get("signed_transaction", "")).strip()
    if not signed_transaction:
        return JSONResponse({"error": "signed_transaction is required"}, status_code=400)

    try:
        txn = await asyncio.to_thread(verify_transaction, signed_transaction)
    except AppleNotConfiguredError:
        return JSONResponse({"error": "billing_not_configured"}, status_code=503)
    except AppleVerificationError as exc:
        # A payload that does not verify is either a bug or a forgery attempt.
        # Either way it is not a purchase, and it is not retryable.
        logger.warn("apple_iap: transaction rejected", {
            "user_id": user_id, "error": str(exc),
        })
        return JSONResponse({"error": "invalid_transaction"}, status_code=400)

    now = datetime.now(UTC)
    try:
        write = activating_write(txn, now)
    except AppleVerificationError as exc:
        logger.error("apple_iap: verified transaction for an unsold product", {
            "user_id": user_id, "product_id": str(txn.productId), "error": str(exc),
        })
        return JSONResponse({"error": "unknown_product"}, status_code=422)

    original_transaction_id = str(txn.originalTransactionId or "")
    if not original_transaction_id:
        return JSONResponse({"error": "invalid_transaction"}, status_code=400)

    # Map first, then apply. If the entitlement write fails, the mapping still
    # exists and Apple's notifications remain routable; the reverse order would
    # leave renewals unattributable.
    await asyncio.to_thread(link_subscription_to_uid, original_transaction_id, user_id)

    # transactionId is unique per purchase and per renewal, so it is the natural
    # idempotency key for a client that re-posts the same transaction on every
    # refresh.
    event_id = f"txn:{txn.transactionId}"
    result = await asyncio.to_thread(
        apply_apple_event, event_id, "client_transaction", user_id, write, now
    )

    if result == "processed":
        await _send_entitlement_updated(user_id, event_id, write)

    return JSONResponse({"status": result})


async def handle_apple_notification(request: Request) -> JSONResponse:
    """App Store Server Notifications V2.

    Apple posts a single `signedPayload` JWS. Its signature is the only auth,
    and a body that does not verify is answered 401 rather than processed.
    """
    if not settings.apple_iap_configured:
        # 503, not 200, so Apple keeps retrying until configuration lands.
        logger.error("apple_iap: notification received but Apple IAP is not configured")
        return JSONResponse({"error": "billing_not_configured"}, status_code=503)

    try:
        body = await request.json()
        signed_payload = str((body or {}).get("signedPayload", "")).strip()
    except Exception:
        return JSONResponse({"error": "invalid_payload"}, status_code=400)
    if not signed_payload:
        return JSONResponse({"error": "invalid_payload"}, status_code=400)

    try:
        payload = await asyncio.to_thread(verify_notification, signed_payload)
    except AppleNotConfiguredError:
        return JSONResponse({"error": "billing_not_configured"}, status_code=503)
    except AppleVerificationError as exc:
        logger.warn("apple_iap: notification signature rejected", {"error": str(exc)})
        return JSONResponse({"error": "invalid_signature"}, status_code=401)

    notification_type = payload.notificationType
    subtype = payload.subtype
    event_id = str(payload.notificationUUID or "")
    if not event_id:
        return JSONResponse({"error": "invalid_payload"}, status_code=400)

    # TEST arrives from App Store Connect's "send test notification" button and
    # from Apple's periodic health check. Acking it is the whole point.
    if notification_type == "TEST" or str(notification_type) == "NotificationTypeV2.TEST":
        logger.info("apple_iap: test notification received", {"event_id": event_id})
        return JSONResponse({"status": "ok"})

    signed_txn = getattr(payload.data, "signedTransactionInfo", None) if payload.data else None
    txn = None
    if signed_txn:
        try:
            txn = await asyncio.to_thread(verify_transaction, signed_txn)
        except AppleVerificationError as exc:
            logger.warn("apple_iap: embedded transaction did not verify", {
                "event_id": event_id, "error": str(exc),
            })
            return JSONResponse({"error": "invalid_signature"}, status_code=401)

    occurred_at = None
    if payload.signedDate is not None:
        try:
            occurred_at = datetime.fromtimestamp(int(payload.signedDate) / 1000, tz=UTC)
        except (TypeError, ValueError, OSError):
            occurred_at = None
    now = occurred_at or datetime.now(UTC)

    try:
        write = entitlement_write_for_notification(notification_type, subtype, txn, now)
    except AppleVerificationError as exc:
        # Unsold product, or an activating notification with no transaction.
        # 500 so Apple redelivers and the error log pages someone; acking would
        # silently drop a real subscription change.
        logger.error("apple_iap: cannot build entitlement write", {
            "event_id": event_id,
            "notification_type": str(notification_type),
            "error": str(exc),
        })
        return JSONResponse({"error": "processing_failed"}, status_code=500)

    if write is None:
        # Handled but stateless: price-increase notices, consumption requests,
        # renewal extensions. Nothing to apply, and Apple should stop retrying.
        return JSONResponse({"status": "ignored"})

    if txn is None or not txn.originalTransactionId:
        logger.error("apple_iap: state-changing notification with no subscription", {
            "event_id": event_id, "notification_type": str(notification_type),
        })
        return JSONResponse({"error": "processing_failed"}, status_code=500)

    uid = await asyncio.to_thread(uid_for_subscription, str(txn.originalTransactionId))
    if not uid:
        # The client has not posted this transaction yet, so the subscription is
        # not attributable. 500 so Apple redelivers: it retries for days, and
        # the app posts the transaction on its next entitlement refresh.
        logger.warn("apple_iap: no account mapped for subscription yet", {
            "event_id": event_id,
            "original_transaction_id": str(txn.originalTransactionId),
        })
        return JSONResponse({"error": "unmapped_subscription"}, status_code=500)

    try:
        result = await asyncio.to_thread(
            apply_apple_event, event_id, str(notification_type), uid, write, occurred_at
        )
    except Exception as exc:
        logger.error("apple_iap: notification processing failed", {
            "event_id": event_id,
            "notification_type": str(notification_type),
            "error": str(exc),
        })
        return JSONResponse({"error": "processing_failed"}, status_code=500)

    if result == "processed":
        await _send_entitlement_updated(uid, event_id, write)

    return JSONResponse({"status": result})
