"""Per-session event recorder.

Owns the mutable state a voice session accumulates (transcript turns, tool-call
names, the done signal) and the AgentSession event handlers that fill it. Call
`attach()` once after the session is built to wire every handler, then read
`turns` / `tool_calls` after `done` is set to feed the post-session pipeline.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from livekit.agents import AgentSession, JobContext

from ...lib.logger import logger
from .errors import classify_pipeline_error, publish_client_error
from .telemetry import log_turn_metrics, log_voice_failure

# Spoken in parallel with each MCP tool round-trip so the user hears on-line
# feedback that the agent is working on it.
TOOL_THINKING_PHRASES: dict[str, str] = {
    "get_upcoming_events": "Alright pulling up your calendar right now!",
    "create_calendar_event": "Cool, adding that to your calendar now!",
    "set_reminder": "Gotcha, setting that reminder for you!",
    "cancel_reminder": "Heard that, taking care of that reminder now...",
    "list_reminders": "pulling up your reminders for you!",
    "store_memory": "Ah huh, got it, I'll keep that in mind!",
    "query_memory": "thinking through what I remember about you...",
    "get_user_context": "pulling up your details for this!",
    "web_surf": "Alright, Lemme surf the web for that!",
    "list_emails": "checking your inbox right now!",
    "read_email": "opening that email for you...",
    "send_email": "alright, firing off that email now!",
}


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
        self._session.on("user_input_transcribed", self._on_user_transcript)
        self._session.on("conversation_item_added", self._on_conversation_item)
        self._session.on("session_usage_updated", self._on_usage)
        self._session.on("error", self._on_session_error)
        self._session.on("close", self._on_close)

    def _on_state(self, ev) -> None:  # type: ignore[misc]
        state = str(getattr(ev, "new_state", ""))
        logger.info("VoiceSession: agent_state_changed", {
            "session_id": self._session_id, "user_id": self._user_id,
            "state": state,
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

        # Fire a per-tool phrase in parallel with the MCP round-trip.
        # Gated on agent_state == "thinking" at fire-time so a phrase never
        # lands on top of the model's actual reply if the tool returns fast.
        tool_calls = getattr(item, "tool_calls", None) or []
        if tool_calls:
            tool_name = getattr(tool_calls[0], "name", "") or ""
            if tool_name:
                self.tool_calls.append(tool_name)
            phrase = TOOL_THINKING_PHRASES.get(tool_name)
            if phrase:
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
