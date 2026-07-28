"""Typed contracts for durable Guide tasks and stateless Guide LLM decisions."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class GuideTaskStatus(StrEnum):
    CLARIFYING = "clarifying"
    ACTIVE = "active"
    WAITING_USER = "waiting_user"
    WAITING_EXTERNAL = "waiting_external"
    VERIFYING = "verifying"
    REPLANNING = "replanning"
    PAUSED_AWAY = "paused_away"
    PAUSED_OFFLINE = "paused_offline"
    PAUSED_APP = "paused_app"
    BLOCKED = "blocked"
    COMPLETE_CANDIDATE = "complete_candidate"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class GuideStepStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    VERIFIED = "verified"
    BLOCKED = "blocked"


class GuideInstructionStatus(StrEnum):
    CLAIMED = "claimed"
    SPEECH_STARTED = "speech_started"
    DELIVERED = "delivered"
    INTERRUPTED = "interrupted"
    SUPERSEDED = "superseded"
    DELIVERY_UNKNOWN = "delivery_unknown"


class GuideDecisionKind(StrEnum):
    INSTRUCT = "instruct"
    ANSWER = "answer"
    VERIFY_CANDIDATE = "verify_candidate"
    WAIT = "wait"
    REPLAN = "replan"
    PAUSE_APP = "pause_app"
    CLARIFY = "clarify"


class GuideBounds(BaseModel):
    x: int
    y: int
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class GuideControl(BaseModel):
    control_id: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=80)
    bounds: GuideBounds


class GuideObservation(BaseModel):
    observation_id: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=500)
    visible_controls: list[GuideControl] = Field(default_factory=list, max_length=40)
    evidence: list[str] = Field(default_factory=list, max_length=8)
    expected_state_delta: str = Field(default="", max_length=240)


class GuideVisualDecision(BaseModel):
    frame_id: str = Field(min_length=1, max_length=160)
    active_window_id: str = Field(default="", max_length=80)
    geometry_revision: int = Field(ge=0)
    observation: GuideObservation
    decision_kind: GuideDecisionKind
    spoken_text: str = Field(default="", max_length=180)
    target_control_id: str | None = Field(default=None, max_length=120)
    expected_next_state: str = Field(default="", max_length=240)
    confidence: float = Field(ge=0.0, le=1.0)
    verification_predicates_met: list[str] = Field(default_factory=list, max_length=12)
    evidence_sources: list[str] = Field(default_factory=list, max_length=8)
    wait_kind: str | None = Field(default=None, min_length=1, max_length=80)


class GuidePlanningStep(BaseModel):
    step_id: str = Field(pattern=r"^[a-z0-9_]{2,80}$")
    title: str = Field(min_length=1, max_length=100)
    dependencies: list[str] = Field(default_factory=list, max_length=8)
    expected_user_action: str = Field(min_length=1, max_length=300)
    expected_duration_seconds: int = Field(default=30, ge=1, le=600)
    verification_predicates: list[str] = Field(min_length=1, max_length=10)
    critical: bool = False


class GuidePlanningDecision(BaseModel):
    goal: str = Field(min_length=1, max_length=500)
    target_app: str = Field(min_length=1, max_length=80)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=20)
    steps: list[GuidePlanningStep] = Field(min_length=1, max_length=30)
    clarification_question: str | None = Field(default=None, max_length=180)
    confidence: float = Field(ge=0.0, le=1.0)


class GuideTaskStep(GuidePlanningStep):
    status: GuideStepStatus = GuideStepStatus.PENDING
    attempt_count: int = Field(default=0, ge=0)
    last_instruction_id: str | None = None
    verified_evidence_refs: list[str] = Field(default_factory=list, max_length=12)


class GuideEvidence(BaseModel):
    evidence_id: str
    step_id: str
    frame_id: str
    observation_id: str
    predicates: list[str] = Field(default_factory=list, max_length=12)
    sources: list[str] = Field(default_factory=list, max_length=8)
    summary: str = Field(default="", max_length=500)
    recorded_at: datetime


class GuideInstruction(BaseModel):
    instruction_id: str
    task_revision: int
    step_id: str
    frame_id: str
    observation_id: str
    spoken_text: str
    target_control_id: str | None = None
    expected_state_delta: str = ""
    status: GuideInstructionStatus
    created_at: datetime
    updated_at: datetime


class GuideTask(BaseModel):
    task_id: str
    user_id: str
    schema_version: int = 1
    protocol_version: int = 2
    task_profile_id: str = "legacy"
    task_profile_version: str = "legacy"
    goal: str
    target_app: str
    constraints: list[str] = Field(default_factory=list, max_length=20)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=20)
    steps: list[GuideTaskStep] = Field(default_factory=list, max_length=30)
    current_step_id: str | None = None
    plan_revision: int = 1
    status: GuideTaskStatus = GuideTaskStatus.ACTIVE
    pause_reason: str | None = None
    blocked_reason: str | None = None
    revision: int = 1
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    last_observation_id: str | None = None
    last_frame_id: str | None = None
    last_active_app: str | None = None
    pending_instruction: GuideInstruction | None = None
    pending_instruction_status: GuideInstructionStatus | None = None
    recent_instruction_ids: list[str] = Field(default_factory=list, max_length=32)
    verified_evidence: list[GuideEvidence] = Field(default_factory=list, max_length=64)
    last_verification: dict | None = None
    planner_prompt_version: str
    guide_prompt_version: str
    model_id: str
    planning_calls: int = 0
    visual_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    resume_count: int = 0
    created_at: datetime
    updated_at: datetime
    last_resumed_at: datetime | None = None
    completed_at: datetime | None = None

    @property
    def current_step(self) -> GuideTaskStep | None:
        return next(
            (step for step in self.steps if step.step_id == self.current_step_id),
            None,
        )

    @property
    def resumable(self) -> bool:
        return self.status not in {
            GuideTaskStatus.COMPLETED,
            GuideTaskStatus.CANCELLED,
        }
