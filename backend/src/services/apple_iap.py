"""Apple In-App Purchase: the iOS seller.

The mirror of `services/billing.py`. iOS cannot offer the Dodo web checkout at
all - App Store Guideline 3.1.1 forbids sending users to an outside payment page
for digital content - so StoreKit sells there and this module turns Apple's
signed payloads into the same `users/{uid}/entitlement/current` merge the web
path writes. One document, two sellers, told apart by its `source` field, and
neither may cancel or expire a subscription the other owns
(`source_may_apply_non_activating`).

Trust model. Every payload arrives as a JWS signed by Apple and is verified
against the Apple Root CA G3 certificate committed at `src/resources/apple`,
using Apple's own library rather than hand-rolled certificate-chain walking. A
forged payload here is a free subscription for anyone who can post JSON, so the
verification is not something to approximate. Nothing in this path is a shared
secret, which is why there is no key to rotate.

Two entry points, both in `handlers/apple_iap.py`:

  * the client posts the transaction JWS after a purchase or restore. This is
    what establishes the originalTransactionId -> uid mapping, because Apple's
    own notifications carry no Firebase identity.
  * Apple posts App Store Server Notifications V2 for everything afterwards:
    renewals, cancellations, dunning, expiry, refunds.

Ordering caveat worth knowing: a notification can arrive before the client has
ever posted its transaction, so no mapping exists yet and the uid is unknown.
That is answered with a 500 so Apple redelivers, and Apple retries for days. The
client also re-verifies on every entitlement refresh, so the mapping lands as
soon as the app is opened.
"""

from __future__ import annotations

import functools
from datetime import UTC, datetime
from pathlib import Path

from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.models.JWSTransactionDecodedPayload import (
    JWSTransactionDecodedPayload,
)
from appstoreserverlibrary.models.NotificationTypeV2 import NotificationTypeV2
from appstoreserverlibrary.models.ResponseBodyV2DecodedPayload import (
    ResponseBodyV2DecodedPayload,
)
from appstoreserverlibrary.models.Subtype import Subtype
from appstoreserverlibrary.signed_data_verifier import (
    SignedDataVerifier,
    VerificationException,
)

from ..config.settings import settings
from ..lib.logger import logger
from .entitlement import SOURCE_APPLE, entitlement_doc_ref

# Idempotency claims share the collection the Dodo webhook already uses, with an
# "apple:" prefix on the id so the two namespaces cannot collide.
from .billing import BILLING_EVENTS_COLLECTION

# originalTransactionId -> {uid}. Apple's notifications identify a subscription,
# never a Firebase account, so without this map a renewal cannot be applied.
APPLE_SUBSCRIPTIONS_COLLECTION = "apple_subscriptions"

_ROOT_CERT_DIR = Path(__file__).resolve().parent.parent / "resources" / "apple"


class AppleVerificationError(Exception):
    """A payload did not verify as genuinely Apple's, or is not for this app.

    Never downgrade this to a warning. It means either a misconfiguration or
    someone posting a forged subscription.
    """


class AppleNotConfiguredError(Exception):
    """Verification cannot be attempted yet (see settings.apple_iap_configured)."""


@functools.lru_cache(maxsize=1)
def _root_certificates() -> tuple[bytes, ...]:
    certs = tuple(p.read_bytes() for p in sorted(_ROOT_CERT_DIR.glob("*.cer")))
    if not certs:
        raise AppleNotConfiguredError(
            f"no Apple root certificates found in {_ROOT_CERT_DIR}"
        )
    return certs


@functools.lru_cache(maxsize=1)
def _verifier() -> SignedDataVerifier:
    if not settings.apple_iap_configured:
        raise AppleNotConfiguredError("Apple IAP is not configured")
    environment = (
        Environment.SANDBOX if settings.apple_iap_sandbox else Environment.PRODUCTION
    )
    return SignedDataVerifier(
        root_certificates=list(_root_certificates()),
        # Online OCSP checks would put an Apple network round trip inside the
        # webhook's response budget. The pinned root plus the chain in the
        # payload is what establishes authenticity; revocation of an Apple
        # signing cert is not a threat this path can usefully react to.
        enable_online_checks=False,
        environment=environment,
        bundle_id=settings.APPLE_IAP_BUNDLE_ID,
        app_apple_id=settings.APPLE_IAP_APP_APPLE_ID or None,
    )


