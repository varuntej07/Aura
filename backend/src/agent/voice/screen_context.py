"""On-screen / field context delivered into a live voice session.

The Buddy Keyboard (and the app) can hand the text the user is looking at into a voice
turn so Buddy can talk about "what is on my screen" and reply, just like in-app voice.
The client publishes a small JSON message over the LiveKit data channel AFTER joining
the room; the voice agent injects it as a ONE-SHOT turn at the next natural boundary
(mirroring the free-tier nudge in ``free_tier_limit.py``), never as a canned interrupt.

Three message types share this module, all sent reliably over the data channel:

  {"type": "screen_context", "context_before": str, "field_type": str, "app": str}
  {"type": "ocr_context",    "text": str}
  {"type": "text_input",     "text": str, "client_message_id": str,
                               "generation": int}

``screen_context`` and ``ocr_context`` carry text the user was reading or typing, which
can include another person's message, so it is UNTRUSTED: it is wrapped in delimiters
and the model is told never to follow instructions inside it (the same posture as the
keyboard drafter). ``text_input`` is the user's own typed words, so it is delivered as a
genuine user turn (``generate_reply(user_input=...)``).

The handler in ``voice_agent.py`` parses the packet and dispatches here; every path is
fail-soft and never raises into the session.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from uuid import UUID, uuid4

from livekit import rtc
from livekit.agents import AgentSession

from ...lib.logger import logger
from ...prompts import keyboard_shared_context_instruction

# Wire types for the data-channel messages (the single source of truth; the keyboard
# and the Flutter client send these exact strings).
SCREEN_CONTEXT_TYPE = "screen_context"
OCR_CONTEXT_TYPE = "ocr_context"
TEXT_INPUT_TYPE = "text_input"
CLIENT_EVENTS_TOPIC = "client_events"
AGENT_EVENTS_TOPIC = "agent_events"

# Defensive cap so a runaway payload never bloats the turn (matches the keyboard's
# CONTEXT_MAX_CHARS).
_CONTEXT_MAX_CHARS = 2000

# Wait for a turn boundary so the injected turn never lands on top of Buddy mid-sentence.
_LISTENING_POLL_INTERVAL_S = 0.5
_LISTENING_MAX_WAIT_S = 15.0


def build_screen_context_instruction(
    context_before: str, field_type: str | None, app: str | None
) -> str:
    """A delimited, untrusted one-shot instruction describing the on-screen text."""
    return keyboard_shared_context_instruction(
        text=context_before or "",
        field_type=field_type or "",
        app=app or "",
        max_chars=_CONTEXT_MAX_CHARS,
    )


async def _wait_for_turn_boundary(session: AgentSession) -> bool:
    waited = 0.0
    while (
        str(getattr(session, "agent_state", "")) != "listening"
        and waited < _LISTENING_MAX_WAIT_S
    ):
        await asyncio.sleep(_LISTENING_POLL_INTERVAL_S)
        waited += _LISTENING_POLL_INTERVAL_S
    return str(getattr(session, "agent_state", "")) == "listening"


async def deliver_screen_context(
    session: AgentSession,
    *,
    context_before: str,
    field_type: str | None,
    app: str | None,
    session_id: str,
    user_id: str,
    on_instruction: Callable[[str], None] | None = None,
) -> None:
    """Inject the on-screen text as a one-shot, untrusted instruction turn.

    No-op on empty context. Loud on every outcome (delivered / empty / failed) so a
    silent drop can never look like success. Never raises into the session.
    """
    try:
        if not (context_before or "").strip():
            logger.info(
                "VoiceSession: screen context empty, skipping",
                {"session_id": session_id, "user_id": user_id},
            )
            return
        if not await _wait_for_turn_boundary(session):
            logger.warn(
                "VoiceSession: screen context skipped, agent stayed busy",
                {"session_id": session_id, "user_id": user_id},
            )
            return
        instruction = build_screen_context_instruction(context_before, field_type, app)
        if on_instruction is not None:
            on_instruction(instruction)
        await session.generate_reply(instructions=instruction)
        logger.info(
            "VoiceSession: screen context delivered",
            {
                "session_id": session_id,
                "user_id": user_id,
                "field_type": field_type or "unknown",
                "app": app or "unknown",
                "chars": len(context_before.strip()),
            },
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warn(
            "VoiceSession: screen context delivery failed",
            {"session_id": session_id, "user_id": user_id, "error": str(exc)},
        )


async def deliver_typed_message(
    session: AgentSession,
    *,
    text: str,
    session_id: str,
    user_id: str,
) -> None:
    """Deliver a user-typed message as a genuine user turn (their own words, trusted)."""
    try:
        if not (text or "").strip():
            return
        if not await _wait_for_turn_boundary(session):
            logger.warn(
                "VoiceSession: typed message skipped, agent stayed busy",
                {"session_id": session_id, "user_id": user_id},
            )
            return
        await session.generate_reply(user_input=text.strip()[:_CONTEXT_MAX_CHARS])
        logger.info(
            "VoiceSession: typed message delivered",
            {"session_id": session_id, "user_id": user_id, "chars": len(text.strip())},
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warn(
            "VoiceSession: typed message delivery failed",
            {"session_id": session_id, "user_id": user_id, "error": str(exc)},
        )


@dataclass(frozen=True)
class _QueuedTypedMessage:
    text: str
    client_message_id: str
    generation: int


class TypedMessageQueue:
    """One FIFO owner for every typed turn in a live voice session."""

    def __init__(
        self,
        *,
        session: AgentSession,
        room: rtc.Room,
        session_id: str,
        user_id: str,
        bind_text_observer: Callable[[Callable[[str], None] | None], None],
    ) -> None:
        self._session = session
        self._room = room
        self._session_id = session_id
        self._user_id = user_id
        self._bind_text_observer = bind_text_observer
        self._queue: asyncio.Queue[_QueuedTypedMessage] = asyncio.Queue()
        self._events: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
        self._seen_client_message_ids: set[str] = set()
        self._active_client_message_id: str | None = None
        self._streaming_client_message_id: str | None = None
        self._active_text_parts: list[str] = []
        self._publisher_task = asyncio.create_task(
            self._publish_events(), name=f"voice-text-events-{session_id[:8]}"
        )
        self._worker_task = asyncio.create_task(
            self._run(), name=f"voice-text-fifo-{session_id[:8]}"
        )
        self._bind_text_observer(self._on_text_delta)

    def submit(self, *, text: str, client_message_id: str, generation: object) -> None:
        client_message_id = (client_message_id or "").strip()
        try:
            parsed_id = UUID(client_message_id)
        except (ValueError, AttributeError):
            logger.warn(
                "VoiceSession: typed message rejected, invalid client_message_id",
                {"session_id": self._session_id, "user_id": self._user_id},
            )
            return
        if parsed_id.version != 4:
            logger.warn(
                "VoiceSession: typed message rejected, client_message_id is not uuid4",
                {"session_id": self._session_id, "user_id": self._user_id},
            )
            return
        dedupe_id = str(parsed_id)
        if dedupe_id in self._seen_client_message_ids:
            self._emit(
                "text_input.failed",
                {"client_message_id": client_message_id, "reason": "duplicate"},
            )
            return
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            self._emit(
                "text_input.failed",
                {"client_message_id": client_message_id, "reason": "invalid_generation"},
            )
            return
        clean_text = (text or "").strip()
        if not clean_text:
            self._emit(
                "text_input.failed",
                {"client_message_id": client_message_id, "reason": "empty"},
            )
            return
        if len(clean_text) > _CONTEXT_MAX_CHARS:
            self._emit(
                "text_input.failed",
                {"client_message_id": client_message_id, "reason": "message_too_long"},
            )
            return

        # Recorded only once the message is actually accepted onto the queue. The
        # set means "this turn was enqueued", not "this id was seen": burning the
        # id on a rejection would make the rejected message unresendable under
        # its own id for the rest of the session.
        self._seen_client_message_ids.add(dedupe_id)
        queue_position = self._queue.qsize() + int(self._active_client_message_id is not None)
        self._emit(
            "text_input.accepted",
            {"client_message_id": client_message_id, "queue_position": queue_position},
        )
        self._queue.put_nowait(
            _QueuedTypedMessage(
                text=clean_text,
                client_message_id=client_message_id,
                generation=generation,
            )
        )

    def submit_legacy(self, *, text: str) -> None:
        """Keep existing non-desktop callers serialized until they adopt the protocol."""
        self.submit(
            text=(text or "").strip()[:_CONTEXT_MAX_CHARS],
            client_message_id=str(uuid4()),
            generation=0,
        )

    async def close(self) -> None:
        self._bind_text_observer(None)
        self._worker_task.cancel()
        self._publisher_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._worker_task
        with suppress(asyncio.CancelledError):
            await self._publisher_task

    def _emit(self, event_type: str, payload: dict) -> None:
        self._events.put_nowait((event_type, payload))

    def _on_text_delta(self, text: str) -> None:
        if self._streaming_client_message_id is None or not text:
            return
        self._active_text_parts.append(text)
        self._emit(
            "assistant.text.delta",
            {"client_message_id": self._streaming_client_message_id, "text": text},
        )

    async def _publish_events(self) -> None:
        while True:
            event_type, payload = await self._events.get()
            try:
                data = json.dumps({"type": event_type, "payload": payload}).encode("utf-8")
                await self._room.local_participant.publish_data(
                    data, reliable=True, topic=AGENT_EVENTS_TOPIC
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warn(
                    "VoiceSession: typed message event publish failed",
                    {
                        "session_id": self._session_id,
                        "user_id": self._user_id,
                        "type": event_type,
                        "error_type": type(exc).__name__,
                    },
                )
            finally:
                self._events.task_done()

    async def _run(self) -> None:
        while True:
            message = await self._queue.get()
            self._active_client_message_id = message.client_message_id
            self._active_text_parts = []
            try:
                if not await _wait_for_turn_boundary(self._session):
                    self._emit(
                        "text_input.failed",
                        {
                            "client_message_id": message.client_message_id,
                            "reason": "busy_timeout",
                        },
                    )
                    continue
                self._emit(
                    "text_input.started",
                    {"client_message_id": message.client_message_id},
                )
                self._streaming_client_message_id = message.client_message_id
                speech = self._session.generate_reply(user_input=message.text)
                await speech.wait_for_playout()
                if speech.interrupted:
                    self._emit(
                        "text_input.failed",
                        {
                            "client_message_id": message.client_message_id,
                            "reason": "interrupted",
                        },
                    )
                    continue
                self._emit(
                    "assistant.text.done",
                    {
                        "client_message_id": message.client_message_id,
                        "text": "".join(self._active_text_parts),
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._emit(
                    "text_input.failed",
                    {
                        "client_message_id": message.client_message_id,
                        "reason": "generation_failed",
                    },
                )
                logger.warn(
                    "VoiceSession: typed message generation failed",
                    {
                        "session_id": self._session_id,
                        "user_id": self._user_id,
                        "client_message_id": message.client_message_id,
                        "error_type": type(exc).__name__,
                    },
                )
            finally:
                self._active_client_message_id = None
                self._streaming_client_message_id = None
                self._active_text_parts = []
                self._queue.task_done()
