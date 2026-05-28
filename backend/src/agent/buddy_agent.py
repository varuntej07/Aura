"""
BuddyAgent — the persona that drives the LiveKit voice session.

Tools are exposed via MCP at /mcp (see backend/src/handlers/mcp.py) and are
not declared on this class. Lifecycle:

* on_enter      -> greet by name and optionally reference one memory.
* on_user_turn_completed -> for non-trivial utterances, arm a 600ms timer
                            that fires "mm-hmm..." as on-line presence if
                            the LLM is still 'thinking' by then. The timer
                            is cancelled the moment the agent transitions
                            to 'speaking'.
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime

from livekit import agents
from livekit.agents import llm as lk_llm

from ..lib.logger import logger
from .voice_prompt import VOICE_PROMPT

# Below this word count the user's turn is treated as a quick prompt
# (greeting, yes/no) where the filler would land on top of the reply.
_FILLER_MIN_WORDS = 6

# How long we let the LLM stay silent before injecting on-line presence.
# 1.2s gives background filler clips (max ~700ms) room to finish before
# this TTS phrase fires, so the two never overlap on the wire.
_FILLER_DELAY_S = 1.2

_FILLER_PHRASES = [
    "mm-hmm,<break time=\"200ms\"/> one sec...",
    "let me check on that...",
    "just a moment...",
    "hmm,<break time=\"150ms\"/> let me think...",
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
        memory_summary = context_vars.get("memory_summary", "").strip()
        self._has_memory = bool(memory_summary)
        self._memory_lines = [
            line.lstrip("- ").strip()
            for line in memory_summary.split("\n")
            if line.strip().startswith("-")
        ]
        self._last_session_summary = context_vars.get("last_session_context", "").strip()
        self._last_session_at = context_vars.get("last_session_at", "").strip()
        self._pending_filler: asyncio.Task | None = None

    def _last_session_is_recent(self) -> bool:
        if not self._last_session_at:
            return False
        try:
            last_dt = datetime.fromisoformat(self._last_session_at)
            age = datetime.now(UTC) - last_dt.replace(tzinfo=UTC if last_dt.tzinfo is None else last_dt.tzinfo)
            return age.total_seconds() < 3 * 86400
        except (ValueError, TypeError):
            return False

    async def on_enter(self) -> None:
        if self._last_session_summary and self._last_session_is_recent():
            greeting_hint = (
                f"Greet the user by name in one short sentence. "
                f"Reference something specific from your last conversation: "
                f"{self._last_session_summary[:300]}"
            )
        elif self._has_memory and self._memory_lines:
            chosen = random.choice(self._memory_lines)
            greeting_hint = (
                f"Greet the user by name in one short sentence. "
                f"Reference this specific memory in a casual question: {chosen}"
            )
        else:
            greeting_hint = "Greet the user by name in one short sentence and ask what's up."
        await self.session.generate_reply(instructions=greeting_hint)

    async def on_user_turn_completed(
        self,
        turn_ctx: lk_llm.ChatContext,
        new_message: lk_llm.ChatMessage,
    ) -> None:
        text = (new_message.text_content or "").strip()
        if not text:
            return

        # Cheap word count — quoted strings, punctuation, etc. don't matter.
        if len(text.split()) <= _FILLER_MIN_WORDS:
            return

        # Cancel any leftover task from a previous turn before scheduling a new one.
        self._cancel_pending_filler()
        self._pending_filler = asyncio.create_task(
            self._speak_filler_if_still_thinking(),
            name=f"buddy-filler-{self._user_id}",
        )

    async def _speak_filler_if_still_thinking(self) -> None:
        try:
            await asyncio.sleep(_FILLER_DELAY_S)
        except asyncio.CancelledError:
            return

        state = getattr(self.session, "agent_state", None)
        if state != "thinking":
            return

        try:
            await self.session.say(
                random.choice(_FILLER_PHRASES),
                allow_interruptions=True,
                add_to_chat_ctx=False,
            )
        except Exception as exc:
            logger.warn("BuddyAgent: filler say() failed", {
                "user_id": self._user_id,
                "error": str(exc),
            })

    def cancel_pending_filler(self) -> None:
        """Cancel the generic filler timer immediately. Called when a per-tool phrase fires."""
        self._cancel_pending_filler()

    def cancel_pending_filler_on_speaking(self, new_state: str) -> None:
        """Wired up from the session-level agent_state_changed handler so the
        filler never lands on top of the model's actual reply."""
        if new_state == "speaking":
            self._cancel_pending_filler()

    def _cancel_pending_filler(self) -> None:
        task = self._pending_filler
        if task is not None and not task.done():
            task.cancel()
        self._pending_filler = None
