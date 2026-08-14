"""Notice that the user's audio never arrived, and break the silence about it.

The failure this exists for: the client joins the room and publishes no microphone
track. Nothing downstream treats that as an error, and three separate mechanisms then
guarantee total silence.

  1. Buddy's opener is the only unprompted speech, and if the room is already broken
     the user has still heard nothing back from THEM.
  2. The 45s away nudge cannot fire. LiveKit skips arming the away timer entirely while
     ``room_io.subscribed_fut`` is pending (agent_session.py, "skip the timer before user
     join the room"), so the one existing silence fallback is structurally disabled in
     exactly the case that needs it.
  3. No audio means no VAD, no STT, no transcript, so the recorder's idle timer is never
     reset and the session dies at the 5-minute mark with zero turns.

Observed in production as a 300993ms session with num_of_turns 0 and no error anywhere.
The user talked into a dead call for five minutes and filed "why don't you respond when I
use voice chat?"

Two states are distinguished because they are different faults with different words:

  ``no_user_audio``   the participant is in the room publishing no audio track at all.
  ``no_transcript``   audio is flowing and STT is returning nothing.

On either, Buddy says something in his OWN words (generate_reply, never a canned line)
and the client gets a structured session.error. Deliberately not "check your mic": the
microphone is usually working and is how they were talking in the first place, so
blaming it is both wrong and irritating. Say what is true, which is that nothing is
coming through on this end.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from livekit import rtc
from livekit.agents import AgentSession, JobContext

from ...config.settings import settings
from ...lib.logger import logger
from .errors import publish_client_error

NO_USER_AUDIO = "no_user_audio"
NO_TRANSCRIPT = "no_transcript"

# Spoken as instructions, not as text. The point is that Buddy raises it the way a
# friend on a bad line would, in his own voice, once.
_NO_AUDIO_INSTRUCTIONS = (
    "You are connected to them but absolutely nothing is coming through from their "
    "side, so you cannot hear a word they may be saying. Say that once, warmly and "
    "briefly, the way a friend on a bad line would. Tell them you are still here and "
    "will keep listening. Do not blame their microphone, do not give troubleshooting "
    "steps, do not list settings, and do not ask a stack of questions."
)
_NO_TRANSCRIPT_INSTRUCTIONS = (
    "Their audio is reaching you but nothing is coming through as words, so you may be "
    "missing what they said. Say that once, lightly and without alarm, and ask them to "
    "say it again. Do not blame their microphone and do not give troubleshooting steps."
)

_CLIENT_MESSAGES = {
    NO_USER_AUDIO: "Buddy isn't picking up any audio from your side.",
    NO_TRANSCRIPT: "Buddy is having trouble making out what you're saying.",
}


@dataclass
class InputLiveness:
    """What the session actually observed on the way in. Read at teardown."""

    participant_linked: bool = False
    audio_track_seen: bool = False
    transcript_seen: bool = False
    verdict: str = ""

    def note_transcript(self) -> None:
        # A transcript is proof the whole inbound path worked, so it settles the two
        # facts below regardless of whether the probe below ever got to run. Without
        # this, a call shorter than the grace period would report "no audio seen" when
        # the user was audibly talking.
        self.transcript_seen = True
        self.participant_linked = True
        self.audio_track_seen = True


def _user_audio_track_present(ctx: JobContext, user_id: str) -> bool:
    """True when the user's participant is publishing at least one audio track."""
    try:
        participant = ctx.room.remote_participants.get(user_id)
        if participant is None:
            # Identity lookup can miss if the client joined under a different
            # identity; fall back to any remote participant with audio.
            candidates = list(ctx.room.remote_participants.values())
        else:
            candidates = [participant]
        for candidate in candidates:
            for publication in candidate.track_publications.values():
                if publication.kind == rtc.TrackKind.KIND_AUDIO:
                    return True
    except Exception as exc:
        # Never let a liveness probe take down a working session. Assume healthy:
        # a false "I can't hear you" mid-conversation is worse than a missed one.
        logger.warn(
            "VoiceSession: input liveness probe failed",
            {"user_id": user_id, "error": str(exc)},
        )
        return True
    return False


async def watch_input_liveness(
    *,
    session: AgentSession,
    ctx: JobContext,
    liveness: InputLiveness,
    session_id: str,
    user_id: str,
) -> None:
    """Fire at most one silence-breaking nudge. Never raises."""
    try:
        await asyncio.sleep(settings.VOICE_INPUT_GRACE_S)
        liveness.audio_track_seen = _user_audio_track_present(ctx, user_id)
        liveness.participant_linked = bool(ctx.room.remote_participants)

        if not liveness.audio_track_seen:
            await _raise_input_problem(
                session=session,
                ctx=ctx,
                liveness=liveness,
                verdict=NO_USER_AUDIO,
                instructions=_NO_AUDIO_INSTRUCTIONS,
                session_id=session_id,
                user_id=user_id,
            )
            return

        # Audio is flowing. Give STT a long window before second-guessing it, so an
        # ordinary quiet user meets the normal away nudge first and only a genuinely
        # broken transcription path reaches here.
        remaining = max(
            0.0,
            settings.VOICE_NO_TRANSCRIPT_GRACE_S - settings.VOICE_INPUT_GRACE_S,
        )
        await asyncio.sleep(remaining)
        if liveness.transcript_seen:
            return
        await _raise_input_problem(
            session=session,
            ctx=ctx,
            liveness=liveness,
            verdict=NO_TRANSCRIPT,
            instructions=_NO_TRANSCRIPT_INSTRUCTIONS,
            session_id=session_id,
            user_id=user_id,
        )
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.warn(
            "VoiceSession: input liveness watch failed",
            {"session_id": session_id, "user_id": user_id, "error": str(exc)},
        )


async def _raise_input_problem(
    *,
    session: AgentSession,
    ctx: JobContext,
    liveness: InputLiveness,
    verdict: str,
    instructions: str,
    session_id: str,
    user_id: str,
) -> None:
    liveness.verdict = verdict
    # Loud: this is a user sitting in a call that is not working. It was previously
    # indistinguishable from a healthy session in every log we had.
    logger.error(
        "voice_session_input_dead",
        {
            "session_id": session_id,
            "user_id": user_id,
            "verdict": verdict,
            "audio_track_seen": liveness.audio_track_seen,
            "participant_linked": liveness.participant_linked,
        },
    )
    # The data channel first: it lands even when the audio path out is also broken.
    await publish_client_error(ctx, verdict, _CLIENT_MESSAGES[verdict])
    try:
        await session.generate_reply(instructions=instructions)
    except Exception as exc:
        logger.warn(
            "VoiceSession: input liveness nudge failed to speak",
            {"session_id": session_id, "user_id": user_id, "error": str(exc)},
        )
