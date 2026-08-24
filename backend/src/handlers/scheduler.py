"""
POST /scheduler/tick finds due reminders and sends FCM push notifications.
Called by a cron job (Cloud Scheduler) every minute.

Periodic work piggybacked here (avoids creating extra Cloud Scheduler jobs):
  minute % 30 == 0  — calendar fallback sync for all users
  hour == 1, minute == 30  — daily plan fan-out (= 07:00 IST)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from ..config.settings import settings
from ..lib.logger import logger
from ..services import alarm_sync
from ..services.notification_rewriter import rewrite_reminder_notification
from ..services.notifications import orchestrator
from ..services.notifications.proposal import (
    SOURCE_REMINDER,
    Disposition,
    NotificationProposal,
    ProposalKind,
)
from ..services.tool_executor import (
    claim_reminder_for_processing,
    fetch_due_reminders,
    mark_reminder_fired,
    mark_reminder_expired,
    reminder_delivery_deadline,
    reminder_delivery_terminal_reason,
    requeue_stuck_reminders,
)


def _json(status: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload),
    }


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


async def _fan_out_daily_plans() -> None:
    """Run the calendar reminder pipeline for every active user.

    Triggered once per day at 01:30 UTC (= 07:00 IST) from handle_scheduler_tick.
    run_daily_plan is idempotent per calendar-date, so a retry on the same day
    produces no duplicate work.

    Runs as fire-and-forget via asyncio.create_task so the scheduler tick
    returns its 200 before the LLM-heavy plan generation completes.
    """
    from ..services.daily_notification.orchestrator import run_daily_plan
    from ..services.signal_engine.feature_store import list_active_user_ids

    try:
        # force_refresh: this runs once a day, so the negligible cost of a fresh
        # read is worth it, freshness matters more here than for the every-minute
        # callers of this same query.
        user_ids = await list_active_user_ids(force_refresh=True)
    except Exception as exc:
        logger.error("scheduler: daily plan fan-out, failed to load active users", {"error": str(exc)})
        return

    if not user_ids:
        logger.info("scheduler: daily plan fan-out, no active users")
        return

    # Cap concurrent LLM calls to avoid Cloud Run OOM during the morning burst.
    semaphore = asyncio.Semaphore(5)

    async def _plan_one(uid: str) -> None:
        async with semaphore:
            await run_daily_plan(uid)

    await asyncio.gather(*[_plan_one(uid) for uid in user_ids])

    logger.info("scheduler: daily plan fan-out complete", {"users": len(user_ids)})


async def _enqueue_tick_tasks(*, tick_at: datetime) -> int:
    """Enqueue one deterministic Cloud Task per active user for the hourly clock signal.

    A tick has no business state to co-commit, so persisting it in Firestore's
    transactional outbox only creates write/read/update/delete amplification. Cloud
    Tasks already provides durable retry. This function is awaited by the scheduler
    request; partial enqueue failure raises so Cloud Scheduler retries, while
    deterministic task names absorb duplicates from that retry.
    """
    from ..services.engagement.task_scheduler import get_task_scheduler
    from ..services.signal_engine.feature_store import list_active_user_ids

    user_ids = await list_active_user_ids()
    if not user_ids:
        return 0

    semaphore = asyncio.Semaphore(10)
    task_scheduler = get_task_scheduler()
    failures: list[tuple[str, str]] = []

    async def _enqueue_one(uid: str) -> None:
        async with semaphore:
            try:
                await asyncio.to_thread(
                    task_scheduler.schedule_reactive_tick,
                    uid,
                    tick_at=tick_at,
                )
            except Exception as exc:
                failures.append((uid, str(exc)))
                logger.error("scheduler: reactive tick enqueue failed", {
                    "user_id": uid, "error": str(exc),
                })

    await asyncio.gather(*[_enqueue_one(uid) for uid in user_ids])
    if failures:
        raise RuntimeError(
            f"reactive tick enqueue failed for {len(failures)} of {len(user_ids)} users"
        )
    logger.info("scheduler: hourly reactive ticks enqueued", {"users": len(user_ids)})
    return len(user_ids)


async def _run_daily_briefing() -> None:
    """Daily-briefing fan-out, on a 15-minute gate.

    Fire-and-forget so the scheduler tick returns its 200 before the LLM-bound
    briefing generation runs. The engine self-gates: it only generates for users
    whose local time is BRIEFING_LOCAL_HOUR and claims once per local date, so
    firing it every 15 minutes is cheap (most users fall out at the hour gate).
    Internally isolated per user, so it can never delay or fail the reminder tick.
    """
    from ..services.briefing.briefing_engine import run_briefing_tick

    try:
        await run_briefing_tick()
    except Exception as exc:
        logger.error("scheduler: daily briefing tick failed", {"error": str(exc)})


async def _sweep_expired_candidates() -> None:
    """Delete expired content-pool candidates, on a 15-minute gate.

    Fire-and-forget so the scheduler tick returns its 200 before the (bounded)
    batched delete runs. Expired docs are already ignored by every pool reader, so
    this only removes dead weight that was crowding out fresh items in the per-user
    find_nearest top-K (the 2026-06-15 "pool HAS fresh content but vector search
    returned nothing for 1 user" warning). A Firestore native TTL on expires_at is
    the eventual backstop; this gives the immediacy the TTL lag cannot. Internally
    bounded + isolated, so it can never delay or fail the reminder tick.
    """
    from ..services.signal_engine.content_pool import delete_expired_candidates

    try:
        await delete_expired_candidates()
    except Exception as exc:
        logger.error("scheduler: expired-candidate sweep failed", {"error": str(exc)})


async def _requeue_stuck_reminders() -> None:
    """Return abandoned "processing" reminders to "pending".

    Isolated and fire-and-forget for the same reason as every other sweep here:
    a failure in recovery must never take down the delivery path it exists to
    protect. See requeue_stuck_reminders for why the stuck state happens at all.
    """
    try:
        await asyncio.to_thread(requeue_stuck_reminders)
    except Exception as exc:
        logger.error("scheduler: stuck-reminder requeue failed", {"error": str(exc)})


async def _run_tracking_checkpoints() -> None:
    """Topic-tracking checkpoint due-scan. Fire-and-forget so the tick returns its 200
    before any fetch/LLM work runs. The due-query is cheap and returns nothing most
    minutes; only genuinely-due checkpoints do any work, and atomic claims prevent a
    double fire under overlapping ticks. Internally isolated so it can never delay or
    fail the reminder tick."""
    from ..services.tracking.tracking_engine import run_checkpoint_tick

    try:
        await run_checkpoint_tick()
    except Exception as exc:
        logger.error("scheduler: tracking checkpoint tick failed", {"error": str(exc)})


async def _run_tracking_reconcile() -> None:
    """Topic-tracking reconcile — re-research each active topic and self-heal its
    schedule. Fire-and-forget; isolated per topic so one bad topic can never fail the
    tick."""
    from ..services.tracking.tracking_engine import run_reconcile_tick

    try:
        await run_reconcile_tick()
    except Exception as exc:
        logger.error("scheduler: tracking reconcile tick failed", {"error": str(exc)})


async def _run_proactive_drain() -> None:
    """Drain every user's proactive notification queue that actually has something in it.

    This is the engine of the proactive lane: each such user's pending/held proposals
    (thread / icebreaker / news / re-engage) are arbitrated, at most the single
    highest-priority one is sent, and the losers are held for a later window. Runs EVERY
    minute (like the reminder scan) so a held proposal can re-compete as windows open.

    Who to drain is discovered via ONE collection_group query across every user's queue
    (queue_store.list_user_ids_with_pending) instead of looping every active user and
    running a separate query per user regardless of whether their queue held anything —
    at this project's user count that was ~15 near-always-empty reads every single
    minute. drain_user_queue's own internal list_pending call is unchanged; it now just
    only runs for uids already known to have something queued. The dark-test allowlist
    gate is applied here (not skipped) so a candidate revision still can't send outside
    the allowlist just because it found its uid via the queue instead of the active-user
    scan. Fire-and-forget and per-user isolated, so it can never delay or fail the
    reminder tick. Flushes the PostHog buffer at the end so the post-send funnel events
    written inside the drain survive a Cloud Run container freeze.
    """
    from ..services.analytics import posthog_client
    from ..services.notifications import orchestrator
    from ..services.notifications.queue_store import list_user_ids_with_pending
    from ..services.signal_engine.feature_store import apply_proactive_allowlist

    try:
        queued_user_ids = await list_user_ids_with_pending()
    except Exception as exc:
        logger.error(
            "scheduler: proactive drain, failed to discover queued users",
            {"error": str(exc)},
        )
        return
    if not queued_user_ids:
        return

    user_ids = apply_proactive_allowlist(list(queued_user_ids))
    if not user_ids:
        return

    semaphore = asyncio.Semaphore(10)

    async def _drain_one(uid: str) -> None:
        async with semaphore:
            try:
                await orchestrator.drain_user_queue(uid)
            except Exception as exc:
                logger.warn("scheduler: proactive drain per-user failed", {
                    "user_id": uid, "error": str(exc),
                })

    await asyncio.gather(*[_drain_one(uid) for uid in user_ids])
    await posthog_client.flush()


async def _run_memory_graph_sweep(
    *, dry_run: bool = False, now: datetime | None = None
) -> list[tuple[str, Any]]:
    """Hourly Phase 4 source A/B sweep. Source C remains evidence only."""
    if not settings.NOTIF_GRAPH:
        return []
    try:
        from ..services.notifications.memory_graph_notifications import (
            run_memory_graph_sweep,
        )

        return await run_memory_graph_sweep(now=now, dry_run=dry_run)
    except Exception as exc:
        logger.error("scheduler: memory graph sweep failed open", {"error": str(exc)})
        return []


async def _run_memory_graph_candidate_drain(*, now: datetime | None = None) -> int:
    """Process due graph candidates through revalidation and the shared funnel."""
    if not settings.NOTIF_GRAPH:
        return 0
    try:
        from ..services.notifications.memory_graph_notifications import run_due_candidates

        return await run_due_candidates(now=now)
    except Exception as exc:
        logger.error("scheduler: memory graph candidate drain failed open", {
            "error": str(exc),
        })
        return 0


async def _run_session_followup_lifecycle_sweep(*, now: datetime | None = None) -> int:
    """Finalize due sessions through the sole lifecycle owner."""
    try:
        from ..services.session_followup.lifecycle import session_lifecycle_service

        return await session_lifecycle_service.sweep_idle_sessions(now=now)
    except Exception as exc:
        logger.error("scheduler: session followup lifecycle sweep failed open", {
            "error": str(exc),
        })
        return 0


async def _run_session_followup_drain(*, now: datetime | None = None) -> int:
    """Revalidate due source-D candidates and submit the survivors."""
    try:
        from ..services.session_followup.revalidator import run_due_followups

        return await run_due_followups(now=now)
    except Exception as exc:
        logger.error("scheduler: session followup drain failed open", {
            "error": str(exc),
        })
        return 0


async def _run_intent_sweep() -> None:
    """Reactive pending-intent supervisor, EVERY minute. Atomically claims due intents
    (a scheduled follow-up whose fire_at has passed) and emits one ``intent_due`` event
    each so the orchestrator delivers it. Cheap when nothing is due (one indexed query);
    atomic claims make overlapping ticks safe. Fire-and-forget; bounded + isolated, so
    it can never delay or fail the reminder tick."""
    from ..services.reactive.intent_supervisor import run_intent_sweep

    try:
        await run_intent_sweep()
    except Exception as exc:
        logger.error("scheduler: intent sweep failed", {"error": str(exc)})


async def _run_outbox_sweep() -> None:
    """Reactive event-bus relay: enqueue one coalesced /internal/orchestrate task
    per user with unconsumed outbox events. Runs every five minutes as the durable
    recovery backstop for post-commit inline dispatch. Persisted events represent
    real state-linked work; synthetic clock ticks use Cloud Tasks directly.
    Fire-and-forget and bounded inside the helper, so it can never delay or fail the
    reminder tick."""
    from ..services.reactive import event_bus

    try:
        await event_bus.dispatch_pending()
    except Exception as exc:
        logger.error("scheduler: outbox sweep failed", {"error": str(exc)})


async def _run_meeting_job_sweep() -> None:
    """Rediscover durable meeting jobs whose Cloud Tasks delivery was missed."""
    from ..services.meetings import tasks as meeting_tasks

    try:
        result = await meeting_tasks.dispatch_pending(limit=50)
        logger.info("scheduler: meeting durable-job sweep", {
            **result,
            "metric": "meeting_outbox_dispatch",
        })
    except Exception as exc:
        logger.error("scheduler: meeting durable-job sweep failed", {
            "error_code": "meeting_outbox_sweep_failed",
            "error": str(exc),
            "alert": True,
        })


async def _run_research_sweep() -> None:
    """Recover research work that Cloud Tasks did not deliver, or that died mid-stage.

    Five passes, all bounded and per-run isolated: outbox redelivery (the commit-then-
    crash window), stale-lease recovery, clarification expiry, stuck fan-out collapse,
    and deletion drain. Without this a crash between an advance commit and its inline
    dispatch would strand a run forever, because the durable state says work exists and
    nothing is delivering it.
    """
    from ..services.research.engine import get_research_engine

    try:
        result = await get_research_engine().sweep(limit=100)
        logger.info("scheduler: research sweep", {
            **result,
            "metric": "research_sweep",
        })
    except Exception as exc:
        logger.error("scheduler: research sweep failed", {
            "error_code": "research_sweep_failed",
            "error": str(exc),
            "alert": True,
        })


async def _run_dictation_audio_reconciliation() -> None:
    """Confirm 180-day lifecycle deletions and retire stale export pointers."""
    from ..services.dictation import store as dictation_store

    try:
        result = await dictation_store.reconcile_expired_audio()
        logger.info("scheduler: dictation audio reconciliation", result)
    except Exception as exc:
        logger.error(
            "scheduler: dictation audio reconciliation failed",
            {"error": str(exc), "error_type": type(exc).__name__},
        )


async def _run_dictation_metadata_reconciliation() -> None:
    """Delete expired Firestore trace metadata when TTL is delayed or unavailable."""
    from ..services.dictation import store as dictation_store

    try:
        result = await dictation_store.reconcile_expired_metadata()
        logger.info("scheduler: dictation metadata reconciliation", result)
    except Exception as exc:
        logger.error(
            "scheduler: dictation metadata reconciliation failed",
            {"error": str(exc), "error_type": type(exc).__name__},
        )


async def _run_meeting_reconciliation() -> None:
    from ..services.meetings.operations import reconciliation_snapshot

    try:
        await reconciliation_snapshot(limit=200)
    except Exception as exc:
        logger.error("scheduler: meeting reconciliation failed", {
            "error_code": "meeting_reconciliation_failed",
            "error": str(exc),
            "alert": True,
        })


async def _run_reengagement() -> None:
    """Dormancy win-back pass, once an hour at minute 45. Catches users idle 5-6 days
    (about to hit the 7-day active cliff) and sends ONE warm opener through the funnel.
    Fire-and-forget and internally isolated, so it can never delay or fail the tick."""
    from ..services.reengagement.reengagement_engine import run_reengagement_tick

    try:
        await run_reengagement_tick()
    except Exception as exc:
        logger.error("scheduler: reengagement tick failed", {"error": str(exc)})


async def _run_trial_lifecycle() -> None:
    """Trial 3-days-left warning + trial-ended notice, every 15 min at minute 7 —
    offset from thread(0)/briefing(5)/sweep(10)/reconcile(12)/icebreaker(15). The
    due-queries are cheap indexed collection_group scans, mostly empty. Fire-and-
    forget and internally isolated, so it can never delay or fail the tick."""
    from ..services.entitlement_notifications import run_trial_lifecycle_tick

    try:
        await run_trial_lifecycle_tick()
    except Exception as exc:
        logger.error("scheduler: trial lifecycle tick failed", {"error": str(exc)})


async def _sweep_stuck_chat_turns() -> None:
    """Backstop for the durable chat-completion Cloud Task. Finishes any chat turn still
    'generating' well past the task delay — the rare case the task failed to enqueue (the
    live request died before the synchronous enqueue) or failed to fire. Bounded and
    isolated, so it can never delay or fail the reminder tick."""
    from datetime import timedelta

    from ..services.chat_completion import turn_store
    from ..services.chat_completion.completion import complete_turn

    try:
        cutoff = datetime.now(UTC) - timedelta(minutes=5)
        stuck = await turn_store.list_stuck_turns(older_than=cutoff)
    except Exception as exc:
        logger.error("scheduler: stuck chat-turn sweep query failed", {"error": str(exc)})
        return
    if not stuck:
        return

    semaphore = asyncio.Semaphore(5)

    async def _complete_one(uid: str, cmid: str, session_id: str) -> None:
        async with semaphore:
            try:
                await complete_turn(uid, cmid, session_id or None)
            except Exception as exc:
                logger.warn("scheduler: stuck chat-turn complete failed", {
                    "user_id": uid, "cmid": cmid, "error": str(exc),
                })

    await asyncio.gather(*[_complete_one(u, c, s) for u, c, s in stuck])
    logger.info("scheduler: stuck chat-turn sweep complete", {"count": len(stuck)})


async def handle_scheduler_tick(event: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one scheduler tick.

    All Firestore / Firebase Admin SDK calls are synchronous (blocking I/O).
    They are dispatched to a thread-pool via `asyncio.to_thread` so they never
    block the event loop.

    Notifications are sent via the centralized `send_notification` function
    which handles token lookup, FCM multicast, and invalid-token cleanup automatically.
    """
    try:
        from ..services.google_calendar_connector import GoogleCalendarConnector

        now_utc = datetime.now(UTC)
        now_minute = now_utc.minute

        # Periodic fallback sync every 30 min. Catches events missed when:
        #   - Google push notification was dropped (documented ~small% rate)
        #   - Watch channel expired between 6-hour renewal windows
        #   - A transient scheduler failure delayed channel renewal past expiry
        periodic_sync_coro = (
            asyncio.to_thread(GoogleCalendarConnector.sync_all_connected_users)
            if now_minute % 30 == 0
            else asyncio.sleep(0)
        )

        # Channel-renewal check, every 5 min rather than every single minute. Lead
        # time is GOOGLE_CALENDAR_CHANNEL_RENEWAL_LEAD_SECONDS = 21600 (6 hours), so
        # checking every 5 min instead of 1 still leaves ~5h55m of margin before a
        # channel could expire uncaught — no realistic risk, and it cuts an
        # unconditional per-minute Firestore query down to 1/5th. NOT applied to
        # process_pending_sync_jobs below: that queue is the real-time delivery path
        # for webhook-triggered calendar syncs, so it stays every minute.
        renew_channels_coro = (
            asyncio.to_thread(GoogleCalendarConnector.renew_expiring_channels, 10)
            if now_minute % 5 == 0
            else asyncio.sleep(0)
        )

        renewed_channels, synced_calendars, due, periodic_sync_result = await asyncio.gather(
            renew_channels_coro,
            asyncio.to_thread(GoogleCalendarConnector.process_pending_sync_jobs, 20),
            asyncio.to_thread(fetch_due_reminders),
            periodic_sync_coro,
        )
        renewed_channels = renewed_channels or 0

        # Daily plan fan-out at 01:30 UTC = 07:00 IST.
        # Fire-and-forget: the tick returns 200 immediately; the LLM plan runs in
        # the background without blocking the Cloud Scheduler timeout window.
        if now_utc.hour == 1 and now_minute == 30:
            asyncio.create_task(_fan_out_daily_plans())

        # Hourly reactive heartbeat: enqueue one deterministic Cloud Task per active
        # user. Clock signals are transient and never touch Firestore. Await the
        # fan-out so a partial Cloud Tasks failure returns 500 and Cloud Scheduler
        # retries; deterministic task names suppress duplicates on that retry.
        if now_minute == 0:
            await _enqueue_tick_tasks(tick_at=now_utc)

        # Daily briefing fan-out, every 15 minutes at minutes 5/20/35/50 — offset
        # from the thread reflector (minute 0) and icebreaker (minute 15) so the LLM
        # passes never burst together. The engine self-gates to each user's local
        # BRIEFING_LOCAL_HOUR and claims once per local date, so running it 4x/hour
        # is cheap. Fire-and-forget.
        if now_minute % 15 == 5:
            asyncio.create_task(_run_daily_briefing())

        # Expired content-pool sweep, every 15 minutes at minute 10 — offset from the
        # thread reflector (0), briefing (5), icebreaker (15), and calendar sync (30).
        # Deletes content_candidates past expires_at so the tombstone pile can't crowd
        # out fresh items in the per-user find_nearest top-K (2026-06-15). Fire-and-
        # forget; bounded + isolated inside the helper so it never delays/fails the tick.
        if now_minute % 15 == 10:
            asyncio.create_task(_sweep_expired_candidates())

        # Abandoned-reminder recovery, every 5 minutes at minute 3 — offset from
        # briefing(5)/sweep(10)/reconcile(12)/icebreaker(15). A reminder claimed
        # by a tick that then died, or whose delivery failed, sits in "processing"
        # where NOTHING can see it again; this is the only path that returns one.
        # Cheap indexed query, empty on almost every run. Fire-and-forget.
        if now_minute % 5 == 3:
            asyncio.create_task(_requeue_stuck_reminders())

        # Topic-tracking checkpoint due-scan, EVERY minute (like the reminder scan) so
        # a pre/live/post update fires near its exact moment. The due-query is cheap and
        # empty most minutes; only due checkpoints do fetch/LLM work, and atomic claims
        # make overlapping ticks safe. Fire-and-forget; no-op while the flag is off.
        asyncio.create_task(_run_tracking_checkpoints())

        # Topic-tracking reconcile (re-research + schedule self-heal), every 15 min at
        # minute 12 — offset from briefing(5)/sweep(10)/icebreaker(15)/thread(0) so the
        # LLM passes never burst together. Fire-and-forget; no-op while the flag is off.
        if now_minute % 15 == 12:
            asyncio.create_task(_run_tracking_reconcile())

        # Trial 3-days-left warning + trial-ended notice, every 15 min at minute 7 —
        # offset from thread(0)/briefing(5)/sweep(10)/reconcile(12)/icebreaker(15).
        # Fire-and-forget; cheap indexed queries, mostly empty.
        if now_minute % 15 == 7:
            asyncio.create_task(_run_trial_lifecycle())

        # Dormancy win-back, once an hour at minute 45 (offset from thread 0 / briefing 5
        # / sweep 10 / icebreaker 15 / reconcile 12). Wins back users idle 5-6 days before
        # the 7-day active cliff. Fire-and-forget and internally isolated.
        if now_minute == 45:
            asyncio.create_task(_run_reengagement())

        # Backstop for the durable chat-completion Cloud Task, every 5 min at minutes
        # ending in 3/8 (offset from thread 0 / briefing 5 / sweep 10 / reconcile 12 /
        # icebreaker 15 / calendar 30 / reengage 45). Finishes any abandoned chat turn the
        # Cloud Task missed and pushes the reply. Fire-and-forget; bounded + isolated.
        if now_minute % 5 == 3:
            asyncio.create_task(_sweep_stuck_chat_turns())

        # Reactive pending-intent supervisor, EVERY minute. Fires due scheduled
        # follow-ups (the revocable-intent "fire" half) as intent_due events. Fire-and-
        # forget; cheap when nothing is due; atomic claims make overlapping ticks safe.
        asyncio.create_task(_run_intent_sweep())

        # Reactive event-bus outbox relay every five minutes. All current persisted
        # producers also dispatch inline after commit; this slower sweep is their
        # durable recovery path, not a polling path for synthetic clock work.
        if now_minute % 5 == 1:
            asyncio.create_task(_run_outbox_sweep())

        # Firestore meeting jobs are authoritative; Cloud Tasks is only the
        # delivery hint. This is the commit/dispatch crash recovery path.
        if now_minute % 5 == 2:
            asyncio.create_task(_run_meeting_job_sweep())

        # Same contract as the meeting sweep above, on its own free slot so the two
        # never share a tick. AWAITED, unlike the meeting sweep: this is the research
        # engine's only recovery path for a stage whose inline dispatch failed, and Cloud
        # Run may suspend CPU the moment this response ends, so a detached task is not
        # reliably allowed to finish. Same reasoning as the dictation reconciliation.
        if now_minute % 5 == 4:
            await _run_research_sweep()

        # Await this too: it now REPAIRS (redelivers stranded jobs, stamps
        # stalled meetings) rather than only counting, and a detached task can be
        # cut off the moment this response ends, leaving the repair half-done.
        if now_minute == 27:
            await _run_meeting_reconciliation()

        # GCS lifecycle deletion is asynchronous. Hourly reconciliation confirms
        # absence by exact object generation before clearing has_audio, so corpus
        # exports never write a manifest entry for a blob that no longer exists.
        # Await this bounded batch: Cloud Run may suspend CPU as soon as the
        # scheduler response ends, so a detached task would not be reliable.
        if now_minute == 40:
            await _run_dictation_audio_reconciliation()
        if now_minute == 41:
            await _run_dictation_metadata_reconciliation()

        # Proactive notification queue drain, EVERY minute. The producers (thread /
        # icebreaker / news / re-engage) only ENQUEUE; this is where their proposals are
        # arbitrated by priority, deduped cross-agent, tap-gated, and at most ONE is sent
        # per user per window (the rest wait). Fire-and-forget and per-user isolated.
        asyncio.create_task(_run_proactive_drain())

        # Phase 4 is fully additive. With NOTIF_GRAPH off, neither task is created,
        # so the scheduler performs no graph reads and writes no candidate state.
        if settings.NOTIF_GRAPH:
            if now_minute == 25:
                asyncio.create_task(_run_memory_graph_sweep(now=now_utc))
            asyncio.create_task(_run_memory_graph_candidate_drain(now=now_utc))

        # Source D: finalize idle sessions, then fire the candidates that came due.
        # Both are every-minute because a follow-up's value decays with its delay.
        asyncio.create_task(_run_session_followup_lifecycle_sweep(now=now_utc))
        asyncio.create_task(_run_session_followup_drain(now=now_utc))

        accepted = 0
        mobile_accepted = 0
        desktop_queued = 0
        expired = 0

        for item in due:
            user_id: str = item["userId"]
            reminder_id: str = item["reminderId"]
            data: dict[str, Any] = item["data"]

            try:
                terminal_reason = reminder_delivery_terminal_reason(data)
                if terminal_reason is not None:
                    did_expire = await asyncio.to_thread(
                        mark_reminder_expired,
                        user_id,
                        reminder_id,
                        terminal_reason,
                    )
                    expired += int(did_expire)
                    logger.warn("Reminder delivery terminalized before send", {
                        "user_id": user_id,
                        "reminder_id": reminder_id,
                        "reason": terminal_reason,
                    })
                    continue

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
                    continue

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
                    accepted += 1
                    mobile_accepted += decision.success_count or 0
                    desktop_queued += decision.desktop_queued_count or 0
                    logger.info("Reminder transport accepted", {
                        "user_id": user_id,
                        "reminder_id": reminder_id,
                        "logical_accepted": 1,
                        "mobile_accepted": decision.success_count or 0,
                        "desktop_queued": decision.desktop_queued_count or 0,
                    })
                elif decision.disposition == Disposition.DROP:
                    did_expire = await asyncio.to_thread(
                        mark_reminder_expired,
                        user_id,
                        reminder_id,
                        f"orchestrator_{decision.reason}",
                    )
                    expired += int(did_expire)
                    logger.warn("Reminder terminally dropped", {
                        "user_id": user_id,
                        "reminder_id": reminder_id,
                        "reason": decision.reason,
                    })
                else:
                    logger.warn("Reminder transport not accepted", {
                        "user_id": user_id,
                        "reminder_id": reminder_id,
                        "disposition": decision.disposition.value,
                        "reason": decision.reason,
                    })

            except Exception as exc:
                logger.error("Failed to deliver reminder", {
                    "user_id": user_id,
                    "reminder_id": reminder_id,
                    "error": str(exc),
                })

        periodic_synced = (
            (periodic_sync_result or {}).get("users_synced", 0)
            if isinstance(periodic_sync_result, dict)
            else 0
        )

        logger.info("Scheduler tick complete", {
            "scanned": len(due),
            "accepted": accepted,
            "mobile_accepted": mobile_accepted,
            "desktop_queued": desktop_queued,
            "expired": expired,
            "calendar_syncs": synced_calendars,
            "renewed_calendar_channels": renewed_channels,
            "periodic_sync_users": periodic_synced,
        })
        return _json(200, {
            "scanned": len(due),
            "accepted": accepted,
            "mobile_accepted": mobile_accepted,
            "desktop_queued": desktop_queued,
            "expired": expired,
            "calendar_syncs": synced_calendars,
            "renewed_calendar_channels": renewed_channels,
            "periodic_sync_users": periodic_synced,
        })

    except Exception as exc:
        logger.error("Scheduler tick failed", {"error": str(exc)})
        return _json(500, {"error": "Internal server error"})
