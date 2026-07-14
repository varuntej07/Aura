"""
Device pairing for the Aura desktop companion (handlers/pairing.py).

Pins the contracts that matter for an endpoint where the code IS the credential:
  - pair/start requires auth; writes the code doc atomically (create, not set)
    with a 300s expiry; enforces the per-uid live-code cap with a 429;
  - pair/claim is a one-time atomic exchange: every failure mode (missing, wrong,
    expired, used, locked out) answers the identical invalid_or_expired body
    (no oracle), failed attempts increment on existing docs, a concurrent double
    claim yields exactly one winner, and a mint failure logs the EXACT tripwire
    line "Pairing: custom token mint failed";
  - unlink deletes the device doc then revokes ALL refresh tokens;
  - the device_link source is registered in the proposal contract;
  - "desktop" is a known voice surface (no longer collapses to "app").
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import BackgroundTasks

from src.handlers import pairing
from src.services.notifications import device_link_push, proposal as proposal_mod

NOW_WINDOW = timedelta(seconds=pairing.PAIRING_CODE_TTL_SECONDS)


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
        self.set_calls: list[tuple[dict, bool]] = []

    def get(self, transaction=None):
        return _FakeSnap(self.data)


class _FakeTxn:
    """Applies writes to _FakeDocRef state so a second claim observes the first."""

    def update(self, ref: _FakeDocRef, payload: dict) -> None:
        assert ref.data is not None, "transaction.update on a missing doc"
        ref.data.update(payload)

    def set(self, ref: _FakeDocRef, payload: dict, merge: bool = False) -> None:
        ref.set_calls.append((payload, merge))
        if not merge:
            ref.data = dict(payload)

    def create(self, ref: _FakeDocRef, payload: dict) -> None:
        assert ref.data is None, "transaction.create on an existing doc"
        ref.data = dict(payload)

    def delete(self, ref: _FakeDocRef) -> None:
        ref.data = None


class _FakeDb:
    """Routes the two collection roots the handler touches:
    pairing_codes/{code} and users/{uid}/{pairing_state|linked_devices}/..."""

    def __init__(self, code_docs: dict[str, dict | None] | None = None,
                 state_data: dict | None = None):
        self.code_refs: dict[str, _FakeDocRef] = {
            code: _FakeDocRef(data) for code, data in (code_docs or {}).items()
        }
        self.state_ref = _FakeDocRef(state_data)
        self.linked_ref = MagicMock()
        self.linked_ref.id = "dev123"
        self.txn = _FakeTxn()

    def collection(self, name: str):
        col = MagicMock()
        if name == pairing.PAIRING_CODES_COLLECTION:
            col.document.side_effect = (
                lambda code: self.code_refs.setdefault(code, _FakeDocRef(None))
            )
        else:  # users
            user_ref = MagicMock()

            def _subcollection(sub_name: str):
                sub = MagicMock()
                if sub_name == pairing.PAIRING_STATE_SUBCOLLECTION:
                    sub.document.return_value = self.state_ref
                else:  # linked_devices; document() with no args -> auto-id ref
                    sub.document.return_value = self.linked_ref
                return sub

            user_ref.collection.side_effect = _subcollection
            col.document.return_value = user_ref
        return col

    def transaction(self):
        return self.txn


@pytest.fixture(autouse=True)
def _passthrough_transactional(monkeypatch):
    """gcloud's @transactional needs a live server; run the body inline instead."""
    monkeypatch.setattr("google.cloud.firestore.transactional", lambda fn: fn)


@pytest.fixture(autouse=True)
def _reset_velocity_tracker():
    pairing._recent_claim_failure_seconds.clear()
    yield
    pairing._recent_claim_failure_seconds.clear()


def _body(resp) -> dict:
    return json.loads(resp.body)


def _live_code_doc(uid: str = "u1", **overrides) -> dict:
    now = datetime.now(UTC)
    doc = {
        pairing.FIELD_UID: uid,
        pairing.FIELD_CREATED_AT: now,
        pairing.FIELD_EXPIRES_AT: now + NOW_WINDOW,
        pairing.FIELD_USED: False,
        pairing.FIELD_ATTEMPTS: 0,
    }
    doc.update(overrides)
    return doc


