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
from ...services.analytics.llm_telemetry import start_llm_generation
from .action_policy import tool_output_succeeded
from .errors import classify_pipeline_error, publish_client_error
from .telemetry import log_turn_metrics, log_voice_failure
from .text_sanitizer import strip_nonverbal_cues

# Slow-tool filler phrases moved to voice/tool_filler.py, triggered from
# BuddyAgent.llm_node (the only pre-execution tool signal on this stack).

# Spoken (LLM-framed) when the user has gone silent long enough. Kept deliberately
# open so the line lands fresh each time: the earlier, more prescriptive version
# converged on the same stock "you still there? no rush" phrasing every session.
AWAY_NUDGE_INSTRUCTIONS = (
    "The user has gone quiet for a bit. In Buddy's warm, casual voice, gently check "
    "whether they're still there with ONE short, low-pressure line. Make it feel "
    "spontaneous and specific to this moment: if you two were mid-conversation, "
    "lightly reference what you were just talking about; otherwise a light, friendly "
    "check-in. Vary the wording naturally every time and never fall back on a stock "
    "phrase like 'you still there? no rush.' No guilt, no list of questions."
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
        tool_observer: object | None = None,
    ) -> None:
        self._session = session
        self._ctx = ctx
        self._session_id = session_id
        self._user_id = user_id
        self._user_tier = user_tier
        self._tool_observer = tool_observer
        self.turns: list[dict] = []
        self.tool_calls: list[str] = []
        self.done = asyncio.Event()
        # Latest CUMULATIVE per-model LLM usage (session_usage_updated re-emits
        # running totals every turn); flushed to Langfuse once at session close
        # as one per-session generation per model.
        self._model_usage_totals: dict[str, dict] = {}

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
            # text_content is the raw llm_node output, which still carries the
            # [laughter] TTS cue (only the caption path strips it). Drop it here
            # too so post-session summaries never quote a "[laughter]" back.
            content = strip_nonverbal_cues(getattr(item, "text_content", None) or str(item))
            logger.info("VoiceSession: agent response", {
                "session_id": self._session_id, "user_id": self._user_id,
                "text_preview": str(content)[:120],
            })
            self.turns.append({
                "role": "assistant",
                "text": str(content)[:500],
                "timestamp": datetime.now(UTC).isoformat(),
            })
            observer = self._tool_observer
            record_item = getattr(observer, "record_voice_conversation_item", None)
            if callable(record_item):
                record_item(item)

        # Tool-call CAPTURE lives in _on_tools_executed (the function_tools_executed
        # event), the only session event that actually carries tool names on this stack
        # (ChatMessage items have no tool_calls field). The spoken slow-tool filler fires
        # pre-execution from BuddyAgent.llm_node via voice/tool_filler.py, since
        # function_tools_executed fires AFTER the tool returns.

    def _on_tools_executed(self, ev) -> None:  # type: ignore[misc]
        # Authoritative tool-call capture. function_tools_executed is the only session event
        # that carries executed tool names (each FunctionCall has .name); it fires after each
        # tool round-trip completes, which is correct for post-session analytics. This replaces
        # the old conversation_item_added/item.tool_calls path, which never populated anything
        # because ChatMessage items carry no tool data on the gpt-4.1-mini path.
        function_calls = getattr(ev, "function_calls", None) or []
        outputs = getattr(ev, "function_call_outputs", None) or []
        for index, fnc_call in enumerate(function_calls):
            name = getattr(fnc_call, "name", "") or ""
            if name:
                self.tool_calls.append(name)
                output = outputs[index] if index < len(outputs) else None
                success = output is not None and tool_output_succeeded(output)
                observer = self._tool_observer
                record = getattr(observer, "record_voice_tool_execution", None)
                if callable(record):
                    record(name, success=success)
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
            model = str(getattr(mu, "model", "") or "")
            if model:
                # Overwrite, never add: these are running totals for the session.
                self._model_usage_totals[model] = {
                    "provider": str(getattr(mu, "provider", "") or ""),
                    "input_tokens": int(input_tokens or 0),
                    "cached_tokens": int(cached_tokens or 0),
                    "output_tokens": int(getattr(mu, "output_tokens", 0) or 0),
                }

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
        self._record_session_llm_usage()
        close_context = getattr(self._tool_observer, "close_voice_context", None)
        if callable(close_context):
            close_context()
        self.done.set()

    def _record_session_llm_usage(self) -> None:
        """Emit one Langfuse generation per model with the session's FINAL
        cumulative token totals (voice cost is tracked per session, not per
        turn — the LiveKit stack exposes no per-call provider hook). LiveKit's
        input_tokens INCLUDES the cached subset, so cached is subtracted out of
        input (Langfuse prices each usage-detail key separately). Best-effort:
        telemetry never blocks or breaks session close."""
        for model, totals in self._model_usage_totals.items():
            recording = start_llm_generation(
                model=model,
                provider=totals.get("provider", ""),
                caller="voice_session",
                uid=self._user_id,
            )
            cached = int(totals.get("cached_tokens", 0) or 0)
            tokens = {
                "input": max(0, int(totals.get("input_tokens", 0) or 0) - cached),
                "output": int(totals.get("output_tokens", 0) or 0),
            }
            if cached:
                tokens["cache_read_input_tokens"] = cached
            recording.finish(tokens=tokens)
