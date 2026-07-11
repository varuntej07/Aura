"""Curiosity agent — the first capability wrapped in the Self-Heal Envelope.

A *thread* is a hole in what Buddy knows about the user's life; this agent picks
the most natural open loop and asks ONE warm, curious question about it (never a
"did you finish?" audit). It is the existing thread engine, re-housed in the agent
protocol so it gets bounded self-heal + agent_runs tracing for free.

Mapping to the envelope:
  SENSE   load consent + timezone + open threads + daily-cap, pick the thread
  PLAN    eligible -> act; any gate (no consent / quiet hours / daily cap / no
          open thread) -> a clean stand-down (NOT a failure)
  ACT     one LLM call to frame the question (raises on a real LLM outage so the
          envelope sees it, instead of swallowing it like the old framer did)
  VERIFY  usable? non-empty body, >=2 replies, and NOT in the accountability voice
          the prompt forbids ("did you finish") -> that is LOW_QUALITY, re-plan
  REPAIR  infra -> retry the frame; low_quality -> re-frame with a stronger
          anti-accountability nudge (one LLM re-plan, capped by the envelope)
  DEGRADE on escalate, ship the deterministic safe-fallback question (a warm
          generic beats silence)

The pure selection (``select_thread_to_follow_up``), the Buddy-facing reason, and
the post-send bookkeeping (``on_thread_delivered``) stay in ``thread_reflector``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel

from ....lib.logger import logger
from ...analytics.funnel_events import NOTIFICATION_ORIGIN_THREAD_ENGINE
from ...firebase import admin_firestore
from ...model_provider import get_model_provider
from ...notifications.proposal import (
    SOURCE_THREAD,
    NotificationProposal,
    ProposalKind,
)
from ...signal_engine.scoring import is_within_active_hours
from ...threads import thread_store
from ...threads.models import Thread
from ...threads.thread_framer import (
    _FRAMER_SYSTEM_PROMPT,
    FollowUpFramingContext,
    FramedFollowUp,
    _build_prompt,
    _normalise,
    _safe_fallback,
)
from ...threads.thread_reflector import (
    NOTIFICATION_TYPE_THREAD_FOLLOW_UP,
    THREAD_DAILY_CAP,
    _build_thread_reason,
    select_thread_to_follow_up,
)
from ...user_aura_schema import top_interest_subjects
from ..agent import (
    RISK_LOW,
    EvalCase,
    Plan,
    UserContext,
    Verdict,
    VerdictKind,
)
from ..events import EVENT_REMINDER_CREATED, EVENT_TICK

# The accountability voice the framer prompt forbids. If the model slips into it,
# VERIFY fails it LOW_QUALITY and the envelope re-plans once with a stronger nudge.
# A semantic judge (below) replaces a fixed phrase list, which can never have full
# coverage — "how's calling your mom going" is the exact accountability check this
# prompt bans, phrased in a way a hardcoded blocklist missed.
_ACCOUNTABILITY_JUDGE_TIMEOUT_S = 6.0


class _AccountabilityVoiceJudgment(BaseModel):
    is_accountability_voice: bool
    reason: str = ""


_ACCOUNTABILITY_JUDGE_SYSTEM = """\
You are a tone gate for a Buddy curiosity notification. Buddy is a close friend who
remembered something and is genuinely curious — never a coach, never checking up on
whether a task got done.

Return ONLY JSON: {"is_accountability_voice": true or false, "reason": "<=8 words"}

