"""
Dashboard-link handshake tests (handlers/dashboard_link.py).

Mirrors test_pairing.py's test-double approach, scaled down: no attempts
counter, no per-uid cap, no linked-device/push side effects. Pins the
contracts that matter for a token that IS the credential:
  - start requires auth; writes the token doc via a plain create() (not set)
    with a 60s expiry;
  - claim is a one-time atomic exchange: every failure mode (missing, wrong,
    expired, used, malformed) answers the identical invalid_or_expired body
    (no oracle), a claimed token cannot be claimed twice, a concurrent double
    claim yields exactly one winner, and a successful claim mints a custom
    token for the right uid.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src.handlers import dashboard_link

NOW_WINDOW = timedelta(seconds=dashboard_link.DASHBOARD_LINK_TOKEN_TTL_SECONDS)
TOKEN = "A" * 43


# ── Test doubles ─────────────────────────────────────────────────────────────
class _Req:
    """Minimal stand-in for a FastAPI Request: only json() is read (auth is
    monkeypatched, so headers are irrelevant)."""

    client = None

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
        assert self.data is None, "create() on an existing doc"
        self.data = dict(payload)


class _FakeTxn:
    """Applies writes to _FakeDocRef state so a second claim observes the first."""

    def update(self, ref: _FakeDocRef, payload: dict) -> None:
        assert ref.data is not None, "transaction.update on a missing doc"
        ref.data.update(payload)


class _FakeDb:
    """Routes the single collection root the handler touches:
    dashboard_link_codes/{token}."""

    def __init__(self, code_docs: dict[str, dict | None] | None = None):
        self.code_refs: dict[str, _FakeDocRef] = {
            token: _FakeDocRef(data) for token, data in (code_docs or {}).items()
        }
        self.txn = _FakeTxn()

    def collection(self, name: str):
        assert name == dashboard_link.DASHBOARD_LINK_CODES_COLLECTION
        col = MagicMock()
        col.document.side_effect = (
            lambda token: self.code_refs.setdefault(token, _FakeDocRef(None))
        )
        return col

    def transaction(self):
        return self.txn


@pytest.fixture(autouse=True)
def _passthrough_transactional(monkeypatch):
    """gcloud's @transactional needs a live server; run the body inline instead."""
    monkeypatch.setattr("google.cloud.firestore.transactional", lambda fn: fn)


def _body(resp) -> dict:
    return json.loads(resp.body)


def _live_token_doc(uid: str = "u1", **overrides) -> dict:
    now = datetime.now(UTC)
    doc = {
        dashboard_link.FIELD_UID: uid,
        dashboard_link.FIELD_CREATED_AT: now,
        dashboard_link.FIELD_EXPIRES_AT: now + NOW_WINDOW,
        dashboard_link.FIELD_USED: False,
    }
    doc.update(overrides)
    return doc


# ── 1. start: auth ────────────────────────────────────────────────────────────
async def test_start_unauthenticated_returns_401(monkeypatch):
    monkeypatch.setattr(dashboard_link, "resolve_user_id_from_request", lambda r: None)
    resp = await dashboard_link.handle_dashboard_link_start(_Req())
    assert resp.status_code == 401


# ── 2. start: happy path ──────────────────────────────────────────────────────
async def test_start_writes_token_doc_and_returns_shape(monkeypatch):
    db = _FakeDb()
    monkeypatch.setattr(dashboard_link, "resolve_user_id_from_request", lambda r: "u1")
    monkeypatch.setattr(dashboard_link, "admin_firestore", lambda: db)

    resp = await dashboard_link.handle_dashboard_link_start(_Req())

    assert resp.status_code == 200
    body = _body(resp)
    assert body["expires_in_seconds"] == 60
    token = body["code"]
    assert len(token) == 43

    token_doc = db.code_refs[token].data
    assert token_doc is not None
    assert token_doc[dashboard_link.FIELD_UID] == "u1"
    assert token_doc[dashboard_link.FIELD_USED] is False
    expiry_delta = (
        token_doc[dashboard_link.FIELD_EXPIRES_AT] - token_doc[dashboard_link.FIELD_CREATED_AT]
    )
    assert expiry_delta == timedelta(seconds=60)


# ── 3. claim: every failure mode answers identically ──────────────────────────
async def test_claim_missing_token_returns_invalid_or_expired(monkeypatch):
    db = _FakeDb()  # no token docs at all
    monkeypatch.setattr(dashboard_link, "admin_firestore", lambda: db)

    resp = await dashboard_link.handle_dashboard_link_claim(_Req({"code": TOKEN}))

    assert resp.status_code == 400
    assert _body(resp) == {"error": "invalid_or_expired"}


async def test_claim_expired_token_returns_invalid_or_expired(monkeypatch):
    expired = _live_token_doc(
        uid="u1", **{dashboard_link.FIELD_EXPIRES_AT: datetime.now(UTC) - timedelta(seconds=1)}
    )
    db = _FakeDb(code_docs={TOKEN: expired})
    monkeypatch.setattr(dashboard_link, "admin_firestore", lambda: db)

    resp = await dashboard_link.handle_dashboard_link_claim(_Req({"code": TOKEN}))

    assert resp.status_code == 400
    assert _body(resp) == {"error": "invalid_or_expired"}


