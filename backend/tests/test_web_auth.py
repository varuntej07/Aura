"""
Browser-based Google sign-up handshake (handlers/web_auth.py).

Pins the contracts that matter for an endpoint pair where the session code is
the ONLY gate on both ends (there is no uid to authenticate against until the
browser leg completes):
  - start issues a long random code and writes a pending doc via create() (not
    set), so a collision would error loudly rather than silently overwrite;
  - status is a one-time read-and-consume: pending stays pending untouched,
    an expired-but-still-pending doc is deleted and reported expired, and a
    completed/failed doc is deleted in the SAME transaction as the read that
    reports it — so a second read of the same code is always not_found
    (structural single-use, not conventional);
  - a concurrent double-read of a completed doc yields exactly one winner;
  - a successful completion records a linked device and fires the same
    "new device" push pairing's claim does, without failing the response if
    that push fails;
  - missing/empty code is a 400, not swallowed into "not_found".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.handlers import pairing, web_auth
from src.services.notifications import device_link_push

NOW_WINDOW = timedelta(seconds=web_auth.WEB_AUTH_SESSION_TTL_SECONDS)


# ── Test doubles (local to this file; mirrors test_pairing.py's shape) ──────
class _Req:
    def __init__(self, body: dict | None = None):
        self._body = body or {}

    async def json(self):
        return self._body


class _FakeSnap:
    def __init__(self, data: dict | None):
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _FakeDocRef:
    """A stateful document: .data is the live dict (None = missing)."""

    def __init__(self, data: dict | None = None):
        self.data = data

    def get(self, transaction=None):
        return _FakeSnap(self.data)

    def create(self, payload: dict) -> None:
        """Plain (non-transactional) create, matching the real Firestore SDK's
        DocumentReference.create() used by handle_web_auth_start."""
        assert self.data is None, "create() on an existing doc"
        self.data = dict(payload)


class _FakeTxn:
    """Applies writes to _FakeDocRef state so a second status check observes
    the first one's mutation (models Firestore's real serialization)."""

    def delete(self, ref: _FakeDocRef) -> None:
        ref.data = None


class _FakeDb:
    """Routes the two collection roots the handler touches:
    web_auth_sessions/{code} and users/{uid}/linked_devices/{auto_id}."""

    def __init__(self, session_docs: dict[str, dict | None] | None = None):
        self.session_refs: dict[str, _FakeDocRef] = {
            code: _FakeDocRef(data) for code, data in (session_docs or {}).items()
        }
        self.linked_ref = MagicMock()
        self.linked_ref.id = "dev123"
        # Stable root users/{uid} ref: routes linked_devices AND captures the
        # denormalized surface-footprint .set() the sign-in now writes onto it.
        self.user_root_ref = MagicMock()
        self.user_root_ref.collection.side_effect = self._subcollection
        self.txn = _FakeTxn()

    def _subcollection(self, sub_name: str):
        sub = MagicMock()
        sub.document.return_value = self.linked_ref  # linked_devices auto-id
        return sub

    def collection(self, name: str):
        col = MagicMock()
        if name == web_auth.WEB_AUTH_SESSIONS_COLLECTION:
            col.document.side_effect = (
                lambda code: self.session_refs.setdefault(code, _FakeDocRef(None))
            )
        else:  # users
            col.document.return_value = self.user_root_ref
        return col

    def transaction(self):
        return self.txn


@pytest.fixture(autouse=True)
def _passthrough_transactional(monkeypatch):
    """gcloud's @transactional needs a live server; run the body inline instead."""
    monkeypatch.setattr("google.cloud.firestore.transactional", lambda fn: fn)


@pytest.fixture(autouse=True)
def _reset_velocity_tracker():
    web_auth._recent_start_seconds.clear()
    yield
    web_auth._recent_start_seconds.clear()


def _body(resp) -> dict:
    return json.loads(resp.body)


