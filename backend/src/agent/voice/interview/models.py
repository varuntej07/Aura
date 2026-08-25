"""State that outlives an agent handoff, and the typed result of intake.

Agent instances do not survive a handoff; ``AgentSession.userdata`` does, so
everything the supervisor, the intake task and the worker must agree on lives
here and nowhere on an agent. ``VoiceSessionState`` is the whole session's
userdata object, which is why ``pipelines.py`` reaches into this package to build
the session.

Feature state is nested rather than flattened into loose fields, so the second
feature to need session state does not force every ``RunContext`` annotation in
the worker to change.

**Who commits what is the whole point of this module.** Interview Mode is a
handoff, and a handoff is only real once LiveKit has activated the new agent. So:

- Entry is committed in ``InterviewSupervisorAgent.on_enter``, never in the tool
  that returns the supervisor. A tool that returned an agent LiveKit then failed
  to activate would otherwise leave the session believing an interview is running
  with nothing behind it.
- IDLE is committed in ``BuddyAgent.on_enter``, never in the tool or hook that
  asks to return. An interrupted return must leave the supervisor ACTIVE and the
  return RETRYABLE, which is exactly what ``RETURN_PENDING`` encodes.

Every phase move goes through the guarded ``transition``; an illegal move is
logged and refused rather than silently applied.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from ....lib.logger import logger
from ....services.interview_preparation import InterviewBrief
from ..guide_session_state import GuideSessionState
from .contracts import START_CLAIM_TTL_S, BuddyFactory

if TYPE_CHECKING:
    from .materials import InterviewMaterialStore


class InterviewPhase(StrEnum):
    IDLE = "idle"
    INTAKE = "intake"
    SETUP_CAPTURED = "setup_captured"
    # The supervisor is making its bounded role-aware planning attempt(s). A
    # distinct phase because it is the only stretch where the interview is
    # neither collecting nor asking, and a failure here has a dossier but no
    # questions.
    PLANNING = "planning"
    # InterviewerAgent has the conversation and is working through the plan.
    INTERVIEWING = "interviewing"
    # The supervisor has asked to hand back and is WAITING for Buddy to actually
    # be entered. It is still the active agent here, and end_mock_interview may
    # be called again from this phase: that is what makes an interrupted return
    # retryable instead of a session stuck between two owners.
    RETURN_PENDING = "return_pending"
    # The user walked away from setup. Distinct from TERMINATED because what they
    # already answered is kept and the return is an ordinary, expected one.
    CANCELLED = "cancelled"
    # Interview Mode ended abnormally (intake raised, session shutting down). Not
    # resumable; the only move out is a return to Buddy.
    TERMINATED = "terminated"


class ConversationOwner(StrEnum):
    BUDDY = "buddy"
    INTERVIEW = "interview"
    GUIDE = "guide"


# The one authority on which phase moves exist. Anything absent is refused.
#
# RETURN_PENDING -> RETURN_PENDING is deliberate and load-bearing: a user whose
# handback got interrupted says "stop" again, and the second attempt must be a
# legal, idempotent no-op rather than a refused transition that strands them.
_ALLOWED_TRANSITIONS: dict[InterviewPhase, frozenset[InterviewPhase]] = {
    InterviewPhase.IDLE: frozenset({InterviewPhase.INTAKE}),
    InterviewPhase.INTAKE: frozenset(
        {
            InterviewPhase.SETUP_CAPTURED,
            InterviewPhase.CANCELLED,
            # The supervisor's end_mock_interview is live throughout, so a user
            # who says "stop" while intake is still asking leaves from here. That
            # is an ordinary exit, not a broken one.
            InterviewPhase.RETURN_PENDING,
            InterviewPhase.TERMINATED,
        }
    ),
    InterviewPhase.SETUP_CAPTURED: frozenset(
        {
            InterviewPhase.PLANNING,
            InterviewPhase.RETURN_PENDING,
            InterviewPhase.TERMINATED,
        }
    ),
    InterviewPhase.PLANNING: frozenset(
        {
            InterviewPhase.INTERVIEWING,
            InterviewPhase.RETURN_PENDING,
            InterviewPhase.TERMINATED,
        }
    ),
    InterviewPhase.INTERVIEWING: frozenset(
        {InterviewPhase.RETURN_PENDING, InterviewPhase.TERMINATED}
    ),
    InterviewPhase.CANCELLED: frozenset(
        {InterviewPhase.RETURN_PENDING, InterviewPhase.TERMINATED}
    ),
    InterviewPhase.TERMINATED: frozenset({InterviewPhase.RETURN_PENDING}),
    InterviewPhase.RETURN_PENDING: frozenset(
        {
            InterviewPhase.IDLE,
            InterviewPhase.RETURN_PENDING,
            InterviewPhase.TERMINATED,
        }
    ),
}


class InterviewDossier(BaseModel):
    """What intake collected. The typed value the intake task returns."""

    model_config = ConfigDict(extra="forbid")

    company: str = ""
    target_role: str = ""
    interview_focus: str = ""
    # Free text, in the user's own words ("about six years, mostly Go and
    # Postgres"). Not an int and not a seniority label: coercing it would force a
    # number out of an answer that legitimately does not contain one.
    experience: str = ""
    # Raw pasted text, never parsed here. Empty on the conversational branch.
    job_description: str = ""
    brief: InterviewBrief | None = None
    source: Literal["conversation", "jd"] = "conversation"
    # Explicit branch decision: True means the user asserted they had a JD to share.
    job_description_requested: bool = False

    _VALID_FOCUS = {"technical", "behavioral", "mixed"}

    def missing_fields(self) -> tuple[str, ...]:
        """Required fields this dossier still lacks, in the order to ask for them.

        Which fields are required depends on the branch taken: a JD carries the
        role and the requirements, so asking for them separately would be asking
        the user to retype what they just pasted.
        """
        missing: list[str] = []
        if not self.company.strip():
            missing.append("company")
        focus = self.interview_focus.strip().lower()
        if focus not in self._VALID_FOCUS:
            missing.append("interview_focus")
        if self.source == "jd":
            if not self.job_description.strip():
                missing.append("job_description")
        else:
            if not self.target_role.strip():
                missing.append("target_role")
            if not self.experience.strip():
                missing.append("experience")
        return tuple(missing)

    @property
    def is_complete(self) -> bool:
        return not self.missing_fields()


class InterviewQuestion(BaseModel):
    """One planned question. The unit the interviewer walks through."""

    model_config = ConfigDict(extra="forbid")

    # Spoken verbatim by the interviewer, so it has to read as speech, not as a
    # form field. Never rendered to a screen anywhere in this phase.
    text: str
    # Stable question identity for traceability in logs.
    question_id: str = ""
    # A two-or-three word label ("system design", "conflict") used to keep the
    # plan diverse and to log progress without logging the question itself.
    focus: str = ""


class InterviewAnswer(BaseModel):
    """One candidate answer retained only for this live interview session."""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    question_text: str
    text: str


class QuestionPlan(BaseModel):
    """The whole set of questions for one interview, fixed before it starts.

    Generated before the interview and never changed while interviewing.
    Follow-ups, scoring and adaptive re-planning are deliberately absent: the
    interviewer walks a fixed list, which keeps the run bounded and legible.
    """

    model_config = ConfigDict(extra="forbid")

    questions: list[InterviewQuestion] = Field(default_factory=list)
    @property
    def count(self) -> int:
        return len(self.questions)


@dataclass(frozen=True, slots=True)
class InterviewStartClaim:
    """One outstanding request to enter Interview Mode, not yet committed.

    Minted by the tool, spent by the supervisor's ``on_enter``. It exists because
    those two moments are not the same moment: LiveKit may never activate the
    agent the tool returned, and the claim is what makes that outcome recoverable
    instead of permanent.
    """

    interview_id: str
    ownership_epoch: int
    claimed_at: float

    def is_expired(self, now: float) -> bool:
        return (now - self.claimed_at) >= START_CLAIM_TTL_S


@dataclass(slots=True)
class InterviewState:
    """Live interview state, shared by every agent in the session."""

    phase: InterviewPhase = InterviewPhase.IDLE
    # Correlates the overlay request, its acknowledgement, and the byte stream
    # that answers it. Minted per interview so a stale paste from a previous one
    # cannot be accepted against this one.
    interview_id: str = ""
    # Bumped per overlay request within one interview. The pair
    # (interview_id, revision) is what the material receiver matches on.
    material_revision: int = 0
    # Bumped every time conversation ownership changes hands. Every async result
    # (an intake task completing, a pasted job description arriving) carries the
    # epoch it was issued under and is discarded when the epoch has moved on.
    # interview_id alone is not enough: it answers "which interview", while the
    # epoch answers "is that interview still the one in charge".
    ownership_epoch: int = 0
    pending_start: InterviewStartClaim | None = None
    dossier: InterviewDossier = field(default_factory=InterviewDossier)
    plan: QuestionPlan = field(default_factory=QuestionPlan)
    # Index of the question currently on the table. Equal to plan.count once the
    # last one has been answered, which is how "the interview is over" is a fact
    # about the cursor rather than a flag someone has to remember to set.
    question_index: int = 0
    # Questions the user asked to move past. Counted, never used to decide
    # anything: it exists so a run that was mostly skipped is visible in the log.
    skipped_count: int = 0
    # Single follow-up allowance per question in this prototype phase.
    follow_up_used_for_current_question: bool = False
    # Answers are intentionally session-only. They are used once for the spoken
    # debrief and are discarded when this AgentSession ends.
    answers: list[InterviewAnswer] = field(default_factory=list)
    # The interview clock starts only once the interviewer is genuinely active,
    # not while setup or question planning is still in progress. It is owned by
    # the live AgentSession and is cancelled on every ordinary return to Buddy.
    time_guard_task: asyncio.Task[None] | None = field(default=None, repr=False)
    time_guard_started_at: float | None = None
    soft_time_warning_due: bool = False
    final_time_warning_due: bool = False
    hard_time_cap_reached: bool = False
    debrief_started: bool = False

    @property
    def current_question_id(self) -> str:
        question = self.current_question
        if question is not None and question.question_id:
            return question.question_id
        return f"q{self.question_index + 1:02d}"

    def _clear_question_turn_state(self) -> None:
        self.follow_up_used_for_current_question = False

    @property
    def owner(self) -> ConversationOwner:
        return (
            ConversationOwner.BUDDY
            if self.phase is InterviewPhase.IDLE
            else ConversationOwner.INTERVIEW
        )

    @property
    def active(self) -> bool:
        return self.owner is ConversationOwner.INTERVIEW

    def is_current(self, interview_id: str, ownership_epoch: int) -> bool:
        """Whether a result issued under this identity still speaks for the session."""
        return (
            bool(interview_id)
            and interview_id == self.interview_id
            and ownership_epoch == self.ownership_epoch
        )

    def claim_start(self) -> InterviewStartClaim | None:
        """Reserve entry into Interview Mode. None when one is already under way.

        Deliberately does NOT touch ``phase``: entry is committed by the
        supervisor once LiveKit activates it, and this is only the reservation
        that makes that commit verifiable.

        Refuses a second claim while a fresh one is outstanding, which is the
        duplicate-start guard: two tool calls emitted for one utterance produce
        one supervisor, not two. The TTL is what keeps that guard from becoming a
        permanent block when the handoff it was waiting for never happened.
        """
        if self.phase is not InterviewPhase.IDLE:
            return None
        now = time.monotonic()
        outstanding = self.pending_start
        if outstanding is not None and not outstanding.is_expired(now):
            logger.info(
                "Interview: duplicate start refused",
                {
                    "interview_id": outstanding.interview_id,
                    "ownership_epoch": outstanding.ownership_epoch,
                    "age_s": round(now - outstanding.claimed_at, 3),
                },
            )
            return None
        if outstanding is not None:
            logger.warn(
                "Interview: start claim expired without activation",
                {
                    "interview_id": outstanding.interview_id,
                    "ownership_epoch": outstanding.ownership_epoch,
                    "age_s": round(now - outstanding.claimed_at, 3),
                },
            )
        self.ownership_epoch += 1
        self.interview_id = uuid.uuid4().hex
        self.material_revision = 0
        self.dossier = InterviewDossier()
        self.plan = QuestionPlan()
        self.question_index = 0
        self.skipped_count = 0
        self.answers = []
        self._cancel_time_guard()
        self.time_guard_started_at = None
        self.soft_time_warning_due = False
        self.final_time_warning_due = False
        self.hard_time_cap_reached = False
        self.debrief_started = False
        claim = InterviewStartClaim(
            interview_id=self.interview_id,
            ownership_epoch=self.ownership_epoch,
            claimed_at=now,
        )
        self.pending_start = claim
        return claim

    def commit_entry(self, claim: InterviewStartClaim) -> bool:
        """IDLE -> INTAKE. The ONLY place entry is committed, from on_enter."""
        outstanding = self.pending_start
        if outstanding is None or outstanding != claim:
            logger.warn(
                "Interview: entry commit rejected, claim superseded",
                {
                    "claim_interview_id": claim.interview_id,
                    "claim_epoch": claim.ownership_epoch,
                    "current_epoch": self.ownership_epoch,
                    "phase": str(self.phase),
                },
            )
            return False
        if not self.is_current(claim.interview_id, claim.ownership_epoch):
            logger.warn(
                "Interview: entry commit rejected, epoch moved",
                {
                    "claim_interview_id": claim.interview_id,
                    "claim_epoch": claim.ownership_epoch,
                    "current_epoch": self.ownership_epoch,
                },
            )
            return False
        if not self.transition(InterviewPhase.INTAKE, "supervisor_entered"):
            return False
        self.pending_start = None
        return True

    def commit_idle(self, ownership_epoch: int) -> bool:
        """RETURN_PENDING -> IDLE. The ONLY place IDLE is committed, from Buddy's on_enter.

        Bumps the epoch on the way out so anything still in flight for the
        interview that just ended is stale by construction.
        """
        if ownership_epoch != self.ownership_epoch:
            logger.warn(
                "Interview: idle commit rejected, epoch moved",
                {
                    "resume_epoch": ownership_epoch,
                    "current_epoch": self.ownership_epoch,
                    "phase": str(self.phase),
                },
            )
            return False
        if not self.transition(InterviewPhase.IDLE, "buddy_entered"):
            return False
        self.ownership_epoch += 1
        self.pending_start = None
        self._cancel_time_guard()
        return True

    def note_setup_captured(self) -> bool:
        return self.transition(InterviewPhase.SETUP_CAPTURED, "intake_completed")

    def note_cancelled(self) -> bool:
        return self.transition(InterviewPhase.CANCELLED, "intake_cancelled")

    def note_planning(self) -> bool:
        return self.transition(InterviewPhase.PLANNING, "planning_questions")

    def note_interviewing(self) -> bool:
        """SETUP -> asking. Committed in ``InterviewerAgent.on_enter``, like entry."""
        return self.transition(InterviewPhase.INTERVIEWING, "interviewer_entered")

    def arm_time_guard(self, task: asyncio.Task[None]) -> None:
        """Own one wall-clock guard for the active interviewer handoff."""
        self._cancel_time_guard()
        self.time_guard_task = task
        self.time_guard_started_at = time.monotonic()
        self.soft_time_warning_due = False
        self.final_time_warning_due = False
        self.hard_time_cap_reached = False
        self.debrief_started = False

    def mark_soft_time_warning_due(self) -> None:
        self.soft_time_warning_due = True

    def mark_final_time_warning_due(self) -> None:
        self.final_time_warning_due = True

    def mark_hard_time_cap_reached(self) -> None:
        self.hard_time_cap_reached = True

    def claim_debrief(self) -> bool:
        """Ensure a normal completion and the timer cannot both debrief."""
        if self.debrief_started:
            return False
        self.debrief_started = True
        return True

    def _cancel_time_guard(self) -> None:
        task = self.time_guard_task
        self.time_guard_task = None
        if task is not None and not task.done():
            task.cancel()

    def adopt_plan(self, plan: QuestionPlan) -> None:
        """Install the plan and rewind the cursor. Planned once, never re-planned."""
        self.plan = plan
        self.question_index = 0
        self.skipped_count = 0
        self._clear_question_turn_state()

    @property
    def current_question(self) -> InterviewQuestion | None:
        """The question on the table, or None once the plan is exhausted."""
        if 0 <= self.question_index < self.plan.count:
            return self.plan.questions[self.question_index]
        return None

    @property
    def questions_remaining(self) -> int:
        return max(0, self.plan.count - self.question_index)

    def advance_question(self, *, skipped: bool = False) -> InterviewQuestion | None:
        """Move past the current question and return the next one, or None at the end.

        The single mutation point for the cursor, so "which question are we on"
        can never disagree between the agent asking and the state everyone reads.
        """
        if self.question_index >= self.plan.count:
            return None
        if skipped:
            self.skipped_count += 1
        self.question_index += 1
        self._clear_question_turn_state()
        return self.current_question

    def record_answer(self, text: str) -> bool:
        """Keep one model-confirmed answer for the question on the table.

        The interviewer model supplies this only when it decides an answer is
        complete. There is deliberately no transcript phrase list or heuristic
        attempting to make that decision in code.
        """
        question = self.current_question
        normalized = " ".join(text.split())
        if question is None or not normalized:
            return False
        self.answers.append(
            InterviewAnswer(
                question_id=self.current_question_id,
                question_text=question.text,
                text=normalized,
            )
        )
        return True

    def request_return(self, reason: str) -> bool:
        """Ask to go back to Buddy. Ownership does NOT move until Buddy enters."""
        transitioned = self.transition(InterviewPhase.RETURN_PENDING, reason)
        if transitioned:
            self._cancel_time_guard()
        return transitioned

    def terminate(self, reason: str) -> bool:
        transitioned = self.transition(InterviewPhase.TERMINATED, reason)
        if transitioned:
            self._cancel_time_guard()
        return transitioned

    def transition(self, to: InterviewPhase, reason: str) -> bool:
        """Apply one phase move if the table allows it. Never raises."""
        if to not in _ALLOWED_TRANSITIONS.get(self.phase, frozenset()):
            logger.warn(
                "Interview: illegal transition refused",
                {
                    "from": str(self.phase),
                    "to": str(to),
                    "reason": reason,
                    "interview_id": self.interview_id,
                    "ownership_epoch": self.ownership_epoch,
                },
            )
            return False
        previous = self.phase
        self.phase = to
        logger.info(
            "Interview: phase",
            {
                "from": str(previous),
                "to": str(to),
                "reason": reason,
                "interview_id": self.interview_id,
                "ownership_epoch": self.ownership_epoch,
                "owner": str(self.owner),
            },
        )
        return True

    def next_material_revision(self) -> int:
        self.material_revision += 1
        return self.material_revision


@dataclass(slots=True)
class VoiceSessionState:
    """The object held by ``AgentSession.userdata``, shared across handoffs."""

    interview: InterviewState = field(default_factory=InterviewState)
    guide: GuideSessionState = field(default_factory=GuideSessionState)
    buddy_factory: BuddyFactory | None = None
    # Wired in voice_agent.py alongside the byte-stream handler registration.
    # Reached through userdata rather than a module global so a second concurrent
    # session in the same worker process cannot see this one's materials.
    materials: InterviewMaterialStore | None = None

    @property
    def owner(self) -> ConversationOwner:
        """The single live conversation owner across all specialized modes."""
        if self.interview.active:
            return ConversationOwner.INTERVIEW
        if self.guide.active:
            return ConversationOwner.GUIDE
        return ConversationOwner.BUDDY


def interview_owns_conversation(session: object) -> bool:
    """Whether Interview Mode currently owns the conversation on this session.

    The single predicate every ambient producer consults before injecting speech
    (proactive Guide nudges, the away nudge, screen/OCR context). They all
    already hold the ``AgentSession``, so this needs no new plumbing.

    Fail-open on anything unexpected: a session whose userdata is not ours is a
    session with no Interview Mode, and a broken read must never silence Buddy.
    """
    try:
        userdata = getattr(session, "userdata", None)
        if not isinstance(userdata, VoiceSessionState):
            return False
        return userdata.owner is ConversationOwner.INTERVIEW
    except Exception:
        return False


def buddy_owns_conversation(session: object) -> bool:
    """Whether the long-lived BuddyAgent currently owns this session."""
    try:
        userdata = getattr(session, "userdata", None)
        if not isinstance(userdata, VoiceSessionState):
            return True
        return userdata.owner is ConversationOwner.BUDDY
    except Exception:
        return True
