"""The transactional outbox + the relay (dispatch_pending).

Proves: staging writes an unconsumed doc onto the caller's batch (so business
state + event commit atomically); the relay coalesces a user's events into one
orchestrate task (dedup happens in _read_pending); a failed enqueue is simply not
counted and the user's events stay consumed=false, so the next sweep re-enqueues
(at-least-once, no "published but stranded" gap — the relay marks NOTHING; the
orchestrator marks events consumed once it has drained them); an empty sweep is a
no-op; and a failed read (e.g. a missing index) returns 0 loudly instead of
raising into the tick. Plus the deploy-order index guard.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

from src.services.reactive import event_bus
from src.services.reactive import events as ev
from src.services.reactive import fields as F


# ── Staging (atomic co-commit) ───────────────────────────────────────────────
def test_stage_event_writes_unconsumed_doc_to_callers_writer(monkeypatch):
    sentinel_ref = object()
    chain = MagicMock()
    # admin_firestore().collection().document().collection().document() -> sentinel_ref
    final_doc = chain.collection.return_value.document.return_value.collection.return_value.document
    final_doc.return_value = sentinel_ref
    monkeypatch.setattr(event_bus, "admin_firestore", lambda: chain)

    writer = MagicMock()  # stands in for a WriteBatch / Transaction
    event = event_bus.build_event("u1", ev.EVENT_REMINDER_CREATED, {"id": "r1"}, source="chat")
    event_bus.stage_event(writer, event)

    writer.set.assert_called_once()
    ref_arg, doc_arg = writer.set.call_args.args
    assert ref_arg is sentinel_ref
    assert doc_arg[F.FIELD_CONSUMED] is False
    assert doc_arg[F.FIELD_TYPE] == ev.EVENT_REMINDER_CREATED
    assert doc_arg[F.FIELD_UID] == "u1"
    assert doc_arg[F.FIELD_EXPIRES_AT] is not None


async def test_emit_commits_one_batch(monkeypatch):
    batch = MagicMock()
    db = MagicMock()
    db.batch.return_value = batch
    monkeypatch.setattr(event_bus, "admin_firestore", lambda: db)

    event_id = await event_bus.emit("u1", ev.EVENT_APP_OPENED, source="client")

    batch.set.assert_called_once()
    batch.commit.assert_called_once()
    assert isinstance(event_id, str) and event_id


# ── Relay: coalescing (in the read) + dispatch ───────────────────────────────
def _outbox_snap(uid: str) -> MagicMock:
    snap = MagicMock()
    snap.to_dict.return_value = {F.FIELD_UID: uid}
    return snap


def test_read_pending_coalesces_distinct_uids_oldest_first(monkeypatch):
    # The coalescing lives here: many unconsumed events for one user collapse to a
    # single uid, so dispatch_pending enqueues one orchestrate per user, not per event.
    chain = MagicMock()
    stream = (
        chain.collection_group.return_value
        .where.return_value
        .order_by.return_value
        .limit.return_value
        .stream
    )
    stream.return_value = [_outbox_snap("a"), _outbox_snap("a"), _outbox_snap("b")]
    monkeypatch.setattr(event_bus, "admin_firestore", lambda: chain)

    uids = event_bus._read_pending(200)

    assert uids == ["a", "b"]  # deduped, first-seen order preserved


async def test_dispatch_pending_enqueues_one_task_per_user(monkeypatch):
    monkeypatch.setattr(event_bus, "_read_pending", lambda limit: ["a", "b"])
    enqueued: list[str] = []
    scheduler = MagicMock()
    scheduler.schedule_orchestrate.side_effect = lambda uid: enqueued.append(uid)
    monkeypatch.setattr(event_bus, "get_task_scheduler", lambda: scheduler)

    dispatched = await event_bus.dispatch_pending()

    assert dispatched == 2
    assert enqueued == ["a", "b"]  # one orchestrate per user


async def test_dispatch_pending_leaves_events_for_retry_when_enqueue_fails(monkeypatch):
    """Crash injection: the Cloud Task enqueue fails for one user. The relay marks
    NOTHING, so that user's events stay consumed=false and the next sweep re-enqueues
    (at-least-once). A failed enqueue is simply not counted in the dispatched total."""
    monkeypatch.setattr(event_bus, "_read_pending", lambda limit: ["a", "b"])

    def _enqueue(uid: str) -> None:
        if uid == "b":
            raise RuntimeError("cloud tasks unavailable")

    scheduler = MagicMock()
    scheduler.schedule_orchestrate.side_effect = _enqueue
    monkeypatch.setattr(event_bus, "get_task_scheduler", lambda: scheduler)

    dispatched = await event_bus.dispatch_pending()

    assert dispatched == 1  # only user a; b is re-swept next minute


async def test_dispatch_pending_empty_is_a_noop(monkeypatch):
    monkeypatch.setattr(event_bus, "_read_pending", lambda limit: [])
    scheduler = MagicMock()
    monkeypatch.setattr(event_bus, "get_task_scheduler", lambda: scheduler)

    dispatched = await event_bus.dispatch_pending()

    assert dispatched == 0
    scheduler.schedule_orchestrate.assert_not_called()


async def test_dispatch_pending_returns_zero_loudly_when_read_fails(monkeypatch):
    """A missing index 400s the read; the sweep returns 0 (logged as an error)
    rather than raising into the tick — but it is NOT silent."""
    def _boom(limit):
        raise RuntimeError("400 The query requires an index")

    monkeypatch.setattr(event_bus, "_read_pending", _boom)
    scheduler = MagicMock()
    monkeypatch.setattr(event_bus, "get_task_scheduler", lambda: scheduler)

    dispatched = await event_bus.dispatch_pending()

    assert dispatched == 0
    scheduler.schedule_orchestrate.assert_not_called()


async def test_dispatch_inline_is_best_effort_and_swallows_errors(monkeypatch):
    scheduler = MagicMock()
    scheduler.schedule_orchestrate.side_effect = RuntimeError("cloud tasks down")
    monkeypatch.setattr(event_bus, "get_task_scheduler", lambda: scheduler)

    # Must not raise into the request path (the sweep is the backstop).
    await event_bus.dispatch_inline("u1")


# ── Deploy-order index guard ─────────────────────────────────────────────────
def test_outbox_collection_group_index_declared():
    indexes_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "firestore.indexes.json",
    )
    with open(indexes_path, encoding="utf-8") as fh:
        indexes = json.load(fh)["indexes"]

    match = [
        idx for idx in indexes
        if idx.get("collectionGroup") == "outbox"
        and idx.get("queryScope") == "COLLECTION_GROUP"
    ]
    assert len(match) == 1, "outbox COLLECTION_GROUP index missing (relay sweep 400s)"
    field_paths = [f["fieldPath"] for f in match[0]["fields"]]
    assert field_paths == ["consumed", "ts"]
