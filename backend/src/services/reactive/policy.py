"""DECIDE — the deterministic policy table mapping events to agent tasks.

This is Anthropic's Routing workflow / OpenAI's manager-pattern-lite: it handles
the settled cases cheaply and is unit-testable as a truth table. In P2 the table
IS the registry's subscription map — an event wakes every agent that subscribes to
its type, deduped to one task per agent (the first/most-recent triggering event
wins). Each agent then gates itself in ``sense``/``plan`` (consent, cadence, etc.),
so dispatching it is cheap when it has nothing to say.

P5 grows this with context-conditioned rows and the LLM escape hatch (decompose /
arbitrate) for genuine ambiguity. The shape stays the same: events in, a deduped
list of ``AgentTask`` out.
"""

from __future__ import annotations

from dataclasses import dataclass

from .agent import Agent
from .events import Event
from .registry import AgentRegistry


@dataclass
class AgentTask:
    agent: Agent
    event: Event  # the event that selected this agent (the most relevant trigger)


def decide(events: list[Event], registry: AgentRegistry) -> list[AgentTask]:
    """Map a coalesced batch of events to a deduped list of agent tasks. An agent
    woken by several events in the batch runs once, against the latest such event
    (a fresher trigger is the better context)."""
    chosen: dict[str, AgentTask] = {}
    for event in events:
        for agent in registry.agents_for_event(event.type):
            existing = chosen.get(agent.name)
            if existing is None or event.ts >= existing.event.ts:
                chosen[agent.name] = AgentTask(agent=agent, event=event)
    return list(chosen.values())
