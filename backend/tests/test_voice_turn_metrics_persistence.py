from __future__ import annotations

import json

from src.agent.voice.turn_metrics import VoiceTurnMetrics, redact_tool_arguments


def test_redacts_long_tool_strings_without_dropping_shape() -> None:
    arguments = {
        "enum": "tomorrow",
        "count": 3,
        "enabled": True,
        "nested": {
            "text": "x" * 41,
            "items": ["short", "y" * 52],
        },
    }

    redacted = redact_tool_arguments(json.dumps(arguments))

    assert redacted == {
        "enum": "tomorrow",
        "count": 3,
        "enabled": True,
        "nested": {
            "text": "<str:41>",
            "items": ["short", "<str:52>"],
        },
    }


def test_appends_one_complete_turn_record(tmp_path) -> None:
    output_path = tmp_path / "logs" / "turn_metrics.jsonl"
    metrics = VoiceTurnMetrics(
        session_id="session-1",
        fallback_legs=["openai:gpt-primary", "anthropic:claude-fallback"],
        openai_api_key_present=True,
        output_path=output_path,
    )
    metrics.note_user_metrics({"stt_final_ms": 84})
    metrics.start_turn(
        turn_index=1,
        user_transcript="Set a reminder",
        frame_id="frame-7",
    )
    metrics.note_model_request(turn_index=1, frame_count_in_ctx=1)
    metrics.note_model_first_chunk(turn_index=1, latency_ms=215)
    metrics.note_llm_node_first_output(turn_index=1, latency_ms=240)
    metrics.note_usage(
        model="claude-fallback",
        provider="anthropic",
        prompt_tokens=1200,
        input_cached_tokens=900,
        cache_hit_pct=75.0,
        completion_tokens=80,
        changed=True,
    )
    metrics.note_tool_call(
        name="set_reminder",
        arguments={"title": "Call Mom", "notes": "z" * 60},
        latency_ms=140,
        success=True,
        error_type=None,
    )

    metrics.complete_turn(
        assistant_text="Done.",
        metrics_payload={
            "llm_model": "FallbackAdapter",
            "llm_provider": "livekit",
            "tts_ttfb_ms": 110,
            "eou_to_first_audio_ms": 720,
        },
    )

    records = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    record = records[0]
    assert set(record) == {
        "session_id",
        "turn_index",
        "timestamp",
        "resolved_model",
        "fallback_occurred",
        "prompt_tokens",
        "input_cached_tokens",
        "cache_hit_pct",
        "completion_tokens",
        "frame_attached",
        "frame_id",
        "frame_count_in_ctx",
        "turn_context_id",
        "context_strategy",
        "structured_context_bytes",
        "stream_assembly_ms",
        "schema_validation_ms",
        "context_wait_ms",
        "turn_finalize_ms",
        "speculative_decision_reason",
        "speculative_reused",
        "screen_text_context",
        "artifact_signal",
        "artifact_kind",
        "artifact_published",
        "tools_deferred",
        "user_transcript",
        "assistant_text",
        "tool_calls",
        "n_model_requests_this_turn",
        "t_stt_final_ms",
        "t_model_first_chunk_ms",
        "t_llm_node_first_output_ms",
        "t_tool_exec_ms",
        "t_tts_first_byte_ms",
        "t_end_to_end_first_audio_ms",
    }
    assert record["resolved_model"] == "anthropic:claude-fallback"
    assert record["fallback_occurred"] is True
    assert record["prompt_tokens"] == 1200
    assert record["input_cached_tokens"] == 900
    assert record["cache_hit_pct"] == 75.0
    assert record["completion_tokens"] == 80
    assert record["frame_attached"] is True
    assert record["frame_id"] == "frame-7"
    assert record["frame_count_in_ctx"] == 1
    assert record["tool_calls"][0]["arguments_redacted"]["notes"] == "<str:60>"
    assert record["t_stt_final_ms"] == 84
    assert record["t_model_first_chunk_ms"] == 215
    assert record["t_llm_node_first_output_ms"] == 240
    assert record["t_tool_exec_ms"] == 140
    assert record["t_tts_first_byte_ms"] == 110
    assert record["t_end_to_end_first_audio_ms"] == 720
