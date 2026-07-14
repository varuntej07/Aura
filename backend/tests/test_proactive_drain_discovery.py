"""
Coverage for the proactive-drain discovery rewrite: instead of looping every
active user and running a per-user notification_queue query every minute
(mostly against empty queues), the drain now finds who has anything queued via
ONE collection_group query, then only drains those uids.

Two contracts pinned:
  1. queue_store.list_user_ids_with_pending finds the right uids (and only
     active-status ones), deduping multiple queued items per user, via a fake
     Firestore that mirrors collection_group semantics.
  2. scheduler._run_proactive_drain calls orchestrator.drain_user_queue ONLY
     for uids the discovery query returns, is a no-op when nothing is queued,
     and still applies the PROACTIVE_NOTIFICATION_UID_ALLOWLIST dark-test gate
     (a user found via the queue must not bypass the allowlist just because it
     didn't come through list_active_user_ids).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class _FakeSnap:
    def __init__(self, path: str):
        self.id = path.rsplit("/", 1)[-1]
        self.reference = _FakeRef(path)


class _FakeRef:
    """Just enough of a DocumentReference to support .parent.parent.id, the
    users/{uid}/notification_queue/{proposal_id} -> uid walk the real code does."""

    def __init__(self, path: str):
        self._path = path

    @property
    def parent(self):
        parts = self._path.split("/")
        if len(parts) <= 1:
            return None
        return _FakeCollectionRef("/".join(parts[:-1]))


class _FakeCollectionRef:
    def __init__(self, path: str):
        self._path = path

    @property
    def parent(self):
        parts = self._path.split("/")
        if len(parts) <= 1:
            return None
        return _FakeDocRef("/".join(parts[:-1]))


class _FakeDocRef:
    def __init__(self, path: str):
        self.id = path.rsplit("/", 1)[-1]


class _FakeQuery:
    def __init__(self, docs: list[dict]):
        self._docs = docs
        self._field = self._values = None
        self._limit = None

    def where(self, filter):  # noqa: A002 - mirrors Firestore's FieldFilter kwarg
        self._field, self._values = filter.field_path, set(filter.value)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def stream(self):
        out = []
        for doc in self._docs:
            if doc["fields"].get(self._field) not in self._values:
                continue
            out.append(_FakeSnap(doc["path"]))
            if self._limit and len(out) >= self._limit:
                break
        return iter(out)


class _FakeFirestore:
    def __init__(self, docs: list[dict]):
        self._docs = docs

    def collection_group(self, name):
        return _FakeQuery(self._docs)


def _doc(path: str, *, status: str) -> dict:
    return {"path": path, "fields": {"status": status}}


# ── queue_store.list_user_ids_with_pending ──────────────────────────────────

@pytest.mark.asyncio
async def test_no_queued_items_returns_empty_set():
    from src.services.notifications import queue_store

    with patch.object(queue_store, "admin_firestore", return_value=_FakeFirestore([])):
        assert await queue_store.list_user_ids_with_pending() == set()


@pytest.mark.asyncio
async def test_finds_and_dedupes_users_with_active_status_only():
    from src.services.notifications import queue_store

    docs = [
        _doc("users/u1/notification_queue/p1", status=queue_store.STATUS_PENDING),
        _doc("users/u1/notification_queue/p2", status=queue_store.STATUS_HELD),  # same user, 2nd item
        _doc("users/u2/notification_queue/p3", status=queue_store.STATUS_HELD),
        _doc("users/u3/notification_queue/p4", status=queue_store.STATUS_SENT),   # terminal, excluded
        _doc("users/u4/notification_queue/p5", status=queue_store.STATUS_DROPPED),  # terminal, excluded
    ]
    with patch.object(queue_store, "admin_firestore", return_value=_FakeFirestore(docs)):
        result = await queue_store.list_user_ids_with_pending()

    assert result == {"u1", "u2"}


@pytest.mark.asyncio
async def test_query_failure_fails_open_to_empty_set():
    from src.services.notifications import queue_store

    def _boom():
        raise RuntimeError("firestore unavailable")

    with patch.object(queue_store, "admin_firestore", side_effect=_boom):
        assert await queue_store.list_user_ids_with_pending() == set()


# ── scheduler._run_proactive_drain ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_drain_is_noop_when_nothing_queued(monkeypatch):
    from src.handlers import scheduler
    from src.services.notifications import orchestrator

    monkeypatch.setattr(
        "src.services.notifications.queue_store.list_user_ids_with_pending",
        AsyncMock(return_value=set()),
    )
    drain_mock = AsyncMock()
    monkeypatch.setattr(orchestrator, "drain_user_queue", drain_mock)
    monkeypatch.setattr("src.services.analytics.posthog_client.flush", AsyncMock())

    await scheduler._run_proactive_drain()

    drain_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_drain_only_called_for_queued_uids(monkeypatch):
    from src.handlers import scheduler
    from src.services.notifications import orchestrator

    monkeypatch.setattr(
        "src.services.notifications.queue_store.list_user_ids_with_pending",
        AsyncMock(return_value={"u1", "u2"}),
    )
    drain_mock = AsyncMock()
    monkeypatch.setattr(orchestrator, "drain_user_queue", drain_mock)
    monkeypatch.setattr("src.services.analytics.posthog_client.flush", AsyncMock())

    await scheduler._run_proactive_drain()

    drained = {call.args[0] for call in drain_mock.await_args_list}
    assert drained == {"u1", "u2"}


@pytest.mark.asyncio
async def test_drain_still_applies_dark_test_allowlist(monkeypatch):
    """A uid discovered via the queue (not list_active_user_ids) must still be
    excluded when the dark-test allowlist is set — the whole point of the gate
    is that a candidate revision can't leak proactive sends outside the
    tester's phone regardless of which discovery path found the user."""
    from src.config.settings import settings
    from src.handlers import scheduler
    from src.services.notifications import orchestrator

    monkeypatch.setattr(settings, "PROACTIVE_NOTIFICATION_UID_ALLOWLIST", "u1")
    monkeypatch.setattr(
        "src.services.notifications.queue_store.list_user_ids_with_pending",
        AsyncMock(return_value={"u1", "u2"}),
    )
    drain_mock = AsyncMock()
    monkeypatch.setattr(orchestrator, "drain_user_queue", drain_mock)
    monkeypatch.setattr("src.services.analytics.posthog_client.flush", AsyncMock())

    await scheduler._run_proactive_drain()

    drained = {call.args[0] for call in drain_mock.await_args_list}
    assert drained == {"u1"}
