"""
Coverage for the dark-test audience gate on proactive notification fan-out.

``feature_store.list_active_user_ids`` is the single seam every proactive
decider (signal engine, threads, daily-plan) resolves its audience through. The
``PROACTIVE_NOTIFICATION_UID_ALLOWLIST`` flag must:
  1. be a no-op when unset — the live/production default returns every active
     user, so a normal deploy reaches everybody;
  2. intersect the active audience with the allowlist when set — so a dark
     candidate revision can send only to the tester's phone.

If (1) ever regressed, a live deploy with the var accidentally set (or a stale
default) would silently page nobody — exactly the "zero rows looks healthy" trap
the project guards against, which is why the gate also logs a loud WARNING.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.config.settings import settings
from src.services.signal_engine import feature_store


@pytest.mark.asyncio
async def test_empty_allowlist_returns_all_active_users(monkeypatch):
    monkeypatch.setattr(settings, "PROACTIVE_NOTIFICATION_UID_ALLOWLIST", "")
    with patch(
        "src.services.fcm_token_registry.list_active_user_ids",
        return_value=["uid-a", "uid-b", "uid-c"],
    ):
        assert await feature_store.list_active_user_ids() == ["uid-a", "uid-b", "uid-c"]


@pytest.mark.asyncio
async def test_set_allowlist_restricts_to_intersection(monkeypatch):
    # Whitespace/comma mix is intentional — proves the parser handles both.
    monkeypatch.setattr(settings, "PROACTIVE_NOTIFICATION_UID_ALLOWLIST", "uid-b, uid-missing")
    with patch(
        "src.services.fcm_token_registry.list_active_user_ids",
        return_value=["uid-a", "uid-b", "uid-c"],
    ):
        # Only the active uid that is also allowlisted survives; an allowlisted
        # uid with no active token is not conjured into the audience.
        assert await feature_store.list_active_user_ids() == ["uid-b"]


@pytest.mark.asyncio
async def test_allowlist_property_parses_whitespace_and_commas(monkeypatch):
    monkeypatch.setattr(
        settings, "PROACTIVE_NOTIFICATION_UID_ALLOWLIST", " uid-1,uid-2   uid-3 "
    )
    assert settings.proactive_notification_uid_allowlist == ["uid-1", "uid-2", "uid-3"]
