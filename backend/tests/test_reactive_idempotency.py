"""The ONE shared idempotency primitive.

Proves the create-if-absent claim: the first caller proceeds, a duplicate/out-of-
order redelivery is suppressed, distinct keys and scopes are independent, and the
primitive fails OPEN (proceeds) when the store is unreachable.
"""

from __future__ import annotations

from google.api_core.exceptions import AlreadyExists  # type: ignore

from src.services.reactive import idempotency


class _FakeChain:
    """Minimal fake of the Firestore doc-ref chain. ``create`` raises AlreadyExists
    if the full path was already created, modelling the server's atomic
    create-if-absent."""

    def __init__(self, store: dict, parts: tuple = ()):
        self._store = store
        self._parts = parts

    def collection(self, name: str) -> _FakeChain:
        return _FakeChain(self._store, self._parts + (name,))

    def document(self, doc_id: str) -> _FakeChain:
        return _FakeChain(self._store, self._parts + (doc_id,))

    def create(self, data: dict) -> None:
        if self._parts in self._store:
            raise AlreadyExists("document already exists")
        self._store[self._parts] = data


async def test_first_claim_proceeds_then_duplicate_is_suppressed(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(idempotency, "admin_firestore", lambda: _FakeChain(store))

    first = await idempotency.idempotent("k1", scope="u1")
    second = await idempotency.idempotent("k1", scope="u1")

    assert first is True       # do the side effect
    assert second is False     # skip — already processed


async def test_distinct_keys_and_scopes_are_independent(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(idempotency, "admin_firestore", lambda: _FakeChain(store))

    assert await idempotency.idempotent("k1", scope="u1") is True
    assert await idempotency.idempotent("k2", scope="u1") is True   # different key
    assert await idempotency.idempotent("k1", scope="u2") is True   # different user


async def test_claim_writes_under_user_processed_path(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(idempotency, "admin_firestore", lambda: _FakeChain(store))

    await idempotency.idempotent("k1", scope="u1")

    # users/{scope}/processed/{key}
    assert ("users", "u1", "processed", "k1") in store


async def test_fails_open_when_store_unreachable(monkeypatch):
    def _boom():
        raise RuntimeError("firestore down")

    monkeypatch.setattr(idempotency, "admin_firestore", _boom)

    # A rare duplicate beats silently dropping a user-facing action.
    assert await idempotency.idempotent("k1", scope="u1") is True