def _pending_doc(**overrides) -> dict:
    now = datetime.now(UTC)
    doc = {
        web_auth.FIELD_STATUS: web_auth.STATUS_PENDING,
        web_auth.FIELD_CREATED_AT: now,
        web_auth.FIELD_EXPIRES_AT: now + NOW_WINDOW,
        web_auth.FIELD_DEVICE_NAME: "Windows PC",
    }
    doc.update(overrides)
    return doc


# ── 1. start: happy path ─────────────────────────────────────────────────────
async def test_start_writes_session_doc_and_returns_shape(monkeypatch):
    db = _FakeDb()
    monkeypatch.setattr(web_auth, "admin_firestore", lambda: db)

    resp = await web_auth.handle_web_auth_start(_Req({"device_name": "Varun's PC"}))

    assert resp.status_code == 200
    body = _body(resp)
    assert body["expires_in_seconds"] == 600
    code = body["code"]
    assert isinstance(code, str) and len(code) > 20  # token_urlsafe(24) is long

    doc = db.session_refs[code].data
    assert doc is not None
    assert doc[web_auth.FIELD_STATUS] == web_auth.STATUS_PENDING
    assert doc[web_auth.FIELD_DEVICE_NAME] == "Varun's PC"
    expiry_delta = doc[web_auth.FIELD_EXPIRES_AT] - doc[web_auth.FIELD_CREATED_AT]
    assert expiry_delta == timedelta(seconds=600)


async def test_start_defaults_device_name_when_absent(monkeypatch):
    db = _FakeDb()
    monkeypatch.setattr(web_auth, "admin_firestore", lambda: db)

    resp = await web_auth.handle_web_auth_start(_Req())

    assert resp.status_code == 200
    code = _body(resp)["code"]
    assert db.session_refs[code].data[web_auth.FIELD_DEVICE_NAME] == "Windows PC"


# ── 2. status: pending, not expired ──────────────────────────────────────────
async def test_status_pending_returns_pending_untouched(monkeypatch):
    db = _FakeDb(session_docs={"code1": _pending_doc()})
    monkeypatch.setattr(web_auth, "admin_firestore", lambda: db)

    resp = await web_auth.handle_web_auth_status(_Req({"code": "code1"}))

    assert resp.status_code == 200
    assert _body(resp) == {"status": "pending"}
    assert db.session_refs["code1"].data is not None  # untouched


# ── 3. status: pending but expired ───────────────────────────────────────────
async def test_status_expired_pending_deletes_and_returns_expired(monkeypatch):
    now = datetime.now(UTC)
    db = _FakeDb(session_docs={
        "code1": _pending_doc(**{web_auth.FIELD_EXPIRES_AT: now - timedelta(seconds=5)}),
    })
    monkeypatch.setattr(web_auth, "admin_firestore", lambda: db)

    resp = await web_auth.handle_web_auth_status(_Req({"code": "code1"}))

    assert resp.status_code == 200
    assert _body(resp) == {"status": "expired"}
    assert db.session_refs["code1"].data is None


# ── 4. status: missing code ──────────────────────────────────────────────────
async def test_status_missing_session_returns_not_found(monkeypatch):
    db = _FakeDb()
    monkeypatch.setattr(web_auth, "admin_firestore", lambda: db)

    resp = await web_auth.handle_web_auth_status(_Req({"code": "nope"}))

    assert resp.status_code == 200
    assert _body(resp) == {"status": "not_found"}


async def test_status_missing_code_in_body_returns_400(monkeypatch):
    resp = await web_auth.handle_web_auth_status(_Req({}))
    assert resp.status_code == 400
    assert _body(resp)["error"] == "missing_code"


async def test_status_empty_code_returns_400(monkeypatch):
    resp = await web_auth.handle_web_auth_status(_Req({"code": "   "}))
    assert resp.status_code == 400


