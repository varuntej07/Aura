"""The agent that actually runs the interview, once a plan exists.

A second handoff, for the same reason as the first: responsibility changes. The
supervisor owns setup and knows nothing about conducting an interview; this agent
owns the conversation from the first question to the last and hands back to Buddy
when it is done. It gets the same isolation as the supervisor, including the
explicit ``mcp_servers=None`` that keeps the session's whole production tool
surface out of it.

**The plan is fixed and the cursor is state, not prompt.** The agent is told what
question is on the table; it cannot invent, reorder, or re-plan, because the only
way to move is through ``next_question``/``skip_question`` and the only place the
index lives is ``InterviewState``. That is what keeps "which question are we on"
from drifting between what was said and what the session believes.

What the model decides, and what it must not:

- **When an answer is finished** is the model's judgment. LiveKit supplies
  finalized user turns; the interviewer simply does not call ``next_question``
  until it judges the candidate has finished. There is no transcript heuristic
  or phrase list watching for completion, because ordinary English is not a
  reliable authorization or finalization signal.
- **Clarifying** uses repeat_question for verbatim repeats and normal speech for
  short directional nudges; the model still cannot move the cursor without tools.
- **Repeating** is a tool only so the wording comes back verbatim from state
  rather than from the model's memory of it.

Every delivered question is logged with its plan question_id and cursor position.
"""

from __future__ import annotations

from livekit.agents import Agent, RunContext, TurnHandlingOptions, function_tool
from livekit.agents import llm as lk_llm

from ....lib.logger import logger
from .debrief import InterviewDebriefService
from .models import InterviewPhase, InterviewQuestion, VoiceSessionState
from .time_limit import start_interview_time_guard

INTERVIEWER_TURN_HANDLING: TurnHandlingOptions = {
    "endpointing": {
        "mode": "dynamic",
        "min_delay": 0.8,
        "max_delay": 4.0,
        "alpha": 0.8,
    },
    "interruption": {
        "enabled": True,
        "mode": "adaptive",
        "min_duration": 0.5,
        "resume_false_interruption": True,
        "false_interruption_timeout": 1.5,
    },
}

INTERVIEWER_INSTRUCTIONS = """
You are conducting a mock interview. You are not Buddy, you are not a coach, and
you have no general-assistant capabilities.

Behave like a real interviewer: neutral, attentive, and evidence-seeking.

- Ask ONE planned question at a time.
- The plan in state is the only question source. Keep cursor movement strictly
  to next_question/skip_question.
- When the candidate has completed an answer, call next_question with a concise,
  faithful account of that answer before moving the cursor. Never add facts or
  judgment to that account.
- Do not invent or summarize a new interview question in ordinary speech.
- Keep your own turns short; the candidate should be doing most of the talking.
- Let answers breathe and only move on after a complete answer.
- If they ask what you mean, rephrase the current question once and call
  repeat_question.
- If they want to hear it again word for word, call repeat_question.
- If they want to move past this one, call skip_question.
- If they want to stop, be done, or go back to Buddy, call end_interview.

Ask follow-up probes through ask_follow_up only.
- Never say automatic praise or agreement.
- Never coach or provide the ideal answer.
- Challenge vague, contradictory, or nonresponsive answers.
- Ask about ownership, scale, tradeoffs, failures, measurable outcomes, and
  concrete alternatives.

Do not add spoken filler for incomplete turns. Wait for the answer to finish.

Never score, rate, grade, or compare them. Never say whether they would get
the role.
Never read the job description aloud, and never repeat back long stretches of
what they just said.
""".strip()


