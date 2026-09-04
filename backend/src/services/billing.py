"""Dodo Payments billing: checkout sessions, customer portal, webhook processing.

Dodo is the merchant of record for the web-only subscription (Companion / Pro).
The purchase handshake is metadata: /billing/checkout stamps the caller's
firebase_uid into the Dodo checkout session, so every later webhook already
knows which account it belongs to and this module can upsert
users/{uid}/entitlement/current. (This module is the tier writer, not the
doc's only backend writer: the first-contact trial stamp in
services/entitlement.py and the notified markers in
services/entitlement_notifications.py also write that doc.)

Two non-negotiables (see SUBSCRIPTION_PLAN.md section 6 and the implementation
prompt's ground rules):
  1. Every webhook is signature-verified per the Standard Webhooks spec before
     anything else happens. Unsigned or badly signed events are rejected.
  2. Processing is idempotent via billing_events/{webhook_id}: the claim, the
     staleness verdict, and the entitlement merge happen in ONE Firestore
     transaction, so Dodo's retries (up to 8 per event) reprocess nothing and
     a crash mid-processing leaves nothing half-applied.

Besides the metadata handshake, every webhook also upserts reverse mappings
(dodo_customers/dodo_subscriptions/dodo_payments -> uid) so events that carry
no checkout metadata (Dodo disputes and refunds reference a payment_id only)
can still be resolved to an account.

Secrets (DODO_API_KEY, DODO_WEBHOOK_SECRET) live in Cloud Run env / Secret
Manager only. While unconfigured, checkout/portal answer 503 billing_not_configured
and the webhook answers 503 so Dodo keeps retrying until the secret lands.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from ..config.settings import settings
from ..lib.logger import logger
from .entitlement import (
    SOURCE_WEB,
    entitlement_doc_ref,
    has_active_paid_subscription,
    normalize_status,
    source_may_apply_non_activating,
)
from .firebase import admin_auth

_DODO_TIMEOUT_S = 15.0

# Reject webhooks whose timestamp is further than this from now (replay guard,
# same 5-minute tolerance the Standard Webhooks reference verifier uses).
_WEBHOOK_TOLERANCE_S = 300

BILLING_EVENTS_COLLECTION = "billing_events"

# Reverse mappings from Dodo entity ids to the owning firebase uid, written on
# every webhook that carries the metadata handshake. Disputes and refunds
# arrive with a payment_id and no metadata; these docs are how they still find
# their account. Deterministic doc ids + merge writes keep them idempotent.
DODO_CUSTOMERS_COLLECTION = "dodo_customers"
DODO_SUBSCRIPTIONS_COLLECTION = "dodo_subscriptions"
DODO_PAYMENTS_COLLECTION = "dodo_payments"

VALID_TIERS = ("companion", "pro")
VALID_PERIODS = ("monthly", "yearly")


class DodoApiError(Exception):
    """A Dodo API call failed (network error or non-2xx). Fails loud to the caller."""


class WebhookPayloadError(Exception):
    """A handled webhook event carries an unusable payload (e.g. an activation
    whose tier cannot be resolved). Raised so the route answers 500 and Dodo
    retries; silently acking would drop a real purchase."""


# ── Signature verification (Standard Webhooks) ──────────────────────────────
def verify_webhook_signature(
    *,
    secret: str,
    msg_id: str,
    timestamp: str,
    body: bytes,
    signature_header: str,
) -> bool:
    """True only for a valid, fresh Standard Webhooks signature.

    The signed string is "{webhook-id}.{webhook-timestamp}.{raw body}"; the
    secret is base64 after its "whsec_" prefix; the header carries one or more
    space-separated "v1,<base64 sig>" candidates. Comparison is constant-time.
    Never raises; any malformed input is simply not a valid signature.
    """
    if not (secret and msg_id and timestamp and signature_header):
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts) > _WEBHOOK_TOLERANCE_S:
        return False
    try:
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except Exception:
        return False

    signed = f"{msg_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()

    for candidate in signature_header.split():
        version, _, sig = candidate.partition(",")
        if version == "v1" and sig and hmac.compare_digest(sig, expected):
            return True
    return False


# ── Dodo API calls ───────────────────────────────────────────────────────────
def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.DODO_API_KEY}"}


async def _fetch_customer_identity(uid: str) -> tuple[str, str] | None:
    """(email, display_name) for the signed-in Firebase account, or None.

    The email is the account's, not something the buyer types: it is what pins
    the Dodo customer, the receipt, and every later support or refund lookup to
    the account that will actually be unlocked. Sign-in is Google or Apple only,
    so an email always exists (an Apple private-relay address is a normal,
    deliverable value and needs no special case).

    Never raises: a failed lookup degrades to an unprefilled checkout rather
    than blocking a purchase.
    """
    try:
        record = await asyncio.to_thread(lambda: admin_auth().get_user(uid))
        email = str(getattr(record, "email", "") or "").strip()
        name = str(getattr(record, "display_name", "") or "").strip()
    except Exception as exc:
        logger.warn("billing: account email lookup failed, checkout unprefilled", {
            "user_id": uid, "error": str(exc),
        })
        return None
    if not email:
        logger.warn("billing: account has no email, checkout unprefilled", {
            "user_id": uid,
        })
        return None
    return email, name


@dataclass(frozen=True)
class CheckoutEligibility:
    """Verdict on whether an account may create a checkout session right now.

    blocked_reason is None when checkout is allowed, else "trial_active" or
    "already_subscribed". tier/status/cancel_at_period_end describe the doc for
    the handler's error body; customer_id is the existing Dodo customer the
    session must be pinned to (None when the account has none yet).
    """

    blocked_reason: str | None
    tier: str
    status: str
    cancel_at_period_end: bool
    customer_id: str | None


def evaluate_checkout_eligibility(entitlement: dict) -> CheckoutEligibility:
    """Checkout policy for one already-fetched entitlement doc. Pure.

    Every account gets the full 45-day trial before it can purchase (enforced
    on the authenticated backend route as well as in the client so a stale or
    modified client cannot create an early checkout session), and one live
    paid subscription per account, including cancelled-but-not-yet-expired:
    the user un-cancels through the portal instead of stacking subscriptions.
    An account with a lapsed subscription reuses its existing Dodo customer so
    a re-purchase never mints a second customer record.
    """
    tier = str(entitlement.get("tier", ""))
    status = normalize_status(entitlement)
    cancel_at_period_end = bool(entitlement.get("cancel_at_period_end", False))
    customer_id = str(entitlement.get("dodo_customer_id", "")).strip() or None

    if status == "trialing":
        blocked_reason: str | None = "trial_active"
    elif has_active_paid_subscription(entitlement):
        blocked_reason = "already_subscribed"
    else:
        blocked_reason = None

    return CheckoutEligibility(
        blocked_reason=blocked_reason,
        tier=tier,
        status=status,
        cancel_at_period_end=cancel_at_period_end,
        customer_id=customer_id,
    )


async def create_checkout_session(
    uid: str, tier: str, period: str, customer_id: str | None = None
) -> str:
    """Creates a Dodo checkout session, returns the hosted checkout URL.

    metadata = {firebase_uid, tier, period} is the account handshake: the
    payment webhook reads it back to know which uid to unlock.

    The buyer's email is NOT free-form. It is prefilled from the signed-in
    Firebase account and locked (allow_customer_editing_email=False), so the
    payment can never land under an address the account does not own. Billing
    name, address, tax id, and card stay the buyer's to fill in.

    When the account already has a Dodo customer (customer_id), the session is
    pinned to it so a re-purchase or plan change never mints a second customer
    record, and the identity lookup is skipped entirely: the pinned record
    already carries the email, and the lock keeps it from drifting.

    Raises DodoApiError on any failure (the handler maps it to 502).
    """
    product_id = settings.dodo_product_ids[(tier, period)]
    payload: dict = {
        "product_cart": [{"product_id": product_id, "quantity": 1}],
        "return_url": settings.DODO_CHECKOUT_RETURN_URL,
        "metadata": {"firebase_uid": uid, "tier": tier, "period": period},
        "feature_flags": {"allow_customer_editing_email": False},
    }
    if customer_id:
        payload["customer"] = {"customer_id": customer_id}
    else:
        identity = await _fetch_customer_identity(uid)
        if identity:
            email, name = identity
            customer: dict = {"email": email}
            if name:
                customer["name"] = name
            payload["customer"] = customer

    url = f"{settings.DODO_API_BASE}/checkouts"
    try:
        async with httpx.AsyncClient(timeout=_DODO_TIMEOUT_S, follow_redirects=True) as client:
            resp = await client.post(url, json=payload, headers=_auth_headers())
    except Exception as exc:
        raise DodoApiError(f"checkout request failed: {exc}") from exc
    if resp.status_code >= 400:
        logger.error("billing: Dodo checkout rejected", {
            "user_id": uid, "status": resp.status_code, "body": resp.text[:300],
        })
        raise DodoApiError(f"checkout returned {resp.status_code}")

    checkout_url = str((resp.json() or {}).get("checkout_url", ""))
    if not checkout_url:
        raise DodoApiError("checkout response missing checkout_url")
    return checkout_url


async def create_portal_session(customer_id: str) -> str:
    """Returns the Dodo customer portal URL (manage/cancel) for a customer.

    Raises DodoApiError on any failure.
    """
    url = f"{settings.DODO_API_BASE}/customers/{customer_id}/customer-portal/session"
    try:
        async with httpx.AsyncClient(timeout=_DODO_TIMEOUT_S, follow_redirects=True) as client:
            resp = await client.post(url, headers=_auth_headers())
    except Exception as exc:
        raise DodoApiError(f"portal request failed: {exc}") from exc
    if resp.status_code >= 400:
        logger.error("billing: Dodo portal rejected", {
            "customer_id": customer_id, "status": resp.status_code, "body": resp.text[:300],
        })
        raise DodoApiError(f"portal returned {resp.status_code}")

    link = str((resp.json() or {}).get("link", ""))
    if not link:
        raise DodoApiError("portal response missing link")
    return link


# ── Webhook state machine ────────────────────────────────────────────────────
# Dodo event type -> how the entitlement doc changes. Anything not listed is
# acknowledged with a 200 and no write. payment.succeeded carries no state the
# activation events don't already have, but it is the only event guaranteed to
# link a payment_id to the checkout metadata, so it is handled purely to record
# the dodo_payments reverse mapping that later disputes/refunds resolve through.
_ACTIVATING_EVENTS = ("subscription.active", "subscription.renewed", "subscription.plan_changed")
_GRACE_EVENTS = ("subscription.on_hold", "payment.failed")
_TERMINAL_EVENTS = ("subscription.expired", "subscription.failed", "refund.succeeded", "dispute.opened")
_CANCEL_EVENT = "subscription.cancelled"
_MAPPING_ONLY_EVENTS = ("payment.succeeded",)
_UPDATED_EVENT = "subscription.updated"

HANDLED_EVENT_TYPES = frozenset(
    (*_ACTIVATING_EVENTS, *_GRACE_EVENTS, *_TERMINAL_EVENTS, _CANCEL_EVENT,
     *_MAPPING_ONLY_EVENTS, _UPDATED_EVENT)
)

# Events whose state change is scoped to one specific subscription. If the
# entitlement doc already tracks a DIFFERENT subscription id, such an event is
# stale (it belongs to a superseded subscription, e.g. after a plan change).
# Activating events are exempt (they legitimately install a new subscription);
# refunds/disputes are exempt (money-back revokes access regardless).
_SUBSCRIPTION_SCOPED_EVENTS = frozenset((
    "subscription.on_hold", "subscription.cancelled", "subscription.expired",
    "subscription.failed", _UPDATED_EVENT, "payment.failed",
))


def _parse_dodo_timestamp(value) -> datetime | None:
    """Dodo ISO timestamps (with or without a trailing Z) -> aware datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _resolve_paid_tier(data: dict) -> str | None:
    """The purchased tier, from checkout metadata first, then by reverse-mapping
    the payload's product_id against the configured product IDs."""
    metadata = data.get("metadata") or {}
    tier = str(metadata.get("tier", "")).strip().lower()
    if tier in VALID_TIERS:
        return tier

    product_id = str(data.get("product_id", "")).strip()
    if product_id:
        for (mapped_tier, _period), mapped_id in settings.dodo_product_ids.items():
            if mapped_id and mapped_id == product_id:
                return mapped_tier
    return None


