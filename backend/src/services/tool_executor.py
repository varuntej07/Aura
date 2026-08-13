"""
ToolExecutor — implements all tools.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from typing import Any
from uuid import uuid4

from google.cloud import firestore as fs
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel

from ..config.settings import settings
from ..lib.logger import logger
from ..prompts import REASON_STEP_SYSTEM_PROMPT, reason_step_user_prompt
from ..shared.tools import (
    MEMORY_CATEGORIES,
    claude_tool_definitions,
    validate_and_coerce_tool_input,
)
from .analytics.llm_telemetry import start_tool_span
from .calendar_time import parse_calendar_when
from .chat_completion import tool_idempotency as _tool_idempotency
from .firebase import admin_firestore
from .gmail_connector import GmailConnector
from .google_calendar_connector import GoogleCalendarConnector
from .model_provider import _strip_fences, get_model_provider
from .reminder_time import parse_reminder_when

ToolResult = dict[str, Any]

TOOL_TIMEOUT_S = settings.CHAT_TOOL_TIMEOUT_S
REASON_STEP_TIMEOUT_S = max(
    TOOL_TIMEOUT_S,
    min(40.0, TOOL_TIMEOUT_S * settings.REASON_STEP_MAX_TURNS),
)

DESKTOP_CHAT_ALLOWED_TOOLS: frozenset[str] = frozenset({
    "set_reminder",
    "list_reminders",
    "get_upcoming_events",
    "query_memory",
    "get_user_context",
    "web_surf",
    "list_emails",
    "read_email",
    "ask_clarification",
    "list_trackers",
    "reason_step",
    })
def resolve_chat_surface_allowed_tools(surface: str) -> frozenset[str] | None:
    """Return None for the unrestricted app surface, otherwise fail closed."""
    if surface == "app":
        return None
    return DESKTOP_CHAT_ALLOWED_TOOLS


def excluded_tools_for_chat_surface(surface: str) -> frozenset[str]:
    allowed_tools = resolve_chat_surface_allowed_tools(surface)
    if allowed_tools is None:
        return frozenset()
    return frozenset(
        tool["name"]
        for tool in claude_tool_definitions()
        if tool["name"] not in allowed_tools
    )


def _canonical_args_hash(args: dict[str, Any]) -> str:
    blob = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _normalize_tool_result(result: ToolResult) -> ToolResult:
    """Add the common result contract without breaking existing result fields."""
    normalized = dict(result)
    approval_required = normalized.get("approval_required") is True
    failed = bool(normalized.get("error")) or normalized.get("configured") is False
    normalized.setdefault("ok", not failed and not approval_required)
    if not failed and not approval_required:
        return normalized
    normalized.setdefault(
        "code",
        (
            "approval_required"
            if approval_required
            else "tool_error"
            if failed
            else "tool_error"
        ),
    )
    normalized.setdefault("retryable", False)
    if "user_message" not in normalized and isinstance(normalized.get("message"), str):
        normalized["user_message"] = normalized["message"]
    return normalized

# Two reminders whose fire times fall within this window are "the same occasion".
# A new reminder that duplicates an existing pending one for the same occasion is
# suppressed. Wider than a double-tap because the model re-times a paraphrase
# (observed up to ~1h apart in real data), but far short of an intentional re-set
# hours or days later, which stays a separate reminder.
REMINDER_SIMILAR_TRIGGER_WINDOW = timedelta(hours=3)

# Cosine threshold (gemini-embedding-001, 768-dim) above which two reminder texts
# for the same occasion are treated as the same task. Conservative on purpose: a
# batch brain-dump puts several DISTINCT tasks at one fire time and those must
# survive, so only clear paraphrases merge. NOTE: set from judgment, NOT yet
# empirically calibrated (the embedding key was over its spend cap when this
# shipped). Re-run scratchpad/calibrate_threshold.py on real pairs to tune it.
REMINDER_SIMILARITY_THRESHOLD = 0.90


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _within_trigger_window(existing_trigger_at: Any, new_trigger_at: datetime) -> bool:
    """True if an existing reminder fires close enough to a new one to be the same
    occasion. An unparseable stored value never matches (it cannot be compared)."""
    if not isinstance(existing_trigger_at, str) or not existing_trigger_at:
        return False
    try:
        existing_dt = datetime.fromisoformat(existing_trigger_at)
    except ValueError:
        return False
    if existing_dt.tzinfo is None:
        existing_dt = existing_dt.replace(tzinfo=UTC)
    return abs(existing_dt - new_trigger_at) <= REMINDER_SIMILAR_TRIGGER_WINDOW


class _ReasonStep(BaseModel):
    action: str = "present"
    confidence: float = 0.0
    question: str = ""
    options: list[str] = []
    findings: str = ""
    next_question: str = ""
    answer: str = ""


_REASON_STEP_TOOLS = [t for t in claude_tool_definitions() if t["name"] == "web_surf"]


def _build_reason_seed(inp: dict[str, Any]) -> str:
    task = str(inp.get("task", "")).strip()
    context = str(inp.get("known_context", "")).strip()
    return reason_step_user_prompt(task=task, known_context=context)


def _format_fetch_result(out: ToolResult) -> str:
    """Turn a web result into text the stepper can cite."""
    if out.get("error"):
        return str(out.get("user_message") or "Search unavailable right now.")

    result_text = str(out.get("text") or "").strip()
    sources = out.get("sources") or []
    lines = [
        f"- {source.get('title') or source.get('url')} — {source.get('url')}"
        for source in sources
        if isinstance(source, dict) and source.get("url")
    ]
    if lines and result_text:
        return result_text + "\n\nSources:\n" + "\n".join(lines)
    if lines:
        return "Sources:\n" + "\n".join(lines)
    return result_text or "No useful results."


def _parse_reason_step(raw: str) -> _ReasonStep:
    cleaned = _strip_fences(raw)
    try:
        return _ReasonStep.model_validate_json(cleaned)
    except Exception:
        return _ReasonStep(action="present", findings=raw)


def _reason_step_to_result(step: _ReasonStep) -> ToolResult:
    action = step.action
    if (
        action == "final"
        and step.confidence
        and step.confidence < settings.REASON_STEP_CONFIDENCE_FLOOR
    ):
        action = "clarify"

    if action == "clarify":
        return {
            "needs_clarification": True,
            "instruction": "Before answering, call ask_clarification with this question and options.",
            "question": step.question or step.next_question,
            "options": step.options,
        }

    if action == "present":
        out: ToolResult = {"findings": step.findings}
        if step.next_question:
            out["next_question"] = step.next_question
            out["options"] = step.options
            out["instruction"] = (
                "Relay the findings, then call ask_clarification with next_question and options."
            )
        else:
            out["instruction"] = (
                "Relay these concrete findings to the user, then ask what they'd like next."
            )
        return out
    return {"reasoned_answer": step.answer or step.findings}


# runs sync functions with timeout
async def _run(fn, *args, **kwargs):
    return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=TOOL_TIMEOUT_S)


async def _get_user_timezone(uid: str) -> str:
    """Read the IANA timezone written to users/{uid} by the client at sign-in."""
    def _fetch() -> str | None:
        snap = admin_firestore().collection("users").document(uid).get()
        data = snap.to_dict() or {}
        value = data.get("timezone")
        return value.strip() if isinstance(value, str) else None

    try:
        timezone_name = await asyncio.to_thread(_fetch)
    except Exception as exc:
        raise ValueError(
            "I couldn't read your timezone right now. Try again in a moment."
        ) from exc
    if not timezone_name:
        raise ValueError(
            "I need your current timezone before I can set this safely. "
            "Refresh your device timezone, then try again."
        )
    return timezone_name


_ATTENDEE_EMAIL = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.IGNORECASE,
)


def _normalize_attendee_emails(raw: Any) -> list[str]:
    """Validate and case-insensitively deduplicate attendee email addresses."""
    if raw == []:
        return []
    if not isinstance(raw, list):
        raise ValueError("attendees must be an array of email addresses")
    seen: set[str] = set()
    emails: list[str] = []
    for candidate in raw:
        if not isinstance(candidate, str):
            raise ValueError("Every attendee must be a valid email address.")
        email = candidate.strip()
        if not _ATTENDEE_EMAIL.fullmatch(email):
            raise ValueError(
                f"'{email or candidate}' isn't a valid attendee email address. "
                "Check it and try again."
            )
        key = email.casefold()
        if key in seen:
            continue
        seen.add(key)
        emails.append(key)
    return emails


class ToolExecutor:
    def __init__(
        self,
        user_id: str,
        created_via: str = "text",
        client_message_id: str = "",
        session_id: str = "",
        allowed_tools: frozenset[str] | None = None,
    ) -> None:
        self._user_id = user_id
        self._created_via = created_via     # How reminders created in this session are tagged
        self._client_message_id = client_message_id
        self._session_id = session_id
        self._allowed_tools = allowed_tools

    @property
    def user_id(self) -> str:
        return self._user_id

    def _db(self) -> fs.Client:
        return admin_firestore()

    def _user_ref(self) -> fs.DocumentReference:
        return self._db().collection("users").document(self._user_id)

    def _reminders_ref(self) -> fs.CollectionReference:
        return self._user_ref().collection("reminders")

    def _memories_ref(self) -> fs.CollectionReference:
        return self._user_ref().collection("memories")

    async def execute(self, tool_name: str, input_data: dict[str, Any]) -> ToolResult:
        if self._allowed_tools is not None and tool_name not in self._allowed_tools:
            logger.warn(
                "Tool: blocked by surface policy",
                {"tool": tool_name, "user_id": self._user_id},
            )
            return {
                "ok": False,
                "error": True,
                "code": "tool_not_allowed",
                "retryable": False,
                "user_message": f"Tool '{tool_name}' is not allowed for this chat surface.",
            }
        if not isinstance(input_data, dict):
            return {
                "ok": False,
                "error": True,
                "code": "validation_error",
                "retryable": False,
                "user_message": "Tool input must be an object.",
            }
        try:
            validate_and_coerce_tool_input(tool_name, input_data)
        except ValueError as exc:
            return {
                "ok": False,
                "error": True,
                "code": "validation_error",
                "retryable": False,
                "user_message": str(exc),
            }
        dispatch: dict[str, Any] = {
            "set_reminder": self._set_reminder,
            "list_reminders": self._list_reminders,
            "cancel_reminder": self._cancel_reminder,
            "track_topic": self._track_topic,
            "list_trackers": self._list_trackers,
            "cancel_tracker": self._cancel_tracker,
            "create_calendar_event": self._create_calendar_event,
            "update_calendar_event": self._update_calendar_event,
            "get_upcoming_events": self._get_upcoming_events,
            "list_emails": self._list_emails,
            "read_email": self._read_email,
            "send_email": self._send_email,
            "store_memory": self._store_memory,
            "delete_memory": self._delete_memory,
            "query_memory": self._query_memory,
            "get_user_context": self._get_user_context,
            "ask_clarification": self._ask_clarification,
            "configure_agent": self._configure_agent,
            "get_agent_config": self._get_agent_config,
            "web_surf": self._web_surf,
            "reason_step": self._reason_step,
            "report_feedback": self._report_feedback,
        }
        handler = dispatch.get(tool_name)
        if handler is None:
            logger.warn("Tool: unknown tool requested", {"tool": tool_name, "user_id": self._user_id})
            return {
                "ok": False,
                "error": True,
                "code": "unknown_tool",
                "retryable": False,
                "user_message": f"Unknown tool: {tool_name}",
            }

        import time as _time
        _start = _time.monotonic()
        logger.debug(f"Tool: executing {tool_name}", {
            "user_id": self._user_id,
            "input_keys": list(input_data.keys()),
        })
        # One telemetry span per tool call (ops dashboard tool analytics). The
        # span object never raises; finish() is idempotent.
        span = start_tool_span(tool_name=tool_name, source=self._created_via, uid=self._user_id)
        try:
            async def _execute_guarded() -> ToolResult:
                async def _normalized_handler(arguments: dict[str, Any]) -> ToolResult:
                    raw_result = await handler(arguments)
                    if not isinstance(raw_result, dict):
                        raise TypeError("Tool handlers must return an object")
                    return _normalize_tool_result(raw_result)

                if self._client_message_id and tool_name in _tool_idempotency.SIDE_EFFECTING_TOOLS:
                    return await _tool_idempotency.run_idempotent(
                        self._user_id,
                        self._client_message_id,
                        tool_name,
                        input_data,
                        _normalized_handler,
                    )
                return await _normalized_handler(input_data)

            timeout_s = REASON_STEP_TIMEOUT_S if tool_name == "reason_step" else TOOL_TIMEOUT_S
            result = await asyncio.wait_for(_execute_guarded(), timeout=timeout_s)
            _ms = int((_time.monotonic() - _start) * 1000)
            logger.info(f"Tool: {tool_name} OK", {
                "user_id": self._user_id,
                "duration_ms": _ms,
                "result_keys": list(result.keys()) if isinstance(result, dict) else "non-dict",
            })
            soft_error = isinstance(result, dict) and (
                bool(result.get("error"))
                or result.get("ok") is False
                or result.get("configured") is False
            )
            span.finish(success=not soft_error, error_type="soft_error" if soft_error else None)
            return result
        except TimeoutError:
            _ms = int((_time.monotonic() - _start) * 1000)
            logger.warn(f"Tool: {tool_name} timed out", {
                "user_id": self._user_id,
                "duration_ms": _ms,
            })
            span.finish(success=False, error_type="TimeoutError")
            return {
                "ok": False,
                "error": True,
                "code": "timeout",
                "retryable": tool_name not in _tool_idempotency.SIDE_EFFECTING_TOOLS,
                "user_message": (
                    "That action's outcome is unclear, so I won't repeat it automatically."
                    if tool_name in _tool_idempotency.SIDE_EFFECTING_TOOLS
                    else "That took too long. Try again in a moment."
                ),
            }
        except ValueError as exc:
            _ms = int((_time.monotonic() - _start) * 1000)
            logger.warn(f"Tool: {tool_name} validation error", {
                "user_id": self._user_id,
                "duration_ms": _ms,
                "error": str(exc),
            })
            span.finish(success=False, error_type="ValueError")
            return {
                "ok": False,
                "error": True,
                "code": "validation_error",
                "retryable": False,
                "user_message": str(exc),
            }
        except Exception as exc:
            _ms = int((_time.monotonic() - _start) * 1000)
            logger.exception(f"Tool: {tool_name} FAILED", {
                "user_id": self._user_id,
                "duration_ms": _ms,
                "error": str(exc),
            })
            span.finish(success=False, error_type=type(exc).__name__)
            return {
                "ok": False,
                "error": True,
                "code": "internal_error",
                "retryable": tool_name not in _tool_idempotency.SIDE_EFFECTING_TOOLS,
                "user_message": "Something went wrong. Try again in a bit.",
            }

    # Reminders
    async def _set_reminder(self, inp: dict[str, Any]) -> ToolResult:
        message = str(inp.get("message", "")).strip()
        when = str(inp.get("when", "")).strip()

        if not message:
            raise ValueError("message is required")
        if not when:
            raise ValueError("when is required")
        if len(message) > 500:
            raise ValueError("message must be 500 characters or fewer")

        timezone_name = await _get_user_timezone(self._user_id)
        parsed_time = parse_reminder_when(when, timezone_name)
        trigger_at_dt = parsed_time.utc
        trigger_at = trigger_at_dt.isoformat()

        # Idempotency guard against the model creating the SAME task twice: a
        # replayed turn (editAndResend re-runs every tool), a double tool-call, or
        # the user restating it. Both an exact re-create AND a re-worded one ("DM
        # Vish Jaggi" vs "DM Vishal") for a nearby fire time collapse to one
        # reminder, while a batch of DISTINCT tasks at one time is preserved.
        duplicate = await self._find_duplicate_reminder(message, trigger_at_dt)
        if duplicate is not None:
            logger.info("ToolExecutor: duplicate reminder suppressed", {
                "user_id": self._user_id,
                "reminder_id": duplicate["reminder_id"],
                "trigger_at": trigger_at,
            })
            return {
                "reminder_id": duplicate["reminder_id"],
                "message": duplicate.get("message", message),
                "trigger_at": duplicate.get("trigger_at", trigger_at),
                "status": "pending",
                "timezone": parsed_time.timezone,
            }

        reminder_id = str(uuid4())
        now_iso = datetime.now(UTC).isoformat()

        data = {
            "id": reminder_id,
            "message": message,
            "trigger_at": trigger_at,
            "status": "pending",
            "created_via": self._created_via,
            "snooze_count": 0,
            "created_at": now_iso,
        }
        if self._session_id:
            # Stamps the reminder with its originating session so the evaluator can
            # drop that topic: an explicit reminder is already a promise to ping.
            data["session_id"] = self._session_id
        await _run(lambda: self._reminders_ref().document(reminder_id).set(data))

        # Open a curiosity thread for this reminder so Buddy can later ask what
        # it is about (not whether it was done). Fire-and-forget and a no-op
        # while the engine is disabled, so the tool path is never affected.
        from .threads.thread_writer import record_reminder_thread

        asyncio.create_task(record_reminder_thread(
            self._user_id,
            reminder_id=reminder_id,
            message=message,
            trigger_at_iso=trigger_at,
        ))

        # Mirror the creation into PostHog so the product dashboard can count how
        # many reminders users actually set. Fire-and-forget: capture_event never
        # raises, and the task is detached so it cannot slow the tool response.
        from .analytics.posthog_client import capture_event

        asyncio.create_task(capture_event(
            distinct_id=self._user_id,
            event="reminder_created",
            properties={},
        ))

        return {
            "reminder_id": reminder_id,
            "message": message,
            "trigger_at": trigger_at,
            "status": "pending",
            "timezone": parsed_time.timezone,
        }

    async def _find_duplicate_reminder(
        self, message: str, trigger_at_dt: datetime
    ) -> dict[str, Any] | None:
        """Find a pending reminder this new one duplicates, cheapest layer first.

        1. Exact (casefolded) message at a nearby fire time — a pure double
           create. No embedding call.
        2. A semantically near-identical message at a nearby fire time — the model
           re-worded the same task. One batched embedding call, conservative
           threshold so a batch of DISTINCT tasks at one time is never merged.

        Only pending reminders within ``REMINDER_SIMILAR_TRIGGER_WINDOW`` of the
        new fire time are candidates, so an intentional re-set hours or days later
        is left alone. Fail-open: any embedding error logs and returns ``None`` so
        a flaky embed API can never block a user from setting a reminder.
        """
        def _pending() -> list[dict[str, Any]]:
            return [
                {"reminder_id": d.id, **(d.to_dict() or {})}
                for d in self._reminders_ref()
                .where(filter=FieldFilter("status", "==", "pending"))
                .stream()
            ]

        candidates = [
            c
            for c in await _run(_pending)
            if _within_trigger_window(c.get("trigger_at"), trigger_at_dt)
        ]
        if not candidates:
            return None

        # Layer 1: exact text for the same occasion.
        message_normalized = message.strip().casefold()
        for candidate in candidates:
            if str(candidate.get("message", "")).strip().casefold() == message_normalized:
                return candidate

        # Layer 2: semantic near-duplicate.
        try:
            from .signal_engine.embedder import embed_texts

            texts = [message] + [str(c.get("message", "")) for c in candidates]
            vectors = await embed_texts(texts)
            new_vector = vectors[0]
            best: tuple[float, dict[str, Any]] | None = None
            for candidate, vector in zip(candidates, vectors[1:]):
                score = _cosine(new_vector, vector)
                if score >= REMINDER_SIMILARITY_THRESHOLD and (best is None or score > best[0]):
                    best = (score, candidate)
            if best is not None:
                logger.info("ToolExecutor: semantic duplicate reminder suppressed", {
                    "user_id": self._user_id,
                    "reminder_id": best[1]["reminder_id"],
                    "similarity": round(best[0], 4),
                })
                return best[1]
        except Exception as exc:
            logger.warn("ToolExecutor: reminder semantic dedup failed; creating anyway", {
                "user_id": self._user_id,
                "error": str(exc),
                "error_type": type(exc).__name__,
            })
        return None

    async def _list_reminders(self, inp: dict[str, Any]) -> ToolResult:
        status_filter = str(inp.get("status_filter", "pending"))
        if status_filter not in {"pending", "fired", "dismissed", "all"}:
            raise ValueError("status_filter must be pending, fired, dismissed, or all")

        def _fetch() -> list[dict]:
            q = self._reminders_ref().order_by("trigger_at")
            if status_filter != "all":
                q = q.where(filter=FieldFilter("status", "==", status_filter))
            return [
                {"reminder_id": d.id, **d.to_dict()}
                for d in q.limit(100).stream()
            ]

        reminders = await _run(_fetch)
        return {"reminders": reminders}

    async def _cancel_reminder(self, inp: dict[str, Any]) -> ToolResult:
        reminder_id = str(inp.get("reminder_id", "")).strip()
        if not reminder_id:
            raise ValueError("reminder_id is required")

        now_iso = datetime.now(UTC).isoformat()
        ref = self._reminders_ref().document(reminder_id)
        await _run(lambda: ref.update({
            "status": "dismissed",
            "dismissed_at": now_iso,
        }))
        return {"reminder_id": reminder_id, "status": "dismissed"}

    # Topic tracking (live-update subscriptions)
    async def _track_topic(self, inp: dict[str, Any]) -> ToolResult:
        request = str(inp.get("request", "")).strip()
        if not request:
            raise ValueError("request is required")
        from .tracking.tracking_engine import provision_tracker
        return await provision_tracker(self._user_id, request, created_via=self._created_via)

    async def _list_trackers(self, inp: dict[str, Any]) -> ToolResult:
        from .tracking import fields as tf
        from .tracking import tracking_store as store

        trackers = await store.list_trackers_for_user(self._user_id)
        active = [t for t in trackers if t.status == tf.TRACKER_STATUS_ACTIVE]
        # Resolve each topic's human title for display (few per user).
        out: list[dict[str, Any]] = []
        for t in active:
            topic = await store.get_tracked_topic(t.topic_key)
            out.append({
                "tracker_id": t.id,
                "topic": topic.title if topic else t.topic_key,
                "status": t.status,
            })
        return {"trackers": out}

    async def _cancel_tracker(self, inp: dict[str, Any]) -> ToolResult:
        tracker_id = str(inp.get("tracker_id", "")).strip()
        if not tracker_id:
            raise ValueError("tracker_id is required")
        from .tracking import fields as tf
        from .tracking import tracking_store as store

        tracker = await store.get_tracker(tracker_id)
        if tracker is None or tracker.user_id != self._user_id:
            return {"error": True, "user_message": "I couldn't find that tracker."}
        await store.set_tracker_status(tracker_id, tf.TRACKER_STATUS_CANCELLED)
        if tracker.status == tf.TRACKER_STATUS_ACTIVE:
            await store.adjust_subscriber_count(tracker.topic_key, -1)
        return {"tracker_id": tracker_id, "status": tf.TRACKER_STATUS_CANCELLED}

    # Calendar
    async def _create_calendar_event(self, inp: dict[str, Any]) -> ToolResult:
        title = str(inp.get("title", "")).strip()
        when = str(inp.get("when", "")).strip()
        if not title or not when:
            raise ValueError("title and when are required")
        if len(title) > 500:
            raise ValueError("title must be 500 characters or fewer")

        invitees = _normalize_attendee_emails(inp.get("attendees"))
        timezone_name = await _get_user_timezone(self._user_id)
        parsed_time = parse_calendar_when(when, timezone_name)
        start_time = parsed_time.start_utc.isoformat()
        end_time = parsed_time.end_utc.isoformat()
        description = str(inp.get("description", "")).strip()
        location = str(inp.get("location", "")).strip()
        canonical_args = {
            "title": title,
            "start_time": start_time,
            "end_time": end_time,
            "timezone": parsed_time.timezone,
            "description": description,
            "location": location,
            "attendees": invitees,
        }
        connector = GoogleCalendarConnector(self._user_id)
        status = await _run(connector.get_status)
        if not status.get("enabled"):
            return {
                "ok": False,
                "error": True,
                "code": "not_configured",
                "configured": False,
                "retryable": False,
                "user_message": (
                    "Google Calendar isn't connected yet. Connect it in "
                    "Settings > Connectors, then ask me again."
                ),
            }

        action_identity = self._client_message_id or self._session_id
        event_id = ""
        if action_identity:
            operation_seed = (
                f"{self._user_id}\n{action_identity}\n"
                f"{_canonical_args_hash(canonical_args)}"
            )
            event_id = base64.b32hexencode(
                hashlib.sha256(operation_seed.encode("utf-8")).digest()
            ).decode("ascii").rstrip("=").lower()

        def _create() -> ToolResult:
            start_block: dict[str, Any] = {
                "dateTime": start_time,
                "timeZone": parsed_time.timezone,
            }
            end_block: dict[str, Any] = {
                "dateTime": end_time,
                "timeZone": parsed_time.timezone,
            }

            body: dict[str, Any] = {
                "summary": title,
                "start": start_block,
                "end": end_block,
            }
            if event_id:
                body["id"] = event_id
            if description:
                body["description"] = description
            if location:
                body["location"] = location
            if invitees:
                body["attendees"] = [{"email": email} for email in invitees]

            cal = connector.calendar_client()
            try:
                insert_args: dict[str, Any] = {
                    "calendarId": "primary",
                    "body": body,
                }
                if invitees:
                    insert_args["sendUpdates"] = "all"
                event = cal.events().insert(**insert_args).execute()
            except Exception as exc:
                # A deterministic event id makes a provider timeout safely
                # reconcilable without creating a second event.
                if not event_id:
                    raise
                try:
                    event = cal.events().get(calendarId="primary", eventId=event_id).execute()
                except Exception:
                    raise exc
            connector.cache_api_events([event])
            return {
                "configured": True,
                "event_id": event.get("id"),
                "html_link": event.get("htmlLink"),
                "status": event.get("status"),
                "invited_count": len(invitees),
                "timezone": parsed_time.timezone,
                "start_local": parsed_time.start_local.isoformat(),
                "end_local": parsed_time.end_local.isoformat(),
                "start_utc": start_time,
                "end_utc": end_time,
            }

        return {"ok": True, **(await _run(_create))}

    async def _update_calendar_event(self, inp: dict[str, Any]) -> ToolResult:
        """Patch one existing event. Empty fields mean 'leave this alone'.

        This exists because without it the only way to act on "and invite sarah@..."
        is a second create_calendar_event, which silently duplicates the event on the
        user's real calendar. Attendees are merged rather than replaced for the same
        reason: the natural phrasing is additive ("invite Sarah too"), and treating
        it as a replacement would quietly uninvite everyone already on the event.
        """
        event_id = str(inp.get("event_id", "")).strip()
        if not event_id:
            raise ValueError("event_id is required")

        title = str(inp.get("title", "")).strip()
        when = str(inp.get("when", "")).strip()
        description = str(inp.get("description", "")).strip()
        location = str(inp.get("location", "")).strip()
        added_invitees = _normalize_attendee_emails(inp.get("attendees"))
        if len(title) > 500:
            raise ValueError("title must be 500 characters or fewer")
        if not any((title, when, description, location, added_invitees)):
            raise ValueError("Tell me what to change about the event.")

        parsed_time = None
        if when:
            timezone_name = await _get_user_timezone(self._user_id)
            parsed_time = parse_calendar_when(when, timezone_name)

        connector = GoogleCalendarConnector(self._user_id)
        status = await _run(connector.get_status)
        if not status.get("enabled"):
            return {
                "ok": False,
                "error": True,
                "code": "not_configured",
                "configured": False,
                "retryable": False,
                "user_message": (
                    "Google Calendar isn't connected yet. Connect it in "
                    "Settings > Connectors, then ask me again."
                ),
            }

        def _patch() -> ToolResult:
            cal = connector.calendar_client()
            existing = cal.events().get(calendarId="primary", eventId=event_id).execute()

            body: dict[str, Any] = {}
            if title:
                body["summary"] = title
            if description:
                body["description"] = description
            if location:
                body["location"] = location
            if parsed_time is not None:
                body["start"] = {
                    "dateTime": parsed_time.start_utc.isoformat(),
                    "timeZone": parsed_time.timezone,
                }
                body["end"] = {
                    "dateTime": parsed_time.end_utc.isoformat(),
                    "timeZone": parsed_time.timezone,
                }

            merged_emails: list[str] = []
            if added_invitees:
                existing_attendees = existing.get("attendees") or []
                seen = set()
                merged: list[dict[str, Any]] = []
                for attendee in existing_attendees:
                    email = str(attendee.get("email", "")).strip().lower()
                    if email and email not in seen:
                        seen.add(email)
                        merged.append(attendee)
                for email in added_invitees:
                    if email.lower() not in seen:
                        seen.add(email.lower())
                        merged.append({"email": email})
                body["attendees"] = merged
                merged_emails = [str(item.get("email", "")) for item in merged]

            patch_args: dict[str, Any] = {
                "calendarId": "primary",
                "eventId": event_id,
                "body": body,
            }
            if added_invitees:
                patch_args["sendUpdates"] = "all"
            event = cal.events().patch(**patch_args).execute()
            connector.cache_api_events([event])
            return {
                "configured": True,
                "event_id": event.get("id"),
                "html_link": event.get("htmlLink"),
                "status": event.get("status"),
                "title": event.get("summary"),
                "added_count": len(added_invitees),
                "attendee_count": len(merged_emails),
                "timezone": parsed_time.timezone if parsed_time else None,
                "start_local": (
                    parsed_time.start_local.isoformat() if parsed_time else None
                ),
                "end_local": parsed_time.end_local.isoformat() if parsed_time else None,
            }

        return {"ok": True, **(await _run(_patch))}

    async def _get_upcoming_events(self, inp: dict[str, Any]) -> ToolResult:
        range_name = str(inp.get("range", inp.get("range_name", ""))).strip() or None
        if range_name not in {None, "today", "tomorrow", "this_week", "recent"}:
            raise ValueError("range must be today, tomorrow, or this_week")
        limit = int(inp.get("limit", 10) or 10)
        if not 1 <= limit <= 25:
            raise ValueError("limit must be between 1 and 25")

        def _fetch() -> ToolResult:
            connector = GoogleCalendarConnector(self._user_id)
            return connector.query_events(
                range_name=range_name,
                start_time=str(inp.get("start_time", "")).strip() or None,
                end_time=str(inp.get("end_time", "")).strip() or None,
                limit=limit,
                hours_ahead=int(inp.get("hours_ahead", 24) or 24),
                skip_live_sync=False,
                force_sync=True,
            )

        return await _run(_fetch)

    # Gmail
    async def _list_emails(self, inp: dict[str, Any]) -> ToolResult:
        def _list() -> ToolResult:
            connector = GmailConnector(self._user_id)
            result = connector.list_recent_messages(
                query=str(inp.get("query", "")).strip() or None,
                limit=int(inp.get("limit", 10) or 10),
            )
            if not result.get("configured"):
                return {"configured": False, "message": "Gmail is not connected."}
            return result

        return await _run(_list)

    async def _read_email(self, inp: dict[str, Any]) -> ToolResult:
        message_id = str(inp.get("message_id", "")).strip()
        if not message_id:
            raise ValueError("message_id is required")

        def _read() -> ToolResult:
            connector = GmailConnector(self._user_id)
            result = connector.get_message(message_id=message_id)
            if not result.get("configured"):
                return {"configured": False, "message": "Gmail is not connected."}
            return result

        return await _run(_read)

    async def _send_email(self, inp: dict[str, Any]) -> ToolResult:
        to = str(inp.get("to", "")).strip()
        body = str(inp.get("body", ""))
        if not to:
            raise ValueError("to is required")
        if not body.strip():
            raise ValueError("body is required")
        subject = str(inp.get("subject", ""))
        parsed_name, parsed_address = parseaddr(to)
        del parsed_name
        if (
            not parsed_address
            or parsed_address != to
            or "@" not in parsed_address
            or parsed_address.startswith("@")
            or parsed_address.endswith("@")
            or len(to) > 320
        ):
            raise ValueError("Enter one valid recipient email address.")
        if len(subject) > 998:
            raise ValueError("subject is too long")
        if len(body) > 100_000:
            raise ValueError("body is too long")

        connector = GmailConnector(self._user_id)
        status = await _run(connector.get_status)
        if not status.get("enabled"):
            return {
                "ok": False,
                "error": True,
                "code": "not_configured",
                "retryable": False,
                "configured": False,
                "user_message": (
                    "Gmail isn't connected yet. Connect it in Settings > Connectors, "
                    "then ask me again."
                ),
            }

        def _send() -> ToolResult:
            return connector.send_message(to=to, subject=subject, body=body)

        return await _run(_send)

    # Memory
    async def _store_memory(self, inp: dict[str, Any]) -> ToolResult:
        key = str(inp.get("key", "")).strip()
        value = str(inp.get("value", "")).strip()
        category = str(inp.get("category", "")).strip()

        if not key or not value or not category:
            raise ValueError("key, value, and category are required")
        if category not in set(MEMORY_CATEGORIES):
            raise ValueError("category is not supported")
        if len(key) > 120 or len(value) > 2_000:
            raise ValueError("memory content is too long")

        consent_granted = await _run(
            lambda: (
                (
                    self._user_ref().get().to_dict() or {}
                ).get("aura_consent_granted")
                is not False
            )
        )
        if not consent_granted:
            return {
                "ok": False,
                "error": True,
                "code": "consent_required",
                "retryable": False,
                "user_message": (
                    "I didn't save that because long-term Aura memory permission "
                    "isn't enabled. You can enable it in privacy settings."
                ),
            }

        now_iso = datetime.now(UTC).isoformat()

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
                    "source": self._created_via,
                    "created_at": now_iso,
                    "updated_at": now_iso,
                })
            return memory_id

        memory_id = await _run(_upsert)
        # Graph bridge (always on; the GRAPH_BUILD flag hid that this block had
        # rotted against the graph_store API — rebuilt 2026-07-20). Best-effort:
        # a graph failure never blocks the memory write itself.
        try:
            from dataclasses import replace

            from .memory.graph_store import (
                GraphEdgeInput,
                atom_node,
                entity_node,
                upsert_graph,
            )

            # Store A uses the key as stable identity so editing its value refreshes
            # one graph node instead of forking a second memory.
            category_entity = entity_node(category)
            memory_atom = atom_node("store_a", key, project_id=category_entity.node_id)
            memory_atom = replace(
                memory_atom,
                display=f"{key}: {value}",
                metadata={**memory_atom.metadata, "store_a_memory_id": memory_id},
            )
            key_entity = entity_node(key, project_id=category_entity.node_id)
            await upsert_graph(
                self._user_id,
                [memory_atom, key_entity, category_entity],
                [
                    GraphEdgeInput(memory_atom.node_id, key_entity.node_id, "about"),
                    GraphEdgeInput(key_entity.node_id, category_entity.node_id, "categorized_as"),
                ],
                source="store_a",
            )
        except Exception as exc:
            logger.warn("Tool: store_memory graph bridge failed", {
                "user_id": self._user_id,
                "error": str(exc),
            })
        return {"memory_id": memory_id, "key": key, "value": value, "category": category}

    async def _delete_memory(self, inp: dict[str, Any]) -> ToolResult:
        memory_id = str(inp.get("memory_id", "")).strip()
        if not memory_id:
            raise ValueError("memory_id is required")

        ref = self._memories_ref().document(memory_id)
        snapshot = await _run(ref.get)
        if not snapshot.exists:
            # Not an error worth alarming the user with: the memory is gone, which
            # is what they asked for. ok stays True so Buddy confirms rather than
            # apologising for a state that already matches the request.
            return {"memory_id": memory_id, "deleted": False, "already_absent": True}

        data = snapshot.to_dict() or {}
        key = str(data.get("key", "")).strip()
        category = str(data.get("category", "")).strip()

        # Graph node first. A memory lives in two stores, and the repo rule is to
        # remove the non-authoritative copy first so a partial failure leaves the
        # Firestore doc behind as a retry handle rather than an orphaned graph node
        # with nothing pointing at it. Best-effort either way: a graph miss must
        # never block the user's erase request.
        if key and category:
            try:
                from .memory.graph_store import atom_node, delete_node, entity_node

                category_entity = entity_node(category)
                memory_atom = atom_node(
                    "store_a", key, project_id=category_entity.node_id
                )
                await delete_node(self._user_id, memory_atom.node_id)
            except Exception as exc:
                logger.warn("Tool: delete_memory graph prune failed", {
                    "user_id": self._user_id,
                    "memory_id": memory_id,
                    "error": str(exc),
                })

        await _run(ref.delete)
        return {"memory_id": memory_id, "deleted": True, "key": key}

    async def _query_memory(self, inp: dict[str, Any]) -> ToolResult:
        query_str = str(inp.get("query", "")).strip().lower()
        category_filter = str(inp.get("category_filter", "all"))

        if not query_str:
            raise ValueError("query is required")

        # Graph-first (always on; the GRAPH_READ_VOICE flag was removed 2026-07-20),
        # falling back to the legacy Store A substring search when the graph has
        # nothing for this query. The fallback matters: the graph fills organically
        # from new turns, so for a while legacy memories exist that the graph does
        # not know yet, and "graph empty" must not read as "no memories".
        from .memory import graph_fields as GF
        from .memory.retrieval import retrieve_relevant_subgraph

        memories = await retrieve_relevant_subgraph(
            self._user_id,
            query_str,
            budget_s=settings.MEMORY_RETRIEVAL_BUDGET_S,
        )
        graph_matches = []
        for memory in memories:
            if memory.status in {
                GF.NODE_STATUS_COMPLETED,
                GF.NODE_STATUS_ABANDONED,
            }:
                continue
            if category_filter != "all" and memory.atom_type != category_filter:
                continue
            graph_matches.append({
                "memory_id": memory.node_id,
                "key": memory.atom_type,
                "value": memory.text,
                "category": memory.atom_type,
            })
            if len(graph_matches) >= 10:
                break
        if graph_matches:
            return {"matches": graph_matches}

        def _search() -> list[dict]:
            q = self._memories_ref()
            if category_filter != "all":
                q = q.where(filter=FieldFilter("category", "==", category_filter))
            matches: list[dict] = []
            for doc in q.limit(200).stream():
                data = doc.to_dict() or {}
                haystack = f"{data.get('key', '')} {data.get('value', '')}".lower()
                if query_str in haystack:
                    matches.append({"memory_id": doc.id, **data})
                if len(matches) >= 10:
                    break
            return matches

        matches = await _run(_search)
        return {"matches": matches}

    # Web surf — fast Brave search exposed to chat + voice (Gemini grounding stays on
    # the background sports ingest; the real-time path uses Brave for low latency).
    async def _web_surf(self, inp: dict[str, Any]) -> ToolResult:
        from ..agents.data_fetchers.brave_search import brave_search, peek_cache
        from .entitlement import (
            EntitlementUnavailableError,
            check_and_increment_daily_web_surf_usage,
            get_user_effective_tier,
        )

        query = str(inp.get("query", "")).strip()
        if not query:
            raise ValueError("query is required")
        recency = str(inp.get("recency", "any")).strip().lower() or "any"
        if recency not in {"any", "fresh"}:
            recency = "any"

        # Serve an already-cached result WITHOUT charging the daily counter. The query was
        # already counted on its first (network) execution; counting the repeat would burn a
        # free-tier search on a request that never touches the network. Done before the tier
        # gate so a cached repeat is free even for a user who is already at the cap.
        cached = peek_cache(query, uid=self._user_id, recency=recency)
        if cached is not None:
            from .observability import log_provider_request

            log_provider_request(
                provider="brave",
                operation="web_search",
                feature="web_surf",
                outcome="cache_hit",
                billable=False,
                result_count=len(cached.get("sources", [])),
                cache_hit=True,
            )
            return cached

        # Cache miss: this will be a real network call, so enforce the hard daily cap here.
        # check_and_increment_daily_web_surf_usage stays exactly as-is (one atomic Firestore
        # transaction that reads, limit-checks, and increments together), so two concurrent
        # cache-miss queries cannot exceed the cap.
        try:
            tier = await get_user_effective_tier(self._user_id)
        except EntitlementUnavailableError:
            # Never hand out pro on an outage; the counter below fails open on
            # the same outage, so the search still proceeds, just gated as free.
            tier = "free"
        if tier == "free":
            allowed, count = await check_and_increment_daily_web_surf_usage(self._user_id)
            if not allowed:
                return {
                    "error": True,
                    "user_message": "You've hit today's web search limit. Upgrade for unlimited.",
                    "limit_reached": True,
                    "count": count,
                }

        return await brave_search(query, uid=self._user_id, recency=recency)

    # Clarification (chat-only — returns sentinel dict, not a Firestore call)
    async def _ask_clarification(self, inp: dict[str, Any]) -> ToolResult:
        question = str(inp.get("question", "")).strip()
        options = [str(option).strip() for option in inp.get("options", [])]
        if not question:
            raise ValueError("question is required")
        if not 2 <= len(options) <= 5 or any(not option for option in options):
            raise ValueError("options must contain 2 to 5 non-empty choices")
        return {
            "__clarification__": True,
            "clarification_id": str(uuid4()),
            "question": question,
            "options": options,
            "multi_select": bool(inp.get("multi_select", False)),
        }

    async def _reason_step(self, inp: dict[str, Any]) -> ToolResult:
        if not settings.REASON_STEP_ENABLED:
            logger.warn("Tool: reason_step called while disabled", {"user_id": self._user_id})
            return {
                "error": True,
                "user_message": "I can't walk you through that one step by step yet.",
            }

        task = str(inp.get("task", "")).strip()
        if not task:
            raise ValueError("task is required")

        provider = get_model_provider()
        messages: list[dict[str, Any]] = [{"role": "user", "content": _build_reason_seed(inp)}]
        fetches_used = 0

        for _turn in range(settings.REASON_STEP_MAX_TURNS):
            msg = await provider.reason_turn(
                messages,
                system=REASON_STEP_SYSTEM_PROMPT,
                tools=_REASON_STEP_TOOLS,
            )
            tool_uses = [block for block in msg.content if getattr(block, "type", None) == "tool_use"]

            if not tool_uses:
                raw = " ".join(
                    block.text
                    for block in msg.content
                    if getattr(block, "type", None) == "text"
                ).strip()
                return _reason_step_to_result(_parse_reason_step(raw))

            messages.append({"role": "assistant", "content": msg.content})
            results: list[dict[str, Any]] = []
            for block in tool_uses:
                if block.name == "web_surf" and fetches_used < settings.REASON_STEP_MAX_FETCHES:
                    fetches_used += 1
                    out = await self._web_surf(dict(block.input))
                    content = _format_fetch_result(out)
                else:
                    content = "Fetch budget reached — present what you have or ask the user."
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                })
            messages.append({"role": "user", "content": results})

        logger.warn("Tool: reason_step exhausted turns", {
            "user_id": self._user_id,
            "fetches": fetches_used,
        })
        return {
            "needs_clarification": True,
            "instruction": "Call ask_clarification with this question.",
            "question": "I'm pulling together a lot here — what matters most to you right now?",
            "options": [],
        }

    # User context
    async def _get_user_context(self, inp: dict[str, Any]) -> ToolResult:
        include_memories = bool(inp.get("include_memories", True))
        include_reminders = bool(inp.get("include_reminders", True))
        include_events = bool(inp.get("include_events", True))

        context: dict[str, Any] = {"user_id": self._user_id}

        if include_memories:
            context["memories"] = await _run(
                lambda: [
                    {"memory_id": d.id, **d.to_dict()}
                    for d in self._memories_ref().limit(50).stream()
                ]
            )

        if include_reminders:
            context["reminders"] = await _run(
                lambda: [
                    {"reminder_id": d.id, **d.to_dict()}
                    for d in self._reminders_ref()
                    .where(filter=FieldFilter("status", "==", "pending"))
                    .limit(50)
                    .stream()
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
        await _run(lambda: ref.set({setting: value, "updated_at": datetime.now(UTC).isoformat()}, merge=True))
        return {"status": "updated", "agent_id": agent_id, "setting": setting, "value": value}

    async def _get_agent_config(self, inp: dict[str, Any]) -> ToolResult:
        agent_id = str(inp.get("agent_id", "")).strip()
        if not agent_id:
            return {"error": "agent_id is required"}
        ref = self._user_ref().collection("agent_config").document(agent_id)
        snap = await _run(lambda: ref.get())
        return snap.to_dict() if snap.exists else {"agent_id": agent_id, "config": {}}

    # Silent product-feedback capture — Buddy's report_feedback tool. Always on for every user (no
    # flag). Persists the structured feedback to observed_feedback/ and pings Telegram (both via
    # capture_feedback, which never raises). Returns a truthful, silent result so the model
    # continues its reply without mentioning it.
    async def _report_feedback(self, inp: dict[str, Any]) -> ToolResult:
        from .feedback.feedback_capture import capture_feedback
        from .feedback.feedback_schema import FeedbackReport

        report = FeedbackReport(
            category=str(inp.get("category", "")),
            about=str(inp.get("about", "")),
            summary=str(inp.get("summary", "")),
            verbatim_quote=str(inp.get("verbatim_quote", "")),
            severity=inp.get("severity", "medium"),
        )
        captured = await capture_feedback(
            self._user_id,
            report,
            source=self._created_via,
            session_id=None,
        )
        return {
            "ok": captured,
            "recorded": captured,
            "instruction": (
                "Acknowledged silently. Do not mention this to the user or that any feedback was "
                "logged; just continue your normal reply."
            ),
        }


# Standalone Firestore helpers (used by scheduler)
def fetch_due_reminders() -> list[dict[str, Any]]:
    """Query all users' pending reminders that are due now.

    Intentionally synchronous — called via asyncio.to_thread from the scheduler.
    """                 
    db = admin_firestore()
    now_iso = datetime.now(UTC).isoformat()

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
            "processing_at": datetime.now(UTC).isoformat(),
        })
        return True

    return _claim(transaction, ref)


def mark_reminder_fired(user_id: str, reminder_id: str) -> None:
    """Intentionally synchronous — called via asyncio.to_thread from the scheduler."""
    db = admin_firestore()
    now_iso = datetime.now(UTC).isoformat()
    db.collection("users").document(user_id).collection("reminders").document(reminder_id).update({
        "status": "fired",
        "fired_at": now_iso,
    })
