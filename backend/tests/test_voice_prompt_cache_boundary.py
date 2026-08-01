"""Production-boundary coverage for the static-first voice companion prompt."""

from __future__ import annotations

from typing import Any

import pytest
import tiktoken
from livekit.agents import llm as lk_llm
from livekit.agents.types import APIConnectOptions
from livekit.plugins import openai

from src.agent.buddy_agent import BuddyAgent
from src.agent.voice import pipelines
from src.prompts import (
    DESKTOP_VOICE_SYSTEM_PROMPT,
    GUIDE_SYSTEM_PROMPT,
    MOBILE_VOICE_SYSTEM_PROMPT,
)
from src.agent.voice_prompt import (
    VOICE_SESSION_CONTEXT_START,
    render_voice_session_context,
)

_ENCODING = tiktoken.get_encoding("o200k_base")
_BEFORE_TOTAL_TOKENS = {
    "app": 1600,
    "keyboard": 1650,
    "desktop": 2000,
}

_CONTEXT_ONE = {
    "name": "CACHE_BOUNDARY_USER",
    "timezone": "America/Los_Angeles",
    "local_time": "8:00 PM",
    "local_date": "July 28, 2026",
    "memory_summary": "CACHE_MEMORY_ONE: building static-first voice prompts.",
    "graph_context": "\nCACHE_GRAPH_ONE: phase five settings are stable.",
    "last_session_context": "CACHE_LAST_ONE: finished parallel-call safety.",
    "archive_context": "CACHE_ARCHIVE_ONE: Aura has persistent memory.",
    "user_aura_profile": "CACHE_PROFILE_ONE: direct and technical.",
}
_CONTEXT_TWO = {
    **_CONTEXT_ONE,
    "local_time": "7:15 AM",
    "local_date": "July 29, 2026",
    "memory_summary": "CACHE_MEMORY_TWO: reviewing provider boundaries.",
    "graph_context": "\nCACHE_GRAPH_TWO: strict tools remain unchanged.",
    "last_session_context": "CACHE_LAST_TWO: completed generation settings.",
    "archive_context": "CACHE_ARCHIVE_TWO: a different session archive.",
    "user_aura_profile": "CACHE_PROFILE_TWO: concise and analytical.",
}
_EQUIVALENT_BASELINE_CONTEXT = {
    "name": "Varun",
    "timezone": "America/Los_Angeles",
    "local_time": "8:00 PM",
    "local_date": "July 28, 2026",
    "memory_summary": (
        "- Building Aura voice caching\n- Prefers concise engineering reports"
    ),
    "graph_context": (
        "\n\n            Related long-term memory:\n"
        "            - Phase 5 standardized voice generation settings"
    ),
    "last_session_context": "We finished the parallel tool safety work.",
    "archive_context": "Aura is a voice-first companion with persistent memory.",
    "user_aura_profile": "Direct, technical, and detail-oriented.",
}


class _EmptyAsyncStream:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def _agent(surface: str, context_vars: dict[str, str]) -> BuddyAgent:
    return BuddyAgent(
        user_id="same-user",
        context_vars=context_vars,
        chat_ctx=lk_llm.ChatContext(),
        launch_surface=surface,
        session_id=f"cache-{surface}",
    )


def _common_token_prefix(left: str, right: str) -> int:
    left_tokens = _ENCODING.encode(left)
    right_tokens = _ENCODING.encode(right)
    return next(
        (
            index
            for index, (left_token, right_token) in enumerate(
                zip(left_tokens, right_tokens, strict=False)
            )
            if left_token != right_token
        ),
        min(len(left_tokens), len(right_tokens)),
    )


async def _capture_openai_request(
    monkeypatch: pytest.MonkeyPatch,
    agent: BuddyAgent,
) -> dict[str, Any]:
    monkeypatch.setattr(pipelines.settings, "OPENAI_API_KEY", "openai-test-key")
    monkeypatch.setattr(pipelines.settings, "ANTHROPIC_API_KEY", "anthropic-test-key")
    monkeypatch.setattr(pipelines.settings, "GEMINI_API_KEY", "gemini-test-key")
    # Select the direct-OpenAI leg by type, not by position. The chain order is
    # tuning surface (Haiku leads today because the OpenAI org is TPM-bound), so
    # indexing into it made this test fail on a deliberate reorder rather than
    # on the prompt-cache behaviour it actually covers.
    adapter = next(
        leg
        for leg in pipelines.build_llm_pipeline("same-user")._llm_instances
        if isinstance(leg, openai.LLM)
    )
    captured: list[dict[str, Any]] = []

    async def _capture_create(**kwargs):
        captured.append(kwargs)
        return _EmptyAsyncStream()

    monkeypatch.setattr(adapter._client.chat.completions, "create", _capture_create)
    chat_ctx = lk_llm.ChatContext()
    chat_ctx.add_message(role="system", content=[agent.instructions])
    chat_ctx.add_message(role="user", content=["Keep helping with this request."])
    stream = adapter.chat(
        chat_ctx=chat_ctx,
        tools=agent.tools,
        conn_options=APIConnectOptions(max_retry=0, timeout=1.0),
    )
    await stream._task
    await stream.aclose()
    assert len(captured) == 1
    return captured[0]