def _activating_write(event_type: str, data: dict, now: datetime) -> dict:
    tier = _resolve_paid_tier(data)
    if tier is None:
        # Never 200-ack a paid activation we cannot apply; raising lets the
        # route 500 so Dodo redelivers (and the error log pages someone).
        logger.error("billing: cannot resolve tier for activating event", {
            "event_type": event_type,
            "product_id": str(data.get("product_id", "")),
        })
        raise WebhookPayloadError(f"unresolvable tier for {event_type}")
    write: dict = {
        "tier": tier,
        "status": "active",
        "source": "web",
        "cancel_at_period_end": False,
        "updated_at": now,
    }
    expires_at = _parse_dodo_timestamp(data.get("next_billing_date"))
    if expires_at is not None:
        write["expires_at"] = expires_at
    subscription_id = data.get("subscription_id")
    if subscription_id:
        write["dodo_subscription_id"] = str(subscription_id)
    customer_id = (data.get("customer") or {}).get("customer_id")
    if customer_id:
        write["dodo_customer_id"] = str(customer_id)
    return write


def _cancel_write(data: dict, now: datetime) -> dict:
    write: dict = {"cancel_at_period_end": True, "updated_at": now}
    expires_at = _parse_dodo_timestamp(data.get("next_billing_date"))
    if expires_at is not None:
        write["expires_at"] = expires_at
    return write


