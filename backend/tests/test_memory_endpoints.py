"""
A2 inspectable + forgettable memory: the list_atoms reader and the
GET/DELETE /aura/memory handlers.

Verifies the reader shapes/caps rows (no embedding leaked) and that the handlers
group by type, enforce auth, and route a delete through to the store. Firestore +
auth + the store are faked so the test is deterministic and offline.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

from src.handlers import aura
from src.services.memory import atom_store, fields as F


# --- fake Firestore query chain for list_atoms ----------------------------
class _Snap:
    def __init__(self, id_, data):
        self.id = id_
        self._d = data

    def to_dict(self):
        return self._d


class _Query:
    def __init__(self, snaps):
        self._snaps = snaps
        self._lim = None

    def order_by(self, _field, direction=None):  # noqa: ARG002 - signature parity
        return self

    def limit(self, n):
        self._lim = n
        return self

    def stream(self):
        return iter(self._snaps[: self._lim] if self._lim else self._snaps)


class _Doc:
    def __init__(self, snaps):
        self._snaps = snaps

    def collection(self, _name):
        return _Query(self._snaps)


class _Parent:
    def __init__(self, snaps):
        self._snaps = snaps

    def document(self, _uid):
        return _Doc(self._snaps)


class _Db:
    def __init__(self, snaps):
        self._snaps = snaps

    def collection(self, _name):
        return _Parent(self._snaps)


def _snap(id_, text, atom_type, last_seen, categories=None):
    return _Snap(id_, {
        F.TEXT: text,
        F.ATOM_TYPE: atom_type,
        F.CATEGORIES: categories or [],
        F.LAST_SEEN: last_seen,
        F.EMBEDDING: [0.1, 0.2, 0.3],  # present in storage, must NOT leak to the client
    })


# --- list_atoms -----------------------------------------------------------
def test_list_atoms_shapes_rows_and_omits_embedding(monkeypatch):
    snaps = [
        _snap("a1", "lives in Hyderabad", F.ATOM_TYPE_FACT, "2026-06-23T12:00:00+00:00"),
        _snap("a2", "job hunt storyline", F.ATOM_TYPE_STORYLINE, "2026-06-22T12:00:00+00:00", ["career_jobs"]),
    ]
    monkeypatch.setattr(atom_store, "admin_firestore", lambda: _Db(snaps))
    rows = asyncio.run(atom_store.list_atoms("u1", limit=10))
    assert [r["id"] for r in rows] == ["a1", "a2"]
    assert rows[0]["text"] == "lives in Hyderabad"
    assert rows[0]["atom_type"] == F.ATOM_TYPE_FACT
    assert "embedding" not in rows[0] and F.EMBEDDING not in rows[0]


def test_list_atoms_respects_limit(monkeypatch):
    snaps = [_snap(f"a{i}", f"t{i}", F.ATOM_TYPE_FACT, "2026-06-23T12:00:00+00:00") for i in range(5)]
    monkeypatch.setattr(atom_store, "admin_firestore", lambda: _Db(snaps))
    rows = asyncio.run(atom_store.list_atoms("u1", limit=2))
    assert len(rows) == 2


# --- GET /aura/memory -----------------------------------------------------
def test_get_memory_groups_by_type(monkeypatch):
    monkeypatch.setattr(aura, "resolve_user_id_from_request", lambda _r: "u1")

    async def _fake_list(_uid, **_kw):
        return [
            {"id": "a1", "text": "fact1", "atom_type": F.ATOM_TYPE_FACT, "categories": [], "last_seen": "t1"},
            {"id": "a2", "text": "story1", "atom_type": F.ATOM_TYPE_STORYLINE, "categories": [], "last_seen": "t2"},
            {"id": "a3", "text": "KCR", "atom_type": F.ATOM_TYPE_INTEREST_SUBJECT, "categories": ["x"], "last_seen": "t3"},
        ]

    monkeypatch.setattr(aura, "list_atoms", _fake_list)
    resp = asyncio.run(aura.handle_get_memory(MagicMock()))
    body = json.loads(bytes(resp.body))
    assert resp.status_code == 200
    assert body["total"] == 3
    assert body["memory"]["facts"][0]["text"] == "fact1"
    assert body["memory"]["storylines"][0]["text"] == "story1"
    assert body["memory"]["interests"][0]["id"] == "a3"


def test_get_memory_unauthorized(monkeypatch):
    monkeypatch.setattr(aura, "resolve_user_id_from_request", lambda _r: None)
    resp = asyncio.run(aura.handle_get_memory(MagicMock()))
    assert resp.status_code == 401


# --- DELETE /aura/memory/{atom_id} ---------------------------------------
def test_delete_memory_routes_to_store(monkeypatch):
    monkeypatch.setattr(aura, "resolve_user_id_from_request", lambda _r: "u1")
    seen: dict = {}

    async def _fake_delete(uid, atom_id):
        seen["uid"], seen["atom_id"] = uid, atom_id
        return True

    monkeypatch.setattr(aura, "delete_atom", _fake_delete)
    resp = asyncio.run(aura.handle_delete_memory(MagicMock(), "fact_abc123"))
    body = json.loads(bytes(resp.body))
    assert resp.status_code == 200 and body["ok"] is True
    assert seen == {"uid": "u1", "atom_id": "fact_abc123"}


def test_delete_memory_unauthorized(monkeypatch):
    monkeypatch.setattr(aura, "resolve_user_id_from_request", lambda _r: None)
    resp = asyncio.run(aura.handle_delete_memory(MagicMock(), "x"))
    assert resp.status_code == 401


def test_delete_memory_missing_id(monkeypatch):
    monkeypatch.setattr(aura, "resolve_user_id_from_request", lambda _r: "u1")
    resp = asyncio.run(aura.handle_delete_memory(MagicMock(), "   "))
    assert resp.status_code == 400
