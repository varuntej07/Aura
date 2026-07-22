"""Per-session event recorder.

Owns the mutable state a voice session accumulates (transcript turns, tool-call
names, the done signal) and the AgentSession event handlers that fill it. Call
`attach()` once after the session is built to wire every handler, then read
`turns` / `tool_calls` after `done` is set to feed the post-session pipeline.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from ast import literal_eval
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from livekit.agents import AgentSession, JobContext

if TYPE_CHECKING:
    from .screen_frames import ScreenFrameStore

from ...config.settings import settings
from ...lib.logger import logger
from ...services.analytics.llm_telemetry import start_llm_generation
from .action_policy import tool_output_succeeded
from .capabilities import VOICE_TOOL_REGISTRY, ToolEffect
from .errors import classify_pipeline_error, publish_client_error
from .telemetry import log_turn_metrics, log_voice_failure
from .text_sanitizer import strip_nonverbal_cues

# Slow-tool filler phrases moved to voice/tool_filler.py, triggered from
# BuddyAgent.llm_node (the only pre-execution tool signal on this stack).

# Two-tier silence presence, both LLM-framed so the line lands fresh each time
# (an earlier prescriptive version converged on the same stock "you still there?
# no rush" phrasing every session). Tier 1 fires on LiveKit's away event
# (settings.VOICE_AWAY_FIRST_NUDGE_S); tier 2 is an escalation timer that fires
# at settings.VOICE_AWAY_SECOND_NUDGE_S total silence if the user is still away.
#
# Both fire AT MOST ONCE per continuous silence. LiveKit re-emits "away" after
# every agent turn while the user stays quiet, so tier 1 is gated behind a
# `_away_nudged` latch that is only released when a real final user transcript
# arrives. Without it, each re-emitted "away" fired a fresh nudge and re-armed
# tier 2, so Buddy talked over and over during a single silence (the "why do you
# keep talking" loop).

FIRST_AWAY_NUDGE_SCREEN_INSTRUCTIONS = (
    "The user has gone quiet for a bit, and a recent screenshot of their screen is "
    "in this conversation's context. In Buddy's warm, casual voice, say ONE short, "
    "playful line. If something on that screenshot is genuinely interesting, riff on "
    "it or ask about it the way a curious friend peeking at the same screen would — "
    "you two are looking at it together, never watching them. If nothing on it is "
    "worth mentioning, just a light, friendly check-in instead. Vary the wording "
    "naturally every time; never a stock phrase like 'you still there? no rush.' "
    "No guilt, no list of questions."
)

FIRST_AWAY_NUDGE_INSTRUCTIONS = (
    "The user has gone quiet for a bit. In Buddy's warm, casual voice, gently check "
    "in with ONE short, low-pressure line. Make it feel spontaneous and specific to "
    "this moment: if you two were mid-conversation, lightly reference what you were "
    "just talking about; otherwise a light, friendly check-in. Vary the wording "
    "naturally every time and never fall back on a stock phrase like 'you still "
    "there? no rush.' No guilt, no list of questions."
)

SECOND_AWAY_NUDGE_INSTRUCTIONS = (
    "The user has stayed quiet for a while now. In Buddy's warm, playful voice, "
    "re-open the conversation with ONE short line that gives them something to bite "
    "on. If a recent screenshot in this conversation's context shows something "
    "genuinely interesting, riff on that. Otherwise pull ONE specific thread from "
    "what you actually know about them — a past conversation, something they were "
    "working toward, a thing they said they'd do — and ask about it like a friend "
    "who's been wondering ('btw, did you ever finish...'). Never invent a memory, "
    "never recap, never ask 'are you still there', and vary the wording every time. "
    "One line, then let it breathe."
)

# `say` is the tool-returned spoken confirmation (Action Truth Contract in
# handlers/mcp.py); captured so session records show exactly what Buddy was
# given to speak for each write.
_SAFE_RESULT_FIELDS: dict[str, frozenset[str]] = {
    "set_reminder": frozenset({"reminder_id", "title", "trigger_at", "timezone", "say"}),
    "create_calendar_event": frozenset({"event_id", "id", "title", "start", "end", "say"}),
    "track_topic": frozenset({"topic_key", "title", "say"}),
    "store_memory": frozenset({"memory_id", "key", "category"}),
}


def _safe_tool_result(tool_name: str, output: object) -> dict[str, Any]:
    raw = getattr(output, "output", "")
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        try:
            parsed = literal_eval(raw)
        except (ValueError, SyntaxError):
            return {}
    if not isinstance(parsed, dict):
        return {}
    allowed = _SAFE_RESULT_FIELDS.get(tool_name, frozenset())
    return {key: parsed[key] for key in allowed if key in parsed}


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
        screen_frames: "ScreenFrameStore | None" = None,
    ) -> None:
        self._session = session
        self._ctx = ctx
        self._session_id = session_id
        self._user_id = user_id
        self._user_tier = user_tier
        self._tool_observer = tool_observer
        # ScreenFrameStore on desktop sessions (None elsewhere); lets the away
        # nudge pick the screen-aware instruction only when a fresh frame exists.
        self._screen_frames = screen_frames
        self.turns: list[dict] = []
        self.tool_calls: list[str] = []
        self.action_receipts: list[dict[str, Any]] = []
        self.done = asyncio.Event()
        self._followup_idle_task: asyncio.Task | None = None
        self._second_away_nudge_task: asyncio.Task | None = None
        # Latched True once Buddy has checked in during the CURRENT silence span;
        # released only by a real final user transcript. Stops LiveKit's repeated
        # "away" re-emits (one per agent turn) from firing back-to-back nudges.
        self._away_nudged = False
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
        self._reset_followup_idle_timer()

    def _reset_followup_idle_timer(self) -> None:
        if not (settings.FOLLOWUP_SHADOW or settings.PROACTIVE_FOLLOWUP_SEND):
            return
        if self._followup_idle_task is not None:
            self._followup_idle_task.cancel()

        async def _close_after_idle() -> None:
            from ...services.session_followup import fields as followup_fields
            from ...services.session_followup.lifecycle import session_lifecycle_service

            try:
                await asyncio.sleep(followup_fields.VOICE_IDLE_TIMEOUT.total_seconds())
                await session_lifecycle_service.finalize_session(
                    self._user_id,
                    self._session_id,
                    reason="idle_timeout",
                )
                await self._session.aclose()
                try:
                    await self._ctx.delete_room()
                except Exception:
                    pass
            except asyncio.CancelledError:
                return

        self._followup_idle_task = asyncio.create_task(
            _close_after_idle(),
            name=f"followup-voice-idle-{self._session_id[:8]}",
        )

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
            # User is back (speaking/listening): the escalation no longer applies.
            # Note we do NOT release _away_nudged here — a brief listening blip
            # between agent turns is not the user actually returning. Only a final
            # transcript (_on_user_transcript) proves they spoke and re-opens nudging.
            self._cancel_second_away_nudge()
            return
        # Already checked in during this silence span. LiveKit re-emits "away"
        # after every agent turn while the user stays quiet, so without this latch
        # each re-emit fires a fresh nudge (the "why do you keep talking" loop).
        if self._away_nudged:
            logger.info("VoiceSession: away nudge skipped (already nudged this silence)", {
                "session_id": self._session_id, "user_id": self._user_id,
            })
            return
        # Gate on agent being idle so the nudge never lands on top of Buddy already
        # speaking or mid tool-call (same guard the tool-thinking phrase uses).
        if str(getattr(self._session, "agent_state", "")) != "listening":
            logger.info("VoiceSession: away nudge skipped (agent not listening)", {
                "session_id": self._session_id, "user_id": self._user_id,
                "agent_state": str(getattr(self._session, "agent_state", "")),
            })
            return

        self._away_nudged = True
        asyncio.create_task(
            self._speak_away_nudge(tier=1), name=f"away-nudge-{self._session_id[:8]}"
        )
        self._arm_second_away_nudge()
        logger.info("VoiceSession: away nudge", {
            "session_id": self._session_id, "user_id": self._user_id,
        })

    async def _speak_away_nudge(self, *, tier: int) -> None:
        """LLM-framed silence nudge. Tier 1 = light presence, tier 2 = re-engage.

        The screen-aware variant is chosen only when a fresh desktop frame exists;
        frames ride user turns, so a fresh frame implies the screenshot is already
        in the chat context for the model to reference. Never raises.
        """
        try:
            instructions = (
                SECOND_AWAY_NUDGE_INSTRUCTIONS if tier == 2
                else FIRST_AWAY_NUDGE_INSTRUCTIONS
            )
            if tier == 1 and await self._has_fresh_screen_frame():
                instructions = FIRST_AWAY_NUDGE_SCREEN_INSTRUCTIONS
            await self._session.generate_reply(instructions=instructions)
        except Exception as exc:
            logger.warn("VoiceSession: away nudge failed", {
                "session_id": self._session_id, "user_id": self._user_id,
                "tier": tier, "error": str(exc),
            })

    async def _has_fresh_screen_frame(self) -> bool:
        if self._screen_frames is None:
            return False
        try:
            return await self._screen_frames.fresh_frame() is not None
        except Exception:
            return False

    def _arm_second_away_nudge(self) -> None:
        """Escalate to the memory-pull nudge if the user stays away past tier 1.

        Cancelled the moment the user does anything (state leaves away, or a
        final transcript arrives). Re-checks away + listening at fire time so a
        race with Buddy speaking can never stack a nudge on top of audio.
        """
        self._cancel_second_away_nudge()
        delay_s = max(
            0.0,
            settings.VOICE_AWAY_SECOND_NUDGE_S - settings.VOICE_AWAY_FIRST_NUDGE_S,
        )

        async def _escalate() -> None:
            try:
                await asyncio.sleep(delay_s)
            except asyncio.CancelledError:
                return
            if str(getattr(self._session, "user_state", "")) != "away":
                return
            if str(getattr(self._session, "agent_state", "")) != "listening":
                return
            logger.info("VoiceSession: second away nudge", {
                "session_id": self._session_id, "user_id": self._user_id,
            })
            await self._speak_away_nudge(tier=2)

        self._second_away_nudge_task = asyncio.create_task(
            _escalate(), name=f"away-nudge-2-{self._session_id[:8]}"
        )

    def _cancel_second_away_nudge(self) -> None:
        if self._second_away_nudge_task is not None:
            self._second_away_nudge_task.cancel()
            self._second_away_nudge_task = None

    def _on_user_transcript(self, ev) -> None:  # type: ignore[misc]
        logger.info("VoiceSession: STT transcript", {
            "session_id": self._session_id, "user_id": self._user_id,
            "text": ev.transcript, "is_final": ev.is_final,
        })
        if ev.is_final and ev.transcript:
            self._cancel_second_away_nudge()
            # The user actually spoke: this silence span is over, re-open nudging
            # so the next quiet stretch can check in once again.
            self._away_nudged = False
            self._reset_followup_idle_timer()
            timestamp = datetime.now(UTC)
            self.turns.append({
                "role": "user",
                "text": ev.transcript,
                "timestamp": timestamp.isoformat(),
            })
            if settings.FOLLOWUP_SHADOW or settings.PROACTIVE_FOLLOWUP_SEND:
                from ...services.session_followup.lifecycle import session_lifecycle_service

                turn_digest = hashlib.sha1(
                    f"{timestamp.isoformat()}|{ev.transcript}".encode()
                ).hexdigest()[:20]
                asyncio.create_task(
                    session_lifecycle_service.note_user_turn(
                        self._user_id,
                        self._session_id,
                        surface="voice",
                        turn_id=f"voice_{turn_digest}",
                        turn_index=sum(
                            turn.get("role") == "user" for turn in self.turns
                        ) - 1,
                        text=str(ev.transcript),
                    ),
                    name=f"followup-voice-turn-{self._session_id[:8]}",
                )

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
                registration = VOICE_TOOL_REGISTRY.get(name)
                if registration is not None and registration.effect is ToolEffect.WRITE:
                    receipt: dict[str, Any] = {
                        "tool_name": name,
                        "call_id": str(
                            getattr(fnc_call, "call_id", "")
                            or getattr(fnc_call, "id", "")
                        ),
                        "success": success,
                        "occurred_at": datetime.now(UTC).isoformat(),
                    }
                    safe_result = _safe_tool_result(name, output) if output is not None else {}
                    if safe_result:
                        receipt["result"] = safe_result
                    self.action_receipts.append(receipt)
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
        self._cancel_second_away_nudge()
        if self._followup_idle_task is not None:
            self._followup_idle_task.cancel()
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
