"""The single notification funnel.

``submit(proposal)`` is the ONLY way anything in Aura sends a push, and
``_deliver`` is the ONLY place ``notification_service.send_notification`` is
called. Two lanes:

  * COMMITTED → sent inline now (freshness + dedup only; never held/arbitrated),
    then recorded to the budget so a later proactive push spaces away from it.
  * PROACTIVE → enqueued; ``drain_user_queue`` (per-minute, per active user on
    ``/scheduler/tick``) drops stale/duplicate items, arbitrates the rest by
    priority, claims a budget slot, sends the single winner, and HOLDS the losers
    for a later window.

Every disposition (send / hold / drop) is logged with its reason so a held or
dropped notification is never silent — the failure mode the old per-engine direct
sends had (a tracker that "fired" but sent nothing looked identical to a healthy
tick).
"""

from __future__ import annotations

import asyncio
import statistics
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...lib.logger import logger
from .. import notification_budget, notification_ledger
from ..firebase import admin_firestore
from ..notification_service import NotificationResult, send_notification
from . import post_send, queue_store, tap_gate
from .proposal import (
    REASON_ACTIVE_TRACKER,
    REASON_BUDGET,
    REASON_DUPLICATE,
    REASON_OFF_PEAK,
    REASON_OK,
    REASON_PRESENCE,
    REASON_QUIET_HOURS,
    REASON_STALE,
    REASON_SUPERSEDED,
    REASON_TAP_GATE,
    SOURCE_ICEBREAKER,
    Disposition,
    NotificationProposal,
    OrchestratorDecision,
    ProposalKind,
    is_stale,
    proposal_sort_key,
)

# Smart timing applies only to the lowest-priority proactive content (news, prio 10):
# personal openers (icebreaker) and higher fire at their own chosen moment, and an
# urgent breaking item opts out by lane. A held news item that never finds a good slot
# is purged by the queue TTL — fine, stale news shouldn't fire anyway.
SMART_TIMING_MAX_PRIORITY = 10

# How far back the cross-agent dedup gate looks. A day is enough to stop the same
# story firing from two deciders or a proactive item being re-proposed each tick,
# without suppressing a genuinely new daily update.
DEDUP_WINDOW = timedelta(hours=24)

# Quiet hours (local): no PROACTIVE delivery. Committed sends (a due reminder,
# a meeting) bypass this — the user asked for them.
_QUIET_START_MINUTES = 23 * 60 + 30  # 23:30
_QUIET_END_MINUTES = 7 * 60          # 07:00

# How far back to check for a recent tracker delivery when deciding whether the user is
# in the middle of a live tracked event (a match in progress) right now. A live match
# polls every 15-30 min, so this comfortably bridges consecutive tracker pushes without
# gaps, while releasing proactive delivery again within ~90 min of a match's last beat —
# no need to special-case "the match just ended" separately.
ACTIVE_TRACKER_HOLD_WINDOW = timedelta(minutes=90)


# ── Public API ──────────────────────────────────────────────────────────────
async def submit(
    proposal: NotificationProposal, *, now: datetime | None = None
) -> OrchestratorDecision:
    """Route a proposal. Committed sends inline; proactive enqueues for the drain."""
    now = now or datetime.now(UTC)
    if proposal.kind == ProposalKind.COMMITTED:
        return await _send_committed(proposal, now)

    await queue_store.enqueue(proposal, now=now)
    logger.info("orchestrator: proactive enqueued", {
        "user_id": proposal.user_id,
        "source": proposal.source,
        "priority": proposal.effective_priority,
    })
    return OrchestratorDecision(Disposition.HOLD, "queued")


