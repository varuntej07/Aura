"""Frame-at-delivery dispatch — LLM copy generation paid only on a send attempt.

A producer whose enqueue cadence exceeds its delivery cadence (news enqueues up
to once per 4h scoring tick while the adaptive budget allows far fewer sends)
submits its proposal with ``deferred_framing`` set and ``title``/``body`` empty.
The drain calls ``frame_winner`` on the arbitration winner AFTER every
non-LLM gate (quiet hours, presence, smart timing, budget precheck) has passed,
so copy is generated once per genuine send attempt instead of once per enqueue.

Like ``post_send``, this module keeps the funnel's dependency direction: the
orchestrator depends on this thin dispatcher, and the producer's framer is
lazy-imported per source. It never raises into the drain — any unexpected
failure is reported as UNAVAILABLE (infra, not a verdict), which the drain
answers with a HOLD and a next-minute retry.
"""

from __future__ import annotations

from ...lib.logger import logger
from .proposal import SOURCE_NEWS, NotificationProposal

# Verdicts frame_winner can reach. FRAMED mutated the proposal in place (copy,
# data payload, dedup_key, decision fields all filled). REJECTED is terminal for
# this proposal: no candidate in its payload survived the producer's relevance
# gate. UNAVAILABLE is infra (LLM outage/timeout): nothing was judged, hold and
# retry.
FRAMED = "framed"
REJECTED = "rejected"
UNAVAILABLE = "unavailable"


async def frame_winner(proposal: NotificationProposal) -> str:
    """Run the source's delivery-time framer on an arbitration winner."""
    try:
        if proposal.source == SOURCE_NEWS:
            from ..signal_engine.notification_framer import (
                frame_news_proposal_at_delivery,
            )

            return await frame_news_proposal_at_delivery(proposal)
        # A deferred proposal from a source with no registered delivery framer
        # cannot be sent (its copy is empty). Fail CLOSED and loudly: this is a
        # producer wiring bug, not a transient condition, so a retry would spin.
        logger.error("delivery framing: no framer registered for source", {
            "user_id": proposal.user_id, "source": proposal.source,
        })
        return REJECTED
    except Exception as exc:
        logger.error("delivery framing: framer crashed (holding for retry)", {
            "user_id": proposal.user_id,
            "source": proposal.source,
            "error": str(exc),
        })
        return UNAVAILABLE
