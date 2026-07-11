"""
Entitlement checks for metered features.

Free tier: 25 chat messages per UTC calendar day.
Free-tier voice: 600s (10 min) of voice per UTC calendar day (enforced; Buddy warns
at ~60s left, then winds the call down at the cap; see agent/voice/free_tier_limit.py).
Trial users (free tier within trial window) get pro access.
Paid users are never gated.

A Firestore failure raises EntitlementUnavailableError instead of silently handing
out "pro" (an outage must never grant the paid product now that money exists).
Callers choose their own degradation: the /entitlement route answers 503 so clients
fall back to their cache, text surfaces treat it as "free" (their usage metering
fails open, so nothing hard-blocks), voice resolves "unknown" and never enforces.

All Firestore reads run in asyncio.to_thread() so the event loop stays unblocked.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from ..lib.logger import logger

FREE_TIER_DAILY_CHAT_LIMIT = 25
FREE_TIER_DAILY_WEB_SURF_LIMIT = 10
FREE_TIER_DAILY_VOICE_SECONDS = 600  # 10 minutes of voice per UTC day (enforced)
FREE_TIER_DAILY_OUTBOUND_DRAFT_LIMIT = 5  # new screen drafts only; refines are never metered

TRIAL_DURATION_DAYS = 45

# Tiers that mean money changed hands. 'starter' only appears in legacy docs.
PAID_TIERS = ("companion", "pro", "starter")

# An auto-renewing subscription whose expires_at has passed is usually a missed
# or delayed renewal webhook, not a real lapse; give the webhook this long to
# land before treating the stored 'active' as expired.
RENEWAL_GRACE = timedelta(days=3)


class EntitlementUnavailableError(Exception):
    """Raised when the entitlement doc cannot be read (Firestore failure).

    Deliberately NOT the same as a missing doc: a missing doc means a brand-new
    account (still effectively in trial), while this means "we don't know", and
    the caller must degrade explicitly instead of inheriting a free pro tier.
    """


def _as_aware(value) -> datetime | None:
    """Firestore datetimes coerced to UTC-aware; anything else -> None."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def normalize_status(data: dict, now: datetime | None = None) -> str:
    """The doc's status corrected against its own authoritative dates.

    Webhooks only ever push status transitions; nothing rewrites the doc when a
    trial or a cancelled period simply runs out. This pure normalizer is what
    responses and enforcement read instead of the raw stored field:
      - 'trialing' past trial_end_date -> 'expired'.
      - 'active'/'gracePeriod' with cancel_at_period_end past expires_at ->
        'expired' (a cancelled sub ends exactly at period end).
      - 'active'/'gracePeriod' auto-renewing past expires_at + RENEWAL_GRACE ->
        'expired' (renewal webhooks normally move expires_at forward well
        before the grace runs out).
      - No stored status (legacy client-stamped docs): 'trialing' while
        trial_end_date is in the future, else 'expired'.
    Never written back to Firestore.
    """
    current = now or datetime.now(UTC)
    stored = data.get("status")

    if not (isinstance(stored, str) and stored):
        trial_end = _as_aware(data.get("trial_end_date"))
        return "trialing" if trial_end is not None and current < trial_end else "expired"

    if stored == "trialing":
        trial_end = _as_aware(data.get("trial_end_date"))
        if trial_end is not None and current >= trial_end:
            return "expired"
        return stored

    if stored in ("active", "gracePeriod"):
        expires_at = _as_aware(data.get("expires_at"))
        if expires_at is not None:
            if data.get("cancel_at_period_end") and current >= expires_at:
                return "expired"
            if not data.get("cancel_at_period_end") and current >= expires_at + RENEWAL_GRACE:
                return "expired"
        return stored

    return stored


def has_active_paid_subscription(data: dict) -> bool:
    """Whether this account already has a live paid subscription (including a
    cancelled-but-not-yet-expired one). Pure; the checkout handler's duplicate
    guard."""
    if data.get("tier") not in PAID_TIERS:
        return False
    return normalize_status(data) in ("active", "gracePeriod")


def resolve_effective_tier(data: dict) -> str:
    """Pure tier resolution for an already-fetched entitlement doc.

    Returns 'free', 'companion', or 'pro' ('starter' may still appear in legacy
    docs and passes through). An empty doc resolves 'pro' permissively, but
    that branch is only reachable in the sub-second window of a racing
    ensure_entitlement_doc create (create() + re-read converge on one doc);
    it exists so that race can never hard-gate a real user, never as a durable
    state. A free-tier user still within their trial window resolves 'pro'
    (reverse trial). A doc whose date-normalized status is 'expired' resolves
    'free' regardless of its tier field, so an out-of-order or missed tier
    write can never leave paid access dangling.
    """
    if not data:
        return "pro"

    if normalize_status(data) == "expired":
        return "free"

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


