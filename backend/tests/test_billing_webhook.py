"""POST /billing/webhook: signature verification, idempotency, the state machine.

The signature tests exercise the REAL verification code with a known test
secret (never a mocked check): a Standard Webhooks HMAC-SHA256 over
"{id}.{timestamp}.{body}" with a whsec_-prefixed base64 secret. Idempotency
guards the ground rule that the same Dodo event delivered twice writes the
entitlement doc exactly once.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from google.api_core.exceptions import AlreadyExists

import src.handlers.billing as billing_handler
import src.services.billing as billing_service
from src.config.settings import settings
from src.services.billing import (
    WebhookPayloadError,
    entitlement_write_for_event,
    extract_id_mappings,
    parse_event_occurred_at,
    process_webhook_event,
    verify_webhook_signature,
)

_KEY_BYTES = b"test-webhook-key-32-bytes-long!!"
_SECRET = "whsec_" + base64.b64encode(_KEY_BYTES).decode()


def _sign(msg_id: str, timestamp: str, body: bytes) -> str:
    signed = f"{msg_id}.{timestamp}.".encode() + body
    return "v1," + base64.b64encode(hmac.new(_KEY_BYTES, signed, hashlib.sha256).digest()).decode()


# --- signature verification (pure) -------------------------------------------------------------

def test_valid_signature_accepted():
    body = b'{"type":"subscription.active"}'
    ts = str(int(time.time()))
    assert verify_webhook_signature(
        secret=_SECRET, msg_id="msg_1", timestamp=ts, body=body,
        signature_header=_sign("msg_1", ts, body),
    )


def test_wrong_signature_rejected():
    body = b"{}"
    ts = str(int(time.time()))
    assert not verify_webhook_signature(
        secret=_SECRET, msg_id="msg_1", timestamp=ts, body=body,
        signature_header="v1," + base64.b64encode(b"x" * 32).decode(),
    )


def test_tampered_body_rejected():
    ts = str(int(time.time()))
    sig = _sign("msg_1", ts, b'{"amount":1}')
    assert not verify_webhook_signature(
        secret=_SECRET, msg_id="msg_1", timestamp=ts, body=b'{"amount":9999}',
        signature_header=sig,
    )


def test_stale_timestamp_rejected():
    body = b"{}"
    ts = str(int(time.time()) - 3600)  # an hour old: outside the replay window
    assert not verify_webhook_signature(
        secret=_SECRET, msg_id="msg_1", timestamp=ts, body=body,
        signature_header=_sign("msg_1", ts, body),
    )


def test_multi_candidate_header_accepted():
    # Standard Webhooks allows several space-separated signatures (key rotation).
    body = b"{}"
    ts = str(int(time.time()))
    bad = "v1," + base64.b64encode(b"y" * 32).decode()
    good = _sign("msg_1", ts, body)
    assert verify_webhook_signature(
        secret=_SECRET, msg_id="msg_1", timestamp=ts, body=body,
        signature_header=f"{bad} {good}",
    )


def test_missing_pieces_rejected():
    assert not verify_webhook_signature(
        secret="", msg_id="m", timestamp="1", body=b"{}", signature_header="v1,abc",
    )
    assert not verify_webhook_signature(
        secret=_SECRET, msg_id="", timestamp="1", body=b"{}", signature_header="v1,abc",
    )
    assert not verify_webhook_signature(
        secret=_SECRET, msg_id="m", timestamp="not-a-number", body=b"{}", signature_header="v1,abc",
    )


# --- state machine (pure) ----------------------------------------------------------------------

_SUB_DATA = {
    "subscription_id": "sub_123",
    "product_id": "prod_companion_m",
    "next_billing_date": "2026-08-09T00:00:00Z",
    "customer": {"customer_id": "cus_456"},
    "metadata": {"firebase_uid": "u1", "tier": "companion", "period": "monthly"},
}

# Real Dodo dispute/refund payloads reference a payment_id and carry NO
# checkout metadata; resolution must go through the dodo_payments mapping.
_DISPUTE_DATA = {"payment_id": "pay_9", "amount": "1999", "currency": "USD"}
_REFUND_DATA = {"payment_id": "pay_9", "refund_id": "ref_1", "amount": "1999"}

_PAYMENT_DATA = {
    "payment_id": "pay_9",
    "subscription_id": "sub_123",
    "customer": {"customer_id": "cus_456"},
    "metadata": {"firebase_uid": "u1", "tier": "companion", "period": "monthly"},
}


@pytest.mark.parametrize("event_type", [
    "subscription.active", "subscription.renewed", "subscription.plan_changed",
])
def test_activating_events_write_full_paid_state(event_type):
    write = entitlement_write_for_event(event_type, _SUB_DATA)
    assert write is not None
    assert write["tier"] == "companion"
    assert write["status"] == "active"
    assert write["source"] == "web"
    assert write["cancel_at_period_end"] is False
    assert write["dodo_subscription_id"] == "sub_123"
    assert write["dodo_customer_id"] == "cus_456"
    assert write["expires_at"] == datetime(2026, 8, 9, tzinfo=UTC)


def test_activation_tier_falls_back_to_product_id(monkeypatch):
    monkeypatch.setattr(settings, "DODO_PRODUCT_PRO_YEARLY", "prod_pro_y")
    data = dict(_SUB_DATA, metadata={"firebase_uid": "u1"}, product_id="prod_pro_y")
    write = entitlement_write_for_event("subscription.active", data)
    assert write is not None and write["tier"] == "pro"


def test_activation_with_unresolvable_tier_raises():
    # Never 200-ack a purchase that cannot be applied; a raise -> 500 -> Dodo retry.
    data = dict(_SUB_DATA, metadata={"firebase_uid": "u1"}, product_id="prod_unknown")
    with pytest.raises(WebhookPayloadError):
        entitlement_write_for_event("subscription.active", data)


@pytest.mark.parametrize("event_type", ["subscription.on_hold", "payment.failed"])
def test_dunning_events_grace_period_keeps_tier(event_type):
    write = entitlement_write_for_event(event_type, _SUB_DATA)
    assert write is not None
    assert write["status"] == "gracePeriod"
    assert "tier" not in write  # paid access is kept during dunning


def test_one_time_payment_failure_is_stateless():
    data = {"metadata": {"firebase_uid": "u1"}}  # no subscription_id
    assert entitlement_write_for_event("payment.failed", data) is None


def test_cancellation_flags_period_end_without_downgrade():
    write = entitlement_write_for_event("subscription.cancelled", _SUB_DATA)
    assert write is not None
    assert write["cancel_at_period_end"] is True
    assert "status" not in write and "tier" not in write  # active until expires_at
    assert write["expires_at"] == datetime(2026, 8, 9, tzinfo=UTC)


@pytest.mark.parametrize("event_type,data", [
    ("subscription.expired", _SUB_DATA),
    ("subscription.failed", _SUB_DATA),
    ("refund.succeeded", _REFUND_DATA),
    ("dispute.opened", _DISPUTE_DATA),
])
def test_terminal_events_expire_to_free(event_type, data):
    write = entitlement_write_for_event(event_type, data)
    assert write is not None
    assert write == {
        "tier": "free", "status": "expired", "cancel_at_period_end": False,
        "updated_at": write["updated_at"],
    }


def test_payment_succeeded_is_mapping_only():
    assert entitlement_write_for_event("payment.succeeded", _PAYMENT_DATA) is None


@pytest.mark.parametrize("payload_status,expected", [
    ("active", {"tier": "companion", "status": "active"}),
    ("on_hold", {"status": "gracePeriod"}),
    ("expired", {"tier": "free", "status": "expired"}),
    ("failed", {"tier": "free", "status": "expired"}),
])
def test_subscription_updated_follows_payload_status(payload_status, expected):
    # subscription.updated carries the authoritative object state; the write
    # must follow the payload's status field, not the event name.
    data = dict(_SUB_DATA, status=payload_status)
    write = entitlement_write_for_event("subscription.updated", data)
    assert write is not None
    for key, value in expected.items():
        assert write[key] == value


def test_subscription_updated_cancelled_flags_period_end():
    data = dict(_SUB_DATA, status="cancelled")
    write = entitlement_write_for_event("subscription.updated", data)
    assert write is not None
    assert write["cancel_at_period_end"] is True
    assert "status" not in write and "tier" not in write


def test_subscription_updated_unknown_status_is_stateless():
    data = dict(_SUB_DATA, status="paused")
    assert entitlement_write_for_event("subscription.updated", data) is None


def test_extract_id_mappings_links_every_payload_id():
    mappings = extract_id_mappings(_PAYMENT_DATA, "u1")
    by_collection = {collection: (doc_id, doc) for collection, doc_id, doc in mappings}
    assert by_collection["dodo_subscriptions"][0] == "sub_123"
    assert by_collection["dodo_customers"][0] == "cus_456"
    assert by_collection["dodo_payments"][0] == "pay_9"
    assert by_collection["dodo_payments"][1]["subscription_id"] == "sub_123"
    assert all(doc["uid"] == "u1" for _, (_, doc) in by_collection.items())


def test_extract_id_mappings_dispute_payload_maps_payment_only():
    mappings = extract_id_mappings(_DISPUTE_DATA, "u1")
    assert [(c, i) for c, i, _ in mappings] == [("dodo_payments", "pay_9")]


_T1 = datetime(2026, 7, 1, tzinfo=UTC)
_T2 = datetime(2026, 7, 2, tzinfo=UTC)


@pytest.mark.parametrize("ent,event_type,occurred_at,sub_id,stale", [
    # Timestamp guard: strictly older -> stale; equal or newer -> applies.
    ({"last_billing_event_at": _T2}, "subscription.active", _T1, "sub_123", True),
    ({"last_billing_event_at": _T1}, "subscription.active", _T1, "sub_123", False),
    ({"last_billing_event_at": _T1}, "subscription.active", _T2, "sub_123", False),
    # Missing timestamps never reject (fail open to pre-guard behavior).
    ({"last_billing_event_at": _T2}, "subscription.active", None, "sub_123", False),
    ({}, "subscription.active", _T1, "sub_123", False),
    # Superseded subscription: scoped state events for an id the doc no longer
    # tracks are stale; activating and refund/dispute events are exempt.
    ({"dodo_subscription_id": "sub_NEW"}, "subscription.on_hold", None, "sub_123", True),
    ({"dodo_subscription_id": "sub_NEW"}, "subscription.cancelled", None, "sub_123", True),
    ({"dodo_subscription_id": "sub_NEW"}, "subscription.active", None, "sub_123", False),
    ({"dodo_subscription_id": "sub_NEW"}, "refund.succeeded", None, "sub_123", False),
    ({"dodo_subscription_id": "sub_NEW"}, "dispute.opened", None, "", False),
    ({"dodo_subscription_id": "sub_123"}, "subscription.on_hold", None, "sub_123", False),
])
def test_is_stale_matrix(ent, event_type, occurred_at, sub_id, stale):
    assert billing_service._is_stale(ent, event_type, occurred_at, sub_id) is stale


def test_parse_event_occurred_at_prefers_envelope_iso():
    parsed = parse_event_occurred_at("2026-07-09T12:00:00Z", "1767960000")
    assert parsed == datetime(2026, 7, 9, 12, tzinfo=UTC)


def test_parse_event_occurred_at_falls_back_to_header_unix():
    parsed = parse_event_occurred_at(None, "1767960000")
    assert parsed == datetime.fromtimestamp(1767960000, UTC)
    assert parse_event_occurred_at(None, "not-a-number") is None


# --- processing: idempotency + doc writes + push ------------------------------------------------

class _FakeSnap:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return self._data


class _FakeDocRef:
    def __init__(self, store, path, fail_paths):
        self._store, self._path, self._fail = store, path, fail_paths

    def collection(self, name):
        return _FakeCollection(self._store, f"{self._path}/{name}", self._fail)

    def get(self, transaction=None):
        return _FakeSnap(self._store.get(self._path))

    def create(self, doc):
        if self._path in self._store:
            raise AlreadyExists("exists")
        self._store[self._path] = doc

    def set(self, doc, merge=False):
        if self._path in self._fail:
            raise RuntimeError("write failed")
        if merge and self._path in self._store:
            self._store[self._path] = {**self._store[self._path], **doc}
        else:
            self._store[self._path] = dict(doc)

    def delete(self):
        self._store.pop(self._path, None)


class _FakeCollection:
    def __init__(self, store, path, fail_paths):
        self._store, self._path, self._fail = store, path, fail_paths

    def document(self, name):
        return _FakeDocRef(self._store, f"{self._path}/{name}", self._fail)


class _FakeTxn:
    """Buffers writes like a real Firestore transaction: nothing lands in the
    store until the transaction body finishes, so a raise mid-body leaves the
    store untouched (the atomicity the production code relies on)."""

    def __init__(self, store, fail_paths):
        self._store, self._fail = store, fail_paths
        self._pending: list[tuple[str, dict, bool]] = []

    def set(self, ref, doc, merge=False):
        if ref._path in self._fail:
            raise RuntimeError("write failed")
        self._pending.append((ref._path, dict(doc), merge))

    def _flush(self):
        for path, doc, merge in self._pending:
            if merge and path in self._store:
                self._store[path] = {**self._store[path], **doc}
            else:
                self._store[path] = doc


class _FakeDb:
    def __init__(self, store, fail_paths=None):
        self._store = store
        self._fail = fail_paths or set()

    def collection(self, name):
        return _FakeCollection(self._store, name, self._fail)

    def transaction(self):
        return _FakeTxn(self._store, self._fail)


@pytest.fixture(autouse=True)
def _passthrough_transactional(monkeypatch):
    """gcloud's @transactional needs a live server; run the body inline and
    commit the fake transaction's buffered writes only on success."""
    def _fake_transactional(fn):
        def _wrapper(txn):
            result = fn(txn)
            txn._flush()
            return result
        return _wrapper

    monkeypatch.setattr("google.cloud.firestore.transactional", _fake_transactional)


