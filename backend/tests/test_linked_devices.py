from datetime import UTC, datetime, timedelta

from src.services import linked_devices


class _Snapshot:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None


class _Document:
    def __init__(self, data=None):
        self.data = data

    def get(self, transaction=None):
        return _Snapshot(self.data)


class _Transaction:
    def set(self, ref, payload, merge=False):
        ref.data = {**(ref.data or {}), **payload} if merge else dict(payload)


class _Collection:
    def __init__(self, db, path):
        self.db = db
        self.path = path

    def document(self, doc_id):
        path = f"{self.path}/{doc_id}"
        return self.db.documents.setdefault(path, _Document())


class _UserDocument(_Document):
    def __init__(self, db, path):
        super().__init__()
        self.db = db
        self.path = path

    def collection(self, name):
        return _Collection(self.db, f"{self.path}/{name}")


class _UsersCollection:
    def __init__(self, db):
        self.db = db

    def document(self, user_id):
        return _UserDocument(self.db, f"users/{user_id}")


class _Db:
    def __init__(self):
        self.documents = {}
        self.txn = _Transaction()

    def collection(self, name):
        assert name == "users"
        return _UsersCollection(self)

    def transaction(self):
        return self.txn


def test_upsert_creates_one_canonical_timestamp_record(monkeypatch):
    monkeypatch.setattr(linked_devices.gcloud_firestore, "transactional", lambda fn: fn)
    db = _Db()
    install_id = "550e8400-e29b-41d4-a716-446655440000"
    linked_at = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)

    result = linked_devices.upsert_linked_device(
        db, "user-a", install_id, "Office PC", now=linked_at
    )

    assert result == install_id
    assert list(db.documents) == [f"users/user-a/linked_devices/{install_id}"]
    assert db.documents[list(db.documents)[0]].data["linked_at"] == linked_at
    assert db.documents[list(db.documents)[0]].data["schema_version"] == 2


def test_upsert_enriches_same_record_without_changing_link_time(monkeypatch):
    monkeypatch.setattr(linked_devices.gcloud_firestore, "transactional", lambda fn: fn)
    db = _Db()
    install_id = "550e8400-e29b-41d4-a716-446655440000"
    linked_at = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    later = linked_at + timedelta(hours=2)

    linked_devices.upsert_linked_device(db, "user-a", install_id, "Office PC", now=linked_at)
    linked_devices.upsert_linked_device(
        db,
        "user-a",
        install_id,
        "Office PC",
        now=later,
        metadata={"app_version": "0.9.0"},
    )

    assert len(db.documents) == 1
    record = db.documents[f"users/user-a/linked_devices/{install_id}"].data
    assert record["linked_at"] == linked_at
    assert record["last_seen_at"] == later
    assert record["app_version"] == "0.9.0"
