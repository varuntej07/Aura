"""Wall-clock guard for one active mock interview.

This is deliberately not the free-tier voice budget limiter. That limiter ends
the room; this guard ends Interview Mode only and returns the still-live session
to Buddy. It uses ``time.monotonic`` so wall-clock adjustments cannot extend or
shorten a candidate's interview.
"""

from __future__ import annotations

import asyncio
import time

from livekit.agents import AgentSession

from ....lib.logger import logger
from .debrief import InterviewDebriefService
from .models import InterviewPhase, VoiceSessionState

SOFT_WARNING_S = 30 * 60
FINAL_WARNING_S = 34 * 60
HARD_CAP_S = 35 * 60

SOFT_WARNING = "We've reached 30 minutes. We have up to five more minutes to wrap up."
FINAL_WARNING = (
    "We have about one minute left. At 35 minutes, I won't ask any more questions "
    "and I'll give you a brief debrief."
)
HARD_CAP_NOTICE = (
    "We've reached 35 minutes. I won't ask any more questions. "
    "I'll give you a brief debrief now."
)


def start_interview_time_guard(*, session: AgentSession, state: VoiceSessionState) -> None:
    """Arm one guard after the interviewer, rather than setup, has entered."""
    interview = state.interview
    interview_id = interview.interview_id
    ownership_epoch = interview.ownership_epoch
    task = asyncio.create_task(
        _run(
            session=session,
            state=state,
            interview_id=interview_id,
            ownership_epoch=ownership_epoch,
        ),
        name=f"interview-time-guard-{interview_id[:8]}",
    )
    interview.arm_time_guard(task)


def _is_active(
    state: VoiceSessionState, *, interview_id: str, ownership_epoch: int
) -> bool:
    interview = state.interview
    return (
        interview.is_current(interview_id, ownership_epoch)
        and interview.phase is InterviewPhase.INTERVIEWING
    )


async def _sleep_until(started_at: float, deadline_s: float) -> None:
    await asyncio.sleep(max(0.0, deadline_s - (time.monotonic() - started_at)))


async def _speak_notice_when_idle(
    *,
    session: AgentSession,
    state: VoiceSessionState,
    interview_id: str,
    ownership_epoch: int,
    text: str,
    final_notice: bool,
) -> None:
    """Speak only at a turn boundary; a 34-minute warning supersedes a late 30-minute one."""
    try:
        await session.wait_for_idle()
        interview = state.interview
        if not _is_active(
            state, interview_id=interview_id, ownership_epoch=ownership_epoch
        ) or interview.hard_time_cap_reached:
            return
        if not final_notice and interview.final_time_warning_due:
            return
        await session.say(text, allow_interruptions=True, add_to_chat_ctx=False)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warn(
            "InterviewTimeGuard: warning failed",
            {
                "interview_id": interview_id,
                "warning": "final" if final_notice else "soft",
                "error_type": type(exc).__name__,
            },
        )


async def _return_to_buddy(
    *, session: AgentSession, state: VoiceSessionState, interview_id: str, ownership_epoch: int
) -> None:
    if not _is_active(state, interview_id=interview_id, ownership_epoch=ownership_epoch):
        return
    buddy_factory = state.buddy_factory
    if buddy_factory is None:
        logger.error(
            "InterviewTimeGuard: missing Buddy factory at hard cap",
            {"interview_id": interview_id},
        )
        return
    buddy = await buddy_factory(session.current_agent.chat_ctx.copy(exclude_instructions=True))
    if not _is_active(state, interview_id=interview_id, ownership_epoch=ownership_epoch):
        return
    state.interview.request_return("time_limit_reached")
    session.update_agent(buddy)


async def _finish_at_hard_cap(
    *, session: AgentSession, state: VoiceSessionState, interview_id: str, ownership_epoch: int
) -> None:
    interview = state.interview
    interview.mark_hard_time_cap_reached()
    if not interview.claim_debrief():
        return
    try:
        # Let an in-progress user utterance end; the state flag above prevents
        # its turn from opening another question once LiveKit finalizes it.
        await session.wait_for_idle()
        if not _is_active(state, interview_id=interview_id, ownership_epoch=ownership_epoch):
            return
        await session.say(HARD_CAP_NOTICE, allow_interruptions=False, add_to_chat_ctx=False)
        debrief = await InterviewDebriefService().build(interview.dossier, interview.answers)
        await session.say(debrief, allow_interruptions=False, add_to_chat_ctx=False)
        await _return_to_buddy(
            session=session,
            state=state,
            interview_id=interview_id,
            ownership_epoch=ownership_epoch,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warn(
            "InterviewTimeGuard: hard-cap finish failed",
            {"interview_id": interview_id, "error_type": type(exc).__name__},
        )


async def _run(
    *, session: AgentSession, state: VoiceSessionState, interview_id: str, ownership_epoch: int
) -> None:
    """Issue two truthful warnings, then end only Interview Mode at 35 minutes."""
    started_at = time.monotonic()
    notice_tasks: set[asyncio.Task[None]] = set()
    try:
        await _sleep_until(started_at, SOFT_WARNING_S)
        if not _is_active(state, interview_id=interview_id, ownership_epoch=ownership_epoch):
            return
        state.interview.mark_soft_time_warning_due()
        notice_tasks.add(
            asyncio.create_task(
                _speak_notice_when_idle(
                    session=session,
                    state=state,
                    interview_id=interview_id,
                    ownership_epoch=ownership_epoch,
                    text=SOFT_WARNING,
                    final_notice=False,
                )
            )
        )

        await _sleep_until(started_at, FINAL_WARNING_S)
        if not _is_active(state, interview_id=interview_id, ownership_epoch=ownership_epoch):
            return
        state.interview.mark_final_time_warning_due()
        notice_tasks.add(
            asyncio.create_task(
                _speak_notice_when_idle(
                    session=session,
                    state=state,
                    interview_id=interview_id,
                    ownership_epoch=ownership_epoch,
                    text=FINAL_WARNING,
                    final_notice=True,
                )
            )
        )

        await _sleep_until(started_at, HARD_CAP_S)
        if _is_active(state, interview_id=interview_id, ownership_epoch=ownership_epoch):
            await _finish_at_hard_cap(
                session=session,
                state=state,
                interview_id=interview_id,
                ownership_epoch=ownership_epoch,
            )
    except asyncio.CancelledError:
        raise
    finally:
        for task in notice_tasks:
            task.cancel()
