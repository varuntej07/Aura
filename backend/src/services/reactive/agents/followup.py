"""Scheduled follow-up agent — delivers a fired pending intent.

When a pending intent's ``fire_at`` passes, the supervisor emits an ``intent_due``
event and the orchestrator dispatches this agent. The intent already carries a warm,
ready-to-send question (framed at schedule time by the closed-set sensor), so this
agent makes NO LLM call at fire time — the emotionally load-bearing send ("how did
your mom's surgery go?") is therefore bulletproof: if the intent survived to fire, it
sends. There is nothing to self-heal and nothing to degrade.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...notifications.proposal import (
    SOURCE_FOLLOWUP,
    NotificationProposal,
    ProposalKind,
)
from ..agent import (
    RISK_LOW,
    EvalCase,
    Plan,
    UserContext,
    Verdict,
)
from ..events import EVENT_INTENT_DUE
from ..intent_store import subject_id

NOTIFICATION_TYPE_FOLLOWUP = "intent_followup"


@dataclass
class _Inputs:
    subject: str = ""
    question: str = ""


@dataclass
class _Result:
    subject: str = ""
    question: str = ""


class ScheduledFollowUpAgent:
    """Implements the ``Agent`` protocol (see reactive/agent.py)."""

    name = "intent.followup"
    intent = "scheduled_followup"
    subscribes_to = frozenset({EVENT_INTENT_DUE})
    risk = RISK_LOW
    allow_degraded_delivery = False

    async def sense(self, ctx: UserContext) -> _Inputs:
        payload = ctx.event.payload if ctx.event is not None else {}
        return _Inputs(
            subject=str(payload.get("subject", "")),
            question=str(payload.get("question", "")),
        )

    async def plan(self, inputs: _Inputs) -> Plan:
        if not inputs.question.strip():
            return Plan.stand_down("no_question")
        return Plan.act(inputs)

    async def act(self, plan: Plan, attempt: int) -> _Result:
        inp: _Inputs = plan.payload
        return _Result(subject=inp.subject, question=inp.question)

    async def verify(self, raw: _Result) -> Verdict:
        if raw.question.strip():
            return Verdict.ok()
        return Verdict.policy("empty_question")

    async def repair(self, verdict: Verdict, plan: Plan, attempt: int) -> Plan | None:
        return None

    async def degraded(self, plan: Plan) -> _Result | None:
        return None

    def to_proposal(self, raw: _Result) -> NotificationProposal | None:
        if not raw.question.strip():
            return None
        return NotificationProposal(
            user_id="",  # the orchestrator fills user_id from context
            source=SOURCE_FOLLOWUP,
            kind=ProposalKind.PROACTIVE,
            dedup_key=f"followup_{subject_id(raw.subject)}",
            title="Buddy",
            body=raw.question,
            data={
                "notification_type": NOTIFICATION_TYPE_FOLLOWUP,
                "subject": raw.subject,
                # Buddy-facing "why I reached out" so a tap into chat stays oriented.
                "notification_reason": (
                    f"You scheduled a warm follow-up about \"{raw.subject}\" because you said "
                    "you'd check back on it. Pick it up naturally and caringly."
                ),
                "opening_chat_message": raw.question,
            },
            notification_type=NOTIFICATION_TYPE_FOLLOWUP,
            collapse_key=f"followup_{subject_id(raw.subject)}",
        )

    def eval_cases(self) -> list[EvalCase]:
        return []
