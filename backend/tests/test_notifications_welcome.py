"""Tests for the one-time day-0 welcome push (services/notifications/welcome.py)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from src.services.notifications import proposal as proposal_mod
from src.services.notifications import welcome

NOW = datetime(2026, 6, 10, 18, 0, tzinfo=UTC)


class _FakeSnap:
    def __init__(self, data: dict | None):
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict | None:
        return self._data


class _FakeDocRef:
    def __init__(self, data: dict | None = None):
        self.data = data

    def get(self, transaction=None):
        return _FakeSnap(self.data)


class _FakeTxn:
    """Applies writes to _FakeDocRef state so a second claim observes the first."""

    def set(self, ref: _FakeDocRef, payload: dict, merge: bool = False) -> None:
        if merge and ref.data:
            ref.data.update(payload)
        else:
            ref.data = dict(payload)


class _FakeDb:
    def __init__(self, data: dict | None = None):
        self.ref = _FakeDocRef(data)

    def collection(self, name: str):
        col = MagicMock()
        col.document.return_value = self.ref
        return col

    def transaction(self):
        return _FakeTxn()


@pytest.fixture(autouse=True)
def _passthrough_transactional(monkeypatch):
    """gcloud's @transactional needs a live server; run the body inline instead."""
    monkeypatch.setattr("google.cloud.firestore.transactional", lambda fn: fn)


def _boom():
    raise RuntimeError("firestore down")


async def test_first_call_sends_and_marks_claim(monkeypatch):
    db = _FakeDb(data=None)
    monkeypatch.setattr(welcome, "admin_firestore", lambda: db)
    submitted = []

    async def _fake_submit(proposal):
        submitted.append(proposal)

    monkeypatch.setattr(welcome.orchestrator, "submit", _fake_submit)

    await welcome.maybe_send_welcome_notification("u1", now=NOW)

    assert len(submitted) == 1
    assert submitted[0].source == proposal_mod.SOURCE_WELCOME
    assert submitted[0].kind == proposal_mod.ProposalKind.COMMITTED
    assert submitted[0].dedup_key == "welcome:u1"
    assert db.ref.data[welcome.FIELD_WELCOME_SENT_AT] == NOW.isoformat()


async def test_second_call_is_a_noop(monkeypatch):
    db = _FakeDb(data={welcome.FIELD_WELCOME_SENT_AT: "2026-06-01T00:00:00+00:00"})
    monkeypatch.setattr(welcome, "admin_firestore", lambda: db)
    submitted = []

    async def _fake_submit(proposal):
        submitted.append(proposal)

    monkeypatch.setattr(welcome.orchestrator, "submit", _fake_submit)

    await welcome.maybe_send_welcome_notification("u1", now=NOW)

    assert submitted == []


async def test_claim_failure_fails_closed_never_sends(monkeypatch):
    monkeypatch.setattr(welcome, "admin_firestore", _boom)
    submitted = []

    async def _fake_submit(proposal):
        submitted.append(proposal)

    monkeypatch.setattr(welcome.orchestrator, "submit", _fake_submit)

    await welcome.maybe_send_welcome_notification("u1", now=NOW)

    assert submitted == []


def test_welcome_source_is_registered_everywhere():
    assert proposal_mod.SOURCE_WELCOME == "welcome"
    assert proposal_mod.SOURCE_WELCOME in proposal_mod.ALL_SOURCES
    assert proposal_mod.PRIORITY[proposal_mod.SOURCE_WELCOME] == 93
    assert proposal_mod.FRESHNESS_MAX_AGE[proposal_mod.SOURCE_WELCOME] is None
