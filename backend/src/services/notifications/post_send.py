"""Post-send dispatch — the producer-specific bookkeeping that must run ONLY on a
real delivery.

In the funnel model the actual proactive send happens in the drain, not in the
producer's tick, so the "I sent it, now record it" side effects (a thread's
follow-up count, an icebreaker's anti-repeat memory, the signal engine's learning
outcome + funnel event) can no longer sit inline with the producer. The drain calls
``dispatch_post_send`` after delivering the winner; each handler reads what it needs
from ``proposal.data`` / ``proposal.decision``.

Lazy per-source imports keep the orchestrator free of a producer import cycle. This
NEVER raises into the drain — a bookkeeping failure must not break delivery.
"""

from __future__ import annotations

from ...lib.logger import logger
from ..notification_service import NotificationResult
from .proposal import (
    SOURCE_ICEBREAKER,
    SOURCE_NEWS,
    SOURCE_REENGAGE,
    SOURCE_THREAD,
    NotificationProposal,
)


async def dispatch_post_send(
    proposal: NotificationProposal, result: NotificationResult
) -> None:
    """Run the source's post-delivery bookkeeping. Best-effort, never raises."""
    try:
        if proposal.source == SOURCE_THREAD:
            from ..threads.thread_reflector import on_thread_delivered
            await on_thread_delivered(proposal, result)
        elif proposal.source == SOURCE_ICEBREAKER:
            from ..icebreaker.icebreaker_engine import on_icebreaker_delivered
            await on_icebreaker_delivered(proposal, result)
        elif proposal.source == SOURCE_NEWS:
            # Wired when the signal-engine cutover lands (Increment 2b).
            from ..signal_engine.scoring_loop import on_news_delivered
            await on_news_delivered(proposal, result)
        elif proposal.source == SOURCE_REENGAGE:
            # Wired when the dormancy re-engagement producer lands (Increment 2c).
            from ..reengagement.reengagement_engine import on_reengage_delivered
            await on_reengage_delivered(proposal, result)
    except ImportError:
        # A source whose handler isn't built yet (news/reengage pre-cutover) simply
        # has no post-send bookkeeping — that's expected, not an error.
        pass
    except Exception as exc:
        logger.warn("post_send: dispatch failed", {
            "source": proposal.source,
            "user_id": proposal.user_id,
            "error": str(exc),
        })
