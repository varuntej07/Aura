"""
ScheduledAgentRegistry — lazy singleton registry for all domain agents.

Adding a new agent (e.g. WeatherAgent):
    1. Create backend/src/agents/implementations/weather_agent.py
    2. Add one lazy property here: weather → WeatherAgent
    3. Add one entry to get_agent() mapping and all_agent_ids list
    That's it. Nothing else changes.
"""

from __future__ import annotations

from ..services.model_provider import ModelProvider, get_model_provider
from .agent_base import ScheduledAgent
from .implementations.jobs_agent import JobsAgent
from .implementations.posts_agent import PostsAgent
from .implementations.sports_agent import SportsAgent
from .implementations.technews_agent import TechNewsAgent


class ScheduledAgentRegistry:
    """Lazy-initialized registry of all scheduled domain agents.

    All agents share one ModelProvider instance. Agents are instantiated
    on first access and reused for the lifetime of the process.
    """

    def __init__(self, models: ModelProvider) -> None:
        self._models = models
        self._sports: SportsAgent | None = None
        self._technews: TechNewsAgent | None = None
        self._jobs: JobsAgent | None = None
        self._posts: PostsAgent | None = None

    @property
    def sports(self) -> SportsAgent:
        if self._sports is None:
            self._sports = SportsAgent(self._models)
        return self._sports

    @property
    def technews(self) -> TechNewsAgent:
        if self._technews is None:
            self._technews = TechNewsAgent(self._models)
        return self._technews

    @property
    def jobs(self) -> JobsAgent:
        if self._jobs is None:
            self._jobs = JobsAgent(self._models)
        return self._jobs

    @property
    def posts(self) -> PostsAgent:
        if self._posts is None:
            self._posts = PostsAgent(self._models)
        return self._posts

    def get_agent(self, agent_id: str) -> ScheduledAgent:
        """Return the agent for a given agent_id string.

        Raises ValueError for unknown IDs so callers fail loudly.
        """
        mapping: dict[str, ScheduledAgent] = {
            "sports":   self.sports,
            "technews": self.technews,
            "jobs":     self.jobs,
            "posts":    self.posts,
        }
        agent = mapping.get(agent_id)
        if agent is None:
            raise ValueError(
                f"ScheduledAgentRegistry: unknown agent '{agent_id}'. "
                f"Known agents: {list(mapping)}"
            )
        return agent

    @property
    def all_agent_ids(self) -> list[str]:
        return ["sports", "technews", "jobs", "posts"]


# ── Module-level singleton ────────────────────────────────────────────────────

_registry: ScheduledAgentRegistry | None = None


def get_scheduled_agent_registry() -> ScheduledAgentRegistry:
    """Return the shared ScheduledAgentRegistry singleton."""
    global _registry
    if _registry is None:
        _registry = ScheduledAgentRegistry(get_model_provider())
    return _registry