_ENT_PATH = "users/u1/entitlement/current"


def _process(store, event_id, event_type, data, fail_paths=None, occurred_at=None):
    push = AsyncMock()
    db = _FakeDb(store, fail_paths)
    with patch("src.services.firebase.admin_firestore", return_value=db):
        with patch.object(billing_service, "_send_entitlement_updated", push):
            result = asyncio.run(
                process_webhook_event(event_id, event_type, data, occurred_at)
            )
    return result, push


def test_processing_writes_entitlement_and_claims_event():
    store = {}
    result, push = _process(store, "evt_1", "subscription.active", _SUB_DATA)

    assert result == {"status": "processed"}
    assert store[_ENT_PATH]["tier"] == "companion"
    assert store[_ENT_PATH]["status"] == "active"
    assert store["billing_events/evt_1"]["uid"] == "u1"
    assert store["billing_events/evt_1"]["event_type"] == "subscription.active"
    push.assert_awaited_once()


def test_duplicate_delivery_writes_exactly_once():
    store = {}
    _process(store, "evt_1", "subscription.active", _SUB_DATA)
    first_write = dict(store[_ENT_PATH])

    # Second delivery: mutate the payload to prove nothing is re-applied.
    tampered = dict(_SUB_DATA, metadata={**_SUB_DATA["metadata"], "tier": "pro"})
    result, push = _process(store, "evt_1", "subscription.active", tampered)

    assert result == {"status": "duplicate"}
    assert store[_ENT_PATH] == first_write
    push.assert_not_awaited()


