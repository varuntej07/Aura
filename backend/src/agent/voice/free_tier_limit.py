"""Free-tier voice budget nudge.

Free voice is a per-UTC-day budget (FREE_TIER_DAILY_VOICE_SECONDS, read at session start).
When a free-tier caller is ~60s from that budget, Buddy slips in one warm, low-pressure line
and then keeps talking. Warn-only: the call is never cut off here.

The nudge is spoken via session.generate_reply (the same mechanism the away-nudge uses in
recorder.py) so it lands in Buddy's own voice at a natural turn boundary, never as a canned
interrupt over the user.
"""

from __future__ import annotations

import asyncio

from livekit.agents import AgentSession

from ...lib.logger import logger

# LLM-framed so it lands in Buddy's voice. Warn-only, so it must NOT wind down or hard-sell.
FREE_TIER_VOICE_WARNING_INSTRUCTIONS = (
    "The user is about a minute away from using up their free voice time for today. In Buddy's "
    "warm, casual voice, slip in ONE short, low-pressure line letting them know you've got about "
    "a minute of free voice left together for today, then keep the conversation going naturally. "
    "No guilt, no hard sell, and do not stop talking or wind the call down."
)

# How long to wait for a turn boundary before firing, so the line never lands on top of Buddy
# mid-sentence or mid tool-call. Polled, matching the away-nudge / tool-phrase listening gate.
_LISTENING_POLL_INTERVAL_S = 0.5
_LISTENING_MAX_WAIT_S = 15.0

# Fire when this many seconds of budget remain.
_WARN_LEAD_SECONDS = 60


async def run_free_tier_voice_nudge(
    session: AgentSession,
    *,
    remaining_seconds: int | None,
    session_id: str,
    user_id: str,
) -> None:
    """Fire one budget warning ~60s before a free-tier caller's daily voice runs out.

    No-op when remaining_seconds is None (budget read failed -> never falsely warn) or <= 0
    (already over budget -> "a minute left" would be untrue). Cancelled by the caller when the
    session ends first. Never raises into the session.
    """
    try:
        if remaining_seconds is None or remaining_seconds <= 0:
            return

        await asyncio.sleep(max(0, remaining_seconds - _WARN_LEAD_SECONDS))

        # Wait for a turn boundary so the warning slips in naturally rather than interrupting.
        waited = 0.0
        while str(getattr(session, "agent_state", "")) != "listening" and waited < _LISTENING_MAX_WAIT_S:
            await asyncio.sleep(_LISTENING_POLL_INTERVAL_S)
            waited += _LISTENING_POLL_INTERVAL_S

        await session.generate_reply(instructions=FREE_TIER_VOICE_WARNING_INSTRUCTIONS)
        logger.info("VoiceSession: free-tier voice budget nudge", {
            "session_id": session_id, "user_id": user_id,
            "remaining_seconds_at_start": remaining_seconds,
        })
    except asyncio.CancelledError:
        # Session ended before the warning was due; nothing to do.
        raise
    except Exception as exc:
        logger.warn("VoiceSession: free-tier voice budget nudge failed", {
            "session_id": session_id, "user_id": user_id,
            "error": str(exc),
        })
