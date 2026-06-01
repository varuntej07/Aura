"""
BaseAgent — abstract interface all engagement agents must implement.

Each agent:
  - Receives a curated engagement_context dict (from DecisionEngine)
  - Calls ModelProvider with a specialized prompt
  - Returns NotificationOutput

Adding a new agent:
  1. Create a subclass here (or in a new file in this package)
  2. Implement generate()
  3. Add one property to AgentRegistry + one entry in get_specialist()
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseAgent(ABC):
    def __init__(self, models: object) -> None:
        # models is ModelProvider — typed as object here to avoid circular imports.
        # Subclasses cast or annotate it directly after importing ModelProvider.
        self._models = models

    @abstractmethod
    async def generate(self, context: dict) -> object:
        """Generate notification copy for the given engagement context.

        Args:
            context: EngagementDecision.engagement_context — curated facts
                     about the event (event title, tone, etc.)

        Returns:
            NotificationOutput with title, body, opening_chat_message, suggested_replies
        """
        ...
