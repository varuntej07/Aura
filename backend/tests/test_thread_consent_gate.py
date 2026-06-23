"""The thread follow-up is a behavioural touch, so it must honour the same Aura
consent gate as the briefing / icebreaker. This pins the GDPR guarantee: with
consent withheld, the reflector sends nothing and records the skip — closing the
gap where a withheld-consent user (or a minor, who is forced to consent=false)
could still receive curiosity follow-ups.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.services.threads import thread_reflector as tr


@pytest.mark.asyncio
async def test_reflect_skips_when_consent_not_granted(monkeypatch):
    summary = tr.ReflectionSummary()

    async def _no_consent(_uid):
        return "UTC", False

    # If the gate fails, these would be reached — make them explode so a regression
    # that lets a no-consent user through is caught, not silently tolerated.
    async def _boom(*_a, **_k):
        raise AssertionError("no-consent user must never reach thread selection")

    monkeypatch.setattr(tr, "_load_tz_and_consent", _no_consent)
    monkeypatch.setattr(tr.thread_store, "read_follow_ups_today", _boom)
    monkeypatch.setattr(tr.thread_store, "list_open_threads", _boom)

    await tr._reflect_one_user("u1", MagicMock(), summary)

    assert summary.skipped_no_consent == 1
    assert summary.enqueued == 0


@pytest.mark.asyncio
async def test_reflect_consent_granted_proceeds_to_active_hours(monkeypatch):
    # Consent granted but it's the dead of night → it should pass the consent gate and
    # stand down at the quiet-hours gate (proving consent isn't the thing blocking it).
    summary = tr.ReflectionSummary()

    async def _consent_midnight(_uid):
        return "UTC", True

    monkeypatch.setattr(tr, "_load_tz_and_consent", _consent_midnight)
    # Force "middle of the night" regardless of wall clock.
    monkeypatch.setattr(tr, "_local_now", lambda _tz: __import__("datetime").datetime(
        2026, 6, 10, 3, 0, tzinfo=__import__("datetime").timezone.utc
    ))
    monkeypatch.setattr(tr, "is_within_active_hours", lambda *_a, **_k: False)

    await tr._reflect_one_user("u1", MagicMock(), summary)

    assert summary.skipped_no_consent == 0
    assert summary.skipped_quiet_hours == 1
