"""Firestore access layer for the Icebreaker engine.

All Firebase Admin SDK calls are blocking, so every public function is an async
wrapper dispatching the sync work via ``asyncio.to_thread`` (matching
``threads.thread_store`` / ``signal_engine.feature_store``). Reads degrade to a
safe default on failure so a Firestore blip can never crash a scheduler tick.

The heart of this module is ``plan_and_claim_today``: a single Firestore
transaction that (1) lazily rolls the week's icebreaker days if the stored week is
stale and (2) atomically claims today's single send by stamping
``last_sent_date``. Claiming inside the transaction — BEFORE the LLM call and the
push — is what makes "at most one icebreaker per user per day" hold even when two
scheduler ticks run concurrently: the loser reads today already stamped and
stands down.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from google.cloud import firestore as fs

from ...lib.logger import logger
from ..firebase import admin_firestore
from . import fields as f


@dataclass
class UserTargeting:
    """The small slice of ``users/{uid}`` the engine needs, fetched in one read."""

    consent_granted: bool = False
    timezone: str = "UTC"
    locale: str = ""
    language: str = "English"
    gender: str | None = None


@dataclass
class ClaimResult:
    """Outcome of ``plan_and_claim_today``."""

    claimed: bool
    reason: str  # "claimed" | "not_scheduled_today" | "already_sent_today"
    recent_opener_topics: list[str] = field(default_factory=list)


def evaluate_claim(
    data: dict,
    *,
    local_date: str,
    week_start_date: str,
    rolled_dates: list[str],
) -> tuple[dict, ClaimResult]:
    """Pure claim decision: given the current state doc, what to write and whether
    today's slot is claimed. No I/O — the transaction below applies the update.

    This is the idempotency core, unit-tested directly: feed back the doc with the
    returned update applied and a SECOND call returns ``already_sent_today`` — which
    is exactly what a second overlapping tick sees, so it stands down.
    """
    scheduled_dates = list(data.get(f.FIELD_SCHEDULED_DATES, []) or [])
    stored_week = data.get(f.FIELD_WEEK_START_DATE)
    recent_topics = list(data.get(f.FIELD_RECENT_OPENER_TOPICS, []) or [])

    update: dict = {}
    # Lazy weekly roll: only when the stored week is stale (or absent).
    if stored_week != week_start_date:
        scheduled_dates = list(rolled_dates)
        update[f.FIELD_WEEK_START_DATE] = week_start_date
        update[f.FIELD_SCHEDULED_DATES] = scheduled_dates

    if local_date not in scheduled_dates:
        return update, ClaimResult(False, "not_scheduled_today", recent_topics)

    if data.get(f.FIELD_LAST_SENT_DATE) == local_date:
        return update, ClaimResult(False, "already_sent_today", recent_topics)

    # Claim: stamping last_sent_date is the lock that makes one-per-day idempotent.
    update[f.FIELD_LAST_SENT_DATE] = local_date
    return update, ClaimResult(True, "claimed", recent_topics)


def _state_ref(user_id: str) -> fs.DocumentReference:
    return (
        admin_firestore()
        .collection("users")
        .document(user_id)
        .collection(f.ICEBREAKER_STATE_SUBCOLLECTION)
        .document(f.ICEBREAKER_STATE_DOC_ID)
    )


async def read_user_targeting(user_id: str) -> UserTargeting:
    """Read consent + timezone + locale + language + gender in a single get.

    Returns a consent-denied default on any error, so a read failure fails CLOSED
    for the icebreaker (no send) — the opposite of the budget's fail-open, because
    sending without a confirmed consent read would be the wrong way to fail.
    """

    def _fetch() -> UserTargeting:
        snap = admin_firestore().collection("users").document(user_id).get()
        if not snap.exists:
            return UserTargeting()
        data = snap.to_dict() or {}
        return UserTargeting(
            consent_granted=data.get("aura_consent_granted", False) is True,
            timezone=str(data.get("timezone", "UTC") or "UTC"),
            locale=str(data.get("locale", "") or ""),
            language=str(data.get("language", "English") or "English"),
            gender=data.get("gender"),
        )

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("icebreaker.store: read_user_targeting failed (failing closed)", {
            "user_id": user_id,
            "error": str(exc),
        })
        return UserTargeting()


async def plan_and_claim_today(
    user_id: str,
    *,
    local_date: str,
    week_start_date: str,
    rolled_dates: list[str],
) -> ClaimResult:
    """Atomically roll the week if needed and claim today's single icebreaker slot.

    ``rolled_dates`` is the deterministic roll for ``week_start_date`` computed by
    the pure scheduler logic; it is only persisted when the stored week differs, so
    a re-roll is idempotent. On a successful claim ``last_sent_date`` is stamped to
    ``local_date`` and the stored recent opener topics are returned so the planner
    can avoid repeats.

    Fails CLOSED (``claimed=False``) on any error — a store failure must never
    double-send.
    """

    def _txn() -> ClaimResult:
        ref = _state_ref(user_id)
        transaction = admin_firestore().transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> ClaimResult:
            snap = ref.get(transaction=txn)
            data = (snap.to_dict() or {}) if snap.exists else {}
            update, result = evaluate_claim(
                data,
                local_date=local_date,
                week_start_date=week_start_date,
                rolled_dates=rolled_dates,
            )
            if update:
                txn.set(ref, update, merge=True)
            return result

        return _apply(transaction)

    try:
        return await asyncio.to_thread(_txn)
    except Exception as exc:
        logger.warn("icebreaker.store: plan_and_claim_today failed (failing closed)", {
            "user_id": user_id,
            "local_date": local_date,
            "error": str(exc),
        })
        return ClaimResult(False, "error")


@dataclass
class PendingOpenerData:
    """Opener content persisted to the state doc after generation, so a crash-then-
    re-drain can recover it without a second LLM call."""
    title: str
    body: str
    opening_chat_message: str
    topic: str
    reason: str
    notification_id: str


async def store_pending_opener(
    user_id: str,
    *,
    local_date: str,
    title: str,
    body: str,
    opening_chat_message: str,
    topic: str,
    reason: str,
    notification_id: str,
) -> None:
    """Write opener content to the state doc after generation, before the push.
    Best-effort: a failure here only disables crash-recovery for this opener;
    it never affects the primary send path. The funnel's dedup_key prevents
    double-send even if the recovered opener fires after the original delivered."""
    def _write() -> None:
        _state_ref(user_id).set({
            f.FIELD_PENDING_OPENER_DATE: local_date,
            f.FIELD_PENDING_OPENER_TITLE: title,
            f.FIELD_PENDING_OPENER_BODY: body,
            f.FIELD_PENDING_OPENER_MSG: opening_chat_message,
            f.FIELD_PENDING_OPENER_TOPIC: topic,
            f.FIELD_PENDING_OPENER_REASON: reason,
            f.FIELD_PENDING_OPENER_NOTIFICATION_ID: notification_id,
        }, merge=True)

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.warn("icebreaker.store: store_pending_opener failed (crash-recovery unavailable)", {
            "user_id": user_id, "local_date": local_date, "error": str(exc),
        })


async def try_recover_pending_opener(
    user_id: str, local_date: str
) -> PendingOpenerData | None:
    """Return a previously-generated opener stored for today, or None if no
    recovery is available (different date, fields missing, or read error).
    Called when the day slot is already claimed — the normal case (already sent)
    and the crash-recovery case (claimed but killed before mark_consumed) look
    identical from the claim alone; this distinguishes them."""
    def _read() -> PendingOpenerData | None:
        snap = _state_ref(user_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        if data.get(f.FIELD_PENDING_OPENER_DATE) != local_date:
            return None
        title = str(data.get(f.FIELD_PENDING_OPENER_TITLE) or "")
        body = str(data.get(f.FIELD_PENDING_OPENER_BODY) or "")
        nid = str(data.get(f.FIELD_PENDING_OPENER_NOTIFICATION_ID) or "")
        if not title or not body or not nid:
            return None
        return PendingOpenerData(
            title=title,
            body=body,
            opening_chat_message=str(data.get(f.FIELD_PENDING_OPENER_MSG) or ""),
            topic=str(data.get(f.FIELD_PENDING_OPENER_TOPIC) or ""),
            reason=str(data.get(f.FIELD_PENDING_OPENER_REASON) or ""),
            notification_id=nid,
        )

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("icebreaker.store: try_recover_pending_opener failed", {
            "user_id": user_id, "local_date": local_date, "error": str(exc),
        })
        return None


async def record_sent_opener(
    user_id: str,
    *,
    topic: str,
    sent_at: datetime,
) -> None:
    """Append a sent opener's topic to the rolling memory and bump counters.

    Read-modify-write in a transaction so the FIFO cap is applied consistently.
    Best-effort: a failure here only means the topic might be eligible to repeat,
    never a double-send (that is already prevented by the claim).
    """
    clean = (topic or "").strip()

    def _txn() -> None:
        ref = _state_ref(user_id)
        transaction = admin_firestore().transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> None:
            snap = ref.get(transaction=txn)
            data = (snap.to_dict() or {}) if snap.exists else {}
            topics = list(data.get(f.FIELD_RECENT_OPENER_TOPICS, []) or [])
            if clean:
                topics.append(clean)
                # FIFO cap so the document can never approach the 1 MiB limit.
                if len(topics) > f.MAX_RECENT_OPENER_TOPICS:
                    topics = topics[-f.MAX_RECENT_OPENER_TOPICS:]
            total = int(data.get(f.FIELD_TOTAL_SENT, 0) or 0) + 1
            txn.set(ref, {
                f.FIELD_RECENT_OPENER_TOPICS: topics,
                f.FIELD_TOTAL_SENT: total,
                f.FIELD_LAST_SENT_AT: sent_at,
            }, merge=True)

        _apply(transaction)

    try:
        await asyncio.to_thread(_txn)
    except Exception as exc:
        logger.warn("icebreaker.store: record_sent_opener failed", {
            "user_id": user_id,
            "error": str(exc),
        })
