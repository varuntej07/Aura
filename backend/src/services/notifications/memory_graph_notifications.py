"""Phase 4 memory-graph notification sources A, B, and C.

Source C is evidence only. The graph write path marks a node when a new strong
edge forms, and this sweep may add that evidence to an otherwise eligible A or B
candidate. No graph write path calls the notification orchestrator.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud.firestore_v1.base_query import FieldFilter

from ...config.settings import settings
from ...lib.logger import logger
from ..firebase import admin_firestore
from ..memory import graph_fields as GF
from ..memory.salience import normalized_graph_salience
from ..signal_engine.feature_store import list_active_user_ids
from . import candidate_machine as machine
from . import orchestrator
from .candidate_machine import CandidateDraft
from .memory_graph_framer import frame_memory_graph_notification, valid_phase4_payload
from .proposal import (
    REASON_STALE,
    SOURCE_MEMORY_GRAPH,
    Disposition,
    NotificationProposal,
    OrchestratorDecision,
    ProposalKind,
)

SOURCE_DORMANT_GOAL = "dormant_goal"
SOURCE_UPCOMING_EVENT = "upcoming_event"

NODE_PREFETCH_LIMIT = 24
DORMANT_MIN_AGE = timedelta(days=5)
DORMANT_MAX_AGE = timedelta(days=14)
EVENT_HORIZON = timedelta(days=14)
EDGE_EVIDENCE_MAX_AGE = timedelta(days=2)
EDGE_EVIDENCE_SCORE_BONUS = 0.2
EVENT_PROXIMITY_SCORE_BONUS = 0.15
PROJECT_RECENCY_PENALTY = 0.15
FRAMING_RETRY_DELAY = timedelta(minutes=15)
ORCHESTRATOR_RETRY_DELAY = machine.COLLISION_WINDOW + timedelta(minutes=1)

SOURCE_A_VALUE_TYPES = frozenset({
    "unresolved_action",
    "next_step",
    "cross_memory_connection",
})


def _aware_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _safe_nonnegative_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _recent_edge_evidence(node: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    marked_at = _aware_datetime(node.get(GF.NEW_STRONG_EDGE_AT))
    evidence = node.get(GF.NEW_STRONG_EDGE_EVIDENCE)
    if (
        marked_at is None
        or not isinstance(evidence, dict)
        or now - marked_at > EDGE_EVIDENCE_MAX_AGE
    ):
        return None
    return dict(evidence)


def _source_a_payload(
    node: dict[str, Any], edge_evidence: dict[str, Any] | None
) -> dict[str, Any] | None:
    payload = valid_phase4_payload(node.get(GF.VALUE_PAYLOAD))
    if payload is not None and payload.get("type") in SOURCE_A_VALUE_TYPES:
        return payload
    if edge_evidence is None:
        return None
    display = str(node.get(GF.DISPLAY) or node.get(GF.ENTITY) or "this").strip()
    connected = str(edge_evidence.get("connected_display") or "another memory").strip()
    return {
        "type": "cross_memory_connection",
        "evidence": f"{display} connects with {connected}",
        "artifact_ref": None,
    }


def _source_b_payload(node: dict[str, Any], deadline: datetime) -> dict[str, Any]:
    existing = valid_phase4_payload(node.get(GF.VALUE_PAYLOAD))
    if existing is not None and existing.get("type") == "deadline":
        return {**existing, "deadline": deadline.isoformat(), "artifact_ref": None}
    display = str(node.get(GF.DISPLAY) or node.get(GF.ENTITY) or "this event").strip()
    return {
        "type": "deadline",
        "evidence": f"{display} is coming up",
        "deadline": deadline.isoformat(),
        "artifact_ref": None,
    }


def candidate_drafts_for_node(
    uid: str,
    node_id: str,
    node: dict[str, Any],
    *,
    now: datetime,
) -> list[CandidateDraft]:
    """Build source-specific drafts. Edge evidence alone cannot make a draft."""
    status = str(node.get(GF.STATUS, GF.NODE_STATUS_ACTIVE))
    if status not in {GF.NODE_STATUS_ACTIVE, GF.NODE_STATUS_DORMANT}:
        return []
    salience = normalized_graph_salience(node)
    if salience <= 0.0:
        return []

    topic_id = machine.topic_id_for(node_id)
    project_id = str(node.get(GF.PROJECT_ID) or "") or None
    edge_evidence = _recent_edge_evidence(node, now)
    edge_bonus = EDGE_EVIDENCE_SCORE_BONUS if edge_evidence else 0.0
    drafts: list[CandidateDraft] = []

    last_engagement = _aware_datetime(node.get(GF.LAST_MEANINGFUL_ENGAGEMENT))
    if last_engagement is not None:
        dormant_age = now - last_engagement
        payload = _source_a_payload(node, edge_evidence)
        if DORMANT_MIN_AGE <= dormant_age <= DORMANT_MAX_AGE and payload is not None:
            event_key = f"{node_id}|{last_engagement.isoformat()}"
            candidate_id = machine.candidate_id_for(
                uid, topic_id, SOURCE_DORMANT_GOAL, event_key
            )
            drafts.append(CandidateDraft(
                candidate_id=candidate_id,
                topic_id=topic_id,
                source=SOURCE_DORMANT_GOAL,
                project_id=project_id,
                node_id=node_id,
                event_id=None,
                value_payload=payload,
                evidence={
                    "normalized_salience": salience,
                    "new_strong_edge": edge_evidence,
                    "last_meaningful_engagement": last_engagement.isoformat(),
                },
                score=salience + edge_bonus,
                fire_at=now,
                expires_at=now + timedelta(hours=6),
            ))

    deadline = _aware_datetime(node.get(GF.DEADLINE))
    if deadline is not None and now < deadline <= now + EVENT_HORIZON:
        milestones = (
            (deadline - timedelta(days=1), "t_minus_1d"),
            (deadline - timedelta(hours=2), "t_minus_2h"),
        )
        next_milestone = next(
            ((fire_at, name) for fire_at, name in milestones if fire_at > now),
            None,
        )
        if next_milestone is not None:
            fire_at, milestone = next_milestone
            event_id = f"{node_id}|{deadline.isoformat()}"
            event_key = f"{event_id}|{milestone}"
            candidate_id = machine.candidate_id_for(
                uid, topic_id, SOURCE_UPCOMING_EVENT, event_key
            )
            drafts.append(CandidateDraft(
                candidate_id=candidate_id,
                topic_id=topic_id,
                source=SOURCE_UPCOMING_EVENT,
                project_id=project_id,
                node_id=node_id,
                event_id=event_id,
                value_payload=_source_b_payload(node, deadline),
                evidence={
                    "normalized_salience": salience,
                    "new_strong_edge": edge_evidence,
                    "deadline": deadline.isoformat(),
                    "milestone": milestone,
                },
                score=salience + edge_bonus + EVENT_PROXIMITY_SCORE_BONUS,
                fire_at=fire_at,
                expires_at=deadline,
            ))
    return drafts


def hard_zero_reason(
    node: dict[str, Any],
    draft: CandidateDraft,
    topic_state: dict[str, Any],
    arbitration: dict[str, Any],
    *,
    now: datetime,
) -> str | None:
    """Apply every permanent scorer gate before creation and again at delivery."""
    payload = draft.value_payload
    if node.get(GF.INFERRED_SENSITIVE) is True or payload.get("sensitive") is True:
        return "inferred_sensitive"
    if str(node.get(GF.STATUS)) in {
        GF.NODE_STATUS_COMPLETED,
        GF.NODE_STATUS_ABANDONED,
    }:
        return "terminal_status"
    if node.get(GF.REMINDER_CREATED_IN_SESSION) is True or payload.get(
        "reminder_created_in_session"
    ) is True:
        return "reminder_created_in_session"
    if topic_state.get("policy_read_failed") or arbitration.get("policy_read_failed"):
        return "policy_read_failed"

    last_notified = _aware_datetime(topic_state.get("last_notified_at"))
    if last_notified is not None and now - last_notified < machine.TOPIC_COOLDOWN:
        return "recent_notification_same_topic"
    topic_cap = _safe_nonnegative_int(
        node.get("notification_topic_cap", machine.DEFAULT_TOPIC_CAP),
        machine.DEFAULT_TOPIC_CAP,
    )
    if _safe_nonnegative_int(topic_state.get("notify_count")) >= topic_cap:
        return "per_topic_cap"
    if draft.event_id:
        event_counts = topic_state.get("event_notify_counts") or {}
        if (
            _safe_nonnegative_int(event_counts.get(draft.event_id))
            >= machine.UPCOMING_EVENT_CAP
        ):
            return "per_event_cap"

    window_started = _aware_datetime(arbitration.get("fatigue_window_started_at"))
    if (
        window_started is not None
        and now - window_started < machine.GLOBAL_FATIGUE_WINDOW
        and _safe_nonnegative_int(arbitration.get("proactive_sent_24h"))
        >= machine.GLOBAL_FATIGUE_CAP
    ):
        return "global_fatigue_cap"
    return None


async def _read_sweep_inputs(uid: str) -> list[tuple[str, dict[str, Any]]]:
    """One consent read plus at most 24 graph-node reads for an active user."""
    def _read() -> list[tuple[str, dict[str, Any]]]:
        db = admin_firestore()
        user_snap = db.collection("users").document(uid).get()
        user = (user_snap.to_dict() or {}) if user_snap.exists else {}
        if user.get("aura_consent_granted") is not True:
            return []
        nodes = (
            db.collection(GF.PARENT_COLLECTION)
            .document(uid)
            .collection(GF.NODE_SUBCOLLECTION)
            .where(filter=FieldFilter(
                GF.STATUS,
                "in",
                [GF.NODE_STATUS_ACTIVE, GF.NODE_STATUS_DORMANT],
            ))
            .order_by(GF.WEIGHT, direction="DESCENDING")
            .limit(NODE_PREFETCH_LIMIT)
            .stream()
        )
        return [(snap.id, snap.to_dict() or {}) for snap in nodes]

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("memory_graph_notifications: sweep read failed open", {
            "user_id": uid,
            "error": str(exc),
        })
        return []


async def sweep_user(
    uid: str, *, now: datetime, dry_run: bool = False
) -> CandidateDraft | None:
    nodes = await _read_sweep_inputs(uid)
    drafts = [
        draft
        for node_id, node in nodes
        for draft in candidate_drafts_for_node(uid, node_id, node, now=now)
    ]
    drafts.sort(
        key=lambda candidate: (candidate.score, -candidate.fire_at.timestamp()),
        reverse=True,
    )
    node_by_id = dict(nodes)
    for draft in drafts:
        topic_state, arbitration = await machine.read_policy_state(uid, draft.topic_id)
        if hard_zero_reason(
            node_by_id[draft.node_id], draft, topic_state, arbitration, now=now
        ) is not None:
            continue
        if dry_run:
            return draft
        if await machine.install_candidate(uid, draft):
            return draft
    return None


async def run_memory_graph_sweep(
    *, now: datetime | None = None, dry_run: bool = False
) -> list[tuple[str, CandidateDraft]]:
    """Evaluate at most one source A/B candidate per active, consented user."""
    if not settings.NOTIF_GRAPH:
        return []
    when = now or datetime.now(UTC)
    user_ids = await list_active_user_ids()
    if not user_ids:
        return []
    semaphore = asyncio.Semaphore(5)

    async def _one(uid: str) -> tuple[str, CandidateDraft] | None:
        async with semaphore:
            draft = await sweep_user(uid, now=when, dry_run=dry_run)
            return (uid, draft) if draft is not None else None

    results = await asyncio.gather(*[_one(uid) for uid in user_ids])
    return [result for result in results if result is not None]


async def _read_revalidation_inputs(
    uid: str, candidate: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], bool]:
    node_id = str(candidate.get("node_id") or "")
    topic_id = str(candidate.get("topic_id") or "")

    def _read() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], bool]:
        db = admin_firestore()
        user_snap, node_snap = db.get_all([
            db.collection("users").document(uid),
            db.collection(GF.PARENT_COLLECTION)
            .document(uid)
            .collection(GF.NODE_SUBCOLLECTION)
            .document(node_id),
        ])
        user = (user_snap.to_dict() or {}) if user_snap.exists else {}
        node = (node_snap.to_dict() or {}) if node_snap.exists else {}
        return user, node, {}, user.get("aura_consent_granted") is True

    user, node, _, consent = await asyncio.to_thread(_read)
    topic, arbitration = await machine.read_policy_state(uid, topic_id)
    return user, node, topic, arbitration, consent


def _draft_from_candidate(candidate: dict[str, Any]) -> CandidateDraft:
    return CandidateDraft(
        candidate_id=str(candidate["candidate_id"]),
        topic_id=str(candidate["topic_id"]),
        source=str(candidate["source"]),
        project_id=str(candidate.get("project_id") or "") or None,
        node_id=str(candidate["node_id"]),
        event_id=str(candidate.get("event_id") or "") or None,
        value_payload=dict(candidate.get("value_payload") or {}),
        evidence=dict(candidate.get("evidence") or {}),
        score=float(candidate.get("score", 0.0) or 0.0),
        fire_at=_aware_datetime(candidate.get("fire_at")) or datetime.now(UTC),
        expires_at=_aware_datetime(candidate.get("expires_at")) or datetime.now(UTC),
    )


def _effective_score(
    candidate: dict[str, Any], arbitration: dict[str, Any], now: datetime
) -> float:
    score = float(candidate.get("score", 0.0) or 0.0)
    project_id = str(candidate.get("project_id") or "")
    last_delivered = _aware_datetime(
        (arbitration.get("project_last_delivered") or {}).get(project_id)
    )
    if last_delivered is not None and now - last_delivered < machine.TOPIC_COOLDOWN:
        score -= PROJECT_RECENCY_PENALTY
    return score


async def process_candidate(uid: str, candidate_id: str, *, now: datetime) -> None:
    candidate = await machine.claim_for_revalidation(uid, candidate_id, now=now)
    if candidate is None:
        return
    draft = _draft_from_candidate(candidate)
    if now >= draft.expires_at:
        await machine.transition_terminal(
            uid, candidate_id, machine.STATE_EXPIRED, "max_age", now=now
        )
        return
    try:
        _, node, topic, arbitration, consent = await _read_revalidation_inputs(uid, candidate)
    except Exception as exc:
        logger.warn("memory_graph_notifications: revalidation read failed", {
            "user_id": uid,
            "candidate_id": candidate_id,
            "error": str(exc),
        })
        await machine.defer_candidate(
            uid,
            candidate_id,
            "revalidation_read_failed",
            fire_at=min(now + FRAMING_RETRY_DELAY, draft.expires_at),
            now=now,
        )
        return
    if not consent or not node:
        await machine.transition_terminal(
            uid, candidate_id, machine.STATE_CANCELED, "consent_or_node_missing", now=now
        )
        return
    zero = hard_zero_reason(node, draft, topic, arbitration, now=now)
    if zero is not None:
        await machine.transition_terminal(
            uid, candidate_id, machine.STATE_SUPPRESSED, zero, now=now
        )
        return

    framed = await frame_memory_graph_notification(draft.value_payload)
    if framed is None:
        await machine.defer_candidate(
            uid,
            candidate_id,
            "framing_retry",
            fire_at=min(now + FRAMING_RETRY_DELAY, draft.expires_at),
            now=now,
        )
        return
    effective_score = _effective_score(candidate, arbitration, now)
    reserved, _ = await machine.reserve_delivery(
        uid,
        candidate_id,
        effective_score=effective_score,
        now=now,
    )
    if not reserved:
        return

    proposal = NotificationProposal(
        user_id=uid,
        source=SOURCE_MEMORY_GRAPH,
        kind=ProposalKind.PROACTIVE,
        dedup_key=candidate_id,
        title=framed.title,
        body=framed.body,
        notification_type="memory_graph",
        collapse_key=f"memory_graph_{draft.topic_id}",
        priority=65,
        data={
            "candidate_id": candidate_id,
            "topic_id": draft.topic_id,
            "project_id": draft.project_id or "",
            "candidate_source": draft.source,
        },
    )
    try:
        decision = await orchestrator.submit(proposal, now=now)
    except Exception as exc:
        logger.warn("memory_graph_notifications: submit failed", {
            "user_id": uid,
            "candidate_id": candidate_id,
            "error": str(exc),
        })
        await machine.defer_candidate(
            uid,
            candidate_id,
            "submit_failed",
            fire_at=min(now + ORCHESTRATOR_RETRY_DELAY, draft.expires_at),
            now=now,
        )
        return
    if decision.disposition == Disposition.DROP:
        await on_orchestrator_outcome(proposal, decision, now=now)


async def run_due_candidates(*, now: datetime | None = None) -> int:
    if not settings.NOTIF_GRAPH:
        return 0
    when = now or datetime.now(UTC)
    due = await machine.list_due_candidates(now=when)
    grouped: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for uid, candidate_id, candidate in due:
        grouped[uid].append((candidate_id, candidate))
    processed = 0
    for uid, candidates in grouped.items():
        arbitration = await machine.read_arbitration_state(uid)
        candidates.sort(
            key=lambda item: _effective_score(item[1], arbitration, when),
            reverse=True,
        )
        for candidate_id, _ in candidates:
            await process_candidate(uid, candidate_id, now=when)
            processed += 1
    return processed


async def on_orchestrator_outcome(
    proposal: NotificationProposal,
    decision: OrchestratorDecision,
    *,
    now: datetime | None = None,
) -> None:
    """Map the existing funnel outcome back onto the shared candidate machine."""
    candidate_id = proposal.data.get("candidate_id", "")
    if proposal.source != SOURCE_MEMORY_GRAPH or not candidate_id:
        return
    when = now or datetime.now(UTC)
    if decision.disposition == Disposition.SEND:
        if decision.delivered:
            await machine.mark_delivered(proposal.user_id, candidate_id, now=when)
        else:
            await machine.defer_candidate(
                proposal.user_id,
                candidate_id,
                "delivery_not_confirmed",
                fire_at=when + ORCHESTRATOR_RETRY_DELAY,
                now=when,
            )
    elif decision.disposition == Disposition.HOLD:
        await machine.defer_candidate(
            proposal.user_id,
            candidate_id,
            decision.reason,
            fire_at=when + ORCHESTRATOR_RETRY_DELAY,
            now=when,
        )
    elif decision.reason == REASON_STALE:
        await machine.transition_terminal(
            proposal.user_id,
            candidate_id,
            machine.STATE_EXPIRED,
            decision.reason,
            now=when,
        )
    else:
        await machine.transition_terminal(
            proposal.user_id,
            candidate_id,
            machine.STATE_SUPPRESSED,
            decision.reason,
            now=when,
        )
