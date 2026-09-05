"""
POST /chat: text-based conversation via Claude with SSE streaming.

SSE event format (each line: "data: <json>\n\n"):
  {"type": "text_delta",      "delta": str}
  {"type": "tool_thinking",   "message": str}
  {"type": "tool_status",     "tool": str, "message": str}
  {"type": "clarification_ui","clarification_id": str, "question": str,
                               "options": list[str], "multi_select": bool}
  {"type": "done",            "metadata": {"tool_names": list, "reminder"?: dict,
                                            "awaiting_clarification"?: bool}}
  {"type": "error",           "message": str}
Terminated by: "data: [DONE]\n\n"
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from collections.abc import AsyncGenerator
from typing import Any
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config.settings import settings
from ..lib.logger import logger
from ..services import desktop_chat_store
from ..services.analytics.llm_telemetry import bind_trace_context, reset_trace_context
from ..services.chat_completion import (
    context_assembler,
    handoff_store,
    mobile_compaction,
    text_compaction,
    turn_store,
)
from ..services.chat_completion import prompt_builder as _prompt_builder
from ..services.chat_completion.prompt_builder import build_turn_system_blocks, fetch_user_doc
from ..services.chat_completion.reminder_receipts import reminder_ui_payload
from ..services.chat_error_copy import CHAT_TEMPORARILY_UNAVAILABLE_MESSAGE
from ..services.claude_client import ClaudeClient
from ..services.engagement.task_scheduler import get_task_scheduler
from ..services.query_log import log_query
from ..services.request_auth import resolve_user_id, resolve_user_id_from_request
from ..services.tool_executor import (
    ToolExecutor,
    excluded_tools_for_chat_surface,
    resolve_chat_surface_allowed_tools,
)
from ..services.user_aura_extractor import extract_and_update_user_aura
from ..shared.capability_claims import log_false_capability_claims
from ..shared.tools import claude_tool_definitions

_build_user_content = _prompt_builder.build_user_content
_NOTIFICATION_REASON_MAX_CHARS = _prompt_builder.NOTIFICATION_REASON_MAX_CHARS


class ChatRequest(BaseModel):
    """Versioned, backward-compatible boundary for released chat clients."""

    model_config = ConfigDict(extra="ignore")

    contract_version: int = 1
    surface: str = "app"
    user_id: str | None = None
    message: str = Field(default="", max_length=8_000)
    attachments: list[Any] = Field(default_factory=list, max_length=10)
    history: list[Any] = Field(default_factory=list, max_length=100)
    session_id: str | None = Field(default=None, max_length=128)
    client_message_id: str | None = Field(default=None, max_length=128)
    notification_reason: str = ""
    lineage_chain: list[Any] = Field(default_factory=list, max_length=50)
    origin: str = Field(default="organic", max_length=64)
    origin_candidate_id: str | None = Field(default=None, max_length=128)
_build_system_blocks = _prompt_builder.build_system_blocks
_build_injected_system_prompt_suffix = _prompt_builder.build_injected_system_prompt_suffix


async def _reconcile_and_schedule_intents(
    user_id: str,
    message: str,
    prev_buddy_response: str | None,
    user_doc: dict[str, Any] | None = None,
    session_id: str = "",
) -> None:
    """Lazy-imported wrapper for the reactive intent sensor, so chat.py stays
    decoupled from the reactive package at module load. Never raises."""
    try:
        from ..services.reactive.intent_sense import reconcile_and_schedule

        await reconcile_and_schedule(
            user_id,
            message,
            prev_buddy_response,
            user_doc=user_doc,
            session_id=session_id,
        )
    except Exception as exc:
        logger.warn("chat: intent sense task failed (swallowed)", {
            "user_id": user_id, "error": str(exc),
        })


def _resolve_user_id(event: dict[str, Any], body: dict[str, Any]) -> str | None:
    try:
        return event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
    except (KeyError, TypeError):
        pass
    uid = body.get("user_id")
    explicit_uid = str(uid) if isinstance(uid, str) and uid else None
    return resolve_user_id(
        event.get("headers"),
        explicit_user_id=explicit_uid if not settings.is_production else None,
    )


def _error_stream(message: str) -> AsyncGenerator[str, None]:
    async def _gen():
        yield f"data: {json.dumps({'type': 'error', 'message': message})}\n\n"
        yield "data: [DONE]\n\n"

    return _gen()


def _chat_limit_reached_stream() -> AsyncGenerator[str, None]:
    _payload = json.dumps({
        "type": "chat_limit_reached",
        "message": "That's your free messages for today! Upgrade to keep the conversation going with Buddy.",
    })

    async def _gen():
        yield f"data: {_payload}\n\n"
        yield "data: [DONE]\n\n"

    return _gen()


def _replay_stream(answer: str, reminder: dict[str, Any] | None) -> AsyncGenerator[str, None]:
    """Re-deliver an answer this exact turn already produced.

    Reached when a desktop client re-POSTs a client_message_id whose canonical assistant
    message is already stored (a lost acknowledgement, or a resend after a dropped
    connection). Replaying costs nothing and cannot produce a second, differently-worded
    answer, which is the whole point of the deterministic message id.
    """
    metadata: dict[str, Any] = {"tool_names": [], "replayed": True}
    if reminder:
        metadata["reminder"] = reminder
    frames = [
        json.dumps({"type": "text_delta", "delta": answer}),
        json.dumps({"type": "done", "metadata": metadata}),
    ]

    async def _gen():
        for frame in frames:
            yield f"data: {frame}\n\n"
        yield "data: [DONE]\n\n"

    return _gen()


def _sse_error_response(
    message: str,
    *,
    status_code: int,
    headers: dict[str, str],
) -> StreamingResponse:
    return StreamingResponse(
        _error_stream(message),
        media_type="text/event-stream",
        status_code=status_code,
        headers=headers,
    )


# Kept in sync with lib/data/models/attachment_validator.dart
_SUPPORTED_IMAGE_MIME_TYPES: frozenset[str] = frozenset({
    "image/jpeg", "image/png", "image/gif", "image/webp",
})
_SUPPORTED_DOCUMENT_MIME_TYPES: frozenset[str] = frozenset({
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "text/plain", "text/csv", "text/tab-separated-values", "text/html", "application/rtf",
    "application/epub+zip",
})
_MAX_ATTACHMENTS_PER_REQUEST = 5
_MAX_IMAGE_BASE64_SIZE = 7_000_000      # ~5 MB raw * 1.33 base64 overhead
_MAX_DOCUMENT_BASE64_SIZE = 14_000_000  # ~10 MB raw * 1.33 base64 overhead
_EXPLICIT_SCREEN_SAVE_REQUEST = re.compile(
    r"\b(?:save|capture|keep|remember)\s+(?:(?:this|the|my|current)\s+)?(?:screen|screenshot)\b"
    r"|\b(?:take|save)\s+(?:a\s+)?screenshot\b"
    r"|\bscreenshot\s+(?:this|that|it)\b",
    re.IGNORECASE,
)


class AttachmentRejection:
    """Details about a rejected attachment for the 422 response."""

    __slots__ = ("index", "file_name", "reason")

    def __init__(self, index: int, file_name: str, reason: str) -> None:
        self.index = index
        self.file_name = file_name
        self.reason = reason

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "file_name": self.file_name, "reason": self.reason}


def _validate_and_filter_attachments(
    raw: list[Any],
    user_id: str,
) -> tuple[list[dict[str, Any]], list[AttachmentRejection]]:
    """
    Server-side trust boundary: validate attachment count, MIME type, and data size.
    Returns (accepted, rejections). Caller should 422 when rejections is non-empty.
    """
    if not raw or not isinstance(raw, list):
        return [], []

    accepted: list[dict[str, Any]] = []
    rejections: list[AttachmentRejection] = []

    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        file_name = str(item.get("file_name", f"attachment_{i}"))

        if len(accepted) >= _MAX_ATTACHMENTS_PER_REQUEST:
            rejections.append(AttachmentRejection(i, file_name, "max 5 attachments per message"))
            continue

        mime = item.get("mime_type", "")
        att_type = item.get("type", "")
        data = item.get("data", "")

        if not isinstance(data, str) or not data:
            rejections.append(AttachmentRejection(i, file_name, "missing or empty data"))
            continue

        if att_type == "image" and mime in _SUPPORTED_IMAGE_MIME_TYPES:
            if len(data) > _MAX_IMAGE_BASE64_SIZE:
                rejections.append(AttachmentRejection(i, file_name, "image exceeds 5 MB"))
                continue
            accepted.append(item)
        elif att_type == "document" and mime in _SUPPORTED_DOCUMENT_MIME_TYPES:
            if len(data) > _MAX_DOCUMENT_BASE64_SIZE:
                rejections.append(AttachmentRejection(i, file_name, "document exceeds 10 MB"))
                continue
            accepted.append(item)
        else:
            rejections.append(AttachmentRejection(i, file_name, f"unsupported type: {mime}"))

    if rejections:
        logger.warn("Chat: attachments rejected", {
            "user_id": user_id,
            "rejected": [r.to_dict() for r in rejections],
        })

    return accepted, rejections


async def _save_attached_desktop_screen(
    *,
    user_id: str,
    session_id: str,
    client_message_id: str,
    attachments: list[dict[str, Any]],
) -> dict[str, Any]:
    attachment = next(
        (
            item
            for item in attachments
            if item.get("type") == "image" and item.get("mime_type") == "image/jpeg"
        ),
        None,
    )
    if not attachment or not session_id or not client_message_id:
        return {"ok": False, "code": "screen_unavailable"}
    try:
        jpeg_bytes = base64.b64decode(str(attachment.get("data") or ""), validate=True)
        from ..agent.voice.screen_frames import ScreenFrame
        from ..agent.voice.screen_saves import save_screen_capture

        frame_id = hashlib.sha256(jpeg_bytes).hexdigest()[:32]
        result = await save_screen_capture(
            uid=user_id,
            session_id=session_id,
            finalized_message_id=client_message_id,
            frame=ScreenFrame(
                jpeg_bytes=jpeg_bytes,
                attributes={
                    "frame_id": frame_id,
                    "active_window_title": "Screen capture",
                },
                received_at_monotonic=time.monotonic(),
            ),
        )
    except Exception as exc:
        logger.warn(
            "Chat: explicit screen save failed",
            {"user_id": user_id, "error_type": type(exc).__name__},
        )
        return {"ok": False, "code": "screen_save_failed"}
    if not result.succeeded:
        return {"ok": False, "code": "screen_save_failed"}
    return {
        "ok": True,
        "item_id": result.item_id,
        "collection_name": result.collection_name,
        "image_path": result.image_path,
        "already_saved": result.already_saved,
    }


_NOTIFICATION_REASON_MAX_CHARS = 600


def _build_system_blocks(
    base_system_prompt: str,
    aura_suffix: str,
    local_datetime: str,
    notification_reason: str = "",
) -> list[dict[str, Any]]:
    """
    Build the Anthropic system parameter as a list of TextBlockParams with
    prompt-cache breakpoints.

    Layout (stable → volatile, so the cache prefix is as long as possible):
      Block 1: base prompt                          [cache_control]  — never changes
      Block 2: aura suffix                          [cache_control]  — stable for ~10 min
      Block 3: current datetime                                      — not cached
      Block 4: why-you-reached-out (optional)                        — not cached

    Anthropic evaluates cache breakpoints in tools → system → messages order.
    The list format is required for explicit cache_control placement; a plain
    string only supports automatic (top-level) caching which cannot exclude the
    volatile datetime from the cached prefix.

    ``notification_reason`` is set ONLY on the first turn after a proactive
    notification tap (the client sends it once, then drops it). It is appended
    AFTER the cached prefix so it never pollutes the cache, and it orients Buddy
    on WHY it reached out so it does not disown its own opener when the user
    replies.
    """
    stable_text = base_system_prompt
    blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": stable_text,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        },
    ]
    if aura_suffix:
        blocks.append({
            "type": "text",
            "text": aura_suffix,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        })
    blocks.append({
        "type": "text",
        "text": f"Current date and time: {local_datetime}",
    })
    if notification_reason:
        blocks.append({
            "type": "text",
            "text": (
                "WHY YOU REACHED OUT (private context for THIS reply only — you "
                "started this conversation by pinging them; do not quote this note or "
                "mention you have one, just stay oriented):\n"
                f"{notification_reason}"
            ),
        })
    return blocks


async def handle_chat_handoff(request: Request) -> JSONResponse:
    """Store the recent text exchanges a voice session about to start should load.

    Desktop calls this in parallel with /voice/token, carrying the same client-owned
    conversation id it stamps into the token. The write has to land before the worker's
    pre-session fetch, which is why it is awaited rather than fired off.

    A rejected or failed handoff is not a failed call: the client treats it as fail-open
    and the session simply starts without text context.
    """
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    conversation_id = str((body or {}).get("conversation_id", "")).strip()
    if not handoff_store.CONVERSATION_ID_RE.fullmatch(conversation_id):
        return JSONResponse({"error": "Invalid conversation_id"}, status_code=400)
    raw_turns = (body or {}).get("turns")
    if not isinstance(raw_turns, list):
        return JSONResponse({"error": "turns must be a list"}, status_code=400)
    stored = await handoff_store.save_handoff(user_id, conversation_id, raw_turns)
    return JSONResponse({"ok": True, "stored": stored})


async def handle_chat_session_background(request: Request) -> JSONResponse:
    """The chat client backgrounded. Arm the grace clock, never finalize here.

    This is a hint, not an end event. Android fires `paused` for a notification
    shade pull or a permission dialog, so the session only becomes eligible for
    finalization once CHAT_BACKGROUND_GRACE elapses with no new turn. Always 200:
    a lost hint costs a slower follow-up, never a wrong one.
    """
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    session_id = str((body or {}).get("session_id", "")).strip()
    if not session_id:
        return JSONResponse({"ok": True, "armed": False})

    from ..services.session_followup.lifecycle import session_lifecycle_service

    asyncio.create_task(
        session_lifecycle_service.note_client_background(user_id, session_id),
        name=f"followup-chat-background-{session_id[:8]}",
    )
    return JSONResponse({"ok": True, "armed": True})


async def handle_chat_stream(event: dict[str, Any]) -> StreamingResponse:
    _sse_headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }

    try:
        body: dict[str, Any] = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _sse_error_response("Invalid JSON body", status_code=400, headers=_sse_headers)
    try:
        body = ChatRequest.model_validate(body).model_dump()
    except ValidationError as exc:
        logger.info(
            "Chat: request contract rejected",
            {"error_count": len(exc.errors())},
        )
        return _sse_error_response(
            "Invalid chat request",
            status_code=422,
            headers=_sse_headers,
        )

    surface = str(body.get("surface") or "app")
    request_headers = {
        str(key).casefold(): str(value)
        for key, value in dict(event.get("headers") or {}).items()
    }
    # Which SSE frame types this client can parse. Absent means 1, which is every
    # build shipped before per-tool activity frames existed. A client that cannot
    # parse a frame renders it as a visible error, so new frame types are opt-in
    # by the CLIENT, never decided by the server's own version.
    try:
        contract_version = int(body.get("contract_version") or 1)
    except (TypeError, ValueError):
        contract_version = 1
    surface_allowed_tools = resolve_chat_surface_allowed_tools(
        surface, contract_version=contract_version
    )
    surface_tool_exclusions = excluded_tools_for_chat_surface(
        surface, contract_version=contract_version
    )

    # verify_id_token does blocking crypto (and a cert refetch when Google's
    # signing keys rotate), so keep it off the event loop the stream shares.
    user_id = await asyncio.to_thread(_resolve_user_id, event, body)
    if not user_id:
        logger.warn("Chat: rejected, missing user_id")
        return _sse_error_response(
            "Unauthorized: user_id required",
            status_code=401,
            headers=_sse_headers,
        )

    # effective_tier is always resolved so it can be passed to the
    # Claude client for tool-level gating regardless of environment.
    effective_tier = "pro"
    if settings.is_production:
        from ..services.entitlement import (
            EntitlementUnavailableError,
            check_and_increment_daily_chat_usage,
            get_user_effective_tier,
        )
        try:
            effective_tier = await get_user_effective_tier(user_id)
        except EntitlementUnavailableError:
            # Never hand out pro on an outage. "free" here only tightens tool
            # gating for the turn; the usage counter below fails open on the
            # same outage, so the user is degraded, never hard-blocked.
            effective_tier = "free"
        if effective_tier == "free":
            allowed, _ = await check_and_increment_daily_chat_usage(user_id)
            if not allowed:
                logger.info("Chat: free-tier daily limit reached", {"user_id": user_id})
                return StreamingResponse(
                    _chat_limit_reached_stream(),
                    media_type="text/event-stream",
                    status_code=200,
                    headers=_sse_headers,
                )

    message = str(body.get("message", "")).strip()
    raw_attachments: list[Any] = body.get("attachments") or []
    if not message and not raw_attachments:
        logger.warn("Chat: rejected, empty message", {"user_id": user_id})
        return _sse_error_response("message or attachments required", status_code=400, headers=_sse_headers)
    if len(message) > 8_000:
        logger.warn(
            "Chat: rejected, message too long",
            {"user_id": user_id, "message_len": len(message)},
        )
        return _sse_error_response(
            "message must be 8 000 characters or fewer",
            status_code=400,
            headers=_sse_headers,
        )

    raw_session_id = body.get("session_id")
    session_id = (
        raw_session_id.strip()
        if isinstance(raw_session_id, str) and raw_session_id.strip()
        else None
    )
    # Both ids reach Firestore as document ids (chat_turns, and the desktop transcript
    # below), so an id containing "/" would build a different path than intended. Guard
    # every surface, not just desktop.
    if session_id is not None and not desktop_chat_store.is_safe_document_id(session_id):
        logger.warn("Chat: rejected, unusable session_id", {"user_id": user_id})
        return _sse_error_response("invalid session_id", status_code=400, headers=_sse_headers)
    if session_id:
        from ..services.session_followup.lifecycle import session_lifecycle_service

        raw_lineage = body.get("lineage_chain")
        asyncio.create_task(
            session_lifecycle_service.start_session(
                user_id,
                session_id,
                surface="chat",
                origin=str(body.get("origin") or "organic"),
                origin_candidate_id=(
                    str(body.get("origin_candidate_id") or "").strip() or None
                ),
                lineage_chain=raw_lineage if isinstance(raw_lineage, list) else [],
            ),
            name=f"followup-chat-session-{session_id[:8]}",
        )

    # Filter first, THEN take the tail. Slicing before filtering and breaking at
    # the window size kept the OLDEST entries of the pre-slice and silently threw
    # away the most recent ones, so any thread longer than the window was answered
    # against conversation state from a window-length ago.
    filtered: list[dict[str, Any]] = []
    for h in (body.get("history") or []):
        if not isinstance(h, dict) or h.get("role") not in ("user", "assistant") or not h.get("content"):
            continue
        content = h["content"]
        filtered.append({
            "role": str(h["role"]),
            "content": content if isinstance(content, list) else str(content),
        })
    history: list[dict[str, Any]] = filtered[-settings.CHAT_HISTORY_WINDOW :]
    # The window has to open on a user message: a leading assistant reply whose
    # prompt was just trimmed away reads as context-free noise, and some provider
    # configurations reject it outright.
    if history and history[0]["role"] == "assistant":
        history = history[1:]

    raw_client_message_id = body.get("client_message_id")
    client_message_id = (
        raw_client_message_id.strip()
        if isinstance(raw_client_message_id, str) and raw_client_message_id.strip()
        else None
    )
    if client_message_id is not None and not desktop_chat_store.is_safe_document_id(
        client_message_id
    ):
        logger.warn("Chat: rejected, unusable client_message_id", {"user_id": user_id})
        return _sse_error_response(
            "invalid client_message_id", status_code=400, headers=_sse_headers
        )
    # Sent ONLY on the first chat turn after a proactive-notification tap (the client
    # drops it after one send). It is the Buddy-facing "why I reached out" note from
    # the push payload; injected into the system prompt below so Buddy stays oriented
    # on the opener it sent. Capped defensively (the producers already keep it short).
    notification_reason = str(body.get("notification_reason") or "").strip()[
        :_NOTIFICATION_REASON_MAX_CHARS
    ]

    validated_attachments, attachment_rejections = _validate_and_filter_attachments(raw_attachments, user_id)

    if attachment_rejections:
        return _sse_error_response(
            f"Invalid attachments: {', '.join(r.file_name + ' (' + r.reason + ')' for r in attachment_rejections)}",
            status_code=422,
            headers=_sse_headers,
        )

    # Canonical desktop transcript. Written BEFORE any generation work, so the user's own
    # message is durable the moment the request is accepted.
    #
    # This one does NOT fail open, unlike turn_store. Firestore is the promised source of
    # truth for desktop chat, so continuing past a failed user-message write would generate
    # an answer, store it, and leave an assistant-only conversation with the recovery record
    # deleted underneath it - a turn the user can neither see the question for nor retry.
    # Refusing up front costs one model call that was never going to be recoverable, and the
    # client already renders a 5xx as a failed bubble with Retry.
    desktop_conversation_id: str | None = None
    if (
        surface == "desktop"
        and session_id
        and client_message_id
        and desktop_chat_store.is_valid_id(session_id)
        and desktop_chat_store.is_valid_id(client_message_id)
    ):
        desktop_conversation_id = session_id
        user_message_result = await desktop_chat_store.put_user_message(
            user_id,
            desktop_conversation_id,
            client_message_id,
            text=message,
            has_attachments=bool(validated_attachments),
        )
        if user_message_result == desktop_chat_store.RESULT_ERROR:
            # Only reachable on a real Firestore failure: both ids were validated above.
            logger.warn("Chat: desktop transcript write failed, refusing the turn", {
                "user_id": user_id, "cmid": client_message_id,
            })
            return _sse_error_response(
                "Could not save this message. Try sending it again.",
                status_code=503,
                headers=_sse_headers,
            )
        if user_message_result == desktop_chat_store.RESULT_DUPLICATE:
            stored_answer = await desktop_chat_store.get_assistant_message(
                user_id, desktop_conversation_id, client_message_id
            )
            if stored_answer:
                logger.info("Chat: replayed stored answer for duplicate turn", {
                    "user_id": user_id, "cmid": client_message_id,
                })
                return StreamingResponse(
                    _replay_stream(
                        str(stored_answer.get(desktop_chat_store.FIELD_TEXT) or ""),
                        stored_answer.get(desktop_chat_store.FIELD_REMINDER),
                    ),
                    media_type="text/event-stream",
                    status_code=200,
                    headers=_sse_headers,
                )

    conversation_summary = ""
    context_source = "client"
    if desktop_conversation_id and client_message_id:
        user_doc, assembled_context = await asyncio.gather(
            fetch_user_doc(user_id),
            context_assembler.assemble_desktop_context(
                user_id,
                desktop_conversation_id,
                current_message_id=client_message_id,
                fallback_history=history,
            ),
        )
        history = assembled_context.history
        conversation_summary = assembled_context.conversation_summary
        context_source = assembled_context.source
    elif surface == "app" and session_id and len(history) >= settings.CHAT_HISTORY_WINDOW:
        # Mobile with a full verbatim window: anything older fell off the
        # 30-message slice, so the rolling summary (folded post-turn by
        # mobile_compaction) is what remembers it. Same parallel-gather rule as
        # desktop — one extra doc read, zero serial latency.
        user_doc, conversation_summary = await asyncio.gather(
            fetch_user_doc(user_id),
            mobile_compaction.load_context_summary(user_id, session_id),
        )
        if conversation_summary:
            context_source = "client_plus_summary"
    else:
        user_doc = await fetch_user_doc(user_id)

    prev_buddy_response: str | None = next(
        (h["content"] for h in reversed(history) if h["role"] == "assistant"),
        None,
    )

    # Single read of users/{uid} for this turn, shared below instead of 4
    # independent re-fetches (datetime, aura-revoke check, and the two fire-and-
    # forget tasks' own consent checks) -- see firestore_read_audit_20260706 memory.
    #
    # The conversation summary rides along in the SAME gather rather than as a
    # second sequential await: it is one extra document read, and putting it in
    # series would add its full round trip to time-to-first-token on every
    # desktop turn. Non-desktop surfaces resolve it to "" without any read.
    # Build the full system prompt (datetime + aura profile suffix + query-relevant
    # long-term memory) via the shared assembler, so the live turn here and the durable
    # background completion (services/chat_completion) construct the EXACT same prompt.
    system_prompt_blocks = await build_turn_system_blocks(
        user_id, message, notification_reason, user_doc=user_doc,
        conversation_summary=conversation_summary,
    )

    asyncio.create_task(
        log_query(
            user_id,
            "chat",
            message,
            session_id=session_id,
            client_message_id=client_message_id,
        )
    )
    asyncio.create_task(
        extract_and_update_user_aura(
            user_id,
            message,
            session_id,
            prev_buddy_response,
            user_doc=user_doc,
            turn_id=client_message_id or None,
            turn_index=len(history),
            surface="chat",
        )
    )
    # Reactive layer: detect resolutions ("mom is fine" -> cancel the queued surgery
    # follow-up) and future concerns ("mom has surgery tomorrow" -> schedule one).
    # Fire-and-forget, consent-gated + cost-capped, never touches the stream.
    asyncio.create_task(
        _reconcile_and_schedule_intents(
            user_id,
            message,
            prev_buddy_response,
            user_doc,
            session_id or "",
        )
    )

    user_content = _build_user_content(message, validated_attachments)
    from ..services.action_intent_policy import (
        blocked_write_reasons_for_text_turn,
        has_unreceipted_reminder_success_claim,
        reminder_receipt_guard_armed,
    )

    # Nothing about the user's WORDING removes a tool any more. The only exclusions left
    # are structural (which surface this is), and a write the turn plainly contradicts is
    # refused at execution with an envelope Buddy can speak. Hiding set_reminder per turn
    # is what taught Buddy to say it has no reminder tool.
    action_tool_exclusions = surface_tool_exclusions
    # What the model will actually be handed this turn, kept so the post-stream check can
    # tell an honest "I can't" from a denial of a tool that was sitting right there.
    exposed_tool_names = frozenset(
        tool["name"]
        for tool in claude_tool_definitions()
        if tool["name"] not in action_tool_exclusions
    )
    blocked_write_reasons = blocked_write_reasons_for_text_turn(message)
    reminder_receipt_check_armed = reminder_receipt_guard_armed(
        message, prev_buddy_response or ""
    )

    logger.info(
        "Chat: stream request received",
        {
            "user_id": user_id,
            "session_id": session_id,
            "message_len": len(message),
            "history_turns": len(history),
            "attachment_count": len(validated_attachments),
            "surface": surface,
            "context_source": context_source,
        },
    )

    start_ts = time.monotonic()

    # Durable background completion: record this turn and enqueue a delayed Cloud Task so
    # that if the phone disconnects mid-stream (the generator below is cancelled and the
    # answer is lost), the turn still finishes server-side and pushes the reply. 
    completion_setup: asyncio.Task[str | None] | None = None
    if client_message_id:
        async def _record_turn_for_recovery() -> str | None:
            # Runs concurrently with the model stream: both halves are fail-open
            # (start_turn never raises, the enqueue is caught here), so nothing a
            # failure could do changes, it just no longer sits serially between
            # the request and the first model token. The enqueued task is delayed
            # by CHAT_COMPLETION_DELAY_SECONDS, so recording it a beat later is
            # immaterial to recovery.
            turn_recorded = await turn_store.start_turn(
                user_id,
                client_message_id,
                session_id=session_id,
                message=message,
                history=history,
                has_attachments=bool(validated_attachments),
                tier=effective_tier,
                notification_reason=notification_reason,
                surface=surface,
            )
            if not turn_recorded:
                return None
            try:
                return await asyncio.to_thread(
                    get_task_scheduler().schedule_chat_completion,
                    user_id,
                    client_message_id,
                    session_id or "",
                    settings.CHAT_COMPLETION_DELAY_SECONDS,
                )
            except Exception as exc:
                logger.warn("Chat: completion task enqueue failed (backstop sweep covers it)", {
                    "user_id": user_id, "cmid": client_message_id, "error": str(exc),
                })
                return None

        completion_setup = asyncio.create_task(
            _record_turn_for_recovery(),
            name=f"chat-turn-record-{client_message_id[:8]}",
        )

    async def _generate() -> AsyncGenerator[str, None]:
        trace_token = bind_trace_context(
            trace_id=client_message_id or uuid4().hex,
            client_message_id=client_message_id,
            session_id=session_id,
            surface="chat",
            prompt_version="chat-v1",
        )
        try:
            effective_system_prompt_blocks = system_prompt_blocks
            screen_save_result: dict[str, Any] | None = None
            if (
                surface == "desktop"
                and _EXPLICIT_SCREEN_SAVE_REQUEST.search(message)
            ):
                screen_tool_id = f"screen-save:{client_message_id or uuid4().hex}"
                if contract_version >= 2:
                    yield "data: " + json.dumps({
                        "type": "tool_start",
                        "id": screen_tool_id,
                        "tool": "save_screen_item",
                        "label": "Saving your screen",
                        "detail": "",
                    }) + "\n\n"
                screen_save_result = await _save_attached_desktop_screen(
                    user_id=user_id,
                    session_id=session_id or "",
                    client_message_id=client_message_id or "",
                    attachments=validated_attachments,
                )
                screen_saved = screen_save_result.get("ok") is True
                if contract_version >= 2:
                    yield "data: " + json.dumps({
                        "type": "tool_end",
                        "id": screen_tool_id,
                        "tool": "save_screen_item",
                        "ok": screen_saved,
                    }) + "\n\n"
                if screen_saved:
                    try:
                        await turn_store.record_completed_tool(
                            user_id,
                            client_message_id or "",
                            tool="save_screen_item",
                            result=screen_save_result,
                        )
                    except Exception as exc:
                        logger.warn(
                            "Chat: screen save receipt recording failed",
                            {"user_id": user_id, "error_type": type(exc).__name__},
                        )
                    screen_action_instruction = (
                        "The exact attached screenshot was durably saved to the user's "
                        "Screen Saves. Confirm that it was saved. Do not claim you cannot "
                        "see or save the screen, and do not attempt a second screen save."
                    )
                else:
                    screen_action_instruction = (
                        "The user explicitly asked to save their screen, but the durable "
                        "save could not be verified. Say the screen save failed and ask "
                        "them to try again. Do not claim that screen capture is unavailable."
                    )
                effective_system_prompt_blocks = [
                    *system_prompt_blocks,
                    {
                        "type": "text",
                        "text": (
                            f"<trusted_action_receipt>{screen_action_instruction}"
                            "</trusted_action_receipt>"
                        ),
                    },
                ]
            tool_executor = ToolExecutor(
                user_id,
                created_via="text",
                client_message_id=client_message_id or "",
                session_id=session_id or "",
                allowed_tools=surface_allowed_tools,
                blocked_write_reasons=blocked_write_reasons,
                user_tier=effective_tier,
                product_surface=surface,
                product_platform=request_headers.get("x-aura-platform", ""),
                app_version=request_headers.get("x-aura-app-version", ""),
            )
            claude = ClaudeClient(tool_executor)
            buffered_text_events: list[dict[str, Any]] = []
            # What the user actually saw, reassembled so the canonical desktop transcript
            # stores the delivered answer rather than a second regeneration of it.
            answer_parts: list[str] = []
            done_metadata: dict[str, Any] = {}
            stream_error_seen = False
            # Server-side TTFT: request received -> first text delta actually sent
            # to the client. Logged with the completion line so a slow client
            # ttft_ms can be split into server vs network without guessing.
            first_text_at_ms: int | None = None
            async for sse_event in claude.send_text_turn_stream(
                system_prompt=effective_system_prompt_blocks,
                user_content=user_content,
                history=history,
                is_agent=False,
                extra_excluded_tools=action_tool_exclusions,
                contract_version=contract_version,
            ):
                if (
                    sse_event.get("type") == "done"
                    and screen_save_result
                    and screen_save_result.get("ok") is True
                ):
                    metadata = sse_event.get("metadata") or {}
                    tool_names = metadata.setdefault("tool_names", [])
                    if isinstance(tool_names, list) and "save_screen_item" not in tool_names:
                        tool_names.append("save_screen_item")
                if sse_event.get("type") == "done":
                    metadata = sse_event.get("metadata") or {}
                    tool_names = [
                        str(name) for name in metadata.get("tool_names") or []
                    ]
                    reminder_payload = reminder_ui_payload(
                        metadata.get("reminder"), tool_names
                    )
                    if reminder_payload is None:
                        metadata.pop("reminder", None)
                    else:
                        metadata["reminder"] = reminder_payload
                    sse_event["metadata"] = metadata
                if reminder_receipt_check_armed and sse_event.get("type") == "text_delta":
                    buffered_text_events.append(sse_event)
                    continue
                if reminder_receipt_check_armed and sse_event.get("type") == "done":
                    metadata = sse_event.get("metadata") or {}
                    reminder_receipt = metadata.get("reminder")
                    buffered_text = "".join(
                        str(event.get("delta", "")) for event in buffered_text_events
                    )
                    if (
                        not reminder_receipt
                        and has_unreceipted_reminder_success_claim(buffered_text)
                    ):
                        logger.warn(
                            "text_action_success_claim_without_receipt",
                            {
                                "user_id": user_id,
                                "session_id": session_id,
                                "tool": "set_reminder",
                            },
                        )
                        buffered_text_events = [{
                            "type": "text_delta",
                            "delta": (
                                "I couldn't verify that reminder, so I won't say it's set. "
                                "Want me to try again?"
                            ),
                        }]
                    for buffered_event in buffered_text_events:
                        if first_text_at_ms is None:
                            first_text_at_ms = int((time.monotonic() - start_ts) * 1000)
                        answer_parts.append(str(buffered_event.get("delta", "")))
                        yield f"data: {json.dumps(buffered_event)}\n\n"
                    buffered_text_events = []
                event_type = sse_event.get("type")
                if event_type == "text_delta":
                    if first_text_at_ms is None:
                        first_text_at_ms = int((time.monotonic() - start_ts) * 1000)
                    answer_parts.append(str(sse_event.get("delta", "")))
                elif event_type == "done":
                    done_metadata = sse_event.get("metadata") or {}
                elif event_type == "error":
                    stream_error_seen = True
                yield f"data: {json.dumps(sse_event)}\n\n"
            duration_ms = int((time.monotonic() - start_ts) * 1000)
            logger.info(
                "Chat: stream complete",
                {
                    "user_id": user_id,
                    "duration_ms": duration_ms,
                    # None means no text was ever emitted (pure-error turn, or the
                    # reminder-receipt guard swallowed the stream until done).
                    "ttft_ms": first_text_at_ms,
                },
            )
            # Log-only, after delivery: did Buddy just tell this user Aura cannot do
            # something it can? A confirmed hit means tool exposure regressed and the
            # user was told a falsehood about the product, which nothing else here
            # would ever surface.
            log_false_capability_claims(
                "".join(answer_parts),
                exposed_tools=exposed_tool_names,
                surface=surface,
                user_id=user_id,
                session_id=session_id or "",
            )
            # The full stream was delivered to the client: mark the turn done so the
            # pending completion task becomes a no-op. Reached only when the loop finishes
            # without the client disconnecting (a disconnect cancels the generator before
            # here, leaving the turn 'generating' for the task to finish + push).
            #
            # For a desktop turn the canonical answer is stored FIRST. The recovery record
            # is the only durable copy until that write lands, so releasing it earlier
            # would mean one Firestore hiccup loses the answer outright. If the canonical
            # write fails, or the turn produced no answer at all, the record stays
            # 'generating' and the delayed task (or the per-minute backstop) repairs it.
            release_recovery = True
            if desktop_conversation_id and client_message_id:
                answer_text = "".join(answer_parts)
                if stream_error_seen or not answer_text.strip():
                    release_recovery = False
                else:
                    reminder_payload = done_metadata.get("reminder")
                    transcript_result = await desktop_chat_store.put_assistant_message(
                        user_id,
                        desktop_conversation_id,
                        client_message_id,
                        text=answer_text,
                        status=desktop_chat_store.STATUS_COMPLETE,
                        reminder=(
                            reminder_payload if isinstance(reminder_payload, dict) else None
                        ),
                    )
                    release_recovery = (
                        transcript_result != desktop_chat_store.RESULT_ERROR
                    )
                    if release_recovery:
                        # Fire and forget, deliberately NOT awaited: the turn is
                        # already answered and the stream is closing, so making
                        # the user wait on a summarization call would charge them
                        # latency for work that only benefits a later turn.
                        # maybe_compact is a no-op unless enough has aged out.
                        asyncio.create_task(
                            text_compaction.maybe_compact(
                                user_id, desktop_conversation_id
                            ),
                            name=f"chat-compact-{desktop_conversation_id[:8]}",
                        )
            if surface == "app" and session_id:
                # Mobile sibling of the desktop compaction task above: fold
                # messages that aged past the client's verbatim window into the
                # rolling summary. Same rules — fired, never awaited, no-op
                # until enough history has aged out of the window.
                asyncio.create_task(
                    mobile_compaction.maybe_compact(user_id, session_id),
                    name=f"chat-mobile-compact-{session_id[:8]}",
                )
            # By stream end the recording task has long finished; awaiting it here
            # (before mark_client_complete) also guarantees the turn doc exists
            # before it is marked complete.
            completion_task_name: str | None = None
            if completion_setup is not None:
                completion_task_name = await completion_setup
            if client_message_id and release_recovery:
                await turn_store.mark_client_complete(user_id, client_message_id)
            if completion_task_name and release_recovery:
                await asyncio.to_thread(
                    get_task_scheduler().cancel_task,
                    completion_task_name,
                )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start_ts) * 1000)
            logger.exception(
                "Chat: stream failed",
                {
                    "user_id": user_id,
                    "duration_ms": duration_ms,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            _err = json.dumps({
                "type": "error",
                "message": CHAT_TEMPORARILY_UNAVAILABLE_MESSAGE,
            })
            yield f"data: {_err}\n\n"
        finally:
            reset_trace_context(trace_token)
            yield "data: [DONE]\n\n"

    return StreamingResponse(_generate(), media_type="text/event-stream", headers=_sse_headers)
