"""Interview Mode: the one place a voice session hands off to a different agent.

Buddy's LLM selects ``start_mock_interview`` by ordinary tool reasoning, control
moves to a separate ``InterviewSupervisorAgent``, and the setup step runs behind
that boundary as a bounded ``AgentTask`` that returns a typed dossier.

The split follows LiveKit's own distinction. A **handoff** is for when
conversational identity and responsibility change, which is Buddy to the
supervisor. An **AgentTask** is for bounded work that returns a result and gives
control back, which is intake.

```text
Buddy
  -> InterviewSupervisorAgent
  -> await InterviewIntakeTask[InterviewIntakeResult]
       -> company
       -> has JD?
          -> no : conversational role + experience, no overlay
          -> yes: one revisioned JD overlay over a bounded byte stream
       -> typed dossier
  -> validate, one retry if incomplete
  -> speak the confirmation WHILE QuestionPlanService makes one role-aware call
       -> one shorter role-aware retry only if that plan is invalid or fails
  -> InterviewerAgent walks the fixed plan
       -> next / repeat / skip / stop, model-confirmed answers
       -> session-only wall-clock warnings at 30 and 34 minutes
       -> no more questions after 35 minutes; debrief, then Buddy
       -> one session-only spoken debrief after completion
  -> Buddy, the same instance that handed off
```

Two handoffs and one task, and the split is the same each time. A **handoff** is
for when responsibility genuinely changes (Buddy to supervisor, supervisor to
interviewer). A **task** is for bounded work that returns a value and gives
control back (intake).

Ownership is committed only in ``on_enter``, never in the tool that returns the
agent, because that is the first moment LiveKit has actually made the handoff
real. ``models.py`` holds the whole guarded state machine.

Layout:

- ``contracts.py``    wire constants and boundary types, importing nothing else here
- ``models.py``       state that outlives a handoff: phases, dossier, plan, cursor
- ``materials.py``    the JD transfer: revisioned overlay + bounded byte stream
- ``intake_task.py``  the setup step, as an AgentTask
- ``question_plan.py`` role-aware planning, with one shorter role-aware retry
- ``supervisor.py``   the agent that owns setup and starts the interview
- ``interviewer.py``  the agent that asks the questions

Import from this package, not from its modules, so the inside can be rearranged
without touching the worker.
"""

from .contracts import (
    ATTR_INTERVIEW_ID,
    ATTR_MATERIAL_TYPE,
    ATTR_REVISION,
    ATTR_SCHEMA_VERSION,
    INTERVIEW_MATERIAL_TOPIC,
    MATERIAL_ARRIVAL_TIMEOUT_S,
    MATERIAL_ASSEMBLY_TIMEOUT_S,
    MATERIAL_OVERLAY_SHOWN_TYPE,
    MATERIAL_REQUEST_TYPE,
    MATERIAL_SCHEMA_VERSION,
    MAX_MATERIAL_BYTES,
    START_CLAIM_TTL_S,
    BuddyFactory,
    IntakeOutcome,
    InterviewIntakeResult,
    MaterialType,
)
from .debrief import InterviewDebriefService
from .intake_task import INTAKE_INSTRUCTIONS, InterviewIntakeTask
from .interviewer import INTERVIEWER_INSTRUCTIONS, InterviewerAgent
from .materials import InterviewMaterialStore, request_material_overlay
from .models import (
    ConversationOwner,
    InterviewAnswer,
    InterviewDossier,
    InterviewPhase,
    InterviewQuestion,
    InterviewStartClaim,
    InterviewState,
    QuestionPlan,
    VoiceSessionState,
    interview_owns_conversation,
)
from .question_plan import QUESTION_COUNT, QuestionPlanService
from .supervisor import INTERVIEW_SUPERVISOR_INSTRUCTIONS, InterviewSupervisorAgent
from .time_limit import FINAL_WARNING_S, HARD_CAP_S, SOFT_WARNING_S

__all__ = [
    "ATTR_INTERVIEW_ID",
    "ATTR_MATERIAL_TYPE",
    "ATTR_REVISION",
    "ATTR_SCHEMA_VERSION",
    "INTAKE_INSTRUCTIONS",
    "INTERVIEWER_INSTRUCTIONS",
    "INTERVIEW_MATERIAL_TOPIC",
    "INTERVIEW_SUPERVISOR_INSTRUCTIONS",
    "MATERIAL_ARRIVAL_TIMEOUT_S",
    "MATERIAL_ASSEMBLY_TIMEOUT_S",
    "MATERIAL_OVERLAY_SHOWN_TYPE",
    "MATERIAL_REQUEST_TYPE",
    "MATERIAL_SCHEMA_VERSION",
    "MAX_MATERIAL_BYTES",
    "QUESTION_COUNT",
    "START_CLAIM_TTL_S",
    "BuddyFactory",
    "ConversationOwner",
    "IntakeOutcome",
    "InterviewAnswer",
    "InterviewDebriefService",
    "InterviewDossier",
    "InterviewIntakeResult",
    "InterviewIntakeTask",
    "InterviewMaterialStore",
    "InterviewPhase",
    "InterviewQuestion",
    "InterviewStartClaim",
    "InterviewState",
    "InterviewSupervisorAgent",
    "InterviewerAgent",
    "MaterialType",
    "QuestionPlan",
    "QuestionPlanService",
    "SOFT_WARNING_S",
    "FINAL_WARNING_S",
    "HARD_CAP_S",
    "VoiceSessionState",
    "interview_owns_conversation",
    "request_material_overlay",
]
