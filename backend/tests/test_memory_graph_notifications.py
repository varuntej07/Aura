"""Phase 4 graph notification policy, lifecycle, and transaction tests."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from src.handlers import scheduler
from src.services.memory import graph_fields as GF
from src.services.notifications import candidate_machine as machine
from src.services.notifications import memory_graph_framer as framer
from src.services.notifications import memory_graph_notifications as notifications
from src.services.notifications.proposal import (
    REASON_QUIET_HOURS,
    REASON_TAP_GATE,
    SOURCE_MEMORY_GRAPH,
    Disposition,
    NotificationProposal,
    OrchestratorDecision,
    ProposalKind,
)

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


class _Snapshot:
    def __init__(self, ref, data):
        self.reference = ref
        self.id = ref.id
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data or {})


class _DocRef:
    def __init__(self, db, path):
        self.db = db
        self.path = tuple(path)
        self.id = self.path[-1]

    @property
    def parent(self):
        return _Collection(self.db, self.path[:-1])

    def collection(self, name):
        return _Collection(self.db, (*self.path, name))

    def get(self, transaction=None):
        return _Snapshot(self, self.db.docs.get(self.path))

    def set(self, data, merge=False):
        if merge:
            self.db.docs.setdefault(self.path, {}).update(dict(data))
        else:
            self.db.docs[self.path] = dict(data)

    def update(self, data):
        current = self.db.docs.setdefault(self.path, {})
        for key, value in data.items():
            if isinstance(value, _Increment):
                current[key] = int(current.get(key, 0) or 0) + value.value
            else:
                current[key] = value


class _Collection:
    def __init__(self, db, path):
        self.db = db
        self.path = tuple(path)
        self.id = self.path[-1]

    @property
    def parent(self):
        if len(self.path) < 2:
            return None
        return _DocRef(self.db, self.path[:-1])

    def document(self, doc_id):
        return _DocRef(self.db, (*self.path, doc_id))


class _Transaction:
    def set(self, ref, data, merge=False):
        ref.set(data, merge=merge)

    def update(self, ref, data):
        ref.update(data)


class _Db:
    def __init__(self):
        self.docs = {}

    def collection(self, name):
        return _Collection(self, (name,))

    def transaction(self):
        return _Transaction()

    def get_all(self, refs):
        return [ref.get() for ref in refs]


class _Increment:
    def __init__(self, value):
        self.value = value


class _Firestore:
    Transaction = _Transaction
    Increment = _Increment
    _lock = threading.Lock()

    @staticmethod
    def transactional(func):
        def _serialized(transaction):
            with _Firestore._lock:
                return func(transaction)

        return _serialized


@pytest.fixture()
def candidate_db(monkeypatch):
    db = _Db()
    monkeypatch.setattr(machine, "admin_firestore", lambda: db)
    monkeypatch.setattr(machine, "fs", _Firestore)
    return db


def _candidate(uid: str, candidate_id: str, topic_id: str, score: float) -> machine.CandidateDraft:
    return machine.CandidateDraft(
        candidate_id=candidate_id,
        topic_id=topic_id,
        source=notifications.SOURCE_DORMANT_GOAL,
        project_id="project-1",
        node_id=f"node-{candidate_id}",
        event_id=None,
        value_payload={
            "type": "next_step",
            "evidence": "map the next step",
            "artifact_ref": None,
        },
        evidence={},
        score=score,
        fire_at=NOW,
        expires_at=NOW + timedelta(hours=6),
    )


def _doc(db: _Db, uid: str, collection: str, doc_id: str):
    return db.docs[(GF.PARENT_COLLECTION, uid, collection, doc_id)]


def test_source_a_b_delays_differ_and_edge_alone_is_evidence_only():
    node = {
        GF.STATUS: GF.NODE_STATUS_ACTIVE,
        GF.WEIGHT: 1.0,
        GF.DEGREE: 0,
        GF.DISPLAY: "launch plan",
        GF.LAST_MEANINGFUL_ENGAGEMENT: NOW - timedelta(days=6),
        GF.VALUE_PAYLOAD: {
            "type": "next_step",
            "evidence": "choose the launch order",
            "artifact_ref": None,
        },
        GF.DEADLINE: NOW + timedelta(days=3),
        GF.NEW_STRONG_EDGE_AT: NOW - timedelta(hours=1),
        GF.NEW_STRONG_EDGE_EVIDENCE: {
            "edge_type": "relates_to",
            "edge_weight": 0.9,
            "connected_display": "pricing notes",
        },
    }

    drafts = notifications.candidate_drafts_for_node("u1", "n1", node, now=NOW)
    by_source = {draft.source: draft for draft in drafts}

    assert by_source[notifications.SOURCE_DORMANT_GOAL].fire_at == NOW
    assert by_source[notifications.SOURCE_UPCOMING_EVENT].fire_at == NOW + timedelta(days=2)

    node[GF.DEADLINE] = NOW + timedelta(hours=5)
    upcoming = {
        draft.source: draft
        for draft in notifications.candidate_drafts_for_node("u1", "n1", node, now=NOW)
    }
    assert upcoming[notifications.SOURCE_UPCOMING_EVENT].fire_at == NOW + timedelta(hours=3)

    edge_only = {
        GF.STATUS: GF.NODE_STATUS_ACTIVE,
        GF.WEIGHT: 1.0,
        GF.DEGREE: 0,
        GF.NEW_STRONG_EDGE_AT: NOW,
        GF.NEW_STRONG_EDGE_EVIDENCE: node[GF.NEW_STRONG_EDGE_EVIDENCE],
    }
    assert notifications.candidate_drafts_for_node("u1", "edge-only", edge_only, now=NOW) == []


@pytest.mark.asyncio
async def test_dry_run_sweep_returns_at_most_one_candidate(monkeypatch):
    nodes = []
    for index in range(2):
        nodes.append((f"n{index}", {
            GF.STATUS: GF.NODE_STATUS_ACTIVE,
            GF.WEIGHT: 1.0 - index * 0.1,
            GF.DEGREE: 0,
            GF.LAST_MEANINGFUL_ENGAGEMENT: NOW - timedelta(days=6),
            GF.VALUE_PAYLOAD: {
                "type": "unresolved_action",
                "evidence": f"action {index}",
                "artifact_ref": None,
            },
        }))

    async def _inputs(uid):
        return nodes

    async def _policy(uid, topic_id):
        return {}, {}

    monkeypatch.setattr(notifications, "_read_sweep_inputs", _inputs)
    monkeypatch.setattr(machine, "read_policy_state", _policy)

    result = await notifications.sweep_user("u1", now=NOW, dry_run=True)

    assert result is not None
    assert result.node_id == "n0"


@pytest.mark.asyncio
async def test_replacement_transaction_keeps_one_active_candidate(candidate_db):
    low = _candidate("u1", "cand-low", "topic-1", 0.5)
    high = _candidate("u1", "cand-high", "topic-1", 0.9)

    await asyncio.gather(
        machine.install_candidate("u1", low),
        machine.install_candidate("u1", high),
    )

    topic = _doc(candidate_db, "u1", machine.TOPIC_STATE_SUBCOLLECTION, "topic-1")
    candidates = [
        _doc(candidate_db, "u1", machine.CANDIDATE_SUBCOLLECTION, "cand-low"),
        _doc(candidate_db, "u1", machine.CANDIDATE_SUBCOLLECTION, "cand-high"),
    ]
    assert topic["active_candidate_id"] == "cand-high"
    assert sum(candidate["state"] in machine.ACTIVE_STATES for candidate in candidates) == 1
    assert _doc(
        candidate_db, "u1", machine.CANDIDATE_SUBCOLLECTION, "cand-low"
    )["state"] == machine.STATE_CANCELED


@pytest.mark.asyncio
async def test_collision_reservation_has_one_winner_and_defers_loser(candidate_db):
    first = _candidate("u1", "cand-1", "topic-1", 0.8)
    second = _candidate("u1", "cand-2", "topic-2", 0.7)
    assert await machine.install_candidate("u1", first)
    assert await machine.install_candidate("u1", second)
    for candidate_id in ("cand-1", "cand-2"):
        _doc(candidate_db, "u1", machine.CANDIDATE_SUBCOLLECTION, candidate_id)[
            "state"
        ] = machine.STATE_REVALIDATING

    results = await asyncio.gather(
        machine.reserve_delivery("u1", "cand-1", effective_score=0.8, now=NOW),
        machine.reserve_delivery("u1", "cand-2", effective_score=0.7, now=NOW),
    )

    assert sum(granted for granted, _ in results) == 1
    states = {
        candidate_id: _doc(
            candidate_db, "u1", machine.CANDIDATE_SUBCOLLECTION, candidate_id
        )["state"]
        for candidate_id in ("cand-1", "cand-2")
    }
    assert sorted(states.values()) == [machine.STATE_DEFERRED, machine.STATE_SUBMITTED]
    assert machine.STATE_SUPPRESSED not in states.values()


@pytest.mark.asyncio
async def test_cooldown_advances_only_on_confirmed_delivery(candidate_db):
    delivered = _candidate("u1", "cand-delivered", "topic-delivered", 0.9)
    suppressed = _candidate("u2", "cand-suppressed", "topic-suppressed", 0.9)
    assert await machine.install_candidate("u1", delivered)
    assert await machine.install_candidate("u2", suppressed)
    _doc(candidate_db, "u1", machine.CANDIDATE_SUBCOLLECTION, "cand-delivered")[
        "state"
    ] = machine.STATE_SUBMITTED
    _doc(candidate_db, "u2", machine.CANDIDATE_SUBCOLLECTION, "cand-suppressed")[
        "state"
    ] = machine.STATE_SUBMITTED

    assert await machine.mark_delivered("u1", "cand-delivered", now=NOW)
    await machine.transition_terminal(
        "u2",
        "cand-suppressed",
        machine.STATE_SUPPRESSED,
        "policy",
        now=NOW,
    )

    delivered_topic = _doc(
        candidate_db, "u1", machine.TOPIC_STATE_SUBCOLLECTION, "topic-delivered"
    )
    suppressed_topic = _doc(
        candidate_db, "u2", machine.TOPIC_STATE_SUBCOLLECTION, "topic-suppressed"
    )
    assert delivered_topic["last_notified_at"] == NOW
    assert delivered_topic["notify_count"] == 1
    assert "last_notified_at" not in suppressed_topic
    assert suppressed_topic.get("notify_count", 0) == 0


@pytest.mark.asyncio
async def test_flag_off_helpers_do_no_work(monkeypatch):
    monkeypatch.setattr(scheduler.settings, "NOTIF_GRAPH", False)

    assert await scheduler._run_memory_graph_sweep(now=NOW) == []
    assert await scheduler._run_memory_graph_candidate_drain(now=NOW) == 0


@pytest.mark.asyncio
async def test_framer_uses_only_structured_payload_and_rejects_artifacts(monkeypatch):
    calls = []

    class _Models:
        body = "Want to map the next step together?"

        async def cheap(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            return framer.FramedMemoryGraphNotification(
                title="pick this up?",
                body=self.body,
            )

    models = _Models()
    monkeypatch.setattr(framer, "get_model_provider", lambda: models)
    payload = {
        "type": "next_step",
        "evidence": "map the next step",
        "artifact_ref": None,
    }

    framed = await framer.frame_memory_graph_notification(payload)

    assert framed is not None
    assert len(calls) == 1
    assert "map the next step" in calls[0][0]
    assert await framer.frame_memory_graph_notification({
        **payload,
        "artifact_ref": "artifact-1",
    }) is None
    assert len(calls) == 1
    models.body = "I drafted the next step for you"
    assert await framer.frame_memory_graph_notification(payload) is None
    assert len(calls) == 2


def test_scorer_hard_zeros_cover_phase4_policy():
    draft = _candidate("u1", "cand-1", "topic-1", 0.9)
    base_node = {GF.STATUS: GF.NODE_STATUS_ACTIVE}

    assert notifications.hard_zero_reason(
        {**base_node, GF.INFERRED_SENSITIVE: True}, draft, {}, {}, now=NOW
    ) == "inferred_sensitive"
    assert notifications.hard_zero_reason(
        {**base_node, GF.STATUS: GF.NODE_STATUS_COMPLETED}, draft, {}, {}, now=NOW
    ) == "terminal_status"
    assert notifications.hard_zero_reason(
        {**base_node, GF.REMINDER_CREATED_IN_SESSION: True}, draft, {}, {}, now=NOW
    ) == "reminder_created_in_session"
    assert notifications.hard_zero_reason(
        base_node,
        draft,
        {"last_notified_at": NOW - timedelta(hours=1)},
        {},
        now=NOW,
    ) == "recent_notification_same_topic"
    assert notifications.hard_zero_reason(
        base_node,
        draft,
        {"notify_count": machine.DEFAULT_TOPIC_CAP},
        {},
        now=NOW,
    ) == "per_topic_cap"
    assert notifications.hard_zero_reason(
        base_node,
        draft,
        {},
        {
            "fatigue_window_started_at": NOW - timedelta(hours=1),
            "proactive_sent_24h": machine.GLOBAL_FATIGUE_CAP,
        },
        now=NOW,
    ) == "global_fatigue_cap"


@pytest.mark.asyncio
async def test_orchestrator_retry_defers_but_permanent_drop_suppresses(monkeypatch):
    proposal = NotificationProposal(
        user_id="u1",
        source=SOURCE_MEMORY_GRAPH,
        kind=ProposalKind.PROACTIVE,
        dedup_key="cand-1",
        data={"candidate_id": "cand-1"},
    )
    defer = AsyncMock()
    terminal = AsyncMock()
    delivered = AsyncMock()
    monkeypatch.setattr(machine, "defer_candidate", defer)
    monkeypatch.setattr(machine, "transition_terminal", terminal)
    monkeypatch.setattr(machine, "mark_delivered", delivered)

    await notifications.on_orchestrator_outcome(
        proposal,
        OrchestratorDecision(Disposition.HOLD, REASON_QUIET_HOURS),
        now=NOW,
    )
    defer.assert_awaited_once()
    terminal.assert_not_awaited()

    await notifications.on_orchestrator_outcome(
        proposal,
        OrchestratorDecision(Disposition.DROP, REASON_TAP_GATE),
        now=NOW,
    )
    terminal.assert_awaited_once_with(
        "u1",
        "cand-1",
        machine.STATE_SUPPRESSED,
        REASON_TAP_GATE,
        now=NOW,
    )
    delivered.assert_not_awaited()
