"""Central notification orchestration.

Every push notification in Aura is produced by one of seven *producers*
(reminders, tracking, calendar, threads, briefing, icebreaker, news) and routed
through the single entrypoint ``orchestrator.submit``. The orchestrator is the
ONLY code that calls ``notification_service.send_notification`` (the FCM choke
point), so freshness, cross-agent dedup, quiet hours, the daily budget, and
priority arbitration live in exactly one place instead of being copied (or
missing) inside each producer.

Design + rationale: ``SIGNAL_ENGINE_ARCHITECTURE.md`` at the repo root.

Two lanes, decided by ``ProposalKind`` on the proposal:
  * COMMITTED (reminder / tracking / calendar / briefing) — the user asked for
    it, so it is sent INLINE the moment it is submitted (time-exact). It still
    passes the freshness + dedup gates, and it records to the budget so a later
    proactive push is spaced away from it.
  * PROACTIVE (thread / icebreaker / news) — engine-initiated. It is enqueued to
    ``users/{uid}/notification_queue`` and the per-minute drain on
    ``/scheduler/tick`` arbitrates across everything pending for that user,
    sending at most the highest-priority one and holding the rest for a later
    window.
"""

from __future__ import annotations
