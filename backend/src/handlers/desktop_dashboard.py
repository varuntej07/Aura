"""Authenticated dashboard projections for Aura Desktop."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services import voice_session_fields as vf
from ..services.drafts import store as draft_store
from ..services.entitlement import (
    FREE_TIER_DAILY_OUTBOUND_DRAFT_LIMIT,
    FREE_TIER_DAILY_VOICE_SECONDS,
    EntitlementUnavailableError,
    ensure_entitlement_doc,
    resolve_effective_tier,
)
from ..services.firebase import admin_firestore
from ..services.memory.atom_store import list_atoms
from ..services.request_auth import resolve_user_id_from_request


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
    if isinstance(value, str) and value:
        return value
    return None


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _limit(request: Request, name: str, default: int, maximum: int) -> int:
    try:
        value = int(request.query_params.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, maximum))


def _voice_sessions(uid: str, limit: int) -> list[dict[str, Any]]:
    """Read the existing voice-session collection scoped to one owner."""
    collection = admin_firestore().collection("users").document(uid).collection("voice_sessions")
    rows: list[dict[str, Any]] = []
    for snap in collection.order_by("started_at", direction="DESCENDING").limit(limit).stream():
        data = snap.to_dict() or {}
        if data.get("archived"):
            continue
        data["id"] = snap.id
        rows.append(data)
    return rows


async def _recent_voice_sessions(uid: str, limit: int) -> list[dict[str, Any]]:
    try:
        return await asyncio.to_thread(_voice_sessions, uid, limit)
    except Exception as exc:
        logger.warn(
            "desktop_dashboard: voice session read failed",
            {"user_id": uid, "error": str(exc)},
        )
        return []


def _duration_seconds(session: dict[str, Any]) -> float | None:
    value = session.get("duration_ms")
    if isinstance(value, (int, float)):
        return value / 1000
    return None


def _is_desktop_session(session: dict[str, Any]) -> bool:
    return session.get(vf.SURFACE) == "desktop"


async def handle_desktop_home_stats(request: Request) -> JSONResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    sessions = [row for row in await _recent_voice_sessions(uid, 100) if _is_desktop_session(row)]
    latest = sessions[0] if sessions else None
    cutoff = datetime.now(UTC) - timedelta(days=7)
    weekly = sum(1 for row in sessions if (_as_datetime(row.get("started_at")) or cutoff) >= cutoff)
    return JSONResponse(
        {
            "last_used_at": _iso(latest.get("started_at")) if latest else None,
            "last_session_seconds": _duration_seconds(latest) if latest else None,
            "sessions_this_week": weekly,
        }
    )


def _voice_item(row: dict[str, Any]) -> dict[str, Any] | None:
    timestamp = _iso(row.get("started_at"))
    if not timestamp:
        return None
    recap = str(row.get(vf.RECAP) or row.get("summary") or "").strip()
    return {
        "id": str(row.get("id", "")),
        "kind": "voice",
        "title": "Voice conversation",
        "subtitle": recap[:200] or None,
        "timestamp": timestamp,
    }


def _draft_item(row: dict[str, Any]) -> dict[str, Any] | None:
    timestamp = _iso(row.get("updated_at") or row.get("created_at"))
    if not timestamp:
        return None
    channel = str(row.get("channel") or "message").replace("_", " ")
    return {
        "id": str(row.get("draft_id", "")),
        "kind": "draft",
        "title": f"Draft: {channel}",
        "subtitle": str(row.get("text") or "").strip()[:200] or None,
        "timestamp": timestamp,
    }


def _saved_item(row: dict[str, Any]) -> dict[str, Any] | None:
    timestamp = _iso(row.get("last_seen"))
    if not timestamp:
        return None
    return {
        "id": str(row.get("id", "")),
        "kind": "saved",
        "title": "Saved memory",
        "subtitle": str(row.get("text") or "").strip()[:200] or None,
        "timestamp": timestamp,
    }


async def handle_desktop_activity(request: Request) -> JSONResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    limit = _limit(request, "limit", 8, 50)
    sessions, drafts, atoms = await asyncio.gather(
        _recent_voice_sessions(uid, limit),
        draft_store.list_drafts(uid, limit=limit),
        list_atoms(uid, limit=limit),
    )
    items = [
        item
        for item in (
            *(_voice_item(row) for row in sessions if _is_desktop_session(row)),
            *(_draft_item(row) for row in drafts),
            *(_saved_item(row) for row in atoms),
        )
        if item is not None
    ]
    items.sort(key=lambda item: item["timestamp"], reverse=True)
    return JSONResponse({"items": items[:limit]})


def _decode_cursor(value: str | None) -> str | None:
    if not value:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(value.encode()).decode())
        return str(payload["id"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError):
        return None


def _encode_cursor(session_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps({"id": session_id}).encode()).decode()


async def handle_desktop_conversations(request: Request) -> JSONResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    limit = _limit(request, "limit", 30, 100)
    cursor_id = _decode_cursor(request.query_params.get("cursor"))
    sessions = [row for row in await _recent_voice_sessions(uid, 101) if _is_desktop_session(row)]
    if cursor_id:
        try:
            sessions = sessions[[row.get("id") for row in sessions].index(cursor_id) + 1 :]
        except ValueError:
            sessions = []
    page, remaining = sessions[:limit], sessions[limit:]
    items = []
    for row in page:
        started_at = _iso(row.get("started_at"))
        if not started_at:
            continue
        recap = str(row.get(vf.RECAP) or row.get("summary") or "").strip()
        items.append(
            {
                "id": str(row.get("id", "")),
                "title": "Voice conversation",
                "preview": recap[:200] or None,
                "started_at": started_at,
                "duration_seconds": _duration_seconds(row),
            }
        )
    body: dict[str, Any] = {"items": items}
    if remaining and page:
        body["next_cursor"] = _encode_cursor(str(page[-1].get("id", "")))
    return JSONResponse(body)


async def handle_desktop_saved(request: Request) -> JSONResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    atoms = await list_atoms(uid, limit=_limit(request, "limit", 50, 200))
    items = [
        {
            "id": row["id"],
            "label": row["text"],
            "value": None,
            "saved_at": _iso(row["last_seen"]),
        }
        for row in atoms
        if _iso(row.get("last_seen"))
    ]
    return JSONResponse({"items": items})


def _usage_doc(uid: str, doc_id: str) -> dict[str, Any]:
    snap = (
        admin_firestore()
        .collection("users")
        .document(uid)
        .collection("usage")
        .document(doc_id)
        .get()
    )
    return snap.to_dict() or {}


async def handle_desktop_usage(request: Request) -> JSONResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        entitlement = await ensure_entitlement_doc(uid)
    except EntitlementUnavailableError:
        return JSONResponse({"error": "entitlement_unavailable"}, status_code=503)

    voice, drafts = await asyncio.gather(
        asyncio.to_thread(_usage_doc, uid, "daily_voice"),
        asyncio.to_thread(_usage_doc, uid, "daily_outbound_draft"),
        return_exceptions=True,
    )
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    voice_used = (
        int(voice.get("seconds", 0))
        if isinstance(voice, dict) and voice.get("date") == today
        else 0
    )
    drafts_used = (
        int(drafts.get("count", 0))
        if isinstance(drafts, dict) and drafts.get("date") == today
        else 0
    )
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    unlimited = resolve_effective_tier(entitlement) != "free"
    return JSONResponse(
        {
            "voice_minutes_used": voice_used / 60,
            "voice_minutes_limit": (None if unlimited else FREE_TIER_DAILY_VOICE_SECONDS / 60),
            "drafts_used": drafts_used,
            "drafts_limit": None if unlimited else FREE_TIER_DAILY_OUTBOUND_DRAFT_LIMIT,
            "period_start": start.isoformat(),
            "period_end": (start + timedelta(days=1)).isoformat(),
        }
    )
