"""Authenticated read endpoints for the canonical Desktop chat transcript.

Read-only. Every UID comes from the verified Firebase token, never from the body or the
query string, so one account can never address another's conversations. Responses are
bounded, cursor-paginated, and carry only the allowlisted projection from
services/desktop_chat_store; a backend exception is never echoed to the client.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services import desktop_chat_store
from ..services.chat_completion import turn_store
from ..services.request_auth import resolve_user_id_from_request

_DEFAULT_SESSION_LIMIT = 20
_DEFAULT_MESSAGE_LIMIT = 100
_DEFAULT_PENDING_LIMIT = 20
_MAX_PENDING_LIMIT = 50


def _limit(request: Request, key: str, fallback: int) -> int:
    raw = request.query_params.get(key)
    if raw is None or not str(raw).strip():
        return fallback
    return int(raw)


async def handle_list_sessions(request: Request) -> JSONResponse:
    """GET /desktop/chat/sessions: recent conversations, most recently active first."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    cursor = str(request.query_params.get("cursor", "")).strip()
    try:
        items, next_cursor = await desktop_chat_store.list_sessions(
            user_id,
            cursor=cursor,
            limit=_limit(request, "limit", _DEFAULT_SESSION_LIMIT),
        )
    except (ValueError, desktop_chat_store.InvalidCursorError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.warn("desktop chat: session list failed", {
            "user_id": user_id, "error_type": type(exc).__name__,
        })
        return JSONResponse({"error": "Temporarily unavailable."}, status_code=503)

    logger.info("desktop chat: sessions listed", {
        "user_id": user_id, "count": len(items), "has_more": next_cursor is not None,
    })
    return JSONResponse({"items": items, "next_cursor": next_cursor})


async def handle_get_session(request: Request, conversation_id: str) -> JSONResponse:
    """GET /desktop/chat/sessions/{conversation_id}: one conversation and the newest page
    of its messages, in send order.

    ``?before=`` walks backwards through older pages; the response's ``older_cursor`` is
    null once the start of the conversation is reached.
    """
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    before = str(request.query_params.get("before", "")).strip()
    try:
        session = await desktop_chat_store.get_session(user_id, conversation_id)
        if session is None:
            return JSONResponse({"error": "Unknown conversation."}, status_code=404)
        items, older_cursor = await desktop_chat_store.list_messages(
            user_id,
            conversation_id,
            before=before,
            limit=_limit(request, "limit", _DEFAULT_MESSAGE_LIMIT),
        )
    except (ValueError, desktop_chat_store.InvalidCursorError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.warn("desktop chat: session read failed", {
            "user_id": user_id, "error_type": type(exc).__name__,
        })
        return JSONResponse({"error": "Temporarily unavailable."}, status_code=503)

    logger.info("desktop chat: session loaded", {
        "user_id": user_id, "count": len(items), "has_older": older_cursor is not None,
    })
    return JSONResponse({
        "session": session, "items": items, "older_cursor": older_cursor,
    })


async def handle_list_pending(request: Request) -> JSONResponse:
    """GET /desktop/chat/pending: turns the backend is still finishing.

    The client pairs this with the transcript: a turn listed here whose assistant message
    has not landed yet is genuinely still running in the background, and one that appears
    in neither place was never accepted. That distinction is what stops the UI from
    inventing a reply for a message that never arrived.
    """
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        limit = _limit(request, "limit", _DEFAULT_PENDING_LIMIT)
    except ValueError:
        return JSONResponse({"error": "invalid limit"}, status_code=400)
    if limit < 1 or limit > _MAX_PENDING_LIMIT:
        return JSONResponse(
            {"error": f"limit must be between 1 and {_MAX_PENDING_LIMIT}"},
            status_code=400,
        )

    try:
        items = await turn_store.list_recent_turns(user_id, limit=limit)
    except Exception as exc:
        logger.warn("desktop chat: pending list failed", {
            "user_id": user_id, "error_type": type(exc).__name__,
        })
        return JSONResponse({"error": "Temporarily unavailable."}, status_code=503)

    logger.info("desktop chat: pending listed", {
        "user_id": user_id, "count": len(items),
    })
    return JSONResponse({"items": items})
