"""
Unified notification ledger — one durable record per notification, across every
send path (signal engine, reminders, calendar meeting reminders, threads,
icebreaker, engagement, briefing, tracker).

Why this exists:
  - Before this, only the signal engine persisted anything (its learning-loop
    ``outcomes`` doc under signal_store/state); every other path sent to FCM and
    forgot. There was no place to answer "what did we send this user, when, why,
    from what source, with what link, and did they tap it".
  - Every path already funnels through
    ``notification_service.send_notification``, so the ledger is written there
    ONCE — a new decider added later gets logged for free.

Storage:
  ``users/{uid}/notifications/{notification_id}`` — a flat, per-user,
  easily-browsable subcollection. Two layers per row:
    core      — present on every notification (type, copy, url, delivery,
                outcome, tap time).
    decision  — only the LLM-framed proactive paths fill it (the math score,
                its components, the framer's relevance reason + prompt version).
                This is the learning substrate for tuning ``scoring.py`` weights
                and the framer prompt against real tap outcomes; deterministic
                paths (reminders / calendar) leave it null.

Discipline:
  - Field names live HERE as constants (one source of truth) so a rename can't
    silently fork the writer from a reader (CLAUDE.md data-layer rule). The
    round-trip is guarded by ``tests/test_notification_ledger.py``.
  - All writes are fire-and-forget and swallow their own errors: a logging write
    must NEVER break or delay a real notification send.
  - Flat, typed top-level fields (not a nested payload dump) so the collection is
    BigQuery-export-ready when offline recommender training outgrows Firestore.
  - ``expires_at`` carries a native Firestore TTL (configure the policy once,
    same mechanism ``content_candidates`` uses) so rows self-purge.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..lib.logger import logger
from .firebase import admin_firestore

# How long a notification row lives. A native Firestore TTL policy on
# ``expires_at`` deletes them after this window. 90 days keeps a full quarter of
# tap history for tuning without unbounded growth.
LEDGER_RETENTION_DAYS = 90

# ---- Field-name contract (single source of truth) -------------------------
FIELD_NOTIFICATION_ID = "notification_id"
FIELD_TYPE = "type"
FIELD_ORIGIN = "origin"
FIELD_TITLE = "title"
FIELD_BODY = "body"
FIELD_URL = "url"
FIELD_CONTENT_ID = "content_id"
FIELD_SOURCE = "source"
FIELD_CATEGORY = "category"
FIELD_CONTENT_KIND = "content_kind"
# Stable content identity, written by the orchestrator so a later send carrying
# the same key (the same story from a second decider, or a re-proposed proactive
# item) can be deduped against recent history. Empty on legacy/direct sends.
FIELD_DEDUP_KEY = "dedup_key"
FIELD_SENT_AT = "sent_at"
FIELD_STATUS = "status"
FIELD_DELIVERY = "delivery"
FIELD_CHANNELS = "channels"
FIELD_OUTCOME = "outcome"
FIELD_OUTCOME_AT = "outcome_at"
FIELD_TAPPED_AT = "tapped_at"
FIELD_TIME_TO_TAP_SECONDS = "time_to_tap_seconds"
FIELD_LED_TO_SESSION = "led_to_session"
FIELD_LED_TO_REPLY = "led_to_reply"
FIELD_DECISION = "decision"
FIELD_EXPIRES_AT = "expires_at"

# Outcome lifecycle values.
OUTCOME_PENDING = "pending"
OUTCOME_OPENED = "opened"
OUTCOME_DISMISSED = "dismissed"
OUTCOME_TIMEOUT = "timeout"

# Delivery status values.
STATUS_SENT = "sent"
STATUS_FAILED = "failed"


@dataclass
class NotificationDecision:
    """Optional learning-substrate metadata for LLM-framed proactive sends.

    Reminders / calendar leave this null — they have no recommender or framer to
    improve. The signal engine fills it so ``scoring.py`` weights and the framer
    prompt can later be tuned against real tap outcomes instead of guesses.

    ``components`` is stored as-is (the raw ``scoring.py`` term map: cosine, slot,
    freshness, fatigue, diversity, region, salience) so a new scoring term flows
    through without a schema change here.
    """

    score: float | None = None
    components: dict[str, float] = field(default_factory=dict)
    gate_a_active: bool | None = None
    matched_interest_slug: str = ""
    relevance_reason: str = ""
    framer_model: str = ""
    framer_prompt_version: str = ""
    lane: str = ""
    sends_today_before: int | None = None
    local_hour: int | None = None
    day_of_week: int | None = None


def _decision_to_doc(decision: NotificationDecision) -> dict[str, Any]:
    return {
        "score": decision.score,
        "components": decision.components,
        "gate_a_active": decision.gate_a_active,
        "matched_interest_slug": decision.matched_interest_slug,
        "relevance_reason": decision.relevance_reason,
        "framer_model": decision.framer_model,
        "framer_prompt_version": decision.framer_prompt_version,
        "lane": decision.lane,
        "sends_today_before": decision.sends_today_before,
        "local_hour": decision.local_hour,
        "day_of_week": decision.day_of_week,
    }


def _notification_ref(user_id: str, notification_id: str):
    return (
        admin_firestore()
        .collection("users")
        .document(user_id)
        .collection("notifications")
        .document(notification_id)
    )


async def record_send(
    user_id: str,
    *,
    notification_id: str,
    notification_type: str,
    origin: str,
    title: str,
    body: str,
    url: str = "",
    content_id: str = "",
    source: str = "",
    category: str = "",
    content_kind: str = "",
    dedup_key: str = "",
    delivered: bool,
    tokens_targeted: int,
    success_count: int,
    failure_count: int,
    channel_results: dict[str, dict[str, Any]] | None = None,
    decision: NotificationDecision | None = None,
) -> None:
    """Write the per-notification ledger row at send time.

    Called once from the shared ``send_notification`` choke point, so every path
    is covered. Outcome starts ``pending`` and is later flipped by ``record_tap``
    / ``record_dismiss`` (or the signal engine's 6h timeout sweep).
    """
    now = datetime.now(UTC)
    channels = channel_results or {
        "mobile": {
            "status": STATUS_SENT if delivered else STATUS_FAILED,
            "delivered": delivered,
            "tokens_targeted": tokens_targeted,
            "success_count": success_count,
            "failure_count": failure_count,
        }
    }
    doc: dict[str, Any] = {
        FIELD_NOTIFICATION_ID: notification_id,
        FIELD_TYPE: notification_type,
        FIELD_ORIGIN: origin or notification_type,
        FIELD_TITLE: title,
        FIELD_BODY: body,
        FIELD_URL: url,
        FIELD_CONTENT_ID: content_id,
        FIELD_SOURCE: source,
        FIELD_CATEGORY: category,
        FIELD_CONTENT_KIND: content_kind,
        FIELD_DEDUP_KEY: dedup_key,
        FIELD_SENT_AT: now,
        FIELD_STATUS: STATUS_SENT if delivered else STATUS_FAILED,
        FIELD_DELIVERY: {
            "tokens_targeted": tokens_targeted,
            "success_count": success_count,
            "failure_count": failure_count,
            "delivered": delivered,
            FIELD_CHANNELS: channels,
        },
        FIELD_OUTCOME: OUTCOME_PENDING,
        FIELD_OUTCOME_AT: None,
        FIELD_TAPPED_AT: None,
        FIELD_TIME_TO_TAP_SECONDS: None,
        FIELD_LED_TO_SESSION: False,
        # led_to_reply is the deepest "obsessed" signal (the user actually replied
        # to Buddy after the tap). Column exists now for a stable BQ schema; it is
        # flipped by the Phase-2 client reply report, not written here.
        FIELD_LED_TO_REPLY: False,
        FIELD_DECISION: _decision_to_doc(decision) if decision else None,
        FIELD_EXPIRES_AT: now + timedelta(days=LEDGER_RETENTION_DAYS),
    }

    def _put() -> None:
        _notification_ref(user_id, notification_id).set(doc)

    try:
        await asyncio.to_thread(_put)
    except Exception as exc:
        logger.warn("notification_ledger.record_send failed", {
            "user_id": user_id,
            "notification_id": notification_id,
            "type": notification_type,
            "error": str(exc),
        })


def delivery_channels(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return per-channel delivery data for both current and legacy rows."""
    delivery = doc.get(FIELD_DELIVERY)
    if not isinstance(delivery, dict):
        return {}
    channels = delivery.get(FIELD_CHANNELS)
    if isinstance(channels, dict):
        return {
            str(channel): value
            for channel, value in channels.items()
            if isinstance(value, dict)
        }
    return {"mobile": dict(delivery)}


async def recent_dedup_keys(user_id: str, *, since: datetime) -> set[str]:
    """The set of non-empty ``dedup_key``s sent to a user since ``since``.

    The orchestrator's cross-agent dedup gate reads this so the same content can't
    fire twice (e.g. a story surfaced by both tracking and news, or a proactive
    item re-proposed on a later tick). A single-field range on ``sent_at`` in a
    per-user subcollection is auto-indexed at collection scope — no explicit index.
    Fails OPEN (returns empty set) so a read error never blocks a send.
    """
    since_aware = since if since.tzinfo else since.replace(tzinfo=UTC)

    def _read() -> set[str]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        snaps = (
            admin_firestore()
            .collection("users")
            .document(user_id)
            .collection("notifications")
            .where(filter=FieldFilter(FIELD_SENT_AT, ">=", since_aware))
            .limit(200)
            .stream()
        )
        keys: set[str] = set()
        for snap in snaps:
            row = snap.to_dict() or {}
            # Only a DELIVERED send dedups: a failed send must stay retryable, so
            # its row never blocks the same content from being attempted again.
            if row.get(FIELD_STATUS) != STATUS_SENT:
                continue
            key = row.get(FIELD_DEDUP_KEY)
            if key:
                keys.add(str(key))
        return keys

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("notification_ledger.recent_dedup_keys failed", {
            "user_id": user_id, "error": str(exc),
        })
        return set()


async def has_recent_delivery(user_id: str, notification_type: str, *, since: datetime) -> bool:
    """True if a notification of ``notification_type`` was DELIVERED to this user since
    ``since``. Same cheap single-field ``sent_at`` range as ``recent_dedup_keys``
    (auto-indexed at collection scope, no explicit index needed). Fails OPEN (False) so
    a read error never holds/blocks a send that would otherwise go out."""
    since_aware = since if since.tzinfo else since.replace(tzinfo=UTC)

    def _read() -> bool:
        from google.cloud.firestore_v1.base_query import FieldFilter

        snaps = (
            admin_firestore()
            .collection("users")
            .document(user_id)
            .collection("notifications")
            .where(filter=FieldFilter(FIELD_SENT_AT, ">=", since_aware))
            .limit(200)
            .stream()
        )
        for snap in snaps:
            row = snap.to_dict() or {}
            if row.get(FIELD_STATUS) == STATUS_SENT and row.get(FIELD_TYPE) == notification_type:
                return True
        return False

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("notification_ledger.has_recent_delivery failed", {
            "user_id": user_id, "notification_type": notification_type, "error": str(exc),
        })
        return False


async def recent_engagement(user_id: str, *, since: datetime) -> tuple[int, int]:
    """``(delivered_count, opened_count)`` over ``[since, now]`` — the substrate for
    the adaptive per-user notification volume (``notification_budget``).

    A user who taps gets a higher daily ceiling + tighter spacing; one who ignores
    gets throttled. Counts only DELIVERED rows (a failed send was never seen, so it
    can't be 'ignored'). Same cheap single-field ``sent_at`` range as
    ``recent_dedup_keys`` (auto-indexed at collection scope). Fails OPEN to ``(0, 0)``
    so a read error falls back to the gentle default tier, never an outage.
    """
    since_aware = since if since.tzinfo else since.replace(tzinfo=UTC)

    def _read() -> tuple[int, int]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        snaps = (
            admin_firestore()
            .collection("users")
            .document(user_id)
            .collection("notifications")
            .where(filter=FieldFilter(FIELD_SENT_AT, ">=", since_aware))
            .limit(500)
            .stream()
        )
        delivered = 0
        opened = 0
        for snap in snaps:
            row = snap.to_dict() or {}
            if row.get(FIELD_STATUS) != STATUS_SENT:
                continue
            delivered += 1
            if row.get(FIELD_OUTCOME) == OUTCOME_OPENED:
                opened += 1
        return delivered, opened

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("notification_ledger.recent_engagement failed", {
            "user_id": user_id, "error": str(exc),
        })
        return 0, 0


async def record_tap(
    user_id: str,
    notification_id: str,
    *,
    tapped_at: datetime | None = None,
) -> None:
    """Mark a notification as tapped: stores the tap time, the send→tap latency,
    flips the outcome to ``opened``, and records that it led to a session.

    Idempotent — a second tap report is ignored once ``tapped_at`` is set. A
    fast tap (low ``time_to_tap_seconds``) is a much stronger positive than a
    slow one, which is why the latency is persisted rather than just a boolean.
    """
    when = tapped_at or datetime.now(UTC)

    def _update() -> None:
        ref = _notification_ref(user_id, notification_id)
        snap = ref.get()
        if not snap.exists:
            return
        current = snap.to_dict() or {}
        if current.get(FIELD_TAPPED_AT) is not None:
            return  # already recorded this tap
        sent_at = current.get(FIELD_SENT_AT)
        time_to_tap: float | None = None
        if isinstance(sent_at, datetime):
            sent_aware = sent_at if sent_at.tzinfo else sent_at.replace(tzinfo=UTC)
            time_to_tap = max(0.0, (when - sent_aware).total_seconds())
        ref.update({
            FIELD_OUTCOME: OUTCOME_OPENED,
            FIELD_OUTCOME_AT: when,
            FIELD_TAPPED_AT: when,
            FIELD_TIME_TO_TAP_SECONDS: time_to_tap,
            FIELD_LED_TO_SESSION: True,
        })

    try:
        await asyncio.to_thread(_update)
    except Exception as exc:
        logger.warn("notification_ledger.record_tap failed", {
            "user_id": user_id,
            "notification_id": notification_id,
            "error": str(exc),
        })


async def record_dismiss(
    user_id: str,
    notification_id: str,
    *,
    dismissed_at: datetime | None = None,
) -> None:
    """Mark a notification as dismissed (Android swipe-away; iOS cannot report
    this). Only flips a still-``pending`` row so a tap already wins.
    """
    when = dismissed_at or datetime.now(UTC)

    def _update() -> None:
        ref = _notification_ref(user_id, notification_id)
        snap = ref.get()
        if not snap.exists:
            return
        current = snap.to_dict() or {}
        if current.get(FIELD_OUTCOME) != OUTCOME_PENDING:
            return
        ref.update({
            FIELD_OUTCOME: OUTCOME_DISMISSED,
            FIELD_OUTCOME_AT: when,
        })

    try:
        await asyncio.to_thread(_update)
    except Exception as exc:
        logger.warn("notification_ledger.record_dismiss failed", {
            "user_id": user_id,
            "notification_id": notification_id,
            "error": str(exc),
        })