# ── 1. pair/start: auth ──────────────────────────────────────────────────────
async def test_pair_start_unauthenticated_returns_401(monkeypatch):
    monkeypatch.setattr(pairing, "resolve_user_id_from_request", lambda r: None)
    resp = await pairing.handle_pair_start(_Req())
    assert resp.status_code == 401


# ── 2. pair/start: happy path ────────────────────────────────────────────────
async def test_pair_start_writes_code_doc_and_returns_shape(monkeypatch):
    db = _FakeDb(state_data=None)  # no pairing_state doc yet (first ever pair)
    monkeypatch.setattr(pairing, "resolve_user_id_from_request", lambda r: "u1")
    monkeypatch.setattr(pairing, "admin_firestore", lambda: db)

    resp = await pairing.handle_pair_start(_Req())

    assert resp.status_code == 200
    body = _body(resp)
    assert body["expires_in_seconds"] == 300
    code = body["code"]
    assert len(code) == 8
    assert all(ch in pairing.PAIRING_CODE_ALPHABET for ch in code)

    # The code doc was CREATEd (atomic; _FakeTxn.create asserts it did not exist).
    code_doc = db.code_refs[code].data
    assert code_doc is not None
    assert code_doc[pairing.FIELD_UID] == "u1"
    assert code_doc[pairing.FIELD_USED] is False
    assert code_doc[pairing.FIELD_ATTEMPTS] == 0
    expiry_delta = code_doc[pairing.FIELD_EXPIRES_AT] - code_doc[pairing.FIELD_CREATED_AT]
    assert expiry_delta == timedelta(seconds=300)

    # The per-uid state doc now tracks the live code (cap accounting, no index).
    state_payload, _merge = db.state_ref.set_calls[-1]
    assert code in state_payload[pairing.FIELD_ACTIVE_CODES]


async def test_pair_start_prunes_expired_codes_and_their_docs(monkeypatch):
    now = datetime.now(UTC)
    stale_code = "AAAABBBB"
    db = _FakeDb(
        code_docs={stale_code: _live_code_doc(expires_at=now - timedelta(seconds=10))},
        state_data={pairing.FIELD_ACTIVE_CODES: {stale_code: now - timedelta(seconds=10)}},
    )
    monkeypatch.setattr(pairing, "resolve_user_id_from_request", lambda r: "u1")
    monkeypatch.setattr(pairing, "admin_firestore", lambda: db)

    resp = await pairing.handle_pair_start(_Req())

    assert resp.status_code == 200
    assert db.code_refs[stale_code].data is None  # dead code doc garbage-collected
    state_payload, _merge = db.state_ref.set_calls[-1]
    assert stale_code not in state_payload[pairing.FIELD_ACTIVE_CODES]


# ── 3. pair/start: per-uid cap ───────────────────────────────────────────────
async def test_pair_start_over_cap_returns_429(monkeypatch):
    live_until = datetime.now(UTC) + timedelta(seconds=200)
    db = _FakeDb(state_data={
        pairing.FIELD_ACTIVE_CODES: {
            "AAAA2222": live_until, "BBBB3333": live_until, "CCCC4444": live_until,
        }
    })
    monkeypatch.setattr(pairing, "resolve_user_id_from_request", lambda r: "u1")
    monkeypatch.setattr(pairing, "admin_firestore", lambda: db)

    resp = await pairing.handle_pair_start(_Req())

    assert resp.status_code == 429
    assert _body(resp)["error"] == "too_many_active_codes"
    # No fourth code was created.
    created = [ref for code, ref in db.code_refs.items() if ref.data is not None]
    assert created == []


