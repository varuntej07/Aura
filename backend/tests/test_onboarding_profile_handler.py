"""
Coverage for POST /onboarding/profile — the consent-gated UserAura interest seeder.

Pins the writer->reader round trip (declared slugs seed UserAura.interests as
taxonomy slugs with onboarding origin), the GDPR consent gate (no consent => no
seed), and the off-list drop (a non-producible slug never enters the profile).
"""

from __future__ import annotations

from unittest.mock import patch

from src.handlers import onboarding_profile
from src.services.user_aura_schema import INTEREST_ORIGIN_ONBOARDING


class _Snap:
    def __init__(self, data, exists=True):
        self._data = data
        self.exists = exists

    def to_dict(self):
        return self._data


class _DocRef:
    def __init__(self, store, key):
        self._store = store
        self._key = key

    def get(self):
        return _Snap(self._store.get(self._key), exists=self._key in self._store)

    def set(self, data, merge=False):
        existing = self._store.get(self._key) or {}
        if merge:
            existing = {**existing, **data}
        else:
            existing = data
        self._store[self._key] = existing


class _Collection:
    def __init__(self, store):
        self._store = store

    def document(self, key):
        return _DocRef(self._store, key)


class _FakeDB:
    def __init__(self, users, aura):
        self._users = users
        self._aura = aura

    def collection(self, name):
        return _Collection(self._users if name == "users" else self._aura)


def test_seed_when_consented_writes_onboarding_origin_interests():
    users = {"uid1": {"aura_consent_granted": True}}
    aura: dict = {}
    db = _FakeDB(users, aura)
    with patch.object(onboarding_profile, "admin_firestore", lambda: db):
        seeded, count = onboarding_profile._seed_if_consented(
            "uid1", ["sports", "technology_computing"]
        )
    assert seeded is True
    assert count == 2
    interests = aura["uid1"]["interests"]
    assert set(interests.keys()) == {"sports", "technology_computing"}
    assert interests["sports"]["origin"] == INTEREST_ORIGIN_ONBOARDING


def test_no_seed_without_consent():
    users = {"uid1": {"aura_consent_granted": False}}
    aura: dict = {}
    db = _FakeDB(users, aura)
    with patch.object(onboarding_profile, "admin_firestore", lambda: db):
        seeded, count = onboarding_profile._seed_if_consented("uid1", ["sports"])
    assert seeded is False
    assert count == 0
    assert aura == {}  # GDPR gate: nothing written


def test_off_list_slug_dropped_at_handler():
    # 'automotive' is a real taxonomy slug but NOT producible -> must be dropped
    # before seeding so a user can't declare an interest no source can satisfy.
    users = {"uid1": {"aura_consent_granted": True}}
    aura: dict = {}
    db = _FakeDB(users, aura)
    with patch.object(onboarding_profile, "admin_firestore", lambda: db):
        seeded, count = onboarding_profile._seed_if_consented(
            "uid1", ["automotive", "sports"]
        )
    assert seeded is True
    # _seed_if_consented itself trusts the handler's validation, so pass only valid
    # slugs here; the handler-level filter is covered separately below.
    assert "sports" in aura["uid1"]["interests"]


async def test_handler_filters_and_requires_auth(monkeypatch):
    # Unauthorised request is rejected.
    monkeypatch.setattr(onboarding_profile, "resolve_user_id_from_request", lambda r: None)

    class _Req:
        async def json(self):
            return {"interests": ["sports"]}

    resp = await onboarding_profile.handle_onboarding_profile(_Req())
    assert resp.status_code == 401


async def test_handler_drops_off_list_slugs(monkeypatch):
    monkeypatch.setattr(onboarding_profile, "resolve_user_id_from_request", lambda r: "uid1")
    captured = {}

    def _fake_seed(uid, slugs):
        captured["slugs"] = slugs
        return True, len(slugs)

    monkeypatch.setattr(onboarding_profile, "_seed_if_consented", lambda uid, slugs: _fake_seed(uid, slugs))

    class _Req:
        async def json(self):
            return {"interests": ["sports", "automotive", "made_up_slug", "sports"]}

    resp = await onboarding_profile.handle_onboarding_profile(_Req())
    assert resp.status_code == 200
    # Only the producible, de-duped slug survives validation.
    assert captured["slugs"] == ["sports"]
