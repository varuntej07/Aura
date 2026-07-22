"""Phase 6 source-D shadow lifecycle, evaluation, and revalidation coverage."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from src.config.settings import settings
from src.handlers import scheduler
from src.services.memory import graph_fields as GF
from src.services.notifications import candidate_machine as machine
from src.services.notifications import orchestrator
from src.services.notifications.memory_graph_framer import (
    FramedMemoryGraphNotification,
)
from src.services.reactive import event_bus
from src.services.session_followup import evaluator, lifecycle, revalidator
from src.services.session_followup import fields as F
from src.services.session_followup.clustering import cluster_turns

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


class _Increment:
    def __init__(self, value):
        self.value = value


class _Snapshot:
    def __init__(self, ref, data):
        self.reference = ref
        self.id = ref.id
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data or {})


def _apply_values(current, data):
    for key, value in data.items():
        if isinstance(value, _Increment):
            current[key] = int(current.get(key, 0) or 0) + value.value
        else:
            current[key] = value


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
            current = self.db.docs.setdefault(self.path, {})
            _apply_values(current, dict(data))
        else:
            current = {}
            _apply_values(current, dict(data))
            self.db.docs[self.path] = current

    def update(self, data):
        current = self.db.docs.setdefault(self.path, {})
        _apply_values(current, dict(data))

    def create(self, data):
        if self.path in self.db.docs:
            raise RuntimeError("already exists")
        self.set(data)


class _Filter:
    def __init__(self, field_path, op_string, value):
        self.field_path = field_path
        self.op_string = op_string
        self.value = value


class _Query:
    def __init__(self, source):
        self.source = source
        self.filters = []
        self.order = None
        self.descending = False
        self.maximum = None

    def where(self, *, filter):
        self.filters.append(filter)
        return self

    def order_by(self, field, direction=None):
        self.order = field
        self.descending = str(direction).upper().endswith("DESCENDING")
        return self

    def limit(self, value):
        self.maximum = value
        return self

    def stream(self, transaction=None):
        snaps = list(self.source._raw_stream())
        for field_filter in self.filters:
            field = field_filter.field_path
            operator = getattr(field_filter, "op_string", None) or getattr(
                field_filter, "op", None
            )
            expected = field_filter.value

            def _matches(snap):
                actual = snap.to_dict().get(field)
                if operator == "==":
                    return actual == expected
                if operator == "in":
                    return actual in expected
                if operator == "<=":
                    return actual is not None and actual <= expected
                if operator == ">=":
                    return actual is not None and actual >= expected
                return False

            snaps = [snap for snap in snaps if _matches(snap)]
        if self.order:
            snaps.sort(
                key=lambda snap: (
                    snap.to_dict().get(self.order) is None,
                    snap.to_dict().get(self.order),
                ),
                reverse=self.descending,
            )
        return snaps[: self.maximum] if self.maximum is not None else snaps


class _Collection:
    def __init__(self, db, path):
        self.db = db
        self.path = tuple(path)
        self.id = self.path[-1]

    @property
    def parent(self):
        return _DocRef(self.db, self.path[:-1]) if len(self.path) >= 2 else None

    def document(self, doc_id):
        return _DocRef(self.db, (*self.path, doc_id))

    def _raw_stream(self):
        expected = len(self.path) + 1
        return [
            _Snapshot(_DocRef(self.db, path), data)
            for path, data in list(self.db.docs.items())
            if len(path) == expected and path[:-1] == self.path
        ]

    def stream(self):
        return self._raw_stream()

    def where(self, *, filter):
        return _Query(self).where(filter=filter)

    def order_by(self, field, direction=None):
        return _Query(self).order_by(field, direction=direction)


class _CollectionGroup:
    def __init__(self, db, name):
        self.db = db
        self.name = name

    def _raw_stream(self):
        return [
            _Snapshot(_DocRef(self.db, path), data)
            for path, data in list(self.db.docs.items())
            if len(path) >= 2 and path[-2] == self.name
        ]

    def where(self, *, filter):
        return _Query(self).where(filter=filter)


class _Transaction:
    def set(self, ref, data, merge=False):
        ref.set(data, merge=merge)

    def update(self, ref, data):
        ref.update(data)

    def create(self, ref, data):
        ref.create(data)


class _Batch(_Transaction):
    def commit(self):
        return None


class _Db:
    def __init__(self):
        self.docs = {}

    def collection(self, name):
        return _Collection(self, (name,))

    def collection_group(self, name):
        return _CollectionGroup(self, name)

    def transaction(self):
        return _Transaction()

    def batch(self):
        return _Batch()

    def get_all(self, refs):
        return [ref.get() for ref in refs]


class _Firestore:
    Transaction = _Transaction
    Increment = _Increment
    FieldFilter = _Filter
    _lock = threading.Lock()

    @staticmethod
    def transactional(func):
        def _serialized(transaction):
            with _Firestore._lock:
                return func(transaction)

        return _serialized


@pytest.fixture()
def followup_db(monkeypatch):
    db = _Db()
    for module in (machine, evaluator, lifecycle, revalidator, event_bus):
        monkeypatch.setattr(module, "admin_firestore", lambda: db)
    for module in (machine, lifecycle, revalidator):
        monkeypatch.setattr(module, "fs", _Firestore)
    monkeypatch.setattr(settings, "FOLLOWUP_SHADOW", True)
    monkeypatch.setattr(settings, "PROACTIVE_FOLLOWUP_SEND", False)
    monkeypatch.setattr(
        orchestrator,
        "_user_local",
        AsyncMock(return_value=(NOW, NOW.date().isoformat())),
    )
    monkeypatch.setattr(orchestrator, "_is_quiet_hours", lambda _: False)

    async def _frame(payload, **kwargs):
        return FramedMemoryGraphNotification(
            title="Want to pick this up?",
            body=f"We can take the next step on {payload['evidence'][:30]}",
        )

    monkeypatch.setattr(revalidator, "frame_memory_graph_notification", _frame)
    monkeypatch.setattr(orchestrator, "submit", AsyncMock())
    return db


def _path(uid, collection, doc_id):
    return (GF.PARENT_COLLECTION, uid, collection, doc_id)


def _seed_session(
    db,
    *,
    uid="u1",
    session_id="s1",
    revision=1,
    origin=F.ORIGIN_ORGANIC,
    origin_candidate_id=None,
    lineage_chain=None,
    turn_count=4,
    entity="launch plan",
    **turn_fields,
):
    db.docs[_path(uid, F.SESSIONS, session_id)] = {
        "session_id": session_id,
        "surface": F.SURFACE_CHAT,
        "origin": origin,
        "origin_candidate_id": origin_candidate_id,
        "lineage_chain": list(lineage_chain or []),
        "state": F.STATE_FINALIZED,
        "input_revision": revision,
        "started_at": NOW - timedelta(minutes=20),
        "last_activity_at": NOW,
        "last_user_turn_at": NOW,
        "finalized_at": NOW,
        "user_turn_count": turn_count,
    }
    for index in range(turn_count):
        db.docs[(
            GF.PARENT_COLLECTION,
            uid,
            F.SESSIONS,
            session_id,
            F.TURNS,
            f"t{index}",
        )] = {
            "turn_index": index,
            "role": "user",
            "entity_keys": [entity],
            "lexical_terms": ["launch", "plan", "follow", "up"],
            "future_intent": True,
            "unresolved_action": True,
            "next_step": True,
            "follow_up_depth": index,
            **turn_fields,
        }
    db.docs[("users", uid)] = {
        "aura_consent_granted": True,
        "timezone": "UTC",
    }


async def _evaluate(uid="u1", session_id="s1", **kwargs):
    return await evaluator.evaluate_finalized_session(
        uid,
        session_id,
        now=NOW,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_one_candidate_per_session_and_shadow_never_submits(followup_db):
    _seed_session(followup_db)
    for index in range(4, 8):
        followup_db.docs[(
            GF.PARENT_COLLECTION, "u1", F.SESSIONS, "s1", F.TURNS, f"other{index}"
        )] = {
            "turn_index": index,
            "role": "user",
            "entity_keys": ["career change"],
            "lexical_terms": ["career", "change", "later"],
            "future_intent": True,
        }

    candidate_id = await _evaluate()

    candidates = [
        data
        for path, data in followup_db.docs.items()
        if len(path) == 4 and path[-2] == machine.CANDIDATE_SUBCOLLECTION
    ]
    assert candidate_id
    assert len(candidates) == 1
    assert candidates[0]["state"] == machine.STATE_SHADOW
    assert candidates[0]["shadow_outcome"] == "would_submit"
    assert candidates[0]["framed_text"]
    orchestrator.submit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("turn_fields", "graph_status", "reminder", "reason"),
    [
        ({"inferred_sensitive": True}, None, False, "inferred_sensitive"),
        ({}, None, True, "reminder_created_in_session"),
        ({}, GF.NODE_STATUS_ABANDONED, False, "terminal_status"),
    ],
)
async def test_hard_blocks_never_candidate(
    followup_db, turn_fields, graph_status, reminder, reason
):
    _seed_session(followup_db, **turn_fields)
    if reminder:
        followup_db.docs[("users", "u1", "reminders", "r1")] = {
            "session_id": "s1",
            "status": "pending",
            "message": "Follow up on the launch plan",
        }
    if graph_status:
        node_id = GF.entity_id("launch plan")
        followup_db.docs[_path("u1", GF.NODE_SUBCOLLECTION, node_id)] = {
            GF.STATUS: graph_status,
        }

    assert await _evaluate() is None
    topic_doc = followup_db.docs[_path("u1", F.SESSION_TOPICS, "s1")]
    assert topic_doc["topics"][0]["drop_reason"] == reason


@pytest.mark.asyncio
async def test_unrelated_reminder_in_session_does_not_suppress_topic(followup_db):
    _seed_session(followup_db)
    followup_db.docs[("users", "u1", "reminders", "r1")] = {
        "session_id": "s1",
        "status": "pending",
        "message": "Buy groceries for dinner",
    }

    assert await _evaluate() is not None


@pytest.mark.asyncio
async def test_cold_start_requires_explicit_future_intent_or_action(followup_db):
    _seed_session(
        followup_db,
        future_intent=False,
        unresolved_action=False,
        next_step=True,
        lexical_terms=["launch", "plan", "next", "step"],
    )

    assert await _evaluate() is None
    topic_doc = followup_db.docs[_path("u1", F.SESSION_TOPICS, "s1")]
    assert topic_doc["topics"][0]["drop_reason"] == (
        "cold_start_requires_explicit_intent"
    )


@pytest.mark.asyncio
async def test_finalize_revision_version_and_task_retry_are_idempotent(followup_db):
    _seed_session(followup_db)
    service = lifecycle.SessionLifecycleService(followup_db)
    candidate_one = await _evaluate()
    candidate = followup_db.docs[_path(
        "u1", machine.CANDIDATE_SUBCOLLECTION, candidate_one
    )]
    fire_epoch = candidate["fire_at"].timestamp()

    assert await _evaluate() == candidate_one
    await revalidator.revalidate_and_submit_followup(
        "u1", candidate_one, expected_fire_epoch=fire_epoch, now=candidate["fire_at"]
    )
    await revalidator.revalidate_and_submit_followup(
        "u1", candidate_one, expected_fire_epoch=fire_epoch, now=candidate["fire_at"]
    )
    assert len([
        path for path in followup_db.docs if path[-2:-1] == (machine.CANDIDATE_SUBCOLLECTION,)
    ]) == 1

    followup_db.docs[_path("u1", F.SESSIONS, "s1")]["input_revision"] = 2
    candidate_two = await _evaluate()
    candidate_three = await _evaluate(evaluator_version="session-followup-v2")
    assert len({candidate_one, candidate_two, candidate_three}) == 3
    assert followup_db.docs[_path(
        "u1", machine.TOPIC_STATE_SUBCOLLECTION, candidate["topic_id"]
    )]["active_candidate_id"] == candidate_three
    assert followup_db.docs[_path(
        "u1", machine.CANDIDATE_SUBCOLLECTION, candidate_one
    )]["state"] == machine.STATE_CANCELED

    # A duplicate finalization is owned and suppressed at the lifecycle boundary.
    assert await service.finalize_session("u1", "s1", reason="duplicate", now=NOW) is False


@pytest.mark.asyncio
async def test_voice_grace_resumes_same_session_and_finalizes_once(followup_db):
    service = lifecycle.SessionLifecycleService(followup_db)
    followup_db.docs[("users", "u1")] = {"aura_consent_granted": True}
    session_id = await service.start_session(
        "u1", "voice-1", surface=F.SURFACE_VOICE, now=NOW
    )
    await service.note_user_turn(
        "u1",
        session_id,
        surface=F.SURFACE_VOICE,
        turn_id="turn-1",
        turn_index=0,
        text="hello there",
        now=NOW,
    )
    await service.note_voice_disconnect("u1", session_id, now=NOW)

    resumed = await service.start_session(
        "u1",
        None,
        surface=F.SURFACE_VOICE,
        now=NOW + timedelta(seconds=30),
    )
    assert resumed == session_id
    root = followup_db.docs[_path("u1", F.SESSIONS, session_id)]
    assert root["finalization"] == {**F.FINALIZATION_DEFAULTS, "reason": None}

    await service.note_voice_disconnect(
        "u1", session_id, now=NOW + timedelta(seconds=30)
    )
    assert await service.sweep_idle_sessions(
        now=NOW + timedelta(seconds=121)
    ) == 1
    assert root["state"] == F.STATE_FINALIZED
    assert root["finalization"]["reason"] == "disconnect_grace_elapsed"
    assert await service.finalize_session(
        "u1", session_id, reason="duplicate", now=NOW + timedelta(seconds=122)
    ) is False
    events = [
        data
        for path, data in followup_db.docs.items()
        if len(path) == 4 and path[-2] == "outbox"
    ]
    assert len(events) == 1
    assert events[0]["payload"] == {
        "uid": "u1",
        "session_id": session_id,
        "surface": F.SURFACE_VOICE,
        "origin": F.ORIGIN_ORGANIC,
        "input_revision": 1,
    }


@pytest.mark.asyncio
async def test_same_topic_live_cancels_other_topic_live_defers(followup_db):
    _seed_session(followup_db)
    candidate_id = await _evaluate()
    candidate = followup_db.docs[_path(
        "u1", machine.CANDIDATE_SUBCOLLECTION, candidate_id
    )]
    topic_id = candidate["topic_id"]
    followup_db.docs[_path("u1", F.SESSIONS, "live")] = {
        "state": F.STATE_ACTIVE,
        "active_topic_id": topic_id,
    }
    result = await revalidator.revalidate_and_submit_followup(
        "u1", candidate_id, expected_fire_epoch=candidate["fire_at"].timestamp(),
        now=candidate["fire_at"],
    )
    assert result == "canceled"
    assert candidate["drop_reason"] == "same_topic_live"

    followup_db.docs[_path("u1", F.SESSIONS, "live")]["active_topic_id"] = "topic_other"
    candidate["drop_reason"] = None
    candidate["fire_at"] = NOW + timedelta(hours=1)
    result = await revalidator.revalidate_and_submit_followup(
        "u1", candidate_id, expected_fire_epoch=candidate["fire_at"].timestamp(),
        now=candidate["fire_at"],
    )
    assert result == "deferred"
    assert candidate["drop_reason"] == "other_topic_live"


@pytest.mark.asyncio
async def test_quiet_hours_defer_then_refire_while_fresh(followup_db, monkeypatch):
    _seed_session(followup_db)
    candidate_id = await _evaluate()
    candidate = followup_db.docs[_path(
        "u1", machine.CANDIDATE_SUBCOLLECTION, candidate_id
    )]
    monkeypatch.setattr(orchestrator, "_is_quiet_hours", lambda _: True)
    result = await revalidator.revalidate_and_submit_followup(
        "u1", candidate_id, expected_fire_epoch=candidate["fire_at"].timestamp(),
        now=candidate["fire_at"],
    )
    assert result == "deferred"
    assert candidate["drop_reason"] == "quiet_hours"
    deferred_fire_at = candidate["fire_at"]

    monkeypatch.setattr(orchestrator, "_is_quiet_hours", lambda _: False)
    result = await revalidator.revalidate_and_submit_followup(
        "u1", candidate_id, expected_fire_epoch=deferred_fire_at.timestamp(),
        now=deferred_fire_at,
    )
    assert result == "shadow"
    assert candidate["shadow_outcome"] == "would_submit"


@pytest.mark.asyncio
async def test_fatigue_suppresses_without_advancing_cooldown(followup_db):
    _seed_session(followup_db)
    candidate_id = await _evaluate()
    candidate = followup_db.docs[_path(
        "u1", machine.CANDIDATE_SUBCOLLECTION, candidate_id
    )]
    arbitration = followup_db.docs.setdefault(
        _path("u1", machine.ARBITRATION_SUBCOLLECTION, machine.ARBITRATION_DOC_ID), {}
    )
    arbitration.update({
        "fatigue_window_started_at": NOW,
        "proactive_sent_24h": machine.GLOBAL_FATIGUE_CAP,
    })

    result = await revalidator.revalidate_and_submit_followup(
        "u1", candidate_id, expected_fire_epoch=candidate["fire_at"].timestamp(),
        now=candidate["fire_at"],
    )
    topic = followup_db.docs[_path(
        "u1", machine.TOPIC_STATE_SUBCOLLECTION, candidate["topic_id"]
    )]
    assert result == "suppressed"
    assert "last_notified_at" not in topic


@pytest.mark.asyncio
async def test_tap_reinforces_only_after_meaningful_finalized_session(followup_db):
    topic_id = cluster_turns([{
        "role": "user",
        "entity_keys": ["launch plan"],
        "lexical_terms": ["launch", "plan"],
    }])[0]["topic_id"]
    followup_db.docs[_path("u1", machine.CANDIDATE_SUBCOLLECTION, "origin")] = {
        "candidate_id": "origin",
        "topic_id": topic_id,
    }
    _seed_session(
        followup_db,
        session_id="tap-small",
        turn_count=1,
        origin=F.ORIGIN_NOTIFICATION_TAP,
        origin_candidate_id="origin",
        lineage_chain=[topic_id],
    )
    await _evaluate(session_id="tap-small")
    topic_path = _path("u1", machine.TOPIC_STATE_SUBCOLLECTION, topic_id)
    assert followup_db.docs.get(topic_path, {}).get("weight", 0) == 0

    _seed_session(
        followup_db,
        session_id="tap-big",
        turn_count=3,
        origin=F.ORIGIN_NOTIFICATION_TAP,
        origin_candidate_id="origin",
        lineage_chain=[topic_id],
    )
    await _evaluate(session_id="tap-big")
    assert followup_db.docs[topic_path]["weight"] == 1
    assert followup_db.docs[topic_path]["last_meaningful_engagement"] == NOW


@pytest.mark.asyncio
async def test_two_concurrent_evaluators_leave_one_active_candidate(
    followup_db, monkeypatch
):
    _seed_session(followup_db, session_id="s1")
    _seed_session(followup_db, session_id="s2")
    monkeypatch.setattr(revalidator, "revalidate_and_submit_followup", AsyncMock())

    candidate_ids = await asyncio.gather(
        _evaluate(session_id="s1"),
        _evaluate(session_id="s2"),
    )
    candidates = [
        data
        for path, data in followup_db.docs.items()
        if len(path) == 4 and path[-2] == machine.CANDIDATE_SUBCOLLECTION
    ]
    topic_id = candidates[0]["topic_id"]
    retained_ids = {candidate["candidate_id"] for candidate in candidates}
    assert followup_db.docs[_path(
        "u1", machine.TOPIC_STATE_SUBCOLLECTION, topic_id
    )]["active_candidate_id"] in retained_ids
    assert any(candidate_id in retained_ids for candidate_id in candidate_ids)
    assert sum(candidate["state"] in machine.ACTIVE_STATES for candidate in candidates) == 1


@pytest.mark.asyncio
async def test_collision_window_transaction_has_one_real_winner(followup_db):
    drafts = []
    for index, topic_id in enumerate(("topic-a", "topic-b")):
        drafts.append(machine.CandidateDraft(
            candidate_id=f"collision-{index}",
            topic_id=topic_id,
            source=F.SOURCE_SESSION_FOLLOWUP,
            project_id=f"project-{index}",
            node_id=f"node-{index}",
            event_id=None,
            value_payload={
                "type": "next_step", "evidence": "continue", "artifact_ref": None,
            },
            evidence={},
            score=0.9 - index * 0.1,
            fire_at=NOW,
            expires_at=NOW + timedelta(hours=6),
        ))
    for draft in drafts:
        assert await machine.install_candidate("u1", draft)
        followup_db.docs[_path(
            "u1", machine.CANDIDATE_SUBCOLLECTION, draft.candidate_id
        )]["state"] = machine.STATE_REVALIDATING
    results = await asyncio.gather(*[
        machine.reserve_delivery(
            "u1", draft.candidate_id, effective_score=draft.score, now=NOW
        )
        for draft in drafts
    ])
    assert sum(granted for granted, _ in results) == 1
    assert sorted(
        followup_db.docs[_path(
            "u1", machine.CANDIDATE_SUBCOLLECTION, draft.candidate_id
        )]["state"]
        for draft in drafts
    ) == [machine.STATE_DEFERRED, machine.STATE_SUBMITTED]


def test_clustering_falls_back_without_graph_documents():
    topics = cluster_turns([
        {"role": "user", "text": "plan the launch order and launch checklist"},
        {"role": "user", "text": "the launch checklist still needs an owner"},
        {"role": "user", "text": "book a dentist appointment for next week"},
    ])
    assert sorted(topic["user_turn_count"] for topic in topics) == [1, 2]


@pytest.mark.asyncio
async def test_flags_off_write_nothing_and_schedule_nothing(followup_db, monkeypatch):
    monkeypatch.setattr(settings, "FOLLOWUP_SHADOW", False)
    monkeypatch.setattr(settings, "PROACTIVE_FOLLOWUP_SEND", False)
    service = lifecycle.SessionLifecycleService(followup_db)

    assert await service.start_session("u1", "off", surface="chat") == "off"
    assert await service.finalize_session("u1", "off", reason="idle", now=NOW) is False
    assert await evaluator.evaluate_finalized_session("u1", "off", now=NOW) is None
    assert await scheduler._run_session_followup_lifecycle_sweep(now=NOW) == 0
    assert await scheduler._run_session_followup_shadow_drain(now=NOW) == 0
    assert not any(path[-1] == "off" for path in followup_db.docs)
