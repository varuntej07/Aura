"""Privacy-safe desktop notification producer for committed meeting states."""

from __future__ import annotations

from ..notifications import orchestrator
from ..notifications.proposal import (
    SOURCE_MEETING,
    DeliveryChannel,
    Disposition,
    NotificationProposal,
    ProposalKind,
)
from . import fields as F
from . import store


class MeetingNotificationDeliveryError(RuntimeError):
    """A committed meeting event could not reach its durable desktop outbox."""


async def notify_settled(uid: str, meeting_id: str) -> bool:
    """Submit the event implied by a committed meeting document.

    Ready always notifies. Failed only notifies when the persisted failure is
    user-actionable. Other terminal states remain visible through meeting
    activity polling without generating a toast.
    """
    meeting = await store.get_meeting(uid, meeting_id)
    if meeting is None:
        return False
    status = str(meeting.get(F.STATUS) or "")
    revision = int(meeting.get(F.STATUS_REVISION, 0))

    if status == F.STATUS_READY:
        notification_type = "meeting_ready"
        title = "Your meeting insights are ready"
        body = "Open Aura to view them."
        severity = "success"
        dedup_key = f"meeting:{meeting_id}:ready:{revision}"
    elif status == F.STATUS_FAILED and meeting.get(F.RETRYABLE) is True:
        notification_type = "meeting_needs_attention"
        title = "A meeting needs your attention"
        body = "Open Aura to review the next safe step."
        severity = "warning"
        dedup_key = f"meeting:{meeting_id}:needs_attention:{revision}"
    else:
        return False

    decision = await orchestrator.submit(NotificationProposal(
        user_id=uid,
        source=SOURCE_MEETING,
        kind=ProposalKind.COMMITTED,
        dedup_key=dedup_key,
        title=title,
        body=body,
        notification_type=notification_type,
        channels=frozenset({DeliveryChannel.DESKTOP}),
        data={
            "severity": severity,
            "toast_policy": "when_hidden",
            "action": "view_meeting",
            "resource_id": meeting_id,
            "sensitive": "true",
            "notification_origin": SOURCE_MEETING,
        },
    ))
    if decision.disposition == Disposition.DROP and decision.reason == "duplicate":
        return True
    if decision.disposition != Disposition.SEND or decision.delivered is not True:
        raise MeetingNotificationDeliveryError(
            f"Desktop notification was not durably queued for meeting {meeting_id}."
        )
    return True
