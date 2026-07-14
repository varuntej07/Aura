"""GET /entitlement: server-stamped trial, usage summary, steering, fail-closed reads.

Covers the contract every client depends on from Phase 2 onward: the first-ever
call stamps a 45-day trial exactly once (race-safe via Firestore create()),
later calls never overwrite, legacy client-stamped docs (no status field) are
tolerated, and a Firestore outage answers 503 instead of silently granting pro.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from google.api_core.exceptions import AlreadyExists

import src.handlers.entitlement as entitlement_handler
from src.config.settings import settings
from src.services.entitlement import (
    RENEWAL_GRACE,
    EntitlementUnavailableError,
    get_user_effective_tier,
    has_active_paid_subscription,
    normalize_status,
    resolve_effective_tier,
)


class _Req:
    def __init__(self, headers: dict | None = None) -> None:
        self.headers = headers or {}


class _FakeSnap:
    def __init__(self, data: dict | None) -> None:
        self._data = data

    def to_dict(self) -> dict | None:
        return self._data


class _FakeDocRef:
    def __init__(self, store: dict, path: str, raise_on_get: set[str]) -> None:
        self._store = store
        self._path = path
        self._raise_on_get = raise_on_get

    def collection(self, name: str) -> "_FakeCollection":
        return _FakeCollection(self._store, f"{self._path}/{name}", self._raise_on_get)

    def get(self) -> _FakeSnap:
        if self._path in self._raise_on_get:
            raise RuntimeError("firestore down")
        return _FakeSnap(self._store.get(self._path))

    def create(self, doc: dict) -> None:
        if self._path in self._store:
            raise AlreadyExists("already exists")
        self._store[self._path] = doc


class _FakeCollection:
    def __init__(self, store: dict, path: str, raise_on_get: set[str]) -> None:
        self._store = store
        self._path = path
        self._raise_on_get = raise_on_get

    def document(self, name: str) -> _FakeDocRef:
        return _FakeDocRef(self._store, f"{self._path}/{name}", self._raise_on_get)


class _FakeDb:
    def __init__(self, store: dict, raise_on_get: set[str] | None = None) -> None:
        self._store = store
        self._raise_on_get = raise_on_get or set()

    def collection(self, name: str) -> _FakeCollection:
        return _FakeCollection(self._store, name, self._raise_on_get)


_ENT_PATH = "users/u1/entitlement/current"
_TODAY = datetime.now(UTC).strftime("%Y-%m-%d")


@pytest.fixture
def store() -> dict:
    return {}


@pytest.fixture
def _auth(monkeypatch):
    monkeypatch.setattr(entitlement_handler, "resolve_user_id_from_request", lambda r: "u1")


def _call(store: dict, raise_on_get: set[str] | None = None,
          headers: dict | None = None) -> tuple[int, dict]:
    db = _FakeDb(store, raise_on_get)
    with patch("src.services.firebase.admin_firestore", return_value=db):
        resp = asyncio.run(entitlement_handler.handle_get_entitlement(_Req(headers)))
    return resp.status_code, json.loads(resp.body)


# --- trial stamping ----------------------------------------------------------------------------

def test_first_call_stamps_45_day_trial(_auth, store):
    status, body = _call(store)

    assert status == 200
    doc = store[_ENT_PATH]
    assert doc["tier"] == "free"
    assert doc["status"] == "trialing"
    assert doc["trial_notified_3d"] is False
    assert doc["trial_notified_expired"] is False
    span = doc["trial_end_date"] - doc["trial_start_date"]
    assert span == timedelta(days=45)
    assert abs((doc["trial_start_date"] - datetime.now(UTC)).total_seconds()) < 30

    assert body["tier"] == "free"
    assert body["status"] == "trialing"
    assert body["effective_tier"] == "pro"  # reverse trial: trial window is the pro experience
    assert body["trial_end_date"] is not None


def test_second_call_never_overwrites(_auth, store):
    original_end = datetime(2026, 1, 1, tzinfo=UTC)
    store[_ENT_PATH] = {
        "tier": "free", "status": "trialing",
        "trial_start_date": datetime(2025, 11, 17, tzinfo=UTC),
        "trial_end_date": original_end,
    }

    status, body = _call(store)

    assert status == 200
    assert store[_ENT_PATH]["trial_end_date"] == original_end
    assert body["trial_end_date"] == original_end.isoformat()


def test_concurrent_first_calls_produce_one_doc(_auth, store, monkeypatch):
    # Simulate losing the create race: the initial read sees no doc, but by the
    # time create() runs another writer (second device, legacy client) won.
    existing = {
        "tier": "free", "status": "trialing",
        "trial_end_date": datetime(2026, 12, 25, tzinfo=UTC),
    }

    async def _fetch_sees_nothing(_uid):
        store.setdefault(_ENT_PATH, existing)  # the racing writer lands now
        return {}

    monkeypatch.setattr("src.services.entitlement.fetch_entitlement_doc", _fetch_sees_nothing)
    status, body = _call(store)

    assert status == 200
    assert store[_ENT_PATH] is existing  # never overwritten
    assert body["trial_end_date"] == existing["trial_end_date"].isoformat()


def test_legacy_doc_without_status_derives_trialing(_auth, store):
    store[_ENT_PATH] = {
        "tier": "free",
        "trial_end_date": datetime.now(UTC) + timedelta(days=10),
    }
    status, body = _call(store)
    assert status == 200
    assert body["status"] == "trialing"
    assert "status" not in store[_ENT_PATH]  # derived for the response, never written back


def test_legacy_doc_past_trial_derives_expired(_auth, store):
    store[_ENT_PATH] = {
        "tier": "free",
        "trial_end_date": datetime.now(UTC) - timedelta(days=1),
    }
    status, body = _call(store)
    assert body["status"] == "expired"
    assert body["effective_tier"] == "free"


# --- usage summary + steering ------------------------------------------------------------------

def test_usage_summary_shape_and_rollover(_auth, store):
    store[_ENT_PATH] = {"tier": "pro", "status": "active"}
    store["users/u1/usage/daily_chat"] = {"date": _TODAY, "count": 3}
    store["users/u1/usage/daily_web_surf"] = {"date": "2000-01-01", "count": 9}  # stale -> 0
    store["users/u1/usage/daily_voice"] = {"date": _TODAY, "seconds": 120}
    # daily_outbound_draft missing entirely -> 0

    _status, body = _call(store)
    usage = body["usage"]
    assert usage["date"] == _TODAY
    assert usage["chat"] == {"used": 3, "limit": 25}
    assert usage["web_surf"] == {"used": 0, "limit": 10}
    assert usage["drafts"] == {"used": 0, "limit": 5}
    assert usage["voice_seconds"] == {"used": 120, "limit": 600}


def test_single_usage_read_failure_yields_null(_auth, store):
    store[_ENT_PATH] = {"tier": "pro", "status": "active"}
    store["users/u1/usage/daily_chat"] = {"date": _TODAY, "count": 1}

    status, body = _call(store, raise_on_get={"users/u1/usage/daily_voice"})

    assert status == 200  # a broken counter never blocks the response
    assert body["usage"]["chat"] == {"used": 1, "limit": 25}
    assert body["usage"]["voice_seconds"] is None


def test_steering_silent_until_dodo_is_fully_configured(_auth, store, monkeypatch):
    monkeypatch.setattr(settings, "DODO_API_KEY", "")
    monkeypatch.setattr(settings, "DODO_WEBHOOK_SECRET", "")
    store[_ENT_PATH] = {"tier": "free", "status": "trialing"}
    _status, body = _call(store)
    assert body["steering"] == {"android_us": "SILENT", "ios_us": "SILENT", "row": "SILENT"}


def test_steering_uses_storefront_config_after_dodo_is_ready(_auth, store, monkeypatch):
    monkeypatch.setattr(settings, "DODO_API_KEY", "test-key")
    monkeypatch.setattr(settings, "DODO_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setattr(settings, "DODO_PRODUCT_COMPANION_MONTHLY", "prod_cm")
    monkeypatch.setattr(settings, "DODO_PRODUCT_COMPANION_YEARLY", "prod_cy")
    monkeypatch.setattr(settings, "DODO_PRODUCT_PRO_MONTHLY", "prod_pm")
    monkeypatch.setattr(settings, "DODO_PRODUCT_PRO_YEARLY", "prod_py")
    store[_ENT_PATH] = {"tier": "free", "status": "expired"}
    _status, body = _call(store)
    assert body["steering"] == {"android_us": "LINK_OUT", "ios_us": "LINK_OUT", "row": "SILENT"}


# --- auth + outage -----------------------------------------------------------------------------

def test_unauthenticated_401(monkeypatch, store):
    monkeypatch.setattr(entitlement_handler, "resolve_user_id_from_request", lambda r: None)
    status, body = _call(store)
    assert status == 401


def test_firestore_outage_503_stale_signal(_auth, store, monkeypatch):
    async def _raise(_uid):
        raise EntitlementUnavailableError("down")

    monkeypatch.setattr(entitlement_handler, "ensure_entitlement_doc", _raise)
    status, body = _call(store)
    assert status == 503
    assert body["error"] == "entitlement_unavailable"


# --- entitlement service: fail-closed + pure tier resolution -----------------------------------

def test_effective_tier_raises_on_firestore_error():
    # The old behavior returned "pro" here; an outage must never grant the paid product.
    with patch("src.services.firebase.admin_firestore", side_effect=RuntimeError("boom")):
        with pytest.raises(EntitlementUnavailableError):
            asyncio.run(get_user_effective_tier("u1"))


def test_effective_tier_missing_doc_stamps_trial_and_resolves_pro():
    # A doc-less uid must not ride the permissive default: enforcement's first
    # contact stamps the 45-day trial, and "pro" then flows from the trial
    # window of a real, durable doc.
    store: dict = {}
    with patch("src.services.firebase.admin_firestore", return_value=_FakeDb(store)):
        assert asyncio.run(get_user_effective_tier("u1")) == "pro"
    doc = store[_ENT_PATH]
    assert doc["tier"] == "free"
    assert doc["status"] == "trialing"
    assert doc["trial_end_date"] - doc["trial_start_date"] == timedelta(days=45)


def test_resolve_effective_tier_trial_window_is_pro():
    data = {"tier": "free", "trial_end_date": datetime.now(UTC) + timedelta(days=1)}
    assert resolve_effective_tier(data) == "pro"


def test_resolve_effective_tier_after_trial_is_free():
    data = {"tier": "free", "trial_end_date": datetime.now(UTC) - timedelta(days=1)}
    assert resolve_effective_tier(data) == "free"


def test_resolve_effective_tier_expired_status_wins_over_tier():
    # Belt and suspenders: a missed tier write can never leave paid access dangling.
    assert resolve_effective_tier({"tier": "pro", "status": "expired"}) == "free"


def test_resolve_effective_tier_paid_passthrough():
    assert resolve_effective_tier({"tier": "companion", "status": "active"}) == "companion"


# --- normalize_status: authoritative dates over stored status -----------------------------------

def _now() -> datetime:
    return datetime.now(UTC)


def test_normalize_status_trialing_past_trial_end_expires():
    data = {"status": "trialing", "trial_end_date": _now() - timedelta(days=1)}
    assert normalize_status(data) == "expired"
    assert resolve_effective_tier({**data, "tier": "free"}) == "free"


def test_normalize_status_trialing_within_window_unchanged():
    data = {"status": "trialing", "trial_end_date": _now() + timedelta(days=1)}
    assert normalize_status(data) == "trialing"


def test_normalize_status_active_within_renewal_grace_stays_active():
    # A renewal webhook can be hours late; the sub must not flicker to free.
    data = {"status": "active", "cancel_at_period_end": False,
            "expires_at": _now() - timedelta(days=1)}
    assert normalize_status(data) == "active"


def test_normalize_status_active_past_renewal_grace_expires():
    data = {"status": "active", "cancel_at_period_end": False,
            "expires_at": _now() - RENEWAL_GRACE - timedelta(hours=1)}
    assert normalize_status(data) == "expired"
    assert resolve_effective_tier({**data, "tier": "pro"}) == "free"


def test_normalize_status_cancelled_expires_exactly_at_period_end():
    # No grace for a deliberate cancellation: access ends at expires_at.
    data = {"status": "active", "cancel_at_period_end": True,
            "expires_at": _now() - timedelta(minutes=5)}
    assert normalize_status(data) == "expired"


def test_normalize_status_cancelled_still_running_stays_active():
    data = {"status": "active", "cancel_at_period_end": True,
            "expires_at": _now() + timedelta(days=5)}
    assert normalize_status(data) == "active"


def test_normalize_status_naive_datetimes_coerced_to_utc():
    naive_past = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
    assert normalize_status({"status": "trialing", "trial_end_date": naive_past}) == "expired"


def test_has_active_paid_subscription_matrix():
    live = {"tier": "pro", "status": "active"}
    assert has_active_paid_subscription(live) is True
    assert has_active_paid_subscription({**live, "status": "gracePeriod"}) is True
    assert has_active_paid_subscription({"tier": "free", "status": "trialing"}) is False
    assert has_active_paid_subscription({}) is False
    lapsed = {"tier": "pro", "status": "active", "cancel_at_period_end": False,
              "expires_at": _now() - RENEWAL_GRACE - timedelta(days=1)}
    assert has_active_paid_subscription(lapsed) is False


def test_stale_trialing_status_served_as_expired(_auth, store):
    # Finding: clients displayed "trial" forever because the stored status was
    # echoed verbatim. The response must normalize from trial_end_date.
    store[_ENT_PATH] = {
        "tier": "free", "status": "trialing",
        "trial_end_date": datetime.now(UTC) - timedelta(days=2),
    }
    _status, body = _call(store)
    assert body["status"] == "expired"
    assert body["effective_tier"] == "free"
    assert store[_ENT_PATH]["status"] == "trialing"  # normalized, never written back


# --- steering country ----------------------------------------------------------------------------

def test_country_null_without_edge_header(_auth, store):
    store[_ENT_PATH] = {"tier": "free", "status": "trialing"}
    _status, body = _call(store)
    assert body["country"] is None


def test_country_resolved_from_edge_header(_auth, store):
    store[_ENT_PATH] = {"tier": "free", "status": "trialing"}
    _status, body = _call(store, headers={"x-client-geo-country": "us"})
    assert body["country"] == "US"


def test_country_ignores_unknown_placeholder(_auth, store):
    store[_ENT_PATH] = {"tier": "free", "status": "trialing"}
    _status, body = _call(store, headers={"x-client-geo-country": "ZZ"})
    assert body["country"] is None
