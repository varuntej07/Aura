"""The interview setup step, as bounded work that returns a typed value.

An ``AgentTask``, not a handoff. LiveKit draws the line at responsibility: a
handoff is for when conversational identity changes and the new agent owns the
conversation from here on, while a task is for bounded work that produces a
result and gives control back. Intake is the second kind. It is a question that
returns a dossier, not a second personality, and modelling it as a task is what
keeps the supervisor responsible for the interview throughout.

Where it may be awaited is fixed by the SDK, not by taste: ``AgentTask`` raises
unless it is awaited inside a tool function or an ``on_enter``/``on_exit`` hook
(``livekit/agents/voice/agent.py``). The supervisor awaits it in ``on_enter``.

The branch is the model's to take, from what the user says, through the tools
below. There is no phrase list deciding whether someone has a job description.
"""

from __future__ import annotations

from livekit.agents import (
    AgentTask,
    RunContext,
    TurnHandlingOptions,
    function_tool,
    get_job_context,
)
from livekit.agents import llm as lk_llm

from ....lib.logger import logger
from .contracts import (
    MATERIAL_ARRIVAL_TIMEOUT_S,
    IntakeOutcome,
    InterviewIntakeResult,
)
from .materials import request_material_overlay
from .models import VoiceSessionState

INTERVIEW_FOCUS_OPTIONS = ("technical", "behavioral", "mixed")

