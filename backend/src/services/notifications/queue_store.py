"""Firestore-backed proactive proposal queue.

Only the PROACTIVE lane uses this (thread / icebreaker / news). Committed sends
never touch it — they go straight through ``orchestrator.submit`` inline. A
proactive proposal lands here as a small TTL'd doc; the per-minute drain on
``/scheduler/tick`` reads everything pending for one user, arbitrates, sends the
winner, and holds the losers for a later window.

Layout: ``users/{uid}/notification_queue/{proposal_id}``
  proposal_id is deterministic per (source, dedup_key) so re-submitting the same
  content before a drain overwrites rather than duplicates.

Field names live HERE (one source of truth, CLAUDE.md data-layer rule). The drain
reads with a single equality filter (``status IN [pending, held]``) and sorts by
priority in Python, so NO composite Firestore index is required.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud import firestore as fs

from ...lib.logger import logger
from ..firebase import admin_firestore
from ..notification_ledger import NotificationDecision
from .proposal import DeliveryChannel, NotificationProposal, ProposalKind

QUEUE_SUBCOLLECTION = "notification_queue"

# A proactive proposal that cannot win a window within this long is no longer
# worth sending (news goes stale, an opener loses its moment). The drain skips
# and a native TTL on ``expires_at`` purges it.
QUEUE_TTL = timedelta(hours=6)

# ── Field-name contract ─────────────────────────────────────────────────────
FIELD_PROPOSAL_ID = "proposal_id"
FIELD_SOURCE = "source"
FIELD_KIND = "kind"
FIELD_DEDUP_KEY = "dedup_key"
FIELD_TITLE = "title"
FIELD_BODY = "body"
FIELD_DATA = "data"
FIELD_COLLAPSE_KEY = "collapse_key"
FIELD_NOTIFICATION_TYPE = "notification_type"
FIELD_DATA_ONLY = "data_only"
FIELD_APNS_CATEGORY = "apns_category"
FIELD_CHANNELS = "channels"
FIELD_CONTENT_TIMESTAMP = "content_timestamp"
FIELD_FRESHNESS_MAX_AGE_S = "freshness_max_age_seconds"
FIELD_PRIORITY = "priority"
FIELD_DECISION = "decision"
FIELD_STATUS = "status"
FIELD_HOLD_COUNT = "hold_count"
FIELD_CREATED_AT = "created_at"
FIELD_UPDATED_AT = "updated_at"
FIELD_EXPIRES_AT = "expires_at"

# Lifecycle.
STATUS_PENDING = "pending"
STATUS_HELD = "held"
STATUS_SENT = "sent"
STATUS_DROPPED = "dropped"

_ACTIVE_STATUSES = [STATUS_PENDING, STATUS_HELD]


def _queue_col(user_id: str):
    return (
        admin_firestore()
        .collection("users")
        .document(user_id)
        .collection(QUEUE_SUBCOLLECTION)
    )


def proposal_id_for(source: str, dedup_key: str) -> str:
    """Deterministic id per (source, dedup_key) so the same content overwrites."""
    if not dedup_key:
        return uuid.uuid4().hex
    digest = hashlib.sha256(f"{source}:{dedup_key}".encode()).hexdigest()
    return digest[:32]


def _proposal_to_doc(proposal: NotificationProposal, now: datetime) -> dict[str, Any]:
    max_age = proposal.freshness_max_age
    decision = proposal.decision
    return {
        FIELD_PROPOSAL_ID: proposal_id_for(proposal.source, proposal.dedup_key),
        FIELD_SOURCE: proposal.source,
        FIELD_KIND: proposal.kind.value,
        FIELD_DEDUP_KEY: proposal.dedup_key,
        FIELD_TITLE: proposal.title,
        FIELD_BODY: proposal.body,
        FIELD_DATA: proposal.data or {},
        FIELD_COLLAPSE_KEY: proposal.collapse_key,
        FIELD_NOTIFICATION_TYPE: proposal.notification_type,
        FIELD_DATA_ONLY: proposal.data_only,
        FIELD_APNS_CATEGORY: proposal.apns_category,
        FIELD_CHANNELS: sorted(channel.value for channel in proposal.channels),
        FIELD_CONTENT_TIMESTAMP: proposal.content_timestamp,
        FIELD_FRESHNESS_MAX_AGE_S: max_age.total_seconds() if max_age else None,
        FIELD_PRIORITY: proposal.effective_priority,
        FIELD_DECISION: dataclasses.asdict(decision) if decision else None,
        FIELD_STATUS: STATUS_PENDING,
        FIELD_HOLD_COUNT: 0,
        FIELD_CREATED_AT: now,
        FIELD_UPDATED_AT: now,
        FIELD_EXPIRES_AT: now + QUEUE_TTL,
    }


def _doc_to_proposal(data: dict[str, Any]) -> NotificationProposal:
    max_age_s = data.get(FIELD_FRESHNESS_MAX_AGE_S)
    decision_doc = data.get(FIELD_DECISION)
    decision = NotificationDecision(**decision_doc) if decision_doc else None
    return NotificationProposal(
        user_id="",  # filled by the caller from the queue path
        source=str(data.get(FIELD_SOURCE, "")),
        kind=ProposalKind(data.get(FIELD_KIND, ProposalKind.PROACTIVE.value)),
        dedup_key=str(data.get(FIELD_DEDUP_KEY, "")),
        title=str(data.get(FIELD_TITLE, "")),
        body=str(data.get(FIELD_BODY, "")),
        data={k: str(v) for k, v in (data.get(FIELD_DATA) or {}).items()},
        collapse_key=data.get(FIELD_COLLAPSE_KEY),
        notification_type=str(data.get(FIELD_NOTIFICATION_TYPE, "")),
        data_only=bool(data.get(FIELD_DATA_ONLY, False)),
        apns_category=data.get(FIELD_APNS_CATEGORY),
        channels=frozenset(
            DeliveryChannel(value)
            for value in data.get(FIELD_CHANNELS, [DeliveryChannel.MOBILE.value])
        ),
        content_timestamp=data.get(FIELD_CONTENT_TIMESTAMP),
        freshness_max_age=timedelta(seconds=max_age_s) if max_age_s is not None else None,
        priority=int(data[FIELD_PRIORITY]) if data.get(FIELD_PRIORITY) is not None else None,
        decision=decision,
    )


async def enqueue(proposal: NotificationProposal, *, now: datetime | None = None) -> str:
    """Write (or overwrite) a proactive proposal. Returns the proposal_id.

    Swallows write errors (a queue write must never break the producer's tick).
    """
    when = now or datetime.now(UTC)
    pid = proposal_id_for(proposal.source, proposal.dedup_key)
    doc = _proposal_to_doc(proposal, when)

    def _put() -> None:
        _queue_col(proposal.user_id).document(pid).set(doc)

    try:
        await asyncio.to_thread(_put)
    except Exception as exc:
        logger.warn("notification queue: enqueue failed", {
            "user_id": proposal.user_id, "source": proposal.source, "error": str(exc),
        })
    return pid


async def list_pending(user_id: str, *, limit: int = 50) -> list[tuple[str, NotificationProposal]]:
    """All pending/held proposals for a user, sorted by priority desc in Python.

    Returns ``(proposal_id, proposal)`` pairs. No composite index needed — the
    only Firestore predicate is a single ``status IN`` equality filter.
    """
    def _read() -> list[tuple[str, NotificationProposal]]:
        snaps = (
            _queue_col(user_id)
            .where(filter=fs.FieldFilter(FIELD_STATUS, "in", _ACTIVE_STATUSES))
            .limit(limit)
            .stream()
        )
        out: list[tuple[str, NotificationProposal]] = []
        for snap in snaps:
            data = snap.to_dict() or {}
            proposal = _doc_to_proposal(data)
            proposal.user_id = user_id
            out.append((snap.id, proposal))
        out.sort(key=lambda pair: pair[1].effective_priority, reverse=True)
        return out

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("notification queue: list_pending failed", {
            "user_id": user_id, "error": str(exc),
        })
        return []


async def list_user_ids_with_pending(*, limit: int = 2000) -> set[str]:
    """Distinct user_ids with an active (pending/held) proposal queued right now,
    across every user, discovered via ONE collection_group query.

    The per-minute proactive drain used to call ``list_pending`` once per active
    user (~15 reads/minute at this project's scale, nearly all against empty
    queues) to find out who needed draining. This does the same discovery in one
    query instead, so ``drain_user_queue`` (which still runs its own
    ``list_pending`` internally) is only invoked for uids known to have
    something queued. Requires a COLLECTION_GROUP field override on
    ``notification_queue.status`` (firestore.indexes.json) — a collection_group
    query filtered on a field is never auto-indexed.

    Loud on truncation: if the result hits ``limit`` exactly, more may exist
    that this pass silently drops, so that's logged rather than looking
    identical to "everyone's queue is empty."
    """
    def _read() -> set[str]:
        snaps = list(
            admin_firestore()
            .collection_group(QUEUE_SUBCOLLECTION)
            .where(filter=fs.FieldFilter(FIELD_STATUS, "in", _ACTIVE_STATUSES))
            .limit(limit)
            .stream()
        )
        if len(snaps) >= limit:
            logger.warn(
                "notification queue: list_user_ids_with_pending hit its limit, "
                "some queued users may be missed this tick",
                {"limit": limit},
            )
        uids: set[str] = set()
        for snap in snaps:
            # Path: users/{uid}/notification_queue/{proposal_id}
            user_doc_ref = snap.reference.parent.parent
            if user_doc_ref is not None:
                uids.add(user_doc_ref.id)
        return uids

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("notification queue: list_user_ids_with_pending failed", {"error": str(exc)})
        return set()


async def drop_if_active(user_id: str, proposal_id: str) -> bool:
    """Mark a queued proposal as dropped if it is still pending or held. Returns
    True iff a proposal was actually dropped. No-op when the doc doesn't exist or
    is already in a terminal state.

    Called by the orchestrator after reconcile resolves subjects: a followup that
    was enqueued in a prior drain must not survive a later "mom is fine" event,
    even though the same-batch race guard only covers the drain that processed both
    events together. The funnel's dedup_key prevents double-send on the rare path
    where the proposal was already sent before this drop arrives."""
    def _drop() -> bool:
        ref = _queue_col(user_id).document(proposal_id)
        snap = ref.get()
        if not snap.exists:
            return False
        if (snap.to_dict() or {}).get(FIELD_STATUS) not in _ACTIVE_STATUSES:
            return False
        ref.update({FIELD_STATUS: STATUS_DROPPED, FIELD_UPDATED_AT: datetime.now(UTC)})
        return True

    try:
        return await asyncio.to_thread(_drop)
    except Exception as exc:
        logger.warn("notification queue: drop_if_active failed", {
            "user_id": user_id, "proposal_id": proposal_id, "error": str(exc),
        })
        return False


async def mark(
    user_id: str, proposal_id: str, status: str, *, now: datetime | None = None
) -> None:
    """Set a queue item's terminal/hold status. SENT/DROPPED are terminal; HELD
    keeps it for the next window and bumps ``hold_count``."""
    when = now or datetime.now(UTC)

    def _update() -> None:
        ref = _queue_col(user_id).document(proposal_id)
        update: dict[str, Any] = {FIELD_STATUS: status, FIELD_UPDATED_AT: when}
        if status == STATUS_HELD:
            update[FIELD_HOLD_COUNT] = fs.Increment(1)
        ref.update(update)

    try:
        await asyncio.to_thread(_update)
    except Exception as exc:
        logger.warn("notification queue: mark failed", {
            "user_id": user_id, "proposal_id": proposal_id,
            "status": status, "error": str(exc),
        })
