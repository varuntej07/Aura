"""Provider-neutral contracts and deterministic helpers for Aura Guide tasks.

This module must not import an application profile, model provider, agent SDK,
transport, database client, or vendor response type. Those integrations adapt
to these contracts at the composition boundary.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from .guide_models import (
    GuidePlanningDecision,
    GuidePlanningStep,
    GuideTask,
    GuideVisualDecision,
)


class GuideStage(StrEnum):
    CAPTURE = "capture"
    PLANNING = "planning"
    EXECUTION = "execution"
    VERIFICATION = "verification"
    SPEECH = "speech"


class GuideTraceOutcome(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass(frozen=True)
class GuideTraceContext:
    trace_id: str
    event_id: str
    parent_event_id: str | None = None
    fields: dict[str, object] = field(default_factory=dict)

    def payload(
        self,
        *,
        stage: GuideStage,
        outcome: GuideTraceOutcome,
        reason: str | None = None,
        **extra: object,
    ) -> dict[str, object]:
        return {
            **self.fields,
            "trace_id": self.trace_id,
            "event_id": self.event_id,
            "parent_event_id": self.parent_event_id,
            "stage": stage,
            "outcome": outcome,
            "reason": reason,
            **extra,
        }


@dataclass(frozen=True)
class GuideFrameInput:
    frame_id: str
    image_bytes: bytes
    width_px: int | None
    height_px: int | None
    active_process: str
    active_window_id: str
    geometry_revision: int | None
    age_seconds: float
    metadata: dict[str, str | int | None]


@dataclass(frozen=True)
class GuideTaskShape:
    task_profile_id: str
    task_profile_version: str
    target_app: str
    constraints: list[str]
    acceptance_criteria: list[str]
    steps: list[GuidePlanningStep]


class GuideTaskProfile(Protocol):
    """Application-specific knowledge outside the provider-neutral kernel."""

    profile_id: str
    profile_version: str

    def planning_context(self) -> str: ...

    def decision_context(self, task: GuideTask) -> str: ...

    def fallback_plan(self, transcript: str) -> GuidePlanningDecision: ...

    def target_app_for(self, plan: GuidePlanningDecision) -> str: ...

    def constraints_for(self, plan: GuidePlanningDecision) -> list[str]: ...

    def acceptance_criteria_for(self, plan: GuidePlanningDecision) -> list[str]: ...

    def steps_for(self, plan: GuidePlanningDecision) -> list[GuidePlanningStep]: ...

    def matches_active_app(self, active_process: str, target_app: str) -> bool: ...

    def completion_ready(self, task: GuideTask) -> bool: ...


class GuideDecisionProvider(Protocol):
    """Typed, stateless planning and observation port."""

    provider_id: str

    async def plan(
        self,
        transcript: str,
        *,
        existing_task: GuideTask | None,
        profile: GuideTaskProfile,
        correlation: dict[str, object],
    ) -> GuidePlanningDecision: ...

    async def decide(
        self,
        task: GuideTask,
        frame: GuideFrameInput,
        transcript: str,
        *,
        profile: GuideTaskProfile,
        correlation: dict[str, object],
    ) -> GuideVisualDecision: ...


class GuideTaskRepository(Protocol):
    """Canonical Aura-owned task persistence port."""

    async def load(self, user_id: str, task_id: str) -> GuideTask | None: ...

    async def create(self, task: GuideTask) -> GuideTask: ...

    async def acquire_lease(
        self,
        user_id: str,
        task_id: str,
        lease_owner: str,
        *,
        resumed: bool,
    ) -> GuideTask: ...

    async def renew_lease(
        self,
        user_id: str,
        task_id: str,
        lease_owner: str,
    ) -> GuideTask: ...

    async def mutate(
        self,
        user_id: str,
        task_id: str,
        lease_owner: str,
        expected_revision: int,
        reducer: Callable[[GuideTask], GuideTask],
    ) -> GuideTask: ...


class GuideKernel:
    """Deterministic, provider-neutral policy owner for one task-profile family."""

    def __init__(self, profile: GuideTaskProfile) -> None:
        self.profile = profile

    def task_shape(self, plan: GuidePlanningDecision) -> GuideTaskShape:
        return GuideTaskShape(
            task_profile_id=self.profile.profile_id,
            task_profile_version=self.profile.profile_version,
            target_app=self.profile.target_app_for(plan),
            constraints=self.profile.constraints_for(plan),
            acceptance_criteria=self.profile.acceptance_criteria_for(plan),
            steps=self.profile.steps_for(plan),
        )

    def frame_authorized(
        self,
        frame: GuideFrameInput,
        task: GuideTask,
        *,
        user_id: str,
        lease_owner: str,
        guide_session_id: str,
    ) -> bool:
        return bool(
            frame.age_seconds <= 3.0
            and frame.metadata.get("guide_session_id") == guide_session_id
            and self.profile.matches_active_app(
                frame.active_process,
                task.target_app,
            )
            and task.user_id == user_id
            and task.lease_owner == lease_owner
        )

    def completion_ready(self, task: GuideTask) -> bool:
        return self.profile.completion_ready(task)

    def spoken_text(self, text: str) -> str | None:
        return safe_spoken_text(text)


_WORD_RE = re.compile(r"\b[\w'-]+\b")
_ACTION_JOIN_RE = re.compile(
    r"\b(?:then|and\s+(?:click|open|select|choose|drag|type))\b",
    re.IGNORECASE,
)


def safe_spoken_text(text: str, *, maximum_words: int = 15) -> str | None:
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return ""
    if len(_WORD_RE.findall(cleaned)) > maximum_words:
        return None
    if cleaned.count(".") + cleaned.count("!") + cleaned.count("?") > 1:
        return None
    if ";" in cleaned or _ACTION_JOIN_RE.search(cleaned):
        return None
    return cleaned


def task_projection(task: GuideTask) -> dict[str, object]:
    step = task.current_step
    return {
        "task_id": task.task_id,
        "revision": task.revision,
        "goal": task.goal,
        "target_app": task.target_app,
        "constraints": task.constraints,
        "acceptance_criteria": task.acceptance_criteria,
        "status": task.status,
        "current_step": step.model_dump(mode="json") if step else None,
        "last_instruction": (
            task.pending_instruction.model_dump(mode="json")
            if task.pending_instruction
            else None
        ),
        "verified_evidence": [
            {
                "step_id": evidence.step_id,
                "predicates": evidence.predicates,
                "sources": evidence.sources,
            }
            for evidence in task.verified_evidence[-12:]
        ],
    }
