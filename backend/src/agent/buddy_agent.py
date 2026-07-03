"""
BuddyAgent — the persona that drives the LiveKit voice session.

Tools are exposed via MCP at /mcp (see backend/src/handlers/mcp.py) and are
not declared on this class. Lifecycle:

* on_enter -> open with one casual greeting from CASUAL_GREETINGS, spoken
              verbatim. The opener never references history; what Buddy knows
              about the user surfaces mid-conversation only when relevant (see
              the "Using what you know" section of the voice prompt).

Per-tool thinking phrases are spoken from voice_agent.py's conversation_item_added handler,
gated on agent_state == "thinking" so they never overlap the real reply.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterable

from livekit import agents
from livekit.agents import Agent, ModelSettings
from livekit.agents import llm as lk_llm

from .voice.point_tag import PointTarget, filter_point_tags, publish_element_point
from .voice.screen_frames import ScreenFrameStore, attach_screen_frame_to_turn
from .voice.text_sanitizer import sanitize_text_stream
from .voice_prompt import VOICE_PROMPT

# Spoken verbatim as the opener, picked at random. Every line is a safe, warm
# hello on its own, so the greeting never depends on a stale memory or last-session
# summary being relevant. Kept lowercase/contracted to read naturally through TTS.
CASUAL_GREETINGS = [
    "whatsup buddy",
    "what's going on",
    "Yooo!! what's good",
    "Heyyy, what's happening",
    "how you doin",
    "hey, what's up",
    "how's it going buddy",
    "what's new with you",
    "hey you, sup?",
]


class BuddyAgent(agents.Agent):
    def __init__(
        self,
        *,
        user_id: str,
        context_vars: dict[str, str],
        chat_ctx: lk_llm.ChatContext,
        screen_frames: ScreenFrameStore | None = None,
        session_id: str = "",
    ) -> None:
        super().__init__(
            instructions=VOICE_PROMPT.format(**context_vars),
            chat_ctx=chat_ctx,
        )
        self._user_id = user_id
        self._screen_frames = screen_frames
        self._session_id = session_id
        # The frame injected into the current turn; element.point events carry
        # its id so the client maps coordinates against the right geometry.
        self._last_injected_frame_id = ""
        self._point_publish_tasks: set[asyncio.Task] = set()

    async def on_enter(self) -> None:
        await self.session.say(random.choice(CASUAL_GREETINGS))

    async def on_user_turn_completed(
        self, turn_ctx: lk_llm.ChatContext, new_message: lk_llm.ChatMessage
    ) -> None:
        """Attach the desktop's screen frame to the turn when screen sight is armed.

        A session with no frames passes through untouched, which keeps preemptive
        generation intact (see screen_frames.py for why that matters). The helper
        never raises; a raised hook would drop the whole turn reply.
        """
        if self._screen_frames is not None:
            frame = await attach_screen_frame_to_turn(
                self._screen_frames,
                turn_ctx,
                new_message,
                session_id=self._session_id,
                user_id=self._user_id,
            )
            self._last_injected_frame_id = frame.frame_id if frame else ""

    async def llm_node(
        self,
        chat_ctx: lk_llm.ChatContext,
        tools: list,
        model_settings: ModelSettings,
    ):
        """Strip [POINT:...] tags from the reply stream before ANY consumer sees them.

        llm_node output feeds TTS, the client captions, and the recorded
        transcript, so this single interception keeps the tag out of all three.
        The first coordinate tag per reply publishes an element.point event the
        desktop overlay animates. Sessions without screen sight pass through
        the same filter as a cheap no-op (no '[' in normal speech).
        """
        published = False

        def _on_point(target: PointTarget) -> None:
            nonlocal published
            if published:
                return  # one pointer per reply; extra tags are stripped silently
            published = True
            task = asyncio.create_task(
                publish_element_point(
                    target,
                    frame_id=self._last_injected_frame_id,
                    session_id=self._session_id,
                    user_id=self._user_id,
                ),
                name=f"voice-point-{self._session_id[:8]}",
            )
            self._point_publish_tasks.add(task)
            task.add_done_callback(self._point_publish_tasks.discard)

        stream = Agent.default.llm_node(self, chat_ctx, tools, model_settings)
        async for item in filter_point_tags(stream, on_point=_on_point):
            yield item

    async def tts_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ):
        """Strip markdown from the reply stream before it reaches Cartesia.

        gpt-4.1-mini frequently emits bold/bullets/headers on a voice call; without this,
        TTS reads the markup literally ("asterisk asterisk content"). The sanitizer is
        deterministic and fail-open (see voice/text_sanitizer.py), and flushes per sentence
        so synthesis stays incremental. We then delegate to the default TTS node.
        """
        cleaned = sanitize_text_stream(text)
        async for frame in Agent.default.tts_node(self, cleaned, model_settings):
            yield frame
