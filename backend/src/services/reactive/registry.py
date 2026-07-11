"""The Agent registry — the gate the research most challenged.

"Easy to add an agent" walks straight into the most-repeated multi-agent failure
mode: bloated, overlapping capability sets that make the orchestrator's selection
ambiguous ("if a human engineer can't say which agent to use, an agent can't
either"). So registration is gated, not just on protocol conformance:

  * NAMESPACED ids — every agent has a dotted name so the selection space stays
    legible (``curiosity.thread_followup``).
  * NON-OVERLAP on intent — two agents that own the same user-need are rejected;
    one must merge into the other. Real capability boundaries, not endpoint wrappers.
  * known event subscriptions — an agent can only subscribe to registered event
    types (a typo'd subscription fails loudly at registration).
  * mandatory eval — an agent SHOULD ship measured. The hard gate is deferred
    while the eval harness is being built (P1); for now a missing eval is a loud
    WARNING, flipped to a hard reject via ``require_evals`` once evals exist.
"""

from __future__ import annotations

from ...lib.logger import logger
from .agent import Agent
from .events import is_known_event_type


class AgentRegistry:
    def __init__(self, *, require_evals: bool = False) -> None:
        self._require_evals = require_evals
        self._agents: dict[str, Agent] = {}
        self._by_event: dict[str, list[str]] = {}

    def register(self, agent: Agent) -> None:
        if "." not in agent.name:
            raise ValueError(
                f"agent name must be namespaced (e.g. 'curiosity.thread_followup'): {agent.name!r}"
            )
        if agent.name in self._agents:
            raise ValueError(f"agent already registered: {agent.name!r}")

        # Non-overlap gate: an intent is owned by exactly one agent.
        for existing in self._agents.values():
            if existing.intent == agent.intent:
                raise ValueError(
                    f"agent {agent.name!r} overlaps {existing.name!r} on intent "
                    f"{agent.intent!r}; merge them or give it a distinct intent"
                )

        for event_type in agent.subscribes_to:
            if not is_known_event_type(event_type):
                raise ValueError(
                    f"agent {agent.name!r} subscribes to unknown event {event_type!r}"
                )

        if not agent.eval_cases():
            if self._require_evals:
                raise ValueError(f"agent {agent.name!r} registered without eval cases")
            logger.warn("registry: agent has no eval cases (eval gate not yet enforced)", {
                "agent": agent.name,
            })

        self._agents[agent.name] = agent
        for event_type in agent.subscribes_to:
            self._by_event.setdefault(event_type, []).append(agent.name)
        logger.info("registry: agent registered", {
            "agent": agent.name, "intent": agent.intent, "risk": agent.risk,
        })

    def get(self, name: str) -> Agent | None:
        return self._agents.get(name)

    def agents_for_event(self, event_type: str) -> list[Agent]:
        return [self._agents[name] for name in self._by_event.get(event_type, [])]

    def all(self) -> list[Agent]:
        return list(self._agents.values())


# ── Module-level singleton, lazily populated with the default agents ─────────
_registry: AgentRegistry | None = None


def get_agent_registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
        _register_default_agents(_registry)
    return _registry


def _register_default_agents(registry: AgentRegistry) -> None:
    """Import + register the built-in agents. Lazy (called on first registry use)
    so the agents package can import the registry types without a cycle."""
    from .agents.curiosity import CuriosityThreadFollowUpAgent
    from .agents.followup import ScheduledFollowUpAgent
    from .agents.icebreaker import IcebreakerOpenerAgent

    registry.register(CuriosityThreadFollowUpAgent())
    registry.register(IcebreakerOpenerAgent())
    registry.register(ScheduledFollowUpAgent())
