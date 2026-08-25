"""The agent that owns Interview Mode once Buddy hands it the conversation.

This is a handoff target, not a persona swap: conversational identity and
responsibility genuinely change here, which is the line LiveKit draws for when a
handoff is the right tool rather than a task.

Two things make the isolation real rather than cosmetic:

- ``mcp_servers=None``. It is NOT decoration. ``AgentActivity`` resolves MCP
  servers as "the agent's own if given, otherwise the session's"
  (``livekit/agents/voice/agent_activity.py``), and the session carries the whole
  production MCP tool surface. Without an explicit None the supervisor would
  silently inherit reminders, calendar, memory and web.
- A separate ``Agent`` subclass, not a prompt swap on Buddy. It gets none of
  Buddy's persona, none of its local tools, and none of its ``llm_node``.

That last point cuts both ways: the supervisor also has no action policy, no
speculation, no artifact handling and no Action Truth envelopes, because all of
that lives in ``BuddyAgent.llm_node``.
"""

from __future__ import annotations

import asyncio

from livekit.agents import Agent, RunContext, function_tool
from livekit.agents import llm as lk_llm

from ....lib.logger import logger
from ....services.interview_preparation import prepare_mock_interview_brief
from .contracts import InterviewIntakeResult
from .intake_task import InterviewIntakeTask, retry_instructions
from .interviewer import InterviewerAgent
from .models import InterviewDossier, InterviewPhase, VoiceSessionState
from .question_plan import QuestionPlanService

INTERVIEW_SUPERVISOR_INSTRUCTIONS = """
You are running the setup for a mock interview. You are not Buddy, and you have
no general-assistant capabilities.

Setup is collected for you. When it comes back, tell the user in one or two
sentences what you have: the company, and either the role and background they
described or that you have their job description. Never read the job description
back to them. Then say you are lining up some questions, and stop talking.

Do not ask interview questions yourself, score anything, or give feedback. The
interview itself is run by someone else, right after you.

If the user wants to stop, cancel, leave Interview Mode, or go back to talking
with Buddy, call end_mock_interview. For anything unrelated to the interview, end
Interview Mode and let Buddy handle it.
""".strip()


def _report_instructions(dossier: InterviewDossier) -> str:
    """What to say once setup is captured, built from what was actually collected."""
    have: list[str] = []
    if dossier.company:
        have.append(f"company: {dossier.company}")
    if dossier.target_role:
        have.append(f"role: {dossier.target_role}")
    if dossier.interview_focus:
        have.append(f"focus: {dossier.interview_focus}")
    if dossier.experience:
        have.append(f"their background: {dossier.experience}")
    if dossier.job_description:
        have.append("their job description, received and stored")

    missing = dossier.missing_fields()
    lines = [
        "Setup is captured. Confirm it back in one or two short sentences, "
        "naturally, without listing fields or reading the job description aloud.",
        f"What you have: {'; '.join(have) if have else 'nothing yet'}.",
    ]
    if missing:
        lines.append(
            f"Still missing: {', '.join(missing)}. Mention it once, lightly, and "
            "do not ask for it again."
        )
    # This line is what covers the planning call. It is spoken while the plan is
    # already being generated, so the wait costs the user nothing they can hear.
    lines.append(
        "Then say you are putting a few questions together and they should get "
        "comfortable. Do not ask a question yourself."
    )
    return " ".join(lines)


