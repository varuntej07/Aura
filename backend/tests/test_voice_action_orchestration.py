"""Focused coverage for prompt-owned voice action orchestration."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from livekit.agents import Agent
from livekit.agents import llm as lk_llm
from livekit.agents.llm._provider_format.openai import to_fnc_ctx

from src.agent import buddy_agent as buddy_agent_module
from src.agent.buddy_agent import BuddyAgent
from src.agent.voice.action_policy import derive_turn_policy, evaluate_execution
from src.agent.voice.capabilities import (
    VOICE_TOOL_REGISTRY,
    ToolEffect,
    VoiceSurface,
)
from src.prompts import MOBILE_VOICE_SYSTEM_PROMPT
from src.services.memory.retrieval import RetrievedAtom
from src.shared.tools import CREATE_CALENDAR_EVENT_TOOL_DEFINITION


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
        "archive_context": "",
        "user_aura_profile": "",
    }


def _fake_tools(*names: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(info=SimpleNamespace(name=name)) for name in names]


def _tool_description(tool) -> str:
    """Read a bound local tool's advertised description, whitespace-normalized.

    Local tools come in two shapes and only one of them has `description`:
    FunctionToolInfo exposes it directly, while RawFunctionToolInfo (report_feedback,
    draft_outbound_message) carries a flat raw_schema instead. Normalizing whitespace
    matters because docstrings wrap, so a phrase like "action tool" can straddle a
    newline and defeat a naive substring check.
    """
    info = tool.info
    description = getattr(info, "description", None)
    if description is None:
        description = (getattr(info, "raw_schema", None) or {}).get("description", "")
    return " ".join(str(description or "").split())


async def _collect_llm(agent: BuddyAgent, context, tools):
    return [item async for item in agent.llm_node(context, tools, None)]


def _tool_chunk(
    chunk_id: str,
    name: str,
    arguments: str,
    call_id: str,
    *,
    extra: dict | None = None,
) -> lk_llm.ChatChunk:
    return lk_llm.ChatChunk(
        id=chunk_id,
        delta=lk_llm.ChoiceDelta(
            tool_calls=[
                lk_llm.FunctionToolCall(
                    name=name,
                    arguments=arguments,
                    call_id=call_id,
                    extra=extra,
                )
            ]
        ),
    )


async def _apply_safety(
    agent: BuddyAgent,
    chunks: list[object],
    *,
    surface: str = "app",
    frame: bool = False,
) -> list[object]:
    async def _chunks():
        for chunk in chunks:
            yield chunk

    return [
        item
        async for item in agent._apply_execution_safety(
            _chunks(),
            policy=_policy("wording is irrelevant", surface=surface, frame=frame),
            chat_ctx=lk_llm.ChatContext(),
        )
    ]


def _calls_from_output(output: list[object]) -> list[lk_llm.FunctionToolCall]:
    return [
        call
        for item in output
        if isinstance(item, lk_llm.ChatChunk) and item.delta is not None
        for call in item.delta.tool_calls
    ]


def test_voice_prompt_owns_action_semantics_without_slot_policy():
    normalized = " ".join(MOBILE_VOICE_SYSTEM_PROMPT.split())
    assert "Use the conversation as one continuous exchange" in normalized
    assert "answers your immediately preceding clarification" in normalized
    assert "never claim more than the envelope states" in normalized
    assert "reminder_exact_time" not in MOBILE_VOICE_SYSTEM_PROMPT
    assert "missing_slots" not in MOBILE_VOICE_SYSTEM_PROMPT


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
    assert "present_visible_artifact" in desktop_without_frame.allowed_tools
    assert "draft_outbound_message" not in desktop_without_frame.allowed_tools
    assert "draft_outbound_message" in desktop_with_frame.allowed_tools


def test_draft_execution_gate_requires_operation():
    policy = _policy("Draft a reply", surface="desktop", frame=True)
    missing = evaluate_execution(
        "draft_outbound_message",
        "{}",
        policy,
        lk_llm.ChatContext(),
    )
    valid = evaluate_execution(
        "draft_outbound_message",
        '{"operation":"new"}',
        policy,
        lk_llm.ChatContext(),
    )

    assert (missing.allowed, missing.reason_code) == (
        False,
        "missing_required_tool_field",
    )
    assert (valid.allowed, valid.reason_code) == (True, "execution_allowed")


def test_actual_bound_draft_tool_serializes_to_exact_openai_schema():
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
    )

    serialized = to_fnc_ctx(
        lk_llm.ToolContext([agent.draft_outbound_message]),
        strict=True,
    )

    assert serialized == [
        {
            "type": "function",
            "function": {
                "name": "draft_outbound_message",
                "description": (
                    "Create or revise copy-ready prose the user will send, post, or "
                    "submit to people using their current desktop screen. Use for "
                    "email replies, DMs, comments, posts, reviews, bios, and "
                    "application responses. Do not use for prompts, code, commands, "
                    "configuration, calendar events, reminders, trackers, or ordinary "
                    "spoken answers. The draft is rendered as a card and is never sent "
                    "automatically."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["new", "refine"],
                            "description": (
                                "Use new to create a separate draft. Use refine only "
                                "to modify the current draft."
                            ),
                        }
                    },
                    "required": ["operation"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }
    ]


@pytest.mark.parametrize(
    "legacy_field",
    ("channel", "length", "recipient_hint", "intent", "refine_instruction"),
)
async def test_bound_draft_tool_rejects_every_legacy_argument(
    monkeypatch,
    legacy_field: str,
):
    calls: list[dict] = []

    async def _draft(*_args, **kwargs):
        calls.append(kwargs)
        return buddy_agent_module.SPOKEN_DRAFT_READY

    monkeypatch.setattr(buddy_agent_module, "run_draft_tool", _draft)
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
    )
    agent._finalized_transcript = "Draft a concise reply."

    with pytest.raises(lk_llm.ToolError, match="requires exactly one field"):
        await agent.draft_outbound_message(
            None,
            {"operation": "new", legacy_field: "legacy"},
        )

    assert calls == []


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
        '{"message":"call Mom","when":"tonight at 9 PM"}',
        policy,
        context,
    )

    assert (unknown.allowed, unknown.reason_code) == (False, "unregistered_voice_tool")
    assert (missing.allowed, missing.reason_code) == (
        False,
        "missing_required_tool_field",
    )
    assert (valid.allowed, valid.reason_code) == (True, "execution_allowed")


@pytest.mark.parametrize(
    "missing_field",
    CREATE_CALENDAR_EVENT_TOOL_DEFINITION["inputSchema"]["required"],
)
def test_calendar_execution_gate_rejects_each_missing_canonical_field(
    missing_field: str,
):
    arguments = {
        "title": "Lunch",
        "when": "tomorrow at noon",
        "description": "",
        "location": "",
        "attendees": [],
    }
    del arguments[missing_field]

    decision = evaluate_execution(
        "create_calendar_event",
        json.dumps(arguments),
        _policy("Schedule lunch"),
        lk_llm.ChatContext(),
    )

    assert (decision.allowed, decision.reason_code) == (
        False,
        "missing_required_tool_field",
    )


def test_calendar_execution_gate_accepts_empty_optional_content_values():
    decision = evaluate_execution(
        "create_calendar_event",
        json.dumps(
            {
                "title": "Lunch",
                "when": "tomorrow at noon",
                "description": "",
                "location": "",
                "attendees": [],
            }
        ),
        _policy("Schedule lunch"),
        lk_llm.ChatContext(),
    )

    assert (decision.allowed, decision.reason_code) == (
        True,
        "execution_allowed",
    )


def test_every_current_voice_tool_has_registry_metadata():
    assert set(VOICE_TOOL_REGISTRY) == {
        "set_reminder",
        "list_reminders",
        "cancel_reminder",
        "create_calendar_event",
        "update_calendar_event",
        "get_upcoming_events",
        "store_memory",
        "delete_memory",
        "query_memory",
        "web_surf",
        "get_user_context",
        "report_feedback",
        "track_topic",
        "draft_outbound_message",
        "present_visible_artifact",
        "set_guide_mode",
    }


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
    assert [tool.info.name for tool in captured["tools"]] == ["set_reminder"]
    passed_context = captured["context"]
    passed_text = [item.text_content for item in passed_context.items]
    assert passed_text[:3] == [
        "Remind me to call Mom",
        "When should I remind you?",
        "Or tonight",
    ]
    assert "<active_intent_state>" in passed_text[3]


async def test_parallel_safe_reads_from_separate_chunks_survive_in_order():
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
    )
    first = _tool_chunk(
        "chunk-1",
        "query_memory",
        '{"query":"launch plan"}',
        "memory-read",
        extra={"provider": "first"},
    )
    second = _tool_chunk(
        "chunk-2",
        "web_surf",
        '{"query":"latest launch news"}',
        "web-read",
        extra={"provider": "second"},
    )

    output = await _apply_safety(agent, [first, second])
    calls = _calls_from_output(output)

    assert len(output) == 1
    assert [(call.name, call.call_id) for call in calls] == [
        ("query_memory", "memory-read"),
        ("web_surf", "web-read"),
    ]
    assert [call.arguments for call in calls] == [
        '{"query":"launch plan"}',
        '{"query":"latest launch news"}',
    ]
    assert [call.extra for call in calls] == [
        {"provider": "first"},
        {"provider": "second"},
    ]


async def test_unsafe_first_survives_and_later_read_is_deferred():
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
    )
    output = await _apply_safety(
        agent,
        [
            _tool_chunk(
                "chunk-1",
                "set_reminder",
                '{"message":"Call Mom","when":"tomorrow at 9 AM"}',
                "reminder-write",
            ),
            _tool_chunk(
                "chunk-2",
                "query_memory",
                '{"query":"Mom"}',
                "memory-read",
            ),
        ],
    )

    assert [call.name for call in _calls_from_output(output)] == ["set_reminder"]


async def test_safe_read_first_survives_and_later_unsafe_call_is_deferred():
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
    )
    output = await _apply_safety(
        agent,
        [
            _tool_chunk(
                "chunk-1",
                "get_upcoming_events",
                '{"range_name":"tomorrow"}',
                "calendar-read",
            ),
            _tool_chunk(
                "chunk-2",
                "set_reminder",
                '{"message":"Call Mom","when":"tomorrow at 9 AM"}',
                "reminder-write",
            ),
        ],
    )

    assert [call.name for call in _calls_from_output(output)] == [
        "get_upcoming_events"
    ]


async def test_same_generation_allows_at_most_one_side_effect():
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
    )
    output = await _apply_safety(
        agent,
        [
            _tool_chunk(
                "chunk-1",
                "create_calendar_event",
                (
                    '{"title":"Lunch","when":"tomorrow at noon",'
                    '"description":"","location":"","attendees":[]}'
                ),
                "calendar-write",
            ),
            _tool_chunk(
                "chunk-2",
                "set_reminder",
                '{"message":"Lunch","when":"tomorrow at 11 AM"}',
                "reminder-write",
            ),
        ],
    )

    assert [call.name for call in _calls_from_output(output)] == [
        "create_calendar_event"
    ]


async def test_partial_rejection_with_surviving_call_has_no_failure_sentence():
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
    )
    output = await _apply_safety(
        agent,
        [
            _tool_chunk(
                "chunk-1",
                "query_memory",
                '{"query":"launch plan"}',
                "memory-read",
            ),
            _tool_chunk("chunk-2", "not_a_tool", "{}", "invalid"),
        ],
    )

    assert [call.name for call in _calls_from_output(output)] == ["query_memory"]
    assert all(not isinstance(item, str) for item in output)


async def test_fully_rejected_generation_without_text_gets_failure_sentence():
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
    )
    output = await _apply_safety(
        agent,
        [_tool_chunk("chunk-1", "not_a_tool", "{}", "invalid")],
    )

    assert _calls_from_output(output) == []
    assert output == ["Hmm, that didn't go through. Say it once more?"]


async def test_single_valid_call_is_preserved_unchanged():
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
    )
    call = lk_llm.FunctionToolCall(
        name="query_memory",
        arguments='{"query":"launch plan"}',
        call_id="memory-read",
        extra={"provider": "kept"},
    )
    chunk = lk_llm.ChatChunk(
        id="chunk-1",
        delta=lk_llm.ChoiceDelta(tool_calls=[call]),
    )

    output = await _apply_safety(agent, [chunk])

    assert output == [chunk]
    assert _calls_from_output(output) == [call]


async def test_parallel_telemetry_records_each_call_once():
    telemetry = SimpleNamespace(
        turn_index=0,
        emitted=Mock(),
        deferred=Mock(),
    )
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
    )
    agent._action_telemetry = telemetry

    output = await _apply_safety(
        agent,
        [
            _tool_chunk(
                "chunk-1",
                "query_memory",
                '{"query":"launch plan"}',
                "memory-read",
            ),
            _tool_chunk(
                "chunk-2",
                "set_reminder",
                '{"message":"Call Mom","when":"tomorrow at 9 AM"}',
                "reminder-write",
            ),
            _tool_chunk(
                "chunk-3",
                "create_calendar_event",
                (
                    '{"title":"Lunch","when":"tomorrow at noon",'
                    '"description":"","location":"","attendees":[]}'
                ),
                "calendar-write",
            ),
        ],
    )

    assert [call.name for call in _calls_from_output(output)] == ["query_memory"]
    telemetry.emitted.assert_called_once_with(
        "query_memory", "execution_allowed"
    )
    assert [entry.args for entry in telemetry.deferred.call_args_list] == [
        ("set_reminder", "unsafe_parallel_tool_batch"),
        ("create_calendar_event", "side_effect_already_emitted"),
    ]


async def test_finalized_artifact_side_effect_invalidates_speculative_generation():
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
    )
    agent._launch_surface = VoiceSurface.DESKTOP
    turn_context = lk_llm.ChatContext()
    message = lk_llm.ChatMessage(role="user", content=["Give me the command"])

    await agent.on_user_turn_completed(turn_context, message)

    assert len(turn_context.items) == 1
    assert "finalized_side_effect" in turn_context.items[0].text_content


async def test_tool_and_policy_telemetry_survive_native_tool_calling(monkeypatch):
    context = lk_llm.ChatContext()
    message = context.add_message(role="user", content=["Remind me tonight"])
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
                            '"when":"tonight at 9 PM"}'
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
    """Surface Routing Contract: the desktop card/draft tools must never
    reabsorb requests an action tool owns (the 2026-07-20 transcript bug where
    "create me a calendar event" was routed to present_visible_artifact), and
    the old "note for other reusable text" catch-all must stay gone.

    Reads the tool's own description, which is where this guidance now lives.
    """
    from src.agent.buddy_agent import BuddyAgent

    visible_artifact = _tool_description(BuddyAgent.present_visible_artifact)
    assert "never use it as a substitute for a real action" in visible_artifact.lower()
    assert "action tool" in visible_artifact
    assert "note for other reusable text" not in visible_artifact


def test_tool_descriptions_contain_selection_and_argument_guidance_only():
    """A description says WHEN to call and WHAT the arguments mean. It must not
    script what to say after the call; the Action Truth envelope owns that."""
    from src.agent.buddy_agent import BuddyAgent
    from src.shared.tools import TOOL_DEFINITIONS

    local_tools = (
        BuddyAgent.report_feedback,
        BuddyAgent.draft_outbound_message,
        BuddyAgent.present_visible_artifact,
        BuddyAgent.set_guide_mode,
    )
    resident = " ".join(
        [definition["description"] for definition in TOOL_DEFINITIONS]
        + [_tool_description(tool) for tool in local_tools]
    ).lower()
    execution_phrases = (
        "report only what the tool returns",
        "report the returned local times",
        "then speak the `say` line",
        "after calling it",
        "never speak the draft itself",
        "tool owns acknowledgement",
        "say only the short line",
        "do not claim it is already active",
    )
    for phrase in execution_phrases:
        assert phrase not in resident


def test_system_prompt_has_one_resident_action_truth_sentence():
    normalized = " ".join(MOBILE_VOICE_SYSTEM_PROMPT.split())
    assert normalized.count("Action Truth envelope") == 1
    assert "render the result by `render`, follow `then`" in normalized
    assert "Presentation tools own their speech" not in normalized
    assert "When a tool's result includes a `say` field" not in normalized
