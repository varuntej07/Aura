"""First-run desktop attribution saved on the authenticated user profile."""

from __future__ import annotations

import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse

from ..services.firebase import admin_firestore
from ..services.request_auth import resolve_user_id_from_request

_FIELDS = ("where_heard", "where_heard_other", "role", "role_other")


async def handle_desktop_profile(request: Request) -> JSONResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)
    profile: dict[str, str | None] = {}
    for field in _FIELDS:
        value = body.get(field)
        if value is not None and not isinstance(value, str):
            return JSONResponse({"error": f"{field} must be a string or null."}, status_code=400)
        profile[field] = value.strip()[:500] if isinstance(value, str) else None
    user_ref = admin_firestore().collection("users").document(uid)
    await asyncio.to_thread(user_ref.set, profile, merge=True)
    return JSONResponse({"ok": True})