# ── 5. status: completed, exactly once ───────────────────────────────────────
async def test_status_completed_returns_token_once_then_not_found(monkeypatch):
    db = _FakeDb(session_docs={
        "code1": _pending_doc(**{
            web_auth.FIELD_STATUS: web_auth.STATUS_COMPLETED,
            web_auth.FIELD_UID: "u1",
            web_auth.FIELD_CUSTOM_TOKEN: "tok123",
        }),
    })
    monkeypatch.setattr(web_auth, "admin_firestore", lambda: db)
    push = AsyncMock()
    monkeypatch.setattr(device_link_push.orchestrator, "submit", push)

    first = await web_auth.handle_web_auth_status(_Req({"code": "code1"}))
    assert first.status_code == 200
    assert _body(first) == {"status": "completed", "custom_token": "tok123"}

    # Linked device recorded + push fired, mirroring pairing's claim success path.
    linked_payload = db.linked_ref.set.call_args.args[0]
    assert linked_payload[web_auth.FIELD_PLATFORM] == "windows"

    # Surface footprint denormalized onto the root user doc (same helper pairing
    # uses): windows array-unioned in via a non-clobbering merge, plus a timestamp.
    root_payload = db.user_root_ref.set.call_args.args[0]
    assert db.user_root_ref.set.call_args.kwargs["merge"] is True
    assert root_payload[pairing.FIELD_LINKED_PLATFORMS].values == ["windows"]
    # ISO string, not a native datetime — the shared root-doc contract (ECOSYSTEM.md).
    assert isinstance(root_payload[pairing.FIELD_LAST_DESKTOP_ACTIVE_AT], str)

    push.assert_awaited_once()
    sent = push.call_args.args[0]
    assert sent.dedup_key == "device_link:dev123"

    # Replay: the doc was deleted in the same transaction as the first read.
    second = await web_auth.handle_web_auth_status(_Req({"code": "code1"}))
    assert _body(second) == {"status": "not_found"}


async def test_status_completed_push_failure_does_not_fail_response(monkeypatch):
    db = _FakeDb(session_docs={
        "code1": _pending_doc(**{
            web_auth.FIELD_STATUS: web_auth.STATUS_COMPLETED,
            web_auth.FIELD_UID: "u1",
            web_auth.FIELD_CUSTOM_TOKEN: "tok123",
        }),
    })
    monkeypatch.setattr(web_auth, "admin_firestore", lambda: db)
    monkeypatch.setattr(
        device_link_push.orchestrator, "submit", AsyncMock(side_effect=RuntimeError("fcm down"))
    )

    resp = await web_auth.handle_web_auth_status(_Req({"code": "code1"}))

    assert resp.status_code == 200
    assert _body(resp)["status"] == "completed"
    assert _body(resp)["custom_token"] == "tok123"


# ── 6. status: failed, exactly once ──────────────────────────────────────────
async def test_status_failed_returns_reason_once_then_not_found(monkeypatch):
    db = _FakeDb(session_docs={
        "code1": _pending_doc(**{
            web_auth.FIELD_STATUS: web_auth.STATUS_FAILED,
            web_auth.FIELD_FAILURE_REASON: "account_exists_different_credential",
        }),
    })
    monkeypatch.setattr(web_auth, "admin_firestore", lambda: db)

    first = await web_auth.handle_web_auth_status(_Req({"code": "code1"}))
    assert _body(first) == {
        "status": "failed",
        "reason": "account_exists_different_credential",
    }

    second = await web_auth.handle_web_auth_status(_Req({"code": "code1"}))
    assert _body(second) == {"status": "not_found"}


# ── 7. status: concurrent double-read, exactly one winner ───────────────────
async def test_status_double_read_of_completed_exactly_one_gets_token(monkeypatch):
    db = _FakeDb(session_docs={
        "code1": _pending_doc(**{
            web_auth.FIELD_STATUS: web_auth.STATUS_COMPLETED,
            web_auth.FIELD_UID: "u1",
            web_auth.FIELD_CUSTOM_TOKEN: "tok123",
        }),
    })
    monkeypatch.setattr(web_auth, "admin_firestore", lambda: db)
    monkeypatch.setattr(device_link_push.orchestrator, "submit", AsyncMock())

    first = await web_auth.handle_web_auth_status(_Req({"code": "code1"}))
    second = await web_auth.handle_web_auth_status(_Req({"code": "code1"}))

    outcomes = sorted([_body(first)["status"], _body(second)["status"]])
    assert outcomes == ["completed", "not_found"]
