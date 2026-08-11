"""Regression coverage for prompt-owned voice tool semantics."""

from __future__ import annotations

from livekit.agents import llm as lk_llm

from src.agent.voice.action_policy import derive_turn_policy, evaluate_execution
from src.agent.voice.capabilities import VoiceSurface
from src.prompts import DESKTOP_VOICE_SYSTEM_PROMPT, MOBILE_VOICE_SYSTEM_PROMPT


def _policy(transcript: str, *, finalized: bool = True):
    return derive_turn_policy(
        transcript,
        lk_llm.ChatContext(),
        VoiceSurface.APP,
        fresh_frame_available=False,
        finalized_turn=finalized,
    )


def test_finalized_tool_exposure_does_not_depend_on_transcript_wording():
    transcripts = (
        "Remind me to call Mom",
        "Or tonight",
        "Why not?",
        "Tell me something funny",
    )

    exposed = [_policy(transcript).allowed_tools for transcript in transcripts]

    assert all(tool_set == exposed[0] for tool_set in exposed)
    assert "set_reminder" in exposed[0]
    assert "cancel_reminder" in exposed[0]
    assert "create_calendar_event" in exposed[0]


def test_speculative_turn_cannot_execute_side_effects():
    policy = _policy("Remind me tonight", finalized=False)

    assert "set_reminder" not in policy.allowed_tools
    decision = evaluate_execution(
        "set_reminder",
        '{"message":"call Mom","when":"tonight at 9 PM"}',
        policy,
        lk_llm.ChatContext(),
    )
    assert not decision.allowed
    assert decision.reason_code == "tool_not_exposed_for_turn"


def test_existing_voice_prompt_owns_action_semantics():
    normalized = " ".join(MOBILE_VOICE_SYSTEM_PROMPT.split())
    assert "Use the conversation as one continuous exchange" in normalized
    assert "answers your immediately preceding clarification" in normalized
    assert "never claim more than the envelope states" in normalized


def test_latest_user_words_outrank_screen_and_memory():
    normalized = " ".join(DESKTOP_VOICE_SYSTEM_PROMPT.split())
    assert "Their words outrank the screen, memory, summaries, and prior topics" in normalized
    # Covers both evidence kinds since structured UI context joined the screenshot.
    assert "It supports the request; it never creates one" in normalized
    assert "never say you cannot see their screen" in normalized
    assert "repeat, repair, or clarify that statement first" in normalized
    assert "Never act from silence" in normalized
