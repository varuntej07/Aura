"""Authenticated read and acknowledgement endpoints for the desktop outbox."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services.notifications import desktop_outbox
from ..services.request_auth import resolve_user_id_from_request


async def handle_list(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    cursor = str(request.query_params.get("cursor", "")).strip()
    try:
        limit = int(request.query_params.get("limit", "50"))
        items, next_cursor = await desktop_outbox.list_notifications(
            user_id,
            cursor=cursor,
            limit=limit,
        )
    except (ValueError, desktop_outbox.InvalidCursorError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.warn("desktop notifications: list failed", {
            "user_id": user_id,
            "error_type": type(exc).__name__,
        })
        return JSONResponse({"error": "Temporarily unavailable."}, status_code=503)

    logger.info("desktop notifications: listed", {
        "user_id": user_id,
        "count": len(items),
        "has_more": next_cursor is not None,
    })
    return JSONResponse({"items": items, "next_cursor": next_cursor})


async def handle_acknowledge(
    request: Request,
    notification_id: str,
) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    notification_id = notification_id.strip()
    if not notification_id or len(notification_id) > desktop_outbox.MAX_ID_LENGTH:
        return JSONResponse({"error": "Invalid notification id."}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

    status = str(body.get("status") or "").strip()
    raw_action = body.get("action")
    action = str(raw_action).strip() if raw_action is not None else None
    try:
        found = await desktop_outbox.acknowledge(
            user_id,
            notification_id,
            status=status,
            action=action,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.warn("desktop notifications: acknowledgement failed", {
            "user_id": user_id,
            "notification_id": notification_id,
            "error_type": type(exc).__name__,
        })
        return JSONResponse({"error": "Temporarily unavailable."}, status_code=503)

    if not found:
        return JSONResponse({"error": "Unknown notification."}, status_code=404)
    logger.info("desktop notifications: acknowledged", {
        "user_id": user_id,
        "notification_id": notification_id,
        "status": status,
    })
    return JSONResponse({"ok": True})
