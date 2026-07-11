"""One-time day-0 welcome push, fired once per account off first device registration.

Fires exactly once per user regardless of how many devices/token-refreshes call
POST /devices/register, via an atomic Firestore claim (mirrors
``icebreaker_store.plan_and_claim_today``) so a race between two near-simultaneous
registrations (e.g. app launch + a background token refresh) can never double-send.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from google.cloud import firestore as fs

from ...lib.logger import logger
from ..firebase import admin_firestore
from . import orchestrator
from .proposal import SOURCE_WELCOME, NotificationProposal, ProposalKind

FIELD_WELCOME_SENT_AT = "welcome_notification_sent_at"

WELCOME_TITLE = "hey, it's Buddy"
WELCOME_BODY = "I'm here whenever you want to talk. What's been on your mind lately?"


def _user_ref(user_id: str):
    return admin_firestore().collection("users").document(user_id)


def _claim_welcome_slot(user_id: str, now: datetime) -> bool:
    """Atomically claim the one-time welcome slot.

    Returns True iff this call won the claim (and so should send); False if
    already claimed. Fails CLOSED (returns False) on any error — a claim failure
    must never double-send."""

    def _txn() -> bool:
        ref = _user_ref(user_id)
        transaction = admin_firestore().transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> bool:
            snap = ref.get(transaction=txn)
            data = (snap.to_dict() or {}) if snap.exists else {}
            if data.get(FIELD_WELCOME_SENT_AT):
                return False
            txn.set(ref, {FIELD_WELCOME_SENT_AT: now.isoformat()}, merge=True)
            return True

        return _apply(transaction)

    try:
        return _txn()
    except Exception as exc:
        logger.warn("notifications.welcome: claim failed (failing closed)", {
            "user_id": user_id, "error": str(exc),
        })
        return False


async def maybe_send_welcome_notification(user_id: str, *, now: datetime | None = None) -> None:
    """Send the one-time day-0 welcome push, if this account hasn't had one yet.

    Safe to call on every ``/devices/register`` hit (app launch, token refresh) —
    the atomic claim above makes every call after the first a no-op."""
    now = now or datetime.now(UTC)
    won_claim = await asyncio.to_thread(_claim_welcome_slot, user_id, now)
    if not won_claim:
        return

    proposal = NotificationProposal(
        user_id=user_id,
        source=SOURCE_WELCOME,
        kind=ProposalKind.COMMITTED,
        dedup_key=f"welcome:{user_id}",
        title=WELCOME_TITLE,
        body=WELCOME_BODY,
    )
    await orchestrator.submit(proposal)
