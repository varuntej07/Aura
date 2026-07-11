"""Free-tier voice budget: warn near the cap, wind the call down at the cap.

Free voice is a per-UTC-day budget (FREE_TIER_DAILY_VOICE_SECONDS, read at
session start). When a free-tier caller is ~60s from that budget, Buddy slips
in one warm heads-up line; when the budget hits zero, Buddy speaks ONE graceful
goodbye and the session is closed server-side. Never a hard cut to silence, and
never a line spoken over the user mid-sentence (both moments wait for a
listening boundary first). Trial and paid users never reach this module: their
effective tier is not "free" (see services/entitlement.py).

The countdown is anchored to time.monotonic() at task start; usage banks to
Firestore only at session end (voice_agent.py), so the in-call countdown itself
is the enforcement. A budget read failure at session start arrives here as
remaining_seconds=None and disables enforcement for the call: an outage must
degrade to a free call, never to a wrongly cut one.

Lines are spoken via session.generate_reply (the same mechanism as the
away-nudge in recorder.py) so they land in Buddy's own voice at a natural turn
boundary. Awaiting the returned SpeechHandle waits for playout, so the goodbye
fully finishes before the close. The close itself is session.aclose() (fires
the session close event, so the recorder's done gate releases and the session
banks its seconds and runs the post-session pipeline exactly like a
client-initiated hangup) followed by a best-effort ctx.delete_room() so the
client's call UI ends instead of idling in a silent room.
"""

from __future__ import annotations

import asyncio
import time

from livekit.agents import AgentSession, JobContext

from ...lib.logger import logger

# LLM-framed so each moment lands in Buddy's voice.
FREE_TIER_VOICE_WARNING_INSTRUCTIONS = (
    "The user is about a minute away from using up their free voice time for today. In Buddy's "
    "warm, casual voice, slip in ONE short, low-pressure line letting them know there's about a "
    "minute of free voice left together for today and that you'll have to wrap up when it runs "
    "out, then keep the conversation going naturally. No guilt and no hard sell."
)

FREE_TIER_VOICE_WIND_DOWN_INSTRUCTIONS = (
    "The user's free voice time for today just ran out, so this call is ending now. In Buddy's "
    "warm, casual voice, say ONE short goodbye: today's free voice time is up, you're still right "
    "there over text, voice is back tomorrow, and upgrading unlocks unlimited voice whenever they "
    "want it. Two sentences at most, no guilt, no hard sell, end on a warm bye."
)

FREE_TIER_VOICE_OUT_OF_TIME_INSTRUCTIONS = (
    "The user is already out of free voice time for today, so this call has to end right away. In "
    "Buddy's warm, casual voice, say ONE short line: today's free voice minutes are used up, "
    "they can still text you anytime, voice is back tomorrow, and upgrading unlocks unlimited "
    "voice. Two sentences at most, no guilt, no hard sell, end on a warm bye."
)

# How long to wait for a turn boundary before speaking, so a line never lands on
# top of the user or Buddy mid-sentence. Polled, matching the away-nudge gate.
_LISTENING_POLL_INTERVAL_S = 0.5
_LISTENING_MAX_WAIT_S = 15.0

# Warn when this many seconds of budget remain.
_WARN_LEAD_SECONDS = 60


async def _wait_for_listening(session: AgentSession) -> None:
    waited = 0.0
    while str(getattr(session, "agent_state", "")) != "listening" and waited < _LISTENING_MAX_WAIT_S:
        await asyncio.sleep(_LISTENING_POLL_INTERVAL_S)
        waited += _LISTENING_POLL_INTERVAL_S


async def _speak_goodbye_and_close(
    session: AgentSession,
    ctx: JobContext,
    *,
    instructions: str,
    session_id: str,
    user_id: str,
    reason: str,
) -> None:
    """One graceful spoken line, fully played out, then the server-side close."""
    await _wait_for_listening(session)
    # Awaiting the SpeechHandle waits for playout; uninterruptible so a stray
    # utterance can't cancel the goodbye and leave the close feeling abrupt.
    await session.generate_reply(
        instructions=instructions,
        allow_interruptions=False,
    )

    logger.info("VoiceSession: free-tier voice budget enforced, closing session", {
        "session_id": session_id, "user_id": user_id, "reason": reason,
    })

    # aclose fires the session close event: the recorder releases the entrypoint,
    # which banks the elapsed seconds and runs the post-session pipeline exactly
    # as a client-initiated hangup does.
    await session.aclose()

    # Best-effort: drop the room so the client's call UI ends too, instead of
    # sitting connected to a silent room until the user hangs up manually.
    try:
        await ctx.delete_room()
    except Exception as exc:
        logger.warn("VoiceSession: room delete after budget close failed", {
            "session_id": session_id, "user_id": user_id, "error": str(exc),
        })


async def run_free_tier_voice_limit(
    session: AgentSession,
    ctx: JobContext,
    *,
    remaining_seconds: int | None,
    session_id: str,
    user_id: str,
) -> None:
    """Warn a free-tier caller ~60s before the daily budget runs out, then wind
    the call down at zero.

    No-op when remaining_seconds is None (budget read failed: never enforce on
    a read error). Cancelled by the caller when the session ends first. An
    unexpected error logs and leaves the call running; an enforcement bug must
    never cut a call.
    """
    try:
        if remaining_seconds is None:
            return
        if remaining_seconds <= 0:
            # Shouldn't reach here (voice_agent routes this to the out-of-time
            # path before starting a countdown), but enforce rather than trust.
            await _speak_goodbye_and_close(
                session, ctx,
                instructions=FREE_TIER_VOICE_OUT_OF_TIME_INSTRUCTIONS,
                session_id=session_id, user_id=user_id,
                reason="no_budget_at_start",
            )
            return

        # Anchor the whole countdown to one clock so the warn and the cutoff
        # can't drift apart across the waits in between.
        anchor = time.monotonic()

        await asyncio.sleep(max(0, remaining_seconds - _WARN_LEAD_SECONDS))
        await _wait_for_listening(session)
        await session.generate_reply(instructions=FREE_TIER_VOICE_WARNING_INSTRUCTIONS)
        logger.info("VoiceSession: free-tier voice budget warning spoken", {
            "session_id": session_id, "user_id": user_id,
            "remaining_seconds_at_start": remaining_seconds,
        })

        await asyncio.sleep(max(0.0, remaining_seconds - (time.monotonic() - anchor)))
        await _speak_goodbye_and_close(
            session, ctx,
            instructions=FREE_TIER_VOICE_WIND_DOWN_INSTRUCTIONS,
            session_id=session_id, user_id=user_id,
            reason="budget_exhausted",
        )
    except asyncio.CancelledError:
        # Session ended before the budget did; nothing to do.
        raise
    except Exception as exc:
        logger.warn("VoiceSession: free-tier voice limit task failed", {
            "session_id": session_id, "user_id": user_id, "error": str(exc),
        })


async def run_out_of_free_time_close(
    session: AgentSession,
    ctx: JobContext,
    *,
    session_id: str,
    user_id: str,
) -> None:
    """A free-tier caller with zero budget left connected anyway: let Buddy's
    greeting finish, say the out-of-time line, and close the session."""
    try:
        await _speak_goodbye_and_close(
            session, ctx,
            instructions=FREE_TIER_VOICE_OUT_OF_TIME_INSTRUCTIONS,
            session_id=session_id, user_id=user_id,
            reason="no_budget_at_start",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warn("VoiceSession: out-of-free-time close failed", {
            "session_id": session_id, "user_id": user_id, "error": str(exc),
        })