Flag true when the message reads as checking whether the person finished, completed,
did, or is keeping up with something — any phrasing, however worded, that puts them
on the spot about progress or follow-through (e.g. "did you get to it", "how's X
coming along", "still need to X?", "on top of X?").

Flag false when the message is purely curious about what the thing IS, who it's for,
or how the person feels about it, with no progress-checking undertone.

Examples:
"how's calling your mom going?" -> true (progress-check on a task, reads as nagging)
"what's the bank thing about, sorting the lease?" -> false (curious about the subject)
"you finish reviewing the doc yet?" -> true (explicit accountability)
"what's the presentation on, you feeling ready?" -> false (curious about content/feelings)

Be BALANCED: only flag true when a reasonable friend would read it as checking up,
not merely mentioning that a task exists."""


async def _judge_accountability_voice(body: str) -> tuple[bool, str]:
    """Fails OPEN toward Verdict.ok() (ship it) on any error/timeout — no worse
    than the fixed phrase list it replaces (an outage there already meant a rare
    bad phrasing could slip through), and consistent with this codebase's
    universal "infra failure never silences a send" rule."""
    try:
        result = await asyncio.wait_for(
            get_model_provider().cheap(
                f'Notification body: "{body}"\n\nIs this accountability voice?',
                system=_ACCOUNTABILITY_JUDGE_SYSTEM,
                response_model=_AccountabilityVoiceJudgment,
                temperature=0.0,
            ),
            timeout=_ACCOUNTABILITY_JUDGE_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warn("curiosity.agent: accountability judge unavailable, failing open (ship)", {
            "error": str(exc),
        })
        return False, "judge_unavailable"
    judgment = cast(_AccountabilityVoiceJudgment, result)
    return bool(judgment.is_accountability_voice), (judgment.reason or "").strip()[:60]


_REPLAN_NUDGE = (
    "\nSTRICT: your previous attempt slipped into checking whether they did or "
    "finished the task. Do NOT mention finishing, completing, doing, or keeping up "
    "with it. Ask purely out of curiosity about what the thing IS or how they feel "
    "about it."
)


@dataclass
class _Inputs:
    eligible: bool
    reason: str = ""
    thread: Thread | None = None
    framing_ctx: FollowUpFramingContext | None = None
    local_date: str = ""


@dataclass
class _CuriosityPlan:
    thread: Thread
    framing_ctx: FollowUpFramingContext
    local_date: str
    extra_instruction: str = ""


@dataclass
class _Result:
    thread: Thread
    framed: FramedFollowUp
    local_date: str


class CuriosityThreadFollowUpAgent:
    """Implements the ``Agent`` protocol (see reactive/agent.py)."""

    name = "curiosity.thread_followup"
    intent = "curiosity_followup"
    subscribes_to = frozenset({EVENT_TICK, EVENT_REMINDER_CREATED})
    risk = RISK_LOW
    allow_degraded_delivery = True

    # ── SENSE ────────────────────────────────────────────────────────────────
    async def sense(self, ctx: UserContext) -> _Inputs:
        user_id = ctx.user_id
        timezone_name, consent_granted = await _load_consent_and_timezone(user_id)

        # Consent gate (GDPR), FIRST. A thread follow-up enriches UserAura, so it
        # needs the same explicit Aura consent as briefing/icebreaker. Fail-closed.
        if not consent_granted:
            return _Inputs(eligible=False, reason="no_consent")

        local_now = _local_now(timezone_name)
        local_date = local_now.date().isoformat()

        if not is_within_active_hours(local_now.hour):
            return _Inputs(eligible=False, reason="quiet_hours")

        if await thread_store.read_follow_ups_today(user_id, local_date) >= THREAD_DAILY_CAP:
            return _Inputs(eligible=False, reason="daily_cap")

        threads = await thread_store.list_open_threads(user_id)
        chosen = select_thread_to_follow_up(threads, datetime.now(UTC))
        if chosen is None:
            return _Inputs(eligible=False, reason="no_thread")

        framing_ctx = await _build_framing_context(user_id, local_now)
        return _Inputs(
            eligible=True, thread=chosen, framing_ctx=framing_ctx, local_date=local_date,
        )

    # ── PLAN ─────────────────────────────────────────────────────────────────
    async def plan(self, inputs: _Inputs) -> Plan:
        if not inputs.eligible or inputs.thread is None or inputs.framing_ctx is None:
            return Plan.stand_down(inputs.reason or "ineligible")
        # No idempotency key: act is read + LLM only (no external side effect). The
        # funnel's dedup_key handles send-side dedup at delivery.
        return Plan.act(_CuriosityPlan(
            thread=inputs.thread,
            framing_ctx=inputs.framing_ctx,
            local_date=inputs.local_date,
        ))

    # ── ACT ──────────────────────────────────────────────────────────────────
    async def act(self, plan: Plan, attempt: int) -> _Result:
        cp = cast(_CuriosityPlan, plan.payload)
        prompt = _build_prompt(cp.thread, cp.framing_ctx) + cp.extra_instruction
        # Raises on a real LLM outage (after the model layer's own retries +
        # fallback chain) so the envelope sees INFRA, instead of silently degrading.
        result = await get_model_provider().cheap(
            prompt,
            system=_FRAMER_SYSTEM_PROMPT,
            response_model=FramedFollowUp,
            temperature=0.7,
        )
        framed = _normalise(cast(FramedFollowUp, result), cp.thread)
        return _Result(thread=cp.thread, framed=framed, local_date=cp.local_date)

    # ── VERIFY ───────────────────────────────────────────────────────────────
    async def verify(self, raw: _Result) -> Verdict:
        body = (raw.framed.body or "").strip()
        if not body:
            return Verdict.empty("empty_body")
        if len(raw.framed.suggested_replies) < 2:
            return Verdict.low_quality("too_few_replies")
        is_accountability, detail = await _judge_accountability_voice(body)
        if is_accountability:
            return Verdict.low_quality("accountability_voice", detail=detail)
        return Verdict.ok()

    # ── REPAIR ───────────────────────────────────────────────────────────────
    async def repair(self, verdict: Verdict, plan: Plan, attempt: int) -> Plan | None:
        cp = cast(_CuriosityPlan, plan.payload)
        if verdict.kind == VerdictKind.INFRA:
            # Infra: retry the same plan (the model layer already backed off; this is
            # a thin top-layer retry, not a duplicate of its internal retries).
            return Plan.act(cp)
        if verdict.kind == VerdictKind.LOW_QUALITY:
            # LLM re-plan: re-frame with a stronger anti-accountability nudge.
            return Plan.act(_CuriosityPlan(
                thread=cp.thread,
                framing_ctx=cp.framing_ctx,
                local_date=cp.local_date,
                extra_instruction=cp.extra_instruction + _REPLAN_NUDGE,
            ))
        # EMPTY / anything else: nothing useful to broaden for a single-thread frame.
        return None

    # ── DEGRADE ──────────────────────────────────────────────────────────────
    async def degraded(self, plan: Plan) -> _Result | None:
        cp = cast(_CuriosityPlan, plan.payload)
        # A warm, deterministic fallback question beats silence on escalation.
        return _Result(
            thread=cp.thread,
            framed=_safe_fallback(cp.thread),
            local_date=cp.local_date,
        )

    # ── TO PROPOSAL ──────────────────────────────────────────────────────────
    def to_proposal(self, raw: _Result) -> NotificationProposal | None:
        thread = raw.thread
        framed = raw.framed
        return NotificationProposal(
            user_id="",  # the caller/orchestrator fills user_id from context
            source=SOURCE_THREAD,
            kind=ProposalKind.PROACTIVE,
            dedup_key=f"thread_{thread.thread_id}",
            title=framed.title,
            body=framed.body,
            data={
                "deep_link": "chat",
                "thread_id": thread.thread_id,
                "question": framed.body,
                # FCM data values must be strings; the client JSON-decodes this.
                "suggested_replies": json.dumps(framed.suggested_replies),
                "opening_chat_message": framed.body,
                "notification_reason": _build_thread_reason(thread),
                "notification_origin": NOTIFICATION_ORIGIN_THREAD_ENGINE,
                "thread_source": str(thread.source),
                "local_date": raw.local_date,
                "followups_before": str(thread.follow_ups_sent),
            },
            notification_type=NOTIFICATION_TYPE_THREAD_FOLLOW_UP,
            collapse_key=f"thread_{thread.thread_id}",
            data_only=True,
        )

    def eval_cases(self) -> list[EvalCase]:
        # Deferred: evals are authored once the harness lands (registry warns).
        return []


# ── Sensing helpers (this agent now owns them; removed from thread_reflector) ──
async def _load_consent_and_timezone(user_id: str) -> tuple[str, bool]:
    """Read timezone + Aura consent in one get. Timezone fails OPEN to UTC; consent
    fails CLOSED to False (a behavioural follow-up must never go out on an
    unconfirmed consent read)."""

    def _fetch() -> tuple[str, bool]:
        doc = admin_firestore().collection("users").document(user_id).get()
        if not doc.exists:
            return "UTC", False
        data = doc.to_dict() or {}
        return (
            data.get("timezone", "UTC"),
            data.get("aura_consent_granted", False) is True,
        )

    try:
        return await asyncio.to_thread(_fetch)
    except Exception:
        return "UTC", False


def _local_now(timezone_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(timezone_name))
    except (ZoneInfoNotFoundError, Exception):
        return datetime.now(UTC)


def _time_band(local_datetime: datetime) -> str:
    h = local_datetime.hour
    if 5 <= h < 11:
        return "morning"
    if 11 <= h < 14:
        return "midday"
    if 14 <= h < 18:
        return "afternoon"
    if 18 <= h < 22:
        return "evening"
    return "late"


async def _build_framing_context(user_id: str, local_now: datetime) -> FollowUpFramingContext:
    """Read tone, depth, and top interests from UserAura (best-effort)."""

    def _fetch() -> dict[str, Any]:
        snap = admin_firestore().collection("UserAura").document(user_id).get()
        return (snap.to_dict() or {}) if snap.exists else {}

    try:
        aura = await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("curiosity.agent: UserAura read failed", {
            "user_id": user_id, "error": str(exc),
        })
        aura = {}

    return FollowUpFramingContext(
        dominant_tone=aura.get("dominant_tone"),
        depth_level=int(aura.get("emotional_engagement_level", 1) or 1),
        top_interests=top_interest_subjects(aura, k=3),
        time_band=_time_band(local_now),
    )
