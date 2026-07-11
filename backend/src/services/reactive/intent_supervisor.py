"""The intent supervisor — fires due pending intents onto the event bus.

Runs on the per-minute scheduler tick (like the reminder scan). It atomically claims
pending intents whose ``fire_at`` has passed and emits one ``intent_due`` event each,
so the orchestrator wakes the follow-up agent. The claim is what makes firing
race-free: two overlapping ticks can never double-fire the same intent.

This is the "fire" half of the revocable-intent contract; ``reconcile`` is the
"invalidate" half. An intent that was cancelled before this sweep is simply no longer
pending, so it is never claimed — the cancel always wins a clean race.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ...lib.logger import logger
from . import event_bus, intent_store


async def run_intent_sweep(*, now: datetime | None = None) -> int:
    """Claim every due intent and trigger the orchestrator for each user. Returns the
    number fired. Cheap when nothing is due (one indexed query). Bounded by the
    store's claim limit, so a backlog drains over successive ticks.

    The ``intent_due`` outbox event is staged atomically with the pending→fired status
    flip inside ``claim_due_intents``, so there is no window where an intent is marked
    FIRED but has no outbox event. The dispatch_inline calls below are a best-effort
    latency optimisation; the per-minute outbox relay is the durable backstop."""
    when = now or datetime.now(UTC)
    claimed = await intent_store.claim_due_intents(now=when)
    if not claimed:
        return 0

    woken: set[str] = set()
    for intent in claimed:
        woken.add(intent.uid)

    for uid in woken:
        await event_bus.dispatch_inline(uid)

    logger.info("intent_supervisor: due intents fired", {
        "fired": len(claimed), "users": len(woken),
    })
    return len(claimed)