async def drain_user_queue(
    user_id: str, *, now: datetime | None = None
) -> OrchestratorDecision | None:
    """Process one user's proactive queue: drop stale/dupes, arbitrate, send one.

    Returns the winner's decision, or ``None`` if the queue was empty / nothing
    was eligible. Safe to call every minute for every active user — it is cheap
    when the queue is empty (one indexed equality read).
    """
    now = now or datetime.now(UTC)
    pending = await queue_store.list_pending(user_id)
    if not pending:
        return None

    recent_keys = await notification_ledger.recent_dedup_keys(
        user_id, since=now - DEDUP_WINDOW
    )

    # Stage 1: freshness + cross-agent dedup. Both are terminal drops.
    survivors: list[tuple[str, NotificationProposal]] = []
    for pid, proposal in pending:
        if is_stale(proposal, now):
            await queue_store.mark(user_id, pid, queue_store.STATUS_DROPPED, now=now)
            _log_drop(proposal, REASON_STALE)
            continue
        if proposal.dedup_key and proposal.dedup_key in recent_keys:
            await queue_store.mark(user_id, pid, queue_store.STATUS_DROPPED, now=now)
            _log_drop(proposal, REASON_DUPLICATE)
            continue
        survivors.append((pid, proposal))

    if not survivors:
        return None

    # Stage 2: quiet hours hold everything for a later window.
    local_now, local_date = await _user_local(user_id, now)
    if _is_quiet_hours(local_now):
        await _hold_all(user_id, survivors, now)
        logger.info("orchestrator: proactive held (quiet hours)", {
            "user_id": user_id, "held": len(survivors),
        })
        return OrchestratorDecision(Disposition.HOLD, REASON_QUIET_HOURS)

    # Stage 2.5: surface-aware delivery. Hold the whole proactive batch when the user
    # is in the app right now (a push would be redundant) or on a dismiss streak (Buddy
    # reads the room and goes quiet). A HOLD, so nothing is lost — it re-competes the
    # next window, and the next tap/open clears the streak. Committed sends never reach
    # here. Fails open (a presence read error never holds).
    hold_present, present_reason = await _presence_hold(user_id, now)
    if hold_present:
        await _hold_all(user_id, survivors, now)
        logger.info("orchestrator: proactive held (surface-aware)", {
            "user_id": user_id, "reason": present_reason, "held": len(survivors),
        })
        return OrchestratorDecision(Disposition.HOLD, REASON_PRESENCE)

    # Stage 2.7: hold everything ELSE while a tracked event (a live match) is actively
    # firing for this user. Tracking itself is COMMITTED (never reaches this queue), so
    # this only holds thread/icebreaker/news — reserving attention for the event the user
    # explicitly asked to be kept posted on instead of diluting it with unrelated content.
    # A HOLD, so nothing is lost — it re-competes once the tracker goes quiet.
    if await _has_active_tracker(user_id, now):
        await _hold_all(user_id, survivors, now)
        logger.info("orchestrator: proactive held (active tracker)", {
            "user_id": user_id, "held": len(survivors),
        })
        return OrchestratorDecision(Disposition.HOLD, REASON_ACTIVE_TRACKER)

    # Stage 3: arbitrate — highest priority wins, the rest wait.
    survivors.sort(key=lambda pair: proposal_sort_key(pair[1]), reverse=True)
    winner_pid, winner = survivors[0]
    losers = survivors[1:]

    # Stage 3.3: smart timing. Defer a low-priority content winner (news) when this is a
    # weak engagement hour for THIS user, so it lands when they actually open the app. A
    # breaking item opts out (urgent), and a user with no learned slot rates is never held
    # (flat default → every hour is "preferred"). The queue TTL is the backstop — a news
    # item that never finds a good slot simply expires, which is fine for stale news.
    if (
        winner.effective_priority <= SMART_TIMING_MAX_PRIORITY
        and winner.data.get("lane") != "breaking"
        and not await _is_preferred_slot(user_id, local_now)
    ):
        await _hold_all(user_id, survivors, now)
        logger.info("orchestrator: proactive held (off-peak smart timing)", {
            "user_id": user_id, "source": winner.source, "held": len(survivors),
        })
        return OrchestratorDecision(Disposition.HOLD, REASON_OFF_PEAK)

    # Stage 3.5: tap-worthiness gate on the winner (balanced bar). A low-value push is
    # worse than silence, so a rejected winner is DROPPED and the losers are held for a
    # later — possibly stronger — window. Judged only on the arbitration winner (one LLM
    # call), and fails OPEN so a judge outage never silences the funnel.
    worthy, tap_reason = await tap_gate.passes(winner)
    if not worthy:
        await queue_store.mark(user_id, winner_pid, queue_store.STATUS_DROPPED, now=now)
        _log_drop(winner, f"{REASON_TAP_GATE}:{tap_reason}")
        for pid, _ in losers:
            await queue_store.mark(user_id, pid, queue_store.STATUS_HELD, now=now)
        logger.info("orchestrator: proactive dropped (tap gate)", {
            "user_id": user_id, "source": winner.source, "reason": tap_reason,
            "held_losers": len(losers),
        })
        return OrchestratorDecision(Disposition.DROP, REASON_TAP_GATE)

    # Stage 4: budget slot (fail-open; effectively unlimited during beta).
    claim = await notification_budget.try_claim_proactive_slot(
        user_id,
        source=winner.source,
        user_local_date=local_date,
        now=now,
        priority=(winner.source == SOURCE_ICEBREAKER),
    )
    if not claim.allowed:
        await _hold_all(user_id, survivors, now)
        logger.info("orchestrator: proactive held (budget)", {
            "user_id": user_id, "reason": claim.reason, "held": len(survivors),
        })
        return OrchestratorDecision(Disposition.HOLD, REASON_BUDGET)

    # Stage 5: atomic cross-agent dedup claim (see _claim_dedup — replaces a
    # read-then-check race that let two overlapping drains both send the same
    # dedup_key), then deliver the winner and hold the losers.
    if winner.dedup_key and not await _claim_dedup(winner.dedup_key, user_id):
        await queue_store.mark(user_id, winner_pid, queue_store.STATUS_DROPPED, now=now)
        _log_drop(winner, REASON_DUPLICATE)
        await _hold_all(user_id, losers, now)
        logger.info("orchestrator: proactive dropped (duplicate)", {
            "user_id": user_id, "source": winner.source, "held_losers": len(losers),
        })
        return OrchestratorDecision(Disposition.DROP, REASON_DUPLICATE)

    try:
        result = await _deliver(winner)
    except Exception:
        if winner.dedup_key:
            await _release_dedup(winner.dedup_key, user_id)
        raise
    if not result.delivered and winner.dedup_key:
        await _release_dedup(winner.dedup_key, user_id)
    await queue_store.mark(user_id, winner_pid, queue_store.STATUS_SENT, now=now)
    # Producer-specific bookkeeping (thread follow-up count, icebreaker memory, signal
    # learning outcome + funnel) runs HERE, on the real delivery — not in the producer
    # tick, which only enqueued. Never raises into the drain.
    await post_send.dispatch_post_send(winner, result)
    for pid, proposal in losers:
        await queue_store.mark(user_id, pid, queue_store.STATUS_HELD, now=now)
        _log_hold(proposal, REASON_SUPERSEDED)

    logger.info("orchestrator: proactive sent", {
        "user_id": user_id,
        "source": winner.source,
        "priority": winner.effective_priority,
        "delivered": result.delivered,
        "held_losers": len(losers),
    })
    return OrchestratorDecision(
        Disposition.SEND, REASON_OK,
        delivered=result.delivered,
        tokens_targeted=result.tokens_targeted,
        success_count=result.success_count,
        failure_count=result.failure_count,
    )


