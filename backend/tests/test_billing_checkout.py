"""POST /billing/checkout + GET /billing/portal: the uid-in-metadata handshake.

The load-bearing assertion: every checkout session carries
metadata = {firebase_uid, tier, period}, because that is the only link between
a Dodo payment and the Firebase account every device unlocks against.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

import src.handlers.billing as billing_handler
import src.services.billing as billing_service
from src.config.settings import settings
from src.services.billing import DodoApiError, create_checkout_session
from src.services.entitlement import EntitlementUnavailableError


class _Req:
    def __init__(self, body: dict | None = None) -> None:
        self._body = body if body is not None else {}
        self.headers: dict = {}

    async def json(self):
        return self._body


class _Resp:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """httpx.AsyncClient stand-in capturing the outgoing POST."""

    def __init__(self, responder) -> None:
        self._responder = responder
        self.posts: list[tuple[str, dict | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def post(self, url, json=None, headers=None):
        self.posts.append((url, json))
        return self._responder(url, json)


@pytest.fixture
def _auth(monkeypatch):
    monkeypatch.setattr(billing_handler, "resolve_user_id_from_request", lambda r: "u1")


@pytest.fixture
def _dodo_configured(monkeypatch):
    monkeypatch.setattr(settings, "DODO_API_KEY", "test-key")
    monkeypatch.setattr(settings, "DODO_PRODUCT_COMPANION_MONTHLY", "prod_cm")
    monkeypatch.setattr(settings, "DODO_PRODUCT_COMPANION_YEARLY", "prod_cy")
    monkeypatch.setattr(settings, "DODO_PRODUCT_PRO_MONTHLY", "prod_pm")
    monkeypatch.setattr(settings, "DODO_PRODUCT_PRO_YEARLY", "prod_py")


async def _no_identity(_uid):
    return None


def _entitlement_doc(monkeypatch, doc: dict):
    async def _fetch(_uid):
        return doc

    monkeypatch.setattr(billing_handler, "fetch_entitlement_doc", _fetch)


@pytest.fixture
def _no_subscription(monkeypatch):
    _entitlement_doc(monkeypatch, {})


# --- checkout ----------------------------------------------------------------------------------

def test_checkout_posts_product_and_uid_handshake(_auth, _dodo_configured, _no_subscription,
                                                  monkeypatch):
    client = _FakeClient(lambda url, body: _Resp(200, {
        "session_id": "cks_1", "checkout_url": "https://checkout.dodo.test/cks_1",
    }))
    monkeypatch.setattr(billing_service.httpx, "AsyncClient", lambda *a, **k: client)
    monkeypatch.setattr(billing_service, "_fetch_customer_identity", _no_identity)

    resp = asyncio.run(billing_handler.handle_billing_checkout(
        _Req({"tier": "pro", "period": "yearly"})))

    assert resp.status_code == 200
    assert json.loads(resp.body) == {"checkout_url": "https://checkout.dodo.test/cks_1"}

    url, payload = client.posts[0]
    assert url == f"{settings.DODO_API_BASE}/checkouts"
    assert payload["product_cart"] == [{"product_id": "prod_py", "quantity": 1}]
    assert payload["metadata"] == {"firebase_uid": "u1", "tier": "pro", "period": "yearly"}
    assert payload["return_url"] == settings.DODO_CHECKOUT_RETURN_URL


@pytest.mark.parametrize("tier,period,product", [
    ("companion", "monthly", "prod_cm"),
    ("companion", "yearly", "prod_cy"),
    ("pro", "monthly", "prod_pm"),
    ("pro", "yearly", "prod_py"),
])
def test_checkout_maps_every_plan_to_its_product(_dodo_configured, monkeypatch,
                                                 tier, period, product):
    client = _FakeClient(lambda url, body: _Resp(200, {"checkout_url": "https://x"}))
    monkeypatch.setattr(billing_service.httpx, "AsyncClient", lambda *a, **k: client)
    monkeypatch.setattr(billing_service, "_fetch_customer_identity", _no_identity)

    asyncio.run(create_checkout_session("u1", tier, period))
    assert client.posts[0][1]["product_cart"][0]["product_id"] == product


@pytest.mark.parametrize("body", [
    {"tier": "max", "period": "monthly"},
    {"tier": "pro", "period": "weekly"},
    {"tier": "", "period": ""},
    {},
])
def test_checkout_rejects_invalid_plan_400(_auth, _dodo_configured, body):
    resp = asyncio.run(billing_handler.handle_billing_checkout(_Req(body)))
    assert resp.status_code == 400


def test_checkout_unauthenticated_401(monkeypatch):
    monkeypatch.setattr(billing_handler, "resolve_user_id_from_request", lambda r: None)
    resp = asyncio.run(billing_handler.handle_billing_checkout(
        _Req({"tier": "pro", "period": "monthly"})))
    assert resp.status_code == 401


def test_checkout_unconfigured_503(_auth, monkeypatch):
    monkeypatch.setattr(settings, "DODO_API_KEY", "")
    resp = asyncio.run(billing_handler.handle_billing_checkout(
        _Req({"tier": "pro", "period": "monthly"})))
    assert resp.status_code == 503
    assert json.loads(resp.body) == {"error": "billing_not_configured"}


def test_checkout_dodo_error_502(_auth, _dodo_configured, _no_subscription, monkeypatch):
    client = _FakeClient(lambda url, body: _Resp(500, {"error": "server error"}))
    monkeypatch.setattr(billing_service.httpx, "AsyncClient", lambda *a, **k: client)
    monkeypatch.setattr(billing_service, "_fetch_customer_identity", _no_identity)

    resp = asyncio.run(billing_handler.handle_billing_checkout(
        _Req({"tier": "companion", "period": "monthly"})))
    assert resp.status_code == 502
    assert json.loads(resp.body) == {"error": "checkout_failed"}


def test_checkout_missing_url_in_response_502(_auth, _dodo_configured, _no_subscription,
                                              monkeypatch):
    client = _FakeClient(lambda url, body: _Resp(200, {"session_id": "cks_2"}))
    monkeypatch.setattr(billing_service.httpx, "AsyncClient", lambda *a, **k: client)
    monkeypatch.setattr(billing_service, "_fetch_customer_identity", _no_identity)

    resp = asyncio.run(billing_handler.handle_billing_checkout(
        _Req({"tier": "companion", "period": "monthly"})))
    assert resp.status_code == 502


# --- checkout: duplicate-subscription guard ------------------------------------------------------

def test_checkout_blocked_409_during_free_trial(_auth, _dodo_configured, monkeypatch):
    _entitlement_doc(monkeypatch, {
        "tier": "free",
        "status": "trialing",
        "trial_end_date": datetime.now(UTC) + timedelta(days=45),
    })

    resp = asyncio.run(billing_handler.handle_billing_checkout(
        _Req({"tier": "companion", "period": "monthly"})))

    assert resp.status_code == 409
    assert json.loads(resp.body) == {"error": "trial_active"}


def test_checkout_blocked_409_when_subscription_live(_auth, _dodo_configured, monkeypatch):
    _entitlement_doc(monkeypatch, {
        "tier": "pro", "status": "active", "dodo_customer_id": "cus_9",
    })
    resp = asyncio.run(billing_handler.handle_billing_checkout(
        _Req({"tier": "companion", "period": "monthly"})))
    assert resp.status_code == 409
    body = json.loads(resp.body)
    assert body["error"] == "already_subscribed"
    assert body["tier"] == "pro"
    assert body["status"] == "active"
    assert body["cancel_at_period_end"] is False


def test_checkout_blocked_409_while_cancelling_but_not_expired(_auth, _dodo_configured,
                                                               monkeypatch):
    # A cancelled-but-still-running sub blocks a second purchase; the user
    # un-cancels through the portal instead of stacking subscriptions.
    _entitlement_doc(monkeypatch, {
        "tier": "companion", "status": "active", "cancel_at_period_end": True,
        "expires_at": datetime.now(UTC) + timedelta(days=10),
    })
    resp = asyncio.run(billing_handler.handle_billing_checkout(
        _Req({"tier": "companion", "period": "monthly"})))
    assert resp.status_code == 409
    assert json.loads(resp.body)["cancel_at_period_end"] is True


def test_checkout_allowed_when_stored_active_is_date_expired(_auth, _dodo_configured,
                                                             monkeypatch):
    # A doc stuck on status "active" whose period (plus renewal grace) has long
    # passed is a lapsed subscription, not a live one; re-purchase must work.
    _entitlement_doc(monkeypatch, {
        "tier": "companion", "status": "active", "cancel_at_period_end": False,
        "expires_at": datetime.now(UTC) - timedelta(days=10),
        "dodo_customer_id": "cus_9",
    })
    client = _FakeClient(lambda url, body: _Resp(200, {"checkout_url": "https://x"}))
    monkeypatch.setattr(billing_service.httpx, "AsyncClient", lambda *a, **k: client)

    resp = asyncio.run(billing_handler.handle_billing_checkout(
        _Req({"tier": "companion", "period": "monthly"})))
    assert resp.status_code == 200
    # The existing Dodo customer is reused, never re-minted.
    assert client.posts[0][1]["customer"] == {"customer_id": "cus_9"}


def test_checkout_entitlement_outage_503_never_blind(_auth, _dodo_configured, monkeypatch):
    async def _raise(_uid):
        raise EntitlementUnavailableError("down")

    monkeypatch.setattr(billing_handler, "fetch_entitlement_doc", _raise)
    resp = asyncio.run(billing_handler.handle_billing_checkout(
        _Req({"tier": "pro", "period": "monthly"})))
    assert resp.status_code == 503
    assert json.loads(resp.body) == {"error": "entitlement_unavailable"}


def test_create_checkout_session_pins_existing_customer(_dodo_configured, monkeypatch):
    client = _FakeClient(lambda url, body: _Resp(200, {"checkout_url": "https://x"}))
    monkeypatch.setattr(billing_service.httpx, "AsyncClient", lambda *a, **k: client)
    identity = AsyncMock()
    monkeypatch.setattr(billing_service, "_fetch_customer_identity", identity)

    asyncio.run(create_checkout_session("u1", "pro", "monthly", customer_id="cus_9"))
    assert client.posts[0][1]["customer"] == {"customer_id": "cus_9"}
    identity.assert_not_awaited()  # pinned customer: no email prefill lookup


# --- portal ------------------------------------------------------------------------------------

def _portal_entitlement(monkeypatch, doc: dict):
    async def _fetch(_uid):
        return doc

    monkeypatch.setattr(billing_handler, "fetch_entitlement_doc", _fetch)


def test_portal_returns_link(_auth, monkeypatch):
    monkeypatch.setattr(settings, "DODO_API_KEY", "test-key")
    _portal_entitlement(monkeypatch, {"dodo_customer_id": "cus_9"})
    create = AsyncMock(return_value="https://portal.dodo.test/cus_9")
    with patch.object(billing_handler, "create_portal_session", create):
        resp = asyncio.run(billing_handler.handle_billing_portal(_Req()))

    assert resp.status_code == 200
    assert json.loads(resp.body) == {"portal_url": "https://portal.dodo.test/cus_9"}
    create.assert_awaited_once_with("cus_9")


def test_portal_without_billing_account_404(_auth, monkeypatch):
    monkeypatch.setattr(settings, "DODO_API_KEY", "test-key")
    _portal_entitlement(monkeypatch, {"tier": "free", "status": "trialing"})
    resp = asyncio.run(billing_handler.handle_billing_portal(_Req()))
    assert resp.status_code == 404
    assert json.loads(resp.body) == {"error": "no_billing_account"}


def test_portal_unconfigured_503(_auth, monkeypatch):
    monkeypatch.setattr(settings, "DODO_API_KEY", "")
    resp = asyncio.run(billing_handler.handle_billing_portal(_Req()))
    assert resp.status_code == 503


def test_portal_entitlement_outage_503(_auth, monkeypatch):
    monkeypatch.setattr(settings, "DODO_API_KEY", "test-key")

    async def _raise(_uid):
        raise EntitlementUnavailableError("down")

    monkeypatch.setattr(billing_handler, "fetch_entitlement_doc", _raise)
    resp = asyncio.run(billing_handler.handle_billing_portal(_Req()))
    assert resp.status_code == 503


def test_portal_dodo_error_502(_auth, monkeypatch):
    monkeypatch.setattr(settings, "DODO_API_KEY", "test-key")
    _portal_entitlement(monkeypatch, {"dodo_customer_id": "cus_9"})
    create = AsyncMock(side_effect=DodoApiError("boom"))
    with patch.object(billing_handler, "create_portal_session", create):
        resp = asyncio.run(billing_handler.handle_billing_portal(_Req()))
    assert resp.status_code == 502
