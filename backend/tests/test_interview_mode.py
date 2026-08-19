"""Interview Mode: the handoff boundary, in both directions.

What these cover is the boundary itself, not interview quality: that Buddy's tool
is reachable on a natural request and absent on an unrelated one, that the
supervisor and the intake task are genuinely isolated rather than re-dressed
Buddies, that state and conversation survive the handoff, and that the user can
talk their way back.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from livekit.agents import llm as lk_llm

from src.agent.buddy_agent import BuddyAgent
from src.agent.voice import pipelines
from src.agent.voice.capabilities import VOICE_TOOL_REGISTRY, VoiceSurface, tool_name
from src.agent.voice.interview import (
    INTERVIEW_SUPERVISOR_INSTRUCTIONS,
    InterviewDossier,
    InterviewIntakeTask,
    InterviewPhase,
    InterviewState,
    InterviewSupervisorAgent,
    VoiceSessionState,
)
from src.agent.voice.tool_discovery import (
    ActiveIntentState,
    EligibilityContext,
    IntentAuthorizationState,
    SelectionContext,
    ToolCatalog,
)


def _buddy(chat_ctx: lk_llm.ChatContext | None = None) -> BuddyAgent:
    return BuddyAgent(
        user_id="interview-user",
        context_vars={
            "name": "Varun",
            "local_time": "10:00 AM",
            "local_date": "August 18, 2026",
            "timezone": "America/Los_Angeles",
            "archive_context": "",
            "user_aura_profile": "",
            "last_session_context": "",
            "memory_summary": "",
            "graph_context": "",
        },
        chat_ctx=chat_ctx or lk_llm.ChatContext(),
        launch_surface="desktop",
        session_id="interview-session",
    )


def _selected_tools(agent: BuddyAgent, request: str) -> tuple[str, ...]:
    """The tool names BuddyAgent.llm_node would expose to the model this turn.

    This is the honest deterministic proxy for "the LLM can pick it": whether the
    model in fact picks it is a live-model question no offline test can answer,
    but a tool the selector never exposes is one it can never pick.
    """
    catalog = ToolCatalog.from_livekit_tools(agent.tools)
    selection = catalog.select(
        SelectionContext(
            finalized_request=request,
            active_objective="",
            screen_referent="",
            prior_clarification="",
            turn_index=1,
        ),
        EligibilityContext(
            surface=VoiceSurface.DESKTOP,
            authenticated=True,
            connector_states={},
            fresh_frame_available=False,
            enabled_feature_rollouts=frozenset(),
            authorization_state=IntentAuthorizationState.NONE,
        ),
        frozenset(VOICE_TOOL_REGISTRY),
        ActiveIntentState(),
    )
    return selection.tool_names


def _texts(chat_ctx: lk_llm.ChatContext) -> list[str]:
    return [
        part
        for item in chat_ctx.items
        if isinstance(item, lk_llm.ChatMessage)
        for part in item.content
        if isinstance(part, str)
    ]


def _session_state(buddy: BuddyAgent) -> VoiceSessionState:
    """The session userdata as voice_agent.py wires it, with a resume factory."""

    state = VoiceSessionState()

    async def _resume_buddy(chat_ctx: lk_llm.ChatContext) -> BuddyAgent:
        await buddy.prepare_interview_resume(
            chat_ctx, state.interview.ownership_epoch
        )
        return buddy

    state.buddy_factory = _resume_buddy
    return state


async def _start_interview(
    buddy: BuddyAgent, state: VoiceSessionState
) -> InterviewSupervisorAgent:
    """Call the tool, then do what LiveKit does: activate the returned agent.

    The tool only RESERVES the interview; the phase moves in the supervisor's
    on_enter, once activation has happened. on_enter itself also awaits the intake
    AgentTask, which needs a live session, so this drives the commit that hook
    performs first and leaves the rest to the live path.
    """
    supervisor, _ = await buddy.start_mock_interview(SimpleNamespace(userdata=state))
    claim = state.interview.pending_start
    assert claim is not None
    assert state.interview.commit_entry(claim)
    return supervisor


def test_natural_interview_request_exposes_start_tool() -> None:
    selected = _selected_tools(_buddy(), "Interview me for a senior backend role.")

    assert "start_mock_interview" in selected


def test_start_tool_exposure_is_structural_not_wording() -> None:
    """Exposure is decided by surface, never by what the user happened to say.

    The handoff is how the user LEAVES the current mode, so the semantic selector
    is not allowed to score it away on a turn whose wording it did not recognise:
    it sits on the selection floor and is present on every eligible desktop turn.
    What removes it is structural, and this is that fact from both sides.
    """
    assert "start_mock_interview" in _selected_tools(
        _buddy(), "Interview me for a senior backend role."
    )
    assert "start_mock_interview" in _selected_tools(
        _buddy(), "What is the capital of France?"
    )
    assert VoiceSurface.APP not in (
        VOICE_TOOL_REGISTRY["start_mock_interview"].allowed_surfaces
    )


def test_agent_session_owns_handoff_state(monkeypatch: pytest.MonkeyPatch) -> None:
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

    userdata = captured["userdata"]
    assert isinstance(userdata, VoiceSessionState)
    # Feature state is nested, so the next feature to need session state does not
    # force every RunContext annotation to change.
    assert isinstance(userdata.interview, InterviewState)
    assert userdata.interview.phase is InterviewPhase.IDLE
    assert userdata.interview.active is False


@pytest.mark.asyncio
async def test_start_handoff_preserves_context_and_state() -> None:
    chat_ctx = lk_llm.ChatContext()
    chat_ctx.add_message(role="user", content=["Remember that I prefer concise answers."])
    buddy = _buddy(chat_ctx)
    state = _session_state(buddy)

    supervisor = await _start_interview(buddy, state)

    assert isinstance(supervisor, InterviewSupervisorAgent)
    assert state.interview.phase is InterviewPhase.INTAKE
    assert state.interview.active is True
    # Minted per interview, so a paste answering an earlier one cannot be
    # accepted against this one.
    assert state.interview.interview_id
    assert "Remember that I prefer concise answers." in _texts(supervisor.chat_ctx)
    # A separate agent with its own instructions, not a Buddy prompt mutation.
    assert supervisor.instructions == INTERVIEW_SUPERVISOR_INSTRUCTIONS
    assert supervisor.instructions != buddy.instructions


@pytest.mark.asyncio
async def test_supervisor_and_intake_are_tool_isolated() -> None:
    buddy = _buddy()
    state = _session_state(buddy)

    supervisor = await _start_interview(buddy, state)
    intake = InterviewIntakeTask(state=state, chat_ctx=lk_llm.ChatContext())

    assert {tool_name(tool) for tool in supervisor.tools} == {"end_mock_interview"}
    assert {tool_name(tool) for tool in intake.tools} == {
        "record_company",
        "record_role_and_experience",
        "record_interview_focus",
        "request_job_description",
        "finish_intake",
        "cancel_setup",
    }
    # None of Buddy's own local tools came along to either one.
    buddy_tools = {tool_name(tool) for tool in buddy.tools}
    assert not {tool_name(tool) for tool in supervisor.tools} & buddy_tools
    assert not {tool_name(tool) for tool in intake.tools} & buddy_tools
    # An explicit None, not NOT_GIVEN: this is what stops either of them
    # inheriting the session's whole MCP tool surface at activity start.
    assert supervisor.mcp_servers is None
    assert intake.mcp_servers is None


@pytest.mark.asyncio
async def test_intake_writes_through_to_session_state() -> None:
    buddy = _buddy()
    state = _session_state(buddy)
    await _start_interview(buddy, state)
    intake = InterviewIntakeTask(state=state, chat_ctx=lk_llm.ChatContext())

    await intake.record_company("  Stripe ")
    await intake.record_role_and_experience(
        "  Senior   backend engineer  ", "about six years, mostly Go"
    )
    await intake.record_interview_focus("technical")

    # Committed on every tool, not only at completion, so a cancelled or
    # timed-out intake still leaves behind what the user actually answered.
    dossier = state.interview.dossier
    assert dossier.company == "Stripe"
    assert dossier.target_role == "Senior backend engineer"
    assert dossier.experience == "about six years, mostly Go"
    assert dossier.source == "conversation"
    assert dossier.is_complete


@pytest.mark.asyncio
async def test_empty_required_answer_is_a_recoverable_tool_error() -> None:
    buddy = _buddy()
    state = _session_state(buddy)
    await _start_interview(buddy, state)
    intake = InterviewIntakeTask(state=state, chat_ctx=lk_llm.ChatContext())

    with pytest.raises(lk_llm.ToolError):
        await intake.record_company("   ")

    assert state.interview.dossier.company == ""


def test_dossier_requires_only_what_its_branch_needs() -> None:
    # A JD carries the role and the requirements, so asking for them separately
    # would be asking the user to retype what they just pasted.
    assert InterviewDossier(company="Stripe", source="jd").missing_fields() == (
        "interview_focus",
        "job_description",
    )
    assert InterviewDossier(
        company="Stripe",
        source="jd",
        interview_focus="technical",
        job_description="Senior backend...",
    ).is_complete
    assert InterviewDossier(
        company="Stripe",
        target_role="backend eng",
        experience="three years in APIs",
        interview_focus="technical",
    ).is_complete
    assert InterviewDossier().missing_fields() == (
        "company",
        "interview_focus",
        "target_role",
        "experience",
    )


@pytest.mark.asyncio
async def test_cancel_returns_to_buddy_with_interview_context() -> None:
    buddy = _buddy()
    state = _session_state(buddy)
    supervisor = await _start_interview(buddy, state)

    supervisor_ctx = supervisor.chat_ctx.copy()
    supervisor_ctx.add_message(role="user", content=["Actually, cancel that for now."])
    await supervisor.update_chat_ctx(supervisor_ctx)

    returned_agent, _ = await supervisor.end_mock_interview(
        SimpleNamespace(userdata=state)
    )

    # The SAME Buddy instance: every coordinator wired in voice_agent.py still
    # points at it, so returning a fresh one would silently orphan all of them.
    assert returned_agent is buddy
    # RETURN_PENDING, not idle. The interview is over only once BuddyAgent.on_enter
    # runs, so a return that gets interrupted here leaves the supervisor active and
    # the handback retryable rather than leaving the session between two owners.
    assert state.interview.phase is InterviewPhase.RETURN_PENDING
    assert state.interview.active is True
    assert "Actually, cancel that for now." in _texts(buddy.chat_ctx)
    assert buddy._resume_from_interview is True
    # Retryable: asking again from RETURN_PENDING works instead of being refused.
    retried_agent, _ = await supervisor.end_mock_interview(
        SimpleNamespace(userdata=state)
    )
    assert retried_agent is buddy
    assert state.interview.phase is InterviewPhase.RETURN_PENDING


@pytest.mark.asyncio
async def test_handoff_refused_when_resume_factory_is_unwired() -> None:
    """Never strand the user in an agent that has no way back to Buddy."""
    buddy = _buddy()

    with pytest.raises(lk_llm.ToolError):
        await buddy.start_mock_interview(SimpleNamespace(userdata=VoiceSessionState()))