# ── 4. pair/claim: happy path ────────────────────────────────────────────────
async def test_pair_claim_happy_path(monkeypatch):
    db = _FakeDb(code_docs={"ABCD2345": _live_code_doc(uid="u1")})
    auth = MagicMock()
    auth.create_custom_token.return_value = b"tok123"  # bytes: the decode path
    submit = AsyncMock()
    monkeypatch.setattr(pairing, "admin_firestore", lambda: db)
    monkeypatch.setattr(pairing, "admin_auth", lambda: auth)
    monkeypatch.setattr(device_link_push.orchestrator, "submit", submit)

    resp = await pairing.handle_pair_claim(
        _Req({"code": "abcd-2345", "device_name": "Varun's PC"})
    )

    assert resp.status_code == 200
    assert _body(resp)["custom_token"] == "tok123"
    auth.create_custom_token.assert_called_once_with("u1")

    # Code doc marked used, stamped with claim time + device name.
    code_doc = db.code_refs["ABCD2345"].data
    assert code_doc[pairing.FIELD_USED] is True
    assert code_doc[pairing.FIELD_CLAIMED_AT] is not None
    assert code_doc[pairing.FIELD_DEVICE_NAME] == "Varun's PC"

    # Linked device recorded.
    linked_payload = db.linked_ref.set.call_args.args[0]
    assert linked_payload[pairing.FIELD_DEVICE_NAME] == "Varun's PC"
    assert linked_payload[pairing.FIELD_PLATFORM] == "windows"
    assert linked_payload[pairing.FIELD_LINKED_AT] is not None

    # The cap slot was freed in the same transaction (merge-set on pairing_state).
    state_payload, merge = db.state_ref.set_calls[-1]
    assert merge is True
    assert "ABCD2345" in state_payload[pairing.FIELD_ACTIVE_CODES]

    # Confirmation push went through the funnel: COMMITTED device_link proposal.
    sent = submit.call_args.args[0]
    assert sent.source == proposal_mod.SOURCE_DEVICE_LINK
    assert sent.kind == proposal_mod.ProposalKind.COMMITTED
    assert sent.dedup_key == "device_link:dev123"
    assert sent.title == "Desktop connected"
    assert "Varun's PC" in sent.body


async def test_pair_claim_push_failure_does_not_fail_claim(monkeypatch):
    db = _FakeDb(code_docs={"ABCD2345": _live_code_doc(uid="u1")})
    auth = MagicMock()
    auth.create_custom_token.return_value = "tok123"
    monkeypatch.setattr(pairing, "admin_firestore", lambda: db)
    monkeypatch.setattr(pairing, "admin_auth", lambda: auth)
    monkeypatch.setattr(
        device_link_push.orchestrator, "submit", AsyncMock(side_effect=RuntimeError("fcm down"))
    )

    resp = await pairing.handle_pair_claim(_Req({"code": "ABCD2345"}))

    assert resp.status_code == 200
    assert _body(resp)["custom_token"] == "tok123"


# ── 5. pair/claim: every failure mode answers identically ────────────────────
async def test_pair_claim_wrong_code_returns_invalid_or_expired(monkeypatch):
    db = _FakeDb()  # no code docs at all
    monkeypatch.setattr(pairing, "admin_firestore", lambda: db)

    resp = await pairing.handle_pair_claim(_Req({"code": "ZZZZ9999"}))

    assert resp.status_code == 400
    assert _body(resp) == {"error": "invalid_or_expired"}
    # A missing doc has nothing to increment on.
    assert db.code_refs["ZZZZ9999"].data is None


