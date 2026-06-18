"""Coverage for delete_expired_candidates (the expired-tombstone sweep).

The 2026-06-15 bug: nothing deleted expired candidates, so the tombstone pile grew
and occupied the top-K that find_nearest pulls back before its in-Python expiry
filter — a niche-interest user's 50 nearest were all expired → [] even though the
pool had fresh content. These pin that the sweep deletes expired docs, is bounded
per call (so one tick can't run an unbounded delete), filters on expires_at (so it
never touches fresh content), batches at the Firestore limit, and never raises into
the scheduler tick it piggybacks on.
"""

from __future__ import annotations

from google.cloud.firestore_v1.base_query import FieldFilter

from src.services.signal_engine import content_pool


class _FakeRef:
    def __init__(self, key: str):
        self.key = key


class _FakeSnap:
    def __init__(self, key: str):
        self.id = key
        self.reference = _FakeRef(key)


class _FakeBatch:
    def __init__(self, db: "_FakeDB"):
        self._db = db
        self._pending: list[str] = []

    def delete(self, ref: _FakeRef):
        self._pending.append(ref.key)

    def commit(self):
        self._db.commit_count += 1
        self._db.delete_keys(self._pending)
        self._pending = []


class _FakeQuery:
    def __init__(self, db: "_FakeDB", recorder: dict):
        self._db = db
        self._rec = recorder
        self._limit = None

    def where(self, filter=None):
        self._rec["filter"] = filter
        return self

    def limit(self, n):
        self._limit = n
        return self

    def stream(self):
        keys = self._db.keys()
        if self._limit is not None:
            keys = keys[: self._limit]
        return iter([_FakeSnap(k) for k in keys])


class _FakeDB:
    """Stateful: deletes actually shrink the doc set, so the paging loop terminates."""

    def __init__(self, n_expired: int, recorder: dict):
        self._keys = [f"doc_{i}" for i in range(n_expired)]
        self._rec = recorder
        self.commit_count = 0

    def collection(self, name):
        self._rec["collection"] = name
        return _FakeQuery(self, self._rec)

    def batch(self):
        return _FakeBatch(self)

    def keys(self) -> list[str]:
        return list(self._keys)

    def delete_keys(self, keys: list[str]):
        gone = set(keys)
        self._keys = [k for k in self._keys if k not in gone]


async def test_sweep_deletes_all_expired_when_under_cap(monkeypatch):
    rec: dict = {}
    db = _FakeDB(n_expired=120, recorder=rec)
    monkeypatch.setattr(content_pool, "admin_firestore", lambda: db)

    deleted = await content_pool.delete_expired_candidates(max_deletes=1000)

    assert deleted == 120
    assert db.keys() == []                                  # pool emptied of expired docs
    assert rec["collection"] == "content_candidates"
    # Decisive contract: the sweep filters on expires_at with a strict "<", so fresh
    # (future-expiry) docs can never be deleted by it.
    assert isinstance(rec["filter"], FieldFilter)
    assert getattr(rec["filter"], "field_path", "") == "expires_at"
    assert getattr(rec["filter"], "op_string", "") == "<"


async def test_sweep_respects_per_tick_cap_and_batches(monkeypatch):
    rec: dict = {}
    db = _FakeDB(n_expired=1200, recorder=rec)
    monkeypatch.setattr(content_pool, "admin_firestore", lambda: db)

    deleted = await content_pool.delete_expired_candidates(max_deletes=1000)

    assert deleted == 1000                                  # stopped at the per-tick cap
    assert len(db.keys()) == 200                            # remainder left for the next tick
    assert db.commit_count == 2                             # 500 + 500, batched at the Firestore limit


async def test_sweep_noop_when_pool_clean(monkeypatch):
    rec: dict = {}
    db = _FakeDB(n_expired=0, recorder=rec)
    monkeypatch.setattr(content_pool, "admin_firestore", lambda: db)

    deleted = await content_pool.delete_expired_candidates()

    assert deleted == 0
    assert db.commit_count == 0                             # nothing expired → no commits


async def test_sweep_fails_safe(monkeypatch):
    """A Firestore error must never raise into the scheduler tick; it reports 0."""
    def _boom():
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(content_pool, "admin_firestore", _boom)
    assert await content_pool.delete_expired_candidates() == 0