def verify_transaction(signed_transaction: str) -> JWSTransactionDecodedPayload:
    """Verify a transaction JWS as posted by the app. Raises on anything else."""
    try:
        return _verifier().verify_and_decode_signed_transaction(signed_transaction)
    except VerificationException as exc:
        raise AppleVerificationError(f"transaction did not verify: {exc}") from exc


def verify_notification(signed_payload: str) -> ResponseBodyV2DecodedPayload:
    """Verify an App Store Server Notification V2 body. Raises on anything else."""
    try:
        return _verifier().verify_and_decode_notification(signed_payload)
    except VerificationException as exc:
        raise AppleVerificationError(f"notification did not verify: {exc}") from exc


# ── Product -> tier ──────────────────────────────────────────────────────────


def tier_for_product(product_id: str) -> str | None:
    """The paid tier a StoreKit product grants, or None if we do not sell it.

    None is never treated as "no change": an unrecognised product on a paid
    event is a configuration error worth failing loudly on, exactly as the Dodo
    path treats an unresolvable tier.
    """
    mapping = {
        settings.APPLE_PRODUCT_COMPANION_MONTHLY: "companion",
        settings.APPLE_PRODUCT_COMPANION_YEARLY: "companion",
        settings.APPLE_PRODUCT_PRO_MONTHLY: "pro",
        settings.APPLE_PRODUCT_PRO_YEARLY: "pro",
    }
    return mapping.get((product_id or "").strip())