def _terminal_write(now: datetime) -> dict:
    return {
        "tier": "free",
        "status": "expired",
        "cancel_at_period_end": False,
        "updated_at": now,
    }


def entitlement_write_for_event(event_type: str, data: dict) -> dict | None:
    """The exact users/{uid}/entitlement/current merge for one webhook event,
    or None for a handled-but-stateless payload (e.g. a one-time payment.failed
    with no subscription attached, or a mapping-only payment.succeeded).
    Pure: no I/O, unit-testable."""
    now = datetime.now(UTC)

    if event_type in _ACTIVATING_EVENTS:
        return _activating_write(event_type, data, now)

    if event_type in _GRACE_EVENTS:
        if event_type == "payment.failed" and not data.get("subscription_id"):
            # A failed one-time payment is not a dunning event; nothing to do.
            return None
        return {"status": "gracePeriod", "updated_at": now}

    if event_type == _CANCEL_EVENT:
        return _cancel_write(data, now)

    if event_type in _TERMINAL_EVENTS:
        return _terminal_write(now)

    if event_type == _UPDATED_EVENT:
        # Dodo's catch-all sync event: the payload's own status field is the
        # authoritative subscription state, not the event name.
        payload_status = str(data.get("status", "")).strip().lower()
        if payload_status == "active":
            return _activating_write(event_type, data, now)
        if payload_status == "on_hold":
            return {"status": "gracePeriod", "updated_at": now}
        if payload_status == "cancelled":
            return _cancel_write(data, now)
        if payload_status in ("expired", "failed"):
            return _terminal_write(now)
        return None

    return None


