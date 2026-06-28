"""Per-session event recorder.

Owns the mutable state a voice session accumulates (transcript turns, tool-call
names, the done signal) and the AgentSession event handlers that fill it. Call
`attach()` once after the session is built to wire every handler, then read
`turns` / `tool_calls` after `done` is set to feed the post-session pipeline.
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime

from livekit.agents import AgentSession, JobContext

from ...lib.logger import logger
from .errors import classify_pipeline_error, publish_client_error
from .telemetry import log_turn_metrics, log_voice_failure

# Spoken in parallel with each MCP tool round-trip so the user hears live
# feedback that the agent is working on it.
TOOL_THINKING_PHRASES: dict[str, list[str]] = {
    "get_upcoming_events": [
        "lemme peek at your calendar",
        "one sec, pulling up your schedule",
        "checking what you've got coming up",
    ],
    "create_calendar_event": [
        "cool, popping that on your calendar",
        "alright, getting that on the calendar",
        "on it, adding that now",
    ],
    "set_reminder": [
        "gotcha, setting that up",
        "say no more, locking that in",
        "yep, setting that reminder now",
    ],
    "cancel_reminder": [
        "yep, clearing that one out",
        "on it, scrapping that reminder",
        "gotcha, getting rid of that one",
    ],
    "track_topic": [
        "ooh nice, I'll keep tabs on that for you",
        "gotcha, I'll keep you posted on that",
        "say less, I'm on it, I'll keep you in the loop",
    ],
    "list_reminders": [
        "lemme pull up what you've got",
        "one sec, grabbing your reminders",
        "checking your reminders real quick",
    ],
    "store_memory": [
        "ooh good to know, filing that away",
        "noted, I'll hang onto that",
        "got it, tucking that away",
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
    "list_emails": [
        "lemme peek at your inbox",
        "one sec, checking your inbox",
    ],
    "read_email": [
        "pulling that email up now",
        "one sec, opening that up",
    ],
    "send_email": [
        "alright, sending that off",
        "on it, firing that email out",
    ],
}

# Spoken (LLM-framed) when the user has gone silent long enough
AWAY_NUDGE_INSTRUCTIONS = (
    "The user has gone quiet for a little while. In Buddy's warm, casual voice, "
    "gently check whether they're still around. Keep it to one short, low-pressure "
    "line, no guilt, no list of questions."
)


class VoiceSessionRecorder:
    """Accumulates transcript/tool state and bridges session events to telemetry + the client."""

    def __init__(
        self,
        *,
        session: AgentSession,
        ctx: JobContext,
        session_id: str,
        user_id: str,
        user_tier: str,
    ) -> None:
        self._session = session
        self._ctx = ctx
        self._session_id = session_id
        self._user_id = user_id
        self._user_tier = user_tier
        self.turns: list[dict] = []
        self.tool_calls: list[str] = []
        self.done = asyncio.Event()

    def attach(self) -> None:
        """Register every handler on the session. Call once, after construction."""
        self._session.on("agent_state_changed", self._on_state)
        self._session.on("user_state_changed", self._on_user_state)
        self._session.on("user_input_transcribed", self._on_user_transcript)
        self._session.on("conversation_item_added", self._on_conversation_item)
        self._session.on("function_tools_executed", self._on_tools_executed)
        self._session.on("session_usage_updated", self._on_usage)
        self._session.on("error", self._on_session_error)
        self._session.on("close", self._on_close)

    def _on_state(self, ev) -> None:  # type: ignore[misc]
        state = str(getattr(ev, "new_state", ""))
        logger.info("VoiceSession: agent_state_changed", {
            "session_id": self._session_id, "user_id": self._user_id,
            "state": state,
        })

    def _on_user_state(self, ev) -> None:  # type: ignore[misc]
        new_state = str(getattr(ev, "new_state", ""))
        logger.info("VoiceSession: user_state_changed", {
            "session_id": self._session_id, "user_id": self._user_id,
            "state": new_state,
        })
        if new_state != "away":
            return
        # Gate on agent being idle so the nudge never lands on top of Buddy already
        # speaking or mid tool-call (same guard the tool-thinking phrase uses).
        if str(getattr(self._session, "agent_state", "")) != "listening":
            logger.info("VoiceSession: away nudge skipped (agent not listening)", {
                "session_id": self._session_id, "user_id": self._user_id,
                "agent_state": str(getattr(self._session, "agent_state", "")),
            })
            return

        async def _nudge() -> None:
            try:
                await self._session.generate_reply(instructions=AWAY_NUDGE_INSTRUCTIONS)
            except Exception as exc:
                logger.warn("VoiceSession: away nudge failed", {
                    "session_id": self._session_id, "user_id": self._user_id,
                    "error": str(exc),
                })

        asyncio.create_task(_nudge(), name=f"away-nudge-{self._session_id[:8]}")
        logger.info("VoiceSession: away nudge", {
            "session_id": self._session_id, "user_id": self._user_id,
        })

    def _on_user_transcript(self, ev) -> None:  # type: ignore[misc]
        logger.info("VoiceSession: STT transcript", {
            "session_id": self._session_id, "user_id": self._user_id,
            "text": ev.transcript, "is_final": ev.is_final,
        })
        if ev.is_final and ev.transcript:
            self.turns.append({
                "role": "user",
                "text": ev.transcript,
                "timestamp": datetime.now(UTC).isoformat(),
            })

    def _on_conversation_item(self, ev) -> None:  # type: ignore[misc]
        item = getattr(ev, "item", None)
        if item is None:
            return

        role = getattr(item, "role", None)

        # Per-turn component telemetry. LiveKit populates ChatMessage.metrics
        # before this event fires (user turns: endpointing + STT;
        # assistant turns: LLM TTFT, TTS TTFB, EOU->first-audio)
        metrics = getattr(item, "metrics", None)
        if isinstance(metrics, dict) and metrics and role in ("user", "assistant"):
            log_turn_metrics(
                session_id=self._session_id,
                user_id=self._user_id,
                role=role,
                metrics=metrics,
                tier=self._user_tier,
            )

        if role == "assistant":
            content = getattr(item, "text_content", None) or str(item)
            logger.info("VoiceSession: agent response", {
                "session_id": self._session_id, "user_id": self._user_id,
                "text_preview": str(content)[:120],
            })
            self.turns.append({
                "role": "assistant",
                "text": str(content)[:500],
                "timestamp": datetime.now(UTC).isoformat(),
            })

        # Tool-call CAPTURE now lives in _on_tools_executed (the function_tools_executed
        # event), the only signal that actually carries tool names on this stack. ChatMessage
        # items have no tool_calls field, so the path below never populated self.tool_calls.
        #
        # The per-tool "thinking" phrase below is dead for the same reason (item.tool_calls is
        # always empty here) and is intentionally LEFT IN PLACE pending the post-demo
        # filler-trigger decision. Do not build on it: function_tools_executed fires AFTER the
        # tool returns, and no public session event carries the tool name pre-execution.
        tool_calls = getattr(item, "tool_calls", None) or []
        if tool_calls:
            tool_name = getattr(tool_calls[0], "name", "") or ""
            phrases = TOOL_THINKING_PHRASES.get(tool_name)
            if phrases:
                phrase = random.choice(phrases)
                async def _speak_tool_phrase(p: str = phrase, name: str = tool_name) -> None:
                    if str(getattr(self._session, "agent_state", "")) != "thinking":
                        logger.info("VoiceSession: tool phrase skipped (not thinking)", {
                            "session_id": self._session_id, "user_id": self._user_id,
                            "tool": name,
                        })
                        return
                    try:
                        await self._session.say(p, allow_interruptions=True, add_to_chat_ctx=False)
                    except Exception as exc:
                        logger.warn("VoiceSession: tool phrase failed", {
                            "session_id": self._session_id, "user_id": self._user_id,
                            "error": str(exc),
                        })

                asyncio.create_task(
                    _speak_tool_phrase(),
                    name=f"tool-phrase-{tool_name}-{self._session_id[:8]}",
                )
                logger.info("VoiceSession: tool thinking phrase", {
                    "session_id": self._session_id, "user_id": self._user_id,
                    "tool": tool_name,
                })

    def _on_tools_executed(self, ev) -> None:  # type: ignore[misc]
        # Authoritative tool-call capture. function_tools_executed is the only session event
        # that carries executed tool names (each FunctionCall has .name); it fires after each
        # tool round-trip completes, which is correct for post-session analytics. This replaces
        # the old conversation_item_added/item.tool_calls path, which never populated anything
        # because ChatMessage items carry no tool data on the gpt-4.1-mini path.
        function_calls = getattr(ev, "function_calls", None) or []
        for fnc_call in function_calls:
            name = getattr(fnc_call, "name", "") or ""
            if name:
                self.tool_calls.append(name)
                logger.info("VoiceSession: tool executed", {
                    "session_id": self._session_id, "user_id": self._user_id,
                    "tool": name,
                })

    def _on_usage(self, ev) -> None:  # type: ignore[misc]
        # Cumulative per-model token counts, re-emitted after every turn.
        # once the session has more than one turn, input_cached_tokens must climb. If
        # it stays 0, the long voice system prompt is being re-billed at full input price.
        usage = getattr(ev, "usage", None)
        model_usage = getattr(usage, "model_usage", None) or []
        for mu in model_usage:
            if getattr(mu, "type", "") != "llm_usage":
                continue
            input_tokens = getattr(mu, "input_tokens", 0)
            cached_tokens = getattr(mu, "input_cached_tokens", 0)
            cache_hit_pct = round(100 * cached_tokens / input_tokens, 1) if input_tokens else 0.0
            logger.info("VoiceSession: llm usage", {
                "session_id": self._session_id, "user_id": self._user_id,
                "model": getattr(mu, "model", ""),
                "provider": getattr(mu, "provider", ""),
                "input_tokens": input_tokens,
                "input_cached_tokens": cached_tokens,
                "cache_hit_pct": cache_hit_pct,
                "output_tokens": getattr(mu, "output_tokens", 0),
            })

    def _on_session_error(self, ev) -> None:  # type: ignore[misc]
        error = getattr(ev, "error", None) or ev
        logger.error("VoiceSession: AgentSession runtime error", {
            "session_id": self._session_id, "user_id": self._user_id,
            "error_type": type(error).__name__,
            "error": str(error),
        })
        # Tell the user. Without this the client just sees a stuck "Listening"
        # screen until its own silence watchdog trips — this is the fast path.
        code, message = classify_pipeline_error(str(error))
        asyncio.create_task(
            publish_client_error(self._ctx, code, message),
            name=f"voice-client-error-{self._session_id[:8]}",
        )

    def _on_close(self, ev) -> None:  # type: ignore[misc]
        close_error = getattr(ev, "error", None)
        logger.info("VoiceSession: session close event", {
            "session_id": self._session_id, "user_id": self._user_id,
            "error": str(close_error) if close_error else None,
        })
        if close_error:
            code, message = classify_pipeline_error(str(close_error))
            log_voice_failure(
                code=code,
                user_id=self._user_id,
                room_name=self._ctx.room.name,
                session_id=self._session_id,
                exc=Exception(str(close_error)),
            )
            # Best-effort nudge to the client in case the runtime "error"
            # event didn't fire (some failures only surface at close time).
            asyncio.create_task(
                publish_client_error(self._ctx, code, message),
                name=f"voice-client-error-close-{self._session_id[:8]}",
            )
        self.done.set()
