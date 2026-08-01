"""Regression guards for the latency-sensitive AgentSession configuration."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from livekit.agents import function_tool
from livekit.agents import llm as lk_llm
from livekit.agents.types import APIConnectOptions

from src.agent.voice import pipelines


@function_tool
async def _voice_profile_test_tool(topic: str) -> str:
    """Look up a topic for the request-boundary tests.

    Args:
        topic: The topic to look up.
    """
    return topic


class _EmptyAsyncStream:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _SingleTextGoogleStream:
    def __init__(self) -> None:
        self._sent = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._sent:
            raise StopAsyncIteration
        self._sent = True
        return SimpleNamespace(
            prompt_feedback=None,
            usage_metadata=None,
            candidates=[
                SimpleNamespace(
                    finish_reason=None,
                    content=SimpleNamespace(
                        parts=[SimpleNamespace(function_call=None, text="ok")]
                    ),
                )
            ],
        )


def _chat_context() -> lk_llm.ChatContext:
    context = lk_llm.ChatContext()
    context.add_message(role="system", content=["You are Buddy."])
    context.add_message(role="user", content=["Look up the launch."])
    return context


def _post_tool_chat_context() -> lk_llm.ChatContext:
    context = _chat_context()
    context.items.append(
        lk_llm.FunctionCall(
            call_id="lookup-1",
            name="_voice_profile_test_tool",
            arguments='{"topic":"launch"}',
        )
    )
    context.items.append(
        lk_llm.FunctionCallOutput(
            call_id="lookup-1",
            name="_voice_profile_test_tool",
            output='{"result":"verified"}',
            is_error=False,
        )
    )
    return context


def _configure_provider_keys(monkeypatch, *, openai_key: str = "openai-test-key"):
    monkeypatch.setattr(pipelines.settings, "OPENAI_API_KEY", openai_key)
    monkeypatch.setattr(pipelines.settings, "ANTHROPIC_API_KEY", "anthropic-test-key")
    monkeypatch.setattr(pipelines.settings, "GEMINI_API_KEY", "gemini-test-key")


async def _complete_request(
    adapter,
    context: lk_llm.ChatContext,
) -> None:
    stream = adapter.chat(
        chat_ctx=context,
        tools=[_voice_profile_test_tool],
        conn_options=APIConnectOptions(max_retry=0, timeout=1.0),
    )
    await stream._task
    await stream.aclose()


def test_shared_voice_generation_profile() -> None:
    assert (
        pipelines.VOICE_GENERATION_TEMPERATURE,
        pipelines.VOICE_MAX_OUTPUT_TOKENS,
    ) == (0.2, 16_384)


def test_agent_session_uses_streaming_endpointing_and_preemptive_tts(monkeypatch) -> None:
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

    # preemptive_generation and turn_detection are no longer top-level kwargs:
    # both are deprecated on AgentSession and removed in Agents 2.0, and the
    # top-level bool form cannot express preemptive_tts at all.
    assert "preemptive_generation" not in captured
    # max_retries and max_speech_duration are raised off LiveKit's defaults
    # (3 / 10.0) because the speculation is re-run on each transcript update and
    # is only reusable when its transcript still matches the finalized one. At
    # the defaults a long or disfluent utterance exhausts its retries, or skips
    # speculation entirely, and pays a full cold round trip.
    assert captured["turn_handling"]["preemptive_generation"] == {
        "enabled": True,
        "preemptive_tts": True,
        "max_retries": 6,
        "max_speech_duration": 20.0,
    }
    # Endpointing is binary, not a gradient: audio_recognition.py picks either
    # min_delay or max_delay, so max_delay is the flat price of every turn the
    # EOU model reads as unfinished, not a rare ceiling. The 2026-08-01 baseline
    # measured 8 of 15 turns paying the whole 2.5s, which made it the single
    # largest term in the turn budget. mode="dynamic" lets DynamicEndpointing
    # converge the ceiling toward this user's real pauses instead.
    assert captured["turn_handling"]["endpointing"] == {
        "mode": "dynamic",
        "min_delay": 0.3,
        "max_delay": 1.0,
    }
    # This call passes turn_detector=None. Absent and None are NOT equivalent:
    # absent lets agent_session.py auto-construct inference.TurnDetector(),
    # while an explicit None disables end-of-turn detection outright.
    assert "turn_detection" not in captured["turn_handling"]


async def test_openai_outgoing_requests_use_shared_voice_profile(monkeypatch) -> None:
    captured: list[dict[str, Any]] = []
    _configure_provider_keys(monkeypatch)
    pipeline = pipelines.build_llm_pipeline("user-123")
    # Select by type, not position: chain ORDER is tuning surface (asserted
    # separately below), while this test covers the per-request voice profile.
    openai_adapter = next(
        leg for leg in pipeline._llm_instances if type(leg) is pipelines.openai.LLM
    )

    async def _capture_create(**kwargs):
        captured.append(kwargs)
        return _EmptyAsyncStream()

    monkeypatch.setattr(
        openai_adapter._client.chat.completions,
        "create",
        _capture_create,
    )
    await _complete_request(openai_adapter, _chat_context())
    await _complete_request(openai_adapter, _post_tool_chat_context())

    assert len(captured) == 2
    for request in captured:
        assert request["temperature"] == pipelines.VOICE_GENERATION_TEMPERATURE
        assert request["max_completion_tokens"] == pipelines.VOICE_MAX_OUTPUT_TOKENS
        assert request["parallel_tool_calls"] is True
        assert request["prompt_cache_key"] == "user-123"
        assert request["stream"] is True
        assert request["stream_options"] == {"include_usage": True}
        assert "top_p" not in request
        function = request["tools"][0]["function"]
        assert function["strict"] is True
        assert function["parameters"]["additionalProperties"] is False


async def test_anthropic_outgoing_requests_use_shared_voice_profile(
    monkeypatch,
) -> None:
    captured: list[dict[str, Any]] = []
    _configure_provider_keys(monkeypatch)
    pipeline = pipelines.build_llm_pipeline("user-123")
    anthropic_adapter = next(
        leg for leg in pipeline._llm_instances if type(leg) is pipelines.anthropic.LLM
    )

    async def _capture_create(**kwargs):
        captured.append(kwargs)
        return _EmptyAsyncStream()

    monkeypatch.setattr(
        anthropic_adapter._client.messages,
        "create",
        _capture_create,
    )
    await _complete_request(anthropic_adapter, _chat_context())
    await _complete_request(anthropic_adapter, _post_tool_chat_context())

    assert len(captured) == 2
    for request in captured:
        assert request["temperature"] == pipelines.VOICE_GENERATION_TEMPERATURE
        assert request["max_tokens"] == pipelines.VOICE_MAX_OUTPUT_TOKENS
        assert request["stream"] is True
        assert "top_p" not in request
        assert "tool_choice" not in request
        assert request["system"][-1]["cache_control"] == {"type": "ephemeral"}
        tool = request["tools"][-1]
        assert tool["name"] == "_voice_profile_test_tool"
        assert tool["cache_control"] == {"type": "ephemeral"}
        assert tool["input_schema"]["additionalProperties"] is False


async def test_gemini_outgoing_requests_use_shared_voice_profile(
    monkeypatch,
) -> None:
    captured: list[dict[str, Any]] = []
    _configure_provider_keys(monkeypatch)
    pipeline = pipelines.build_llm_pipeline("user-123")
    gemini_adapter = next(
        leg for leg in pipeline._llm_instances if type(leg) is pipelines.google.LLM
    )

    async def _capture_generate_content_stream(**kwargs):
        captured.append(kwargs)
        return _SingleTextGoogleStream()

    monkeypatch.setattr(
        gemini_adapter._client.aio.models,
        "generate_content_stream",
        _capture_generate_content_stream,
    )
    await _complete_request(gemini_adapter, _chat_context())
    await _complete_request(gemini_adapter, _post_tool_chat_context())

    assert len(captured) == 2
    for request in captured:
        config = request["config"]
        serialized = config.model_dump(exclude_none=True, mode="json")
        assert config.temperature == pipelines.VOICE_GENERATION_TEMPERATURE
        assert config.max_output_tokens == pipelines.VOICE_MAX_OUTPUT_TOKENS
        assert "top_p" not in serialized
        assert "thinking_config" not in serialized
        assert config.tools


def test_voice_adapter_order_with_openai(monkeypatch) -> None:
    _configure_provider_keys(monkeypatch)

    adapters = pipelines.build_llm_pipeline("user-123")._llm_instances

    # GPT-4.1 is the base voice model. The first two legs are the same model on
    # purpose — LiveKit-brokered and direct — for transport and quota redundancy.
    assert [type(adapter) for adapter in adapters] == [
        pipelines.inference.LLM,
        pipelines.openai.LLM,
        pipelines.anthropic.LLM,
        pipelines.google.LLM,
    ]


def test_voice_adapter_order_without_openai(monkeypatch) -> None:
    _configure_provider_keys(monkeypatch, openai_key="")

    adapters = pipelines.build_llm_pipeline("user-123")._llm_instances

    assert [type(adapter) for adapter in adapters] == [
        pipelines.anthropic.LLM,
        pipelines.google.LLM,
    ]
