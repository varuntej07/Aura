"""The durable per-generation claim behind ingest-triggered signal scoring.

Cloud Tasks delivers at-least-once and Cloud Scheduler retries ingest runs, so
the generation record in signal_engine/generation_store.py is what guarantees
that each 4-hour ingest generation is scored exactly once effectively: a
completed generation no-ops, a live lease defers a concurrent duplicate, an
expired lease (crashed worker) is reclaimable, and a failed pass stays
retryable. These tests pin every branch of that claim, plus the deterministic
generation ID that makes six — and only six — scheduled scoring generations
possible per UTC day.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest

from src.services.signal_engine import generation_store
from src.services.signal_engine.generation_store import (
    FIELD_NEW_CANDIDATES_WRITTEN,
    FIELD_SCORING_STATUS,
    GENERATIONS_COLLECTION,
    STATUS_COMPLETE,
    STATUS_RUNNING,
    ClaimOutcome,
    ScoringRunStats,
)

GENERATION_ID = "20260709T1200Z"


# ── Fake Firestore with serializable transactions ────────────────────────────
# The claim relies on Firestore transactions being atomic. The fake models that
# with one process-wide lock around each transactional body, so two "concurrent"
# claims (separate asyncio.to_thread threads) serialize exactly like the server
# would serialize them — which is precisely the property under test.

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

    def get(self, transaction=None) -> _FakeSnapshot:
        return _FakeSnapshot(self._store.get(self._path))

    def set(self, data: dict) -> None:
        self._store[self._path] = dict(data)

    def update(self, fields: dict) -> None:
        if self._path not in self._store:
            raise KeyError(f"update on missing doc {self._path}")
        self._store[self._path].update(fields)


class _FakeCollection:
    def __init__(self, store: dict, name: str):
        self._store = store
        self._name = name

    def document(self, doc_id: str) -> _FakeDocRef:
        return _FakeDocRef(self._store, (self._name, doc_id))


class _FakeTransaction:
    def set(self, ref: _FakeDocRef, data: dict) -> None:
        ref.set(data)

    def update(self, ref: _FakeDocRef, fields: dict) -> None:
        ref.update(fields)


class _FakeDb:
    def __init__(self, store: dict):
        self._store = store

    def collection(self, name: str) -> _FakeCollection:
        return _FakeCollection(self._store, name)

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()


class _FakeFirestoreModule:
    """Stand-in for `google.cloud.firestore` inside generation_store."""

    Transaction = _FakeTransaction
    _lock = threading.Lock()

    @staticmethod
    def transactional(fn):
        def _serialized(transaction):
            with _FakeFirestoreModule._lock:
                return fn(transaction)
        return _serialized


@pytest.fixture()
def store(monkeypatch) -> dict:
    docs: dict = {}
    monkeypatch.setattr(generation_store, "admin_firestore", lambda: _FakeDb(docs))
    monkeypatch.setattr(generation_store, "fs", _FakeFirestoreModule)
    return docs


def _record(store: dict) -> dict:
    return store[(GENERATIONS_COLLECTION, GENERATION_ID)]


# ── Deterministic generation ID / six per UTC day ────────────────────────────

def test_generation_id_floors_to_the_four_hour_utc_bucket():
    assert generation_store.generation_id_for(
        datetime(2026, 7, 9, 13, 47, tzinfo=UTC)
    ) == "20260709T1200Z"
    assert generation_store.generation_id_for(
        datetime(2026, 7, 9, 0, 0, tzinfo=UTC)
    ) == "20260709T0000Z"
    assert generation_store.generation_id_for(
        datetime(2026, 7, 9, 23, 59, tzinfo=UTC)
    ) == "20260709T2000Z"


def test_exactly_six_scheduled_generations_per_utc_day():
    # The ingest cron fires "0 */4 * * *": whatever minute a run (or its retry)
    # actually lands at, the derived ID collapses to one of exactly six buckets.
    day = datetime(2026, 7, 9, tzinfo=UTC)
    ids = {
        generation_store.generation_id_for(day + timedelta(minutes=m))
        for m in range(24 * 60)
    }
    assert len(ids) == 6
    assert 24 % generation_store.GENERATION_WINDOW_HOURS == 0
    assert generation_store.GENERATIONS_PER_UTC_DAY == 6


# ── Claim semantics ──────────────────────────────────────────────────────────

async def test_two_concurrent_deliveries_cannot_both_claim(store):
    await generation_store.record_ingest_completed(
        GENERATION_ID, new_candidates_written=12
    )

    outcomes = await asyncio.gather(
        generation_store.claim_for_scoring(GENERATION_ID),
        generation_store.claim_for_scoring(GENERATION_ID),
    )

    assert sorted(o.value for o in outcomes) == ["claimed", "lease_held"]
    assert _record(store)[FIELD_SCORING_STATUS] == STATUS_RUNNING


async def test_completed_generation_noops_on_duplicate_delivery(store):
    await generation_store.record_ingest_completed(GENERATION_ID, new_candidates_written=3)
    assert await generation_store.claim_for_scoring(GENERATION_ID) is ClaimOutcome.CLAIMED
    await generation_store.mark_scoring_complete(
        GENERATION_ID,
        ScoringRunStats(users_considered=5, users_scored=4, users_skipped=1, knn_query_count=4),
    )

    duplicate = await generation_store.claim_for_scoring(GENERATION_ID)

    assert duplicate is ClaimOutcome.ALREADY_COMPLETE
    record = _record(store)
    assert record[FIELD_SCORING_STATUS] == STATUS_COMPLETE
    assert record[generation_store.FIELD_KNN_QUERY_COUNT] == 4
    assert record[generation_store.FIELD_USERS_CONSIDERED] == 5
    assert record[generation_store.FIELD_USERS_SCORED] == 4
    assert record[generation_store.FIELD_USERS_SKIPPED] == 1


async def test_running_generation_with_unexpired_lease_noops(store):
    await generation_store.record_ingest_completed(GENERATION_ID, new_candidates_written=3)
    assert await generation_store.claim_for_scoring(GENERATION_ID) is ClaimOutcome.CLAIMED

    duplicate = await generation_store.claim_for_scoring(GENERATION_ID)

    assert duplicate is ClaimOutcome.LEASE_HELD


async def test_expired_lease_is_reclaimed_after_worker_failure(store):
    start = datetime(2026, 7, 9, 12, 5, tzinfo=UTC)
    await generation_store.record_ingest_completed(
        GENERATION_ID, new_candidates_written=3, now=start
    )
    assert (
        await generation_store.claim_for_scoring(GENERATION_ID, now=start)
        is ClaimOutcome.CLAIMED
    )

    after_lease_expiry = start + timedelta(
        seconds=generation_store.SCORING_LEASE_SECONDS + 1
    )
    reclaim = await generation_store.claim_for_scoring(
        GENERATION_ID, now=after_lease_expiry
    )

    assert reclaim is ClaimOutcome.CLAIMED
    assert _record(store)[FIELD_SCORING_STATUS] == STATUS_RUNNING


async def test_failed_scoring_stays_retryable(store):
    await generation_store.record_ingest_completed(GENERATION_ID, new_candidates_written=3)
    assert await generation_store.claim_for_scoring(GENERATION_ID) is ClaimOutcome.CLAIMED
    await generation_store.mark_scoring_failed(GENERATION_ID, error="framer exploded")

    retry = await generation_store.claim_for_scoring(GENERATION_ID)

    assert retry is ClaimOutcome.CLAIMED


async def test_missing_record_is_claimable_for_manual_recovery(store):
    # Manual recovery may target a bucket no ingest ever recorded.
    outcome = await generation_store.claim_for_scoring(GENERATION_ID)

    assert outcome is ClaimOutcome.CLAIMED
    assert _record(store)[FIELD_SCORING_STATUS] == STATUS_RUNNING


async def test_ingest_retry_never_resets_scoring_status(store):
    await generation_store.record_ingest_completed(GENERATION_ID, new_candidates_written=7)
    assert await generation_store.claim_for_scoring(GENERATION_ID) is ClaimOutcome.CLAIMED

    # A Cloud Scheduler retry of the SAME generation lands mid-scoring: it may
    # only add to the candidate count, never knock running back to pending.
    await generation_store.record_ingest_completed(GENERATION_ID, new_candidates_written=5)

    record = _record(store)
    assert record[FIELD_SCORING_STATUS] == STATUS_RUNNING
    assert record[FIELD_NEW_CANDIDATES_WRITTEN] == 12


async def test_claim_fails_closed_when_store_is_unreachable(monkeypatch):
    def _boom():
        raise RuntimeError("firestore down")

    monkeypatch.setattr(generation_store, "admin_firestore", _boom)
    monkeypatch.setattr(generation_store, "fs", _FakeFirestoreModule)

    # Raising (not claiming, not skipping) is what routes the Cloud Task into
    # its retry-with-backoff path instead of double-running or dropping a pass.
    with pytest.raises(RuntimeError):
        await generation_store.claim_for_scoring(GENERATION_ID)
