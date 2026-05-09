"""
LiveKit voice agent using cascading architecture: 
Deepgram STT -> Claude LLM -> Cartesia TTS pipeline.

The worker connects to LiveKit Cloud and waits for participant joins.
When a Flutter client joins room "voice-{uid}", this agent starts a pipeline session.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable
from uuid import uuid4

from livekit import agents
from livekit.agents import AgentSession, JobContext, WorkerOptions, cli, function_tool, room_io
from livekit.agents import llm as lk_llm
from livekit.agents import stt as lk_stt
from livekit.agents import tts as lk_tts
from livekit.plugins import anthropic, cartesia, deepgram, google, silero
from livekit.agents.voice import room_io
from livekit.agents import TurnHandlingOptions
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from ..config.settings import settings
from ..lib.logger import logger
from ..services.tool_executor import ToolExecutor

_TOOL = settings.VOICE_TOOL_TIMEOUT_S


@asynccontextmanager
async def _voice_session_context(user_id: str, room_name: str) -> AsyncIterator[str]:
    session_id = str(uuid4())
    start = time.monotonic()
    logger.info("VoiceSession: started", {
        "session_id": session_id, "user_id": user_id, "room": room_name,
    })
    error: Exception | None = None
    try:
        yield session_id
    except Exception as exc:
        error = exc
        logger.exception("VoiceSession: unhandled error", {
            "session_id": session_id, "user_id": user_id,
            "error_type": type(exc).__name__, "error": str(exc),
        })
        raise
    finally:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info("VoiceSession: closed", {
            "session_id": session_id, "user_id": user_id,
            "duration_ms": elapsed_ms,
            "error": str(error) if error else None,
        })


class BuddyAgent(agents.Agent):
    def __init__(self, user_id: str, publish_event: Callable[[dict], Awaitable[None]]) -> None:
        super().__init__(instructions=settings.VOICE_PROMPT)
        self._user_id = user_id
        self._executor = ToolExecutor(user_id)
        self._publish_event = publish_event

    def _ok(self, result: dict) -> str:
        return json.dumps(result)

    async def _thinking(self, message: str) -> None:
        await self._publish_event({"type": "tool_thinking", "message": message})

    def _err(self, tool: str, exc: Exception) -> str:
        logger.exception("VoiceAgent: tool error", {"tool": tool, "user_id": self._user_id, "error": str(exc)})
        return json.dumps({"error": str(exc)})

    def _timeout(self, tool: str) -> str:
        logger.error("VoiceAgent: tool timed out", {"tool": tool, "user_id": self._user_id})
        return json.dumps({"error": "timed out — please try again"})

    # Reminders

    @function_tool
    async def set_reminder(self, message: str, delay_minutes: float, priority: str = "normal") -> str:
        """Set a reminder for the user that fires after delay_minutes minutes."""
        try:
            await self._thinking("Setting the reminder.")
            result = await asyncio.wait_for(
                self._executor.execute("set_reminder", {"message": message, "delay_minutes": delay_minutes, "priority": priority}),
                timeout=_TOOL,
            )
            return self._ok(result)
        except asyncio.TimeoutError:
            return self._timeout("set_reminder")
        except Exception as exc:
            return self._err("set_reminder", exc)

    @function_tool
    async def list_reminders(self, status_filter: str = "pending") -> str:
        """List the user's reminders. status_filter: 'pending', 'all', 'fired', 'dismissed'."""
        try:
            await self._thinking("Checking reminders.")
            result = await asyncio.wait_for(
                self._executor.execute("list_reminders", {"status_filter": status_filter}),
                timeout=_TOOL,
            )
            return self._ok(result)
        except asyncio.TimeoutError:
            return self._timeout("list_reminders")
        except Exception as exc:
            return self._err("list_reminders", exc)

    @function_tool
    async def cancel_reminder(self, reminder_id: str) -> str:
        """Cancel (dismiss) a reminder by its ID."""
        try:
            await self._thinking("Canceling that reminder.")
            result = await asyncio.wait_for(
                self._executor.execute("cancel_reminder", {"reminder_id": reminder_id}),
                timeout=_TOOL,
            )
            return self._ok(result)
        except asyncio.TimeoutError:
            return self._timeout("cancel_reminder")
        except Exception as exc:
            return self._err("cancel_reminder", exc)

    # Calendar

    @function_tool
    async def create_calendar_event(
        self,
        title: str,
        start_time: str,
        end_time: str = "",
        description: str = "",
        location: str = "",
    ) -> str:
        """Create a Google Calendar event. start_time and end_time are ISO 8601 strings."""
        try:
            await self._thinking("Creating the calendar event.")
            result = await asyncio.wait_for(
                self._executor.execute("create_calendar_event", {
                    "title": title,
                    "start_time": start_time,
                    "end_time": end_time or None,
                    "description": description or None,
                    "location": location or None,
                }),
                timeout=_TOOL,
            )
            return self._ok(result)
        except asyncio.TimeoutError:
            return self._timeout("create_calendar_event")
        except Exception as exc:
            return self._err("create_calendar_event", exc)

    @function_tool
    async def get_upcoming_events(self, hours_ahead: int = 24, limit: int = 10) -> str:
        """Fetch upcoming Google Calendar events within the next hours_ahead hours."""
        try:
            await self._thinking("Checking your calendar.")
            result = await asyncio.wait_for(
                self._executor.execute("get_upcoming_events", {"hours_ahead": hours_ahead, "limit": limit}),
                timeout=_TOOL,
            )
            return self._ok(result)
        except asyncio.TimeoutError:
            return self._timeout("get_upcoming_events")
        except Exception as exc:
            return self._err("get_upcoming_events", exc)

    # Memory

    @function_tool
    async def store_memory(self, key: str, value: str, category: str) -> str:
        """Store a memory about the user. category: 'personal', 'preference', 'fact', etc."""
        try:
            await self._thinking("Saving that memory.")
            result = await asyncio.wait_for(
                self._executor.execute("store_memory", {"key": key, "value": value, "category": category}),
                timeout=_TOOL,
            )
            return self._ok(result)
        except asyncio.TimeoutError:
            return self._timeout("store_memory")
        except Exception as exc:
            return self._err("store_memory", exc)

    @function_tool
    async def query_memory(self, query: str, category_filter: str = "all") -> str:
        """Search the user's memories. category_filter: 'all' or a specific category."""
        try:
            await self._thinking("Searching memory.")
            result = await asyncio.wait_for(
                self._executor.execute("query_memory", {"query": query, "category_filter": category_filter}),
                timeout=_TOOL,
            )
            return self._ok(result)
        except asyncio.TimeoutError:
            return self._timeout("query_memory")
        except Exception as exc:
            return self._err("query_memory", exc)

    # Nutrition

    @function_tool
    async def analyze_nutrition(
        self,
        ocr_text: str,
        quantity: float = 1.0,
        occasion: str = "",
        is_cheat_meal: bool = False,
    ) -> str:
        """Analyze nutrition information from a food label's OCR text."""
        try:
            await self._thinking("Reading the nutrition label.")
            result = await asyncio.wait_for(
                self._executor.execute("analyze_nutrition", {
                    "ocr_text": ocr_text,
                    "quantity": quantity,
                    "occasion": occasion or None,
                    "is_cheat_meal": is_cheat_meal,
                }),
                timeout=_TOOL,
            )
            return self._ok(result)
        except asyncio.TimeoutError:
            return self._timeout("analyze_nutrition")
        except Exception as exc:
            return self._err("analyze_nutrition", exc)

    # User context

    @function_tool
    async def get_user_context(
        self,
        include_memories: bool = True,
        include_reminders: bool = True,
        include_events: bool = True,
    ) -> str:
        """Get a snapshot of the user's memories, reminders, and upcoming calendar events."""
        try:
            await self._thinking("Checking your context.")
            result = await asyncio.wait_for(
                self._executor.execute("get_user_context", {
                    "include_memories": include_memories,
                    "include_reminders": include_reminders,
                    "include_events": include_events,
                }),
                timeout=_TOOL,
            )
            return self._ok(result)
        except asyncio.TimeoutError:
            return self._timeout("get_user_context")
        except Exception as exc:
            return self._err("get_user_context", exc)


