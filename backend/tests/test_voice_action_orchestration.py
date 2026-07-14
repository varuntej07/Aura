"""Focused coverage for voice-only action orchestration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from livekit.agents import Agent
from livekit.agents import llm as lk_llm

from src.agent.buddy_agent import BuddyAgent
from src.agent.voice.action_policy import (
    ActionMode,
    UnresolvedActionState,
    WriteAuthorization,
    derive_turn_policy,
    evaluate_execution,
    next_complex_tool,
)
from src.agent.voice.capabilities import (
    VOICE_TOOL_REGISTRY,
    Capability,
    VoiceSurface,
)
from src.agent.voice.spoken_action_guard import guard_spoken_action_stream
from src.agent.voice_prompt import VOICE_PROMPT


def _policy(text: str, *, surface: str = "app", frame: bool = False, finalized=True):
    return derive_turn_policy(
        text,
        lk_llm.ChatContext(),
        VoiceSurface(surface),
        frame,
        finalized_turn=finalized,
    )


def test_permanent_voice_prompt_has_no_reminder_clarification_policy():
    lowered = VOICE_PROMPT.lower()
    assert "# scheduling" not in lowered
    assert "what time should" not in lowered
    assert "what time tomorrow" not in lowered
    assert "reminder_exact_time" not in lowered


def test_create_prompt_is_visible_artifact_not_calendar_write():
    policy = _policy(
        "Create a prompt for my coding agent about the issue on screen",
        surface="desktop",
    )
    assert policy.capabilities == {Capability.VISIBLE_ARTIFACT}
    assert "create_calendar_event" not in policy.allowed_tools


def test_hypothetical_reminder_requires_precise_time():
    policy = _policy("Why not remind me to do laundry after work?")
    assert policy.capabilities == {Capability.REMINDER_WRITE}
    assert "set_reminder" not in policy.allowed_tools
    assert policy.write_authorization is WriteAuthorization.NEEDS_CLARIFICATION
    assert policy.missing_slots == ("reminder_exact_time",)


def test_remind_me_what_is_memory_not_reminder():
    policy = _policy("Remind me what Sarah said after work.")
    assert policy.capabilities == {Capability.MEMORY_READ}
    assert policy.allowed_tools == {"query_memory"}
    assert policy.action_mode is ActionMode.FAST
    assert "set_reminder" not in policy.allowed_tools
    assert "one natural clarification" in policy.transient_instruction()


def test_calendar_compound_request_is_multi_label_and_clarifies_first():
    policy = _policy("Check if I'm free, schedule lunch, and remind me an hour before.")
    assert policy.capabilities == {
        Capability.CALENDAR_READ,
        Capability.CALENDAR_WRITE,
        Capability.REMINDER_WRITE,
    }
    assert policy.action_mode is ActionMode.COMPLEX
    assert policy.allowed_tools == {"get_upcoming_events"}
    assert set(policy.missing_slots) == {
        "calendar_date",
        "calendar_time",
        "reminder_exact_time",
    }
    assert policy.plan is not None
    assert [step.tool for step in policy.plan.steps] == [
        "get_upcoming_events",
        "create_calendar_event",
        "set_reminder",
    ]
    assert next_complex_tool(policy, lk_llm.ChatContext()) is None


def test_desktop_draft_and_reminder_keeps_only_safe_first_step():
    policy = _policy(
        "Draft a response to what's on my screen and remind me to send it tonight.",
        surface="desktop",
        frame=True,
    )
    assert policy.capabilities == {
        Capability.OUTBOUND_DRAFT,
        Capability.REMINDER_WRITE,
    }
    assert policy.action_mode is ActionMode.COMPLEX
    assert policy.allowed_tools == {"draft_outbound_message"}
    assert next_complex_tool(policy, lk_llm.ChatContext()) == "draft_outbound_message"


def test_screen_draft_surface_and_freshness_rules():
    request = "Draft a response to what's on my screen"
    assert "draft_outbound_message" not in _policy(request, surface="keyboard").allowed_tools
    assert "draft_outbound_message" not in _policy(request, surface="app").allowed_tools
    assert (
        "draft_outbound_message"
        not in _policy(request, surface="desktop", frame=False).allowed_tools
    )
    assert "draft_outbound_message" in _policy(request, surface="desktop", frame=True).allowed_tools


def test_command_artifact_needs_no_screen_frame_and_does_not_expose_message_draft():
    policy = _policy(
        "Give me the PowerShell command to make MobileApps the default folder",
        surface="desktop",
        frame=False,
    )
    assert policy.capabilities == {Capability.VISIBLE_ARTIFACT}
    assert "present_visible_artifact" in policy.allowed_tools
    assert "draft_outbound_message" not in policy.allowed_tools
    assert policy.write_authorization is WriteAuthorization.NONE


def test_visible_artifact_execution_needs_content_but_not_write_authorization():
    policy = _policy(
        "Give me the PowerShell command to fix this",
        surface="desktop",
        frame=False,
    )
    allowed = evaluate_execution(
        "present_visible_artifact",
        '{"kind":"command","title":"Fix","content":"Get-Process"}',
        policy,
        lk_llm.ChatContext(),
    )
    missing = evaluate_execution(
        "present_visible_artifact",
        '{"kind":"command","title":"Fix","content":""}',
        policy,
        lk_llm.ChatContext(),
    )
    assert allowed.allowed is True
    assert allowed.reason_code == "execution_allowed"
    assert missing.allowed is False
    assert missing.reason_code == "missing_required_tool_field"


def test_visible_output_repair_forces_the_registered_skill():
    policy = _policy(
        "No, stop reading it out loud. I asked you to show the command on my screen.",
        surface="desktop",
        frame=False,
    )
    instruction = policy.transient_instruction()
    assert "visible_output_repair" in policy.reason_codes
    assert "MUST use present_visible_artifact" in instruction
    assert "must not speak the requested content" in instruction


def test_failed_visible_output_then_short_retry_forces_visible_artifact():
    policy = derive_turn_policy(
        "Again, please.",
        lk_llm.ChatContext(),
        VoiceSurface.DESKTOP,
        False,
        previous_visible_output_failed=True,
    )
    assert Capability.VISIBLE_ARTIFACT in policy.capabilities
    assert "visible_output_repeat" in policy.reason_codes
    assert "MUST use present_visible_artifact" in policy.transient_instruction()


def test_short_retry_does_not_invent_visible_intent_without_a_previous_failure():
    policy = _policy("Again, please.", surface="desktop", frame=False)
    assert Capability.VISIBLE_ARTIFACT not in policy.capabilities
    assert "visible_output_repeat" not in policy.reason_codes
    assert "MUST use present_visible_artifact" not in policy.transient_instruction()


def test_general_speaking_frustration_does_not_force_an_artifact():
    policy = _policy("Stop speaking so loudly", surface="desktop", frame=False)
    assert Capability.VISIBLE_ARTIFACT not in policy.capabilities
    assert "visible_output_repair" not in policy.reason_codes


def test_artifact_specific_speaking_frustration_forces_an_artifact():
    policy = _policy("Don't read the command out loud", surface="desktop", frame=False)
    assert Capability.VISIBLE_ARTIFACT in policy.capabilities
    assert "visible_output_repair" in policy.reason_codes


def test_prompt_and_multistep_guidance_use_visible_artifact():
    for request in (
        "Generate a prompt for the agent in my codebase",
        "What are the next steps to fix this?",
        "Walk me through this setup",
    ):
        policy = _policy(request, surface="desktop", frame=False)
        assert Capability.VISIBLE_ARTIFACT in policy.capabilities
        assert "present_visible_artifact" in policy.allowed_tools


def test_conversational_desktop_turn_can_choose_visible_output_without_forcing_it():
    policy = _policy(
        "Explain why PowerShell execution policy blocks scripts",
        surface="desktop",
        frame=False,
    )
    assert Capability.VISIBLE_ARTIFACT not in policy.capabilities
    assert "present_visible_artifact" in policy.allowed_tools
    assert "MUST use present_visible_artifact" not in policy.transient_instruction()


def test_stale_desktop_turn_exposes_no_presentation_tool():
    policy = _policy(
        "Give me the command to fix this",
        surface="desktop",
        frame=False,
        finalized=False,
    )
    assert policy.allowed_tools == set()


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
    from src.agent.voice.tool_skills import VOICE_TOOL_SKILLS

    registered_skills = {
        item.skill_name for item in VOICE_TOOL_REGISTRY.values() if item.skill_name
    }
    assert registered_skills <= set(VOICE_TOOL_SKILLS)
    assert set(VOICE_TOOL_SKILLS) == registered_skills


def _result_context(*, failed: bool) -> lk_llm.ChatContext:
    context = lk_llm.ChatContext()
    context.add_message(
        role="user",
        content=["Check if I'm free tomorrow at noon, schedule lunch, and remind me at 11 am."],
    )
    context.items.append(
        lk_llm.FunctionCall(
            call_id="availability-call",
            name="get_upcoming_events",
            arguments='{"range_name":"tomorrow"}',
        )
    )
    context.items.append(
        lk_llm.FunctionCallOutput(
            call_id="availability-call",
            name="get_upcoming_events",
            output=(
                '{"error":true,"user_message":"Your calendar is taking too long '
                'to respond. Try again in a moment."}'
                if failed
                else '{"events":[]}'
            ),
            is_error=False,
        )
    )
    return context


def test_dependent_writes_run_one_step_at_a_time():
    text = "Check if I'm free tomorrow at noon, schedule lunch, and remind me at 11 am."
    policy = derive_turn_policy(text, _result_context(failed=False), VoiceSurface.APP, False)
    assert next_complex_tool(policy, _result_context(failed=False)) == ("create_calendar_event")
    allowed = evaluate_execution(
        "create_calendar_event",
        '{"title":"Lunch","start_time":"2026-07-13T12:00:00-07:00"}',
        policy,
        _result_context(failed=False),
    )
    deferred = evaluate_execution(
        "set_reminder",
        '{"message":"Lunch","scheduled_at":"2026-07-13T11:00:00-07:00"}',
        policy,
        _result_context(failed=False),
    )
    assert allowed.allowed is True
    assert deferred.allowed is False
    assert deferred.reason_code == "dependent_action_deferred"


def test_failed_prerequisite_halts_write_and_preserves_failure_message():
    context = _result_context(failed=True)
    text = "Check if I'm free tomorrow at noon, schedule lunch, and remind me at 11 am."
    policy = derive_turn_policy(text, context, VoiceSurface.APP, False)
    assert next_complex_tool(policy, context) is None
    decision = evaluate_execution(
        "create_calendar_event",
        '{"title":"Lunch","start_time":"2026-07-13T12:00:00-07:00"}',
        policy,
        context,
    )
    assert decision.allowed is False
    output = context.items[-1]
    assert isinstance(output, lk_llm.FunctionCallOutput)
    assert output.output == (
        '{"error":true,"user_message":"Your calendar is taking too long '
        'to respond. Try again in a moment."}'
    )


def test_stale_preemptive_turn_never_authorizes_write():
    policy = _policy("Remind me tomorrow at 9 am", finalized=False)
    assert policy.write_authorization is WriteAuthorization.STALE_TURN
    assert policy.allowed_tools == set()


def test_hypothetical_calendar_question_does_not_authorize_write():
    policy = _policy("Should I schedule lunch tomorrow at noon?")
    assert Capability.CALENDAR_WRITE in policy.capabilities
    assert policy.write_authorization is WriteAuthorization.NEEDS_CLARIFICATION
    assert "create_calendar_event" not in policy.allowed_tools


def test_clarification_turn_preserves_prior_authorization_and_missing_slots():
    unresolved = UnresolvedActionState(
        source_message_id="turn-1",
        source_turn_index=1,
        capabilities=frozenset({Capability.REMINDER_WRITE}),
        missing_slots=("reminder_exact_time",),
        created_at_turn=1,
        write_authorized=True,
    )
    policy = derive_turn_policy(
        "7 pm",
        lk_llm.ChatContext(),
        VoiceSurface.APP,
        False,
        unresolved,
        source_message_id="turn-2",
        turn_index=2,
    )
    assert policy.write_authorization is WriteAuthorization.AUTHORIZED
    assert policy.missing_slots == ()
    assert policy.allowed_tools == {"set_reminder"}


def test_stale_reminder_state_cannot_latch_for_twenty_turns():
    unresolved = UnresolvedActionState(
        source_message_id="turn-1",
        source_turn_index=1,
        capabilities=frozenset({Capability.REMINDER_WRITE}),
        missing_slots=("reminder_exact_time",),
        created_at_turn=1,
        write_authorized=True,
    )
    policy = derive_turn_policy(
        "Generate a prompt draft about the issue on screen",
        lk_llm.ChatContext(),
        VoiceSurface.DESKTOP,
        False,
        unresolved,
        source_message_id="turn-20",
        turn_index=20,
    )
    assert policy.capabilities == {Capability.VISIBLE_ARTIFACT}
    assert policy.missing_slots == ()
    assert policy.clarification_question is None
    assert "set_reminder" not in policy.allowed_tools
    assert "unresolved_action_cleared" in policy.reason_codes


def test_correction_clears_immediately_preceding_reminder():
    unresolved = UnresolvedActionState(
        source_message_id="turn-1",
        source_turn_index=1,
        capabilities=frozenset({Capability.REMINDER_WRITE}),
        missing_slots=("reminder_exact_time",),
        created_at_turn=1,
        write_authorized=True,
    )
    policy = derive_turn_policy(
        "Actually, generate a prompt instead",
        lk_llm.ChatContext(),
        VoiceSurface.DESKTOP,
        False,
        unresolved,
        source_message_id="turn-2",
        turn_index=2,
    )
    assert policy.capabilities == {Capability.VISIBLE_ARTIFACT}
    assert policy.missing_slots == ()
    assert policy.allowed_tools == {"present_visible_artifact"}


def test_summary_text_cannot_create_reminder_capability():
    context = lk_llm.ChatContext()
    context.add_message(
        role="system",
        content=[
            '<voice_session_summary>{"current_topic":"old reminder"}'
            "</voice_session_summary>"
        ],
    )
    policy = derive_turn_policy(
        "Generate a prompt about the issue on screen",
        context,
        VoiceSurface.DESKTOP,
        False,
        source_message_id="turn-30",
        turn_index=30,
    )
    assert policy.capabilities == {Capability.VISIBLE_ARTIFACT}
    assert not ({"set_reminder", "list_reminders", "cancel_reminder"} & policy.allowed_tools)


def test_thirty_turn_coding_flow_never_exposes_reminder_guidance():
    unresolved = UnresolvedActionState()
    for turn_index in range(1, 31):
        policy = derive_turn_policy(
            f"Generate prompt {turn_index} for my coding agent",
            lk_llm.ChatContext(),
            VoiceSurface.DESKTOP,
            False,
            unresolved,
            source_message_id=f"turn-{turn_index}",
            turn_index=turn_index,
        )
        instruction = policy.transient_instruction().lower()
        assert policy.capabilities == {Capability.VISIBLE_ARTIFACT}
        assert not ({"set_reminder", "list_reminders", "cancel_reminder"} & policy.allowed_tools)
        assert "reminder clarification" not in instruction
        assert policy.clarification_question is None
        unresolved = UnresolvedActionState()


def _agent_context_vars() -> dict[str, str]:
    return {
        "name": "V",
        "timezone": "America/Los_Angeles",
        "local_time": "8:00 PM",
        "local_date": "July 12, 2026",
        "memory_summary": "",
        "last_session_context": "",
        "last_session_at": "",
        "archive_context": "",
        "user_aura_profile": "",
        "surface": "",
        "screen_sight": "",
    }


def _fake_tools(*extra_names: str) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(info=SimpleNamespace(name=name))
        for name in ("set_reminder", "query_memory", "web_surf", *extra_names)
    ]


async def _collect_llm(agent: BuddyAgent, context, tools):
    return [item async for item in agent.llm_node(context, tools, None)]


async def test_always_on_orchestration_records_turn_policy_and_tool_telemetry(monkeypatch):
    context = lk_llm.ChatContext()
    message = context.add_message(role="user", content=["Remind me what Sarah said"])
    tools = _fake_tools()
    telemetry = SimpleNamespace(
        turn_index=1,
        start_turn=Mock(),
        policy=Mock(),
        first_response=Mock(),
        emitted=Mock(),
        deferred=Mock(),
        execution=Mock(),
    )

    async def _default(agent, passed_context, passed_tools, settings):
        yield lk_llm.ChatChunk(
            id="tool-calls",
            delta=lk_llm.ChoiceDelta(
                tool_calls=[
                    lk_llm.FunctionToolCall(
                        name="query_memory",
                        arguments='{"query":"Sarah"}',
                        call_id="allowed-memory-read",
                    ),
                    lk_llm.FunctionToolCall(
                        name="set_reminder",
                        arguments=(
                            '{"message":"Sarah","scheduled_at":"2026-07-13T18:00:00-07:00"}'
                        ),
                        call_id="blocked-reminder-write",
                    ),
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

    await agent.on_user_turn_completed(lk_llm.ChatContext(), message)
    output = await _collect_llm(agent, context, tools)
    agent.record_voice_tool_execution("query_memory", success=True)

    assert [call.name for call in output[0].delta.tool_calls] == ["query_memory"]
    telemetry.start_turn.assert_called_once_with()
    telemetry.policy.assert_called_once()
    telemetry.first_response.assert_called()
    telemetry.emitted.assert_called_once_with("query_memory", "execution_allowed")
    telemetry.deferred.assert_called_once()
    telemetry.execution.assert_called_once_with("query_memory", success=True)


async def test_orchestration_filters_only_llm_visible_list_and_keeps_context(monkeypatch):
    context = lk_llm.ChatContext()
    message = context.add_message(role="user", content=["Remind me what Sarah said"])
    tools = _fake_tools()
    captured = {}

    async def _default(agent, passed_context, passed_tools, settings):
        captured.update(context=passed_context, tools=passed_tools)
        yield "checking"

    monkeypatch.setattr(Agent.default, "llm_node", _default)
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
    )
    agent._finalized_message_id = message.id
    agent._finalized_transcript = message.text_content
    original_items = list(context.items)
    await _collect_llm(agent, context, tools)
    assert [tool.info.name for tool in captured["tools"]] == ["query_memory"]
    assert captured["context"] is not context
    assert context.items == original_items
    assert [tool.info.name for tool in tools] == [
        "set_reminder",
        "query_memory",
        "web_surf",
    ]


async def test_orchestration_injects_only_the_visible_artifact_skill(monkeypatch):
    context = lk_llm.ChatContext()
    message = context.add_message(
        role="user", content=["Give me the PowerShell command to fix this"]
    )
    tools = _fake_tools("present_visible_artifact", "draft_outbound_message")
    captured = {}

    async def _default(agent, passed_context, passed_tools, settings):
        captured.update(context=passed_context, tools=passed_tools)
        yield "done"

    monkeypatch.setattr(Agent.default, "llm_node", _default)
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
        launch_surface="desktop",
    )
    agent._finalized_message_id = message.id
    agent._finalized_transcript = message.text_content

    await _collect_llm(agent, context, tools)

    assert [tool.info.name for tool in captured["tools"]] == ["present_visible_artifact"]
    assert captured["context"] is not context
    transient = captured["context"].items[-1]
    assert "<tool_skills>" in transient.text_content
    assert "Use present_visible_artifact" in transient.text_content
    assert "outbound_draft" not in transient.text_content


async def test_orchestration_drops_same_generation_dependent_writes():
    text = "Check if I'm free tomorrow at noon, schedule lunch, and remind me at 11 am."
    context = lk_llm.ChatContext()
    message = context.add_message(role="user", content=[text])
    policy = derive_turn_policy(text, context, VoiceSurface.APP, False)
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
    )
    agent._finalized_message_id = message.id
    agent._finalized_transcript = text
    original_items = list(context.items)

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
                    arguments=('{"title":"Lunch","start_time":"2026-07-13T12:00:00-07:00"}'),
                    call_id="calendar-write",
                ),
                lk_llm.FunctionToolCall(
                    name="set_reminder",
                    arguments=('{"message":"Lunch","scheduled_at":"2026-07-13T11:00:00-07:00"}'),
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
            _chunks(), policy=policy, chat_ctx=context
        )
    ]
    kept = output[0].delta.tool_calls
    assert [call.name for call in kept] == ["get_upcoming_events"]
    assert context.items == original_items


async def test_final_turn_marker_invalidates_preemptive_generation():
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
    )
    turn_context = lk_llm.ChatContext()
    message = lk_llm.ChatMessage(role="user", content=["Remind me tomorrow at 9 am"])

    await agent.on_user_turn_completed(turn_context, message)

    assert agent._finalized_message_id == message.id
    assert len(turn_context.items) == 1
    marker = turn_context.items[0]
    assert isinstance(marker, lk_llm.ChatMessage)
    assert marker.role == "system"
    assert "finalized transcript" in marker.text_content


async def test_agent_remembers_failed_visible_output_for_the_next_turn():
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
        launch_surface="desktop",
    )

    first_context = lk_llm.ChatContext()
    first_message = lk_llm.ChatMessage(role="user", content=["Show me the PowerShell command"])
    await agent.on_user_turn_completed(first_context, first_message)
    assert agent._current_turn_visible_request is True
    assert agent._current_turn_visible_success is False

    second_context = lk_llm.ChatContext()
    second_message = lk_llm.ChatMessage(role="user", content=["Again, please"])
    await agent.on_user_turn_completed(second_context, second_message)
    assert agent._visible_output_failed_previous_turn is True

    policy = derive_turn_policy(
        agent._finalized_transcript,
        second_context,
        VoiceSurface.DESKTOP,
        False,
        previous_visible_output_failed=agent._visible_output_failed_previous_turn,
    )
    assert "visible_output_repeat" in policy.reason_codes


async def test_successful_visible_output_clears_repeat_repair_state():
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
        launch_surface="desktop",
    )

    first_context = lk_llm.ChatContext()
    first_message = lk_llm.ChatMessage(role="user", content=["Show me the PowerShell command"])
    await agent.on_user_turn_completed(first_context, first_message)
    agent.record_voice_tool_execution("present_visible_artifact", success=True)

    second_context = lk_llm.ChatContext()
    second_message = lk_llm.ChatMessage(role="user", content=["Again, please"])
    await agent.on_user_turn_completed(second_context, second_message)

    assert agent._visible_output_failed_previous_turn is False


async def test_fake_llm_reminder_question_is_blocked_and_regenerated_once():
    async def _primary():
        yield "What exact time should the reminder fire?"

    async def _retry():
        yield "I put the prompt on your screen."

    output = [
        item
        async for item in guard_spoken_action_stream(
            _primary(),
            capabilities=frozenset({Capability.VISIBLE_ARTIFACT}),
            regenerate=_retry,
            neutral_recovery="Let's stick with the prompt.",
        )
    ]
    assert output == ["I put the prompt on your screen."]


async def test_spoken_guard_fails_closed_when_retry_repeats_reminder_language():
    async def _unsafe():
        yield "What time should I set the reminder?"

    output = [
        item
        async for item in guard_spoken_action_stream(
            _unsafe(),
            capabilities=frozenset({Capability.VISIBLE_ARTIFACT}),
            regenerate=_unsafe,
            neutral_recovery="Let's stick with the prompt.",
        )
    ]
    assert output == ["Let's stick with the prompt."]


def test_genuine_reminder_clarification_is_not_blocked_by_policy_guard():
    policy = _policy("Remind me tomorrow to call Sarah")
    assert policy.capabilities == {Capability.REMINDER_WRITE}
    assert policy.clarification_question == "what exact time the reminder should fire"


async def test_any_tool_completion_clears_unresolved_action_state():
    agent = BuddyAgent(
        user_id="u",
        context_vars=_agent_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="s",
    )
    agent._unresolved_action = UnresolvedActionState(
        source_message_id="turn-1",
        source_turn_index=1,
        capabilities=frozenset({Capability.REMINDER_WRITE}),
        missing_slots=("reminder_exact_time",),
        created_at_turn=1,
        write_authorized=True,
    )
    agent.record_voice_tool_execution("query_memory", success=False)
    assert agent._unresolved_action == UnresolvedActionState()
