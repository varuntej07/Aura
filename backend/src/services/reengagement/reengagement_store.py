"""Firestore state for the dormancy win-back.

One marker doc per user (``users/{uid}/reengagement/state``) so a win-back fires at
most once per dormancy episode — not every hourly tick while the user sits in the
5-6-day-idle cohort, and not again until they've returned and lapsed afresh.

Field names live here (one source of truth, CLAUDE.md data-layer rule).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from google.cloud import firestore as fs

from ...lib.logger import logger
from ..firebase import admin_firestore

REENGAGE_SUBCOLLECTION = "reengagement"
REENGAGE_DOC_ID = "state"
FIELD_LAST_REENGAGED_AT = "last_reengaged_at"

# Never re-engage the same user twice within this window. Guards both against firing
# every hour while they sit in the idle cohort AND against back-to-back episodes; it is
# longer than the cohort window (5-6 days) so one lapse earns at most one win-back.
REENGAGE_COOLDOWN = timedelta(days=6)


@dataclass
class ReengageTargeting:
    consent_granted: bool = False
    timezone: str = "UTC"
    # The user's single top interest subject, ONLY when Aura consent is granted (reading
    # it is behavioural profiling). Empty otherwise → the opener stays warm but generic.
    top_interest: str = ""


def _ref(user_id: str) -> fs.DocumentReference:
    return (
        admin_firestore()
        .collection("users")
        .document(user_id)
        .collection(REENGAGE_SUBCOLLECTION)
        .document(REENGAGE_DOC_ID)
    )


async def claim_reengagement(user_id: str, *, now: datetime | None = None) -> bool:
    """Atomically claim a win-back slot. Returns True if claimed (the caller may send),
    False if a recent win-back is still within the cooldown. Fails CLOSED (False) on any
    error so a glitch can never spam a dormant user with repeated win-backs."""
    now = now or datetime.now(UTC)

    def _claim() -> bool:
        ref = _ref(user_id)
        transaction = admin_firestore().transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> bool:
            snap = ref.get(transaction=txn)
            data = (snap.to_dict() or {}) if snap.exists else {}
            last = data.get(FIELD_LAST_REENGAGED_AT)
            if isinstance(last, datetime):
                last_aware = last if last.tzinfo else last.replace(tzinfo=UTC)
                if now - last_aware < REENGAGE_COOLDOWN:
                    return False
            txn.set(ref, {FIELD_LAST_REENGAGED_AT: now}, merge=True)
            return True

        return _apply(transaction)

    try:
        return await asyncio.to_thread(_claim)
    except Exception as exc:
        logger.warn("reengagement_store: claim failed (fail-closed)", {
            "user_id": user_id, "error": str(exc),
        })
        return False


async def read_targeting(user_id: str) -> ReengageTargeting:
    """Timezone + consent (one user-doc read) and, only when consent is granted, the
    user's top interest for a personalised line. Consent fails CLOSED; a read error
    yields a consent-denied default so personalisation never runs on an unconfirmed read.
    """
    def _read() -> ReengageTargeting:
        user_snap = admin_firestore().collection("users").document(user_id).get()
        user = (user_snap.to_dict() or {}) if user_snap.exists else {}
        consent = user.get("aura_consent_granted", False) is True
        timezone = str(user.get("timezone", "UTC") or "UTC")
        top_interest = ""
        if consent:
            aura_snap = admin_firestore().collection("UserAura").document(user_id).get()
            if aura_snap.exists:
                from ..user_aura_schema import top_interest_subjects

                subjects = top_interest_subjects(aura_snap.to_dict() or {}, k=1)
                top_interest = subjects[0] if subjects else ""
        return ReengageTargeting(
            consent_granted=consent, timezone=timezone, top_interest=top_interest,
        )

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("reengagement_store: targeting read failed", {
            "user_id": user_id, "error": str(exc),
        })
        return ReengageTargeting()