def prewarm(proc: agents.JobProcess) -> None:
    logger.info("VoiceWorker: prewarming VAD model")
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext) -> None:
    logger.info("VoiceAgent: job dispatched", {"room": ctx.room.name})
    try:
        await asyncio.wait_for(ctx.connect(), timeout=settings.VOICE_CONNECT_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.error("VoiceAgent: room connect timed out", {"room": ctx.room.name})
        return
    except Exception as exc:
        logger.exception("VoiceAgent: room connect failed", {"room": ctx.room.name, "error": str(exc)})
        return

    user_id = ctx.room.name.removeprefix("voice-")
    if not user_id:
        logger.error("VoiceAgent: could not extract user_id from room name", {"room": ctx.room.name})
        return

    async with _voice_session_context(user_id, ctx.room.name) as session_id:
        async def publish_client_event(payload: dict) -> None:
            try:
                await ctx.room.local_participant.publish_data(
                    json.dumps(payload),
                    reliable=True,
                    destination_identities=[user_id],
                )
            except Exception as exc:
                logger.warn("VoiceSession: failed to publish client event", {
                    "session_id": session_id,
                    "user_id": user_id,
                    "error": str(exc),
                })

        stt_pipeline = lk_stt.FallbackAdapter(
            [
                deepgram.STT(model="nova-3", api_key=settings.DEEPGRAM_API_KEY),
                deepgram.STT(model="nova-2", api_key=settings.DEEPGRAM_API_KEY),
            ],
            attempt_timeout=10.0,
            max_retry_per_stt=1,
            retry_interval=0.5,
        )

        llm_pipeline = lk_llm.FallbackAdapter(
            [
                anthropic.LLM(model=settings.ANTHROPIC_CHAT_MODEL, api_key=settings.ANTHROPIC_API_KEY),
                google.LLM(model=settings.TIER_CHEAP, api_key=settings.GEMINI_API_KEY),
            ],
            attempt_timeout=8.0,
        )

        tts_pipeline = lk_tts.FallbackAdapter(
            [
                cartesia.TTS(api_key=settings.CARTESIA_API_KEY, model="sonic-3"),
                cartesia.TTS(api_key=settings.CARTESIA_API_KEY, model="sonic-2"),
            ],
            max_retry_per_tts=1,
        )

        session = AgentSession(
            stt=stt_pipeline,
            llm=llm_pipeline,
            tts=tts_pipeline,
            vad=ctx.proc.userdata["vad"],
            turn_handling=TurnHandlingOptions(
                turn_detection=MultilingualModel(),
                interruption={
                    "mode": "adaptive",
                    # "min_duration": 0.5,
                    # "false_interruption_timeout": 2.0,
                },
            ),
        )

        session_done = asyncio.Event()

        @session.on("agent_state_changed")
        def _on_state(ev) -> None:  # type: ignore[misc]
            logger.info("VoiceSession: agent_state_changed", {
                "session_id": session_id, "user_id": user_id,
                "state": str(ev.new_state),
            })

        @session.on("user_input_transcribed")
        def _on_user_transcript(ev) -> None:  # type: ignore[misc]
            logger.info("VoiceSession: STT transcript", {
                "session_id": session_id, "user_id": user_id,
                "text": ev.transcript, "is_final": ev.is_final,
            })

        @session.on("conversation_item_added")
        def _on_conversation_item(ev) -> None:  # type: ignore[misc]
            item = getattr(ev, "item", None)
            if item and getattr(item, "role", None) == "assistant":
                content = getattr(item, "text_content", None) or str(item)
                logger.info("VoiceSession: agent response", {
                    "session_id": session_id, "user_id": user_id,
                    "text_preview": str(content)[:120],
                })

        @session.on("session_usage_updated")
        def _on_usage(ev) -> None:  # type: ignore[misc]
            logger.info("VoiceSession: usage updated", {
                "session_id": session_id, "user_id": user_id,
                "usage": str(ev),
            })

        @session.on("close")
        def _on_close(ev) -> None:  # type: ignore[misc]
            logger.info("VoiceSession: session close event", {
                "session_id": session_id, "user_id": user_id,
                "error": str(ev.error) if getattr(ev, "error", None) else None,
            })
            session_done.set()

        try:
            await session.start(
                room=ctx.room,
                agent=BuddyAgent(user_id=user_id, publish_event=publish_client_event),
                # Unified RoomOptions replaces deprecated room_input_options / room_output_options
                room_options=room_io.RoomOptions(
                    participant_identity=user_id,
                    audio_input=room_io.AudioInputOptions(
                        sample_rate=16000,
                        frame_size_ms=20,
                        # Uncomment + add `from livekit.plugins import noise_cancellation` to enable BVC:
                        # noise_cancellation=noise_cancellation.BVC(),
                    ),
                    audio_output=room_io.AudioOutputOptions(
                        sample_rate=24000,  # Cartesia output rate
                    ),
                    # text_output=True is the default — keeps transcription working
                ),
            )
            # session.start() returns immediately in livekit-agents 1.5.x.
            # Wait for the close event so the entrypoint stays alive for the
            # full duration of the session.
            await session_done.wait()
        except Exception as exc:
            logger.exception("VoiceSession: session.start() failed", {
                "session_id": session_id, "user_id": user_id,
                "error_type": type(exc).__name__, "error": str(exc),
            })
            raise


if __name__ == "__main__":
    logger.info("VoiceWorker: starting", {
        "livekit_url": settings.LIVEKIT_URL,
        "livekit_configured": settings.livekit_configured,
        "deepgram_configured": bool(settings.DEEPGRAM_API_KEY),
        "cartesia_configured": bool(settings.CARTESIA_API_KEY),
        "anthropic_configured": bool(settings.ANTHROPIC_API_KEY),
    })
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            max_retry=3,
        )
    )