# ── uid resolution, id mappings, staleness (Firestore I/O + pure helpers) ────
def _payment_id_from(data: dict) -> str:
    """payment_id wherever Dodo put it (top-level on payment/refund/dispute
    payloads; nested defensively checked)."""
    top = str(data.get("payment_id") or "").strip()
    if top:
        return top
    return str((data.get("payment") or {}).get("payment_id") or "").strip()


def _subscription_id_from(data: dict) -> str:
    return str(data.get("subscription_id") or "").strip()


def _customer_id_from(data: dict) -> str:
    return str((data.get("customer") or {}).get("customer_id") or "").strip()


def _safe_doc_id(value: str) -> str:
    """A Dodo id usable as a Firestore doc id, or "" when it is not."""
    return value if value and "/" not in value else ""


def extract_id_mappings(data: dict, uid: str) -> list[tuple[str, str, dict]]:
    """(collection, doc_id, doc) triples for every Dodo id in the payload,
    linking it to the resolved uid. Pure; written idempotently by the caller."""
    now = datetime.now(UTC)
    mappings: list[tuple[str, str, dict]] = []

    subscription_id = _safe_doc_id(_subscription_id_from(data))
    if subscription_id:
        mappings.append(
            (DODO_SUBSCRIPTIONS_COLLECTION, subscription_id, {"uid": uid, "updated_at": now})
        )
    customer_id = _safe_doc_id(_customer_id_from(data))
    if customer_id:
        mappings.append(
            (DODO_CUSTOMERS_COLLECTION, customer_id, {"uid": uid, "updated_at": now})
        )
    payment_id = _safe_doc_id(_payment_id_from(data))
    if payment_id:
        doc: dict = {"uid": uid, "updated_at": now}
        if subscription_id:
            doc["subscription_id"] = subscription_id
        mappings.append((DODO_PAYMENTS_COLLECTION, payment_id, doc))
    return mappings


