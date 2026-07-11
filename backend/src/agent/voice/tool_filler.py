"""Spoken filler for slow tool calls, so Buddy never leaves dead air.

The trigger lives in ``BuddyAgent.llm_node``: a tool call surfacing in the LLM
stream (``ChatChunk.delta.tool_calls``) is the ONLY signal that fires before
the framework executes the tool. Session events cannot do this job:
``conversation_item_added`` never carries tool calls on this stack and
``function_tools_executed`` fires after the tool returns (see the recorder
history for the dead path this replaces).

Timing safety, verified against livekit-agents 1.5.8 scheduling
(agent_activity: the generation marks itself done before awaiting tool
execution): a ``say()`` issued while a tool is executing plays immediately and
the post-tool reply politely queues behind it; a ``say()`` issued during plain
LLM streaming would queue behind the WHOLE reply and play after the answer.
That is why the speaker below only fires once the session is actually in the
"thinking" state (a preemptive/speculative generation that gets discarded
never reaches it) and why only tools slow enough to still be running when the
filler lands are listed here. Fast tools (set_reminder, store_memory, ...)
stay silent on purpose: a filler would only delay their instant confirmation.

Known quirk, accepted: the filler playout flips agent_state to "speaking" and
back mid-tool. The away nudge needs a long user silence, so it cannot misfire
from this.
"""

from __future__ import annotations

import asyncio
import random
import time

from livekit.agents import AgentSession

from ...lib.logger import logger

# Tools whose round-trip is long enough (multi-second LLM/vision/web work, up
# to the 8s MCP cap) that the user needs to hear Buddy acknowledge before the
# real reply. Keep every phrase short: it must finish before the tool does.
SLOW_TOOL_THINKING_PHRASES: dict[str, list[str]] = {
    # draft_outbound_message is deliberately NOT listed: it is an async tool
    # (ctx.update in draft_outbound.py speaks a contextual acknowledgment in
    # Buddy's persona); a canned phrase here would double-speak on top of it.
    "create_calendar_event": [
        "cool, popping that on your calendar",
        "alright, getting that on the calendar",
        "on it, adding that now",
    ],
    "query_memory": [
        "lemme think back for a sec",
        "digging through what I remember",
        "one sec, jogging my memory",
    ],
    "get_user_context": [
        "lemme pull up your stuff real quick",
        "one sec, grabbing your details",
    ],
    "web_surf": [
        "ooh good question, lemme look that up",
        "hang on, let me actually check that",
        "one sec, looking that up real quick",
        "lemme make sure I get this right",
    ],
}

# Spoken while the draft tool's vision call is still running after the first
# filler has landed (see draft_outbound_message in buddy_agent.py). The delay
# only ever trips on the genuinely slow expert-vision path: the ask-length and
# refine paths finish well inside it.
DRAFT_STILL_WORKING_PHRASES: list[str] = [
    "still on it, almost there",
    "almost done with it, hang tight",
]
# Idle dwell before the first "still on it" line, and the cooldown between
# subsequent ones. Both feed ctx.with_filler in draft_outbound.py.
DRAFT_STILL_WORKING_DELAY_S = 6.0
DRAFT_FILLER_INTERVAL_S = 8.0

# One filler per stretch of tool work: chained tool rounds inside this window
# stay silent so Buddy doesn't stack "one sec" on "one sec".
_FILLER_DEDUP_WINDOW_S = 4.0

# How long to wait for the generation that carried the tool call to be
# committed (agent_state == "thinking"). Preemptive speculative generations
# that get discarded never reach that state, so hitting this cap means the
# turn was thrown away and the filler must not speak.
_WAIT_FOR_THINKING_CAP_S = 5.0
_WAIT_FOR_THINKING_POLL_S = 0.05


class ToolFillerSpeaker:
    """Speaks one short acknowledgment when a slow tool starts executing."""

    def __init__(self, *, session: AgentSession, session_id: str, user_id: str) -> None:
        self._session = session
        self._session_id = session_id
        self._user_id = user_id
        self._last_filler_monotonic = 0.0
        self._speak_tasks: set[asyncio.Task] = set()

    def speak_for_tool(self, tool_name: str) -> None:
        """Fire-and-forget a filler for ``tool_name``. Safe to call per chunk.

        Unknown (fast) tools and repeat calls inside the dedupe window are
        silent no-ops. Never raises: a broken filler must not touch the turn.
        """
        phrases = SLOW_TOOL_THINKING_PHRASES.get(tool_name)
        if not phrases:
            return
        now = time.monotonic()
        if (now - self._last_filler_monotonic) < _FILLER_DEDUP_WINDOW_S:
            return
        self._last_filler_monotonic = now

        task = asyncio.create_task(
            self._speak(random.choice(phrases), tool_name),
            name=f"tool-filler-{tool_name}-{self._session_id[:8]}",
        )
        self._speak_tasks.add(task)
        task.add_done_callback(self._speak_tasks.discard)

    async def _speak(self, phrase: str, tool_name: str) -> None:
        try:
            if not await self._wait_until_thinking():
                logger.info("VoiceSession: tool filler skipped (never reached thinking)", {
                    "session_id": self._session_id, "user_id": self._user_id,
                    "tool": tool_name,
                })
                return
            await self._session.say(
                phrase, allow_interruptions=True, add_to_chat_ctx=False
            )
            logger.info("VoiceSession: tool filler spoken", {
                "session_id": self._session_id, "user_id": self._user_id,
                "tool": tool_name, "phrase": phrase,
            })
        except Exception as exc:
            logger.warn("VoiceSession: tool filler failed", {
                "session_id": self._session_id, "user_id": self._user_id,
                "tool": tool_name, "error": str(exc),
            })

    async def _wait_until_thinking(self) -> bool:
        deadline = time.monotonic() + _WAIT_FOR_THINKING_CAP_S
        while time.monotonic() < deadline:
            if str(getattr(self._session, "agent_state", "")) == "thinking":
                return True
            await asyncio.sleep(_WAIT_FOR_THINKING_POLL_S)
        return False
