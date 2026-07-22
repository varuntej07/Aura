"""Regression guards for the latency-sensitive AgentSession configuration."""

from __future__ import annotations

from types import SimpleNamespace

from src.agent.voice import pipelines


def test_agent_session_caps_uncertain_endpointing_at_800ms(monkeypatch) -> None:
    captured: dict = {}

    def _capture_session(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(pipelines, "AgentSession", _capture_session)

    pipelines.build_agent_session(
        stt=object(),
        llm=object(),
        tts=object(),
        vad=object(),
        turn_detector=None,
        mcp_server=object(),
    )

    assert captured["preemptive_generation"] is True
    assert captured["turn_handling"]["endpointing"] == {
        "min_delay": 0.2,
        "max_delay": 0.8,
    }