# ── Committed lane ───────────────────────────────────────────────────────────
async def _send_committed(
    proposal: NotificationProposal, now: datetime
) -> OrchestratorDecision:
    if is_stale(proposal, now):
        _log_drop(proposal, REASON_STALE)
        return OrchestratorDecision(Disposition.DROP, REASON_STALE)

    if proposal.dedup_key and not await _claim_dedup(proposal.dedup_key, proposal.user_id):
        _log_drop(proposal, REASON_DUPLICATE)
        return OrchestratorDecision(Disposition.DROP, REASON_DUPLICATE)

    try:
        result = await _deliver(proposal)
    except Exception:
        if proposal.dedup_key:
            await _release_dedup(proposal.dedup_key, proposal.user_id)
        raise
    if not result.delivered and proposal.dedup_key:
        await _release_dedup(proposal.dedup_key, proposal.user_id)
    if result.delivered:
        # Record for spacing (only a real delivery is worth spacing away from) so a
        # later proactive push keeps its distance from this committed one.
        await notification_budget.record_committed_send(
            proposal.user_id, source=proposal.source, now=now
        )
    logger.info("orchestrator: committed sent", {
        "user_id": proposal.user_id,
        "source": proposal.source,
        "delivered": result.delivered,
    })
    return OrchestratorDecision(
        Disposition.SEND, REASON_OK,
        delivered=result.delivered,
        tokens_targeted=result.tokens_targeted,
        success_count=result.success_count,
        failure_count=result.failure_count,
    )