def test_merge_preserves_trial_fields():
    trial_end = datetime(2026, 8, 1, tzinfo=UTC)
    store = {_ENT_PATH: {"trial_start_date": "x", "trial_end_date": trial_end,
                         "tier": "free", "status": "trialing"}}
    _process(store, "evt_1", "subscription.active", _SUB_DATA)
    assert store[_ENT_PATH]["trial_end_date"] == trial_end  # merge, not overwrite
    assert store[_ENT_PATH]["status"] == "active"


def test_unhandled_event_ignored_without_writes():
    store = {}
    result, push = _process(store, "evt_2", "payment.cancelled", _SUB_DATA)
    assert result == {"status": "ignored"}
    assert store == {}
    push.assert_not_awaited()


def test_missing_uid_ignored_without_writes():
    store = {}
    data = dict(_SUB_DATA, metadata={})
    result, push = _process(store, "evt_3", "subscription.active", data)
    assert result == {"status": "ignored"}
    assert store == {}
    push.assert_not_awaited()


def test_write_failure_commits_nothing_and_raises():
    # Atomicity: a failed entitlement write must leave no claim and no mapping
    # behind, so Dodo's retry genuinely reprocesses the whole event.
    store = {}
    with pytest.raises(RuntimeError):
        _process(store, "evt_4", "subscription.active", _SUB_DATA,
                 fail_paths={_ENT_PATH})
    assert store == {}


