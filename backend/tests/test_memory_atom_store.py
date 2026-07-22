"""
Memory atom store — the UNBOUNDED long-term writer.

Verifies: a new atom embeds once and writes the full doc; the SAME text upserts in
place WITHOUT re-embedding (only weight/last_seen bump); DIFFERENT text creates a
second atom with NO eviction (the "remember forever" contract); atom_id is stable +
normalized + type-scoped; wipe clears everything. Firestore + the embedder are faked
so the test is deterministic and offline.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from src.services.memory import atom_store
from src.services.memory import fields as F
from src.services.memory.atom_store import AtomInput

NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)


# --- fake Firestore (a dict-backed subcollection) -------------------------
class _Ref:
    def __init__(self, id_: str, store: dict):
        self.id = id_
        self._store = store

    @property
    def reference(self):  # used by wipe's batch.delete(snap.reference)
        return self

    def delete(self):
        self._store.pop(self.id, None)


class _Snap:
    def __init__(self, id_: str, data, store: dict):
        self.id = id_
        self._data = data
        self.exists = data is not None
        self._store = store

    def to_dict(self):
        return dict(self._data or {})

    @property
    def reference(self):
        return _Ref(self.id, self._store)


class _Batch:
    def __init__(self, store: dict):
        self.store = store

    def set(self, ref, doc):
        self.store[ref.id] = dict(doc)

    def update(self, ref, doc):
        self.store.setdefault(ref.id, {}).update(doc)

    def delete(self, ref):
        self.store.pop(ref.id, None)

    def commit(self):
        pass


class _Coll:
    def __init__(self, store: dict):
        self.store = store

    def document(self, aid: str):
        return _Ref(aid, self.store)

    def stream(self):
        return [_Snap(k, v, self.store) for k, v in list(self.store.items())]


class _Doc:
    def __init__(self, store: dict):
        self.store = store

    def collection(self, _name: str):
        return _Coll(self.store)


class _Parent:
    def __init__(self, store: dict):
        self.store = store

    def document(self, _uid: str):
        return _Doc(self.store)


class _Db:
    def __init__(self, store: dict):
        self.store = store

    def collection(self, _name: str):
        return _Parent(self.store)

    def get_all(self, refs):
        return [_Snap(r.id, self.store.get(r.id), self.store) for r in refs]

    def batch(self):
        return _Batch(self.store)


def _install(monkeypatch, store: dict, embed_calls: list):
    monkeypatch.setattr(atom_store, "admin_firestore", lambda: _Db(store))

    async def _fake_embed(texts):
        embed_calls.append(list(texts))
        return [[float(len(t)), 0.0, 1.0] for t in texts]

    monkeypatch.setattr(atom_store, "embed_texts", _fake_embed)


# --- atom_id helper -------------------------------------------------------
def test_atom_id_is_stable_normalized_and_type_scoped():
    assert F.atom_id(F.ATOM_TYPE_FACT, "KCR") == F.atom_id(F.ATOM_TYPE_FACT, "  kcr ")
    assert F.atom_id(F.ATOM_TYPE_FACT, "x") != F.atom_id(F.ATOM_TYPE_STORYLINE, "x")


# --- writer ---------------------------------------------------------------
def test_new_atom_embeds_once_and_writes_full_doc(monkeypatch):
    store: dict = {}
    calls: list = []
    _install(monkeypatch, store, calls)

    n = asyncio.run(atom_store.upsert_atoms(
        "u1",
        [AtomInput(text="dislikes early showers", atom_type=F.ATOM_TYPE_FACT, importance=0.6)],
        source="extractor", now=NOW,
    ))
    assert n == 1
    assert len(calls) == 1  # embedded exactly once
    aid = F.atom_id(F.ATOM_TYPE_FACT, "dislikes early showers")
    doc = store[aid]
    assert doc[F.TEXT] == "dislikes early showers"
    assert doc[F.ATOM_TYPE] == F.ATOM_TYPE_FACT
    assert doc[F.WEIGHT] == 1.0
    assert F.EMBEDDING in doc


def test_same_text_skips_embed_and_bumps_weight(monkeypatch):
    store: dict = {}
    calls: list = []
    _install(monkeypatch, store, calls)
    atom = AtomInput(text="lives in Hyderabad", atom_type=F.ATOM_TYPE_FACT)

    asyncio.run(atom_store.upsert_atoms("u1", [atom], source="extractor", now=NOW))
    asyncio.run(atom_store.upsert_atoms("u1", [atom], source="extractor", now=NOW))

    assert len(calls) == 1  # second upsert did NOT re-embed
    aid = F.atom_id(F.ATOM_TYPE_FACT, "lives in Hyderabad")
    assert store[aid][F.WEIGHT] == 2.0  # decay-then-increment, same NOW -> +1


def test_different_text_creates_second_atom_no_eviction(monkeypatch):
    store: dict = {}
    calls: list = []
    _install(monkeypatch, store, calls)

    asyncio.run(atom_store.upsert_atoms(
        "u1", [AtomInput(text="fact one", atom_type=F.ATOM_TYPE_FACT)], source="extractor", now=NOW))
    asyncio.run(atom_store.upsert_atoms(
        "u1", [AtomInput(text="fact two", atom_type=F.ATOM_TYPE_FACT)], source="extractor", now=NOW))

    assert len(store) == 2  # unbounded: both kept, nothing evicted
    assert len(calls) == 2


def test_blank_text_is_dropped(monkeypatch):
    store: dict = {}
    calls: list = []
    _install(monkeypatch, store, calls)
    n = asyncio.run(atom_store.upsert_atoms(
        "u1", [AtomInput(text="   ", atom_type=F.ATOM_TYPE_FACT)], source="extractor", now=NOW))
    assert n == 0
    assert store == {}
    assert calls == []


def test_wipe_removes_all_atoms(monkeypatch):
    store: dict = {}
    calls: list = []
    _install(monkeypatch, store, calls)
    asyncio.run(atom_store.upsert_atoms("u1", [
        AtomInput(text="a", atom_type=F.ATOM_TYPE_FACT),
        AtomInput(text="b", atom_type=F.ATOM_TYPE_FACT),
    ], source="extractor", now=NOW))
    assert len(store) == 2

    removed = asyncio.run(atom_store.wipe_atoms("u1"))
    assert removed == 2
    assert store == {}


def test_delete_atom_cascades_graph_delete_and_fails_open(monkeypatch):
    store = {"fact_abc": {F.TEXT: "remember me"}}
    _install(monkeypatch, store, [])
    from src.services.memory import graph_store

    seen = []

    async def _graph_failure(uid, node_id):
        seen.append((uid, node_id))
        raise RuntimeError("graph unavailable")

    monkeypatch.setattr(graph_store, "delete_node", _graph_failure)

    async def _delete_and_drain():
        result = await atom_store.delete_atom("u1", "fact_abc")
        await asyncio.sleep(0)
        return result

    assert asyncio.run(_delete_and_drain()) is True
    assert store == {}
    assert seen == [("u1", "fact_abc")]
