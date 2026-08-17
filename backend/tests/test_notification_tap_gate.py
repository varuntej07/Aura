"""Focused coverage for the proactive notification tap-worthiness gate."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.notifications import tap_gate
from src.services.notifications.proposal import NotificationProposal, ProposalKind


def _proposal() -> NotificationProposal:
    return NotificationProposal(
        user_id="user-1",
        source="thread",
        kind=ProposalKind.PROACTIVE,
        dedup_key="thread-1",
        title="A thought about your project",
        body="Want to pick this back up?",
        data={
            "opening_chat_message": (
                "I pulled together the unresolved decision and three concrete options."
            ),
        },
    )


@pytest.mark.asyncio
async def test_passes_uses_background_gate_timeout(monkeypatch):
    provider = MagicMock()
    provider.cheap = AsyncMock(return_value='{"worthy": true, "reason": "specific"}')
    observed: dict[str, float] = {}

    async def _wait_for(awaitable, *, timeout):
        observed["timeout"] = timeout
        return await awaitable

    monkeypatch.setattr(tap_gate, "get_model_provider", lambda: provider)
    monkeypatch.setattr(tap_gate.asyncio, "wait_for", _wait_for)

    worthy, reason = await tap_gate.passes(_proposal())

    assert (worthy, reason) == (True, "specific")
    assert observed["timeout"] == 15.0


@pytest.mark.asyncio
async def test_passes_fails_closed_when_background_gate_times_out(monkeypatch):
    provider = MagicMock()
    provider.cheap = AsyncMock(return_value='{"worthy": false, "reason": "generic"}')

    async def _wait_for(awaitable, *, timeout):
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(tap_gate, "get_model_provider", lambda: provider)
    monkeypatch.setattr(tap_gate.asyncio, "wait_for", _wait_for)

    assert await tap_gate.passes(_proposal()) == (False, "gate_unavailable")
