"""
Writer/reader contract test for the FCM active-user query.

Guards against the field-name drift that caused a 4-day notification outage: a
reader queried ``last_seen`` while the writer only ever wrote ``registered_at``,
so the query matched zero docs and every dispatch path went silent. Here
``register_token`` (writer) and ``list_active_user_ids`` (reader) share ONE tiny
in-memory store, so renaming either side without the other breaks this test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch


class _FakeFirestore:
    """Minimal dict-backed Firestore: enough surface for register_token's
    nested writes and the collection_group read to share one ``_docs`` store."""

    def __init__(self):
        self._docs: dict[str, dict] = {}  # "users/{uid}/fcm_tokens/{token}" -> fields

    # writer: collection("users").document(uid).collection("fcm_tokens").document(token)
    def collection(self, name):
        return _Chain(self, f"{name}")

    # reader: collection_group("fcm_tokens").where(...).limit(n).stream()
    def collection_group(self, name):
        return _Query(self)


class _Chain:
    """Walks the users/{uid}/fcm_tokens/{token} path, then reads/writes _docs."""

    def __init__(self, store, path):
        self._store = store
        self._path = path

    def document(self, seg):
        return _Chain(self._store, f"{self._path}/{seg}")

    def collection(self, seg):
        return _Chain(self._store, f"{self._path}/{seg}")

    def get(self):
        snap = MagicMock()
        snap.exists = self._path in self._store._docs
        return snap

    def set(self, data):
        self._store._docs[self._path] = dict(data)

    def update(self, data):
        self._store._docs.setdefault(self._path, {}).update(data)


class _Query:
    def __init__(self, store):
        self._store = store
        self._field = self._cutoff = self._limit = None

    def where(self, filter):  # noqa: A002 - mirrors Firestore's FieldFilter kwarg
        self._field, self._cutoff = filter.field_path, filter.value
        return self

    def limit(self, n):
        self._limit = n
        return self

    def stream(self):
        out = []
        for path, data in self._store._docs.items():
            if self._field is not None:
                v = data.get(self._field)  # missing field never matches — the outage's failure mode
                if v is None or v < self._cutoff:
                    continue
            doc = MagicMock()
            doc.reference.path = path
            out.append(doc)
            if self._limit and len(out) >= self._limit:
                break
        return iter(out)


def _patch_db(store):
    from src.services import fcm_token_registry

    return patch.object(fcm_token_registry, "admin_firestore", return_value=store)


def test_register_token_then_list_active_finds_user():
    """The core contract: a token the writer stored is found by the reader.
    Fails if writer/reader field names drift apart."""
    from src.services import fcm_token_registry

    store = _FakeFirestore()
    with _patch_db(store):
        fcm_token_registry.register_token("user_42", "tok_xyz", "android")
        assert fcm_token_registry.list_active_user_ids(inactivity_days=7) == ["user_42"]


def test_dedupes_multiple_tokens_for_one_user():
    from src.services import fcm_token_registry

    store = _FakeFirestore()
    with _patch_db(store):
        fcm_token_registry.register_token("user_42", "tok_a", "android")
        fcm_token_registry.register_token("user_42", "tok_b", "ios")
        assert fcm_token_registry.list_active_user_ids(inactivity_days=7) == ["user_42"]


def test_stale_token_outside_window_is_excluded():
    from src.services import fcm_token_registry

    store = _FakeFirestore()
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    store._docs["users/old_user/fcm_tokens/tok_old"] = {
        fcm_token_registry.FIELD_TOKEN: "tok_old",
        fcm_token_registry.FIELD_PLATFORM: "android",
        fcm_token_registry.FIELD_REGISTERED_AT: old,
    }
    with _patch_db(store):
        assert fcm_token_registry.list_active_user_ids(inactivity_days=7) == []


def test_any_token_registered_probe():
    from src.services import fcm_token_registry

    store = _FakeFirestore()
    with _patch_db(store):
        assert fcm_token_registry.any_token_registered() is False
        fcm_token_registry.register_token("user_42", "tok_xyz", "android")
        assert fcm_token_registry.any_token_registered() is True
