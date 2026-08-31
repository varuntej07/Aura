"""Delivery pipeline for one due reminder.

Extracted verbatim from handlers/scheduler.py so the scheduler tick handler owns
only the claim/iterate/tally loop. Everything here is the per-reminder path:
terminalization check, atomic claim, LLM copy rewrite, alarm-vs-reminder push
shaping, funnel submission, and the disposition-to-state mapping. Payload keys,
log message strings, and Firestore state transitions are a cross-client contract
and must not change.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Any, NamedTuple

from ..lib.logger import logger
from . import alarm_sync
from .notification_rewriter import rewrite_reminder_notification
from .notifications import orchestrator
from .notifications.proposal import (
    SOURCE_REMINDER,
    Disposition,
    NotificationProposal,
    ProposalKind,
)
from .tool_executor import (
    claim_reminder_for_processing,
    mark_reminder_fired,
    mark_reminder_expired,
    reminder_delivery_deadline,
    reminder_delivery_terminal_reason,
)


class DeliveryOutcome(NamedTuple):
    """Per-reminder tallies the scheduler tick sums into its response counters."""

    accepted: int = 0
    mobile_accepted: int = 0
    desktop_queued: int = 0
    expired: int = 0


def _reminder_dedup_key(message: str, trigger_at_iso: str | None) -> str:
    """Cross-send dedup key for a reminder: the same message firing in the same
    minute is the same reminder.

    This is the ledger backstop the create-time window cannot cover. A sub-second
    CONCURRENT double-create mints two docs with near-identical fire times; they
    collide on this key and the orchestrator drops the second (24h ledger
    window). The sequential "minute apart" replay is handled upstream at creation
    instead, since by definition those two land in different minute buckets.
    """
    minute = "na"
    if isinstance(trigger_at_iso, str):
        try:
            minute = (
                datetime.fromisoformat(trigger_at_iso).astimezone(UTC).strftime("%Y%m%d%H%M")
            )
        except ValueError:
            minute = "na"
    digest = hashlib.sha1(message.strip().casefold().encode("utf-8")).hexdigest()[:12]
    return f"reminder_{minute}_{digest}"


async def deliver_due_reminder(
    user_id: str,
    reminder_id: str,
    data: dict[str, Any],
) -> DeliveryOutcome:
    """Deliver one due reminder end to end, with per-item error isolation.

    Returns the tallies the scheduler loop adds into its counters. Any failure
    is logged and swallowed here (zero tallies) so one bad reminder can never
    stop the rest of the batch.
    """
    try:
        terminal_reason = reminder_delivery_terminal_reason(data)
        if terminal_reason is not None:
            did_expire = await asyncio.to_thread(
                mark_reminder_expired,
                user_id,
                reminder_id,
                terminal_reason,
            )
            logger.warn("Reminder delivery terminalized before send", {
                "user_id": user_id,
                "reminder_id": reminder_id,
                "reason": terminal_reason,
            })
            return DeliveryOutcome(expired=int(did_expire))

        # Atomically claim the reminder before any slow work (model call, FCM).
        # If another scheduler tick already claimed it, skip — prevents duplicate fires.
        claimed = await asyncio.to_thread(
            claim_reminder_for_processing,
            user_id,
            reminder_id,
        )
        if not claimed:
            logger.info("Reminder already claimed by concurrent tick, skipping", {
                "user_id": user_id,
                "reminder_id": reminder_id,
            })
            return DeliveryOutcome()

        raw_message = str(data.get("message", "Reminder due now"))
        copy = await rewrite_reminder_notification(raw_message)
        body = copy.body
        is_alarm = alarm_sync.is_alarm(data)
        # An alarm keeps a stable, unmistakable title: someone half asleep at
        # 6am needs to recognise it instantly, not read a witty subject line.
        # A plain reminder takes the framed title when there is one, so the
        # bold line names the actual thing instead of the word "Reminder".
        title = "Buddy Alarm" if is_alarm else (copy.title or "Buddy Reminder")

        # Committed lane: the user asked for this, so the orchestrator sends
        # it inline (freshness n/a, dedup handled by the atomic claim above).
        # The orchestrator records the committed send to the shared budget
        # itself, so a later proactive push is spaced away from it.
        decision = await orchestrator.submit(
            NotificationProposal(
                user_id=user_id,
                source=SOURCE_REMINDER,
                kind=ProposalKind.COMMITTED,
                # Backstop the atomic claim for a concurrent double-create:
                # two same-minute same-message docs collide on this key and the
                # orchestrator drops the second within its 24h ledger window.
                dedup_key=_reminder_dedup_key(raw_message, data.get("trigger_at")),
                title=title,
                body=body,
                data={
                    "reminder_id": reminder_id,
                    # The tap-through chat seed. Without this, the client
                    # falls back to the rewritten push BODY alone, and the
                    # rewriter may have moved half the instruction into the
                    # title, which the tap path discards. The raw message is
                    # the only fire-time text that is never LLM-generated.
                    "opening_chat_message": f"Reminder: {raw_message}",
                    "created_via": str(data.get("created_via", "voice")),
                    "tier": alarm_sync.normalize_tier(data.get("tier")),
                    # For an alarm this push is a BACKSTOP, not the
                    # delivery mechanism: the device should already have
                    # rung off its own local schedule seconds ago. The
                    # flag lets the client suppress this banner when its
                    # native ledger says the alarm already fired, so a
                    # half-asleep user is not woken a second time by the
                    # safety net for the thing that worked.
                    "alarm_fallback": "1" if is_alarm else "0",
                    # The client dedupes one alarm OCCURRENCE, not the
                    # reminder id. A snooze keeps the same id but must be
                    # allowed to ring again at its new trigger time.
                    **(
                        {"alarm_trigger_at": str(data.get("trigger_at", ""))}
                        if is_alarm
                        else {}
                    ),
                    # data_only strips the display block, so the text has
                    # to travel in the payload for the client to render
                    # it. Only populated for alarms; a plain reminder is
                    # still drawn by the OS from title/body above.
                    **({"alarm_body": body} if is_alarm else {}),
                },
                notification_type="reminder",
                # Collapse prevents duplicate banners if overlapping scheduler
                # ticks fire before the user dismisses the first notification.
                collapse_key=f"reminder_{reminder_id}",
                apns_category="BUDDY_REMINDER",
                # An alarm's backstop is sent data-only so the CLIENT
                # decides whether to render it. Android draws a
                # notification block itself, with no chance to ask
                # whether the local alarm already rang, so a banner would
                # arrive seconds after the device stopped ringing and
                # wake a half-asleep user for the thing that worked.
                # Plain reminders keep the OS-rendered banner: there is
                # nothing local to duplicate.
                data_only=is_alarm,
                valid_until=reminder_delivery_deadline(data),
            )
        )

        if decision.disposition == Disposition.SEND and decision.transport_accepted:
            await asyncio.to_thread(mark_reminder_fired, user_id, reminder_id)
            logger.info("Reminder transport accepted", {
                "user_id": user_id,
                "reminder_id": reminder_id,
                "logical_accepted": 1,
                "mobile_accepted": decision.success_count or 0,
                "desktop_queued": decision.desktop_queued_count or 0,
            })
            return DeliveryOutcome(
                accepted=1,
                mobile_accepted=decision.success_count or 0,
                desktop_queued=decision.desktop_queued_count or 0,
            )
        elif decision.disposition == Disposition.DROP:
            did_expire = await asyncio.to_thread(
                mark_reminder_expired,
                user_id,
                reminder_id,
                f"orchestrator_{decision.reason}",
            )
            logger.warn("Reminder terminally dropped", {
                "user_id": user_id,
                "reminder_id": reminder_id,
                "reason": decision.reason,
            })
            return DeliveryOutcome(expired=int(did_expire))
        else:
            logger.warn("Reminder transport not accepted", {
                "user_id": user_id,
                "reminder_id": reminder_id,
                "disposition": decision.disposition.value,
                "reason": decision.reason,
            })
            return DeliveryOutcome()

    except Exception as exc:
        logger.error("Failed to deliver reminder", {
            "user_id": user_id,
            "reminder_id": reminder_id,
            "error": str(exc),
        })
        return DeliveryOutcome()
