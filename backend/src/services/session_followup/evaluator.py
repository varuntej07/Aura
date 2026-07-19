"""Offline Phase 6 session-finalization evaluator."""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from ...config.settings import settings
from ...lib.logger import logger
from ..firebase import admin_firestore
from ..memory import graph_fields as GF
from ..memory.salience import normalized_graph_salience
from ..notifications import candidate_machine as machine
from ..notifications.candidate_machine import CandidateDraft
from . import fields as F
from .clustering import cluster_turns

_FUTURE_INTENT = re.compile(
    r"\b(i(?:'m| am)? going to|i plan to|i want to|later|next week|tomorrow)\b",
    re.I,
)
_UNRESOLVED_ACTION = re.compile(
    r"\b(need to|have to|still need|figure out|follow up|todo|to-do)\b",
    re.I,
)
_NEXT_STEP = re.compile(r"\b(next step|where do i start|how should i|what should i do)\b", re.I)
_DEADLINE = re.compile(
    r"\b(deadline|due|by (?:monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|tomorrow))\b",
    re.I,
)
_SHALLOW_FACT = re.compile(r"^\s*(what|who|when|where)\s+(is|are|was|were)\b", re.I)
_SENSITIVE = re.compile(
    r"\b(ssn|social security|bank account|credit card|diagnosis|pregnan|sexual|"
    r"therapy|medication|medical record|passport number)\b",
    re.I,
)
_ACTION_STOP_WORDS = frozenset({
    "about", "after", "again", "before", "could", "from", "have", "into",
    "just", "later", "need", "should", "that", "their", "there", "these",
    "they", "this", "want", "what", "when", "where", "which", "with", "would",
})

W_TURNS = 0.18
W_DEPTH = 0.12
W_INTENT = 0.18
W_ACTION = 0.24
W_NEXT = 0.14
W_CONN = 0.07
W_EDGE = 0.05
W_REPEAT = 0.04
P_SHALLOW = 0.25
P_INTRO = 0.15
TURN_CAP = 6
DEPTH_CAP = 5


def candidate_id_for(
    session_id: str, topic_id: str, input_revision: int, evaluator_version: str
) -> str:
    raw = f"{session_id}|{topic_id}|{input_revision}|{evaluator_version}"
    return f"cand_{hashlib.sha1(raw.encode('utf-8')).hexdigest()}"


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


def _topic_text(topic: dict[str, Any]) -> str:
    parts = [str(topic.get("summary") or "")]
    for turn in topic.get("turns") or []:
        parts.append(str(turn.get("text") or turn.get("transcript") or ""))
        terms = turn.get("lexical_terms")
        if isinstance(terms, list):
            parts.append(" ".join(str(term) for term in terms))
    return " ".join(parts).strip()


def _action_terms(value: str) -> set[str]:
    return {
        term
        for term in re.findall(r"[a-z0-9]{3,}", value.lower().replace("_", " "))
        if term not in _ACTION_STOP_WORDS
    }


def _action_matches_topic(action: dict[str, Any], topic: dict[str, Any]) -> bool:
    topic_terms = _action_terms(_topic_text(topic))
    action_text = " ".join(
        str(action.get(field) or "") for field in ("message", "subject", "question")
    )
    action_terms = _action_terms(action_text)
    shared = topic_terms & action_terms
    if not shared:
        return False
    return len(shared) >= 2 or len(action_terms) == 1


def _signals(topic: dict[str, Any]) -> dict[str, Any]:
    turns = list(topic.get("turns") or [])
    text = _topic_text(topic)
    def explicit(name: str) -> bool:
        return any(turn.get(name) is True for turn in turns)
    future_intent = explicit("future_intent") or bool(_FUTURE_INTENT.search(text))
    unresolved_action = explicit("unresolved_action") or bool(_UNRESOLVED_ACTION.search(text))
    next_step = explicit("next_step") or bool(_NEXT_STEP.search(text))
    sensitive = explicit("inferred_sensitive") or bool(_SENSITIVE.search(text))
    reminder = explicit("reminder_created_in_session")
    completed_action = explicit("completed_action")
    shallow = len(turns) <= 1 and bool(_SHALLOW_FACT.search(text))
    assistant_intro = explicit("assistant_introduced") and len(turns) <= 1
    depth = max(
        [int(turn.get("follow_up_depth", 0) or 0) for turn in turns] or [len(turns) - 1]
    )
    return {
        "text": text,
        "future_intent": future_intent,
        "unresolved_action": unresolved_action,
        "next_step": next_step,
        "sensitive": sensitive,
        "reminder_created_in_session": reminder,
        "completed_action": completed_action,
        "shallow_factual": shallow,
        "assistant_introduced": assistant_intro,
        "follow_up_depth": max(0, depth),
    }