async def _resolve_uid(data: dict) -> str:
    """The account a webhook belongs to: checkout metadata first, then the
    durable reverse mappings. "" when unresolvable."""
    metadata = data.get("metadata") or {}
    uid = str(metadata.get("firebase_uid", "")).strip()
    if uid:
        return uid

    candidates = (
        (DODO_SUBSCRIPTIONS_COLLECTION, _safe_doc_id(_subscription_id_from(data))),
        (DODO_PAYMENTS_COLLECTION, _safe_doc_id(_payment_id_from(data))),
        (DODO_CUSTOMERS_COLLECTION, _safe_doc_id(_customer_id_from(data))),
    )

    def _lookup() -> str:
        from ..services.firebase import admin_firestore

        db = admin_firestore()
        for collection, doc_id in candidates:
            if not doc_id:
                continue
            found = db.collection(collection).document(doc_id).get().to_dict() or {}
            mapped = str(found.get("uid", "")).strip()
            if mapped:
                return mapped
        return ""

    return await asyncio.to_thread(_lookup)


def _is_stale(
    ent: dict,
    event_type: str,
    occurred_at: datetime | None,
    payload_subscription_id: str,
) -> bool:
    """Whether an event must NOT be applied because newer state already landed.

    Dodo delivers the latest object state and explicitly does not guarantee
    ordering, so a redelivered subscription.renewed must never overwrite a
    later cancellation. Two guards:
      1. Timestamp: older than the doc's last applied event -> stale. Equal
         timestamps apply (second-granularity ties are ambiguous and the merge
         is idempotent); events with no parsable timestamp always apply.
      2. Superseded subscription: a subscription-scoped state event for a
         subscription the doc no longer tracks -> stale.

    Ownership by a rival seller is deliberately NOT decided here: whether a write
    grants or revokes access is a property of the computed write, not of the
    event name, so that guard lives in [_apply_webhook_txn].
    """
    last = ent.get("last_billing_event_at")
    if occurred_at is not None and isinstance(last, datetime):
        last_aware = last if last.tzinfo else last.replace(tzinfo=UTC)
        if occurred_at < last_aware:
            return True

    if event_type in _SUBSCRIPTION_SCOPED_EVENTS:
        tracked = str(ent.get("dodo_subscription_id", "")).strip()
        if tracked and payload_subscription_id and tracked != payload_subscription_id:
            return True

    return False


