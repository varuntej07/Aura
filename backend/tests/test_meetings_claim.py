"""The meeting claim - the money gate for meeting notes.

Claim is where the monthly cap is enforced and the counter charged, inside one
Firestore transaction, so these tests pin every branch: cap denial for free
AND companion (only pro is unlimited), the idempotent same-device rejoin that
never double-charges, the cross-device 409, lock expiry allowing a fresh
capture of the same event later, and the compare-and-set transition primitive
the synthesis worker's idempotency rests on.

Fake Firestore follows test_signal_generation_store.py: path-tuple docs plus
a serialized transactional decorator.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest

from src.services.meetings import fields as F
from src.services.meetings import store

UID = "user-1"
EVENT = "cal-instance-abc123"


# ── Fake Firestore (nested collections via path tuples) ──────────────────────

class _FakeSnapshot:
    def __init__(self, data: dict | None):
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict:
        return dict(self._data) if self._data else {}


class _FakeDocRef:
    def __init__(self, store: dict, path: tuple):
        self._store = store
        self._path = path

    def collection(self, name: str) -> "_FakeCollection":
        return _FakeCollection(self._store, self._path + (name,))

    def get(self, transaction=None) -> _FakeSnapshot:
        return _FakeSnapshot(self._store.get(self._path))

    def set(self, data: dict) -> None:
        self._store[self._path] = dict(data)

    def update(self, fields: dict) -> None:
        if self._path not in self._store:
            raise KeyError(f"update on missing doc {self._path}")
        doc = self._store[self._path]
        for key, value in fields.items():
            if isinstance(value, _FakeArrayUnion):
                existing = doc.get(key, [])
                for element in value.elements:
                    if element not in existing:
                        existing.append(element)
                doc[key] = existing
            else:
                doc[key] = value


class _FakeCollection:
    def __init__(self, store: dict, path: tuple):
        self._store = store
        self._path = path

    def document(self, doc_id: str) -> _FakeDocRef:
        return _FakeDocRef(self._store, self._path + (doc_id,))


class _FakeTransaction:
    def set(self, ref: _FakeDocRef, data: dict) -> None:
        ref.set(data)

    def update(self, ref: _FakeDocRef, fields: dict) -> None:
        ref.update(fields)


class _FakeDb:
    def __init__(self, store: dict):
        self._store = store

    def collection(self, name: str) -> _FakeCollection:
        return _FakeCollection(self._store, (name,))

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()


class _FakeArrayUnion:
    def __init__(self, elements):
        self.elements = elements


class _FakeFirestoreModule:
    Transaction = _FakeTransaction
    ArrayUnion = _FakeArrayUnion
    _lock = threading.Lock()

    @staticmethod
    def transactional(fn):
        def _serialized(transaction):
            with _FakeFirestoreModule._lock:
                return fn(transaction)
        return _serialized


@pytest.fixture()
def docs(monkeypatch) -> dict:
    store_docs: dict = {}
    monkeypatch.setattr(store, "admin_firestore", lambda: _FakeDb(store_docs))
    monkeypatch.setattr(store, "gcloud_firestore", _FakeFirestoreModule)
    return store_docs


def _claim(tier: str, *, event_id: str = EVENT, device: str = "desktop-a",
           end_in_minutes: int = 30) -> store.ClaimResult:
    end_time = (datetime.now(UTC) + timedelta(minutes=end_in_minutes)).isoformat()
    return asyncio.run(store.claim_meeting(
        UID,
        event_id=event_id,
        title="Weekly sync",
        start_time=datetime.now(UTC).isoformat(),
        end_time=end_time,
        device_id=device,
        effective_tier=tier,
    ))


def _counter(docs: dict) -> int:
    month_key = datetime.now(UTC).strftime("%Y%m")
    doc = docs.get(("users", UID, "usage", f"meetings_{month_key}"), {})
    return int(doc.get("count", 0))


def _set_counter(docs: dict, count: int) -> None:
    month_key = datetime.now(UTC).strftime("%Y%m")
    docs[("users", UID, "usage", f"meetings_{month_key}")] = {"count": count}


# ── Claim branches ────────────────────────────────────────────────────────────

def test_first_claim_creates_meeting_and_charges_counter(docs):
    result = _claim("free")
    assert result.meeting_id and not result.denied_cap and not result.denied_conflict
    assert result.cap_minutes == F.FREE_SYNTHESIS_CAP_MINUTES
    assert _counter(docs) == 1
    meeting = docs[("users", UID, "meetings", result.meeting_id)]
    assert meeting[F.STATUS] == F.STATUS_CAPTURING
    assert meeting[F.EVENT_ID] == EVENT


def test_free_and_companion_are_capped_pro_is_not(docs):
    _set_counter(docs, F.MONTHLY_MEETING_CAP)
    for tier in ("free", "companion", "starter"):
        result = _claim(tier, event_id=f"evt-{tier}")
        assert result.denied_cap, tier
        assert result.seconds_until_reset > 0

    result = _claim("pro", event_id="evt-pro")
    assert not result.denied_cap
    assert result.meeting_id
    assert result.cap_minutes == F.PRO_SYNTHESIS_CAP_MINUTES


def test_same_device_reclaim_is_idempotent_and_free(docs):
    first = _claim("free")
    again = _claim("free")
    assert again.meeting_id == first.meeting_id
    assert again.rejoined
    assert again.cap_minutes == first.cap_minutes
    assert _counter(docs) == 1  # rejoin never double-charges


def test_reclaim_after_complete_mints_a_new_meeting(docs):
    """Once /complete moved the meeting past capturing, its uploads 409 - a
    live lock must not steer a rejoin into it (review finding #9)."""
    first = _claim("free")
    docs[("users", UID, "meetings", first.meeting_id)][F.STATUS] = F.STATUS_UPLOADED
    second = _claim("free")
    assert second.meeting_id and second.meeting_id != first.meeting_id
    assert not second.rejoined
    assert _counter(docs) == 2  # a fresh capture is a fresh charge


def test_other_device_gets_conflict_while_lock_is_live(docs):
    _claim("free", device="desktop-a")
    other = _claim("free", device="desktop-b")
    assert other.denied_conflict
    assert not other.meeting_id
    assert _counter(docs) == 1


def test_expired_lock_allows_a_fresh_claim(docs):
    first = _claim("free")
    event_key = store.event_key_for(EVENT)
    docs[("users", UID, "meeting_claims", event_key)][F.CLAIM_EXPIRES_AT_MS] = 1
    second = _claim("free")
    assert second.meeting_id and second.meeting_id != first.meeting_id
    assert not second.rejoined
    assert _counter(docs) == 2


def test_event_key_is_firestore_safe_for_manual_ids():
    key = store.event_key_for("manual:0a1b/2c?3d")
    assert "/" not in key and len(key) == 40


# ── Status compare-and-set (the worker's idempotency primitive) ───────────────

def test_transition_status_moves_only_from_allowed_states(docs):
    result = _claim("free")
    mid = result.meeting_id

    ok, now = asyncio.run(store.transition_status(
        UID, mid, from_statuses=(F.STATUS_CAPTURING,), to_status=F.STATUS_UPLOADED,
    ))
    assert ok and now == F.STATUS_UPLOADED

    ok, now = asyncio.run(store.transition_status(
        UID, mid, from_statuses=(F.STATUS_CAPTURING,), to_status=F.STATUS_UPLOADED,
    ))
    assert not ok and now == F.STATUS_UPLOADED  # settled re-run reports itself

    ok, now = asyncio.run(store.transition_status(
        UID, "missing", from_statuses=(F.STATUS_CAPTURING,), to_status=F.STATUS_UPLOADED,
    ))
    assert not ok and now == ""


# ── Note persistence / retention ──────────────────────────────────────────────

def _note() -> dict:
    return {"summary": "s", "decisions": [], "action_items": [],
            "open_questions": [], "language": "en", "one_sided": False}


def test_save_note_stamps_ttl_for_non_pro_only(docs):
    for tier, expects_ttl in (("free", True), ("companion", True), ("pro", False)):
        result = _claim(tier, event_id=f"evt-note-{tier}")
        asyncio.run(store.save_note(UID, result.meeting_id, _note(), effective_tier=tier))
        meeting = docs[("users", UID, "meetings", result.meeting_id)]
        assert meeting[F.STATUS] == F.STATUS_READY
        assert (F.EXPIRES_AT in meeting) == expects_ttl, tier


def test_append_segment_meta_is_idempotent(docs):
    result = _claim("free")
    for _ in range(2):
        asyncio.run(store.append_segment_meta(
            UID, result.meeting_id, seq=0, start_ms=0, duration_ms=300_000,
            incomplete=False,
        ))
    meeting = docs[("users", UID, "meetings", result.meeting_id)]
    assert meeting[F.SEGMENTS] == [
        {"seq": 0, "start_ms": 0, "duration_ms": 300_000, "incomplete": False},
    ]


# ── Synthesis lease (Cloud Tasks at-least-once defense) ───────────────────────

def test_synthesis_lease_refuses_concurrent_duplicate_but_allows_stale(docs):
    result = _claim("free")
    mid = result.meeting_id
    asyncio.run(store.transition_status(
        UID, mid, from_statuses=(F.STATUS_CAPTURING,), to_status=F.STATUS_UPLOADED,
    ))

    claimed, now = asyncio.run(store.claim_synthesis(UID, mid))
    assert claimed and now == F.STATUS_SYNTHESIZING

    # A duplicate delivery while the lease is fresh must NOT re-run the work.
    claimed, now = asyncio.run(store.claim_synthesis(UID, mid))
    assert not claimed and now == F.STATUS_SYNTHESIZING

    # A crashed worker's stale lease is reclaimable.
    docs[("users", UID, "meetings", mid)][F.SYNTHESIS_STARTED_AT_MS] = 1
    claimed, now = asyncio.run(store.claim_synthesis(UID, mid))
    assert claimed and now == F.STATUS_SYNTHESIZING

    # Settled meetings are never claimable.
    docs[("users", UID, "meetings", mid)][F.STATUS] = F.STATUS_READY
    claimed, now = asyncio.run(store.claim_synthesis(UID, mid))
    assert not claimed and now == F.STATUS_READY
