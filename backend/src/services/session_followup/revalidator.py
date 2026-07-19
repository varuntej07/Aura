"""Fire-time revalidation for source D, with a complete shadow dry run."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from google.cloud import firestore as fs

from ...config.settings import settings
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
        if candidate.get("state") not in {machine.STATE_SCHEDULED, machine.STATE_SHADOW}:
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


async def _shadow_write(
    uid: str,
    candidate_id: str,
    *,
    now: datetime,
    framed_text: dict[str, str] | None,
    would_reserve: bool,
    drop_reason: str | None,
    outcome: str,
    fire_at: datetime | None = None,
) -> None:
    data: dict[str, Any] = {
        "state": machine.STATE_SHADOW,
        "framed_text": framed_text,
        "would_reserve": would_reserve,
        "drop_reason": drop_reason,
        "shadow_outcome": outcome,
        "shadow_evaluated_at": now,
        "last_transition": now,
    }
    if fire_at is not None:
        data["fire_at"] = fire_at
    await asyncio.to_thread(_candidate_ref(uid, candidate_id).update, data)


async def _defer_shadow(
    uid: str,
    candidate_id: str,
    *,
    now: datetime,
    reason: str,
    delay,
) -> None:
    await _shadow_write(
        uid,
        candidate_id,
        now=now,
        framed_text=None,
        would_reserve=False,
        drop_reason=reason,
        outcome="deferred",
        fire_at=now + delay,
    )


async def revalidate_and_submit_followup(
    uid: str,
    candidate_id: str,
    *,
    expected_fire_epoch: float,
    now: datetime | None = None,
) -> str | None:
    """Run every fire-time gate; shadow records the would-be push and never submits."""
    if not F.feature_enabled(settings):
        return None
    when = now or datetime.now(UTC)
    candidate = await _guard_candidate(uid, candidate_id, expected_fire_epoch)
    if candidate is None:
        return None
    created_at = _aware(candidate.get("created_at")) or when
    expires_at = _aware(candidate.get("expires_at")) or created_at + F.FOLLOWUP_MAX_AGE
    if when > expires_at or when - created_at > F.FOLLOWUP_MAX_AGE:
        await _shadow_write(
            uid, candidate_id, now=when, framed_text=None, would_reserve=False,
            drop_reason="max_age", outcome="expired",
        )
        return "expired"
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
        await _defer_shadow(
            uid, candidate_id, now=when, reason="state_read_failed",
            delay=F.OTHER_TOPIC_DEFER,
        )
        return "deferred"

    if any(
        str(node.get(GF.STATUS) or GF.NODE_STATUS_ACTIVE)
        in {GF.NODE_STATUS_COMPLETED, GF.NODE_STATUS_ABANDONED}
        for node in nodes
    ):
        await _shadow_write(
            uid, candidate_id, now=when, framed_text=None, would_reserve=False,
            drop_reason="terminal_status", outcome="canceled",
        )
        return "canceled"
    topic_id = str(candidate.get("topic_id") or "")
    live_topic_ids = {
        str(live.get("active_topic_id") or "")
        for live in live_sessions
        if str(live.get("active_topic_id") or "")
    }
    if topic_id in live_topic_ids:
        await _shadow_write(
            uid, candidate_id, now=when, framed_text=None, would_reserve=False,
            drop_reason="same_topic_live", outcome="canceled",
        )
        return "canceled"
    if live_sessions:
        await _defer_shadow(
            uid, candidate_id, now=when, reason="other_topic_live",
            delay=F.OTHER_TOPIC_DEFER,
        )
        return "deferred"
    finalized_at = _aware(session.get("finalized_at")) or created_at
    last_engagement = _aware(topic.get("last_meaningful_engagement"))
    if last_engagement is not None and last_engagement > finalized_at:
        await _shadow_write(
            uid, candidate_id, now=when, framed_text=None, would_reserve=False,
            drop_reason="meaningful_reengagement", outcome="canceled",
        )
        return "canceled"
    if (
        user.get("aura_consent_granted") is not True
        or user.get("proactive_followup_opt_out") is True
        or user.get("notifications_enabled") is False
    ):
        await _shadow_write(
            uid, candidate_id, now=when, framed_text=None, would_reserve=False,
            drop_reason="consent_or_opt_out", outcome="canceled",
        )
        return "canceled"
    if (
        (candidate.get("evidence") or {}).get("sensitive") is True
        or any(node.get(GF.INFERRED_SENSITIVE) is True for node in nodes)
    ):
        await _shadow_write(
            uid, candidate_id, now=when, framed_text=None, would_reserve=False,
            drop_reason="inferred_sensitive", outcome="suppressed",
        )
        return "suppressed"
    last_notified = _aware(topic.get("last_notified_at"))
    if last_notified is not None and when - last_notified < machine.TOPIC_COOLDOWN:
        await _shadow_write(
            uid, candidate_id, now=when, framed_text=None, would_reserve=False,
            drop_reason="topic_cooldown", outcome="suppressed",
        )
        return "suppressed"
    fatigue_started = _aware(arbitration.get("fatigue_window_started_at"))
    if (
        fatigue_started is not None
        and when - fatigue_started < machine.GLOBAL_FATIGUE_WINDOW
        and int(arbitration.get("proactive_sent_24h", 0) or 0) >= machine.GLOBAL_FATIGUE_CAP
    ):
        await _shadow_write(
            uid, candidate_id, now=when, framed_text=None, would_reserve=False,
            drop_reason="global_fatigue_cap", outcome="suppressed",
        )
        return "suppressed"

    local_now, _ = await orchestrator._user_local(uid, when)
    if orchestrator._is_quiet_hours(local_now):
        await _defer_shadow(
            uid, candidate_id, now=when, reason="quiet_hours",
            delay=F.QUIET_HOURS_DEFER,
        )
        return "deferred"

    framed = await frame_memory_graph_notification(
        dict(candidate.get("value_payload") or {}),
        session_followup=True,
    )
    if framed is None:
        await _shadow_write(
            uid, candidate_id, now=when, framed_text=None, would_reserve=False,
            drop_reason="value_or_framing_rejected", outcome="suppressed",
        )
        return "suppressed"
    would_reserve = await machine.dry_run_reservation(
        uid, candidate_id, now=when
    )
    framed_text = {"title": framed.title, "body": framed.body}
    if not would_reserve:
        await _defer_shadow(
            uid, candidate_id, now=when, reason="reservation_lost",
            delay=machine.COLLISION_WINDOW + machine.RESERVATION_RETRY_DELAY,
        )
        return "deferred"

    await _shadow_write(
        uid,
        candidate_id,
        now=when,
        framed_text=framed_text,
        would_reserve=True,
        drop_reason=None,
        outcome="would_submit",
    )

    # This is the sole shadow-to-send boundary. FOLLOWUP_SHADOW always wins, so
    # enabling shadow can never leak a push even if the future send flag is set.
    if settings.PROACTIVE_FOLLOWUP_SEND and not settings.FOLLOWUP_SHADOW:
        candidate_ref = _candidate_ref(uid, candidate_id)
        await asyncio.to_thread(candidate_ref.update, {
            "state": machine.STATE_REVALIDATING,
            "last_transition": when,
        })
        reserved, _ = await machine.reserve_delivery(
            uid,
            candidate_id,
            effective_score=float(candidate.get("score", 0.0) or 0.0),
            now=when,
        )
        if not reserved:
            return "deferred"
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
                    "lineage_chain": list(candidate.get("lineage_chain") or []),
                },
            ),
            now=when,
        )
        if decision.disposition == Disposition.SEND and decision.delivered:
            await machine.mark_delivered(uid, candidate_id, now=when)
            return "delivered"
        return "deferred"
    return "shadow"


async def run_due_shadow_followups(*, now: datetime | None = None) -> int:
    """Run scheduled shadow revalidation with an exact fire-epoch retry payload."""
    if not F.feature_enabled(settings):
        return 0
    when = now or datetime.now(UTC)

    def _read() -> list[tuple[str, str, dict[str, Any]]]:
        snaps = list(
            admin_firestore()
            .collection_group(machine.CANDIDATE_SUBCOLLECTION)
            .where(filter=fs.FieldFilter("state", "==", machine.STATE_SHADOW))
            .where(filter=fs.FieldFilter("fire_at", "<=", when))
            .order_by("fire_at")
            .limit(_DUE_LIMIT)
            .stream()
        )
        result = []
        for snap in snaps:
            user_ref = snap.reference.parent.parent
            data = snap.to_dict() or {}
            if user_ref is not None and data.get("source") == F.SOURCE_SESSION_FOLLOWUP:
                result.append((user_ref.id, snap.id, data))
        return result

    try:
        due = await asyncio.to_thread(_read)
    except Exception as exc:
        logger.error("session_followup: due shadow query failed (missing index?)", {
            "error": str(exc),
        })
        return 0
    processed = 0
    for uid, candidate_id, candidate in due:
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
    return processed