def _apply_webhook_txn(
    event_id: str,
    event_type: str,
    uid: str,
    write: dict | None,
    occurred_at: datetime | None,
    mappings: list[tuple[str, str, dict]],
    payload_subscription_id: str,
) -> str:
    """The idempotency claim, staleness verdict, mapping upserts, and
    entitlement merge as ONE transaction. Runs in a worker thread.
    Returns "processed" | "duplicate" | "stale". A raise commits nothing, so
    the route 500s and Dodo's redelivery genuinely reprocesses.
    """
    from google.cloud import firestore as gcloud_firestore

    from ..services.firebase import admin_firestore

    db = admin_firestore()
    claim_ref = db.collection(BILLING_EVENTS_COLLECTION).document(event_id)
    ent_ref = entitlement_doc_ref(uid, db)
    transaction = db.transaction()

    @gcloud_firestore.transactional
    def _execute(txn) -> str:
        # Firestore transaction rule: every read before any write.
        if claim_ref.get(transaction=txn).exists:
            return "duplicate"
        ent = ent_ref.get(transaction=txn).to_dict() or {}
        stale = _is_stale(ent, event_type, occurred_at, payload_subscription_id)

        # Rival-seller guard. iOS sells through StoreKit, so this document may be
        # owned by Apple. A write that GRANTS access may always land and takes
        # ownership; a write that revokes or degrades it describes the web
        # subscription only, and must not touch an entitlement Apple owns.
        # Judged on the write, not the event name: `subscription.updated` can
        # mean either, and blocking it wholesale would leave a live web
        # subscriber on the free tier after their old App Store plan lapsed.
        if (
            not stale
            and write is not None
            and write.get("status") != "active"
            and not source_may_apply_non_activating(ent, SOURCE_WEB)
        ):
            stale = True

        # A stale event still writes its claim: staleness is a final verdict,
        # a redelivery must not reprocess it.
        txn.set(claim_ref, {
            "uid": uid,
            "event_type": event_type,
            "processed_at": datetime.now(UTC),
            "stale": stale,
        })
        for collection, doc_id, doc in mappings:
            txn.set(db.collection(collection).document(doc_id), doc, merge=True)
        if stale:
            return "stale"

        if write is not None:
            merged = dict(write)
            if occurred_at is not None:
                merged["last_billing_event_at"] = occurred_at
                merged["last_billing_event_id"] = event_id
            txn.set(ent_ref, merged, merge=True)
        return "processed"

    return _execute(transaction)


# ── Client sync push ─────────────────────────────────────────────────────────
# The visible copy matters on iOS: data_only still carries an aps.alert there
# (notification_service.py), so the line must read as real account news.
# Android delivers straight to the background handler.
_PUSH_BODY_BY_STATUS = {
    "active": "Your Aura plan is active. Everything is unlocked.",
    "gracePeriod": "There was a problem with your payment. Your plan is still active, update your card to keep it that way.",
    "expired": "Your Aura plan has ended. You are back on the free tier.",
}
_PUSH_BODY_CANCELLED = "Your plan stays active until the end of this billing period."


async def _send_entitlement_updated(uid: str, event_id: str, write: dict) -> None:
    """Nudge every device to refetch /entitlement after a webhook write.

    Routed through the orchestrator like every other producer (COMMITTED lane:
    sends inline, never held). Best-effort: a push failure never fails the
    webhook, the TTL refetch on next launch is the safety net.
    """
    from .notifications import orchestrator
    from .notifications.proposal import SOURCE_BILLING, NotificationProposal, ProposalKind

    status = str(write.get("status", ""))
    if write.get("cancel_at_period_end") and "status" not in write:
        body = _PUSH_BODY_CANCELLED
    else:
        body = _PUSH_BODY_BY_STATUS.get(status, "Your Aura plan changed.")

    try:
        await orchestrator.submit(
            NotificationProposal(
                user_id=uid,
                source=SOURCE_BILLING,
                kind=ProposalKind.COMMITTED,
                dedup_key=f"billing_{event_id}",
                title="Aura",
                body=body,
                data={
                    "type": "entitlement-updated",
                    "tier": str(write.get("tier", "")),
                    "status": status,
                },
                notification_type="entitlement_updated",
                data_only=True,
                collapse_key=f"entitlement_{uid}",
            )
        )
    except Exception as exc:
        logger.warn("billing: entitlement-updated push failed", {
            "user_id": uid, "event_id": event_id, "error": str(exc),
        })