# ── The single send choke point ──────────────────────────────────────────────
async def _deliver(proposal: NotificationProposal) -> NotificationResult:
    """The ONLY call to ``send_notification`` in the whole system."""
    return await send_notification(
        proposal.user_id,
        title=proposal.title,
        body=proposal.body,
        data=proposal.data,
        notification_type=proposal.notification_type,
        collapse_key=proposal.collapse_key,
        data_only=proposal.data_only,
        apns_category=proposal.apns_category,
        dedup_key=proposal.dedup_key,
        decision=proposal.decision,
    )


# ── Helpers ──────────────────────────────────────────────────────────────────
async def _hold_all(
    user_id: str, pairs: list[tuple[str, NotificationProposal]], now: datetime
) -> None:
    for pid, _ in pairs:
        await queue_store.mark(user_id, pid, queue_store.STATUS_HELD, now=now)


async def _has_active_tracker(user_id: str, now: datetime) -> bool:
    """True if a tracker update was delivered to this user within
    ``ACTIVE_TRACKER_HOLD_WINDOW`` — the signal that a tracked event (a live match) is
    actively firing for them right now. Lazy-imports the tracking notification-type
    constant (see ``_presence_hold`` for why a top-level import is avoided here too —
    this keeps the funnel's normal dependency direction, producers depend on the
    funnel, not the other way around, so this one constant lookup stays lazy rather
    than adding a permanent module-level coupling). Fails open: a read error never
    holds a send that would otherwise go out."""
    try:
        from ..tracking.fields import NOTIFICATION_TYPE_TRACKER_UPDATE

        return await notification_ledger.has_recent_delivery(
            user_id, NOTIFICATION_TYPE_TRACKER_UPDATE, since=now - ACTIVE_TRACKER_HOLD_WINDOW,
        )
    except Exception as exc:
        logger.warn("orchestrator: active-tracker check failed (fail-open)", {
            "user_id": user_id, "error": str(exc),
        })
        return False


async def _claim_dedup(dedup_key: str, user_id: str) -> bool:
    """Atomic cross-agent dedup claim. Replaces a read-then-check race
    (``recent_dedup_keys`` read, then a later ``_deliver``) that let two
    concurrent sends of the same ``dedup_key`` both pass the check before
    either one's ledger write was visible to the other. Lazy-imports the
    reactive idempotency store (see ``_presence_hold`` for why). Fails open: a
    claim-store error never blocks a send — a rare duplicate is preferable to
    silently dropping a real notification."""
    try:
        from ..reactive import idempotency

        return await idempotency.idempotent(
            f"notif_dedup:{dedup_key}", scope=user_id, ttl=DEDUP_WINDOW,
        )
    except Exception as exc:
        logger.warn("orchestrator: dedup claim failed (fail-open)", {
            "user_id": user_id, "dedup_key": dedup_key, "error": str(exc),
        })
        return True


