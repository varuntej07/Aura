"""Phase 0 deterministic graph schema and mutation tests."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime

from src.services.memory import graph_fields as F
from src.services.memory import graph_store
from src.services.memory.graph_store import GraphEdgeInput, entity_node

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


class _Snap:
    def __init__(self, ref, data):
        self.id = ref.id
        self.reference = ref
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data or {})


class _Ref:
    def __init__(self, db, path):
        self._db = db
        self.path = tuple(path)
        self.id = self.path[-1]

    def collection(self, name):
        return _Collection(self._db, (*self.path, name))

    def get(self):
        return _Snap(self, self._db.docs.get(self.path))

    def set(self, data, merge=False):
        if merge:
            self._db.docs.setdefault(self.path, {}).update(dict(data))
        else:
            self._db.docs[self.path] = dict(data)

    def update(self, data):
        if self.path not in self._db.docs:
            raise KeyError(self.path)
        self._db.docs[self.path].update(dict(data))

    def create(self, data):
        if self.path in self._db.docs:
            raise RuntimeError("already exists")
        self._db.docs[self.path] = dict(data)

    def delete(self):
        self._db.docs.pop(self.path, None)

    def collections(self):
        prefix_len = len(self.path)
        names = {
            path[prefix_len]
            for path in self._db.docs
            if len(path) > prefix_len + 1 and path[:prefix_len] == self.path
        }
        return [_Collection(self._db, (*self.path, name)) for name in sorted(names)]


class _Query:
    def __init__(self, collection, field_name, value):
        self._collection = collection
        self._field_name = field_name
        self._value = value

    def stream(self):
        return [
            snap
            for snap in self._collection.stream()
            if snap.to_dict().get(self._field_name) == self._value
        ]


class _Collection:
    def __init__(self, db, path):
        self._db = db
        self.path = tuple(path)

    def document(self, id_):
        return _Ref(self._db, (*self.path, id_))

    def stream(self):
        expected_len = len(self.path) + 1
        return [
            _Snap(_Ref(self._db, path), data)
            for path, data in list(self._db.docs.items())
            if len(path) == expected_len and path[:-1] == self.path
        ]

    def where(self, *, filter):
        return _Query(self, filter.field_path, filter.value)


class _Batch:
    def __init__(self):
        self.ops = []

    def set(self, ref, data, merge=False):
        self.ops.append((ref.set, (data,), {"merge": merge}))

    def delete(self, ref):
        self.ops.append((ref.delete, (), {}))

    def commit(self):
        for func, args, kwargs in self.ops:
            func(*args, **kwargs)


class _Db:
    def __init__(self):
        self.docs = {}

    def collection(self, name):
        return _Collection(self, (name,))

    def get_all(self, refs):
        return [ref.get() for ref in refs]

    def batch(self):
        return _Batch()


def _install(monkeypatch):
    db = _Db()
    monkeypatch.setattr(graph_store, "admin_firestore", lambda: db)
    return db


def _collection_docs(db, name):
    prefix = (F.PARENT_COLLECTION, "u1", name)
    return {
        path[-1]: data
        for path, data in db.docs.items()
        if len(path) == 4 and path[:3] == prefix
    }


def test_deterministic_identity_is_typed_and_edges_are_directed():
    expected_entity_hash = hashlib.sha1(b"company:annapurna labs").hexdigest()[:24]
    assert F.entity_id("company", "Annapurna Labs") == (
        f"entity_company_{expected_entity_hash}"
    )
    left = F.entity_id("artifact", "blog")
    right = F.entity_id("goal", "role")
    expected_edge_hash = hashlib.sha1(f"{left}|about|{right}".encode()).hexdigest()[:32]
    assert F.edge_id(left, "about", right) == f"edge_{expected_edge_hash}"
    assert F.edge_id(left, "about", right) != F.edge_id(right, "about", left)


def test_upsert_is_idempotent_for_same_nodes_and_edge(monkeypatch):
    db = _install(monkeypatch)
    left = entity_node("Annapurna Labs", project_id="project_jobs")
    right = entity_node("SDE role", project_id="project_jobs")
    edge = GraphEdgeInput(left.node_id, right.node_id, "relates_to")

    asyncio.run(graph_store.upsert_graph("u1", [left, right], [edge], source="test", now=NOW))
    asyncio.run(graph_store.upsert_graph("u1", [left, right], [edge], source="test", now=NOW))

    nodes = _collection_docs(db, F.NODE_SUBCOLLECTION)
    edges = _collection_docs(db, F.EDGE_SUBCOLLECTION)
    adjacency = _collection_docs(db, F.ADJ_SUBCOLLECTION)
    assert len(nodes) == 2
    assert len(edges) == 1
    assert len(adjacency) == 2
    assert nodes[left.node_id][F.DEGREE] == 1
    assert adjacency[left.node_id][F.NEIGHBORS] == [right.node_id]
    assert nodes[left.node_id][F.ENTITY] == "annapurna labs"
    assert nodes[left.node_id][F.DISPLAY] == "Annapurna Labs"
    assert nodes[left.node_id][F.ALIASES] == []
    assert nodes[left.node_id][F.STATUS] == F.NODE_STATUS_ACTIVE
    assert nodes[left.node_id][F.PROJECT_ID] == "project_jobs"
    assert nodes[left.node_id][F.DECAY_KIND]


def test_new_strong_edge_only_marks_sweep_evidence(monkeypatch):
    db = _install(monkeypatch)
    monkeypatch.setattr(graph_store.settings, "NOTIF_GRAPH", True)
    left = entity_node("launch plan")
    right = entity_node("pricing notes")

    asyncio.run(graph_store.upsert_graph(
        "u1",
        [left, right],
        [GraphEdgeInput(left.node_id, right.node_id, "relates_to", weight=0.9)],
        source="test",
        now=NOW,
    ))

    nodes = _collection_docs(db, F.NODE_SUBCOLLECTION)
    evidence = nodes[left.node_id][F.NEW_STRONG_EDGE_EVIDENCE]
    assert evidence["event"] == F.EVENT_MEMORY_EDGE
    assert evidence["connected_node_id"] == right.node_id
    assert nodes[left.node_id][F.NEW_STRONG_EDGE_AT] == NOW.isoformat()


def test_delete_node_cascades_edges_and_adjacency_and_gc(monkeypatch):
    db = _install(monkeypatch)
    center = entity_node("center")
    left = entity_node("left")
    right = entity_node("right")
    anchor = entity_node("anchor")
    asyncio.run(graph_store.upsert_graph(
        "u1",
        [center, left, right, anchor],
        [
            GraphEdgeInput(center.node_id, left.node_id),
            GraphEdgeInput(center.node_id, right.node_id),
            GraphEdgeInput(left.node_id, anchor.node_id),
        ],
        source="test",
        now=NOW,
    ))

    assert asyncio.run(graph_store.delete_node("u1", center.node_id)) is True

    nodes = _collection_docs(db, F.NODE_SUBCOLLECTION)
    assert center.node_id not in nodes
    assert right.node_id not in nodes
    assert nodes[left.node_id][F.DEGREE] == 1
    assert nodes[anchor.node_id][F.DEGREE] == 1
    edges = _collection_docs(db, F.EDGE_SUBCOLLECTION)
    assert len(edges) == 1
    assert next(iter(edges.values()))[F.SRC] == left.node_id
    adjacency = _collection_docs(db, F.ADJ_SUBCOLLECTION)
    assert center.node_id not in adjacency
    assert right.node_id not in adjacency
    assert adjacency[left.node_id][F.NEIGHBORS] == [anchor.node_id]


def test_adjacency_neighbors_are_capped_at_32(monkeypatch):
    db = _install(monkeypatch)
    center = entity_node("center")
    leaves = [entity_node(f"leaf-{index}") for index in range(40)]
    edges = [GraphEdgeInput(center.node_id, leaf.node_id) for leaf in leaves]

    asyncio.run(graph_store.upsert_graph(
        "u1", [center, *leaves], edges, source="test", now=NOW,
    ))

    adjacency = _collection_docs(db, F.ADJ_SUBCOLLECTION)
    assert len(adjacency[center.node_id][F.NEIGHBORS]) == 32
    assert _collection_docs(db, F.NODE_SUBCOLLECTION)[center.node_id][F.DEGREE] == 40


def test_abandoned_status_is_hidden_but_history_remains_recallable(monkeypatch):
    db = _install(monkeypatch)
    goal = entity_node("marathon")
    reason = entity_node("knee physio")
    asyncio.run(graph_store.upsert_graph(
        "u1",
        [goal, reason],
        [GraphEdgeInput(goal.node_id, reason.node_id)],
        source="test",
        now=NOW,
    ))

    assert asyncio.run(graph_store.set_node_status(
        "u1", goal.node_id, F.NODE_STATUS_ABANDONED,
    )) is True
    nodes = _collection_docs(db, F.NODE_SUBCOLLECTION)
    assert nodes[goal.node_id][F.STATUS] == F.NODE_STATUS_ABANDONED
    assert len(_collection_docs(db, F.EDGE_SUBCOLLECTION)) == 1


def test_wipe_graph_removes_all_three_graph_collections(monkeypatch):
    db = _install(monkeypatch)
    left = entity_node("left")
    right = entity_node("right")
    asyncio.run(graph_store.upsert_graph(
        "u1", [left, right], [GraphEdgeInput(left.node_id, right.node_id)],
        source="test", now=NOW,
    ))

    assert asyncio.run(graph_store.wipe_graph("u1")) == 5
    assert _collection_docs(db, F.NODE_SUBCOLLECTION) == {}
    assert _collection_docs(db, F.EDGE_SUBCOLLECTION) == {}
    assert _collection_docs(db, F.ADJ_SUBCOLLECTION) == {}
