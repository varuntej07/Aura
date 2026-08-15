"""
POST /devices/register -> register or refresh an FCM device token.

Called by the Flutter app:
  • After sign-in (initial registration)
  • Whenever FirebaseMessaging.onTokenRefresh fires
"""

from __future__ import annotations

import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services.fcm_token_registry import register_token
from ..services.notifications.welcome import maybe_send_welcome_notification
from ..services.request_auth import resolve_user_id_from_request

VALID_PLATFORMS = frozenset({"android", "ios", "web"})


async def register_device(request: Request) -> JSONResponse:
    """Register or refresh an FCM token for the authenticated user"""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        logger.warn("register_device: rejected, missing user_id", {
            "client_ip": request.client.host if request.client else "unknown",
        })
        return JSONResponse({"error": "Unauthorized: valid Firebase ID token required."}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

    token = str(body.get("token", "") or "").strip()
    if not token:
        return JSONResponse({"error": "token is required."}, status_code=400)

    # Sanity-check length; FCM tokens are typically ~163 chars.
    if len(token) > 4096:
        return JSONResponse({"error": "token is too long."}, status_code=400)

    platform = str(body.get("platform", "android") or "android").strip().lower()
    if platform not in VALID_PLATFORMS:
        return JSONResponse(
            {"error": f"platform must be one of: {', '.join(sorted(VALID_PLATFORMS))}."},
            status_code=400,
        )

    # Whether this device can actually ring an alarm (exact-alarm access granted).
    # Three states, and the distinction matters: True and False are reports from a
    # client that knows, absent is a client that has never heard of alarms. Only an
    # explicit bool is accepted — coercing here would let a truthy string claim a
    # capability the device does not have, and the cost of that lie is Buddy
    # promising a 3 AM wake-up that never comes.
    alarm_capable = body.get("alarm_capable")
    if alarm_capable is not None and not isinstance(alarm_capable, bool):
        return JSONResponse(
            {"error": "alarm_capable must be a boolean."}, status_code=400
        )

    await asyncio.to_thread(
        register_token, user_id, token, platform, alarm_capable=alarm_capable
    )

    logger.info("register_device: token registered", {
        "user_id": user_id,
        "platform": platform,
        "alarm_capable": alarm_capable,
        "token_preview": token[:20],
    })

    # Best-effort: the one-time claim inside already no-ops on every call after
    # the first, but a failure here (Firestore blip, FCM outage) must never turn
    # a successful token registration into a 500.
    try:
        await maybe_send_welcome_notification(user_id)
    except Exception as exc:
        logger.exception("register_device: welcome notification failed", {
            "user_id": user_id,
            "error": str(exc),
        })

    return JSONResponse({"ok": True, "platform": platform}, status_code=200)
