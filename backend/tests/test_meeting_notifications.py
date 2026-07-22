from __future__ import annotations

import pytest

from src.services.meetings import fields as F
from src.services.meetings import notifications
from src.services.notifications.proposal import (
    SOURCE_MEETING,
    Disposition,
    OrchestratorDecision,
)


async def test_ready_notification_is_generic_desktop_committed_and_revision_deduped(
    monkeypatch,
):
    async def get_meeting(uid, meeting_id):
        return {
            F.STATUS: F.STATUS_READY,
            F.STATUS_REVISION: 4,
            F.NOTE: {"summary": "Private content must not leave this field."},
        }

    proposals = []

    async def submit(proposal):
        proposals.append(proposal)
        return OrchestratorDecision(Disposition.SEND, "ok", delivered=True)

    monkeypatch.setattr(notifications.store, "get_meeting", get_meeting)
    monkeypatch.setattr(notifications.orchestrator, "submit", submit)

    assert await notifications.notify_settled("u1", "m1") is True
    proposal = proposals[0]
    assert proposal.source == SOURCE_MEETING
    assert proposal.dedup_key == "meeting:m1:ready:4"
    assert proposal.notification_type == "meeting_ready"
    assert "Private content" not in proposal.title + proposal.body
    assert {channel.value for channel in proposal.channels} == {"desktop"}


async def test_only_retryable_failure_emits_needs_attention(monkeypatch):
    meeting = {
        F.STATUS: F.STATUS_FAILED,
        F.STATUS_REVISION: 7,
        F.RETRYABLE: False,
    }

    async def get_meeting(uid, meeting_id):
        return dict(meeting)

    proposals = []

    async def submit(proposal):
        proposals.append(proposal)
        return OrchestratorDecision(Disposition.SEND, "ok", delivered=True)

    monkeypatch.setattr(notifications.store, "get_meeting", get_meeting)
    monkeypatch.setattr(notifications.orchestrator, "submit", submit)

    assert await notifications.notify_settled("u1", "m1") is False
    meeting[F.RETRYABLE] = True
    assert await notifications.notify_settled("u1", "m1") is True
    assert proposals[0].notification_type == "meeting_needs_attention"
    assert proposals[0].dedup_key == "meeting:m1:needs_attention:7"


async def test_failed_outbox_delivery_stays_retryable(monkeypatch):
    async def get_meeting(uid, meeting_id):
        return {F.STATUS: F.STATUS_READY, F.STATUS_REVISION: 1}

    async def submit(proposal):
        return OrchestratorDecision(Disposition.SEND, "ok", delivered=False)

    monkeypatch.setattr(notifications.store, "get_meeting", get_meeting)
    monkeypatch.setattr(notifications.orchestrator, "submit", submit)

    with pytest.raises(notifications.MeetingNotificationDeliveryError):
        await notifications.notify_settled("u1", "m1")


async def test_duplicate_revision_is_already_settled(monkeypatch):
    async def get_meeting(uid, meeting_id):
        return {F.STATUS: F.STATUS_READY, F.STATUS_REVISION: 1}

    async def submit(proposal):
        return OrchestratorDecision(Disposition.DROP, "duplicate")

    monkeypatch.setattr(notifications.store, "get_meeting", get_meeting)
    monkeypatch.setattr(notifications.orchestrator, "submit", submit)

    assert await notifications.notify_settled("u1", "m1") is True
