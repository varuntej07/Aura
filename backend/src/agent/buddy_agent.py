"""
BuddyAgent — the persona that drives the LiveKit voice session.

Tools are exposed via MCP at /mcp (see backend/src/handlers/mcp.py) and are
not declared on this class. Lifecycle:

* on_enter -> greet by name and optionally reference one memory or the
              last session.

Per-tool thinking phrases are spoken from voice_agent.py's conversation_item_added handler,
gated on agent_state == "thinking" so they never overlap the real reply.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

from livekit import agents
from livekit.agents import llm as lk_llm

from .voice_prompt import VOICE_PROMPT


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
