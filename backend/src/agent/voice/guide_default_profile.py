"""The default, application-neutral Guide task profile.

Guide Mode must adapt to whatever the user asks (editing a photo, filling a form,
configuring an app, learning a tool), not one hard-coded flow. This profile owns
no application entities: it trusts the planner's own ordered steps and the app the
planner identified from the user's request, and verifies each step against the
per-step predicates the planner produced. ``CapCutExampleProfile`` remains an
isolated example skill (``guide_capcut_example.py``) that a future task-profile
registry can select; it is no longer the one wired-in profile.
"""

from __future__ import annotations

import re

from .guide_kernel import GuideTaskProfile
from .guide_models import (
    GuidePlanningDecision,
    GuidePlanningStep,
    GuideStepStatus,
    GuideTask,
)

_DEFAULT_PROFILE_ID = "generic_screen_task"
_DEFAULT_PROFILE_VERSION = "generic-v1"

_PLANNING_CONTEXT = """
Plan a durable, screen-grounded guidance task for exactly what the user asked, in
whatever application they are using. Produce an ordered list of small steps, each a
single visible action the user performs. Use dependencies when a step needs an
earlier one finished first. For every step give verification_predicates: short
snake_case facts that are observable in a screenshot and prove the step is done
(for example a specific panel is open, a value is set, an export succeeded). Set
target_app to the application the user is working in. Keep steps minimal and
concrete. Never invent a control, menu, or fact Aura cannot see on screen.
""".strip()


class GenericGuideProfile(GuideTaskProfile):
    """Application-neutral profile: the planner's plan is the task."""

    profile_id = _DEFAULT_PROFILE_ID
    profile_version = _DEFAULT_PROFILE_VERSION

    def planning_context(self) -> str:
        return _PLANNING_CONTEXT

    def decision_context(self, task: GuideTask) -> str:
        step = task.current_step
        return (
            f"Target application: {task.target_app}\n"
            f"Current step: {step.model_dump_json() if step else 'none'}"
        )

    def fallback_plan(self, transcript: str) -> GuidePlanningDecision:
        # Used only when the planning provider is unavailable. The task is created
        # BLOCKED (blocked_provider) and retried on the next turn, so this shape
        # only has to be valid, not complete.
        return GuidePlanningDecision(
            goal=transcript or "Guide the user through their current task.",
            target_app="the current app",
            constraints=[],
            acceptance_criteria=["The user completes the task they described."],
            steps=[
                GuidePlanningStep(
                    step_id="get_oriented",
                    title="Get oriented",
                    dependencies=[],
                    expected_user_action="Show the screen where you want help.",
                    verification_predicates=["user_showed_relevant_screen"],
                )
            ],
            clarification_question=None,
            confidence=0.0,
        )

    def target_app_for(self, plan: GuidePlanningDecision) -> str:
        return plan.target_app

    def constraints_for(self, plan: GuidePlanningDecision) -> list[str]:
        return list(plan.constraints)

    def acceptance_criteria_for(self, plan: GuidePlanningDecision) -> list[str]:
        return list(plan.acceptance_criteria)

    def steps_for(self, plan: GuidePlanningDecision) -> list[GuidePlanningStep]:
        # The planner's own steps ARE the task. This is what makes Guide adapt to
        # any request rather than forcing a fixed per-application script.
        return list(plan.steps)

    def matches_active_app(self, active_process: str, target_app: str) -> bool:
        # Token match of the planned target app against the foreground process
        # name. Fine for native apps (Photoshop -> photoshop.exe); an app running
        # inside a browser tab will not match here (see the PAUSED_APP diagnostics
        # in guide_task_runtime for evidence to generalize this later).
        candidates = {
            value
            for value in re.split(r"[^a-z0-9]+", target_app.casefold())
            if len(value) >= 3
        }
        process = active_process.casefold()
        return bool(process and any(candidate in process for candidate in candidates))

    def completion_ready(self, task: GuideTask) -> bool:
        return bool(task.steps) and all(
            step.status == GuideStepStatus.VERIFIED for step in task.steps
        )