INTAKE_TURN_HANDLING: TurnHandlingOptions = {
    "endpointing": {
        "mode": "dynamic",
        "min_delay": 0.6,
        "max_delay": 3.0,
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

INTAKE_INSTRUCTIONS = """
You are collecting the setup for a mock interview. Speak briefly and naturally,
one question at a time, and never read a form out loud.

Collect in this order:

1. The company they are interviewing with. Call record_company.
2. Whether they have the job description to hand. Ask; do not assume.
   - If they have it, call request_job_description. That puts a paste box on
     their screen. Tell them it is there and wait; do not ask them to read it
     out.
3. Collect the interview focus by calling record_interview_focus.
   - Must be one of: technical, behavioral, mixed.
4. Only if no job description arrived, call record_role_and_experience with the
   role and background.
5. Call finish_intake as soon as you have what you need.

If the job description does not arrive in this turn, treat it as unavailable and
continue, do not stall on it.

Stay on setup. Do not ask interview questions, plan an interview, start a timer,
score anything, or give feedback. If they ask to begin the interview itself, tell
them that part is not ready yet and call finish_intake.

If they want to stop, cancel, or go back to talking with Buddy, call
cancel_setup. Never leave them stuck answering setup questions.
""".strip()

_RETRY_PREFIX = """
Some of the setup is still missing: {missing}. Ask only for that, briefly, then
call finish_intake. Do not re-ask for anything you already have.
""".strip()


def retry_instructions(missing: tuple[str, ...]) -> str:
    """Instructions for one follow-up pass naming only what is absent."""
    readable = {
        "company": "which company",
        "target_role": "which role",
        "job_description": "the job description",
        "interview_focus": "which interview focus",
        "experience": "their background",
    }
    named = ", ".join(readable.get(field, field) for field in missing)
    return f"{_RETRY_PREFIX.format(missing=named)}\n\n{INTAKE_INSTRUCTIONS}"


class InterviewIntakeTask(AgentTask[InterviewIntakeResult]):
    """Collects the interview dossier, then completes with a typed result."""

    def __init__(
        self,
        *,
        state: VoiceSessionState,
        chat_ctx: lk_llm.ChatContext,
        instructions: str = INTAKE_INSTRUCTIONS,
    ) -> None:
        super().__init__(
            instructions=instructions,
            chat_ctx=chat_ctx,
            turn_handling=INTAKE_TURN_HANDLING,
            # Same reason as the supervisor: an agent that leaves this NOT_GIVEN
            # inherits the session's entire production MCP tool surface. Intake
            # asks three questions; it has no business setting reminders.
            mcp_servers=None,
        )
        self._state = state
        # Seeded from what the session already holds, so a retry pass starts from
        # what the first pass collected instead of asking for it again.
        self._draft = state.interview.dossier.model_copy(deep=True)
        # Stamped once, at construction, and carried out in the result. This is
        # what lets the supervisor tell "the answer to the interview I started"
        # from "an answer that arrived after the session moved on".
        self._interview_id = state.interview.interview_id
        self._ownership_epoch = state.interview.ownership_epoch

    @function_tool
    async def record_company(self, company: str) -> str:
        """Save the company the user is interviewing with.

        Args:
            company: The company name as the user said it.
        """
        normalized = " ".join(company.split())
        if not normalized:
            raise lk_llm.ToolError("record_company needs a non-empty company.")
        self._draft.company = normalized
        self._commit()
        return f"Company saved: {normalized}."

    @function_tool
    async def record_role_and_experience(
        self, target_role: str, experience: str
    ) -> str:
        """Save the target role and the users background, in their own words.

        Use this when the user does not have a job description to share.

        Args:
            target_role: The role they are interviewing for, as they described it.
            experience: What they said about their background. Keep their own
                phrasing; do not convert it into a job title or a number of years.
        """
        role = " ".join(target_role.split())
        normalized_experience = " ".join(experience.split())
        if not role:
            raise lk_llm.ToolError("record_role_and_experience needs a target_role.")
        if not normalized_experience:
            raise lk_llm.ToolError(
                "record_role_and_experience needs non-empty background experience."
            )
        self._draft.target_role = role
        self._draft.experience = normalized_experience
        self._commit()
        return f"Role saved: {role}."

    @function_tool
    async def record_interview_focus(self, interview_focus: str) -> str:
        """Save the requested interview focus.

        Args:
            interview_focus: Must be exactly one of technical, behavioral, mixed.
        """
        normalized = interview_focus.strip().lower()
        if normalized not in INTERVIEW_FOCUS_OPTIONS:
            raise lk_llm.ToolError(
                "record_interview_focus must be technical, behavioral, or mixed."
            )
        self._draft.interview_focus = normalized
        self._commit()
        return f"Interview focus saved: {normalized}."

    @function_tool
    async def request_job_description(self, context: RunContext[VoiceSessionState]) -> str:
        """Put a paste box on the users screen and wait for the job description.

        Use this only when the user says they have the job description available.
        The text arrives from their machine directly; they never read it aloud.
        """
        state = context.userdata
        store = state.materials
        if store is None:
            # No receiver wired, so nothing could ever arrive. Say so plainly and
            # let the model fall back rather than leaving the user watching for a
            # box that is never coming.
            return (
                "The paste box is unavailable on this device. "
                "Ask for the role and their background instead."
            )

        interview = state.interview
        revision = interview.next_material_revision()
        self._draft.job_description_requested = True
        self._draft.source = "jd"
        try:
            room = get_job_context().room
        except Exception:
            self._draft.source = "conversation"
            self._draft.job_description_requested = False
            return (
                "The paste box is unavailable right now. "
                "Ask for the role and their background instead."
            )

        # Everything from arming to the end of the wait sits under one finally.
        # The wait is long and interruptible, so a user who talks over it cancels
        # this coroutine mid-await; without the finally the store stays armed for
        # a paste box that is no longer on anyone's screen.
        armed_epoch = interview.ownership_epoch
        try:
            shown = await request_material_overlay(
                store=store,
                room=room,
                interview_id=interview.interview_id,
                revision=revision,
                ownership_epoch=armed_epoch,
            )
            if not shown:
                logger.info(
                    "InterviewIntake: overlay not confirmed",
                    {"interview_id": interview.interview_id, "revision": revision},
                )
                # Never claim a box is on their screen without proof it is.
                self._draft.source = "conversation"
                self._draft.job_description_requested = False
                return (
                    "The paste box did not open. "
                    "Ask for the role and their background instead."
                )

            async with context.with_filler(
                "Take your time, I'm watching for it.", delay=12.0, interval=25.0
            ):
                text = await store.wait_for_material(MATERIAL_ARRIVAL_TIMEOUT_S)
        finally:
            store.disarm()

        if not text:
            self._draft.source = "conversation"
            self._draft.job_description_requested = False
            return (
                "Nothing came through from the paste box. "
                "Ask for the role and their background instead."
            )

        # The text is real, but the interview it answers may not be any more: this
        # wait can outlive a cancellation or a return to Buddy. The epoch, not the
        # payload, is what decides that, which is why it never had to go on the
        # wire the desktop speaks.
        if not self._state.interview.is_current(self._interview_id, armed_epoch):
            logger.info(
                "InterviewIntake: material discarded, epoch moved",
                {
                    "interview_id": self._interview_id,
                    "armed_epoch": armed_epoch,
                    "current_epoch": self._state.interview.ownership_epoch,
                    "chars": len(text),
                },
            )
            self._draft.source = "conversation"
            self._draft.job_description_requested = False
            return (
                "That job description arrived too late to use. "
                "Ask for the role and their background instead."
            )

        self._draft.job_description_requested = True
        self._draft.source = "jd"
        self._draft.job_description = text
        self._commit()
        return (
            f"Job description received ({len(text)} characters). "
            "Do not read it back; acknowledge briefly and continue."
        )

    @function_tool
    async def finish_intake(self) -> None:
        """Finish setup only when every required field is present."""
        missing = self._draft.missing_fields()
        if missing:
            self._commit()
            missing_text = ", ".join(missing)
            raise lk_llm.ToolError(
                f"finish_intake cannot complete setup yet. Missing: {missing_text}"
            )
        self._commit()
        self.complete(self._result(IntakeOutcome.COMPLETED))

    @function_tool
    async def cancel_setup(self) -> None:
        """Stop setup and hand the user back to Buddy.

        Call this whenever the user wants to stop, cancel, or go back to talking
        with Buddy instead of finishing the interview setup.
        """
        self._commit()
        # The result still carries the dossier: what the user already answered is
        # worth keeping even though they walked away from the rest.
        self.complete(self._result(IntakeOutcome.CANCELLED))

    def _result(self, outcome: IntakeOutcome) -> InterviewIntakeResult:
        return InterviewIntakeResult(
            outcome=outcome,
            dossier=self._draft.model_copy(deep=True),
            interview_id=self._interview_id,
            ownership_epoch=self._ownership_epoch,
        )

    def _is_current(self) -> bool:
        return self._state.interview.is_current(
            self._interview_id, self._ownership_epoch
        )

    def _commit(self) -> None:
        """Publish the draft to session state after every change.

        Written through on each tool rather than only at completion, so an intake
        that is cancelled or times out still leaves behind what the user actually
        answered.

        Skipped once the session has moved on: a task still finishing a turn for
        an interview that already ended must not write over the state of whatever
        replaced it.
        """
        if not self._is_current():
            logger.info(
                "InterviewIntake: draft commit skipped, epoch moved",
                {
                    "interview_id": self._interview_id,
                    "task_epoch": self._ownership_epoch,
                    "current_epoch": self._state.interview.ownership_epoch,
                },
            )
            return
        self._state.interview.dossier = self._draft.model_copy(deep=True)