def _ms_to_datetime(value: int | None) -> datetime | None:
    """Apple sends timestamps as milliseconds since the epoch."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


# ── Entitlement writes (pure: no I/O, inspectable) ───────────────────────────

# Everything that means "this account is paid right now".
_ACTIVATING_TYPES = frozenset({
    NotificationTypeV2.SUBSCRIBED,
    NotificationTypeV2.DID_RENEW,
    NotificationTypeV2.OFFER_REDEEMED,
    NotificationTypeV2.DID_CHANGE_RENEWAL_PREF,
})

# Access is over, immediately.
_TERMINAL_TYPES = frozenset({
    NotificationTypeV2.EXPIRED,
    NotificationTypeV2.GRACE_PERIOD_EXPIRED,
    NotificationTypeV2.REFUND,
    NotificationTypeV2.REVOKE,
})


def activating_write(txn: JWSTransactionDecodedPayload, now: datetime) -> dict:
    """The entitlement merge for a transaction that grants access.

    Raises when the product is not one we sell, so the route 500s and Apple
    redelivers, rather than silently acknowledging a purchase we cannot honour.
    """
    tier = tier_for_product(txn.productId)
    if tier is None:
        logger.error("apple_iap: cannot resolve tier for product", {
            "product_id": str(txn.productId),
        })
        raise AppleVerificationError(f"unsold product id {txn.productId!r}")

    write: dict = {
        "tier": tier,
        "status": "active",
        "source": SOURCE_APPLE,
        "cancel_at_period_end": False,
        "updated_at": now,
        "apple_product_id": txn.productId,
        "apple_original_transaction_id": txn.originalTransactionId,
    }
    expires_at = _ms_to_datetime(txn.expiresDate)
    if expires_at is not None:
        write["expires_at"] = expires_at
    return write


def terminal_write(now: datetime) -> dict:
    return {
        "tier": "free",
        "status": "expired",
        "source": SOURCE_APPLE,
        "cancel_at_period_end": False,
        "updated_at": now,
    }


def entitlement_write_for_notification(
    notification_type: NotificationTypeV2 | None,
    subtype: Subtype | None,
    txn: JWSTransactionDecodedPayload | None,
    now: datetime,
) -> dict | None:
    """The exact entitlement merge for one notification, or None for a payload
    that carries no state change (TEST, price-increase notices, consumption
    requests). Pure, so it can be inspected without Firestore.
    """
    if notification_type in _ACTIVATING_TYPES:
        if txn is None:
            raise AppleVerificationError(
                f"{notification_type} carried no transaction to apply"
            )
        # A downgrade takes effect only at the period end, so the plan the user
        # is entitled to TODAY is still the one in the current transaction.
        return activating_write(txn, now)

    if notification_type in _TERMINAL_TYPES:
        return terminal_write(now)

    if notification_type == NotificationTypeV2.DID_FAIL_TO_RENEW:
        # With a grace period the user keeps access while Apple retries the
        # card; without one, billing retry has already ended access.
        if subtype == Subtype.GRACE_PERIOD:
            return {"status": "gracePeriod", "source": SOURCE_APPLE, "updated_at": now}
        return terminal_write(now)

    if notification_type == NotificationTypeV2.DID_CHANGE_RENEWAL_STATUS:
        # Auto-renew off is not a cancellation: access runs to the paid-through
        # date. This mirrors the web path's cancel_at_period_end exactly.
        turning_off = subtype == Subtype.AUTO_RENEW_DISABLED
        write: dict = {
            "cancel_at_period_end": turning_off,
            "source": SOURCE_APPLE,
            "updated_at": now,
        }
        expires_at = _ms_to_datetime(txn.expiresDate) if txn is not None else None
        if expires_at is not None:
            write["expires_at"] = expires_at
        return write

    return None


# ── Firestore ────────────────────────────────────────────────────────────────


def link_subscription_to_uid(original_transaction_id: str, uid: str) -> None:
    """Record which account owns a StoreKit subscription.

    Written when the client posts its transaction, read when Apple sends a
    notification that identifies only the subscription.
    """
    from .firebase import admin_firestore

    admin_firestore().collection(APPLE_SUBSCRIPTIONS_COLLECTION).document(
        original_transaction_id
    ).set({"uid": uid, "updated_at": datetime.now(UTC)}, merge=True)


def uid_for_subscription(original_transaction_id: str) -> str | None:
    from .firebase import admin_firestore

    snap = (
        admin_firestore()
        .collection(APPLE_SUBSCRIPTIONS_COLLECTION)
        .document(original_transaction_id)
        .get()
    )
    if not snap.exists:
        return None
    return (snap.to_dict() or {}).get("uid")


def apply_apple_event(
    event_id: str,
    event_type: str,
    uid: str,
    write: dict | None,
    occurred_at: datetime | None,
) -> str:
    """Idempotency claim plus entitlement merge as ONE transaction.

    Returns "processed" | "duplicate" | "stale". Deliberately the same shape and
    the same guarantees as the Dodo path's `_apply_webhook_txn`, including the
    rule that a stale event still writes its claim so a redelivery cannot
    reprocess it. A raise commits nothing, so the route 500s and Apple's
    redelivery genuinely reprocesses.
    """
    from google.cloud import firestore as gcloud_firestore

    from .entitlement import source_may_apply_non_activating
    from .firebase import admin_firestore

    db = admin_firestore()
    claim_ref = db.collection(BILLING_EVENTS_COLLECTION).document(f"apple:{event_id}")
    ent_ref = entitlement_doc_ref(uid, db)
    transaction = db.transaction()

    @gcloud_firestore.transactional
    def _execute(txn) -> str:
        # Firestore transaction rule: every read before any write.
        if claim_ref.get(transaction=txn).exists:
            return "duplicate"
        ent = ent_ref.get(transaction=txn).to_dict() or {}

        stale = False
        if occurred_at is not None:
            last = ent.get("last_billing_event_at")
            if isinstance(last, datetime):
                last_aware = last if last.tzinfo else last.replace(tzinfo=UTC)
                if occurred_at < last_aware:
                    stale = True

        # The mirror of the web path's rival-seller guard. A write that grants
        # access always lands and takes ownership; one that revokes or degrades
        # it must not touch an entitlement the web checkout owns.
        if (
            not stale
            and write is not None
            and write.get("status") != "active"
            and not source_may_apply_non_activating(ent, SOURCE_APPLE)
        ):
            stale = True

        txn.set(claim_ref, {
            "uid": uid,
            "event_type": event_type,
            "processed_at": datetime.now(UTC),
            "stale": stale,
        })
        if stale:
            return "stale"

        if write is not None:
            merged = dict(write)
            if occurred_at is not None:
                merged["last_billing_event_at"] = occurred_at
                merged["last_billing_event_id"] = f"apple:{event_id}"
            txn.set(ent_ref, merged, merge=True)
        return "processed"

    return _execute(transaction)