def test_activating_event_writes_reverse_mappings():
    store = {}
    _process(store, "evt_m1", "subscription.active", _SUB_DATA)
    assert store["dodo_subscriptions/sub_123"]["uid"] == "u1"
    assert store["dodo_customers/cus_456"]["uid"] == "u1"


def test_payment_succeeded_records_payment_mapping_only():
    store = {}
    result, push = _process(store, "evt_p1", "payment.succeeded", _PAYMENT_DATA)
    assert result == {"status": "processed"}
    assert store["dodo_payments/pay_9"]["uid"] == "u1"
    assert store["dodo_payments/pay_9"]["subscription_id"] == "sub_123"
    assert store["billing_events/evt_p1"]["event_type"] == "payment.succeeded"
    assert _ENT_PATH not in store  # mapping-only: no entitlement write
    push.assert_not_awaited()  # nothing changed, nothing to sync


def test_metadata_less_dispute_resolves_via_payment_mapping():
    # The original CRITICAL bug: a dispute carries payment_id but no metadata.
    store = {
        _ENT_PATH: {"tier": "companion", "status": "active"},
        "dodo_payments/pay_9": {"uid": "u1", "subscription_id": "sub_123"},
    }
    result, push = _process(store, "evt_d1", "dispute.opened", _DISPUTE_DATA)
    assert result == {"status": "processed"}
    assert store[_ENT_PATH]["tier"] == "free"
    assert store[_ENT_PATH]["status"] == "expired"
    push.assert_awaited_once()