async def fetch_entitlement_doc(uid: str) -> dict:
    """Reads users/{uid}/entitlement/current, {} when the doc does not exist.

    Raises EntitlementUnavailableError on a Firestore failure; never fails open.
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
            logger.warn("entitlement: Firestore read failed", {
                "user_id": uid,
                "error": str(exc),
            })
            raise EntitlementUnavailableError(str(exc)) from exc

    return await asyncio.to_thread(_fetch)


def _stamp_trial_doc(uid: str) -> dict:
    """Create the entitlement doc for a first-contact account, race-safe.

    Runs in a worker thread. create() is atomic create-if-absent; on
    AlreadyExists (a concurrent first call, or a legacy client write that won
    the race) the existing doc is re-read and served untouched.
    """
    from google.api_core.exceptions import AlreadyExists

    from ..services.firebase import admin_firestore

    now = datetime.now(UTC)
    doc = {
        "tier": "free",
        "status": "trialing",
        "trial_start_date": now,
        "trial_end_date": now + timedelta(days=TRIAL_DURATION_DAYS),
        "trial_notified_3d": False,
        "trial_notified_expired": False,
        "updated_at": now,
    }
    ref = (
        admin_firestore()
        .collection("users")
        .document(uid)
        .collection("entitlement")
        .document("current")
    )
    try:
        ref.create(doc)
        logger.info("entitlement: trial stamped on first contact", {
            "user_id": uid,
            "trial_end_date": doc["trial_end_date"].isoformat(),
        })
        return doc
    except AlreadyExists:
        return ref.get().to_dict() or doc


async def ensure_entitlement_doc(uid: str) -> dict:
    """users/{uid}/entitlement/current, atomically stamping the 45-day trial
    when the doc does not exist yet.

    This is the shared get-or-create every read path goes through, so an
    account's first contact with ANY metered surface starts its trial; a client
    that never calls GET /entitlement can no longer ride the permissive
    missing-doc default forever. Raises EntitlementUnavailableError on a
    Firestore failure; never fails open.
    """
    data = await fetch_entitlement_doc(uid)
    if data:
        return data
    try:
        return await asyncio.to_thread(_stamp_trial_doc, uid)
    except Exception as exc:
        logger.warn("entitlement: trial stamp failed", {
            "user_id": uid,
            "error": str(exc),
        })
        raise EntitlementUnavailableError(str(exc)) from exc


async def get_user_effective_tier(uid: str) -> str:
    """
    Returns 'free', 'companion'/'starter', or 'pro'.

    A free-tier user still within their trial window is returned as 'pro'
    so they are never gated during the reverse-trial period.

    A missing doc is stamped with the 45-day trial on the spot (which also
    resolves 'pro', via the trial window rather than a permissive default).

    Raises EntitlementUnavailableError on a Firestore failure (never fails
    open to 'pro'; see the module docstring for how each caller degrades).
    """
    data = await ensure_entitlement_doc(uid)
    return resolve_effective_tier(data)


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

    Falls back to (True, 0) if Firestore is unavailable; infra failures should not
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


async def check_and_increment_daily_outbound_draft_usage(uid: str) -> tuple[bool, int]:
    """
    Atomically checks then increments the UTC-day outbound-draft counter for a
    free-tier user. Charged once per NEW screen draft; refines of an existing
    draft never reach this counter.

    Returns (allowed, count_after_this_call).
    Counter resets each UTC calendar day. Stored at users/{uid}/usage/daily_outbound_draft.

    Falls back to (True, 0) if Firestore is unavailable: infra failures should not
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
            .document("daily_outbound_draft")
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
            if count >= FREE_TIER_DAILY_OUTBOUND_DRAFT_LIMIT:
                return False, count

            new_count = count + 1
            txn.update(usage_ref, {"count": new_count})
            return True, new_count

        return _execute(transaction)

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:
        logger.warn("entitlement: outbound_draft usage increment failed, allowing request", {
            "user_id": uid,
            "error": str(exc),
        })
        return True, 0


async def get_remaining_free_voice_seconds(uid: str) -> int | None:
    """
    Returns the free-tier voice seconds remaining for this UTC day, or None on any failure.

    Reads users/{uid}/usage/daily_voice {date, seconds}. If the stored date is not today the
    budget has rolled over, so the full FREE_TIER_DAILY_VOICE_SECONDS is available.

    Returns None (not 0) on a Firestore failure so the caller SKIPS the nudge rather than
    falsely warning; a read error must never fabricate "you're almost out of free voice time".
    """
    from ..services.firebase import admin_firestore

    today = datetime.now(UTC).strftime("%Y-%m-%d")

    def _fetch() -> int | None:
        try:
            db = admin_firestore()
            snap = (
                db.collection("users")
                .document(uid)
                .collection("usage")
                .document("daily_voice")
                .get()
            )
            data = snap.to_dict() or {}
            if data.get("date") != today:
                return FREE_TIER_DAILY_VOICE_SECONDS
            used = int(data.get("seconds", 0))
            return max(0, FREE_TIER_DAILY_VOICE_SECONDS - used)
        except Exception as exc:
            logger.warn("entitlement: voice budget read failed, skipping nudge", {
                "user_id": uid,
                "error": str(exc),
            })
            return None

    return await asyncio.to_thread(_fetch)


async def add_free_voice_seconds(uid: str, seconds: int) -> None:
    """
    Transactionally add elapsed voice seconds to the free-tier UTC-day counter.

    Stored at users/{uid}/usage/daily_voice {date, seconds}, resetting when the day rolls over
    (same shape as the daily_chat / daily_web_surf counters). Fire-and-forget at session end;
    fail-open (log and swallow) so a write failure never affects the call that just finished.
    """
    if seconds <= 0:
        return

    from google.cloud import firestore as gcloud_firestore

    from ..services.firebase import admin_firestore

    today = datetime.now(UTC).strftime("%Y-%m-%d")

    def _run() -> None:
        db = admin_firestore()
        usage_ref = (
            db.collection("users")
            .document(uid)
            .collection("usage")
            .document("daily_voice")
        )
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> None:
            snap = usage_ref.get(transaction=txn)
            data = snap.to_dict() or {}
            if data.get("date") != today:
                txn.set(usage_ref, {"date": today, "seconds": seconds})
            else:
                txn.update(usage_ref, {"seconds": int(data.get("seconds", 0)) + seconds})

        _execute(transaction)

    try:
        await asyncio.to_thread(_run)
    except Exception as exc:
        logger.warn("entitlement: voice seconds write failed", {
            "user_id": uid,
            "error": str(exc),
        })
