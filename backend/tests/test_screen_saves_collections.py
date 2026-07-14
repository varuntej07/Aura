"""
Screen-save collection-name resolver.

Verifies: a near-duplicate name ("kicks" vs "Shoes") resolves to the SAME
existing collection and bumps its item_count instead of minting a duplicate;
a genuinely distinct name mints a new collection; a missing-vector-index
failure logs the loud, specific fix message (not a generic warning) and still
degrades to minting rather than silently going always-create-new with no
explanation. Firestore + the embedder are faked so the test is deterministic
and offline, mirroring test_memory_atom_store.py's style.
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime

from src.services.screen_saves import collections as C
from src.services.screen_saves import fields as F

NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class _FakeVector:
    """Stand-in for google.cloud.firestore_v1.vector.Vector — keeps the raw list."""

    def __init__(self, values):
        self.values = list(values)


class _Snap:
    def __init__(self, id_: str, data: dict):
        self.id = id_
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _NearestQuery:
    def __init__(self, snaps: list[_Snap]):
        self._snaps = snaps

    def stream(self):
        return self._snaps


class _Ref:
    def __init__(self, id_: str, store: dict):
        self.id = id_
        self._store = store

    def set(self, doc: dict, merge: bool = False):
        existing = dict(self._store.get(self.id, {})) if merge else {}
        existing.update(doc)
        self._store[self.id] = existing

    def update(self, patch: dict):
        from google.cloud.firestore_v1.transforms import Increment

        doc = self._store.setdefault(self.id, {})
        for key, value in patch.items():
            if isinstance(value, Increment):
                doc[key] = doc.get(key, 0) + value.value
            else:
                doc[key] = value


class _Coll:
    def __init__(self, store: dict):
        self.store = store
        self.raise_missing_index = False

    def document(self, doc_id: str):
        return _Ref(doc_id, self.store)

    def find_nearest(self, *, vector_field, query_vector, distance_measure, limit, distance_result_field):
        if self.raise_missing_index:
            raise Exception(
                "400 FAILED_PRECONDITION: no matching COLLECTION_GROUP vector index found"
            )
        query_values = query_vector.values if isinstance(query_vector, _FakeVector) else list(query_vector)
        scored = []
        for doc_id, data in self.store.items():
            vec = data.get(vector_field)
            if vec is None:
                continue
            vec_values = vec.values if isinstance(vec, _FakeVector) else list(vec)
            distance = 1.0 - _cosine(query_values, vec_values)
            scored.append((distance, doc_id, {**data, distance_result_field: distance}))
        scored.sort(key=lambda t: t[0])
        return _NearestQuery([_Snap(doc_id, data) for _, doc_id, data in scored[:limit]])


class _Doc:
    def __init__(self, coll: _Coll):
        self._coll = coll

    def collection(self, _name: str):
        return self._coll


class _Parent:
    def __init__(self, coll: _Coll):
        self._coll = coll

    def document(self, _uid: str):
        return _Doc(self._coll)


class _Db:
    def __init__(self, coll: _Coll):
        self._coll = coll

    def collection(self, _name: str):
        return _Parent(self._coll)


class _FakeLogger:
    def __init__(self):
        self.errors: list[tuple[str, dict]] = []
        self.warnings: list[tuple[str, dict]] = []
        self.infos: list[tuple[str, dict]] = []

    def error(self, msg, extra=None):
        self.errors.append((msg, extra or {}))

    def warn(self, msg, extra=None):
        self.warnings.append((msg, extra or {}))

    def info(self, msg, extra=None):
        self.infos.append((msg, extra or {}))


def _install(monkeypatch, store: dict, vectors_by_text: dict[str, list[float]]):
    coll = _Coll(store)
    monkeypatch.setattr(C, "admin_firestore", lambda: _Db(coll))
    monkeypatch.setattr(C, "Vector", _FakeVector)
    fake_logger = _FakeLogger()
    monkeypatch.setattr(C, "logger", fake_logger)

    async def _fake_embed_text(text: str):
        if text not in vectors_by_text:
            raise KeyError(f"no fake vector configured for {text!r}")
        return vectors_by_text[text]

    monkeypatch.setattr(C, "embed_text", _fake_embed_text)
    return coll, fake_logger


# "Shoes" and "kicks" point the same direction (near-duplicate); "Recipes" is orthogonal.
_VECTORS = {
    "Shoes": [1.0, 0.0, 0.0],
    "kicks": [0.99, 0.02, 0.0],
    "Recipes": [0.0, 1.0, 0.0],
}


def test_near_duplicate_name_reuses_existing_collection(monkeypatch):
    store: dict = {}
    _install(monkeypatch, store, _VECTORS)

    first = asyncio.run(C.resolve_collection_name("u1", "Shoes", now=NOW))
    assert first.is_new is True
    assert first.display_name == "Shoes"
    assert store[first.doc_id][F.ITEM_COUNT] == 1

    second = asyncio.run(C.resolve_collection_name("u1", "kicks", now=NOW))
    assert second.is_new is False
    assert second.doc_id == first.doc_id
    assert second.display_name == "Shoes"  # canonical spelling wins, not "kicks"
    assert store[first.doc_id][F.ITEM_COUNT] == 2  # bumped, not duplicated


def test_distinct_name_mints_a_new_collection(monkeypatch):
    store: dict = {}
    _install(monkeypatch, store, _VECTORS)

    shoes = asyncio.run(C.resolve_collection_name("u1", "Shoes", now=NOW))
    recipes = asyncio.run(C.resolve_collection_name("u1", "Recipes", now=NOW))

    assert recipes.is_new is True
    assert recipes.doc_id != shoes.doc_id
    assert len(store) == 2


def test_missing_vector_index_logs_loud_and_still_mints(monkeypatch):
    store: dict = {}
    coll, fake_logger = _install(monkeypatch, store, _VECTORS)
    coll.raise_missing_index = True

    result = asyncio.run(C.resolve_collection_name("u1", "Shoes", now=NOW))

    assert result.is_new is True
    assert result.display_name == "Shoes"
    assert len(store) == 1
    assert len(fake_logger.errors) == 1
    logged_msg = fake_logger.errors[0][0]
    assert "MISSING VECTOR INDEX" in logged_msg
    assert "gcloud firestore indexes composite create" in logged_msg


def test_embedder_failure_fails_open_to_minting_without_a_vector(monkeypatch):
    store: dict = {}
    _install(monkeypatch, store, {})  # no vectors configured -> embed_text always raises

    result = asyncio.run(C.resolve_collection_name("u1", "Shoes", now=NOW))

    assert result.is_new is True
    assert result.display_name == "Shoes"
    assert F.EMBEDDING not in store[result.doc_id]  # minted without ever getting a vector