async def _release_dedup(dedup_key: str, user_id: str) -> None:
    """Release a dedup claim after a failed send so a legitimate retry isn't
    blocked for the rest of the TTL window. Best-effort."""
    try:
        from ..reactive import idempotency

        await idempotency.release(f"notif_dedup:{dedup_key}", scope=user_id)
    except Exception as exc:
        logger.warn("orchestrator: dedup release failed (TTL will reclaim it)", {
            "user_id": user_id, "dedup_key": dedup_key, "error": str(exc),
        })


async def _presence_hold(user_id: str, now: datetime) -> tuple[bool, str]:
    """Surface-aware hold check. Lazy-imports the reactive presence store (the reactive
    orchestrator imports this funnel, so a top-level import would cycle). Fails open: a
    presence-store error never holds a notification."""
    try:
        from ..reactive import presence

        return await presence.should_hold_proactive(user_id, now=now)
    except Exception as exc:
        logger.warn("orchestrator: presence hold check failed (fail-open)", {
            "user_id": user_id, "error": str(exc),
        })
        return False, ""


def _is_quiet_hours(local_now: datetime) -> bool:
    minutes = local_now.hour * 60 + local_now.minute
    if _QUIET_START_MINUTES <= _QUIET_END_MINUTES:
        return _QUIET_START_MINUTES <= minutes < _QUIET_END_MINUTES
    # window wraps midnight (23:30 -> 07:00)
    return minutes >= _QUIET_START_MINUTES or minutes < _QUIET_END_MINUTES


async def _user_local(user_id: str, now: datetime) -> tuple[datetime, str]:
    """User's local datetime + 'YYYY-MM-DD' date. Falls back to UTC on any error,
    but LOUDLY: a user with no timezone gets quiet-hours + day boundaries computed
    against UTC, i.e. notifications land at the wrong local clock — that must be
    visible in logs, not silent (CLAUDE.md fail-loud rule)."""

    def _fetch_tz() -> str | None:
        doc = admin_firestore().collection("users").document(user_id).get()
        if doc.exists:
            return (doc.to_dict() or {}).get("timezone")  # None when the field is absent
        return None

    try:
        tz_name = await asyncio.to_thread(_fetch_tz)
        if not tz_name:
            logger.warn("orchestrator: user has no timezone, using UTC (wrong local clock)", {
                "user_id": user_id,
            })
            local = now.astimezone(UTC)
        else:
            local = now.astimezone(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, Exception) as exc:
        logger.warn("orchestrator: timezone resolve failed, using UTC", {
            "user_id": user_id, "error": str(exc),
        })
        local = now.astimezone(UTC)
    return local, local.date().isoformat()


async def _is_preferred_slot(user_id: str, local_now: datetime) -> bool:
    """True when the user's current 30-min slot has an at-or-above-median open rate.

    A user with no learned rates (the flat default) returns True for every slot, so
    smart timing never holds a brand-new user. Lazy import keeps the orchestrator
    decoupled from the signal engine at module load. Fails toward True (send).
    """
    from ..signal_engine.feature_store import read_time_slot_open_rates

    try:
        rates = await read_time_slot_open_rates(user_id)
    except Exception:
        return True
    if not rates:
        return True
    slot_count = len(rates)
    minutes = local_now.hour * 60 + local_now.minute
    slot = min(minutes * slot_count // (24 * 60), slot_count - 1)
    return rates[slot] >= statistics.median(rates)


def _log_drop(proposal: NotificationProposal, reason: str) -> None:
    logger.info("orchestrator: dropped", {
        "user_id": proposal.user_id, "source": proposal.source, "reason": reason,
    })


def _log_hold(proposal: NotificationProposal, reason: str) -> None:
    logger.info("orchestrator: held", {
        "user_id": proposal.user_id, "source": proposal.source, "reason": reason,
    })
