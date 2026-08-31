"""The per-user reactive orchestrator (Buddy Brain).

Woken by a coalesced ``/internal/orchestrate`` Cloud Task (one per user, however
many events are pending). The pipeline (§4.2):

  [LEASE]      claim the per-user single-flight lease (drop if another pass holds it)
  [LOAD/DRAIN] read the user's unconsumed events in ONE pass (coalesce)
  [CONSUME]    idempotently claim each event (crash/duplicate safety)
  [RECONCILE]  invalidate pending intents a new event obsoletes (P3)
  [DECIDE]     deterministic policy table: events -> agent tasks (+ LLM escape hatch, P5)
  [DISPATCH]   run each agent inside the Self-Heal Envelope, collect proposals
  [GUARD]      input/output guardrails (P5)
  [ROUTE]      hand proposals to the funnel (the surface-aware Delivery Arbiter)
  [PERSIST]    mark events consumed; every decision already logged loud

The orchestrator never raises into the Cloud Task handler — every failure path
returns a logged summary. Duplicate dispatch is prevented by the idempotent
per-event consume, not the lease (the lease only avoids wasted concurrent work).
"""

from __future__ import annotations

from ...lib.logger import logger
from ..analytics.llm_telemetry import bind_llm_user
from ..notifications import orchestrator as funnel
from ..notifications.proposal import SOURCE_FOLLOWUP
from ..model_provider import capture_usage
from ..notifications.queue_store import drop_if_active, proposal_id_for
from ..user_aura_extractor import _user_has_granted_aura_consent
from . import cost_cap, guardrails, inbox, lease, policy, reconcile
from .agent import UserContext
from .envelope import run_agent
from .events import EVENT_INTENT_DUE, Event
from .intent_store import subject_id as _subject_id
from .registry import get_agent_registry


async def run_orchestrate(
    user_id: str,
    *,
    transient_events: list[Event] | None = None,
) -> dict[str, object]:
    """Run one orchestrate pass for a user. Coalesces the user's pending events,
    reconciles, decides, dispatches agents, and routes proposals to the funnel.

    ``transient_events`` carries durable Cloud Task payloads such as the hourly
    clock tick. They intentionally bypass the Firestore inbox because Cloud Tasks
    already owns their retry lifecycle.
    """
    token = await lease.acquire(user_id)
    if token is None:
        logger.info("orchestrate: lease held, dropping (events stay for the holder)", {
            "user_id": user_id,
        })
        return {"skipped": "lease_held"}

    try:
        # Attribute every LLM call this pass makes to this user, so background agent
        # spend lands in the per-user ledger the same way chat already does.
        with bind_llm_user(user_id):
            return await _drain_and_dispatch(
                user_id,
                transient_events=transient_events,
            )
    except Exception as exc:
        logger.exception("orchestrate: unhandled error", {"user_id": user_id, "error": str(exc)})
        return {"error": str(exc)}
    finally:
        await lease.release(user_id, token)


