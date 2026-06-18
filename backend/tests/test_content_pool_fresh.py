"""Coverage for the fresh-content probe (count_fresh_candidates / has_any_candidate).

The 2026-06-14 misdiagnosis: the old probe counted ANY doc, so a pool full of expired
tombstones read as "has content" and sent the scoring loop chasing a (healthy) vector
index instead of the starved ingest. These pin that the probe filters by expires_at
(only servable docs count), is bounded by the limit, and fails open."""

from __future__ import annotations

from google.cloud.firestore_v1.base_query import FieldFilter

from src.services.signal_engine import content_pool


class _FakeQuery:
    def __init__(self, docs, recorder):
        self._docs = docs
        self._rec = recorder
        self._limit = None

    def where(self, filter=None):
        self._rec["filter"] = filter
        return self

    def limit(self, n):
        self._limit = n
        self._rec["limit"] = n
        return self

    def stream(self):
        docs = self._docs[: self._limit] if self._limit is not None else self._docs
        return iter(docs)


class _FakeDB:
    def __init__(self, docs, recorder):
        self._docs = docs
        self._rec = recorder

    def collection(self, name):
        self._rec["collection"] = name
        return _FakeQuery(self._docs, self._rec)


async def test_count_fresh_filters_by_expiry_and_caps_at_limit(monkeypatch):
    rec: dict = {}
    monkeypatch.setattr(content_pool, "admin_firestore", lambda: _FakeDB([object()] * 10, rec))

    n = await content_pool.count_fresh_candidates(limit=3)

    assert n == 3                                   # capped at the limit, not the 10 docs
    assert rec["collection"] == "content_candidates"
    assert rec["limit"] == 3
    # The decisive contract: it filters on expires_at (only servable docs count), so an
    # all-expired pool can never again read as healthy.
    assert isinstance(rec["filter"], FieldFilter)
    assert getattr(rec["filter"], "field_path", "") == "expires_at"


async def test_has_any_candidate_true_when_fresh_exist(monkeypatch):
    rec: dict = {}
    monkeypatch.setattr(content_pool, "admin_firestore", lambda: _FakeDB([object()], rec))
    assert await content_pool.has_any_candidate() is True


async def test_has_any_candidate_false_when_pool_all_expired(monkeypatch):
    # No docs match the expires_at filter → empty stream → not fresh.
    rec: dict = {}
    monkeypatch.setattr(content_pool, "admin_firestore", lambda: _FakeDB([], rec))
    assert await content_pool.has_any_candidate() is False


async def test_count_fresh_fails_open(monkeypatch):
    """A probe error must NOT trigger a needless paid fallback or a false alarm:
    it returns the limit (treat the pool as healthy)."""
    def _boom():
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(content_pool, "admin_firestore", _boom)
    assert await content_pool.count_fresh_candidates(limit=30) == 30