async def test_pair_claim_expired_code_returns_invalid_and_counts_attempt(monkeypatch):
    expired = _live_code_doc(
        uid="u1", expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    db = _FakeDb(code_docs={"ABCD2345": expired})
    monkeypatch.setattr(pairing, "admin_firestore", lambda: db)

    resp = await pairing.handle_pair_claim(_Req({"code": "ABCD2345"}))

    assert resp.status_code == 400
    assert _body(resp) == {"error": "invalid_or_expired"}
    assert db.code_refs["ABCD2345"].data[pairing.FIELD_ATTEMPTS] == 1


async def test_pair_claim_already_used_code_returns_invalid(monkeypatch):
    used = _live_code_doc(uid="u1", used=True)
    db = _FakeDb(code_docs={"ABCD2345": used})
    monkeypatch.setattr(pairing, "admin_firestore", lambda: db)

    resp = await pairing.handle_pair_claim(_Req({"code": "ABCD2345"}))

    assert resp.status_code == 400
    assert _body(resp) == {"error": "invalid_or_expired"}
    assert db.code_refs["ABCD2345"].data[pairing.FIELD_ATTEMPTS] == 1


async def test_pair_claim_malformed_code_returns_same_body(monkeypatch):
    # Wrong alphabet / length never even reaches Firestore, same anonymous answer.
    monkeypatch.setattr(pairing, "admin_firestore", MagicMock(side_effect=AssertionError))
    resp = await pairing.handle_pair_claim(_Req({"code": "0000-0000"}))
    assert resp.status_code == 400
    assert _body(resp) == {"error": "invalid_or_expired"}


# ── 6. pair/claim: attempt-cap lockout ───────────────────────────────────────
async def test_pair_claim_locked_out_even_with_correct_code(monkeypatch):
    # 10 failures already recorded on the doc: the CORRECT, unexpired, unused code
    # is now refused too (and answers exactly like every other failure).
    locked = _live_code_doc(uid="u1", attempts=pairing.MAX_CLAIM_ATTEMPTS_PER_CODE)
    db = _FakeDb(code_docs={"ABCD2345": locked})
    auth = MagicMock()
    monkeypatch.setattr(pairing, "admin_firestore", lambda: db)
    monkeypatch.setattr(pairing, "admin_auth", lambda: auth)

    resp = await pairing.handle_pair_claim(_Req({"code": "ABCD2345"}))

    assert resp.status_code == 400
    assert _body(resp) == {"error": "invalid_or_expired"}
    auth.create_custom_token.assert_not_called()
    assert db.code_refs["ABCD2345"].data[pairing.FIELD_USED] is False


# ── 7. pair/claim: concurrent double-claim, exactly one winner ───────────────
async def test_pair_claim_double_claim_exactly_one_succeeds(monkeypatch):
    # Firestore serializes the two transactions; the loser's (re)run reads the
    # winner's used=true. The stateful _FakeTxn reproduces that: the first claim
    # mutates the shared doc, the second transactional callable sees it.
    db = _FakeDb(code_docs={"ABCD2345": _live_code_doc(uid="u1")})
    auth = MagicMock()
    auth.create_custom_token.return_value = "tok123"
    monkeypatch.setattr(pairing, "admin_firestore", lambda: db)
    monkeypatch.setattr(pairing, "admin_auth", lambda: auth)
    monkeypatch.setattr(device_link_push.orchestrator, "submit", AsyncMock())

    first = await pairing.handle_pair_claim(_Req({"code": "ABCD2345"}))
    second = await pairing.handle_pair_claim(_Req({"code": "ABCD2345"}))

    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [200, 400]
    auth.create_custom_token.assert_called_once()  # exactly one token ever minted


# ── 8. pair/claim: mint failure -> 500 + the exact tripwire log line ─────────
async def test_pair_claim_mint_failure_returns_500_with_exact_log(monkeypatch):
    db = _FakeDb(code_docs={"ABCD2345": _live_code_doc(uid="u1")})
    auth = MagicMock()
    auth.create_custom_token.side_effect = RuntimeError("missing iam.signBlob")
    spy_logger = MagicMock()
    monkeypatch.setattr(pairing, "admin_firestore", lambda: db)
    monkeypatch.setattr(pairing, "admin_auth", lambda: auth)
    monkeypatch.setattr(pairing, "logger", spy_logger)

    resp = await pairing.handle_pair_claim(_Req({"code": "ABCD2345"}))

    assert resp.status_code == 500
    logged_messages = [call.args[0] for call in spy_logger.error.call_args_list]
    assert "Pairing: custom token mint failed" in logged_messages


# ── Velocity alarm ───────────────────────────────────────────────────────────
def test_claim_failure_velocity_alarm_fires(monkeypatch):
    spy_logger = MagicMock()
    monkeypatch.setattr(pairing, "logger", spy_logger)

    for _ in range(pairing.CLAIM_FAILURE_VELOCITY_THRESHOLD):
        pairing._record_claim_failure()
    spy_logger.error.assert_not_called()  # at the threshold: still quiet

    pairing._record_claim_failure()  # one past it
    assert spy_logger.error.call_args.args[0] == "Pairing: high claim failure velocity"


# ── 9. unlink ────────────────────────────────────────────────────────────────
async def test_unlink_deletes_device_inline_and_schedules_revoke_in_background(monkeypatch):
    db = MagicMock()
    device_ref = (
        db.collection.return_value.document.return_value
        .collection.return_value.document.return_value
    )
    device_ref.get.return_value = MagicMock(exists=True)
    auth = MagicMock()
    monkeypatch.setattr(pairing, "resolve_user_id_from_request", lambda r: "u1")
    monkeypatch.setattr(pairing, "admin_firestore", lambda: db)
    monkeypatch.setattr(pairing, "admin_auth", lambda: auth)
    background_tasks = BackgroundTasks()

    resp = await pairing.handle_unlink_device(_Req({"device_id": "dev123"}), background_tasks)

    # The response reflects the deletion immediately...
    assert resp.status_code == 200
    assert _body(resp) == {"ok": True}
    device_ref.delete.assert_called_once()
    # ...and revocation hasn't run yet: it's scheduled, not inline.
    auth.revoke_refresh_tokens.assert_not_called()

    # Only once Starlette actually runs the background tasks (post-response) does
    # the revoke happen — this is what would hold up the response if it were awaited inline.
    await background_tasks()
    auth.revoke_refresh_tokens.assert_called_once_with("u1")


async def test_unlink_background_revoke_failure_is_logged_not_raised(monkeypatch):
    db = MagicMock()
    device_ref = (
        db.collection.return_value.document.return_value
        .collection.return_value.document.return_value
    )
    device_ref.get.return_value = MagicMock(exists=True)
    auth = MagicMock()
    auth.revoke_refresh_tokens.side_effect = RuntimeError("identity toolkit down")
    spy_logger = MagicMock()
    monkeypatch.setattr(pairing, "resolve_user_id_from_request", lambda r: "u1")
    monkeypatch.setattr(pairing, "admin_firestore", lambda: db)
    monkeypatch.setattr(pairing, "admin_auth", lambda: auth)
    monkeypatch.setattr(pairing, "logger", spy_logger)
    background_tasks = BackgroundTasks()

    resp = await pairing.handle_unlink_device(_Req({"device_id": "dev123"}), background_tasks)
    assert resp.status_code == 200  # the client-visible outcome is unaffected

    await background_tasks()  # must not raise past the caller
    logged_messages = [call.args[0] for call in spy_logger.exception.call_args_list]
    assert "Pairing: background refresh-token revoke failed" in logged_messages


async def test_unlink_missing_device_returns_404_without_revoking(monkeypatch):
    db = MagicMock()
    device_ref = (
        db.collection.return_value.document.return_value
        .collection.return_value.document.return_value
    )
    device_ref.get.return_value = MagicMock(exists=False)
    auth = MagicMock()
    monkeypatch.setattr(pairing, "resolve_user_id_from_request", lambda r: "u1")
    monkeypatch.setattr(pairing, "admin_firestore", lambda: db)
    monkeypatch.setattr(pairing, "admin_auth", lambda: auth)
    background_tasks = BackgroundTasks()

    resp = await pairing.handle_unlink_device(_Req({"device_id": "nope"}), background_tasks)

    assert resp.status_code == 404
    device_ref.delete.assert_not_called()
    assert background_tasks.tasks == []  # nothing scheduled when there was nothing to unlink
    auth.revoke_refresh_tokens.assert_not_called()


async def test_unlink_unauthenticated_returns_401(monkeypatch):
    monkeypatch.setattr(pairing, "resolve_user_id_from_request", lambda r: None)
    resp = await pairing.handle_unlink_device(_Req({"device_id": "dev123"}), BackgroundTasks())
    assert resp.status_code == 401


# ── Proposal contract ────────────────────────────────────────────────────────
def test_device_link_source_is_registered_everywhere():
    assert proposal_mod.SOURCE_DEVICE_LINK == "device_link"
    assert proposal_mod.SOURCE_DEVICE_LINK in proposal_mod.ALL_SOURCES
    assert proposal_mod.PRIORITY[proposal_mod.SOURCE_DEVICE_LINK] == 95
    # A security alert never goes stale.
    assert proposal_mod.FRESHNESS_MAX_AGE[proposal_mod.SOURCE_DEVICE_LINK] is None


# ── 10. voice surface: desktop no longer collapses to "app" ──────────────────
def test_desktop_is_a_known_voice_surface():
    from src import main as main_module

    assert "desktop" in main_module._VOICE_SURFACES
    assert "app" in main_module._VOICE_SURFACES  # the neutral default stays
    assert "smartfridge" not in main_module._VOICE_SURFACES  # unknowns still collapse
