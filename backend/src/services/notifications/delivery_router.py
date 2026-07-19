"""Channel-aware delivery for one logical notification proposal."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from ...lib.logger import logger
from .. import notification_ledger
from ..notification_service import NotificationResult, send_notification
from . import desktop_outbox
from .proposal import DeliveryChannel, NotificationProposal


def notification_id_for(proposal: NotificationProposal) -> str:
    """Use stable ids for deduplicated events and random ids otherwise."""
    if proposal.dedup_key:
        identity = f"{proposal.user_id}:{proposal.dedup_key}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
    return str(uuid.uuid4())


async def deliver(proposal: NotificationProposal) -> NotificationResult:
    notification_id = notification_id_for(proposal)
    mobile_result: NotificationResult | None = None
    desktop_result: desktop_outbox.OutboxWriteResult | None = None
    channel_results: dict[str, dict[str, Any]] = {}

    async def _mobile() -> NotificationResult:
        data = dict(proposal.data)
        data["notification_id"] = notification_id
        return await send_notification(
            proposal.user_id,
            title=proposal.title,
            body=proposal.body,
            data=data,
            notification_type=proposal.notification_type,
            collapse_key=proposal.collapse_key,
            data_only=proposal.data_only,
            apns_category=proposal.apns_category,
            dedup_key=proposal.dedup_key,
            decision=proposal.decision,
            notification_id=notification_id,
            record_ledger=False,
        )

    async def _desktop() -> desktop_outbox.OutboxWriteResult:
        return await desktop_outbox.enqueue(proposal, notification_id)

    jobs: list[tuple[DeliveryChannel, asyncio.Task[Any]]] = []
    if DeliveryChannel.MOBILE in proposal.channels:
        jobs.append((DeliveryChannel.MOBILE, asyncio.create_task(_mobile())))
    if DeliveryChannel.DESKTOP in proposal.channels:
        jobs.append((DeliveryChannel.DESKTOP, asyncio.create_task(_desktop())))

    outcomes = await asyncio.gather(*(job for _, job in jobs), return_exceptions=True)
    for (channel, _), outcome in zip(jobs, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            if not isinstance(outcome, Exception):
                raise outcome
            channel_results[channel.value] = {
                "status": notification_ledger.STATUS_FAILED,
                "delivered": False,
                "error_type": type(outcome).__name__,
            }
            logger.error("notification delivery channel failed", {
                "user_id": proposal.user_id,
                "notification_id": notification_id,
                "channel": channel.value,
                "error_type": type(outcome).__name__,
            })
        elif channel == DeliveryChannel.MOBILE:
            mobile_result = outcome
            channel_results[channel.value] = {
                "status": (
                    notification_ledger.STATUS_SENT
                    if outcome.delivered
                    else notification_ledger.STATUS_FAILED
                ),
                "delivered": outcome.delivered,
                "tokens_targeted": outcome.tokens_targeted,
                "success_count": outcome.success_count,
                "failure_count": outcome.failure_count,
            }
        else:
            desktop_result = outcome
            channel_results[channel.value] = {
                "status": "queued" if outcome.created else "deduplicated",
                "delivered": outcome.accepted,
                "created": outcome.created,
            }

    mobile_result = mobile_result or NotificationResult(0, 0, 0)
    desktop_queued = 1 if desktop_result and desktop_result.accepted else 0
    result = NotificationResult(
        tokens_targeted=mobile_result.tokens_targeted,
        success_count=mobile_result.success_count,
        failure_count=mobile_result.failure_count,
        invalid_tokens=mobile_result.invalid_tokens,
        notification_id=notification_id,
        desktop_queued_count=desktop_queued,
        channel_results=channel_results,
    )

    data = proposal.data
    await notification_ledger.record_send(
        proposal.user_id,
        notification_id=notification_id,
        notification_type=proposal.notification_type,
        origin=str(data.get("notification_origin", proposal.notification_type)),
        title=proposal.title,
        body=proposal.body,
        url=str(data.get("url", "")),
        content_id=str(data.get("content_id", "")),
        source=str(data.get("source", "")),
        category=str(data.get("category", "")),
        content_kind=str(data.get("content_kind", "")),
        dedup_key=proposal.dedup_key,
        delivered=result.delivered,
        tokens_targeted=result.tokens_targeted,
        success_count=result.success_count,
        failure_count=result.failure_count,
        channel_results=channel_results,
        decision=proposal.decision,
    )
    return result
