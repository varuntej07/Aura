"""Output mute: keep thinking and keep streaming text, produce no audio.

The desktop user can silence Buddy without ending the call (library, open
office, someone else's meeting). Two layers do the work, and both matter:

* ``session.output.set_audio_enabled(False)`` is the real switch. With the
  audio sink detached, ``AgentActivity`` resolves ``audio_output`` to None and
  never calls ``perform_tts_inference`` at all, so a muted turn costs no
  Cartesia request and no TTS latency. It covers ``session.say``,
  ``session.generate_reply``, and the Realtime path in one place, because all
  three read the same flag. Text keeps flowing: the transcript synchronizer
  notices the detached audio and forwards text straight through.
* ``BuddyAgent.tts_node`` early-returns as well, so even a code path that
  somehow reaches the node with audio still attached synthesizes nothing.

The initial mode does NOT arrive here. It rides in the ``/voice/token``
participant metadata and is read before ``BuddyAgent`` is built, because a mute
published after connect loses the race against the worker's first speech. This
controller owns every LATER change, plus the acknowledgement for both.

The ack is the point. Without it a client whose worker is on an older build
would mute its own speakers, look muted, and quietly keep paying for TTS.
"""

from __future__ import annotations

from typing import Protocol

from livekit import rtc
from livekit.agents import AgentSession

from ...lib.logger import logger
from .transport import publish_client_event

OUTPUT_MODE_TYPE = "output.mode"
OUTPUT_MODE_ACK_TYPE = "output.mode_ack"
KNOWN_OUTPUT_MODES = frozenset({"voice", "text"})
DEFAULT_OUTPUT_MODE = "voice"


class OutputModeBuddy(Protocol):
    """The agent only needs to be told which mode is live."""

    def set_text_output(self, text_output: bool) -> None: ...


class OutputModeController:
    """Applies output-mode changes and acknowledges every one of them."""

    def __init__(
        self,
        *,
        session: AgentSession,
        room: rtc.Room,
        buddy: OutputModeBuddy,
        session_id: str,
        user_id: str,
        initial_mode: str,
        client_events_topic: str,
    ) -> None:
        self._session = session
        self._room = room
        self._buddy = buddy
        self._session_id = session_id
        self._user_id = user_id
        self._mode = initial_mode if initial_mode in KNOWN_OUTPUT_MODES else DEFAULT_OUTPUT_MODE
        self._client_events_topic = client_events_topic
        # Generation 0 belongs to the token metadata. Every published control
        # carries a higher one, and a control at or below the current
        # generation is a stale toggle that lost its race.
        self._generation = 0

    @property
    def mode(self) -> str:
        return self._mode

    async def apply_initial(self) -> None:
        """Apply the token-stamped mode once the session is live, and ack it.

        Called even for the default 'voice' mode so the desktop learns that this
        worker understands output modes at all, rather than inferring it from a
        silence it cannot distinguish from an old build.
        """
        await self._apply(self._mode, self._generation, source="token_metadata")

    async def apply_control(
        self, msg: dict, participant_identity: str, topic: str
    ) -> None:
        """Handle an ``output.mode`` control published by the session owner."""
        if participant_identity != self._user_id or topic != self._client_events_topic:
            logger.warn(
                "VoiceSession: output mode packet rejected",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "participant": participant_identity,
                    "topic": topic,
                },
            )
            return
        mode = msg.get("mode")
        if mode not in KNOWN_OUTPUT_MODES:
            logger.warn(
                "VoiceSession: output mode packet rejected, unknown mode",
                {"session_id": self._session_id, "user_id": self._user_id},
            )
            return
        generation = msg.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            logger.warn(
                "VoiceSession: output mode packet rejected, invalid generation",
                {"session_id": self._session_id, "user_id": self._user_id},
            )
            return
        if generation <= self._generation:
            logger.info(
                "VoiceSession: output mode packet ignored, stale generation",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "generation": generation,
                    "current_generation": self._generation,
                },
            )
            return
        await self._apply(mode, generation, source="client_control")

    async def _apply(self, mode: str, generation: int, *, source: str) -> None:
        applied = True
        reason: str | None = None
        try:
            # Detaching the audio sink is what actually stops synthesis. An
            # in-flight speech already captured its output and finishes
            # audibly; the client mutes its own playback for that one, and
            # every later turn is silent at the source.
            self._session.output.set_audio_enabled(mode == "voice")
            self._buddy.set_text_output(mode == "text")
        except Exception as exc:
            applied = False
            reason = type(exc).__name__
            logger.warn(
                "VoiceSession: output mode change failed",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "mode": mode,
                    "generation": generation,
                    "error_type": type(exc).__name__,
                },
            )
        if applied:
            self._mode = mode
            self._generation = generation
            logger.info(
                "VoiceSession: output mode applied",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "mode": mode,
                    "generation": generation,
                    "source": source,
                },
            )
        await self._publish_ack(mode=mode, generation=generation, applied=applied, reason=reason)

    async def _publish_ack(
        self, *, mode: str, generation: int, applied: bool, reason: str | None
    ) -> None:
        await publish_client_event(
            self._room,
            OUTPUT_MODE_ACK_TYPE,
            {
                "mode": mode,
                "generation": generation,
                "applied": applied,
                "reason": reason,
            },
            log_message="VoiceSession: output mode acknowledgement failed",
            log_fields={
                "session_id": self._session_id,
                "user_id": self._user_id,
                "mode": mode,
                "generation": generation,
            },
        )