def _value_payload(signals: dict[str, Any], topic: dict[str, Any]) -> dict[str, Any] | None:
    evidence = str(topic.get("summary") or signals.get("text") or "").strip()[:220]
    if not evidence:
        return None
    text = str(signals.get("text") or "")
    if signals["unresolved_action"]:
        payload_type = "unresolved_action"
    elif _DEADLINE.search(text):
        payload_type = "deadline"
    elif signals["next_step"] or signals["future_intent"]:
        payload_type = "next_step"
    elif any(turn.get("new_information") is True for turn in topic.get("turns") or []):
        payload_type = "new_information"
    elif len(topic.get("entity_keys") or []) >= 2:
        payload_type = "cross_memory_connection"
    else:
        return None
    return {
        "type": payload_type,
        "evidence": evidence,
        "artifact_ref": None,
    }


def score_topic(
    topic: dict[str, Any],
    signals: dict[str, Any],
    graph_nodes: list[dict[str, Any]],
) -> float:
    turns = int(topic.get("user_turn_count", 0) or 0)
    depth = int(signals.get("follow_up_depth", 0) or 0)
    connection = max(
        [normalized_graph_salience(node) for node in graph_nodes] or [0.0]
    )
    edge = any(node.get(GF.NEW_STRONG_EDGE_EVIDENCE) for node in graph_nodes)
    repeated = any(float(node.get(GF.WEIGHT, 0.0) or 0.0) > 1.0 for node in graph_nodes)
    score = (
        W_TURNS * min(turns, TURN_CAP) / TURN_CAP
        + W_DEPTH * min(depth, DEPTH_CAP) / DEPTH_CAP
        + W_INTENT * float(signals["future_intent"])
        + W_ACTION * float(signals["unresolved_action"])
        + W_NEXT * float(signals["next_step"])
        + W_CONN * min(connection, 1.0)
        + W_EDGE * float(edge)
        + W_REPEAT * float(repeated)
        - P_SHALLOW * float(signals["shallow_factual"])
        - P_INTRO * float(signals["assistant_introduced"])
    )
    return round(max(0.0, min(1.0, score)), 6)


def _jitter_minutes(session_id: str, topic_id: str, revision: int, version: str) -> int:
    digest = hashlib.sha1(
        f"{session_id}|{topic_id}|{revision}|{version}|jitter".encode()
    ).digest()
    return 55 + int.from_bytes(digest[:2], "big") % 21


