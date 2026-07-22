"""Regression coverage for prompt-owned voice tool semantics."""

from __future__ import annotations

from livekit.agents import llm as lk_llm

from src.agent.voice.action_policy import derive_turn_policy, evaluate_execution
from src.agent.voice.capabilities import VoiceSurface
from src.agent.voice_prompt import VOICE_PROMPT


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
        '{"message":"call Mom","scheduled_at":"2026-07-19T21:00:00-07:00"}',
        policy,
        lk_llm.ChatContext(),
    )
    assert not decision.allowed
    assert decision.reason_code == "tool_not_exposed_for_turn"


def test_existing_voice_prompt_owns_action_semantics():
    normalized = " ".join(VOICE_PROMPT.split())
    assert "Use the conversation as one continuous exchange" in normalized
    assert "answers your immediately preceding clarification" in normalized
    assert "Never claim an action succeeded before its tool returns success" in normalized
