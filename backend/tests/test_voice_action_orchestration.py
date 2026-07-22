"""Focused coverage for prompt-owned voice action orchestration."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import Mock

from livekit.agents import Agent
from livekit.agents import llm as lk_llm

from src.agent import buddy_agent as buddy_agent_module
from src.agent.buddy_agent import BuddyAgent
from src.agent.voice.action_policy import derive_turn_policy, evaluate_execution
from src.agent.voice.capabilities import (
    VOICE_TOOL_REGISTRY,
    ToolEffect,
    VoiceSurface,
)
from src.agent.voice.tool_skills import VOICE_TOOL_SKILLS
from src.agent.voice_prompt import VOICE_PROMPT
from src.services.memory.retrieval import RetrievedAtom


def _policy(
    text: str,
    *,
    surface: str = "app",
    frame: bool = False,
    finalized: bool = True,
):
    return derive_turn_policy(
        text,
        lk_llm.ChatContext(),
        VoiceSurface(surface),
        frame,
        finalized_turn=finalized,
    )


def _agent_context_vars() -> dict[str, str]:
    return {
        "name": "V",
        "timezone": "America/Los_Angeles",
        "local_time": "8:00 PM",
        "local_date": "July 19, 2026",
        "memory_summary": "",
        "graph_context": "",
        "last_session_context": "",
        "last_session_at": "",
        "archive_context": "",
        "user_aura_profile": "",
        "surface": "",
        "screen_sight": "",
    }


def _fake_tools(*names: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(info=SimpleNamespace(name=name)) for name in names]


async def _collect_llm(agent: BuddyAgent, context, tools):
    return [item async for item in agent.llm_node(context, tools, None)]


def test_voice_prompt_owns_action_semantics_without_slot_policy():
    normalized = " ".join(VOICE_PROMPT.split())
    assert "Use the conversation as one continuous exchange" in normalized
    assert "answers your immediately preceding clarification" in normalized
    assert "Never claim an action succeeded before its tool returns success" in normalized
    assert "reminder_exact_time" not in VOICE_PROMPT
    assert "missing_slots" not in VOICE_PROMPT


def test_tool_exposure_is_identical_for_different_language():
    utterances = (
        "Remind me to call Mom",
        "Or tonight",
        "Why not?",
        "Tell me a quick joke",
    )
    policies = [_policy(utterance) for utterance in utterances]
    assert all(policy.allowed_tools == policies[0].allowed_tools for policy in policies)
    assert "set_reminder" in policies[0].allowed_tools
    assert "cancel_reminder" in policies[0].allowed_tools
    assert "create_calendar_event" in policies[0].allowed_tools


def test_surface_and_fresh_frame_are_the_only_desktop_tool_boundaries():
    app = _policy("anything", surface="app")
    desktop_without_frame = _policy("anything", surface="desktop", frame=False)
    desktop_with_frame = _policy("anything", surface="desktop", frame=True)

    assert "present_visible_artifact" not in app.allowed_tools
    assert "save_screen_item" not in app.allowed_tools
    assert "present_visible_artifact" in desktop_without_frame.allowed_tools
    assert "save_screen_item" not in desktop_without_frame.allowed_tools
    assert "draft_outbound_message" not in desktop_without_frame.allowed_tools
    assert "save_screen_item" in desktop_with_frame.allowed_tools
    assert "draft_outbound_message" in desktop_with_frame.allowed_tools


def test_speculative_generation_exposes_reads_only():
    policy = _policy("Remind me tonight", finalized=False)
    assert policy.allowed_tools
    assert all(
        VOICE_TOOL_REGISTRY[name].effect is ToolEffect.READ
        for name in policy.allowed_tools
    )


def test_execution_gate_checks_registration_exposure_and_required_fields():
    policy = _policy("wording is irrelevant")
    context = lk_llm.ChatContext()

    unknown = evaluate_execution("not_a_tool", "{}", policy, context)
    missing = evaluate_execution(
        "set_reminder", '{"message":"call Mom"}', policy, context
    )
    valid = evaluate_execution(
        "set_reminder",
        '{"message":"call Mom","scheduled_at":"2026-07-19T21:00:00-07:00"}',
        policy,
        context,
    )

    assert (unknown.allowed, unknown.reason_code) == (False, "unregistered_voice_tool")
    assert (missing.allowed, missing.reason_code) == (
        False,
        "missing_required_tool_field",
    )
    assert (valid.allowed, valid.reason_code) == (True, "execution_allowed")


def test_every_current_voice_tool_has_registry_metadata():
    assert set(VOICE_TOOL_REGISTRY) == {
        "set_reminder",
        "list_reminders",
        "cancel_reminder",
        "create_calendar_event",
        "get_upcoming_events",
        "store_memory",
        "query_memory",
        "web_surf",
        "get_user_context",
        "report_feedback",
        "track_topic",
        "save_screen_item",
        "draft_outbound_message",
        "present_visible_artifact",
    }


def test_registered_tool_skills_resolve_without_orphans():
    registered_skills = {
        item.skill_name for item in VOICE_TOOL_REGISTRY.values() if item.skill_name
    }
    assert registered_skills == set(VOICE_TOOL_SKILLS)


async def test_original_followup_reaches_existing_model_with_reminder_tool(monkeypatch):
    context = lk_llm.ChatContext()
    context.add_message(role="user", content=["Remind me to call Mom"])
    context.add_message(role="assistant", content=["When should I remind you?"])
    message = context.add_message(role="user", content=["Or tonight"])
    tools = _fake_tools("set_reminder", "query_memory", "web_surf")
    captured: dict[str, object] = {}

    async def _default(_agent, passed_context, passed_tools, _settings):
        captured.update(context=passed_context, tools=passed_tools)
        yield "model decides from the existing conversation"

    monkeypatch.setattr(Agent.default, "llm_node", _default)
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
    )
    agent._finalized_message_id = message.id
    agent._finalized_transcript = message.text_content

    output = await _collect_llm(agent, context, tools)

    assert output == ["model decides from the existing conversation"]
    assert [tool.info.name for tool in captured["tools"]] == [
        "set_reminder",
        "query_memory",
        "web_surf",
    ]
    passed_context = captured["context"]
    assert [item.text_content for item in passed_context.items] == [
        "Remind me to call Mom",
        "When should I remind you?",
        "Or tonight",
    ]


async def test_same_generation_allows_at_most_one_side_effect():
    policy = _policy("wording is irrelevant")
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
    )
    chunk = lk_llm.ChatChunk(
        id="chunk",
        delta=lk_llm.ChoiceDelta(
            tool_calls=[
                lk_llm.FunctionToolCall(
                    name="get_upcoming_events",
                    arguments='{"range_name":"tomorrow"}',
                    call_id="read",
                ),
                lk_llm.FunctionToolCall(
                    name="create_calendar_event",
                    arguments=(
                        '{"title":"Lunch","start_time":"2026-07-20T12:00:00-07:00"}'
                    ),
                    call_id="calendar-write",
                ),
                lk_llm.FunctionToolCall(
                    name="set_reminder",
                    arguments=(
                        '{"message":"Lunch","scheduled_at":"2026-07-20T11:00:00-07:00"}'
                    ),
                    call_id="reminder-write",
                ),
            ]
        ),
    )

    async def _chunks():
        yield chunk

    output = [
        item
        async for item in agent._apply_execution_safety(
            _chunks(), policy=policy, chat_ctx=lk_llm.ChatContext()
        )
    ]

    assert [call.name for call in output[0].delta.tool_calls] == [
        "get_upcoming_events",
        "create_calendar_event",
    ]


async def test_every_finalized_turn_invalidates_speculative_generation():
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
    )
    for text in ("Remind me tonight", "Tell me a joke"):
        turn_context = lk_llm.ChatContext()
        message = lk_llm.ChatMessage(role="user", content=[text])
        await agent.on_user_turn_completed(turn_context, message)
        assert len(turn_context.items) == 1
        assert "finalized transcript" in turn_context.items[0].text_content


async def test_tool_and_policy_telemetry_survive_native_tool_calling(monkeypatch):
    context = lk_llm.ChatContext()
    message = context.add_message(role="user", content=["Or tonight"])
    telemetry = SimpleNamespace(
        turn_index=1,
        start_turn=Mock(),
        policy=Mock(),
        first_response=Mock(),
        emitted=Mock(),
        deferred=Mock(),
        execution=Mock(),
    )

    async def _default(_agent, _context, _tools, _settings):
        yield lk_llm.ChatChunk(
            id="tool-call",
            delta=lk_llm.ChoiceDelta(
                tool_calls=[
                    lk_llm.FunctionToolCall(
                        name="set_reminder",
                        arguments=(
                            '{"message":"call Mom",'
                            '"scheduled_at":"2026-07-19T21:00:00-07:00"}'
                        ),
                        call_id="reminder",
                    )
                ]
            ),
        )

    monkeypatch.setattr(Agent.default, "llm_node", _default)
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
    )
    agent._action_telemetry = telemetry
    agent._finalized_message_id = message.id
    agent._finalized_transcript = message.text_content

    output = await _collect_llm(
        agent,
        context,
        _fake_tools("set_reminder", "query_memory"),
    )
    agent.record_voice_tool_execution("set_reminder", success=True)

    assert [call.name for call in output[0].delta.tool_calls] == ["set_reminder"]
    telemetry.policy.assert_called_once()
    telemetry.emitted.assert_called_once_with("set_reminder", "execution_allowed")
    telemetry.execution.assert_called_once_with("set_reminder", success=True)


async def test_graph_retrieval_exception_does_not_drop_voice_reply(monkeypatch):
    async def _boom(*_args, **_kwargs):
        raise RuntimeError("firestore failed")

    async def _default(_agent, _ctx, _tools, _settings):
        yield "still replying"

    monkeypatch.setattr(buddy_agent_module, "retrieve_relevant_subgraph", _boom)
    monkeypatch.setattr(Agent.default, "llm_node", _default)
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
    )
    turn_ctx = lk_llm.ChatContext()
    message = lk_llm.ChatMessage(role="user", content=["What was my interview plan?"])

    await agent.on_user_turn_completed(turn_ctx, message)
    output = await _collect_llm(agent, turn_ctx, [])

    assert output == ["still replying"]
    assert agent._finalized_message_id == message.id


async def test_slow_live_graph_retrieval_respects_turn_budget(monkeypatch):
    async def _slow(*_args, **_kwargs):
        await asyncio.sleep(0.2)
        return [RetrievedAtom("late", "fact", 0.9, 0.9)]

    monkeypatch.setattr(buddy_agent_module, "VOICE_RETRIEVAL_BUDGET_S", 0.03)
    monkeypatch.setattr(buddy_agent_module, "retrieve_relevant_subgraph", _slow)
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
    )
    turn_ctx = lk_llm.ChatContext()
    message = lk_llm.ChatMessage(role="user", content=["What was my interview plan?"])

    started = time.monotonic()
    await agent.on_user_turn_completed(turn_ctx, message)

    assert time.monotonic() - started < 0.12
    assert all("late" not in item.text_content for item in turn_ctx.items)


async def test_smalltalk_skips_live_graph_without_calling_retrieval(monkeypatch):
    calls = 0

    async def _retrieve(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(buddy_agent_module, "retrieve_relevant_subgraph", _retrieve)
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
    )

    await agent.on_user_turn_completed(
        lk_llm.ChatContext(),
        lk_llm.ChatMessage(role="user", content=["thanks"]),
    )

    assert calls == 0


def test_card_skills_yield_actions_to_dedicated_tools():
    """Surface Routing Contract: the desktop card/draft skills must never
    reabsorb requests an action tool owns (the 2026-07-20 transcript bug where
    "create me a calendar event" was routed to present_visible_artifact), and
    the old "note for other reusable text" catch-all must stay gone."""
    from src.agent.voice.tool_skills import VOICE_TOOL_SKILLS

    visible_artifact = VOICE_TOOL_SKILLS["visible_artifact"].instruction
    outbound_draft = VOICE_TOOL_SKILLS["outbound_draft"].instruction
    assert "never a substitute" in visible_artifact
    assert "action tool" in visible_artifact
    assert "never a substitute" in outbound_draft
    assert "note for other reusable text" not in visible_artifact

    calendar_write = VOICE_TOOL_SKILLS["calendar_write"].instruction
    assert "never a card" in calendar_write
    assert "ONE" in calendar_write  # single-question delegation rule
