"""Append-only per-turn voice metrics persistence."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...lib.logger import logger

_TURN_METRICS_PATH = Path("logs") / "turn_metrics.jsonl"
_APPEND_LOCK = threading.Lock()


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= 40 else f"<str:{len(value)}>"
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    return value


def redact_tool_arguments(raw_arguments: object) -> Any:
    """Preserve argument shape while replacing long free-text strings."""
    if isinstance(raw_arguments, str):
        try:
            parsed = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            return _redact_value(raw_arguments)
        return _redact_value(parsed)
    return _redact_value(raw_arguments)


class VoiceTurnMetrics:
    """Collect existing per-turn emitter values and append one completion record."""

    def __init__(
        self,
        *,
        session_id: str,
        fallback_legs: list[str],
        openai_api_key_present: bool,
        output_path: Path = _TURN_METRICS_PATH,
    ) -> None:
        self._session_id = session_id
        self._fallback_legs = list(fallback_legs)
        self._openai_api_key_present = openai_api_key_present
        self._output_path = output_path
        self._turn_index = 0
        self._turns: dict[int, dict[str, Any]] = {}
        self._usage_by_model: dict[str, dict[str, Any]] = {}
        self._pending_screen_text_context: str | None = None
        self._pending_stt_final_ms: int | None = None
        self._session_resolution_logged = False

    def _turn(self, turn_index: int | None = None) -> dict[str, Any]:
        index = self._turn_index if turn_index is None else turn_index
        return self._turns.setdefault(
            index,
            {
                "session_id": self._session_id,
                "turn_index": index,
                "timestamp": None,
                "resolved_model": None,
                "fallback_occurred": False,
                "prompt_tokens": 0,
                "input_cached_tokens": 0,
                "cache_hit_pct": 0.0,
                "completion_tokens": 0,
                "frame_attached": False,
                "frame_id": None,
                "frame_count_in_ctx": 0,
                # Correlates this turn with the desktop's own stage timings.
                # Ids, never clocks: the two processes are on different
                # machines and their monotonic clocks are not comparable.
                "turn_context_id": None,
                "context_strategy": "none",
                "structured_context_bytes": 0,
                "stream_assembly_ms": None,
                "schema_validation_ms": None,
                "context_wait_ms": None,
                "turn_finalize_ms": None,
                # What the finalization hook decided, and what the SDK then
                # actually did. Recorded apart because intending reuse is not
                # evidence of reuse (see voice/speculation.py).
                "speculative_decision_reason": None,
                "speculative_reused": None,
                "screen_text_context": None,
                # Why a turn ended up spoken or carded. Names, flags and reason
                # codes only, never the artifact body or the request wording.
                # Reconstructing this from a transcript is what turned one bad
                # session into a full investigation.
                "artifact_signal": None,  # "intent" | "output" | None
                "artifact_kind": None,
                "artifact_published": False,
                # Calls the action policy refused, with the reason code. Executed
                # calls land in tool_calls; a gated one produced no call at all
                # and was invisible here.
                "tools_deferred": [],
                "user_transcript": "",
                "assistant_text": "",
                "tool_calls": [],
                "n_model_requests_this_turn": 0,
                "t_stt_final_ms": None,
                "t_model_first_chunk_ms": None,
                "t_llm_node_first_output_ms": None,
                "t_tool_exec_ms": None,
                "t_tts_first_byte_ms": None,
                "t_end_to_end_first_audio_ms": None,
            },
        )

    def start_turn(
        self,
        *,
        turn_index: int,
        user_transcript: str,
        frame_id: str | None,
    ) -> None:
        self._turn_index = turn_index
        turn = self._turn(turn_index)
        turn["user_transcript"] = user_transcript
        turn["frame_attached"] = frame_id is not None
        turn["frame_id"] = frame_id
        turn["t_stt_final_ms"] = self._pending_stt_final_ms
        self._pending_stt_final_ms = None

    def note_turn_context(
        self,
        *,
        turn_index: int,
        turn_context_id: str,
        context_strategy: str,
        structured_context_bytes: int = 0,
        stream_assembly_ms: int | None = None,
        schema_validation_ms: int | None = None,
    ) -> None:
        """Records which kind of screen context answered this turn."""
        turn = self._turn(turn_index)
        turn["turn_context_id"] = turn_context_id or None
        turn["context_strategy"] = context_strategy
        turn["structured_context_bytes"] = structured_context_bytes
        turn["stream_assembly_ms"] = stream_assembly_ms
        turn["schema_validation_ms"] = schema_validation_ms

    def note_speculation(
        self,
        *,
        turn_index: int,
        decision_reason: str,
        turn_finalize_ms: int,
    ) -> None:
        """Records the finalization hook's reuse decision for this turn."""
        turn = self._turn(turn_index)
        turn["speculative_decision_reason"] = decision_reason
        turn["turn_finalize_ms"] = turn_finalize_ms

    def note_speculative_outcome(self, *, turn_index: int, reused: bool) -> None:
        """Records what LiveKit actually did with the speculation, which is only
        knowable after the fact (see BuddyAgent._resolve_previous_speculation)."""
        self._turn(turn_index)["speculative_reused"] = reused

    def note_screen_text_context(self, instruction: str) -> None:
        self._pending_screen_text_context = instruction

    def note_model_request(self, *, turn_index: int, frame_count_in_ctx: int) -> None:
        self._turn_index = turn_index
        turn = self._turn(turn_index)
        turn["n_model_requests_this_turn"] += 1
        turn["frame_count_in_ctx"] = frame_count_in_ctx
        if self._pending_screen_text_context is not None:
            turn["screen_text_context"] = self._pending_screen_text_context
            self._pending_screen_text_context = None

    def note_artifact(
        self,
        *,
        turn_index: int,
        signal: str,
        kind: str,
        published: bool,
    ) -> None:
        """Records that content was routed to a card instead of to speech.

        ``signal`` is which layer caught it: "intent" when the request wording
        armed the guard, "output" when the answer contained something speech
        would have destroyed. Knowing which one fired is the difference between
        tuning request matching and tuning content detection.
        """
        turn = self._turn(turn_index)
        turn["artifact_signal"] = signal
        turn["artifact_kind"] = kind
        turn["artifact_published"] = published

    def note_tool_deferred(
        self, *, turn_index: int, name: str, reason: str, effect: str
    ) -> None:
        """Records one call the action policy refused, and why."""
        self._turn(turn_index)["tools_deferred"].append(
            {"name": name, "reason": reason, "effect": effect}
        )

    def note_model_first_chunk(self, *, turn_index: int, latency_ms: int) -> None:
        turn = self._turn(turn_index)
        if turn["t_model_first_chunk_ms"] is None:
            turn["t_model_first_chunk_ms"] = latency_ms

    def note_llm_node_first_output(self, *, turn_index: int, latency_ms: int) -> None:
        turn = self._turn(turn_index)
        if turn["t_llm_node_first_output_ms"] is None:
            turn["t_llm_node_first_output_ms"] = latency_ms

    def note_user_metrics(self, payload: dict[str, Any]) -> None:
        turn = self._turns.get(self._turn_index)
        if turn is not None and turn["user_transcript"]:
            turn["t_stt_final_ms"] = payload.get("stt_final_ms")
        else:
            self._pending_stt_final_ms = payload.get("stt_final_ms")

    def note_usage(
        self,
        *,
        model: str,
        provider: str,
        prompt_tokens: int,
        input_cached_tokens: int,
        cache_hit_pct: float,
        completion_tokens: int,
        changed: bool,
    ) -> None:
        if not model:
            return
        self._usage_by_model[model] = {
            "provider": provider,
            "prompt_tokens": prompt_tokens,
            "input_cached_tokens": input_cached_tokens,
            "cache_hit_pct": cache_hit_pct,
            "completion_tokens": completion_tokens,
        }
        if changed:
            resolved_model = f"{provider}:{model}" if provider else model
            turn = self._turn()
            turn["resolved_model"] = resolved_model
            turn["fallback_occurred"] = turn["fallback_occurred"] or bool(
                self._fallback_legs and resolved_model != self._fallback_legs[0]
            )

    def note_tool_call(
        self,
        *,
        name: str,
        arguments: object,
        latency_ms: int | None,
        success: bool,
        error_type: str | None,
    ) -> None:
        turn = self._turn()
        turn["tool_calls"].append(
            {
                "name": name,
                "arguments_redacted": redact_tool_arguments(arguments),
                "latency_ms": latency_ms,
                "success": success,
                "error_type": error_type,
            }
        )
        if latency_ms is not None:
            turn["t_tool_exec_ms"] = (turn["t_tool_exec_ms"] or 0) + latency_ms

    def complete_turn(
        self,
        *,
        assistant_text: str,
        metrics_payload: dict[str, Any],
    ) -> None:
        turn = self._turn()
        resolved_model = turn["resolved_model"]
        model = resolved_model.split(":", 1)[-1] if resolved_model else ""
        turn["timestamp"] = datetime.now(UTC).isoformat()
        turn["assistant_text"] = assistant_text
        turn["t_tts_first_byte_ms"] = metrics_payload.get("tts_ttfb_ms")
        turn["t_end_to_end_first_audio_ms"] = metrics_payload.get("eou_to_first_audio_ms")

        usage = self._usage_by_model.get(model)
        if usage is not None:
            turn.update(
                {
                    "prompt_tokens": usage["prompt_tokens"],
                    "input_cached_tokens": usage["input_cached_tokens"],
                    "cache_hit_pct": usage["cache_hit_pct"],
                    "completion_tokens": usage["completion_tokens"],
                }
            )

        self._append(turn)
        self._log(turn)
        if resolved_model is not None and not self._session_resolution_logged:
            self._session_resolution_logged = True
            logger.info(
                "VoiceSession: resolved LLM adapter",
                {
                    "session_id": self._session_id,
                    "resolved_adapter": resolved_model,
                    "openai_api_key_present": self._openai_api_key_present,
                    "fallback_adapter_legs": self._fallback_legs,
                },
            )
        self._turns.pop(self._turn_index, None)

    def _log(self, record: dict[str, Any]) -> None:
        """Emit the per-turn timings to stdout so they are readable in production.

        _append alone is not enough. The worker runs on LiveKit Cloud Agents, where
        logs/turn_metrics.jsonl lives on an ephemeral container filesystem nobody can
        reach, so every latency number this class collects was being written straight
        into a black hole. Reading a real session's timings meant guessing. This makes
        `lk agent logs` the answer instead.

        Transcript text is deliberately excluded: these lines go to a log sink with a
        different retention and access story than the session recorder, and the
        timings are the whole point. Everything here is a number, an id, or a tool name.
        """
        try:
            logger.info(
                "VoiceTurn: metrics",
                {
                    "session_id": record.get("session_id"),
                    "turn_index": record.get("turn_index"),
                    "resolved_model": record.get("resolved_model"),
                    "fallback_occurred": record.get("fallback_occurred"),
                    "prompt_tokens": record.get("prompt_tokens"),
                    "input_cached_tokens": record.get("input_cached_tokens"),
                    "cache_hit_pct": record.get("cache_hit_pct"),
                    "completion_tokens": record.get("completion_tokens"),
                    "frame_attached": record.get("frame_attached"),
                    "frame_count_in_ctx": record.get("frame_count_in_ctx"),
                    # Screen evidence was already collected but only ever reached
                    # _append, whose jsonl sits on an ephemeral container disk. It
                    # belongs here, where `lk agent logs` can actually read it.
                    "context_strategy": record.get("context_strategy"),
                    "structured_context_bytes": record.get("structured_context_bytes"),
                    "turn_context_id": record.get("turn_context_id"),
                    "artifact_signal": record.get("artifact_signal"),
                    "artifact_kind": record.get("artifact_kind"),
                    "artifact_published": record.get("artifact_published"),
                    "n_model_requests_this_turn": record.get("n_model_requests_this_turn"),
                    "tool_names": [
                        call.get("name") for call in record.get("tool_calls") or []
                    ],
                    "tool_failures": [
                        call.get("name")
                        for call in record.get("tool_calls") or []
                        if not call.get("success")
                    ],
                    "tools_deferred": record.get("tools_deferred") or [],
                    "t_stt_final_ms": record.get("t_stt_final_ms"),
                    "t_model_first_chunk_ms": record.get("t_model_first_chunk_ms"),
                    "t_llm_node_first_output_ms": record.get("t_llm_node_first_output_ms"),
                    "t_tool_exec_ms": record.get("t_tool_exec_ms"),
                    "t_tts_first_byte_ms": record.get("t_tts_first_byte_ms"),
                    "t_end_to_end_first_audio_ms": record.get(
                        "t_end_to_end_first_audio_ms"
                    ),
                },
            )
        except Exception:
            # Metrics reporting must never be able to take down a live call.
            pass

    # Free-text fields that carry what was actually said. Redacted on the way
    # to disk for the same reason `_log` omits them entirely: this file has no
    # retention or access story, and the row it sits in already redacts long
    # tool arguments.
    _SPOKEN_TEXT_FIELDS = ("user_transcript", "assistant_text")

    def _append(self, record: dict[str, Any]) -> None:
        try:
            record = {
                key: (
                    _redact_value(value)
                    if key in self._SPOKEN_TEXT_FIELDS
                    else value
                )
                for key, value in record.items()
            }
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            with _APPEND_LOCK:
                self._output_path.parent.mkdir(parents=True, exist_ok=True)
                with self._output_path.open("a", encoding="utf-8", newline="") as handle:
                    handle.write(line)
        except Exception as exc:
            logger.warn(
                "VoiceSession: turn metrics persistence failed",
                {
                    "session_id": self._session_id,
                    "turn_index": record.get("turn_index"),
                    "error_type": type(exc).__name__,
                },
            )
