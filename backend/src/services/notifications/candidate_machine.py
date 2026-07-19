"""Shared durable state machine for inferred proactive notification candidates.

Candidate identity, evidence, score, payload, and generation are immutable after
creation. Only lifecycle fields such as state, fire_at, attempts, and transition
timestamps change while the candidate moves toward a confirmed delivery.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud import firestore as fs

from ...lib.logger import logger
from ..firebase import admin_firestore
from ..memory import graph_fields as GF

CANDIDATE_SUBCOLLECTION = "notif_candidates"
TOPIC_STATE_SUBCOLLECTION = "topic_notification_state"
ARBITRATION_SUBCOLLECTION = "notification_arbitration"
ARBITRATION_DOC_ID = "proactive"

STATE_PENDING = "pending"
STATE_SCHEDULED = "scheduled"
STATE_REVALIDATING = "revalidating"
STATE_SUBMITTED = "submitted"
STATE_DELIVERED = "delivered"
STATE_DEFERRED = "deferred"
STATE_SUPPRESSED = "suppressed"
STATE_CANCELED = "canceled"
STATE_EXPIRED = "expired"
STATE_SHADOW = "shadow"

ACTIVE_STATES = frozenset({
    STATE_PENDING,
    STATE_SCHEDULED,
    STATE_REVALIDATING,
    STATE_SUBMITTED,
    STATE_DEFERRED,
    STATE_SHADOW,
})
TERMINAL_STATES = frozenset({
    STATE_DELIVERED,
    STATE_SUPPRESSED,
    STATE_CANCELED,
    STATE_EXPIRED,
})
DUE_STATES = [STATE_SCHEDULED, STATE_DEFERRED]

COLLISION_WINDOW = timedelta(hours=2)
RESERVATION_RETRY_DELAY = timedelta(minutes=1)
TOPIC_COOLDOWN = timedelta(hours=72)
GLOBAL_FATIGUE_WINDOW = timedelta(hours=24)
GLOBAL_FATIGUE_CAP = 3
DEFAULT_TOPIC_CAP = 3
UPCOMING_EVENT_CAP = 2


@dataclass(frozen=True)
class CandidateDraft:
    candidate_id: str
    topic_id: str
    source: str
    project_id: str | None
    node_id: str
    event_id: str | None
    value_payload: dict[str, Any]
    evidence: dict[str, Any]
    score: float
    fire_at: datetime
    expires_at: datetime
    session_id: str | None = None
    input_revision: int | None = None
    evaluator_version: str | None = None
    lineage_chain: tuple[str, ...] = ()
    initial_state: str = STATE_SCHEDULED


def candidate_id_for(uid: str, topic_id: str, source: str, event_key: str) -> str:
    raw = f"{uid}|{topic_id}|{source}|{event_key}"
    return f"cand_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:32]}"


def topic_id_for(node_id: str) -> str:
    digest = hashlib.sha1(node_id.encode("utf-8")).hexdigest()[:24]
    return f"topic_{digest}"


def _user_ref(uid: str):
    return admin_firestore().collection(GF.PARENT_COLLECTION).document(uid)


def _candidate_ref(uid: str, candidate_id: str):
    return _user_ref(uid).collection(CANDIDATE_SUBCOLLECTION).document(candidate_id)


def _topic_ref(uid: str, topic_id: str):
    return _user_ref(uid).collection(TOPIC_STATE_SUBCOLLECTION).document(topic_id)


def _arbitration_ref(uid: str):
    return _user_ref(uid).collection(ARBITRATION_SUBCOLLECTION).document(
        ARBITRATION_DOC_ID
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _candidate_document(draft: CandidateDraft, generation: int, now: datetime) -> dict[str, Any]:
    document = {
        **asdict(draft),
        "generation": generation,
        "state": draft.initial_state,
        "created_at": now,
        "attempts": 0,
        "lineage_chain": list(draft.lineage_chain),
        "last_transition": now,
        "state_history": [STATE_PENDING, draft.initial_state],
    }
    document.pop("initial_state", None)
    for optional_field in ("session_id", "input_revision", "evaluator_version"):
        if document.get(optional_field) is None:
            document.pop(optional_field, None)
    return document


async def install_candidate(uid: str, draft: CandidateDraft) -> bool:
    """Create one immutable generation and atomically select the active topic winner."""
    now = datetime.now(UTC)

    def _install() -> bool:
        db = admin_firestore()
        incoming_ref = _candidate_ref(uid, draft.candidate_id)
        topic_ref = _topic_ref(uid, draft.topic_id)
        transaction = db.transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> bool:
            topic_snap = topic_ref.get(transaction=txn)
            topic = (topic_snap.to_dict() or {}) if topic_snap.exists else {}
            incoming_snap = incoming_ref.get(transaction=txn)
            if incoming_snap.exists:
                return topic.get("active_candidate_id") == draft.candidate_id

            active_id = str(topic.get("active_candidate_id") or "")
            active_ref = _candidate_ref(uid, active_id) if active_id else None
            active_snap = active_ref.get(transaction=txn) if active_ref else None
            active = (
                (active_snap.to_dict() or {})
                if active_snap is not None and active_snap.exists
                else {}
            )
            generation = max(0, int(topic.get("latest_generation", 0) or 0)) + 1
            active_state = str(active.get("state") or "")
            active_score = float(active.get("score", 0.0) or 0.0)
            replaces = (
                not active_id
                or not active
                or active_state not in ACTIVE_STATES
                or draft.score > active_score
                or (
                    bool(draft.session_id)
                    and active.get("session_id") == draft.session_id
                    and active_id != draft.candidate_id
                )
            )

            incoming = _candidate_document(draft, generation, now)
            if not replaces:
                incoming["state"] = STATE_CANCELED
                incoming["last_transition"] = now
                incoming["state_history"] = [STATE_PENDING, STATE_CANCELED]
                incoming["terminal_reason"] = "lower_score_replacement_loser"
            txn.set(incoming_ref, incoming)

            if replaces:
                if active_ref is not None and active_state in ACTIVE_STATES:
                    txn.update(active_ref, {
                        "state": STATE_CANCELED,
                        "last_transition": now,
                        "terminal_reason": "superseded",
                    })
                txn.set(topic_ref, {
                    "topic_id": draft.topic_id,
                    "project_id": draft.project_id,
                    "active_candidate_id": draft.candidate_id,
                    "active_score": draft.score,
                    "latest_generation": generation,
                }, merge=True)
            else:
                txn.set(topic_ref, {"latest_generation": generation}, merge=True)
            return replaces

        return _apply(transaction)

    try:
        return await asyncio.to_thread(_install)
    except Exception as exc:
        logger.warn("candidate_machine: replacement transaction failed", {
            "user_id": uid,
            "candidate_id": draft.candidate_id,
            "error": str(exc),
        })
        return False


async def list_due_candidates(
    *, now: datetime | None = None, limit: int = 100
) -> list[tuple[str, str, dict[str, Any]]]:
    """Discover due candidates globally with one indexed collection-group query."""
    when = now or datetime.now(UTC)

    def _read() -> list[tuple[str, str, dict[str, Any]]]:
        snaps = list(
            admin_firestore()
            .collection_group(CANDIDATE_SUBCOLLECTION)
            .where(filter=fs.FieldFilter("state", "in", DUE_STATES))
            .where(filter=fs.FieldFilter("fire_at", "<=", when))
            .order_by("fire_at")
            .limit(limit)
            .stream()
        )
        if len(snaps) >= limit:
            logger.warn("candidate_machine: due query hit limit", {"limit": limit})
        result: list[tuple[str, str, dict[str, Any]]] = []
        for snap in snaps:
            user_ref = snap.reference.parent.parent
            if user_ref is not None:
                result.append((user_ref.id, snap.id, snap.to_dict() or {}))
        return result

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("candidate_machine: due query failed open", {"error": str(exc)})
        return []


async def claim_for_revalidation(
    uid: str, candidate_id: str, *, now: datetime
) -> dict[str, Any] | None:
    """CAS a due active candidate into revalidating and return its current data."""
    def _claim() -> dict[str, Any] | None:
        ref = _candidate_ref(uid, candidate_id)
        db = admin_firestore()
        transaction = db.transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> dict[str, Any] | None:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return None
            data = snap.to_dict() or {}
            state = str(data.get("state") or "")
            fire_at = data.get("fire_at")
            if (
                state not in DUE_STATES
                or not isinstance(fire_at, datetime)
                or _aware(fire_at) > now
            ):
                return None
            actual_topic_ref = _topic_ref(uid, str(data.get("topic_id") or ""))
            topic_snap = actual_topic_ref.get(transaction=txn)
            topic = (topic_snap.to_dict() or {}) if topic_snap.exists else {}
            if topic.get("active_candidate_id") != candidate_id:
                txn.update(ref, {
                    "state": STATE_CANCELED,
                    "last_transition": now,
                    "terminal_reason": "inactive_pointer",
                })
                return None
            txn.update(ref, {
                "state": STATE_REVALIDATING,
                "last_transition": now,
                "attempts": fs.Increment(1),
            })
            data["state"] = STATE_REVALIDATING
            return data

        return _apply(transaction)

    try:
        return await asyncio.to_thread(_claim)
    except Exception as exc:
        logger.warn("candidate_machine: revalidation claim failed", {
            "user_id": uid,
            "candidate_id": candidate_id,
            "error": str(exc),
        })
        return None


async def reserve_delivery(
    uid: str,
    candidate_id: str,
    *,
    effective_score: float,
    now: datetime,
) -> tuple[bool, datetime | None]:
    """Authoritatively reserve one per-user submission slot in a transaction."""
    def _reserve() -> tuple[bool, datetime | None]:
        candidate_ref = _candidate_ref(uid, candidate_id)
        arbitration_ref = _arbitration_ref(uid)
        transaction = admin_firestore().transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> tuple[bool, datetime | None]:
            candidate_snap = candidate_ref.get(transaction=txn)
            arbitration_snap = arbitration_ref.get(transaction=txn)
            if not candidate_snap.exists:
                return False, None
            candidate = candidate_snap.to_dict() or {}
            arbitration = (
                (arbitration_snap.to_dict() or {}) if arbitration_snap.exists else {}
            )
            reserved_id = str(arbitration.get("reserved_candidate_id") or "")
            reserved_until = arbitration.get("reserved_until")
            if (
                reserved_id
                and reserved_id != candidate_id
                and isinstance(reserved_until, datetime)
                and _aware(reserved_until) > now
            ):
                retry_at = _aware(reserved_until) + RESERVATION_RETRY_DELAY
                txn.update(candidate_ref, {
                    "state": STATE_DEFERRED,
                    "fire_at": retry_at,
                    "last_transition": now,
                    "deferred_reason": "reservation_lost",
                })
                return False, retry_at
            if candidate.get("state") != STATE_REVALIDATING:
                return False, None

            until = now + COLLISION_WINDOW
            txn.set(arbitration_ref, {
                "reserved_candidate_id": candidate_id,
                "reserved_until": until,
                "reserved_score": effective_score,
                "updated_at": now,
            }, merge=True)
            txn.update(candidate_ref, {
                "state": STATE_SUBMITTED,
                "last_transition": now,
            })
            return True, until

        return _apply(transaction)

    try:
        return await asyncio.to_thread(_reserve)
    except Exception as exc:
        logger.warn("candidate_machine: reservation transaction failed", {
            "user_id": uid,
            "candidate_id": candidate_id,
            "error": str(exc),
        })
        return False, None


async def dry_run_reservation(
    uid: str,
    candidate_id: str,
    *,
    now: datetime,
) -> bool:
    """Compute the collision-window winner in a read-only transaction."""
    def _reserve() -> bool:
        db = admin_firestore()
        candidate_collection = _user_ref(uid).collection(CANDIDATE_SUBCOLLECTION)
        arbitration_ref = _arbitration_ref(uid)
        transaction = db.transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> bool:
            arbitration_snap = arbitration_ref.get(transaction=txn)
            arbitration = (
                (arbitration_snap.to_dict() or {}) if arbitration_snap.exists else {}
            )
            reserved_id = str(arbitration.get("reserved_candidate_id") or "")
            reserved_until = arbitration.get("reserved_until")
            if (
                reserved_id
                and reserved_id != candidate_id
                and isinstance(reserved_until, datetime)
                and _aware(reserved_until) > now
            ):
                return False

            competitors = [
                snap.to_dict() or {}
                for snap in (
                    candidate_collection
                    .where(filter=fs.FieldFilter("state", "in", [
                        STATE_SCHEDULED,
                        STATE_SHADOW,
                    ]))
                    .where(filter=fs.FieldFilter(
                        "fire_at", ">=", now - COLLISION_WINDOW
                    ))
                    .where(filter=fs.FieldFilter(
                        "fire_at", "<=", now + COLLISION_WINDOW
                    ))
                    .limit(50)
                    .stream(transaction=txn)
                )
            ]
            project_last = arbitration.get("project_last_delivered") or {}

            def _effective(item: dict[str, Any]) -> tuple[float, str]:
                score = float(item.get("score", 0.0) or 0.0)
                project_id = str(item.get("project_id") or "")
                delivered = project_last.get(project_id)
                if (
                    isinstance(delivered, datetime)
                    and now - _aware(delivered) < TOPIC_COOLDOWN
                ):
                    score -= 0.15
                return score, str(item.get("candidate_id") or "")

            if not competitors:
                return False
            winner = max(competitors, key=_effective)
            return winner.get("candidate_id") == candidate_id

        return _apply(transaction)

    try:
        return await asyncio.to_thread(_reserve)
    except Exception as exc:
        logger.warn("candidate_machine: dry reservation transaction failed closed", {
            "user_id": uid,
            "candidate_id": candidate_id,
            "error": str(exc),
        })
        return False


async def read_policy_state(uid: str, topic_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read the topic cooldown/caps and per-user fatigue state together."""
    def _read() -> tuple[dict[str, Any], dict[str, Any]]:
        topic_snap, arbitration_snap = admin_firestore().get_all([
            _topic_ref(uid, topic_id),
            _arbitration_ref(uid),
        ])
        return (
            (topic_snap.to_dict() or {}) if topic_snap.exists else {},
            (arbitration_snap.to_dict() or {}) if arbitration_snap.exists else {},
        )

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("candidate_machine: policy state failed closed", {
            "user_id": uid,
            "topic_id": topic_id,
            "error": str(exc),
        })
        return {"policy_read_failed": True}, {"policy_read_failed": True}


