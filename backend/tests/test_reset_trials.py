"""scripts/reset_trials.py: the launch migration must cover EVERY user.

The original script only iterated existing entitlement docs and only touched
trial dates, so an account that never called GET /entitlement got nothing and a
doc stuck on status "expired" stayed dead. These tests pin the fixed contract:
users are enumerated from the users collection, missing docs are created, dead
docs are reset to a live trial, and only genuinely paying accounts are skipped.
"""

from __future__ import annotations

import importlib.util
import os
from datetime import UTC, datetime, timedelta

_SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "reset_trials.py",
)
_spec = importlib.util.spec_from_file_location("reset_trials", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
reset_trials_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reset_trials_module)
reset_trials = reset_trials_module.reset_trials

_LAUNCH = datetime(2026, 8, 1, tzinfo=UTC)


class _FakeSnap:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return self._data


class _FakeEntRef:
    def __init__(self, docs: dict, uid: str):
        self._docs = docs
        self.path = f"users/{uid}/entitlement/current"

    def get(self):
        return _FakeSnap(self._docs.get(self.path))


class _FakeUserRef:
    def __init__(self, docs: dict, uid: str):
        self._docs = docs
        self.id = uid

    def collection(self, name):
        assert name == "entitlement"
        return type("_C", (), {"document": lambda _self, doc_id: _FakeEntRef(self._docs, self.id)})()


class _FakeBatch:
    def __init__(self, docs: dict):
        self._docs = docs
        self._pending: list[tuple[str, dict, bool]] = []

    def set(self, ref, doc, merge=False):
        self._pending.append((ref.path, dict(doc), merge))

    def commit(self):
        for path, doc, merge in self._pending:
            if merge and path in self._docs:
                self._docs[path] = {**self._docs[path], **doc}
            else:
                self._docs[path] = doc
        self._pending = []


class _FakeDb:
    """docs: {"users/{uid}/entitlement/current": {...}}. uids lists every user
    document reference to enumerate, whether or not it has an entitlement doc."""

    def __init__(self, uids: list[str], docs: dict | None = None):
        self._uids = uids
        self.docs = docs or {}

    def collection(self, name):
        assert name == "users"
        db = self

        class _Users:
            def list_documents(self):
                return [_FakeUserRef(db.docs, uid) for uid in db._uids]

            def document(self, uid):
                return _FakeUserRef(db.docs, uid)

        return _Users()

    def batch(self):
        return _FakeBatch(self.docs)


def _ent(uid: str) -> str:
    return f"users/{uid}/entitlement/current"


def test_user_without_entitlement_doc_gets_trial_created():
    db = _FakeDb(["u_new"])
    counts = reset_trials(db, _LAUNCH, apply=True)

    assert counts == {"reset": 0, "created": 1, "skipped_paid": 0}
    doc = db.docs[_ent("u_new")]
    assert doc["tier"] == "free"
    assert doc["status"] == "trialing"
    assert doc["trial_start_date"] == _LAUNCH
    assert doc["trial_end_date"] == _LAUNCH + timedelta(days=45)
    assert doc["trial_notified_3d"] is False
    assert doc["trial_notified_expired"] is False


def test_expired_free_doc_reset_to_live_trial():
    # The original bug: only dates were updated, so status "expired" kept the
    # account dead through tier resolution no matter the new trial_end_date.
    db = _FakeDb(["u1"], {_ent("u1"): {
        "tier": "free", "status": "expired",
        "trial_end_date": datetime(2026, 1, 1, tzinfo=UTC),
    }})
    counts = reset_trials(db, _LAUNCH, apply=True)

    assert counts["reset"] == 1
    doc = db.docs[_ent("u1")]
    assert doc["status"] == "trialing"
    assert doc["tier"] == "free"
    assert doc["trial_end_date"] == _LAUNCH + timedelta(days=45)


def test_actively_paying_account_untouched():
    original = {"tier": "pro", "status": "active", "dodo_customer_id": "cus_1"}
    db = _FakeDb(["u_paid"], {_ent("u_paid"): dict(original)})
    counts = reset_trials(db, _LAUNCH, apply=True)

    assert counts == {"reset": 0, "created": 0, "skipped_paid": 1}
    assert db.docs[_ent("u_paid")] == original


def test_churned_paid_account_gets_trial_and_keeps_billing_ids():
    # Paid tier but date-expired (missed/lapsed period long past the renewal
    # grace): a churned user, reset to a live trial. merge=True must preserve
    # the Dodo linkage for a future re-purchase.
    db = _FakeDb(["u_churn"], {_ent("u_churn"): {
        "tier": "companion", "status": "active", "cancel_at_period_end": False,
        "expires_at": datetime.now(UTC) - timedelta(days=30),
        "dodo_customer_id": "cus_9", "dodo_subscription_id": "sub_9",
    }})
    counts = reset_trials(db, _LAUNCH, apply=True)

    assert counts["reset"] == 1
    doc = db.docs[_ent("u_churn")]
    assert doc["tier"] == "free"
    assert doc["status"] == "trialing"
    assert doc["dodo_customer_id"] == "cus_9"
    assert doc["dodo_subscription_id"] == "sub_9"


def test_dry_run_writes_nothing():
    db = _FakeDb(["u_new", "u1"], {_ent("u1"): {"tier": "free", "status": "expired"}})
    counts = reset_trials(db, _LAUNCH, apply=False)

    assert counts["created"] == 1
    assert counts["reset"] == 1
    assert _ent("u_new") not in db.docs
    assert db.docs[_ent("u1")]["status"] == "expired"


def test_only_uid_limits_the_run():
    db = _FakeDb(["u1", "u2"], {
        _ent("u1"): {"tier": "free", "status": "expired"},
        _ent("u2"): {"tier": "free", "status": "expired"},
    })
    counts = reset_trials(db, _LAUNCH, apply=True, only_uid="u1")

    assert counts["reset"] == 1
    assert db.docs[_ent("u1")]["status"] == "trialing"
    assert db.docs[_ent("u2")]["status"] == "expired"


def test_many_users_cross_batch_boundary():
    uids = [f"u{i}" for i in range(401)]  # one past _BATCH_SIZE = 400
    db = _FakeDb(uids)
    counts = reset_trials(db, _LAUNCH, apply=True)

    assert counts["created"] == 401
    assert all(db.docs[_ent(uid)]["status"] == "trialing" for uid in uids)
