"""Regression coverage for bounded, authorization-neutral voice context."""

from __future__ import annotations

import json

from livekit.agents import llm as lk_llm

from src.agent.voice.action_policy import derive_turn_policy
from src.agent.voice.capabilities import VoiceSurface
from src.agent.voice.context_compaction import (
    HARD_RAW_TURN_CEILING,
    VoiceContextCompactor,
    build_compaction_snapshot,
    completed_turn_count,
)


def _add_turn(
    context: lk_llm.ChatContext,
    index: int,
    *,
    interrupted: bool = False,
    tool_status: str | None = None,
) -> None:
    context.add_message(role="user", content=[f"coding request {index}"])
    if tool_status is not None:
        call_id = f"call-{index}"
        context.items.append(
            lk_llm.FunctionCall(
                call_id=call_id,
                name="web_surf",
                arguments=json.dumps({"query": f"topic {index}"}),
            )
        )
        context.items.append(
            lk_llm.FunctionCallOutput(
                call_id=call_id,
                name="web_surf",
                output=(
                    '{"result":"verified"}'
                    if tool_status == "success"
                    else '{"error":true,"message":"timeout"}'
                ),
                is_error=False,
            )
        )
    context.items.append(
        lk_llm.ChatMessage(
            role="assistant",
            content=[f"assistant speculation {index}"],
            interrupted=interrupted,
        )
    )


async def _summary(_: str) -> str:
    return json.dumps(
        {
            "current_objective": "fix the coding issue",
            "current_topic": "coding",
            "user_constraints": ["make it maintainable"],
            "confirmed_facts": [],
            "decisions": [],
            "steps_already_attempted": [],
            "successful_tool_results": [],
            "failed_attempts": [],
            "pending_next_step": "generate the prompt",
            "explicitly_cancelled_intents": ["old reminder request"],
            "important_entities": ["Aura"],
        }
    )


def _summary_items(context: lk_llm.ChatContext) -> list[lk_llm.ChatMessage]:
    return [
        item
        for item in context.items
        if isinstance(item, lk_llm.ChatMessage)
        and item.role == "system"
        and item.text_content.startswith("<voice_session_summary>")
    ]


async def test_compaction_keeps_one_summary_and_last_eight_complete_turns():
    context = lk_llm.ChatContext()
    for index in range(16):
        _add_turn(context, index, tool_status="success" if index in {2, 10} else None)

    compactor = VoiceContextCompactor(session_id="session", summarize=_summary)
    assert compactor.maybe_schedule(context) is True
    await compactor.wait_for_idle()
    compacted = compactor.apply_ready(context)

    assert compacted is not None
    assert len(_summary_items(compacted)) == 1
    assert completed_turn_count(compacted) == 8
    retained_calls = {
        item.call_id for item in compacted.items if isinstance(item, lk_llm.FunctionCall)
    }
    retained_outputs = {
        item.call_id
        for item in compacted.items
        if isinstance(item, lk_llm.FunctionCallOutput)
    }
    assert retained_calls == retained_outputs == {"call-10"}


def test_interrupted_assistant_text_is_excluded_from_summary_input():
    context = lk_llm.ChatContext()
    _add_turn(context, 0, interrupted=True)
    for index in range(1, 17):
        _add_turn(context, index)
    snapshot = build_compaction_snapshot(context)
    assert snapshot is not None
    assert "assistant speculation 0" not in snapshot.serialized_turns
    assert "coding request 0" in snapshot.serialized_turns


async def test_stale_background_result_cannot_overwrite_changed_context():
    context = lk_llm.ChatContext()
    for index in range(16):
        _add_turn(context, index)
    compactor = VoiceContextCompactor(session_id="session", summarize=_summary)
    assert compactor.maybe_schedule(context)
    await compactor.wait_for_idle()
    context.items.insert(2, lk_llm.ChatMessage(role="system", content=["late tool state"]))
    assert compactor.apply_ready(context) is None


async def test_compaction_failure_cannot_change_structural_tool_exposure():
    async def _fail(_: str) -> str:
        raise RuntimeError("provider unavailable")

    context = lk_llm.ChatContext()
    for index in range(HARD_RAW_TURN_CEILING):
        _add_turn(context, index)
    compactor = VoiceContextCompactor(session_id="session", summarize=_fail)
    assert compactor.maybe_schedule(context)
    await compactor.wait_for_idle()
    compacted = compactor.enforce_hard_ceiling(context)
    assert compacted is not None
    assert completed_turn_count(compacted) == 10
    policy = derive_turn_policy(
        "Generate a prompt for my coding agent",
        compacted,
        VoiceSurface.DESKTOP,
        False,
        source_message_id="turn-21",
        turn_index=21,
    )
    unrelated_policy = derive_turn_policy(
        "Or tonight",
        compacted,
        VoiceSurface.DESKTOP,
        False,
        source_message_id="turn-21",
        turn_index=21,
    )
    assert policy.allowed_tools == unrelated_policy.allowed_tools
    assert "set_reminder" in policy.allowed_tools


async def test_one_hundred_turn_session_stays_bounded():
    context = lk_llm.ChatContext()
    compactor = VoiceContextCompactor(session_id="session", summarize=_summary)
    for index in range(100):
        _add_turn(context, index)
        if compactor.maybe_schedule(context):
            await compactor.wait_for_idle()
            context = compactor.apply_ready(context) or context
        context = compactor.enforce_hard_ceiling(context) or context
        assert completed_turn_count(context) <= HARD_RAW_TURN_CEILING
    assert len(_summary_items(context)) == 1
    assert completed_turn_count(context) <= HARD_RAW_TURN_CEILING