async def read_arbitration_state(uid: str) -> dict[str, Any]:
    """Read project-recency and fatigue data for advisory winner ordering."""
    try:
        snap = await asyncio.to_thread(_arbitration_ref(uid).get)
        return (snap.to_dict() or {}) if snap.exists else {}
    except Exception as exc:
        logger.warn("candidate_machine: arbitration read failed open", {
            "user_id": uid,
            "error": str(exc),
        })
        return {}


async def transition_terminal(
    uid: str,
    candidate_id: str,
    state: str,
    reason: str,
    *,
    now: datetime | None = None,
) -> None:
    if state not in TERMINAL_STATES:
        raise ValueError(f"not a terminal candidate state: {state}")
    when = now or datetime.now(UTC)

    def _update() -> None:
        ref = _candidate_ref(uid, candidate_id)
        snap = ref.get()
        if not snap.exists or str((snap.to_dict() or {}).get("state")) in TERMINAL_STATES:
            return
        ref.update({
            "state": state,
            "last_transition": when,
            "terminal_reason": reason,
        })

    try:
        await asyncio.to_thread(_update)
    except Exception as exc:
        logger.warn("candidate_machine: terminal transition failed", {
            "user_id": uid,
            "candidate_id": candidate_id,
            "state": state,
            "error": str(exc),
        })