def test_metadata_less_refund_resolves_via_payment_mapping():
    store = {
        _ENT_PATH: {"tier": "companion", "status": "active"},
        "dodo_payments/pay_9": {"uid": "u1"},
    }
    result, _push = _process(store, "evt_r1", "refund.succeeded", _REFUND_DATA)
    assert result == {"status": "processed"}
    assert store[_ENT_PATH]["status"] == "expired"


def test_unresolvable_terminal_event_raises_for_redelivery():
    # No metadata AND no mapping yet: acking would silently drop a revocation,
    # so the route must 500 and let Dodo redeliver (the mapping may land from
    # a payment.succeeded still in flight).
    store = {}
    with pytest.raises(WebhookPayloadError):
        _process(store, "evt_d2", "dispute.opened", _DISPUTE_DATA)
    assert store == {}


def test_out_of_order_replay_cannot_restore_revoked_access():
    # subscription.expired lands at T2, then a delayed/redelivered
    # subscription.active from T1 (< T2) arrives with a fresh event id: the
    # doc must stay expired.
    store = {}
    _process(store, "evt_o1", "subscription.expired", _SUB_DATA, occurred_at=_T2)
    assert store[_ENT_PATH]["status"] == "expired"

    result, push = _process(store, "evt_o2", "subscription.active", _SUB_DATA,
                            occurred_at=_T1)
    assert result == {"status": "stale"}
    assert store[_ENT_PATH]["status"] == "expired"
    assert store[_ENT_PATH]["tier"] == "free"
    # The stale event is still claimed: its verdict is final, a redelivery of
    # the same event id must short-circuit as a duplicate.
    assert store["billing_events/evt_o2"]["stale"] is True
    push.assert_not_awaited()


def test_superseded_subscription_event_is_stale():
    # After a plan change installed sub_NEW, a straggling on_hold for the old
    # subscription must not push the account into gracePeriod.
    store = {_ENT_PATH: {"tier": "pro", "status": "active",
                         "dodo_subscription_id": "sub_NEW"}}
    old_sub = dict(_SUB_DATA)  # subscription_id sub_123
    result, push = _process(store, "evt_s1", "subscription.on_hold", old_sub)
    assert result == {"status": "stale"}
    assert store[_ENT_PATH]["status"] == "active"
    push.assert_not_awaited()


def test_applied_event_stamps_last_billing_event_fields():
    store = {}
    _process(store, "evt_t1", "subscription.active", _SUB_DATA, occurred_at=_T1)
    assert store[_ENT_PATH]["last_billing_event_at"] == _T1
    assert store[_ENT_PATH]["last_billing_event_id"] == "evt_t1"


