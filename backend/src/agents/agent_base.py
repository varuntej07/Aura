"""
Base class for all scheduled domain agents (sports, tech news, jobs, posts).
Each agent fetches fresh content, runs a judge LLM call, and — only if the
judge approves — sends a push notification.

Subclasses implement:
  - fetch_data(user_config)                     -> raw content items
  - build_notification(user_id, ...)            -> notification dict or None
  - agent_id property                           -> unique string identifier

The base provides:
  - load_user_config(user_id)                   -> Firestore agent_config doc
  - load_interaction_history(user_id, limit)    -> last N interactions
  - save_interaction(...)                        -> write interaction record
  - load_agent_state(user_id)                   -> dedup + daily count state
  - save_agent_state(user_id, event_fingerprint) -> persist post-notification state
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

from google.cloud import firestore as fs

from ..lib.logger import logger
from ..services.firebase import admin_firestore

_TODAY_FORMAT = "%Y-%m-%d"


class ScheduledAgent(ABC):
    """
    Abstract base for all scheduled domain agents.

    The judge pattern: build_notification returns None when the agent decides
    the content is not worth notifying the user. The orchestrator skips FCM
    when it receives None — no notification is sent.
    """

    @property
    @abstractmethod
    def agent_id(self) -> str: ...

    @abstractmethod
    async def fetch_data(self, user_config: dict[str, Any]) -> list[dict[str, Any]]:
        """Fetch fresh content. No LLM calls here — pure data retrieval."""
        ...

    @abstractmethod
    async def build_notification(
        self,
        user_id: str,
        content: list[dict[str, Any]],
        user_config: dict[str, Any],
        interaction_history: list[dict[str, Any]],
    ) -> dict[str, str] | None:
        """
        Judge the fetched content and, if worthy, produce a push notification.

        Returns a notification dict:
            {"title": str, "body": str, "opening_chat_message": str}
        or None if the judge decides nothing is worth notifying.
        """
        ...

    def _db(self) -> fs.Client:
        return admin_firestore()

    def _agent_config_ref(self, user_id: str) -> fs.DocumentReference:
        return (
            self._db()
            .collection("users")
            .document(user_id)
            .collection("agent_config")
            .document(self.agent_id)
        )

    def _interactions_ref(self, user_id: str) -> fs.CollectionReference:
        return (
            self._db()
            .collection("users")
            .document(user_id)
            .collection("agent_memory")
            .document(self.agent_id)
            .collection("interactions")
        )

    def _agent_state_ref(self, user_id: str) -> fs.DocumentReference:
        return (
            self._db()
            .collection("users")
            .document(user_id)
            .collection("agent_state")
            .document(self.agent_id)
        )

    async def load_user_config(self, user_id: str) -> dict[str, Any]:
        import asyncio
        snap = await asyncio.to_thread(lambda: self._agent_config_ref(user_id).get())
        config: dict[str, Any] = snap.to_dict() or {} if snap.exists else {}
        config.setdefault("enabled", True)
        return config

    async def load_interaction_history(
        self, user_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        import asyncio
        docs = await asyncio.to_thread(
            lambda: list(
                self._interactions_ref(user_id)
                .order_by("created_at", direction=fs.Query.DESCENDING)
                .limit(limit)
                .stream()
            )
        )
        return [d.to_dict() or {} for d in docs]

    async def save_interaction(
        self,
        user_id: str,
        nudge_id: str,
        content_source: str,
        content_topic: str,
        user_action: str,
        user_reply: str | None = None,
    ) -> None:
        import asyncio
        doc = {
            "nudge_id": nudge_id,
            "content_source": content_source,
            "content_topic": content_topic,
            "user_action": user_action,
            "user_reply": user_reply,
            "created_at": datetime.now(UTC).isoformat(),
        }
        await asyncio.to_thread(
            lambda: self._interactions_ref(user_id).add(doc)
        )
        logger.info(
            f"Agent {self.agent_id}: interaction saved",
            {"user_id": user_id, "action": user_action, "topic": content_topic},
        )

    async def load_agent_state(self, user_id: str) -> dict[str, Any]:
        """
        Returns:
            seen_today: list of event fingerprint strings notified today
            daily_count: int, how many notifications sent today
            daily_date: str, YYYY-MM-DD of the current day window
        State resets automatically when daily_date doesn't match today.
        """
        import asyncio
        today = datetime.now(UTC).strftime(_TODAY_FORMAT)

        def _read() -> dict[str, Any]:
            snap = self._agent_state_ref(user_id).get()
            if not snap.exists:
                return {"seen_today": [], "daily_count": 0, "daily_date": today}
            data = snap.to_dict() or {}
            if data.get("daily_date") != today:
                return {"seen_today": [], "daily_count": 0, "daily_date": today}
            return {
                "seen_today": data.get("seen_today", []),
                "daily_count": data.get("daily_count", 0),
                "daily_date": today,
            }

        try:
            return await asyncio.to_thread(_read)
        except Exception as exc:
            logger.error(f"Agent {self.agent_id}: load_agent_state failed", {"error": str(exc)})
            today = datetime.now(UTC).strftime(_TODAY_FORMAT)
            return {"seen_today": [], "daily_count": 0, "daily_date": today}

    async def save_agent_state(self, user_id: str, event_fingerprint: str) -> None:
        """Append event_fingerprint to seen_today and increment daily_count."""
        import asyncio
        today = datetime.now(UTC).strftime(_TODAY_FORMAT)

        def _write() -> None:
            ref = self._agent_state_ref(user_id)
            snap = ref.get()
            data = snap.to_dict() or {} if snap.exists else {}
            if data.get("daily_date") != today:
                data = {"seen_today": [], "daily_count": 0, "daily_date": today}
            seen: list[str] = data.get("seen_today", [])
            if event_fingerprint not in seen:
                seen.append(event_fingerprint)
            ref.set({
                "seen_today": seen,
                "daily_count": data.get("daily_count", 0) + 1,
                "daily_date": today,
            })

        try:
            await asyncio.to_thread(_write)
        except Exception as exc:
            logger.error(f"Agent {self.agent_id}: save_agent_state failed", {"error": str(exc)})