async def defer_candidate(
    uid: str,
    candidate_id: str,
    reason: str,
    *,
    fire_at: datetime,
    now: datetime | None = None,
) -> None:
    when = now or datetime.now(UTC)
    try:
        await asyncio.to_thread(
            _candidate_ref(uid, candidate_id).update,
            {
                "state": STATE_DEFERRED,
                "fire_at": fire_at,
                "last_transition": when,
                "deferred_reason": reason,
            },
        )
    except Exception as exc:
        logger.warn("candidate_machine: defer failed", {
            "user_id": uid,
            "candidate_id": candidate_id,
            "error": str(exc),
        })


async def mark_delivered(uid: str, candidate_id: str, *, now: datetime | None = None) -> bool:
    """Confirm delivery and only then advance cooldown, caps, and fatigue."""
    when = now or datetime.now(UTC)

    def _deliver() -> bool:
        candidate_ref = _candidate_ref(uid, candidate_id)
        transaction = admin_firestore().transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> bool:
            candidate_snap = candidate_ref.get(transaction=txn)
            if not candidate_snap.exists:
                return False
            candidate = candidate_snap.to_dict() or {}
            if candidate.get("state") == STATE_DELIVERED:
                return True
            if candidate.get("state") != STATE_SUBMITTED:
                return False
            topic_ref = _topic_ref(uid, str(candidate.get("topic_id") or ""))
            arbitration_ref = _arbitration_ref(uid)
            topic_snap = topic_ref.get(transaction=txn)
            arbitration_snap = arbitration_ref.get(transaction=txn)
            topic = (topic_snap.to_dict() or {}) if topic_snap.exists else {}
            arbitration = (
                (arbitration_snap.to_dict() or {}) if arbitration_snap.exists else {}
            )

            event_counts = dict(topic.get("event_notify_counts") or {})
            event_id = str(candidate.get("event_id") or "")
            if event_id:
                event_counts[event_id] = int(event_counts.get(event_id, 0) or 0) + 1
            txn.update(candidate_ref, {
                "state": STATE_DELIVERED,
                "last_transition": when,
                "delivered_at": when,
            })
            txn.set(topic_ref, {
                "last_notified_at": when,
                "notify_count": int(topic.get("notify_count", 0) or 0) + 1,
                "last_dedup_key": str(candidate.get("topic_id") or ""),
                "event_notify_counts": event_counts,
            }, merge=True)

            window_started = arbitration.get("fatigue_window_started_at")
            sent_count = int(arbitration.get("proactive_sent_24h", 0) or 0)
            if (
                not isinstance(window_started, datetime)
                or when - _aware(window_started) >= GLOBAL_FATIGUE_WINDOW
            ):
                window_started = when
                sent_count = 0
            project_deliveries = dict(arbitration.get("project_last_delivered") or {})
            project_id = str(candidate.get("project_id") or "")
            if project_id:
                project_deliveries[project_id] = when
            txn.set(arbitration_ref, {
                "fatigue_window_started_at": window_started,
                "proactive_sent_24h": sent_count + 1,
                "project_last_delivered": project_deliveries,
                "updated_at": when,
            }, merge=True)
            return True

        return _apply(transaction)

    try:
        return await asyncio.to_thread(_deliver)
    except Exception as exc:
        logger.warn("candidate_machine: delivery confirmation failed", {
            "user_id": uid,
            "candidate_id": candidate_id,
            "error": str(exc),
        })
        return False