@pytest.mark.parametrize("surface", ["app", "keyboard", "desktop"])
async def test_actual_openai_boundary_has_cacheable_static_prefix(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    first_agent = _agent(surface, _CONTEXT_ONE)
    second_agent = _agent(surface, _CONTEXT_TWO)
    first_request = await _capture_openai_request(monkeypatch, first_agent)
    second_request = await _capture_openai_request(monkeypatch, second_agent)

    first_prompt = first_request["messages"][0]["content"]
    second_prompt = second_request["messages"][0]["content"]
    first_stable = first_prompt.split(VOICE_SESSION_CONTEXT_START, 1)[0]
    second_stable = second_prompt.split(VOICE_SESSION_CONTEXT_START, 1)[0]

    assert first_request["messages"][0]["role"] == "system"
    assert second_request["messages"][0]["role"] == "system"
    assert first_request["messages"][1]["role"] == "user"
    assert second_request["messages"][1]["role"] == "user"
    assert first_prompt != second_prompt
    assert first_stable == second_stable
    assert first_prompt.endswith(render_voice_session_context(_CONTEXT_ONE))
    assert second_prompt.endswith(render_voice_session_context(_CONTEXT_TWO))
    assert _common_token_prefix(first_prompt, second_prompt) >= 1024
    assert _common_token_prefix(first_prompt, second_prompt) >= len(
        _ENCODING.encode(first_stable)
    )
    for dynamic_value in {
        value
        for context in (_CONTEXT_ONE, _CONTEXT_TWO)
        for value in context.values()
        if value
    }:
        assert dynamic_value not in first_stable
    assert first_request["prompt_cache_key"] == "same-user"
    assert second_request["prompt_cache_key"] == "same-user"
    assert first_request["tools"] == second_request["tools"]


@pytest.mark.parametrize("surface", ["app", "keyboard", "desktop"])
def test_static_first_prompt_preserves_surface_behavior_and_size(surface: str) -> None:
    agent = _agent(surface, _EQUIVALENT_BASELINE_CONTEXT)
    prompt = agent.instructions
    normalized = " ".join(prompt.split())
    context_start = prompt.index(VOICE_SESSION_CONTEXT_START)

    assert prompt.endswith(render_voice_session_context(_EQUIVALENT_BASELINE_CONTEXT))
    assert len(_ENCODING.encode(prompt)) <= _BEFORE_TOTAL_TOKENS[surface]
    # Per-tool guidance moved into each tool's own description (GPT-4.1 guide:
    # use the tools field, not the prompt). Nothing may reintroduce it here.
    assert "<tool_skills>" not in prompt
    if surface == "desktop":
        assert "Their words outrank the screen, memory, summaries, and prior topics" in normalized
    else:
        assert "Their latest finalized words are the task" in prompt
    assert "Background. Latest finalized user turn has authority." in prompt
    assert all(
        value in prompt
        for value in _EQUIVALENT_BASELINE_CONTEXT.values()
    )
    if surface == "keyboard":
        assert "opened voice from the mobile keyboard" in normalized
        assert prompt.index("Keyboard launch") < context_start
    else:
        assert "opened voice from the mobile keyboard" not in normalized
    if surface == "desktop":
        assert "Current screen evidence" in prompt[:context_start]
        assert prompt.index("Current screen evidence") < context_start
    else:
        assert "Current screen evidence" not in prompt


def test_static_prompt_has_no_session_placeholders_or_values() -> None:
    forbidden = (
        "{name}",
        "{local_time}",
        "{local_date}",
        "{timezone}",
        "archive_context",
        "user_aura_profile",
        "last_session_context",
        "memory_summary",
        "graph_context",
    )

    assert all(
        value not in prompt
        for prompt in (MOBILE_VOICE_SYSTEM_PROMPT, DESKTOP_VOICE_SYSTEM_PROMPT)
        for value in forbidden
    )


async def test_guide_mode_restores_byte_identical_companion_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent("desktop", _CONTEXT_ONE)
    companion = agent._companion_instructions
    instruction_updates: list[str] = []

    async def _update_instructions(instructions: str) -> None:
        instruction_updates.append(instructions)

    async def _update_tools(_tools: list) -> None:
        return None

    monkeypatch.setattr(agent, "update_instructions", _update_instructions)
    monkeypatch.setattr(agent, "update_tools", _update_tools)

    await agent.apply_guide_persona(True)
    await agent.apply_guide_persona(False)

    assert instruction_updates == [
        GUIDE_SYSTEM_PROMPT.format(name=_CONTEXT_ONE["name"]),
        companion,
    ]
    assert companion not in instruction_updates[0]
    assert instruction_updates[1].encode() == companion.encode()