class InterviewSupervisorAgent(Agent):
    """Owns Interview Mode: runs setup, reports it, and hands back on request."""

    def __init__(
        self,
        *,
        state: VoiceSessionState,
        chat_ctx: lk_llm.ChatContext,
        planner: QuestionPlanService | None = None,
    ) -> None:
        super().__init__(
            instructions=INTERVIEW_SUPERVISOR_INSTRUCTIONS,
            chat_ctx=chat_ctx,
            # See the module docstring: an explicit None is what actually keeps
            # the session's MCP tool surface out of this agent. NOT_GIVEN would
            # inherit all of it.
            mcp_servers=None,
        )
        # The same object the session holds, not a copy, so every agent in the
        # session observes one state.
        self._state = state
        self._planner = planner or QuestionPlanService()

    async def on_enter(self) -> None:
        """Commit entry, run intake, then report.

        Entry is committed HERE and nowhere else. This hook runs only once
        LiveKit has actually activated this agent, which is the first moment the
        handoff is real; the tool that returned this agent merely reserved it. A
        claim that no longer matches means the session moved on while this
        activation was in flight, so the only safe move is straight back out.

        Awaiting an AgentTask here is required, not a convenience:
        ``AgentTask.__await_impl`` raises unless it is awaited inside a tool
        function or an on_enter/on_exit hook.
        """
        interview = self._state.interview
        claim = interview.pending_start
        if claim is None or not interview.commit_entry(claim):
            logger.warn(
                "InterviewSupervisor: entry not committed, returning to Buddy",
                {
                    "interview_id": interview.interview_id,
                    "phase": str(interview.phase),
                    "ownership_epoch": interview.ownership_epoch,
                    "had_claim": claim is not None,
                },
            )
            await self._hand_back_to_buddy("entry_not_committed")
            return

        try:
            result = await self._collect_setup()
        except Exception as exc:
            logger.warn(
                "InterviewSupervisor: intake failed",
                {
                    "interview_id": interview.interview_id,
                    "error_type": type(exc).__name__,
                },
            )
            interview.terminate("intake_failed")
            # Never strand the user in an agent that went quiet. Hand back rather
            # than sit in a mode whose setup never finished.
            await self._hand_back_to_buddy("intake_failed")
            return

        if result is None or not interview.is_current(
            result.interview_id, result.ownership_epoch
        ):
            # A result for an interview this session has already left. Committing
            # it would write a stale dossier over whatever replaced it, and
            # reporting it would narrate setup the user has moved on from.
            logger.warn(
                "InterviewSupervisor: intake result discarded, epoch moved",
                {
                    "interview_id": interview.interview_id,
                    "ownership_epoch": interview.ownership_epoch,
                    "result_epoch": (
                        result.ownership_epoch if result is not None else None
                    ),
                },
            )
            return

        dossier = result.dossier
        interview.dossier = dossier
        if result.cancelled:
            interview.note_cancelled()
            await self._hand_back_to_buddy("intake_cancelled")
            return

        dossier.brief = prepare_mock_interview_brief(
            company=dossier.company,
            role=dossier.target_role,
            experience=dossier.experience,
            job_description=dossier.job_description,
        )
        interview.dossier = dossier

        if not interview.note_setup_captured():
            return
        logger.info(
            "InterviewSupervisor: setup captured",
            {
                "interview_id": interview.interview_id,
                "source": dossier.source,
                "interview_focus": dossier.interview_focus,
                "missing": list(dossier.missing_fields()),
                "job_description_chars": len(dossier.job_description),
            },
        )
        await self._plan_and_hand_off(dossier)

    async def _plan_and_hand_off(self, dossier: InterviewDossier) -> None:
        """Plan the questions behind the confirmation line, then start the interview.

        The planning call is started BEFORE the confirmation is spoken and awaited
        after it, so the one round trip happens underneath speech the user was
        going to hear anyway. Sequencing it the other way would add its whole
        latency to a silence.

        A failed planner returns None after its bounded role-aware retry. That is
        an honest exit, not permission to ask a canned question unrelated to the
        candidate's role.
        """
        interview = self._state.interview
        if not interview.note_planning():
            return
        # Captured BEFORE the awaits below, which is the whole point: comparing
        # state to itself afterwards would always agree and prove nothing.
        planning_id = interview.interview_id
        planning_epoch = interview.ownership_epoch
        planning = asyncio.create_task(
            self._planner.plan(dossier),
            name=f"interview-plan-{interview.interview_id[:8]}",
        )
        try:
            await self.session.generate_reply(
                instructions=_report_instructions(dossier)
            )
        except Exception as exc:
            # The line failed to speak. The plan is still worth having, so this
            # falls through to the handoff rather than aborting the interview.
            logger.warn(
                "InterviewSupervisor: setup confirmation failed to speak",
                {
                    "interview_id": interview.interview_id,
                    "error_type": type(exc).__name__,
                },
            )
        try:
            plan = await planning
        except Exception as exc:
            logger.warn(
                "InterviewSupervisor: planning task failed",
                {
                    "interview_id": interview.interview_id,
                    "error_type": type(exc).__name__,
                },
            )
            plan = None

        # The plan is only worth installing if this is still the same interview:
        # planning is the longest gap in the whole flow, and a user who cancelled
        # during it must not be handed an interviewer afterwards.
        if not interview.is_current(planning_id, planning_epoch):
            logger.info(
                "InterviewSupervisor: plan discarded, epoch moved",
                {
                    "planning_id": planning_id,
                    "planning_epoch": planning_epoch,
                    "current_epoch": interview.ownership_epoch,
                },
            )
            return
        if interview.phase is not InterviewPhase.PLANNING:
            logger.info(
                "InterviewSupervisor: plan discarded, interview left planning",
                {
                    "interview_id": interview.interview_id,
                    "phase": str(interview.phase),
                },
            )
            return

        if plan is None:
            logger.warn(
                "InterviewSupervisor: tailored planning unavailable",
                {"interview_id": interview.interview_id},
            )
            try:
                await self.session.say(
                    "I couldn't prepare a tailored interview right now, so I won't "
                    "give you generic questions. Let's try again shortly."
                )
            except Exception as exc:
                logger.warn(
                    "InterviewSupervisor: planning failure failed to speak",
                    {"interview_id": interview.interview_id, "error_type": type(exc).__name__},
                )
            await self._hand_back_to_buddy("tailored_planning_unavailable")
            return

        interview.adopt_plan(plan)
        # A handoff, not a task: responsibility genuinely changes here. The
        # interviewer commits INTERVIEWING in its own on_enter, once LiveKit has
        # actually activated it.
        self.session.update_agent(
            InterviewerAgent(
                state=self._state,
                chat_ctx=self.chat_ctx.copy(exclude_instructions=True),
            )
        )

    async def on_exit(self) -> None:
        """Terminate cleanly if this agent is torn down without returning.

        The ordinary exit is a return to Buddy, which is already RETURN_PENDING by
        the time LiveKit swaps the agent, and terminate() refuses that move. This
        is the other case: the session ended, or something else replaced this
        agent, while the interview still believed it was running. Leaving a live
        phase behind would keep every ambient producer suspended for the rest of
        the call.
        """
        interview = self._state.interview
        if interview.phase in (
            InterviewPhase.INTAKE,
            InterviewPhase.SETUP_CAPTURED,
            InterviewPhase.CANCELLED,
        ):
            interview.terminate("supervisor_exited")
        # PLANNING is deliberately absent: the ordinary exit from this agent is
        # the handoff to the interviewer, which happens while the phase is still
        # PLANNING. Terminating here would kill the interview it just started.

    async def _collect_setup(self) -> InterviewIntakeResult | None:
        """Run intake, retrying once when it comes back incomplete."""
        task = InterviewIntakeTask(
            state=self._state, chat_ctx=self.chat_ctx.copy(exclude_instructions=True)
        )
        result = await task
        if result.cancelled or result.dossier.is_complete:
            return result
        if not self._state.interview.is_current(
            result.interview_id, result.ownership_epoch
        ):
            # Do not open a second intake for an interview that is already gone.
            return result

        # One retry, naming only what is absent. One recovers a dropped answer;
        # looping would trap a user who does not want to answer at all.
        retry = InterviewIntakeTask(
            state=self._state,
            chat_ctx=self.chat_ctx.copy(exclude_instructions=True),
            instructions=retry_instructions(result.dossier.missing_fields()),
        )
        return await retry

    async def _hand_back_to_buddy(self, reason: str) -> None:
        """Return control to Buddy from inside on_enter, without a tool call.

        Requests the return and hands the floor over; it does NOT declare the
        interview idle. Only ``BuddyAgent.on_enter`` does that, so an activation
        that never lands leaves this agent in RETURN_PENDING, still active and
        still able to try again.
        """
        buddy_factory = self._state.buddy_factory
        if buddy_factory is None:
            return
        self._state.interview.request_return(reason)
        buddy = await buddy_factory(self.chat_ctx.copy(exclude_instructions=True))
        self.session.update_agent(buddy)

    @function_tool
    async def end_mock_interview(
        self, context: RunContext[VoiceSessionState]
    ) -> tuple[Agent, str]:
        """End or cancel Interview Mode and hand the conversation back to Buddy.

        Call this whenever the user wants to stop the mock interview, leave
        Interview Mode, or go back to talking with Buddy normally.
        """
        state = context.userdata
        buddy_factory = state.buddy_factory
        if buddy_factory is None:
            # Unreachable in a real session (voice_agent.py wires the factory
            # before session.start), so this is a wiring bug, not a user-facing
            # one. ToolError keeps it a recoverable turn rather than tearing the
            # session down.
            raise lk_llm.ToolError("Interview Mode cannot hand back right now.")
        # RETURN_PENDING, not idle. The interview is over only once Buddy is
        # actually entered, and RETURN_PENDING -> RETURN_PENDING is a legal move,
        # so a user whose handback got interrupted can simply say it again and
        # this tool works a second time instead of refusing.
        state.interview.request_return("end_mock_interview")
        # Buddy resumes holding what was said in here. Instructions are excluded
        # because they are the supervisor's persona, not conversation.
        buddy = await buddy_factory(self.chat_ctx.copy(exclude_instructions=True))
        return buddy, "Interview Mode ended. Control returned to Buddy."
