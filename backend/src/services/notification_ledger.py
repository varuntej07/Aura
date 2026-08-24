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
FIELD_FIRST_ATTEMPT_AT = "first_attempt_at"
FIELD_LAST_ATTEMPT_AT = "last_attempt_at"
FIELD_ATTEMPT_COUNT = "attempt_count"
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

# Delivery status values. STATUS_SENT is legacy-only and remains readable.
STATUS_SENT = "sent"
STATUS_ACCEPTED = "accepted"
STATUS_QUEUED = "queued"
STATUS_RECEIVED = "received"
STATUS_SEEN = "seen"
STATUS_ACTED = "acted"
STATUS_FAILED = "failed"
STATUS_NO_ELIGIBLE_ENDPOINT = "no_eligible_endpoint"

TRANSPORT_ACCEPTED_STATUSES = frozenset({
    STATUS_SENT,
    STATUS_ACCEPTED,
    STATUS_QUEUED,
    STATUS_RECEIVED,
    STATUS_SEEN,
    STATUS_ACTED,
})

_ACK_STATUS_RANK = {
    STATUS_QUEUED: 0,
    STATUS_RECEIVED: 1,
    STATUS_SEEN: 2,
    STATUS_ACTED: 3,
}


def resolve_delivery_status(
    channels: dict[str, dict[str, Any]],
) -> str:
    """Resolve one truthful top-level status from independent channel states."""
    statuses = {
        str(value.get("status") or "")
        for value in channels.values()
        if isinstance(value, dict)
    }
    for status in (STATUS_ACTED, STATUS_SEEN, STATUS_RECEIVED, STATUS_ACCEPTED):
        if status in statuses:
            return status
    if STATUS_QUEUED in statuses or "deduplicated" in statuses:
        return STATUS_QUEUED
    if statuses and statuses <= {STATUS_NO_ELIGIBLE_ENDPOINT}:
        return STATUS_NO_ELIGIBLE_ENDPOINT
    if not statuses:
        return STATUS_NO_ELIGIBLE_ENDPOINT
    return STATUS_FAILED


@dataclass
class NotificationDecision:
    """Learning and policy metadata persisted with an orchestrated send attempt.

    The orchestrator fills policy fields for every attempted send. The signal
    engine additionally fills ranking and framing fields so those can be tuned
    against real tap outcomes instead of guesses.

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
    policy_version: str = ""
    declared_kind: str = ""
    effective_kind: str = ""
    lane_reason: str = ""
    policy_checks: dict[str, str] = field(default_factory=dict)


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
        "policy_version": decision.policy_version,
        "declared_kind": decision.declared_kind,
        "effective_kind": decision.effective_kind,
        "lane_reason": decision.lane_reason,
        "policy_checks": decision.policy_checks,
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
    accepted: bool | None = None,
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
    accepted = delivered if accepted is None else accepted
    channels = channel_results or {
        "mobile": {
            "status": (
                STATUS_ACCEPTED
                if accepted
                else STATUS_NO_ELIGIBLE_ENDPOINT
                if tokens_targeted == 0
                else STATUS_FAILED
            ),
            "accepted": accepted,
            "delivered": delivered,
            "tokens_targeted": tokens_targeted,
            "success_count": success_count,
            "failure_count": failure_count,
        }
    }
    status = resolve_delivery_status(channels)
    received = status in {STATUS_RECEIVED, STATUS_SEEN, STATUS_ACTED}
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
        FIELD_STATUS: status,
        FIELD_DELIVERY: {
            "tokens_targeted": tokens_targeted,
            "success_count": success_count,
            "failure_count": failure_count,
            "accepted": accepted,
            "received": received,
            "delivered": received,
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
        ref = _notification_ref(user_id, notification_id)
        snap = ref.get()
        current = snap.to_dict() if getattr(snap, "exists", False) else {}
        if not isinstance(current, dict):
            current = {}
        previous_delivery = current.get(FIELD_DELIVERY)
        if not isinstance(previous_delivery, dict):
            previous_delivery = {}
        previous_channel_attempts = previous_delivery.get("channel_attempt_counts")
        if not isinstance(previous_channel_attempts, dict):
            previous_channel_attempts = {}
        previous_channel_accepts = previous_delivery.get("channel_accept_counts")
        if not isinstance(previous_channel_accepts, dict):
            previous_channel_accepts = {}

        channel_attempt_counts = dict(previous_channel_attempts)
        channel_accept_counts = dict(previous_channel_accepts)
        for channel, outcome in channels.items():
            channel_attempt_counts[channel] = int(channel_attempt_counts.get(channel, 0)) + 1
            if bool(outcome.get("accepted")):
                channel_accept_counts[channel] = int(channel_accept_counts.get(channel, 0)) + 1

        previous_attempts = current.get(FIELD_ATTEMPT_COUNT, 0)
        try:
            attempt_count = max(0, int(previous_attempts)) + 1
        except (TypeError, ValueError):
            attempt_count = 1
        first_attempt_at = current.get(FIELD_FIRST_ATTEMPT_AT) or now
        doc[FIELD_FIRST_ATTEMPT_AT] = first_attempt_at
        doc[FIELD_LAST_ATTEMPT_AT] = now
        doc[FIELD_ATTEMPT_COUNT] = attempt_count
        doc[FIELD_DELIVERY]["channel_attempt_counts"] = channel_attempt_counts
        doc[FIELD_DELIVERY]["channel_accept_counts"] = channel_accept_counts
        ref.set(doc)

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
            # Only a transport-accepted send dedups: a failed send stays retryable, so
            # its row never blocks the same content from being attempted again.
            if row.get(FIELD_STATUS) not in TRANSPORT_ACCEPTED_STATUSES:
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


async def has_recent_acceptance(
    user_id: str, notification_type: str, *, since: datetime
) -> bool:
    """True if a notification of ``notification_type`` was transport-accepted since
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
            if (
                row.get(FIELD_STATUS) in TRANSPORT_ACCEPTED_STATUSES
                and row.get(FIELD_TYPE) == notification_type
            ):
                return True
        return False

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("notification_ledger.has_recent_acceptance failed", {
            "user_id": user_id, "notification_type": notification_type, "error": str(exc),
        })
        return False


