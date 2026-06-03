"""
AgentRegistry — single source of truth for all Juno engagement agents.

Usage:
    registry = get_agent_registry()
    agent = registry.get_specialist("calendar_prep")
    output = await agent.generate(engagement_context)

Adding a new agent (e.g. GymCoachAgent):
    1. Create backend/src/services/engagement/agents/gym_coach.py
       inheriting BaseAgent, implementing generate()
    2. Add one lazy property here: gym_coach → GymCoachAgent
    3. Add one entry to get_specialist(): "gym_coach" → self.gym_coach
    That's it. Nothing else changes.
"""

from __future__ import annotations

from ..model_provider import ModelProvider, get_model_provider
from .agents.base_agent import BaseAgent
from .agents.calendar_prep import CalendarPrepAgent
from .agents.habit_nudge import HabitNudgeAgent
from .agents.re_engagement import ReEngagementAgent


class AgentRegistry:
    """Lazy-initialized registry of all engagement agents.

    All agents share the same ModelProvider instance (one Anthropic + one Gemini client).
    Agents are instantiated on first access and reused — no per-request overhead.
    """

    def __init__(self, models: ModelProvider) -> None:
        self._models = models
        self._habit_nudge: HabitNudgeAgent | None = None
        self._calendar_prep: CalendarPrepAgent | None = None
        self._re_engagement: ReEngagementAgent | None = None
        # Future agents follow the same pattern — add property + _field above

    # ── Specialist agents ─────────────────────────────────────────────────────

    @property
    def habit_nudge(self) -> HabitNudgeAgent:
        if self._habit_nudge is None:
            self._habit_nudge = HabitNudgeAgent(self._models)
        return self._habit_nudge

    @property
    def calendar_prep(self) -> CalendarPrepAgent:
        if self._calendar_prep is None:
            self._calendar_prep = CalendarPrepAgent(self._models)
        return self._calendar_prep

    @property
    def re_engagement(self) -> ReEngagementAgent:
        if self._re_engagement is None:
            self._re_engagement = ReEngagementAgent(self._models)
        return self._re_engagement

    # ── Dispatch ──────────────────────────────────────────────────────────────

    def get_specialist(self, agent_type: str) -> BaseAgent:
        """Return the agent for a given type string.

        Raises ValueError for unknown agent types so callers fail loudly
        rather than silently sending nothing.
        """
        mapping: dict[str, BaseAgent] = {
            "habit_nudge":        self.habit_nudge,
            "calendar_prep":      self.calendar_prep,
            # re_engagement is invoked directly — not via get_specialist
        }
        agent = mapping.get(agent_type)
        if agent is None:
            raise ValueError(
                f"AgentRegistry: unknown agent type '{agent_type}'. "
                f"Known types: {list(mapping)}"
            )
        return agent


# ── Module-level singleton ────────────────────────────────────────────────────

_registry: AgentRegistry | None = None


def get_agent_registry() -> AgentRegistry:
    """Return the shared AgentRegistry singleton."""
    global _registry
    if _registry is None:
        _registry = AgentRegistry(get_model_provider())
    return _registry
