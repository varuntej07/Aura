"""Icebreaker agent — one warm, life-aware opener on ~3 random days a week.

Wraps the existing icebreaker engine in the Agent protocol. The cadence machinery
(rolled days, target hour, atomic per-day claim) and the free-context opener stay
exactly as they were; they just run inside the envelope now.

Mapping to the envelope:
  SENSE   consent + timezone, then the cadence gates (active hours, is-today-a-
          rolled-day, is-this-the-target-hour)
  PLAN    any gate fails -> clean stand-down; else -> act
  ACT     atomically claim today's slot (the idempotency guard), build the free
          context packet, generate ONE opener with its reject gate
  VERIFY  claimed + a send-worthy opener -> OK; not-claimed / no-hook / rejected
          are deliberate POLICY stand-downs (not failures)
  REPAIR  infra -> retry the opener (the claim is idempotent, so a retry that the
          claim already burned simply stands down — the cadence is a ceiling)

There is no degraded delivery: when there is genuinely nothing worth opening
about, silence is correct. ``on_icebreaker_delivered`` (post-send bookkeeping)
stays in ``icebreaker_engine``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ....lib.logger import logger
from ...analytics.funnel_events import (
    NOTIFICATION_ORIGIN_ICEBREAKER,
    PROP_NOTIFICATION_ID,
    PROP_NOTIFICATION_ORIGIN,
)
from ...icebreaker import icebreaker_store as store
from ...icebreaker.context_bundle import build_context_bundle
from ...icebreaker.fields import NOTIFICATION_TYPE_ICEBREAKER
from ...icebreaker.icebreaker_framer import IcebreakerOpener, generate_opener
from ...icebreaker.icebreaker_store import UserTargeting
from ...icebreaker.scheduler_logic import (
    current_week_start_date,
    is_scheduled_today,
    roll_week_dates,
    target_local_hour,
)
from ...model_provider import get_model_provider
from ...notifications.proposal import (
    SOURCE_ICEBREAKER,
    NotificationProposal,
    ProposalKind,
)
from ...signal_engine.scoring import is_within_active_hours
from ..agent import (
    RISK_LOW,
    EvalCase,
    Plan,
    UserContext,
    Verdict,
    VerdictKind,
)
from ..events import EVENT_TICK


@dataclass
class _Inputs:
    eligible: bool
    user_id: str = ""
    reason: str = ""
    targeting: UserTargeting | None = None
    local_now: datetime | None = None
    local_date: str = ""
    week_start: str = ""
    rolled_dates: list[str] | None = None


@dataclass
class _Result:
    ok: bool
    reason: str = ""
    opener: IcebreakerOpener | None = None
    local_date: str = ""
    notification_id: str = ""


class IcebreakerOpenerAgent:
    """Implements the ``Agent`` protocol (see reactive/agent.py)."""

    name = "icebreaker.opener"
    intent = "icebreaker_opener"
    subscribes_to = frozenset({EVENT_TICK})
    risk = RISK_LOW
    allow_degraded_delivery = False

    # ── SENSE ────────────────────────────────────────────────────────────────
    async def sense(self, ctx: UserContext) -> _Inputs:
        targeting = await store.read_user_targeting(ctx.user_id)
        if not targeting.consent_granted:
            return _Inputs(eligible=False, reason="no_consent")

        local_now = _local_now(targeting.timezone)
        if not is_within_active_hours(local_now.hour, local_now.minute):
            return _Inputs(eligible=False, reason="quiet_hours")

        local_date = local_now.date().isoformat()
        week_start = current_week_start_date(local_now)
        rolled_dates = roll_week_dates(ctx.user_id, week_start)

        if not is_scheduled_today(local_date, rolled_dates):
            return _Inputs(eligible=False, reason="not_scheduled")
        if local_now.hour != target_local_hour(ctx.user_id, local_date):
            return _Inputs(eligible=False, reason="not_target_hour")

        return _Inputs(
            eligible=True, user_id=ctx.user_id, targeting=targeting, local_now=local_now,
            local_date=local_date, week_start=week_start, rolled_dates=rolled_dates,
        )

    # ── PLAN ─────────────────────────────────────────────────────────────────
    async def plan(self, inputs: _Inputs) -> Plan:
        if not inputs.eligible:
            return Plan.stand_down(inputs.reason or "ineligible")
        return Plan.act(inputs)

    # ── ACT ──────────────────────────────────────────────────────────────────
    async def act(self, plan: Plan, attempt: int) -> _Result:
        inp = cast(_Inputs, plan.payload)
        # plan only reaches act when eligible, so these are populated.
        assert inp.targeting is not None
        assert inp.local_now is not None and inp.rolled_dates is not None

        # Atomically claim today's slot — the idempotency guard that makes one-per-day
        # safe under overlapping ticks AND envelope retries.
        claim = await store.plan_and_claim_today(
            inp.user_id,
            local_date=inp.local_date,
            week_start_date=inp.week_start,
            rolled_dates=inp.rolled_dates,
        )
        if not claim.claimed:
            if claim.reason == "already_sent_today":
                # The slot was claimed before us. The normal case: the opener was
                # already sent and this is a duplicate tick — stand down. The crash
                # case: our instance claimed the slot, generated the opener, then
                # died before mark_consumed; the re-drain sees the slot taken. Try to
                # recover the stored opener. The funnel's dedup_key prevents double-send
                # if the opener was already delivered before the re-drain arrived.
                recovery = await store.try_recover_pending_opener(inp.user_id, inp.local_date)
                if recovery is not None:
                    logger.info("icebreaker.agent: recovered pending opener after crash-restart", {
                        "user_id": inp.user_id, "local_date": inp.local_date,
                    })
                    return _Result(
                        ok=True,
                        opener=IcebreakerOpener(
                            title=recovery.title,
                            body=recovery.body,
                            opening_chat_message=recovery.opening_chat_message,
                            topic=recovery.topic,
                            reason=recovery.reason,
                            is_send_worthy=True,
                        ),
                        local_date=inp.local_date,
                        notification_id=recovery.notification_id,
                    )
            return _Result(ok=False, reason=claim.reason or "not_claimed")

        context = await build_context_bundle(
            inp.user_id, inp.targeting, inp.local_now, claim.recent_opener_topics
        )
        if not context.has_any_hook():
            return _Result(ok=False, reason="no_hook")

        opener = await generate_opener(get_model_provider(), context)
        if not opener.is_send_worthy:
            return _Result(ok=False, reason=f"rejected:{opener.reason}")

        notification_id = str(uuid.uuid4())

        # Best-effort: persist the opener to the state doc so a crash-then-re-drain
        # can recover it rather than silently losing the day's opener.
        await store.store_pending_opener(
            inp.user_id,
            local_date=inp.local_date,
            title=opener.title,
            body=opener.body,
            opening_chat_message=opener.opening_chat_message,
            topic=opener.topic,
            reason=opener.reason,
            notification_id=notification_id,
        )

        return _Result(
            ok=True, opener=opener, local_date=inp.local_date,
            notification_id=notification_id,
        )

    # ── VERIFY ───────────────────────────────────────────────────────────────
    async def verify(self, raw: _Result) -> Verdict:
        if raw.ok:
            return Verdict.ok()
        # not_claimed / no_hook / rejected are all deliberate no-gos, not failures.
        return Verdict.policy(raw.reason)

    # ── REPAIR ───────────────────────────────────────────────────────────────
    async def repair(self, verdict: Verdict, plan: Plan, attempt: int) -> Plan | None:
        if verdict.kind == VerdictKind.INFRA:
            return Plan.act(plan.payload)  # retry; the claim is idempotent
        return None

    async def degraded(self, plan: Plan) -> _Result | None:
        return None

    # ── TO PROPOSAL ──────────────────────────────────────────────────────────
    def to_proposal(self, raw: _Result) -> NotificationProposal | None:
        if not raw.ok or raw.opener is None:
            return None
        opener = raw.opener
        return NotificationProposal(
            user_id="",  # the orchestrator fills user_id from context
            source=SOURCE_ICEBREAKER,
            kind=ProposalKind.PROACTIVE,
            dedup_key=f"icebreaker_{raw.local_date}",
            title=opener.title,
            body=opener.body,
            data={
                PROP_NOTIFICATION_ID: raw.notification_id,
                PROP_NOTIFICATION_ORIGIN: NOTIFICATION_ORIGIN_ICEBREAKER,
                "opening_chat_message": opener.opening_chat_message,
                "topic": opener.topic,
                "reason": opener.reason,
                "notification_reason": opener.reason,
            },
            notification_type=NOTIFICATION_TYPE_ICEBREAKER,
            collapse_key=f"icebreaker_{raw.local_date}",
        )

    def eval_cases(self) -> list[EvalCase]:
        return []


def _local_now(timezone_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(timezone_name))
    except (ZoneInfoNotFoundError, Exception):
        return datetime.now(UTC)