async def has_recent_delivery(user_id: str, notification_type: str, *, since: datetime) -> bool:
    """Backward-compatible alias for legacy callers and rows."""
    return await has_recent_acceptance(user_id, notification_type, since=since)


async def recent_notification_activity(
    user_id: str, *, since: datetime
) -> tuple[int, int]:
    """``(delivered_count, opened_count)`` over ``[since, now]`` — the substrate for
    the adaptive per-user notification volume (``notification_budget``).

    A user who taps gets a higher daily ceiling + tighter spacing; one who ignores
    gets throttled. Counts only transport-accepted rows; failed sends do not enter
    the engagement denominator. Same cheap single-field ``sent_at`` range as
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
        accepted = 0
        opened = 0
        for snap in snaps:
            row = snap.to_dict() or {}
            if row.get(FIELD_STATUS) not in TRANSPORT_ACCEPTED_STATUSES:
                continue
            accepted += 1
            if row.get(FIELD_OUTCOME) == OUTCOME_OPENED:
                opened += 1
        return accepted, opened

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("notification_ledger.recent_notification_activity failed", {
            "user_id": user_id, "error": str(exc),
        })
        return 0, 0


async def recent_engagement(user_id: str, *, since: datetime) -> tuple[int, int]:
    """Backward-compatible alias; the first count is transport-accepted rows."""
    return await recent_notification_activity(user_id, since=since)


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
        channels = delivery_channels(current)
        acted_channel = (
            "desktop"
            if channels.get("desktop", {}).get("status") == STATUS_ACTED
            else "mobile"
        )
        ref.update({
            FIELD_STATUS: STATUS_ACTED,
            FIELD_OUTCOME: OUTCOME_OPENED,
            FIELD_OUTCOME_AT: when,
            FIELD_TAPPED_AT: when,
            FIELD_TIME_TO_TAP_SECONDS: time_to_tap,
            FIELD_LED_TO_SESSION: True,
            f"{FIELD_DELIVERY}.received": True,
            f"{FIELD_DELIVERY}.delivered": True,
            f"{FIELD_DELIVERY}.{FIELD_CHANNELS}.{acted_channel}.status": STATUS_ACTED,
            f"{FIELD_DELIVERY}.{FIELD_CHANNELS}.{acted_channel}.acted_at": when,
        })

    try:
        await asyncio.to_thread(_update)
    except Exception as exc:
        logger.warn("notification_ledger.record_tap failed", {
            "user_id": user_id,
            "notification_id": notification_id,
            "error": str(exc),
        })


async def record_desktop_ack(
    user_id: str,
    notification_id: str,
    *,
    status: str,
    acknowledged_at: datetime | None = None,
) -> None:
    """Record desktop receipt/visibility without creating a second logical event."""
    when = acknowledged_at or datetime.now(UTC)

    def _update_channel() -> None:
        ref = _notification_ref(user_id, notification_id)
        snap = ref.get()
        if not snap.exists:
            return
        current = snap.to_dict() or {}
        channels = delivery_channels(current)
        desktop = dict(channels.get("desktop") or {})
        current_status = str(desktop.get("status") or STATUS_QUEUED)
        if _ACK_STATUS_RANK.get(current_status, -1) >= _ACK_STATUS_RANK.get(status, -1):
            return
        desktop["status"] = status
        channels["desktop"] = desktop
        top_level = resolve_delivery_status(channels)
        ref.update({
            FIELD_STATUS: top_level,
            f"{FIELD_DELIVERY}.{FIELD_CHANNELS}.desktop.status": status,
            f"{FIELD_DELIVERY}.{FIELD_CHANNELS}.desktop.{status}_at": when,
            f"{FIELD_DELIVERY}.received": top_level
            in {STATUS_RECEIVED, STATUS_SEEN, STATUS_ACTED},
            f"{FIELD_DELIVERY}.delivered": top_level
            in {STATUS_RECEIVED, STATUS_SEEN, STATUS_ACTED},
        })

    try:
        await asyncio.to_thread(_update_channel)
        if status == STATUS_ACTED:
            await record_tap(user_id, notification_id, tapped_at=when)
    except Exception as exc:
        logger.warn("notification_ledger.record_desktop_ack failed", {
            "user_id": user_id,
            "notification_id": notification_id,
            "error_type": type(exc).__name__,
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
