"""Closed-set resolution + future-concern sensor.

Fire-and-forget after every chat message (like the UserAura extractor). ONE Gemini
Flash call does two conservative things:

  1. RESOLUTION — given the user's OPEN follow-ups (a closed set of slug + question),
     decide which, if any, THIS message resolves. Closed-set classification (pick an
     id from the list), never free-text equality, so "mom's operation" and "mom is
     fine" resolve the same intent. Resolutions are emitted as a ``life_update`` event
     so the orchestrator's reconcile cancels them durably (retryable).
  2. NEW FOLLOW-UP — does the user mention something with a future outcome a caring
     friend would check back on (an exam, surgery, trip, hard conversation)? If so,
     schedule ONE revocable pending intent with a warm, ready-to-send question and a
     fire time after the event likely concludes.

Never blocks the chat stream; all failures are swallowed. Consent-gated (GDPR, same
gate as UserAura) and cost-capped. Most messages produce neither output — the
restraint is the point.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from pydantic import BaseModel

from ...lib.logger import logger
from ...prompts import REACTIVE_INTENT_SYSTEM_PROMPT, reactive_intent_user_prompt
from ..model_provider import get_model_provider
from ..user_aura_extractor import _user_has_granted_aura_consent
from . import cost_cap, event_bus, intent_store
from .events import EVENT_LIFE_UPDATE

# Clamp the model's fire delay so a hallucinated number can't queue a follow-up for
# years out or fire it before the event even happens.
_MIN_FIRE_HOURS = 1.0
_MAX_FIRE_HOURS = 24.0 * 30  # 30 days
_SENSE_TEMPERATURE = 0.1
_SUBJECT_MAX_CHARS = 60
_QUESTION_MAX_CHARS = 240
_TITLE_MAX_CHARS = 40


class NewFollowup(BaseModel):
    subject: str = ""
    question: str = ""
    title: str = ""
    fire_in_hours: float = 24.0


class IntentSenseResult(BaseModel):
    resolved: list[str] = []
    new_followup: NewFollowup | None = None



def _slug(subject: str) -> str:
    cleaned = "".join(c if (c.isalnum() or c == "_") else "_" for c in subject.strip().lower())
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    return cleaned[:_SUBJECT_MAX_CHARS]


async def reconcile_and_schedule(
    uid: str,
    message: str,
    prev_buddy_response: str | None = None,
    *,
    now: datetime | None = None,
    user_doc: dict[str, Any] | None = None,
    session_id: str = "",
) -> None:
    """Public entry point — called via asyncio.create_task from the chat handler.
    Never raises; never blocks the stream. ``user_doc`` lets the chat handler pass
    the users/{uid} doc it already fetched this turn instead of this (detached,
    fire-and-forget) task re-fetching it independently."""
    if not message or not message.strip():
        return
    if not await _user_has_granted_aura_consent(uid, user_doc):
        return
    if not await cost_cap.within_daily_budget(uid):
        return

    when = now or datetime.now(UTC)
    try:
        open_intents = await intent_store.list_open_subjects(uid)
        prompt = reactive_intent_user_prompt(
            open_followups=[(intent.subject, intent.question) for intent in open_intents],
            previous_buddy=prev_buddy_response or "",
            message=message,
        )
        result = cast(IntentSenseResult, await get_model_provider().cheap(
            prompt,
            system=REACTIVE_INTENT_SYSTEM_PROMPT,
            response_model=IntentSenseResult,
            temperature=_SENSE_TEMPERATURE,
        ))
        await cost_cap.record_llm_call(uid)
    except Exception as exc:
        logger.warn("intent_sense: classification failed (swallowed)", {
            "user_id": uid, "error": str(exc),
        })
        return

    # 1) Resolutions -> durable life_update event -> reconcile cancels.
    open_subjects = {i.subject for i in open_intents}
    resolved = [s for s in result.resolved if isinstance(s, str) and s in open_subjects]
    if resolved:
        try:
            await event_bus.emit(
                uid, EVENT_LIFE_UPDATE,
                payload={"resolved_subjects": resolved},
                source="intent_sense",
            )
            await event_bus.dispatch_inline(uid)
            logger.info("intent_sense: emitted resolution", {
                "user_id": uid, "resolved": resolved,
            })
        except Exception as exc:
            logger.warn("intent_sense: resolution emit failed", {
                "user_id": uid, "error": str(exc),
            })

    # 2) New future concern -> schedule a revocable pending intent.
    nf = result.new_followup
    if nf and nf.subject.strip() and nf.question.strip():
        subject = _slug(nf.subject)
        if subject:
            hours = max(_MIN_FIRE_HOURS, min(float(nf.fire_in_hours or 24.0), _MAX_FIRE_HOURS))
            fire_at = when + timedelta(hours=hours)
            await intent_store.schedule_intent(
                uid,
                kind="life_followup",
                subject=subject,
                question=nf.question.strip()[:_QUESTION_MAX_CHARS],
                title=nf.title.strip()[:_TITLE_MAX_CHARS],
                fire_at=fire_at,
                source="intent_sense",
                session_id=session_id,
                now=when,
            )