async def test_claim_used_token_returns_invalid_or_expired(monkeypatch):
    used = _live_token_doc(uid="u1", **{dashboard_link.FIELD_USED: True})
    db = _FakeDb(code_docs={TOKEN: used})
    monkeypatch.setattr(dashboard_link, "admin_firestore", lambda: db)

    resp = await dashboard_link.handle_dashboard_link_claim(_Req({"code": TOKEN}))

    assert resp.status_code == 400
    assert _body(resp) == {"error": "invalid_or_expired"}


async def test_claim_malformed_token_returns_same_body(monkeypatch):
    # Wrong length / bad character / missing field never even reach Firestore.
    monkeypatch.setattr(dashboard_link, "admin_firestore", MagicMock(side_effect=AssertionError))

    resp = await dashboard_link.handle_dashboard_link_claim(_Req({"code": "tooshort"}))
    assert resp.status_code == 400
    assert _body(resp) == {"error": "invalid_or_expired"}

    resp = await dashboard_link.handle_dashboard_link_claim(_Req({"code": "../" + "A" * 40}))
    assert resp.status_code == 400
    assert _body(resp) == {"error": "invalid_or_expired"}

    resp = await dashboard_link.handle_dashboard_link_claim(_Req({}))
    assert resp.status_code == 400
    assert _body(resp) == {"error": "invalid_or_expired"}


# ── 4. claim: happy path ───────────────────────────────────────────────────────
async def test_claim_happy_path_mints_token_for_right_uid(monkeypatch):
    db = _FakeDb(code_docs={TOKEN: _live_token_doc(uid="u1")})
    auth = MagicMock()
    auth.create_custom_token.return_value = b"tok123"  # bytes: the decode path
    monkeypatch.setattr(dashboard_link, "admin_firestore", lambda: db)
    monkeypatch.setattr(dashboard_link, "admin_auth", lambda: auth)

    resp = await dashboard_link.handle_dashboard_link_claim(_Req({"code": TOKEN}))

    assert resp.status_code == 200
    assert _body(resp)["custom_token"] == "tok123"
    auth.create_custom_token.assert_called_once_with("u1")
    assert db.code_refs[TOKEN].data[dashboard_link.FIELD_USED] is True


# ── 5. claim: cannot be claimed twice ─────────────────────────────────────────
async def test_claim_cannot_be_claimed_twice(monkeypatch):
    db = _FakeDb(code_docs={TOKEN: _live_token_doc(uid="u1")})
    auth = MagicMock()
    auth.create_custom_token.return_value = "tok123"
    monkeypatch.setattr(dashboard_link, "admin_firestore", lambda: db)
    monkeypatch.setattr(dashboard_link, "admin_auth", lambda: auth)

    first = await dashboard_link.handle_dashboard_link_claim(_Req({"code": TOKEN}))
    assert first.status_code == 200

    second = await dashboard_link.handle_dashboard_link_claim(_Req({"code": TOKEN}))
    assert second.status_code == 400
    assert _body(second) == {"error": "invalid_or_expired"}


# ── 6. claim: concurrent double-claim, exactly one winner ─────────────────────
async def test_claim_double_claim_exactly_one_succeeds(monkeypatch):
    # Firestore serializes the two transactions; the loser's (re)run reads the
    # winner's used=true. The stateful _FakeTxn reproduces that: the first claim
    # mutates the shared doc, the second transactional callable sees it.
    db = _FakeDb(code_docs={TOKEN: _live_token_doc(uid="u1")})
    auth = MagicMock()
    auth.create_custom_token.return_value = "tok123"
    monkeypatch.setattr(dashboard_link, "admin_firestore", lambda: db)
    monkeypatch.setattr(dashboard_link, "admin_auth", lambda: auth)

    first = await dashboard_link.handle_dashboard_link_claim(_Req({"code": TOKEN}))
    second = await dashboard_link.handle_dashboard_link_claim(_Req({"code": TOKEN}))

    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [200, 400]
    auth.create_custom_token.assert_called_once()  # exactly one token ever minted


# ── 7. claim: mint failure -> 500 ─────────────────────────────────────────────
async def test_claim_mint_failure_returns_500(monkeypatch):
    db = _FakeDb(code_docs={TOKEN: _live_token_doc(uid="u1")})
    auth = MagicMock()
    auth.create_custom_token.side_effect = RuntimeError("missing iam.signBlob")
    spy_logger = MagicMock()
    monkeypatch.setattr(dashboard_link, "admin_firestore", lambda: db)
    monkeypatch.setattr(dashboard_link, "admin_auth", lambda: auth)
    monkeypatch.setattr(dashboard_link, "logger", spy_logger)

    resp = await dashboard_link.handle_dashboard_link_claim(_Req({"code": TOKEN}))

    assert resp.status_code == 500
    logged_messages = [call.args[0] for call in spy_logger.error.call_args_list]
    assert "DashboardLink: custom token mint failed" in logged_messages
