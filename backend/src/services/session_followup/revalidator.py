"""Fire-time revalidation for source D.

Every gate here re-asks a question the evaluator already answered an hour ago,
because the answer can have changed: the user may have resolved the thing, come
back and talked about it, revoked consent, or simply gone to sleep. A candidate
that fails a gate lands in a real terminal state so the reason survives in the
candidate doc; a candidate that fails a *temporary* gate is deferred with a new
fire_at and picked up again by the same drain.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from google.cloud import firestore as fs

from ...lib.logger import logger
from ..firebase import admin_firestore
from ..memory import graph_fields as GF
from ..notifications import candidate_machine as machine
from ..notifications import orchestrator
from ..notifications.memory_graph_framer import frame_memory_graph_notification
from ..notifications.proposal import (
    SOURCE_FOLLOWUP,
    Disposition,
    NotificationProposal,
    ProposalKind,
)
from . import fields as F

_DUE_LIMIT = 100


def _aware(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _candidate_ref(uid: str, candidate_id: str):
    return (
        admin_firestore()
        .collection(GF.PARENT_COLLECTION)
        .document(uid)
        .collection(machine.CANDIDATE_SUBCOLLECTION)
        .document(candidate_id)
    )


async def _guard_candidate(
    uid: str,
    candidate_id: str,
    expected_fire_epoch: float,
) -> dict[str, Any] | None:
    """Silently reject retries, superseded candidates, and stale task payloads."""
    def _read() -> dict[str, Any] | None:
        db = admin_firestore()
        candidate_ref = _candidate_ref(uid, candidate_id)
        candidate_snap = candidate_ref.get()
        if not candidate_snap.exists:
            return None
        candidate = candidate_snap.to_dict() or {}
        if candidate.get("state") not in set(machine.DUE_STATES):
            return None
        fire_at = _aware(candidate.get("fire_at"))
        if fire_at is None or abs(fire_at.timestamp() - expected_fire_epoch) > 0.001:
            return None
        topic_ref = (
            db.collection(GF.PARENT_COLLECTION)
            .document(uid)
            .collection(machine.TOPIC_STATE_SUBCOLLECTION)
            .document(str(candidate.get("topic_id") or ""))
        )
        topic_snap = topic_ref.get()
        if not topic_snap.exists:
            return None
        if (topic_snap.to_dict() or {}).get("active_candidate_id") != candidate_id:
            return None
        return candidate

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("session_followup: fire guard failed closed", {
            "user_id": uid,
            "candidate_id": candidate_id,
            "error": str(exc),
        })
        return None


async def _read_current_state(
    uid: str, candidate: dict[str, Any]
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    def _read():
        db = admin_firestore()
        user_aura_ref = db.collection(GF.PARENT_COLLECTION).document(uid)
        session_ref = user_aura_ref.collection(F.SESSIONS).document(
            str(candidate.get("session_id") or "")
        )
        topic_ref = user_aura_ref.collection(machine.TOPIC_STATE_SUBCOLLECTION).document(
            str(candidate.get("topic_id") or "")
        )
        arbitration_ref = user_aura_ref.collection(
            machine.ARBITRATION_SUBCOLLECTION
        ).document(machine.ARBITRATION_DOC_ID)
        user_snap, session_snap, topic_snap, arbitration_snap = db.get_all([
            db.collection("users").document(uid),
            session_ref,
            topic_ref,
            arbitration_ref,
        ])
        nodes = []
        node_ids = list((candidate.get("evidence") or {}).get("node_ids") or [])
        if candidate.get("node_id"):
            node_ids.append(str(candidate["node_id"]))
        node_collection = user_aura_ref.collection(GF.NODE_SUBCOLLECTION)
        for snap in db.get_all([
            node_collection.document(node_id) for node_id in dict.fromkeys(node_ids)
        ]):
            if snap.exists:
                nodes.append(snap.to_dict() or {})
        live_sessions = [
            snap.to_dict() or {}
            for snap in (
                user_aura_ref.collection(F.SESSIONS)
                .where(filter=fs.FieldFilter("state", "==", F.STATE_ACTIVE))
                .limit(20)
                .stream()
            )
        ]
        return (
            (user_snap.to_dict() or {}) if user_snap.exists else {},
            (session_snap.to_dict() or {}) if session_snap.exists else {},
            (topic_snap.to_dict() or {}) if topic_snap.exists else {},
            (arbitration_snap.to_dict() or {}) if arbitration_snap.exists else {},
            nodes,
            live_sessions,
        )

    return await asyncio.to_thread(_read)


async def _terminate(
    uid: str,
    candidate_id: str,
    *,
    now: datetime,
    reason: str,
    state: str,
) -> str:
    """Close the candidate out for good, keeping the reason on the doc."""
    await asyncio.to_thread(_candidate_ref(uid, candidate_id).update, {
        "state": state,
        "terminal_reason": reason,
        "last_transition": now,
    })
    return state


async def _defer(
    uid: str,
    candidate_id: str,
    *,
    now: datetime,
    reason: str,
    delay,
) -> str:
    """Push the candidate out to a later fire_at; the drain re-picks it up."""
    await asyncio.to_thread(_candidate_ref(uid, candidate_id).update, {
        "state": machine.STATE_DEFERRED,
        "defer_reason": reason,
        "fire_at": now + delay,
        "last_transition": now,
    })
    return machine.STATE_DEFERRED


async def revalidate_and_submit_followup(
    uid: str,
    candidate_id: str,
    *,
    expected_fire_epoch: float,
    now: datetime | None = None,
) -> str | None:
    """Run every fire-time gate, then submit the push through the shared funnel."""
    when = now or datetime.now(UTC)
    candidate = await _guard_candidate(uid, candidate_id, expected_fire_epoch)
    if candidate is None:
        return None
    created_at = _aware(candidate.get("created_at")) or when
    expires_at = _aware(candidate.get("expires_at")) or created_at + F.FOLLOWUP_MAX_AGE
    if when > expires_at or when - created_at > F.FOLLOWUP_MAX_AGE:
        return await _terminate(
            uid, candidate_id, now=when,
            reason="max_age", state=machine.STATE_EXPIRED,
        )
    try:
        user, session, topic, arbitration, nodes, live_sessions = await _read_current_state(
            uid, candidate
        )
    except Exception as exc:
        logger.warn("session_followup: current-state read failed", {
            "user_id": uid,
            "candidate_id": candidate_id,
            "error": str(exc),
        })
        return await _defer(
            uid, candidate_id, now=when, reason="state_read_failed",
            delay=F.OTHER_TOPIC_DEFER,
        )

    if any(
        str(node.get(GF.STATUS) or GF.NODE_STATUS_ACTIVE)
        in {GF.NODE_STATUS_COMPLETED, GF.NODE_STATUS_ABANDONED}
        for node in nodes
    ):
        return await _terminate(
            uid, candidate_id, now=when,
            reason="terminal_status", state=machine.STATE_CANCELED,
        )
    topic_id = str(candidate.get("topic_id") or "")
    live_topic_ids = {
        str(live.get("active_topic_id") or "")
        for live in live_sessions
        if str(live.get("active_topic_id") or "")
    }
    if topic_id in live_topic_ids:
        # They are talking about this right now. Poking them about the thing on
        # their screen is the worst failure this system can produce.
        return await _terminate(
            uid, candidate_id, now=when,
            reason="same_topic_live", state=machine.STATE_CANCELED,
        )
    if live_sessions:
        return await _defer(
            uid, candidate_id, now=when, reason="other_topic_live",
            delay=F.OTHER_TOPIC_DEFER,
        )
    finalized_at = _aware(session.get("finalized_at")) or created_at
    last_engagement = _aware(topic.get("last_meaningful_engagement"))
    if last_engagement is not None and last_engagement > finalized_at:
        return await _terminate(
            uid, candidate_id, now=when,
            reason="meaningful_reengagement", state=machine.STATE_CANCELED,
        )
    if (
        user.get("aura_consent_granted") is not True
        or user.get("proactive_followup_opt_out") is True
        or user.get("notifications_enabled") is False
    ):
        return await _terminate(
            uid, candidate_id, now=when,
            reason="consent_or_opt_out", state=machine.STATE_CANCELED,
        )
    if (
        (candidate.get("evidence") or {}).get("sensitive") is True
        or any(node.get(GF.INFERRED_SENSITIVE) is True for node in nodes)
    ):
        # Depth is not permission. A long, real conversation about health, money,
        # or a relationship is exactly the one we must never chase.
        return await _terminate(
            uid, candidate_id, now=when,
            reason="inferred_sensitive", state=machine.STATE_SUPPRESSED,
        )
    last_notified = _aware(topic.get("last_notified_at"))
    if last_notified is not None and when - last_notified < machine.TOPIC_COOLDOWN:
        return await _terminate(
            uid, candidate_id, now=when,
            reason="topic_cooldown", state=machine.STATE_SUPPRESSED,
        )
    fatigue_started = _aware(arbitration.get("fatigue_window_started_at"))
    if (
        fatigue_started is not None
        and when - fatigue_started < machine.GLOBAL_FATIGUE_WINDOW
        and int(arbitration.get("proactive_sent_24h", 0) or 0) >= machine.GLOBAL_FATIGUE_CAP
    ):
        return await _terminate(
            uid, candidate_id, now=when,
            reason="global_fatigue_cap", state=machine.STATE_SUPPRESSED,
        )

    local_now, _ = await orchestrator._user_local(uid, when)
    if orchestrator._is_quiet_hours(local_now):
        # Deferred, not dropped: it retries every 30 min until FOLLOWUP_MAX_AGE
        # expires it, so a late-night thought dies quietly rather than waking them.
        return await _defer(
            uid, candidate_id, now=when, reason="quiet_hours",
            delay=F.QUIET_HOURS_DEFER,
        )

    framed = await frame_memory_graph_notification(
        dict(candidate.get("value_payload") or {}),
        session_followup=True,
    )
    if framed is None:
        return await _terminate(
            uid, candidate_id, now=when,
            reason="value_or_framing_rejected", state=machine.STATE_SUPPRESSED,
        )

    await asyncio.to_thread(_candidate_ref(uid, candidate_id).update, {
        "state": machine.STATE_REVALIDATING,
        "framed_text": {"title": framed.title, "body": framed.body},
        "last_transition": when,
    })
    reserved, _ = await machine.reserve_delivery(
        uid,
        candidate_id,
        effective_score=float(candidate.get("score", 0.0) or 0.0),
        now=when,
    )
    if not reserved:
        return await _defer(
            uid, candidate_id, now=when, reason="reservation_lost",
            delay=machine.COLLISION_WINDOW + machine.RESERVATION_RETRY_DELAY,
        )

    decision = await orchestrator.submit(
        NotificationProposal(
            user_id=uid,
            source=SOURCE_FOLLOWUP,
            kind=ProposalKind.PROACTIVE,
            dedup_key=topic_id,
            title=framed.title,
            body=framed.body,
            notification_type=F.NOTIFICATION_TYPE,
            collapse_key=f"session_followup_{topic_id}",
            priority=75,
            data={
                "candidate_id": candidate_id,
                "topic_id": topic_id,
                "notification_origin": F.SOURCE_SESSION_FOLLOWUP,
                # FCM data values must be strings; a list here fails at delivery.
                "lineage_chain": ",".join(
                    str(item) for item in (candidate.get("lineage_chain") or [])
                ),
            },
        ),
        now=when,
    )
    if decision.disposition == Disposition.SEND and decision.delivered:
        await machine.mark_delivered(uid, candidate_id, now=when)
        return machine.STATE_DELIVERED
    # The orchestrator held or dropped it (presence, budget, arbitration). Retry
    # once the collision window has passed rather than losing the intent.
    return await _defer(
        uid, candidate_id, now=when, reason=f"orchestrator_{decision.disposition}",
        delay=machine.RESERVATION_RETRY_DELAY,
    )


async def run_due_followups(*, now: datetime | None = None) -> int:
    """Revalidate every due follow-up candidate with an exact fire-epoch payload.

    Reuses the shared due query so scheduled and deferred candidates are both
    picked up; a candidate deferred for quiet hours must come back.
    """
    when = now or datetime.now(UTC)
    due = await machine.list_due_candidates(now=when, limit=_DUE_LIMIT)
    mine = [
        (uid, candidate_id, candidate)
        for uid, candidate_id, candidate in due
        if candidate.get("source") == F.SOURCE_SESSION_FOLLOWUP
    ]
    processed = 0
    for uid, candidate_id, candidate in mine:
        fire_at = _aware(candidate.get("fire_at"))
        if fire_at is None:
            continue
        await revalidate_and_submit_followup(
            uid,
            candidate_id,
            expected_fire_epoch=fire_at.timestamp(),
            now=when,
        )
        processed += 1
    # Zero due and zero processed are the same shape; say which one happened so a
    # silently stalled drain cannot look identical to a quiet hour.
    if mine and not processed:
        logger.warn("session_followup: due candidates resolved to zero work", {
            "due": len(mine),
        })
    return processed
