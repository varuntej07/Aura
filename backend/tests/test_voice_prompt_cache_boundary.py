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
from src.agent.voice_prompt import (
    VOICE_SESSION_CONTEXT_START,
    render_voice_session_context,
)
from src.prompts import (
    DESKTOP_VOICE_SYSTEM_PROMPT,
    GUIDE_SYSTEM_PROMPT,
    MOBILE_VOICE_SYSTEM_PROMPT,
    VOICE_TURN_CLOSING_CHECK,
)

_ENCODING = tiktoken.get_encoding("o200k_base")
# A bloat ceiling, not a hard API limit: the whole prompt is spoken-turn latency and it
# is paid on every session. Raised once, by the measured cost of _AURA_PRODUCT_TRUTH,
# after Buddy invented a cross-device limitation it does not have because no prompt on
# any surface stated a single fact about Aura. Raised a second time (2026-09), by the
# measured cost of the Session facts blocks and the catalog-derived capability digest,
# after a live desktop session had Buddy tell a speaking user "I can't actually hear
# you, but I can read everything you type" — LiveKit's prompting guide names the gap:
# the LLM in an STT-LLM-TTS pipeline "has no built-in understanding of its own position
# in a voice pipeline." Raised a third time (2026-09-08, desktop only), by the measured
# cost of the Action Truth rule against announcing a durable action before its envelope
# returns, after a live desktop session told a free user four separate times that a
# background research run was in progress when none was ever created and the correct
# paid-plan refusal was sitting unread in the tool's own `say`. Anything added here has
# to earn its tokens the same way; do not raise these to make a comfortable prompt fit.
#
# Raised a fourth time (2026-09-08, all three surfaces, ~85 tokens each) by the measured
# cost of the self-harm exception and the never-guess-an-age rule in _SAFETY_AND_STOP_RULES.
# Neither is optional: the backend had NO crisis handling of any kind (a grep for
# suicid|self-harm|crisis|hotline|988 across src/ returned one alarm ringtone slug), while
# California SB 243 and New York's AI companion law both require detecting and responding to
# expressions of suicidal ideation, and this product meets their definition of a companion
# chatbot. The age line answers a live session that guessed a user's age and then treated
# their real one as banter. These are the cheapest correct versions of both, already trimmed
# once from 173 tokens to 121.
#
# Raised a fifth time (2026-09-09) by two blocks that each answer a defect observed in
# ONE live session, sess_09615c9ddc704982b976def51c520a5f:
#   * _MEMORY_FACTS (~91 tokens, all three surfaces). Asked "Do you remember our last
#     conversation?", Buddy said it had none and that "each session is independent for
#     your privacy and security". The worker logs show memory retrieval SUCCEEDED that
#     turn (memory_outcome "ok", 6-14 atoms) with no pre-session fetch timeout, so the
#     data was in the prompt and only the capability claim was missing. Three existing
#     lines lean the other way ("never from memory or conversation history", "never
#     introduce them", "do not answer from memory"), all meaning do-not-fabricate. With
#     nothing affirmative to read, the model fell back to generic-assistant boilerplate.
#     Same failure class as _*_VOICE_SESSION_FACTS and the same fix.
#   * The screen-scope clause (~73 further tokens, desktop only). The user said "What the
#     fuck? Busy drill." and got a bullet list about the repo on their screen, then asked
#     "Who the fuck told you to talk about my screen?". A frame is attached to EVERY armed
#     turn with no relevance gate, so the rule has to say that presence is not a request.
#     This partly replaces the old one-line "Never narrate or expand from screen evidence
#     unless asked", which sat next to the opposite instruction and lost to it.
# Both were compressed once before landing (memory 167 -> 91, screen 110 -> 73). The cost
# is paid on turn 1 only: pipelines.py pins prompt_cache_key per user on every OpenAI leg
# and caching="ephemeral" on the Anthropic leg, so turn 2+ reads this prefix from cache.
#
# Each ceiling sits ~48 tokens above its measured prompt, so unintended growth still
# trips the guard rather than being absorbed by it.
# Raised a sixth time (2026-09-09, ~98 tokens on all three surfaces) by "How a turn
# opens and closes" in _SPOKEN_DELIVERY plus the two-line hello rule in the identity
# block. A live desktop session opened nearly every turn with an affirmation ("You're
# right, you didn't ask about your screen"), closed nearly every turn with a service
# offer ("if you want X, just say so"), answered "good morning" with a well-wish
# paragraph, and spent whole turns promising to be shorter instead of being shorter.
# Two of those four had no rule at all. The other two DID, and lost: anti-acknowledgement
# was stated in both _CONVERSATION_AUTHORITY and _SPOKEN_DELIVERY, which is the same
# dilution the comment above _SPOKEN_DELIVERY diagnoses and claims to have removed - the
# block was added while the originals stayed. Stating it once is the fix; the duplicates
# are deleted here, which is why the net is +98 and not the +145 the new block costs.
# The closing offer additionally had a competing INSTRUCTION in voice/tool_result.py
# ("Offer to go further rather than going further unasked") that no prompt wording could
# outrank; that line is deleted rather than argued with. Compressed once before landing,
# 145 -> 98, per the rule above. HBS's analysis of 1,200 companion chats puts this exact
# closing-offer shape among the tactics that make users angry and churn, so it is a
# retention defect and not a style preference.
#
# Raised deliberately on 2026-09-09, +143 app, +143 keyboard, +177 desktop, after a live
# desktop session in which every rule above was present and none of them held: the
# model opened on a concession and closed on a service offer, turn after turn. Two
# structural changes, not more words:
#   - VOICE_TURN_CLOSING_CHECK (101 tokens) now terminates the assembled prompt, after
#     the session block. Position was the defect. _SPOKEN_DELIVERY sat at roughly the
#     midpoint of a ~9k-token turn, which the architecture doc itself names as where
#     attention decays, and the desktop prompt ended on an age-and-crisis rule.
#   - Two contrastive <example> blocks in _SPOKEN_DELIVERY. gpt-4.1 mirrors the shape
#     of its prompt far more reliably than it obeys a prohibition, and every rule these
#     illustrate was already stated and already ignored.
# Paid for partly by deleting the mobile in-body "Final check", which the closing block
# now covers - same de-duplication rule as the paragraph above, applied again.
_BEFORE_TOTAL_TOKENS = {
    "app": 2513,
    "keyboard": 2566,
    "desktop": 2884,
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
    # The session block is no longer the literal tail: VOICE_TURN_CLOSING_CHECK is
    # appended after it, deliberately, because the end of context is where adherence
    # is highest and the register rules were otherwise buried mid-prompt. The
    # invariant this guards is unchanged - nothing dynamic survives past the cache
    # boundary and the tail is deterministic - so it now asserts both segments.
    assert first_prompt.endswith(
        render_voice_session_context(_CONTEXT_ONE) + VOICE_TURN_CLOSING_CHECK
    )
    assert second_prompt.endswith(
        render_voice_session_context(_CONTEXT_TWO) + VOICE_TURN_CLOSING_CHECK
    )
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

    assert prompt.endswith(
        render_voice_session_context(_EQUIVALENT_BASELINE_CONTEXT)
        + VOICE_TURN_CLOSING_CHECK
    )
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


def test_guide_mode_prompt_is_isolated_from_buddy() -> None:
    agent = _agent("desktop", _CONTEXT_ONE)
    guide_prompt = GUIDE_SYSTEM_PROMPT.format(name=_CONTEXT_ONE["name"])

    assert not hasattr(agent, "apply_guide_persona")
    assert guide_prompt != agent.instructions
    assert "plan_guide_task" in guide_prompt
    assert "You are Buddy, the companion inside Aura." in agent.instructions
    assert "plan_guide_task" not in agent.instructions