async def _drain_and_dispatch(
    user_id: str,
    *,
    transient_events: list[Event] | None = None,
) -> dict[str, object]:
    drained = [] if transient_events is not None else await inbox.drain(user_id)
    refs = [ref for ref, _ in drained]
    events = [event for _, event in drained]
    events.extend(transient_events or [])
    if not events:
        return {"events": 0}

    # Dispatch is at-least-once: a crash before mark_consumed re-drains these events
    # next sweep. We do NOT claim-then-skip per event — that would LOSE a notification
    # if a pass died mid-dispatch. Instead, double-dispatch is absorbed downstream: the
    # funnel collapses re-submits on the deterministic dedup_key (one send), and any
    # side-effecting agent act is guarded by its own idempotency key inside the envelope.

    # RECONCILE: a new event may invalidate a pending intent ("mom is fine"
    # cancels the queued surgery follow-up).
    resolved_subjects = await reconcile.reconcile(user_id, events)

    # PURGE QUEUED FOLLOWUPS: the same-batch race guard (below) only catches
    # intent_due events in the SAME drain as the life_update. If the followup was
    # enqueued in a previous drain, it still sits in notification_queue as pending/
    # held. Drop it now, before the proactive drain can send it. The funnel's
    # dedup_key is the last-resort guard if this drop loses a race with a send.
    if resolved_subjects:
        for subj in resolved_subjects:
            pid = proposal_id_for(SOURCE_FOLLOWUP, f"followup_{_subject_id(subj)}")
            if await drop_if_active(user_id, pid):
                logger.info("orchestrate: dropped queued followup after resolution", {
                    "user_id": user_id, "subject": subj,
                })

    # DECIDE: deterministic policy table (subscription map in P2).
    registry = get_agent_registry()
    tasks = policy.decide(events, registry)

    # Same-batch race: if the resolution arrived in the same drain as the intent's
    # own due-fire, suppress that follow-up — don't ask "how did the surgery go?" right
    # after "mom is fine". (The tombstone already blocks re-creation; this blocks send.)
    if resolved_subjects:
        tasks = [
            task for task in tasks
            if not (
                task.event.type == EVENT_INTENT_DUE
                and task.event.payload.get("subject") in resolved_subjects
            )
        ]

    # CONSENT: agents whose output is built from remembered personal data cannot run
    # without aura_consent_granted. Filtered HERE, once per pass, instead of inside
    # each agent's sense(): a non-consenting user previously cost one Cloud Task, one
    # orchestrate pass, N agent dispatches, N duplicate user-doc reads, and an
    # LLM-budget debit — all to reach a `no_consent` stand-down. In the 14-day
    # production window that was the single largest stand-down reason (177 of 400).
    # Consent is read only if some selected agent actually needs it.
    if tasks and any(getattr(task.agent, "requires_aura_consent", False) for task in tasks):
        if not await _user_has_granted_aura_consent(user_id):
            gated = [
                task for task in tasks
                if not getattr(task.agent, "requires_aura_consent", False)
            ]
            logger.info("orchestrate: aura consent absent, skipping memory-backed agents", {
                "user_id": user_id,
                "skipped": [
                    task.agent.name for task in tasks if task not in gated
                ],
            })
            tasks = gated

    # COST CAP: the runaway guard gates only the LLM-bearing dispatch — reconcile
    # (invalidation) already ran above, so "mom is fine" still cancels its follow-up
    # even on a user who has hit the ceiling. Fails open.
    if tasks and not await cost_cap.within_daily_budget(user_id):
        await inbox.mark_consumed(user_id, refs)
        logger.warn("orchestrate: over daily LLM budget, dispatch skipped", {
            "user_id": user_id, "events": len(events), "reconciled": len(resolved_subjects),
        })
        return {"events": len(events), "reconciled": len(resolved_subjects), "skipped": "cost_cap"}

    # DISPATCH: run each agent inside the envelope, collect produced proposals.
    # capture_usage() records one entry per real provider ATTEMPT made anywhere inside
    # the block, threading through the retry and cross-provider fallback path without
    # touching any signature (model_provider.py:53-83). Failed attempts emit too, which
    # is the correct direction for a runaway breaker: a fallback chain that burned three
    # providers really did make three API calls.
    proposals = []
    with capture_usage() as llm_attempts:
        for task in tasks:
            ctx = UserContext(user_id=user_id, event=task.event)
            outcome = await run_agent(task.agent, ctx)
            if outcome.has_proposal and outcome.proposal is not None:
                proposals.append(outcome.proposal)

    # Debit REAL provider attempts, not dispatched agents. The old proxy charged once per
    # dispatched agent whether or not it called an LLM, and the agents stand down without
    # one on the overwhelming majority of ticks: measured in production over 14 days, the
    # icebreaker made 6 real calls while being debited ~500 times, and intent.followup
    # makes no LLM call at all yet was charged every time. A breaker fed idle dispatches
    # measures the opposite of the runaway it exists to catch.
    if llm_attempts:
        await cost_cap.record_llm_call(user_id, n=len(llm_attempts))

    # GUARD: deterministic output safety floor — drop a proposal whose copy is empty,
    # over-length, or leaked an unrendered template / refusal (the framing step broke).
    # The funnel's tap-gate is the separate LLM quality bar; this is the safety bar.
    proposals = guardrails.filter_proposals(proposals)

    # ROUTE: hand proposals to the funnel (the surface-aware Delivery Arbiter).
    routed = 0
    routing_failed = False
    for proposal in proposals:
        try:
            await funnel.submit(proposal)
            routed += 1
        except Exception as exc:
            routing_failed = True
            logger.warn("orchestrate: funnel submit failed", {
                "user_id": user_id, "source": proposal.source, "error": str(exc),
            })

    # PERSIST: consume only after every proposal was durably accepted. A failed
    # funnel handoff leaves the source events available for the next sweep.
    if not routing_failed:
        await inbox.mark_consumed(user_id, refs)
    else:
        logger.warn(
            "orchestrate: events retained for retry after routing failure",
            {"user_id": user_id, "event_count": len(refs)},
        )

    logger.info("orchestrate: dispatched", {
        "user_id": user_id,
        "events": len(events),
        "reconciled": len(resolved_subjects),
        "tasks": len(tasks),
        "routed": routed,
    })
    return {
        "events": len(events),
        "reconciled": len(resolved_subjects),
        "tasks": len(tasks),
        "routed": routed,
        "retry_pending": routing_failed,
    }