# ── Webhook entry point ──────────────────────────────────────────────────────
def _parse_event_occurred_at(envelope_timestamp, header_timestamp: str) -> datetime | None:
    """When the event happened, for the staleness guard: the envelope's ISO
    timestamp first, else the webhook-timestamp header (unix seconds)."""
    parsed = _parse_dodo_timestamp(envelope_timestamp)
    if parsed is not None:
        return parsed
    try:
        return datetime.fromtimestamp(int(header_timestamp), UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


async def process_webhook_envelope(
    event_id: str, envelope: dict, header_timestamp: str
) -> dict:
    """Decomposes one signature-verified webhook envelope (type, data,
    occurred_at) and applies it via process_webhook_event. The handler hands
    over the parsed JSON dict as-is; the envelope shape is billing's to own."""
    event_type = str(envelope.get("type", ""))
    data = envelope.get("data")
    if not isinstance(data, dict):
        data = {}
    occurred_at = _parse_event_occurred_at(envelope.get("timestamp"), header_timestamp)
    return await process_webhook_event(event_id, event_type, data, occurred_at)


async def process_webhook_event(
    event_id: str,
    event_type: str,
    data: dict,
    occurred_at: datetime | None = None,
) -> dict:
    """Applies one verified webhook event. Returns {"status": ...} for the handler.

    uid resolves from checkout metadata or the reverse mappings; the claim,
    staleness check, mapping upserts, and entitlement merge then commit in one
    transaction, and the sync push fires last. Any raise reaches the route as a
    500 so Dodo redelivers against an untouched store.
    """
    if event_type not in HANDLED_EVENT_TYPES:
        logger.info("billing: webhook event ignored", {
            "event_id": event_id, "event_type": event_type,
        })
        return {"status": "ignored"}

    uid = await _resolve_uid(data)
    if not uid:
        if event_type in _TERMINAL_EVENTS:
            # A terminal event revokes access; acking it away silently is how
            # a dispute never downgrades anyone. Raising lets Dodo redeliver:
            # the mapping it needs may land moments later from a
            # payment.succeeded or subscription event still in flight.
            logger.error("billing: cannot resolve uid for terminal event", {
                "event_id": event_id, "event_type": event_type,
            })
            raise WebhookPayloadError(f"unresolvable uid for {event_type}")
        # For non-terminal events a retry cannot fix a payload with no account
        # handshake and no mapping; ack it and alert loudly instead of letting
        # Dodo retry for 8 rounds.
        logger.error("billing: webhook payload has no resolvable uid", {
            "event_id": event_id, "event_type": event_type,
        })
        return {"status": "ignored"}

    write = entitlement_write_for_event(event_type, data)
    mappings = extract_id_mappings(data, uid)
    if write is None and not mappings:
        return {"status": "ignored"}

    result = await asyncio.to_thread(
        _apply_webhook_txn,
        event_id,
        event_type,
        uid,
        write,
        occurred_at,
        mappings,
        _subscription_id_from(data),
    )

    if result == "duplicate":
        logger.info("billing: duplicate webhook delivery skipped", {
            "event_id": event_id, "event_type": event_type, "user_id": uid,
        })
        return {"status": "duplicate"}

    if result == "stale":
        logger.info("billing: stale webhook event skipped", {
            "event_id": event_id, "event_type": event_type, "user_id": uid,
        })
        return {"status": "stale"}

    if write is not None:
        logger.info("billing: entitlement updated from webhook", {
            "event_id": event_id, "event_type": event_type, "user_id": uid,
            "status": str(write.get("status", "")), "tier": str(write.get("tier", "")),
        })
        await _send_entitlement_updated(uid, event_id, write)

    return {"status": "processed"}
