"""Guards the per-turn voice latency extraction in telemetry.log_turn_metrics.

Two jobs:
1. Regression guard for OUR extraction: the LiveKit metric keys must map to the
   ms fields the Cloud Monitoring distribution metrics read
   (jsonPayload.eou_to_first_audio_ms, llm_ttft_ms, tts_ttfb_ms,
   endpointing_ms, stt_final_ms). If _to_ms or the role branching breaks, this fails.
2. Drift canary: when an assistant turn carries a non-empty metrics dict but none
   of the latency keys resolve (the silent-null a LiveKit rename would cause),
   log_turn_metrics MUST emit a WARNING, never quietly an INFO with all-null
   latency. "Zero must never look like healthy."
"""

from __future__ import annotations

import pytest

from src.agent.voice import telemetry


class _LogCapture:
    def __init__(self) -> None:
        self.info_calls: list[tuple[str, dict]] = []
        self.warn_calls: list[tuple[str, dict]] = []

    def info(self, message: str, metadata: dict | None = None) -> None:
        self.info_calls.append((message, metadata or {}))

    def warn(self, message: str, metadata: dict | None = None) -> None:
        self.warn_calls.append((message, metadata or {}))


@pytest.fixture
def captured(monkeypatch) -> _LogCapture:
    cap = _LogCapture()
    monkeypatch.setattr(telemetry.logger, "info", cap.info)
    monkeypatch.setattr(telemetry.logger, "warn", cap.warn)
    return cap


def test_assistant_turn_extracts_latency_to_ms(captured: _LogCapture) -> None:
    telemetry.log_turn_metrics(
        session_id="s1",
        user_id="u1",
        role="assistant",
        metrics={
            "llm_node_ttft": 0.42,
            "tts_node_ttfb": 0.13,
            "e2e_latency": 0.95,
            "llm_metadata": {"model_name": "claude", "model_provider": "anthropic"},
            "tts_metadata": {"model_name": "sonic", "model_provider": "cartesia"},
        },
        tier="free",
    )

    assert len(captured.info_calls) == 1
    _, payload = captured.info_calls[0]
    assert payload["llm_ttft_ms"] == 420
    assert payload["tts_ttfb_ms"] == 130
    assert payload["eou_to_first_audio_ms"] == 950
    assert payload["llm_provider"] == "anthropic"
    assert not captured.warn_calls


def test_user_turn_extracts_endpointing_and_stt(captured: _LogCapture) -> None:
    telemetry.log_turn_metrics(
        session_id="s1",
        user_id="u1",
        role="user",
        metrics={
            "end_of_turn_delay": 0.20,
            "transcription_delay": 0.08,
            "stt_metadata": {"model_name": "nova", "model_provider": "deepgram"},
        },
        tier="free",
    )

    assert len(captured.info_calls) == 1
    _, payload = captured.info_calls[0]
    assert payload["endpointing_ms"] == 200
    assert payload["stt_final_ms"] == 80
    assert not captured.warn_calls


def test_assistant_turn_with_no_known_latency_keys_warns(captured: _LogCapture) -> None:
    # Simulates a LiveKit metrics-schema rename: the dict is present and non-empty
    # but none of the keys we read exist. Must warn AND still emit the INFO line.
    telemetry.log_turn_metrics(
        session_id="s1",
        user_id="u1",
        role="assistant",
        metrics={"renamed_ttft": 0.42, "renamed_e2e": 0.95},
        tier="free",
    )

    assert len(captured.warn_calls) == 1
    message, meta = captured.warn_calls[0]
    assert "latency fields null" in message
    # raw keys surfaced so the new LiveKit names are visible in the log
    assert meta["available_metric_keys"] == ["renamed_e2e", "renamed_ttft"]
    # the INFO line still fires (so an absent-key turn is not dropped)
    assert len(captured.info_calls) == 1


def test_unknown_role_is_ignored(captured: _LogCapture) -> None:
    telemetry.log_turn_metrics(
        session_id="s1",
        user_id="u1",
        role="system",
        metrics={"x": 1},
        tier="free",
    )

    assert not captured.info_calls
    assert not captured.warn_calls
