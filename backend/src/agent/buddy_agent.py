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

import random

from livekit import agents
from livekit.agents import llm as lk_llm

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
    ) -> None:
        super().__init__(
            instructions=VOICE_PROMPT.format(**context_vars),
            chat_ctx=chat_ctx,
        )
        self._user_id = user_id

    async def on_enter(self) -> None:
        await self.session.say(random.choice(CASUAL_GREETINGS))
