"""Per-session event recorder.

Owns the mutable state a voice session accumulates (transcript turns, tool-call
names, the done signal) and the AgentSession event handlers that fill it. Call
`attach()` once after the session is built to wire every handler, then read
`turns` / `tool_calls` after `done` is set to feed the post-session pipeline.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from livekit.agents import AgentSession, JobContext

if TYPE_CHECKING:
    from .input_liveness import InputLiveness
    from .screen_frames import ScreenFrameStore

from ...lib.logger import logger
from ...prompts import (
    FIRST_AWAY_NUDGE_INSTRUCTIONS,
    FIRST_AWAY_NUDGE_SCREEN_INSTRUCTIONS,
)
from ...services import voice_action_receipts
from ...services.analytics.llm_telemetry import start_llm_generation
from .action_policy import tool_output_succeeded
from .capabilities import VOICE_TOOL_REGISTRY, ToolEffect
from .errors import classify_pipeline_error, publish_client_error
from .interview import interview_owns_conversation
from .telemetry import log_turn_metrics, log_voice_failure
from .text_sanitizer import strip_nonverbal_cues
from .tool_discovery import IntentPendingRequirement
from .tool_result import parse_tool_output
from .turn_metrics import VoiceTurnMetrics

# Slow-tool filler phrases moved to voice/tool_filler.py, triggered from
# BuddyAgent.llm_node (the only pre-execution tool signal on this stack).

# One silence-presence line, LLM-framed so it lands fresh each time. It fires
# on LiveKit's away event after settings.VOICE_AWAY_FIRST_NUDGE_S.
#
# It fires AT MOST ONCE per continuous silence. LiveKit re-emits "away" after
# every agent turn while the user stays quiet, so it is gated behind a
# `_away_nudged` latch that is only released when a real final user transcript
# arrives. Without it, Buddy talks repeatedly during one silence span.

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
    parsed = parse_tool_output(output)
    if parsed is None:
        return {}
    allowed = _SAFE_RESULT_FIELDS.get(tool_name, frozenset())
    return {key: parsed[key] for key in allowed if key in parsed}


def _pending_requirement(output: object) -> IntentPendingRequirement | None:
    parsed = parse_tool_output(output)
    if parsed is None:
        return None
    if parsed.get("approval_required") is True:
        return IntentPendingRequirement.APPROVAL
    if (
        parsed.get("needs_clarification") is True
        or parsed.get("__clarification__") is True
    ):
        return IntentPendingRequirement.CLARIFICATION
    return None



class GuideObserver(Protocol):
    """The Guide coordinator, as the recorder uses it.

    Declared rather than duck-typed because the recorder forwards five methods
    and used to reach every one of them through ``getattr`` + ``callable``. That
    made a rename silent: the recorder kept running and simply stopped
    forwarding, so Guide usage would quietly stop being measured.
    """

    def is_active(self) -> bool: ...

    def current_reply_source(self) -> str: ...

    def note_turn_metrics(self, role: str, metrics: dict) -> None: ...

    def note_tool(self, name: str) -> None: ...

    def note_user_turn(self, text: str) -> None: ...


class VoiceToolObserver(Protocol):
    """The BuddyAgent, as the recorder uses it. Same reasoning as above."""

    def record_voice_conversation_item(self, item: object) -> None: ...

    def record_voice_tool_execution(
        self,
        tool_name_value: str,
        *,
        success: bool,
        pending_requirement: IntentPendingRequirement | None = None,
    ) -> int | None: ...

    def close_voice_context(self) -> None: ...


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
        tool_observer: VoiceToolObserver | None = None,
        screen_frames: "ScreenFrameStore | None" = None,
        guide: GuideObserver | None = None,
        worker_started_monotonic: float | None = None,
        voice_requested_at_ms: int | None = None,
        voice_request_id: str = "",
        surface: str = "unknown",
        turn_metrics: VoiceTurnMetrics | None = None,
        liveness: "InputLiveness | None" = None,
    ) -> None:
        self._session = session
        self._ctx = ctx
        self._session_id = session_id
        self._user_id = user_id
        self._user_tier = user_tier
        self._tool_observer = tool_observer
        # Guide Mode coordinator (None on non-desktop sessions). The recorder is
        # the one place that already sees per-turn metrics + tool executions, so
        # it forwards them; the coordinator keeps only what lands inside an armed
        # Guide window.
        self._guide = guide
        self._worker_started_monotonic = worker_started_monotonic
        self._voice_requested_at_ms = voice_requested_at_ms
        self._voice_request_id = voice_request_id
        self._surface = surface
        self._turn_metrics = turn_metrics
        # Shared with the input-liveness watchdog: a real transcript is proof the
        # inbound path works and calls off the "I can't hear you" nudge.
        self._liveness = liveness
        self._first_talk_logged = False
        # ScreenFrameStore on desktop sessions (None elsewhere); lets the away
        # nudge pick the screen-aware instruction only when a fresh frame exists.
        self._screen_frames = screen_frames
        self.turns: list[dict] = []
        self.tool_calls: list[str] = []
        self.action_receipts: list[dict[str, Any]] = []
        self._receipt_tasks: set[asyncio.Task[None]] = set()
        self.done = asyncio.Event()
        # True when the 5-minute no-transcript watchdog is what ended the call, rather
        # than the user hanging up. Read at teardown to explain a zero-turn session.
        self.closed_by_idle_timeout = False
        self._closing = False
        self._followup_idle_task: asyncio.Task | None = None
        # Latched True once Buddy has checked in during the CURRENT silence span;
        # released only by a real final user transcript. Stops LiveKit's repeated
        # "away" re-emits (one per agent turn) from firing back-to-back nudges.
        self._away_nudged = False
        # Latest CUMULATIVE per-model LLM usage (session_usage_updated re-emits
        # running totals every turn); flushed to Langfuse once at session close
        # as one per-session generation per model.
        self._model_usage_totals: dict[str, dict] = {}
        # Explicit FallbackAdapter availability transitions, timestamped. Usage
        # totals say WHAT served; these say WHEN a leg failed and recovered,
        # which is the difference between inferring degradation and observing it.
        self.llm_fallback_events: list[dict[str, Any]] = []

    def attach(self) -> None:
        """Register every handler on the session. Call once, after construction."""
        self._session.on("agent_state_changed", self._on_state)
        self._session.on("user_state_changed", self._on_user_state)
        self._session.on("user_input_transcribed", self._on_user_transcript)
        self._session.on("conversation_item_added", self._on_conversation_item)
        self._session.on("function_tools_executed", self._on_tools_executed)
        self._session.on("session_usage_updated", self._on_usage)
        self._session.on("agent_false_interruption", self._on_false_interruption)
        self._session.on("overlapping_speech", self._on_overlapping_speech)
        self._session.on("error", self._on_session_error)
        self._session.on("close", self._on_close)
        self._reset_followup_idle_timer()

    def _reset_followup_idle_timer(self) -> None:
        if self._followup_idle_task is not None:
            self._followup_idle_task.cancel()

        async def _close_after_idle() -> None:
            from ...services.session_followup import fields as followup_fields

            try:
                await asyncio.sleep(followup_fields.VOICE_IDLE_TIMEOUT.total_seconds())
                # This timer is reset ONLY by a final user transcript, so reaching
                # here means the whole window passed without one. On a session that
                # also captured no turns it is the signature of a dead inbound audio
                # path, and it used to end the call with no trace of why.
                await self.close_session(
                    "idle_timeout",
                    idle_s=followup_fields.VOICE_IDLE_TIMEOUT.total_seconds(),
                )
            except asyncio.CancelledError:
                return

        self._followup_idle_task = asyncio.create_task(
            _close_after_idle(),
            name=f"followup-voice-idle-{self._session_id[:8]}",
        )


    async def close_session(self, reason: str, *, idle_s: float | None = None) -> None:
        """End the live session, stamping WHY on the session doc. Idempotent.

        The single server-side close. Guide's idle watchdog used to call
        ``session.aclose()`` directly, which meant a session it ended was
        indistinguishable in Firestore from the user hanging up: no
        ``closed_by_idle_timeout``, no finalize reason, nothing for the
        post-session pipeline to explain a zero-turn call with. Every
        server-initiated close comes through here so that can no longer happen.
        """
        if self._closing:
            return
        self._closing = True
        from ...services.session_followup.lifecycle import session_lifecycle_service

        logger.warn(
            "voice_session_closed_by_idle_timeout",
            {
                "session_id": self._session_id,
                "user_id": self._user_id,
                "reason": reason,
                "idle_s": idle_s,
                "turns_captured": len(self.turns),
            },
        )
        self.closed_by_idle_timeout = True
        await session_lifecycle_service.finalize_session(
            self._user_id,
            self._session_id,
            reason=reason,
        )
        await self._session.aclose()
        try:
            await self._ctx.delete_room()
        except Exception:
            pass

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
            return
        # Guide Mode owns the conversation with terse, screen-driven steps; a chatty
        # companion-persona away nudge would break that flow, so suppress it.
        if self._guide is not None and self._guide.is_active():
            return
        # Same reasoning one step further: Interview Mode owns the conversation
        # outright, and a companion-persona check-in landing on the intake task
        # would answer a silence the interview is entitled to. Deliberately does
        # NOT set the _away_nudged latch below, so the nudge is still available
        # once the interview ends.
        if interview_owns_conversation(self._session):
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
            self._speak_away_nudge(), name=f"away-nudge-{self._session_id[:8]}"
        )
        logger.info("VoiceSession: away nudge", {
            "session_id": self._session_id, "user_id": self._user_id,
        })

    async def _speak_away_nudge(self) -> None:
        """Speak the one LLM-framed silence nudge. Never raises."""
        try:
            instructions = FIRST_AWAY_NUDGE_INSTRUCTIONS
            if await self._has_fresh_screen_frame():
                instructions = FIRST_AWAY_NUDGE_SCREEN_INSTRUCTIONS
            await self._session.generate_reply(instructions=instructions)
        except Exception as exc:
            logger.warn("VoiceSession: away nudge failed", {
                "session_id": self._session_id, "user_id": self._user_id,
                "error": str(exc),
            })

    async def _has_fresh_screen_frame(self) -> bool:
        if self._screen_frames is None:
            return False
        try:
            return await self._screen_frames.fresh_frame() is not None
        except Exception:
            return False

    def _on_user_transcript(self, ev) -> None:  # type: ignore[misc]
        logger.info("VoiceSession: STT transcript", {
            "session_id": self._session_id, "user_id": self._user_id,
            "text_length": len(str(ev.transcript or "")),
            "is_final": ev.is_final,
        })
        if ev.is_final and ev.transcript:
            # Proof the inbound audio path works end to end. Recorded before anything
            # else so the liveness watchdog can never accuse a working session.
            if self._liveness is not None:
                self._liveness.note_transcript()
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
            if self._guide is not None:
                self._guide.note_user_turn(str(ev.transcript))
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
        metrics_payload: dict[str, Any] = {}

        if role == "assistant":
            source = (
                self._guide.current_reply_source()
                if self._guide is not None
                else "normal_turn"
            )
            logger.info("VoiceSession: reply completed", {
                "session_id": self._session_id,
                "user_id": self._user_id,
                "source": source,
            })

        # Per-turn component telemetry. LiveKit populates ChatMessage.metrics
        # before this event fires (user turns: endpointing + STT;
        # assistant turns: LLM TTFT, TTS TTFB, EOU->first-audio)
        metrics = getattr(item, "metrics", None)
        if isinstance(metrics, dict) and metrics and role in ("user", "assistant"):
            metrics_payload = log_turn_metrics(
                session_id=self._session_id,
                user_id=self._user_id,
                role=role,
                metrics=metrics,
                tier=self._user_tier,
            )
            if self._turn_metrics is not None and role == "user":
                self._turn_metrics.note_user_metrics(metrics_payload)
            if self._guide is not None:
                self._guide.note_turn_metrics(role, metrics)
            if role == "assistant" and not self._first_talk_logged:
                self._first_talk_logged = True
                now_epoch_ms = int(time.time() * 1000)
                worker_first_talk_ms = (
                    int((time.monotonic() - self._worker_started_monotonic) * 1000)
                    if self._worker_started_monotonic is not None
                    else None
                )
                request_first_talk_ms = (
                    max(0, now_epoch_ms - self._voice_requested_at_ms)
                    if self._voice_requested_at_ms is not None
                    else None
                )
                logger.info("VoiceSession: first talk metrics", {
                    "session_id": self._session_id,
                    "voice_request_id": self._voice_request_id,
                    "surface": self._surface,
                    "tier": self._user_tier,
                    "worker_first_talk_ms": worker_first_talk_ms,
                    "token_minted_to_first_talk_ms": request_first_talk_ms,
                    "measurement_quality": "assistant_audio_metrics_observed",
                })

        if role == "assistant":
            # text_content is the raw llm_node output, which still carries the
            # [laughter] TTS cue (only the caption path strips it). Drop it here
            # too so post-session summaries never quote a "[laughter]" back.
            content = strip_nonverbal_cues(getattr(item, "text_content", None) or str(item))
            logger.info("VoiceSession: agent response", {
                "session_id": self._session_id, "user_id": self._user_id,
                "text_length": len(str(content)),
            })
            self.turns.append({
                "role": "assistant",
                "text": str(content)[:500],
                "timestamp": datetime.now(UTC).isoformat(),
            })
            if self._turn_metrics is not None and not bool(
                getattr(item, "interrupted", False)
            ):
                self._turn_metrics.complete_turn(
                    assistant_text=str(content),
                    metrics_payload=metrics_payload,
                )
            if self._tool_observer is not None:
                self._tool_observer.record_voice_conversation_item(item)

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
                if self._guide is not None:
                    self._guide.note_tool(name)
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
                    self._append_action_receipt(receipt)
                latency_ms = None
                if self._tool_observer is not None:
                    latency_ms = self._tool_observer.record_voice_tool_execution(
                        name,
                        success=success,
                        pending_requirement=(
                            _pending_requirement(output) if output is not None else None
                        ),
                    )
                if self._turn_metrics is not None:
                    is_error = output is None or bool(getattr(output, "is_error", False))
                    self._turn_metrics.note_tool_call(
                        name=name,
                        arguments=getattr(fnc_call, "arguments", "{}"),
                        latency_ms=latency_ms,
                        success=success,
                        error_type=(
                            "MissingToolOutput"
                            if output is None
                            else "ToolError"
                            if is_error
                            else "ToolResultError"
                            if not success
                            else None
                        ),
                    )
                logger.info("VoiceSession: tool executed", {
                    "session_id": self._session_id, "user_id": self._user_id,
                    "tool": name,
                })

    def record_direct_action(
        self,
        *,
        name: str,
        call_id: str,
        success: bool,
        result: dict[str, Any],
        latency_ms: int | None,
    ) -> None:
        """Record a finalized deterministic action that bypassed model tools."""
        self.tool_calls.append(name)
        if self._guide is not None:
            self._guide.note_tool(name)
        receipt: dict[str, Any] = {
            "tool_name": name,
            "call_id": call_id,
            "success": success,
            "occurred_at": datetime.now(UTC).isoformat(),
            "result": {
                key: value
                for key, value in result.items()
                if value is not None
            },
        }
        self._append_action_receipt(receipt)
        if self._turn_metrics is not None:
            self._turn_metrics.note_tool_call(
                name=name,
                arguments="{}",
                latency_ms=latency_ms,
                success=success,
                error_type=None if success else "DeterministicActionError",
            )
        logger.info(
            "VoiceSession: deterministic action executed",
            {
                "session_id": self._session_id,
                "user_id": self._user_id,
                "action": name,
                "success": success,
            },
        )

    def _append_action_receipt(self, receipt: dict[str, Any]) -> None:
        self.action_receipts.append(receipt)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Synchronous harnesses persist the retained receipt at session end.
            return
        name = str(receipt.get("tool_name") or "action")
        receipt_task = loop.create_task(
            voice_action_receipts.persist(
                self._user_id,
                self._session_id,
                receipt,
            ),
            name=f"voice-receipt-{self._session_id[:8]}-{name}",
        )
        self._receipt_tasks.add(receipt_task)

    async def flush_action_receipts(self) -> None:
        """Wait for every immediate receipt write before the worker can exit."""
        if not self._receipt_tasks:
            return
        tasks = tuple(self._receipt_tasks)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self._receipt_tasks.difference_update(tasks)
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            logger.error(
                "VoiceSession: action receipt persistence failed",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "failure_count": len(failures),
                },
            )

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
            model = str(getattr(mu, "model", "") or "")
            provider = str(getattr(mu, "provider", "") or "")
            usage_totals = {
                "provider": provider,
                "input_tokens": int(input_tokens or 0),
                "cached_tokens": int(cached_tokens or 0),
                "output_tokens": int(getattr(mu, "output_tokens", 0) or 0),
            }
            usage_changed = bool(model) and self._model_usage_totals.get(model) != usage_totals
            logger.info("VoiceSession: llm usage", {
                "session_id": self._session_id, "user_id": self._user_id,
                "model": model,
                "provider": provider,
                "input_tokens": input_tokens,
                "input_cached_tokens": cached_tokens,
                "cache_hit_pct": cache_hit_pct,
                "output_tokens": getattr(mu, "output_tokens", 0),
            })
            if self._turn_metrics is not None:
                self._turn_metrics.note_usage(
                    model=model,
                    provider=provider,
                    prompt_tokens=int(input_tokens or 0),
                    input_cached_tokens=int(cached_tokens or 0),
                    cache_hit_pct=cache_hit_pct,
                    completion_tokens=int(getattr(mu, "output_tokens", 0) or 0),
                    changed=usage_changed,
                )
            if model:
                # Overwrite, never add: these are running totals for the session.
                self._model_usage_totals[model] = usage_totals

    def _on_false_interruption(self, ev) -> None:  # type: ignore[misc]
        # Observability only, no behavior change. Counts how often a detected
        # interruption produced no transcript and Buddy resumed talking — the
        # exact mechanism behind "it keeps talking over me" reports. Tune
        # interruption knobs (min_duration, resume_false_interruption) from
        # these counts, never from intuition. Only `resumed`/`created_at` are
        # read: the event's `message`/`extra_instructions` fields are
        # deprecated in livekit-agents 1.6.4 and warn on access.
        logger.info("voice_false_interruption_resumed", {
            "session_id": self._session_id, "user_id": self._user_id,
            "resumed": bool(getattr(ev, "resumed", False)),
            "created_at": getattr(ev, "created_at", None),
        })

    def _on_overlapping_speech(self, ev) -> None:  # type: ignore[misc]
        # The adaptive interruption model's per-detection verdict: whether user
        # speech over Buddy's playback counted as a real interruption. Paired
        # with voice_false_interruption_resumed this separates "never detected"
        # from "detected, then wrongly resumed".
        logger.info("voice_overlapping_speech", {
            "session_id": self._session_id, "user_id": self._user_id,
            "is_interruption": bool(getattr(ev, "is_interruption", False)),
            "total_duration": getattr(ev, "total_duration", None),
            "prediction_duration": getattr(ev, "prediction_duration", None),
            "detected_at": getattr(ev, "detected_at", None),
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
        if self._tool_observer is not None:
            self._tool_observer.close_voice_context()
        self.done.set()

    def watch_llm_fallback(self, adapter: Any) -> None:
        """Record every LLM availability transition from the FallbackAdapter.

        A leg going unavailable is a provider failure, not a preference — say it
        at error the moment it happens, in a form that names the leg, so a
        degraded session is visible while it is still live instead of being
        reconstructed from usage totals afterwards.
        """

        def _on_availability_changed(ev: Any) -> None:
            leg = getattr(ev, "llm", None)
            available = bool(getattr(ev, "available", False))
            event = {
                "at": datetime.now(UTC).isoformat(),
                "model": str(getattr(leg, "model", "") or type(leg).__name__),
                "provider": str(getattr(leg, "provider", "") or ""),
                "available": available,
            }
            self.llm_fallback_events.append(event)
            log = logger.info if available else logger.error
            log(
                "voice_llm_availability_changed",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    **event,
                },
            )

        try:
            adapter.on("llm_availability_changed", _on_availability_changed)
        except Exception as exc:
            # Observability must never take down the pipeline it observes.
            logger.warn("VoiceSession: fallback watch unavailable", {
                "session_id": self._session_id,
                "error_type": type(exc).__name__,
            })

    @property
    def llm_usage_totals(self) -> dict[str, dict]:
        """Final cumulative per-model token totals for this session.

        Keys are model names as reported by the serving provider, so this is the
        ground truth for which pipeline leg actually served — the session doc's
        model_used used to be a hardcoded constant and lied during fallback.
        """
        return {model: dict(totals) for model, totals in self._model_usage_totals.items()}

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