def _ask_instructions(
    question_text: str, *, position: int, total: int, question_id: str
) -> str:
    """What to say when a new question goes on the table."""
    if position == 1:
        lead = (
            "Open the interview: one short warm sentence to settle them, then ask "
            "the question below."
        )
    elif position == total:
        lead = (
            "Acknowledge neutrally, then say this is the last one and ask the "
            "question below."
        )
    else:
        lead = (
            "Acknowledge neutrally, then ask the question below."
        )
    return (
        f"{lead} Ask it in your own words, naturally, but keep its substance "
        f"exactly. Do not number it or mention how many are left.\n\n"
        f"Question {position} of {total} [plan_id={question_id}]: {question_text}"
    )


def _normalize_transcript(text: str) -> str:
    return " ".join(text.split())


class InterviewerAgent(Agent):
    """Walks the fixed plan, then hands the conversation back to Buddy."""

    def __init__(self, *, state: VoiceSessionState, chat_ctx: lk_llm.ChatContext) -> None:
        super().__init__(
            instructions=INTERVIEWER_INSTRUCTIONS,
            chat_ctx=chat_ctx,
            turn_handling=INTERVIEWER_TURN_HANDLING,
            # Same reason as the supervisor: without an explicit None this agent
            # inherits the session's entire production MCP tool surface, and an
            # interviewer has no business setting reminders.
            mcp_servers=None,
        )
        self._state = state
        self._debrief = InterviewDebriefService()

    async def on_enter(self) -> None:
        """Commit INTERVIEWING, then ask the first question.

        Committed HERE for the same reason entry is committed in the supervisor's
        on_enter: this hook is the first moment LiveKit has actually made this
        agent the one talking. A commit in the supervisor would claim the
        interview started even if this activation never landed.
        """
        interview = self._state.interview
        if not interview.note_interviewing():
            # The session moved on while this activation was in flight. Do not
            # start asking into a conversation somebody else owns.
            logger.warn(
                "Interviewer: entry not committed, staying silent",
                {
                    "interview_id": interview.interview_id,
                    "phase": str(interview.phase),
                    "ownership_epoch": interview.ownership_epoch,
                },
            )
            return

        question = interview.current_question
        if question is None:
            # An empty plan should be impossible: QuestionPlanService always
            # returns a usable one. If it happens anyway, leaving the user in
            # silence with an interviewer that has nothing to ask is the one
            # outcome worth spending a handback on.
            logger.warn(
                "Interviewer: entered with an empty plan",
                {"interview_id": interview.interview_id},
            )
            await self._hand_back_to_buddy("empty_plan")
            return

        logger.info(
            "Interviewer: started",
            {
                "interview_id": interview.interview_id,
                "questions": interview.plan.count,
            },
        )
        start_interview_time_guard(session=self.session, state=self._state)
        self.session.generate_reply(
            instructions=self._question_instructions(question, position=1)
        )

    async def on_exit(self) -> None:
        """Terminate cleanly if this agent is torn down without returning.

        The ordinary exit is already RETURN_PENDING by the time LiveKit swaps the
        agent, and terminate() refuses that move. This covers the other case: the
        call ended mid-interview, and leaving INTERVIEWING behind would keep every
        ambient producer suspended for the rest of the session.
        """
        interview = self._state.interview
        if interview.phase is InterviewPhase.INTERVIEWING:
            interview.terminate("interviewer_exited")

    async def _hand_back_to_buddy(self, reason: str) -> None:
        """Return control from inside a hook, without a tool call."""
        buddy_factory = self._state.buddy_factory
        if buddy_factory is None:
            return
        self._state.interview.request_return(reason)
        buddy = await buddy_factory(self.chat_ctx.copy(exclude_instructions=True))
        self.session.update_agent(buddy)

    async def _buddy_for_return(
        self, state: VoiceSessionState, reason: str
    ) -> Agent:
        """The Buddy to hand a tool's return value, with the return recorded."""
        buddy_factory = state.buddy_factory
        if buddy_factory is None:
            # Unreachable in a real session (voice_agent.py wires the factory
            # before session.start), so this is a wiring bug. ToolError keeps it a
            # recoverable turn rather than tearing the session down.
            raise lk_llm.ToolError("The interview cannot hand back right now.")
        # RETURN_PENDING, not idle: the interview is over only once Buddy is
        # actually entered, and the move is idempotent, so an interrupted return
        # can simply be asked for again.
        state.interview.request_return(reason)
        return await buddy_factory(self.chat_ctx.copy(exclude_instructions=True))

    def _advance(
        self, context: RunContext[VoiceSessionState], *, skipped: bool
    ) -> InterviewQuestion | None:
        """Move the cursor and return the next question, or None at the end."""
        interview = context.userdata.interview
        asked = interview.question_index + 1
        question = interview.advance_question(skipped=skipped)
        logger.info(
            "Interviewer: question closed",
            {
                "interview_id": interview.interview_id,
                "position": asked,
                "total": interview.plan.count,
                "skipped": skipped,
                "remaining": interview.questions_remaining,
            },
        )
        return question

    def _question_instructions(
        self, question: InterviewQuestion, *, position: int
    ) -> str:
        """Build the prompt and log every question delivery."""
        interview = self._state.interview
        q_id = question.question_id or f"q{position:02d}"
        logger.info(
            "Interviewer: delivering question",
            {
                "interview_id": interview.interview_id,
                "question_id": q_id,
                "position": position,
                "total": interview.plan.count,
            },
        )
        return _ask_instructions(
            question.text, position=position, total=interview.plan.count, question_id=q_id
        )

    @function_tool
    async def next_question(
        self, context: RunContext[VoiceSessionState], answer: str
    ) -> tuple[Agent, str] | str:
        """Move on to the next question, once the candidate has finished answering.

        Call this only when their answer is genuinely complete. A pause, a
        thinking-out-loud aside, or a half-finished thought is not the end of an
        answer; let them keep going and call this afterwards. Do not call it when
        they are asking you what the question means.

        Args:
            answer: A concise, faithful account of the candidate's completed
                answer to the current question. Keep their facts and outcomes;
                do not judge, add, or infer anything.
        """
        interview = context.userdata.interview
        if interview.current_question is None:
            return "No question is currently active."
        if not interview.record_answer(answer):
            raise lk_llm.ToolError(
                "next_question needs a non-empty answer for the current question."
            )
        if interview.hard_time_cap_reached:
            raise lk_llm.StopResponse()
        next_question = self._advance(context, skipped=False)
        if next_question is None:
            return await self._complete_and_debrief(context, reason="plan_completed")
        return self._question_instructions(
            next_question, position=interview.question_index + 1
        )

    @function_tool
    async def skip_question(
        self, context: RunContext[VoiceSessionState]
    ) -> tuple[Agent, str] | str:
        """Move past the current question without an answer, at their request.

        Use this when the candidate wants to pass on this one, come back to it,
        or move along. Do not use it to end the whole interview.
        """
        interview = context.userdata.interview
        if interview.hard_time_cap_reached:
            raise lk_llm.StopResponse()
        next_question = self._advance(context, skipped=True)
        if next_question is None:
            return await self._complete_and_debrief(
                context, reason="plan_completed_after_skip"
            )
        return (
            "Acknowledge the skip in a few words, without judgment, then "
            + self._question_instructions(
                next_question, position=interview.question_index + 1
            )
        )

    @function_tool
    async def repeat_question(self, context: RunContext[VoiceSessionState]) -> str:
        """Say the current question again, word for word.

        Use this when the candidate did not catch it or asks to hear it again.
        This does NOT move the interview on; they still have to answer it.
        """
        interview = context.userdata.interview
        if interview.hard_time_cap_reached:
            raise lk_llm.StopResponse()
        question = interview.current_question
        if question is None:
            return "There is no question on the table right now."
        # Verbatim out of state rather than out of the model's memory of it: a
        # question that drifts on every repeat is a question nobody can answer.
        return (
            "Say this question again, word for word, with no preamble and no "
            f"rephrasing: {question.text}"
        )

    @function_tool
    async def ask_follow_up(
        self,
        context: RunContext[VoiceSessionState],
        *,
        question_id: str,
        follow_up: str,
    ) -> None:
        """Ask one follow-up question tied to the current planned question.

        Args:
            question_id: Must match the currently delivered question ID.
            follow_up: The exact follow-up you want the interviewer to ask.
        """
        interview = context.userdata.interview
        if interview.hard_time_cap_reached:
            raise lk_llm.StopResponse()
        if interview.current_question is None:
            raise lk_llm.ToolError("No active question to follow up on.")
        if question_id != interview.current_question_id:
            raise lk_llm.ToolError(
                "ask_follow_up must target the currently active question."
            )
        if interview.follow_up_used_for_current_question:
            raise lk_llm.ToolError(
                "A follow-up was already asked for this question. Move on with "
                "next_question or skip_question."
            )

        normalized = _normalize_transcript(follow_up)
        if not normalized:
            raise lk_llm.ToolError("ask_follow_up requires a non-empty follow-up.")

        if len(normalized) > 420:
            raise lk_llm.ToolError("ask_follow_up cannot exceed 420 characters.")

        interview.follow_up_used_for_current_question = True
        logger.info(
            "Interviewer: follow-up asked",
            {
                "interview_id": interview.interview_id,
                "question_id": interview.current_question_id,
                "position": interview.question_index + 1,
                "ownership_epoch": interview.ownership_epoch,
            },
        )
        try:
            context.session.say(normalized)
        except Exception as exc:
            logger.warn(
                "Interviewer: follow-up speech failed",
                {
                    "interview_id": interview.interview_id,
                    "question_id": interview.current_question_id,
                    "error_type": type(exc).__name__,
                },
            )
            raise lk_llm.ToolError("Unable to speak follow-up right now.") from exc
        raise lk_llm.StopResponse()

    @function_tool
    async def end_interview(
        self, context: RunContext[VoiceSessionState]
    ) -> tuple[Agent, str]:
        """End the interview now and hand the conversation back to Buddy.

        Call this whenever the candidate wants to stop, is finished, wants to
        leave the interview, or wants to go back to talking with Buddy. Also call
        it for anything unrelated to the interview.
        """
        if context.userdata.interview.hard_time_cap_reached:
            # The timer owns the final spoken boundary and debrief once the cap
            # has landed; an overlapping model tool call must not cancel it.
            raise lk_llm.StopResponse()
        return await self._finish(context, reason="ended_by_request")

    async def _finish(
        self, context: RunContext[VoiceSessionState], *, reason: str
    ) -> tuple[Agent, str]:
        """The one exit: record the return, then give Buddy back the floor."""
        state = context.userdata
        interview = state.interview
        logger.info(
            "Interviewer: finished",
            {
                "interview_id": interview.interview_id,
                "reason": reason,
                "asked": interview.question_index,
                "total": interview.plan.count,
                "skipped": interview.skipped_count,
            },
        )
        buddy = await self._buddy_for_return(state, reason)
        return buddy, "The interview is over. Control returned to Buddy."

    async def _complete_and_debrief(
        self, context: RunContext[VoiceSessionState], *, reason: str
    ) -> tuple[Agent, str]:
        """Speak one bounded, session-only debrief before returning to Buddy."""
        interview = context.userdata.interview
        if not interview.claim_debrief():
            raise lk_llm.StopResponse()
        debrief = await self._debrief.build(interview.dossier, interview.answers)
        try:
            # LiveKit documents speech from a function tool; awaiting its handle
            # keeps the handoff from cutting off the feedback mid-sentence.
            await context.session.say(debrief)
        except Exception as exc:
            logger.warn(
                "Interviewer: debrief speech failed",
                {
                    "interview_id": interview.interview_id,
                    "answer_count": len(interview.answers),
                    "error_type": type(exc).__name__,
                },
            )
        return await self._finish(context, reason=reason)
