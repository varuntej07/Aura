"""Billing routes: web checkout creation, Dodo webhook intake, customer portal.

Thin handlers over services/billing.py. Checkout and portal are Firebase-auth
routes (the caller's uid is the account the purchase attaches to); the webhook
carries no Firebase identity, its Standard Webhooks signature IS the auth.
"""

from __future__ import annotations

import json

from fastapi import Request
from fastapi.responses import JSONResponse

from ..config.settings import settings
from ..lib.logger import logger
from ..services.billing import (
    VALID_PERIODS,
    VALID_TIERS,
    DodoApiError,
    create_checkout_session,
    create_portal_session,
    parse_event_occurred_at,
    process_webhook_event,
    verify_webhook_signature,
)
from ..services.entitlement import (
    EntitlementUnavailableError,
    fetch_entitlement_doc,
    has_active_paid_subscription,
    normalize_status,
)
from ..services.request_auth import resolve_user_id_from_request


async def handle_billing_checkout(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    tier = str(body.get("tier", "")).strip().lower()
    period = str(body.get("period", "")).strip().lower()
    if tier not in VALID_TIERS or period not in VALID_PERIODS:
        return JSONResponse(
            {"error": "tier must be companion|pro and period must be monthly|yearly"},
            status_code=400,
        )

    if not settings.dodo_configured:
        logger.error("billing: checkout requested but Dodo is not configured", {
            "user_id": user_id,
        })
        return JSONResponse({"error": "billing_not_configured"}, status_code=503)

    # Duplicate-subscription guard: one live paid subscription per account.
    # Firestore being down is a deliberate 503, never a blind checkout that
    # could mint a second subscription for an already-paying user.
    try:
        entitlement = await fetch_entitlement_doc(user_id)
    except EntitlementUnavailableError:
        return JSONResponse({"error": "entitlement_unavailable"}, status_code=503)

    # Every account gets the full 45-day trial before it can purchase. Keep
    # this guard on the authenticated backend route as well as in the client so
    # a stale or modified client cannot create an early checkout session.
    if normalize_status(entitlement) == "trialing":
        return JSONResponse({"error": "trial_active"}, status_code=409)

    if has_active_paid_subscription(entitlement):
        logger.info("billing: checkout blocked, subscription already live", {
            "user_id": user_id, "tier": str(entitlement.get("tier", "")),
        })
        return JSONResponse({
            "error": "already_subscribed",
            "tier": str(entitlement.get("tier", "")),
            "status": normalize_status(entitlement),
            "cancel_at_period_end": bool(entitlement.get("cancel_at_period_end", False)),
        }, status_code=409)

    customer_id = str(entitlement.get("dodo_customer_id", "")).strip() or None

    try:
        checkout_url = await create_checkout_session(
            user_id, tier, period, customer_id=customer_id
        )
    except DodoApiError as exc:
        logger.error("billing: checkout creation failed", {
            "user_id": user_id, "tier": tier, "period": period, "error": str(exc),
        })
        return JSONResponse({"error": "checkout_failed"}, status_code=502)

    return JSONResponse({"checkout_url": checkout_url})


async def handle_billing_webhook(request: Request) -> JSONResponse:
    if not settings.dodo_webhook_configured:
        # 503 (not 200) so Dodo keeps retrying until the secret is configured;
        # an unverifiable event must never be processed or acked away.
        logger.error("billing: webhook received but DODO_WEBHOOK_SECRET is not set")
        return JSONResponse({"error": "billing_not_configured"}, status_code=503)

    raw_body = await request.body()
    msg_id = request.headers.get("webhook-id", "")
    timestamp = request.headers.get("webhook-timestamp", "")
    signature = request.headers.get("webhook-signature", "")

    if not verify_webhook_signature(
        secret=settings.DODO_WEBHOOK_SECRET,
        msg_id=msg_id,
        timestamp=timestamp,
        body=raw_body,
        signature_header=signature,
    ):
        logger.warn("billing: webhook signature rejected", {"webhook_id": msg_id})
        return JSONResponse({"error": "invalid_signature"}, status_code=401)

    try:
        envelope = json.loads(raw_body)
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid_payload"}, status_code=400)
    if not isinstance(envelope, dict):
        return JSONResponse({"error": "invalid_payload"}, status_code=400)

    event_type = str(envelope.get("type", ""))
    data = envelope.get("data")
    if not isinstance(data, dict):
        data = {}
    occurred_at = parse_event_occurred_at(envelope.get("timestamp"), timestamp)

    try:
        result = await process_webhook_event(msg_id, event_type, data, occurred_at)
    except Exception as exc:
        # 500 -> Dodo redelivers with backoff; the idempotency claim was rolled
        # back (or never made), so the retry actually reprocesses.
        logger.error("billing: webhook processing failed", {
            "webhook_id": msg_id, "event_type": event_type, "error": str(exc),
        })
        return JSONResponse({"error": "processing_failed"}, status_code=500)

    return JSONResponse(result)


async def handle_billing_portal(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if not settings.DODO_API_KEY:
        logger.error("billing: portal requested but Dodo is not configured", {
            "user_id": user_id,
        })
        return JSONResponse({"error": "billing_not_configured"}, status_code=503)

    try:
        entitlement = await fetch_entitlement_doc(user_id)
    except EntitlementUnavailableError:
        return JSONResponse({"error": "entitlement_unavailable"}, status_code=503)

    customer_id = str(entitlement.get("dodo_customer_id", "")).strip()
    if not customer_id:
        return JSONResponse({"error": "no_billing_account"}, status_code=404)

    try:
        portal_url = await create_portal_session(customer_id)
    except DodoApiError as exc:
        logger.error("billing: portal creation failed", {
            "user_id": user_id, "error": str(exc),
        })
        return JSONResponse({"error": "portal_failed"}, status_code=502)

    return JSONResponse({"portal_url": portal_url})
