"""
ToolExecutor — implements all tools.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from google.cloud import firestore as fs
from google.cloud.firestore_v1.base_query import FieldFilter

from ..config.settings import settings
from ..lib.logger import logger
from .firebase import admin_firestore, admin_messaging
from .google_calendar_connector import GoogleCalendarConnector

ToolResult = dict[str, Any]

TOOL_TIMEOUT_S = settings.VOICE_TOOL_TIMEOUT_S


# runs sync functions with timeout
async def _run(fn, *args, **kwargs):
    return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=TOOL_TIMEOUT_S)


class ToolExecutor:
    def __init__(self, user_id: str) -> None:
        self._user_id = user_id

    def _db(self) -> fs.Client:
        return admin_firestore()

    def _user_ref(self) -> fs.DocumentReference:
        return self._db().collection("users").document(self._user_id)

    def _reminders_ref(self) -> fs.CollectionReference:
        return self._user_ref().collection("reminders")

    def _memories_ref(self) -> fs.CollectionReference:
        return self._user_ref().collection("memories")

    def _nutrition_logs_ref(self) -> fs.CollectionReference:
        return self._user_ref().collection("nutrition_logs")

    async def execute(self, tool_name: str, input_data: dict[str, Any]) -> ToolResult:
        dispatch: dict[str, Any] = {
            "set_reminder": self._set_reminder,
            "list_reminders": self._list_reminders,
            "cancel_reminder": self._cancel_reminder,
            "create_calendar_event": self._create_calendar_event,
            "get_upcoming_events": self._get_upcoming_events,
            "store_memory": self._store_memory,
            "query_memory": self._query_memory,
            "analyze_nutrition": self._analyze_nutrition,
            "get_user_context": self._get_user_context,
            "ask_clarification": self._ask_clarification,
            "configure_agent": self._configure_agent,
            "get_agent_config": self._get_agent_config,
            "web_search": self._web_search,
        }
        handler = dispatch.get(tool_name)
        if handler is None:
            logger.warn("Tool: unknown tool requested", {"tool": tool_name, "user_id": self._user_id})
            return {"error": f"Unknown tool: {tool_name}"}

        import time as _time
        _start = _time.monotonic()
        logger.debug(f"Tool: executing {tool_name}", {
            "user_id": self._user_id,
            "input_keys": list(input_data.keys()),
        })
        try:
            result = await handler(input_data)
            _ms = int((_time.monotonic() - _start) * 1000)
            logger.info(f"Tool: {tool_name} OK", {
                "user_id": self._user_id,
                "duration_ms": _ms,
                "result_keys": list(result.keys()) if isinstance(result, dict) else "non-dict",
            })
            return result
        except Exception as exc:
            _ms = int((_time.monotonic() - _start) * 1000)
            logger.exception(f"Tool: {tool_name} FAILED", {
                "user_id": self._user_id,
                "duration_ms": _ms,
                "error": str(exc),
            })
            raise

    # Reminders
    async def _set_reminder(self, inp: dict[str, Any]) -> ToolResult:
        message = str(inp.get("message", "")).strip()
        delay_minutes = inp.get("delay_minutes")
        priority = str(inp.get("priority", "normal"))

        if not message:
            raise ValueError("message is required")
        if not isinstance(delay_minutes, (int, float)) or delay_minutes <= 0:
            raise ValueError("delay_minutes must be a positive number")

        trigger_at = (
            datetime.now(timezone.utc) + timedelta(minutes=float(delay_minutes))
        ).isoformat()

        reminder_id = str(uuid4())
        now_iso = datetime.now(timezone.utc).isoformat()

        data = {
            "id": reminder_id,
            "message": message,
            "trigger_at": trigger_at,
            "status": "pending",
            "priority": priority,
            "created_via": "voice",
            "snooze_count": 0,
            "created_at": now_iso,
        }
        ref = self._reminders_ref().document(reminder_id)
        await _run(lambda: ref.set(data))

        return {
            "reminder_id": reminder_id,
            "message": message,
            "trigger_at": trigger_at,
            "status": "pending",
            "priority": priority,
        }

    async def _list_reminders(self, inp: dict[str, Any]) -> ToolResult:
        status_filter = str(inp.get("status_filter", "pending"))

        def _fetch() -> list[dict]:
            q = self._reminders_ref().order_by("trigger_at")
            if status_filter != "all":
                q = q.where(filter=FieldFilter("status", "==", status_filter))
            return [{"reminder_id": d.id, **d.to_dict()} for d in q.stream()]

        reminders = await _run(_fetch)
        return {"reminders": reminders}

    async def _cancel_reminder(self, inp: dict[str, Any]) -> ToolResult:
        reminder_id = str(inp.get("reminder_id", "")).strip()
        if not reminder_id:
            raise ValueError("reminder_id is required")

        now_iso = datetime.now(timezone.utc).isoformat()
        ref = self._reminders_ref().document(reminder_id)
        await _run(lambda: ref.update({
            "status": "dismissed",
            "dismissed_at": now_iso,
        }))
        return {"reminder_id": reminder_id, "status": "dismissed"}

    # Calendar
    async def _create_calendar_event(self, inp: dict[str, Any]) -> ToolResult:
        title = str(inp.get("title", "")).strip()
        start_time = str(inp.get("start_time", "")).strip()
        if not title or not start_time:
            raise ValueError("title and start_time are required")

        end_time = inp.get("end_time")
        if not end_time:
            start_dt = datetime.fromisoformat(start_time)
            end_time = (start_dt + timedelta(minutes=30)).isoformat()

        body: dict[str, Any] = {
            "summary": title,
            "start": {"dateTime": start_time},
            "end": {"dateTime": end_time},
        }
        if inp.get("description"):
            body["description"] = inp["description"]
        if inp.get("location"):
            body["location"] = inp["location"]

        def _create() -> ToolResult:
            connector = GoogleCalendarConnector(self._user_id)
            status = connector.get_status()
            if not status.get("enabled"):
                return {"configured": False, "message": "Google Calendar is not configured."}
            cal = connector.calendar_client()
            event = cal.events().insert(calendarId="primary", body=body).execute()
            connector.cache_api_events([event])
            return {
                "configured": True,
                "event_id": event.get("id"),
                "html_link": event.get("htmlLink"),
                "status": event.get("status"),
            }

        return await _run(_create)

    async def _get_upcoming_events(self, inp: dict[str, Any]) -> ToolResult:
        def _fetch() -> ToolResult:
            connector = GoogleCalendarConnector(self._user_id)
            return connector.query_events(
                range_name=str(inp.get("range", "")).strip() or None,
                start_time=str(inp.get("start_time", "")).strip() or None,
                end_time=str(inp.get("end_time", "")).strip() or None,
                limit=int(inp.get("limit", 10) or 10),
                hours_ahead=int(inp.get("hours_ahead", 24) or 24),
            )

        return await _run(_fetch)

    # Memory
    async def _store_memory(self, inp: dict[str, Any]) -> ToolResult:
        key = str(inp.get("key", "")).strip()
        value = str(inp.get("value", "")).strip()
        category = str(inp.get("category", "")).strip()

        if not key or not value or not category:
            raise ValueError("key, value, and category are required")

        now_iso = datetime.now(timezone.utc).isoformat()

        def _upsert() -> str:
            existing = list(
                self._memories_ref().where(filter=FieldFilter("key", "==", key)).limit(1).stream()
            )
            if existing:
                memory_id = existing[0].id
                self._memories_ref().document(memory_id).set(
                    {"key": key, "value": value, "category": category, "updated_at": now_iso},
                    merge=True,
                )
            else:
                memory_id = str(uuid4())
                self._memories_ref().document(memory_id).set({
                    "key": key,
                    "value": value,
                    "category": category,
                    "source": "voice",
                    "created_at": now_iso,
                    "updated_at": now_iso,
                })
            return memory_id

        memory_id = await _run(_upsert)
        return {"memory_id": memory_id, "key": key, "value": value, "category": category}

    async def _query_memory(self, inp: dict[str, Any]) -> ToolResult:
        query_str = str(inp.get("query", "")).strip().lower()
        category_filter = str(inp.get("category_filter", "all"))

        if not query_str:
            raise ValueError("query is required")

        def _search() -> list[dict]:
            q = self._memories_ref()
            if category_filter != "all":
                q = q.where(filter=FieldFilter("category", "==", category_filter))
            matches: list[dict] = []
            for doc in q.stream():
                data = doc.to_dict() or {}
                haystack = f"{data.get('key', '')} {data.get('value', '')}".lower()
                if query_str in haystack:
                    matches.append({"memory_id": doc.id, **data})
                if len(matches) >= 10:
                    break
            return matches

        matches = await _run(_search)
        return {"matches": matches}

    # Nutrition
    async def _analyze_nutrition(self, inp: dict[str, Any]) -> ToolResult:
        ocr_text = str(inp.get("ocr_text", "")).strip()
        if not ocr_text:
            raise ValueError("ocr_text is required")

        quantity = float(inp.get("quantity", 1) or 1)

        def _extract(pattern: str) -> float:
            m = re.search(pattern, ocr_text, re.IGNORECASE)
            return float(m.group(1)) if m else 0.0

        calories = _extract(r"calories[^\d]*(\d+(?:\.\d+)?)")
        protein = _extract(r"protein[^\d]*(\d+(?:\.\d+)?)")
        sugar = _extract(r"sugar[^\d]*(\d+(?:\.\d+)?)")
        sodium = _extract(r"sodium[^\d]*(\d+(?:\.\d+)?)")

        concerns: list[str] = []
        if sugar * quantity >= 20:
            concerns.append("high sugar")
        if sodium * quantity >= 600:
            concerns.append("high sodium")
        if protein * quantity <= 5:
            concerns.append("low protein")

        recommendation = "moderate" if ("high sugar" in concerns or "high sodium" in concerns) else "eat"

        log_id = str(uuid4())
        now_iso = datetime.now(timezone.utc).isoformat()
        log_data = {
            "ocr_text": ocr_text,
            "occasion": inp.get("occasion"),
            "quantity": quantity,
            "is_cheat_meal": bool(inp.get("is_cheat_meal", False)),
            "analysis": {"calories": calories, "protein": protein, "sugar": sugar, "sodium": sodium},
            "concerns": concerns,
            "recommendation": recommendation,
            "timestamp": now_iso,
        }
        ref = self._nutrition_logs_ref().document(log_id)
        await _run(lambda: ref.set(log_data))

        return {
            "nutrition_log_id": log_id,
            "calories": calories,
            "protein_grams": protein,
            "sugar_grams": sugar,
            "sodium_mg": sodium,
            "quantity": quantity,
            "concerns": concerns,
            "recommendation": recommendation,
        }

    # Web search (agent-only)
    async def _web_search(self, inp: dict[str, Any]) -> ToolResult:
        from ..agents.data_fetchers.web_search import web_search
        query = str(inp.get("query", "")).strip()
        if not query:
            raise ValueError("query is required")
        result = await web_search(query, self._user_id)
        return {"result": result}

    # Clarification (chat-only — returns sentinel dict, not a Firestore call)
    async def _ask_clarification(self, inp: dict[str, Any]) -> ToolResult:
        return {
            "__clarification__": True,
            "clarification_id": str(uuid4()),
            "question": str(inp.get("question", "")).strip(),
            "options": [str(o) for o in inp.get("options", [])],
            "multi_select": bool(inp.get("multi_select", False)),
        }

    # User context
    async def _get_user_context(self, inp: dict[str, Any]) -> ToolResult:
        include_memories = bool(inp.get("include_memories", True))
        include_reminders = bool(inp.get("include_reminders", True))
        include_events = bool(inp.get("include_events", True))

        context: dict[str, Any] = {"user_id": self._user_id}

        if include_memories:
            context["memories"] = await _run(
                lambda: [{"memory_id": d.id, **d.to_dict()} for d in self._memories_ref().stream()]
            )

        if include_reminders:
            context["reminders"] = await _run(
                lambda: [
                    {"reminder_id": d.id, **d.to_dict()}
                    for d in self._reminders_ref().where(filter=FieldFilter("status", "==", "pending")).stream()
                ]
            )

        if include_events:
            result = await self._get_upcoming_events({"hours_ahead": 24})
            context["upcoming_events"] = result.get("events", [])

        return context

    # Agent configuration — lets users configure agents through chat
    async def _configure_agent(self, inp: dict[str, Any]) -> ToolResult:
        agent_id = str(inp.get("agent_id", "")).strip()
        setting = str(inp.get("setting", "")).strip()
        value = inp.get("value")
        if not agent_id or not setting:
            return {"error": "agent_id and setting are required"}
        ref = self._user_ref().collection("agent_config").document(agent_id)
        await _run(lambda: ref.set({setting: value, "updated_at": datetime.now(timezone.utc).isoformat()}, merge=True))
        return {"status": "updated", "agent_id": agent_id, "setting": setting, "value": value}

    async def _get_agent_config(self, inp: dict[str, Any]) -> ToolResult:
        agent_id = str(inp.get("agent_id", "")).strip()
        if not agent_id:
            return {"error": "agent_id is required"}
        ref = self._user_ref().collection("agent_config").document(agent_id)
        snap = await _run(lambda: ref.get())
        return snap.to_dict() if snap.exists else {"agent_id": agent_id, "config": {}}


# Standalone Firestore helpers (used by scheduler)
def fetch_due_reminders() -> list[dict[str, Any]]:
    """Query all users' pending reminders that are due now.

    Intentionally synchronous — called via asyncio.to_thread from the scheduler.
    """                 
    db = admin_firestore()
    now_iso = datetime.now(timezone.utc).isoformat()

    docs = (
        db.collection_group("reminders")
        .where(filter=FieldFilter("status", "==", "pending"))
        .where(filter=FieldFilter("trigger_at", "<=", now_iso))
        .stream()
    )

    results = []
    for doc in docs:
        parent = doc.reference.parent.parent
        if parent is None:
            logger.error("Could not resolve userId for reminder", {"doc_id": doc.id})
            continue
        results.append({"userId": parent.id, "reminderId": doc.id, "data": doc.to_dict()})
    return results


def claim_reminder_for_processing(user_id: str, reminder_id: str) -> bool:
    """Atomically claim a pending reminder for processing.

    Uses a Firestore transaction to flip status from "pending" → "processing".
    Returns True if this caller claimed it, False if another tick already did.
    Intentionally synchronous — called via asyncio.to_thread from the scheduler.
    """
    db = admin_firestore()
    ref = db.collection("users").document(user_id).collection("reminders").document(reminder_id)
    transaction = db.transaction()

    @fs.transactional
    def _claim(txn, doc_ref):
        snap = doc_ref.get(transaction=txn)
        if not snap.exists:
            return False
        if (snap.to_dict() or {}).get("status") != "pending":
            return False
        txn.update(doc_ref, {
            "status": "processing",
            "processing_at": datetime.now(timezone.utc).isoformat(),
        })
        return True

    return _claim(transaction, ref)


def mark_reminder_fired(user_id: str, reminder_id: str) -> None:
    """Intentionally synchronous — called via asyncio.to_thread from the scheduler."""
    db = admin_firestore()
    now_iso = datetime.now(timezone.utc).isoformat()
    db.collection("users").document(user_id).collection("reminders").document(reminder_id).update({
        "status": "fired",
        "fired_at": now_iso,
    })


def list_user_fcm_tokens(user_id: str) -> list[str]:
    """Return all FCM token strings for a user.

    Reads from the ``users/{uid}/fcm_tokens`` subcollection managed by
    :mod:`fcm_token_registry`.  Kept for backward compatibility with any
    callers that haven't been migrated to ``send_notification`` yet.
    """
    from .fcm_token_registry import get_user_tokens
    return [t["token"] for t in get_user_tokens(user_id)]


def log_tool_failure(tool_name: str, error: Exception) -> None:
    logger.error("Tool execution failed", {"tool": tool_name, "error": str(error)})
