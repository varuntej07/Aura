"""The Self-Heal Envelope — Claude Code's tool-calling resilience, generalized.

Every agent runs inside this contract: SENSE -> PLAN -> ACT -> VERIFY, and on a
non-OK VERDICT, a BOUNDED REPAIR loop that ends in OK, a clean stand-down, or a
loud ESCALATION with an optional graceful degraded output. A verdict (not an
exception) is the trigger, so a Gemini timeout AND an empty-but-successful fetch
both route to recovery instead of to silence.

Recovery is hybrid and bounded:
  * deterministic ladders for infra/empty (retry, broaden, switch source) — the
    AGENT decides what each repair does; the envelope just bounds the loop;
  * an LLM re-plan for low-quality, capped SEPARATELY and more tightly than the
    total repair budget (Anthropic: "agents struggle to judge appropriate effort");
  * a concrete integer cap on total repairs so the loop can never spiral, ending
    in a controlled degraded output + a fail-loud signal, never an infinite retry.

The model layer already does its own HTTP retry + model fallback, so these caps
are a THIN top layer (no double-counting the model's internal 2-3 retries).

The envelope PRODUCES a proposal; it never delivers. Delivery is the funnel's job
(P1 caller) / the orchestrator's Delivery Arbiter (P2). Keeping produce and
deliver separate is what lets P2 collect proposals before routing them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from ...lib.logger import logger
from . import agent_runs
from .agent import Agent, UserContext, Verdict, VerdictKind
from .idempotency import idempotent

if TYPE_CHECKING:
    from ..notifications.proposal import NotificationProposal

# Bounded recovery. Total repairs is the hard ceiling; LLM re-plans are capped
# tighter because they are the expensive, judgment-heavy path.
TOTAL_REPAIR_CAP = 2
LLM_REPLAN_CAP = 1

# Terminal statuses (single source of truth for logs + callers + agent_runs).
STATUS_OK = "ok"
STATUS_RECOVERED = "recovered"                  # OK, but only after >=1 repair
STATUS_SKIPPED = "skipped"                       # plan said no action (the common case)
STATUS_DUPLICATE = "duplicate"                   # idempotency claim lost (already done)
STATUS_STOOD_DOWN = "stood_down"                 # a deliberate policy no-go
STATUS_ESCALATED = "escalated"                   # caps reached, fail-loud, nothing shipped
STATUS_ESCALATED_DEGRADED = "escalated_degraded"  # caps reached, a degraded output shipped


@dataclass
class EnvelopeOutcome:
    agent: str
    user_id: str
    status: str
    proposal: NotificationProposal | None = None
    reason: str = ""
    attempts: int = 0
    repairs: int = 0
    replans: int = 0

    @property
    def has_proposal(self) -> bool:
        return self.proposal is not None


async def run_agent(
    agent: Agent, ctx: UserContext, *, now: datetime | None = None
) -> EnvelopeOutcome:
    """Run one agent through the envelope. Never raises — every failure path ends
    in a recorded, logged outcome."""
    when = now or ctx.now
    trace = agent_runs.AgentRunTrace(
        run_id=uuid.uuid4().hex,
        agent=agent.name,
        intent=agent.intent,
        user_id=ctx.user_id,
        event_type=ctx.event.type if ctx.event else "",
    )

    # ── SENSE + PLAN ─────────────────────────────────────────────────────────
    try:
        inputs = await agent.sense(ctx)
        plan = await agent.plan(inputs)
    except Exception as exc:
        trace.add_step("plan_error", error=str(exc))
        return await _finish(
            trace,
            EnvelopeOutcome(agent.name, ctx.user_id, STATUS_ESCALATED, reason=f"plan_error:{exc}"),
            when, loud=True,
        )

    if not plan.act_needed:
        trace.add_step("stand_down", reason=plan.stand_down_reason)
        return await _finish(
            trace,
            EnvelopeOutcome(agent.name, ctx.user_id, STATUS_SKIPPED, reason=plan.stand_down_reason),
            when,
        )

    # Idempotency claim BEFORE a side-effecting act (the shared primitive). A lost
    # claim means a duplicate delivery already did the work — skip.
    if plan.idempotency_key:
        if not await idempotent(plan.idempotency_key, scope=ctx.user_id):
            trace.add_step("duplicate", key=plan.idempotency_key)
            return await _finish(
                trace,
                EnvelopeOutcome(
                    agent.name, ctx.user_id, STATUS_DUPLICATE, reason="idempotent_skip",
                ),
                when,
            )

    def _proposal(raw: object) -> NotificationProposal | None:
        # Centralize user_id stamping so every agent's to_proposal can stay
        # context-free (it returns the proposal with an empty user_id).
        proposal = agent.to_proposal(raw)
        if proposal is not None:
            proposal.user_id = ctx.user_id
        return proposal

    # ── ACT -> VERIFY -> REPAIR (bounded) ────────────────────────────────────
    attempts = repairs = replans = 0
    last_raw = None
    last_verdict: Verdict | None = None

    while True:
        attempts += 1
        try:
            raw = await agent.act(plan, attempts)
            verdict = await agent.verify(raw)
            last_raw = raw
        except Exception as exc:
            # Verdict, not exception, is the trigger — a raised act becomes INFRA.
            verdict = Verdict.infra("act_raised", detail=str(exc))
        last_verdict = verdict
        trace.add_step(
            "act", attempt=attempts, verdict=str(verdict.kind),
            reason=verdict.reason, detail=verdict.detail[:200],
        )

        if verdict.is_ok:
            status = STATUS_RECOVERED if repairs > 0 else STATUS_OK
            return await _finish(
                trace,
                EnvelopeOutcome(
                    agent.name, ctx.user_id, status, proposal=_proposal(last_raw),
                    reason="ok", attempts=attempts, repairs=repairs, replans=replans,
                ),
                when,
            )

        # A deliberate no-go: stand down cleanly, do NOT escalate.
        if verdict.kind == VerdictKind.POLICY:
            trace.add_step("policy_standdown", reason=verdict.reason)
            return await _finish(
                trace,
                EnvelopeOutcome(
                    agent.name, ctx.user_id, STATUS_STOOD_DOWN, reason=verdict.reason,
                    attempts=attempts, repairs=repairs, replans=replans,
                ),
                when,
            )

        # Bounded recovery, with a tighter cap on the expensive LLM re-plan path.
        is_replan = verdict.kind == VerdictKind.LOW_QUALITY
        if repairs >= TOTAL_REPAIR_CAP or (is_replan and replans >= LLM_REPLAN_CAP):
            break  # escalate

        new_plan = await agent.repair(verdict, plan, attempts)
        if new_plan is None:
            trace.add_step("repair_gaveup", verdict=str(verdict.kind), reason=verdict.reason)
            break
        repairs += 1
        if is_replan:
            replans += 1
        plan = new_plan
        trace.add_step("repair", repairs=repairs, replans=replans, verdict=str(verdict.kind))

    # ── ESCALATE (caps reached or repair gave up) ────────────────────────────
    degraded_raw = None
    if agent.allow_degraded_delivery:
        try:
            degraded_raw = await agent.degraded(plan)
        except Exception as exc:
            trace.add_step("degraded_error", error=str(exc))

    reason = last_verdict.reason if last_verdict else "cap_reached"
    if degraded_raw is not None:
        trace.add_step("escalate_degraded", verdict=str(last_verdict.kind) if last_verdict else "")
        return await _finish(
            trace,
            EnvelopeOutcome(
                agent.name, ctx.user_id, STATUS_ESCALATED_DEGRADED,
                proposal=_proposal(degraded_raw), reason=reason,
                attempts=attempts, repairs=repairs, replans=replans,
            ),
            when, loud=True,
        )

    trace.add_step("escalate", verdict=str(last_verdict.kind) if last_verdict else "")
    return await _finish(
        trace,
        EnvelopeOutcome(
            agent.name, ctx.user_id, STATUS_ESCALATED, reason=reason,
            attempts=attempts, repairs=repairs, replans=replans,
        ),
        when, loud=True,
    )


async def _finish(
    trace: agent_runs.AgentRunTrace,
    outcome: EnvelopeOutcome,
    when: datetime,
    *,
    loud: bool = False,
) -> EnvelopeOutcome:
    if loud:
        logger.error("envelope: ESCALATED (self-heal cap reached, fail-loud)", {
            "agent": outcome.agent,
            "user_id": outcome.user_id,
            "status": outcome.status,
            "reason": outcome.reason,
            "attempts": outcome.attempts,
            "repairs": outcome.repairs,
            "replans": outcome.replans,
        })
    await agent_runs.record_run(
        trace,
        status=outcome.status,
        reason=outcome.reason,
        attempts=outcome.attempts,
        repairs=outcome.repairs,
        replans=outcome.replans,
        delivered=None,  # the funnel records the real delivery; the envelope only produces
        now=when,
    )
    return outcome
