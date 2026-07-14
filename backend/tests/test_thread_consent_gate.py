"""The thread follow-up is a behavioural touch, so it must honour the same Aura
consent gate as the briefing / icebreaker. This pins the GDPR guarantee: with
consent withheld, the curiosity agent stands down and never reaches thread
selection — closing the gap where a withheld-consent user (or a minor, who is
forced to consent=false) could still receive curiosity follow-ups.

The consent gate lives in ``CuriosityThreadFollowUpAgent.sense`` (the reactive
orchestration cut-over moved it here from the old ``thread_reflector`` fan-out).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.services.reactive.agent import UserContext
from src.services.reactive.agents import curiosity
from src.services.reactive.agents.curiosity import CuriosityThreadFollowUpAgent


@pytest.mark.asyncio
async def test_sense_skips_when_consent_not_granted(monkeypatch):
    async def _no_consent(_uid):
        return "UTC", False

    # If the gate fails, these would be reached — make them explode so a regression
    # that lets a no-consent user through is caught, not silently tolerated.
    async def _boom(*_a, **_k):
        raise AssertionError("no-consent user must never reach thread selection")

    monkeypatch.setattr(curiosity, "_load_consent_and_timezone", _no_consent)
    monkeypatch.setattr(curiosity.thread_store, "read_follow_ups_today", _boom)
    monkeypatch.setattr(curiosity.thread_store, "list_open_threads", _boom)

    inputs = await CuriosityThreadFollowUpAgent().sense(UserContext(user_id="u1"))

    assert inputs.eligible is False
    assert inputs.reason == "no_consent"


@pytest.mark.asyncio
async def test_sense_consent_granted_proceeds_to_active_hours(monkeypatch):
    # Consent granted but it's the dead of night -> it should pass the consent gate
    # and stand down at the quiet-hours gate (proving consent isn't what blocked it).
    async def _consent(_uid):
        return "UTC", True

    monkeypatch.setattr(curiosity, "_load_consent_and_timezone", _consent)
    # Force "middle of the night" regardless of wall clock.
    monkeypatch.setattr(
        curiosity, "_local_now",
        lambda _tz: datetime(2026, 6, 10, 3, 0, tzinfo=UTC),
    )
    monkeypatch.setattr(curiosity, "is_within_active_hours", lambda *_a, **_k: False)

    inputs = await CuriosityThreadFollowUpAgent().sense(UserContext(user_id="u1"))

    assert inputs.eligible is False
    assert inputs.reason == "quiet_hours"
