"""RECONCILE — the "best friend" step: does a new event invalidate a pending intent?

When the user says "mom is home and fine," the queued surgery follow-up must NOT
fire. A resolution arrives as a ``life_update`` event carrying ``resolved_subjects``
(closed-set slugs the classifier matched against the user's OPEN intents). This step
cancels each matching pending intent and writes its tombstone, so the scheduled
action is revoked before it fires — and cannot be re-created by a late duplicate.

Routing resolution through the durable event bus (rather than cancelling inline in
the chat path) makes the cancel retryable: a transient failure leaves the event
unconsumed and the next sweep retries, so "mom is fine" reliably wins the race
against the queued follow-up.
"""

from __future__ import annotations

from ...lib.logger import logger
from . import intent_store
from .events import EVENT_LIFE_UPDATE, Event


async def reconcile(user_id: str, events: list[Event]) -> set[str]:
    """Cancel pending intents the given events resolve. Returns the SET of resolved
    subjects, so the orchestrator can also suppress a same-batch ``intent_due`` whose
    subject was just resolved (the resolution-races-the-fire window). A tombstone is
    written for every resolved subject even if no pending intent remained, so a
    just-fired intent cannot be re-created."""
    subjects: list[str] = []
    for event in events:
        if event.type != EVENT_LIFE_UPDATE:
            continue
        raw = event.payload.get("resolved_subjects") or []
        if isinstance(raw, list):
            subjects.extend(s for s in raw if isinstance(s, str) and s.strip())

    if not subjects:
        return set()

    resolved: set[str] = set()
    cancelled = 0
    for subject in dict.fromkeys(subjects):  # de-dup, preserve order
        if await intent_store.cancel_pending_by_subject(
            user_id, subject, reason="resolved via life_update"
        ):
            cancelled += 1
        resolved.add(subject)

    logger.info("reconcile: subjects resolved", {
        "user_id": user_id, "resolved": len(resolved), "cancelled_pending": cancelled,
    })
    return resolved