def test_push_carries_entitlement_updated_type():
    store = {}
    db = _FakeDb(store)
    submit = AsyncMock()
    with patch("src.services.firebase.admin_firestore", return_value=db):
        with patch("src.services.notifications.orchestrator.submit", submit):
            asyncio.run(process_webhook_event("evt_5", "subscription.active", _SUB_DATA))

    submit.assert_awaited_once()
    proposal = submit.call_args.args[0]
    assert proposal.user_id == "u1"
    assert proposal.source == "billing"
    assert proposal.data_only is True
    assert proposal.data["type"] == "entitlement-updated"
    assert proposal.data["tier"] == "companion"
    assert proposal.data["status"] == "active"


def test_push_failure_never_fails_the_webhook():
    store = {}
    db = _FakeDb(store)
    submit = AsyncMock(side_effect=RuntimeError("fcm down"))
    with patch("src.services.firebase.admin_firestore", return_value=db):
        with patch("src.services.notifications.orchestrator.submit", submit):
            result = asyncio.run(process_webhook_event("evt_6", "subscription.active", _SUB_DATA))
    assert result == {"status": "processed"}


# --- route handler: header + config gates -------------------------------------------------------

class _Req:
    def __init__(self, body: bytes, headers: dict | None = None) -> None:
        self._body = body
        self.headers = headers or {}

    async def body(self) -> bytes:
        return self._body


def _envelope(event_type: str, data: dict) -> bytes:
    return json.dumps({"business_id": "biz", "type": event_type,
                       "timestamp": "2026-07-09T00:00:00Z", "data": data}).encode()


def _signed_request(event_type: str, data: dict, msg_id: str = "evt_h1") -> _Req:
    body = _envelope(event_type, data)
    ts = str(int(time.time()))
    return _Req(body, {
        "webhook-id": msg_id,
        "webhook-timestamp": ts,
        "webhook-signature": _sign(msg_id, ts, body),
    })


def test_handler_unconfigured_secret_503(monkeypatch):
    monkeypatch.setattr(settings, "DODO_WEBHOOK_SECRET", "")
    resp = asyncio.run(billing_handler.handle_billing_webhook(_signed_request(
        "subscription.active", _SUB_DATA)))
    assert resp.status_code == 503  # Dodo retries; unsigned events are never acked away


def test_handler_bad_signature_401(monkeypatch):
    monkeypatch.setattr(settings, "DODO_WEBHOOK_SECRET", _SECRET)
    body = _envelope("subscription.active", _SUB_DATA)
    req = _Req(body, {
        "webhook-id": "evt_h2",
        "webhook-timestamp": str(int(time.time())),
        "webhook-signature": "v1," + base64.b64encode(b"z" * 32).decode(),
    })
    process = AsyncMock()
    with patch.object(billing_handler, "process_webhook_event", process):
        resp = asyncio.run(billing_handler.handle_billing_webhook(req))
    assert resp.status_code == 401
    process.assert_not_awaited()


def test_handler_valid_signature_processes(monkeypatch):
    monkeypatch.setattr(settings, "DODO_WEBHOOK_SECRET", _SECRET)
    process = AsyncMock(return_value={"status": "processed"})
    with patch.object(billing_handler, "process_webhook_event", process):
        resp = asyncio.run(billing_handler.handle_billing_webhook(
            _signed_request("subscription.active", _SUB_DATA)))
    assert resp.status_code == 200
    assert json.loads(resp.body) == {"status": "processed"}
    # occurred_at comes from the envelope's timestamp field (see _envelope).
    process.assert_awaited_once_with(
        "evt_h1", "subscription.active", _SUB_DATA,
        datetime(2026, 7, 9, tzinfo=UTC),
    )


def test_handler_processing_error_500_for_redelivery(monkeypatch):
    monkeypatch.setattr(settings, "DODO_WEBHOOK_SECRET", _SECRET)
    process = AsyncMock(side_effect=RuntimeError("firestore down"))
    with patch.object(billing_handler, "process_webhook_event", process):
        resp = asyncio.run(billing_handler.handle_billing_webhook(
            _signed_request("subscription.active", _SUB_DATA)))
    assert resp.status_code == 500