async def _read_inputs(
    uid: str, session_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    def _read() -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        db = admin_firestore()
        session_ref = (
            db.collection(GF.PARENT_COLLECTION)
            .document(uid)
            .collection(F.SESSIONS)
            .document(session_id)
        )
        session_snap = session_ref.get()
        user_snap = db.collection("users").document(uid).get()
        turns = [
            snap.to_dict() or {}
            for snap in session_ref.collection(F.TURNS).order_by("turn_index").stream()
        ]
        return (
            (session_snap.to_dict() or {}) if session_snap.exists else {},
            turns,
            (user_snap.to_dict() or {}) if user_snap.exists else {},
        )

    return await asyncio.to_thread(_read)


async def _read_graph_nodes(uid: str, topic: dict[str, Any]) -> list[dict[str, Any]]:
    node_ids = [GF.entity_id(key) for key in topic.get("entity_keys") or []]
    if not node_ids:
        return []

    def _read() -> list[dict[str, Any]]:
        db = admin_firestore()
        collection = (
            db.collection(GF.PARENT_COLLECTION)
            .document(uid)
            .collection(GF.NODE_SUBCOLLECTION)
        )
        return [
            snap.to_dict() or {}
            for snap in db.get_all([collection.document(node_id) for node_id in node_ids])
            if snap.exists
        ]

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("session_followup: graph read failed open", {
            "user_id": uid,
            "topic_id": topic.get("topic_id"),
            "error": str(exc),
        })
        return []


async def _has_session_owned_action(uid: str, session_id: str, topic: dict[str, Any]) -> bool:
    if any(turn.get("reminder_created_in_session") is True for turn in topic.get("turns") or []):
        return True

    def _read() -> bool:
        db = admin_firestore()
        for collection_name in ("reminders", "intents"):
            query = (
                db.collection("users")
                .document(uid)
                .collection(collection_name)
                .where(filter=machine.fs.FieldFilter("session_id", "==", session_id))
                .limit(10)
            )
            for snapshot in query.stream():
                action = snapshot.to_dict() or {}
                if _action_matches_topic(action, topic):
                    return True
        return False

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("session_followup: reminder suppression read failed closed", {
            "user_id": uid,
            "session_id": session_id,
            "error": str(exc),
        })
        return True


async def _prior_finalized_count(uid: str, session: dict[str, Any]) -> int:
    explicit = session.get("prior_finalized_session_count")
    if explicit is not None:
        return max(0, int(explicit or 0))

    def _count() -> int:
        return sum(
            1
            for _ in (
                admin_firestore()
                .collection(GF.PARENT_COLLECTION)
                .document(uid)
                .collection(F.SESSIONS)
                .where(filter=machine.fs.FieldFilter("state", "==", F.STATE_FINALIZED))
                .limit(F.COLD_START_SESSION_COUNT)
                .stream()
            )
        )

    try:
        return await asyncio.to_thread(_count)
    except Exception:
        return 0


async def _read_eval_doc(uid: str, session_id: str) -> dict[str, Any]:
    ref = (
        admin_firestore()
        .collection(GF.PARENT_COLLECTION)
        .document(uid)
        .collection(F.SESSION_TOPICS)
        .document(session_id)
    )
    snap = await asyncio.to_thread(ref.get)
    return (snap.to_dict() or {}) if snap.exists else {}


async def _write_eval_doc(uid: str, session_id: str, data: dict[str, Any]) -> None:
    ref = (
        admin_firestore()
        .collection(GF.PARENT_COLLECTION)
        .document(uid)
        .collection(F.SESSION_TOPICS)
        .document(session_id)
    )
    await asyncio.to_thread(ref.set, data)


async def _reinforce_meaningful_tap(
    uid: str,
    session: dict[str, Any],
    topics: list[dict[str, Any]],
    *,
    now: datetime,
) -> None:
    if session.get("origin") != F.ORIGIN_NOTIFICATION_TAP:
        return
    origin_candidate_id = str(session.get("origin_candidate_id") or "")
    if not origin_candidate_id:
        return
    candidate_ref = (
        admin_firestore()
        .collection(GF.PARENT_COLLECTION)
        .document(uid)
        .collection(machine.CANDIDATE_SUBCOLLECTION)
        .document(origin_candidate_id)
    )
    snap = await asyncio.to_thread(candidate_ref.get)
    if not snap.exists:
        return
    origin_topic_id = str((snap.to_dict() or {}).get("topic_id") or "")
    matching = next(
        (topic for topic in topics if topic.get("topic_id") == origin_topic_id),
        None,
    )
    if matching is None:
        return
    signals = _signals(matching)
    meaningful = (
        int(matching.get("user_turn_count", 0) or 0) >= F.MIN_MEANINGFUL_TURNS
        or signals["completed_action"]
    )
    if not meaningful:
        return

    def _write() -> None:
        db = admin_firestore()
        user_ref = db.collection(GF.PARENT_COLLECTION).document(uid)
        topic_ref = user_ref.collection(machine.TOPIC_STATE_SUBCOLLECTION).document(
            origin_topic_id
        )
        topic_ref.set({
            "last_meaningful_engagement": now,
            "weight": machine.fs.Increment(1),
        }, merge=True)
        node_collection = user_ref.collection(GF.NODE_SUBCOLLECTION)
        for entity_key in matching.get("entity_keys") or []:
            node_ref = node_collection.document(GF.entity_id(entity_key))
            node_snap = node_ref.get()
            if node_snap.exists:
                node_ref.update({
                    GF.LAST_MEANINGFUL_ENGAGEMENT: now,
                    GF.WEIGHT: machine.fs.Increment(1),
                })

    await asyncio.to_thread(_write)


async def evaluate_finalized_session(
    uid: str,
    session_id: str,
    *,
    evaluator_version: str = F.EVALUATOR_VERSION,
    now: datetime | None = None,
) -> str | None:
    """Evaluate one finalized session. The exact evaluation tuple is a no-op."""
    if not F.feature_enabled(settings):
        return None
    when = now or datetime.now(UTC)
    session, turns, user = await _read_inputs(uid, session_id)
    if not session or session.get("state") != F.STATE_FINALIZED:
        return None
    revision = max(1, int(session.get("input_revision", 1) or 1))
    existing = await _read_eval_doc(uid, session_id)
    if (
        existing.get("input_revision") == revision
        and existing.get("evaluator_version") == evaluator_version
    ):
        return str(existing.get("candidate_id") or "") or None

    topics = cluster_turns(turns)
    prior_count = await _prior_finalized_count(uid, session)
    evaluated_topics: list[dict[str, Any]] = []
    lineage = list(session.get("lineage_chain") or [])
    best: tuple[float, dict[str, Any], dict[str, Any]] | None = None
    for topic in topics:
        signals = _signals(topic)
        graph_nodes = await _read_graph_nodes(uid, topic)
        reminder = await _has_session_owned_action(uid, session_id, topic)
        terminal = any(
            str(node.get(GF.STATUS) or GF.NODE_STATUS_ACTIVE)
            in {GF.NODE_STATUS_COMPLETED, GF.NODE_STATUS_ABANDONED}
            for node in graph_nodes
        )
        payload = _value_payload(signals, topic)
        score = score_topic(topic, signals, graph_nodes)
        cold_blocked = (
            prior_count < F.COLD_START_SESSION_COUNT
            and not (signals["unresolved_action"] or signals["future_intent"])
        )
        blocked_reason = None
        if signals["sensitive"]:
            blocked_reason = "inferred_sensitive"
        elif reminder:
            blocked_reason = "reminder_created_in_session"
        elif terminal:
            blocked_reason = "terminal_status"
        elif topic["topic_id"] in lineage:
            blocked_reason = "lineage_loop"
        elif cold_blocked:
            blocked_reason = "cold_start_requires_explicit_intent"
        elif payload is None:
            blocked_reason = "missing_value_payload"
        elif score < F.SCORE_THRESHOLD:
            blocked_reason = "below_threshold"
        topic_result = {
            **{key: value for key, value in topic.items() if key != "turns"},
            "node_ids": [GF.entity_id(key) for key in topic.get("entity_keys") or []],
            "follow_up_depth": signals["follow_up_depth"],
            "value_payload": payload,
            "sensitive": signals["sensitive"],
            "reminder_created_in_session": reminder,
            "score": score,
            "drop_reason": blocked_reason,
        }
        evaluated_topics.append(topic_result)
        if blocked_reason is None and (best is None or score > best[0]):
            best = (score, topic_result, signals)

    await _reinforce_meaningful_tap(uid, session, topics, now=when)
    candidate_id: str | None = None
    if best is not None and user.get("aura_consent_granted") is True:
        score, topic, _ = best
        candidate_id = candidate_id_for(
            session_id, topic["topic_id"], revision, evaluator_version
        )
        fire_at = when + timedelta(
            minutes=_jitter_minutes(
                session_id, topic["topic_id"], revision, evaluator_version
            )
        )
        draft = CandidateDraft(
            candidate_id=candidate_id,
            topic_id=topic["topic_id"],
            source=F.SOURCE_SESSION_FOLLOWUP,
            project_id=topic.get("project_id"),
            node_id=(topic.get("node_ids") or [topic["topic_id"]])[0],
            event_id=None,
            value_payload=dict(topic["value_payload"]),
            evidence={
                "summary": topic.get("summary", ""),
                "entity_keys": topic.get("entity_keys", []),
                "node_ids": topic.get("node_ids", []),
                "sensitive": False,
            },
            score=score,
            fire_at=fire_at,
            expires_at=when + F.FOLLOWUP_MAX_AGE,
            session_id=session_id,
            input_revision=revision,
            evaluator_version=evaluator_version,
            lineage_chain=tuple([*lineage, topic["topic_id"]]),
            initial_state=(
                machine.STATE_SHADOW
                if settings.FOLLOWUP_SHADOW
                else machine.STATE_SCHEDULED
            ),
        )
        installed = await machine.install_candidate(uid, draft)
        if not installed:
            candidate_id = None

    await _write_eval_doc(uid, session_id, {
        "session_id": session_id,
        "input_revision": revision,
        "evaluator_version": evaluator_version,
        "evaluated_at": when,
        "candidate_id": candidate_id,
        "topics": evaluated_topics,
    })
    if candidate_id is not None and settings.FOLLOWUP_SHADOW:
        from .revalidator import revalidate_and_submit_followup

        candidate = next(
            topic
            for topic in evaluated_topics
            if topic["topic_id"] == best[1]["topic_id"]
        )
        fire_at = when + timedelta(
            minutes=_jitter_minutes(session_id, candidate["topic_id"], revision, evaluator_version)
        )
        await revalidate_and_submit_followup(
            uid,
            candidate_id,
            expected_fire_epoch=fire_at.timestamp(),
            now=fire_at,
        )
    return candidate_id
