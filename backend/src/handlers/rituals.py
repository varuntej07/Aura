"""HTTP surface for recurring rituals.

These routes exist because a home-deck card creates the thing it promises the
moment it is confirmed, under the user's finger, rather than opening a chat and
asking the model to run a tool. That makes the card honest (what you tapped is
what happened) and fast (no turn, no tokens), and it is why creation is validated
server-side: the fire instant, the timezone and the daylight-saving fold are not
decisions a device should be making.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services import rituals
from ..services.request_auth import resolve_user_id_from_request


def _clean_id(value: Any) -> str:
    """Accept only an opaque client-generated id, used verbatim as the document id."""
    text = str(value or "").strip()
    if not text or len(text) > 64:
        return ""
    return text if all(ch.isalnum() or ch in "-_" for ch in text) else ""


def _public(ritual: dict[str, Any]) -> dict[str, Any]:
    """The fields a client needs. Internal generation state never leaves the server."""
    return {
        "ritual_id": ritual.get("id", ""),
        "content_kind": ritual.get("content_kind", ""),
        "title": ritual.get("title", ""),
        "freq": ritual.get("freq", ""),
        "hour": ritual.get("hour", 0),
        "minute": ritual.get("minute", 0),
        "weekdays": ritual.get("weekdays", []),
        "timezone": ritual.get("timezone", ""),
        "status": ritual.get("status", ""),
        "next_fire_at": ritual.get("next_fire_at", ""),
        "next_fire_local": ritual.get("next_fire_local", ""),
        "fire_count": ritual.get("fire_count", 0),
    }


async def handle_create_ritual(request: Request) -> JSONResponse:
    """POST /rituals — create a recurring ritual and arm its first occurrence.

    Idempotent on ``client_ritual_id``: a double-tapped card addresses the same
    document twice instead of scheduling two daily pushes.
    """
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid request."}, status_code=400)

    ritual_id = _clean_id(body.get("client_ritual_id"))
    if not ritual_id:
        return JSONResponse({"error": "Invalid request."}, status_code=400)

    schedule = body.get("schedule")
    if not isinstance(schedule, dict):
        return JSONResponse({"error": "Invalid schedule."}, status_code=400)

    try:
        weekdays = [int(day) for day in (schedule.get("weekdays") or [])]
        stored = await rituals.create_ritual(
            user_id,
            ritual_id=ritual_id,
            content_kind=str(body.get("content_kind") or ""),
            title=str(body.get("title") or ""),
            freq=str(schedule.get("freq") or "daily"),
            hour=int(schedule.get("hour", -1)),
            minute=int(schedule.get("minute", -1)),
            weekdays=weekdays,
        )
    except rituals.RitualLimitReached as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except (TypeError, ValueError) as exc:
        # Both the schedule validator and the timezone reader raise ValueError with
        # copy written for the user, so it is safe to surface verbatim.
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.error("rituals: create failed", {
            "user_id": user_id, "ritual_id": ritual_id, "error": str(exc),
        })
        return JSONResponse({"error": "Temporarily unavailable"}, status_code=503)

    created = bool(stored.get("created", False))
    return JSONResponse(
        {**_public(stored), "created": created},
        status_code=201 if created else 200,
    )


async def handle_list_rituals(request: Request) -> JSONResponse:
    """GET /rituals — the user's active and paused rituals."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        items = await rituals.list_rituals(user_id)
    except Exception as exc:
        logger.error("rituals: list failed", {"user_id": user_id, "error": str(exc)})
        return JSONResponse({"error": "Temporarily unavailable"}, status_code=503)
    return JSONResponse({"rituals": [_public(item) for item in items]})


async def handle_update_ritual(request: Request, ritual_id: str) -> JSONResponse:
    """PATCH /rituals/{id} — pause or resume, cancelling or re-arming the occurrence."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = None
    status = str((body or {}).get("status") or "").strip().lower()

    try:
        stored = await rituals.set_ritual_status(user_id, _clean_id(ritual_id), status)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.error("rituals: update failed", {
            "user_id": user_id, "ritual_id": ritual_id, "error": str(exc),
        })
        return JSONResponse({"error": "Temporarily unavailable"}, status_code=503)
    if stored is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse(_public(stored))


async def handle_delete_ritual(request: Request, ritual_id: str) -> JSONResponse:
    """DELETE /rituals/{id} — stop repeating, including the already-armed occurrence.

    Stopping has to actually stop. Leaving the armed occurrence pending would send
    one more push after the user said no, which is the version of this feature that
    gets the app uninstalled.
    """
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        stored = await rituals.set_ritual_status(
            user_id,
            _clean_id(ritual_id),
            rituals.STATUS_DELETED,
        )
    except Exception as exc:
        logger.error("rituals: delete failed", {
            "user_id": user_id, "ritual_id": ritual_id, "error": str(exc),
        })
        return JSONResponse({"error": "Temporarily unavailable"}, status_code=503)
    if stored is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse({"deleted": True})
