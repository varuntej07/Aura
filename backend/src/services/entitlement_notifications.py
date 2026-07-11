"""Trial lifecycle notifications: warn a free-tier user 3 days before their trial
ends, then tell them once it has.

Reads the same ``users/{uid}/entitlement/current`` doc ``entitlement.py`` reads.
Two independent due-queries (collection_group, indexed, cheap when empty; see the
project's Firestore read-discipline rule) instead of looping every user and reading
their entitlement doc individually:

  3-days-left warning: tier == free AND trial_notified_3d == false
                        AND now < trial_end_date <= now + 3d
  trial-ended notice:  tier == free AND trial_notified_expired == false
                        AND trial_end_date <= now

``tier == free`` excludes anyone who already upgraded via a real purchase:
``_verifyAndGrantEntitlement`` (subscription_service.dart) sets ``tier`` to
companion/pro on purchase but never clears ``trial_end_date``, so without this
filter a paying user could still match.

Copy is fixed, not LLM-framed: this is billing-adjacent lifecycle messaging, not
personalized content, so plain templated strings avoid both the extra LLM cost and
any hallucination risk on money-related copy.

Sent via the COMMITTED lane (like SOURCE_DEVICE_LINK) because this is important account
info the user must see, not an engagement-optimized proactive opener, so it must
never be held or arbitrated away.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.cloud.firestore_v1.base_query import FieldFilter

from ..lib.logger import logger
from .firebase import admin_firestore
from .notifications import orchestrator
from .notifications.proposal import SOURCE_TRIAL, NotificationProposal, ProposalKind
from .signal_engine.scoring import is_within_active_hours

COLLECTION = "entitlement"
DOC_ID = "current"

FIELD_TIER = "tier"
FIELD_TRIAL_END_DATE = "trial_end_date"
FIELD_TRIAL_NOTIFIED_3D = "trial_notified_3d"
FIELD_TRIAL_NOTIFIED_EXPIRED = "trial_notified_expired"

TIER_FREE = "free"

WARNING_WINDOW = timedelta(days=3)

_3D_TITLE = "3 days left in your trial"
_3D_BODY = (
    "Your free Buddy trial ends in 3 days. After that you'll keep chatting on the "
    "free plan with some daily limits, or upgrade anytime for full access."
)
_EXPIRED_TITLE = "Your trial just ended"
_EXPIRED_BODY = (
    "You're on the free plan now (25 messages/day). Upgrade anytime to keep "
    "unlimited access to Buddy."
)

TRIAL_NOTICE_CONCURRENCY = 10


@dataclass
class TrialLifecycleTickSummary:
    due_3d: int = 0
    due_expired: int = 0
    sent_3d: int = 0
    sent_expired: int = 0
    skipped_quiet_hours: int = 0


def _query_due_3d(now: datetime) -> list[tuple[str, str]]:
    """Returns [(user_id, doc_path)] due for the 3-days-left warning.

    Fails LOUD then returns [] so a swallowed missing-index 400 can never
    masquerade as "nobody due", the exact silent-zero failure mode CLAUDE.md
    warns about. Requires the entitlement COLLECTION_GROUP index on
    (tier, trial_notified_3d, trial_end_date).
    """
    try:
        db = admin_firestore()
        query = (
            db.collection_group(COLLECTION)
            .where(filter=FieldFilter(FIELD_TIER, "==", TIER_FREE))
            .where(filter=FieldFilter(FIELD_TRIAL_NOTIFIED_3D, "==", False))
            .where(filter=FieldFilter(FIELD_TRIAL_END_DATE, "<=", now + WARNING_WINDOW))
        )
        out: list[tuple[str, str]] = []
        for doc in query.stream():
            data = doc.to_dict() or {}
            trial_end = data.get(FIELD_TRIAL_END_DATE)
            if trial_end is None:
                continue
            trial_end = trial_end if trial_end.tzinfo else trial_end.replace(tzinfo=UTC)
            if trial_end <= now:
                continue  # already past due; the expired query owns this doc
            user_doc = doc.reference.parent.parent
            if user_doc:
                out.append((user_doc.id, doc.reference.path))
        return out
    except Exception as exc:
        logger.error("entitlement_notifications: 3d-warning query failed", {"error": str(exc)})
        return []


def _query_due_expired(now: datetime) -> list[tuple[str, str]]:
    """Returns [(user_id, doc_path)] whose trial has ended. See ``_query_due_3d``
    for the fail-loud rationale. Requires the entitlement COLLECTION_GROUP index on
    (tier, trial_notified_expired, trial_end_date)."""
    try:
        db = admin_firestore()
        query = (
            db.collection_group(COLLECTION)
            .where(filter=FieldFilter(FIELD_TIER, "==", TIER_FREE))
            .where(filter=FieldFilter(FIELD_TRIAL_NOTIFIED_EXPIRED, "==", False))
            .where(filter=FieldFilter(FIELD_TRIAL_END_DATE, "<=", now))
        )
        out: list[tuple[str, str]] = []
        for doc in query.stream():
            user_doc = doc.reference.parent.parent
            if user_doc:
                out.append((user_doc.id, doc.reference.path))
        return out
    except Exception as exc:
        logger.error("entitlement_notifications: expired query failed", {"error": str(exc)})
        return []


async def _read_timezone(user_id: str) -> str:
    def _read() -> str:
        snap = admin_firestore().collection("users").document(user_id).get()
        data = (snap.to_dict() or {}) if snap.exists else {}
        return str(data.get("timezone", "UTC") or "UTC")

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("entitlement_notifications: timezone read failed, defaulting UTC", {
            "user_id": user_id, "error": str(exc),
        })
        return "UTC"


def _local_now(timezone_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(timezone_name))
    except (ZoneInfoNotFoundError, Exception):
        return datetime.now(UTC)


async def _mark_notified(doc_path: str, field: str) -> None:
    def _write() -> None:
        admin_firestore().document(doc_path).update({field: True})

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.error("entitlement_notifications: failed to mark notified", {
            "doc_path": doc_path, "field": field, "error": str(exc),
        })


async def run_trial_lifecycle_tick() -> TrialLifecycleTickSummary:
    """Public entrypoint, called from the scheduler tick on its minute % 15 == 7 gate."""
    summary = TrialLifecycleTickSummary()
    now = datetime.now(UTC)

    due_3d, due_expired = await asyncio.gather(
        asyncio.to_thread(_query_due_3d, now),
        asyncio.to_thread(_query_due_expired, now),
    )
    summary.due_3d = len(due_3d)
    summary.due_expired = len(due_expired)

    if not due_3d and not due_expired:
        return summary

    semaphore = asyncio.Semaphore(TRIAL_NOTICE_CONCURRENCY)

    async def _send_3d(user_id: str, doc_path: str) -> None:
        async with semaphore:
            try:
                if not await _within_active_hours(user_id):
                    summary.skipped_quiet_hours += 1
                    return
                await orchestrator.submit(
                    NotificationProposal(
                        user_id=user_id,
                        source=SOURCE_TRIAL,
                        kind=ProposalKind.COMMITTED,
                        dedup_key=f"trial_3d_warning_{user_id}",
                        title=_3D_TITLE,
                        body=_3D_BODY,
                        data={"deep_link": "paywall", "trial_variant": "3d_warning"},
                        collapse_key=f"trial_3d_warning_{user_id}",
                    )
                )
                await _mark_notified(doc_path, FIELD_TRIAL_NOTIFIED_3D)
                summary.sent_3d += 1
            except Exception as exc:
                logger.exception("entitlement_notifications: 3d-warning send failed", {
                    "user_id": user_id, "error": str(exc),
                })

    async def _send_expired(user_id: str, doc_path: str) -> None:
        async with semaphore:
            try:
                if not await _within_active_hours(user_id):
                    summary.skipped_quiet_hours += 1
                    return
                await orchestrator.submit(
                    NotificationProposal(
                        user_id=user_id,
                        source=SOURCE_TRIAL,
                        kind=ProposalKind.COMMITTED,
                        dedup_key=f"trial_expired_{user_id}",
                        title=_EXPIRED_TITLE,
                        body=_EXPIRED_BODY,
                        data={"deep_link": "paywall", "trial_variant": "expired"},
                        collapse_key=f"trial_expired_{user_id}",
                    )
                )
                await _mark_notified(doc_path, FIELD_TRIAL_NOTIFIED_EXPIRED)
                summary.sent_expired += 1
            except Exception as exc:
                logger.exception("entitlement_notifications: expired-notice send failed", {
                    "user_id": user_id, "error": str(exc),
                })

    async def _within_active_hours(user_id: str) -> bool:
        timezone = await _read_timezone(user_id)
        return is_within_active_hours(_local_now(timezone).hour)

    await asyncio.gather(
        *[_send_3d(uid, path) for uid, path in due_3d],
        *[_send_expired(uid, path) for uid, path in due_expired],
    )

    logger.info("entitlement_notifications: tick complete", {
        "due_3d": summary.due_3d,
        "due_expired": summary.due_expired,
        "sent_3d": summary.sent_3d,
        "sent_expired": summary.sent_expired,
        "skipped_quiet_hours": summary.skipped_quiet_hours,
    })
    return summary
