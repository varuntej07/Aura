"""GET/DELETE /history/sessions — voice session history for the desktop "History" screen.

Exposes what already exists in Firestore (users/{uid}/voice_sessions, written by
voice_session_summarizer.py) to a client for the first time; this module writes
nothing new. Same shape as handlers/aura.py's memory endpoints: read-only list + one
lazy detail fetch + per-item hard delete, auth via the same Firebase ID token check,
not consent-gated for the same reason handle_get_memory isn't — a user can always see
and delete their own stored data.

The list query intentionally has no Firestore-side equality filter (only a single-field
order_by, which Firestore always covers automatically in either direction) rather than
`.where("archived", "==", False)` — the latter would need a composite index whose
existence I can't confirm is deployed, and a missing-index 400 is exactly the silent-
failure mode this backend already warns about elsewhere (turn_store.list_stuck_turns).
`archived` docs are filtered out here in Python instead; in practice this collection
holds almost no archived docs at any moment since the archive cycle deletes them
immediately after rolling them up (voice_session_summarizer._cleanup_archived_docs).
"""

from __future__ import annotations

import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services.firebase import admin_firestore
from ..services.request_auth import resolve_user_id_from_request

_SESSIONS_COLLECTION = "voice_sessions"
_STATE_COLLECTION = "voice_session_state"
_ARCHIVE_DOC = "archive"

# Matches voice_session_summarizer._ACTIVE_SESSION_THRESHOLD — the collection never
# holds meaningfully more than this many non-archived docs, so this is a safety cap,
# not a real pagination limit.
_LIST_LIMIT = 30


def _sessions_collection(uid: str):
    return (
        admin_firestore()
        .collection("users").document(uid)
        .collection(_SESSIONS_COLLECTION)
    )


def _archive_ref(uid: str):
    return (
        admin_firestore()
        .collection("users").document(uid)
        .collection(_STATE_COLLECTION).document(_ARCHIVE_DOC)
    )


def _session_summary_row(doc_id: str, data: dict) -> dict:
    """Lightweight projection for the list view — deliberately excludes raw_turns
    (the full transcript) to keep the list payload small; see
    handle_get_session_detail for the full transcript, fetched lazily per row."""
    return {
        "session_id": doc_id,
        "started_at": data.get("started_at", ""),
        "ended_at": data.get("ended_at", ""),
        "total_duration": data.get("total_duration", ""),
        "num_of_turns": data.get("num_of_turns", 0),
        "num_of_tool_calls": data.get("num_of_tool_calls", 0),
        "summary": data.get("summary", ""),
        "screen_sight_frame_count": data.get("screen_sight_frame_count", 0),
    }


async def handle_list_sessions(request: Request) -> JSONResponse:
    """GET /history/sessions — recent voice sessions, newest first, plus a rolled-up
    "earlier history" entry if older sessions have already been archived. Read-only,
    never writes. Fails closed (empty list) rather than raising, matching every other
    read path in this file."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    def _read_sessions() -> list[dict]:
        query = (
            _sessions_collection(user_id)
            .order_by("started_at", direction="DESCENDING")
            .limit(_LIST_LIMIT)
        )
        rows = []
        for snap in query.stream():
            data = snap.to_dict() or {}
            if data.get("archived"):
                continue
            rows.append(_session_summary_row(snap.id, data))
        return rows

    def _read_archive() -> dict | None:
        snap = _archive_ref(user_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        if not data.get("sessions_archived_count"):
            return None
        return {
            "archive_summary": data.get("archive_summary", ""),
            "sessions_archived_count": data.get("sessions_archived_count", 0),
            "oldest_archived_session_at": data.get("oldest_archived_session_at", ""),
            "newest_archived_session_at": data.get("newest_archived_session_at", ""),
        }

    try:
        sessions, archive = await asyncio.gather(
            asyncio.to_thread(_read_sessions),
            asyncio.to_thread(_read_archive),
        )
    except Exception as exc:
        logger.warn("History: list sessions failed", {"user_id": user_id, "error": str(exc)})
        return JSONResponse({"sessions": [], "archive": None})

    logger.info("History: listed", {"user_id": user_id, "total": len(sessions)})
    return JSONResponse({"sessions": sessions, "archive": archive})


async def handle_get_session_detail(request: Request, session_id: str) -> JSONResponse:
    """GET /history/sessions/{session_id} — one session's full transcript, fetched
    lazily only when a user expands a row in the history list."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    session_id = (session_id or "").strip()
    if not session_id:
        return JSONResponse({"error": "Missing session id."}, status_code=400)

    def _read() -> dict | None:
        snap = _sessions_collection(user_id).document(session_id).get()
        return (snap.to_dict() or {}) if snap.exists else None

    try:
        data = await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("History: get session detail failed", {
            "user_id": user_id, "session_id": session_id, "error": str(exc),
        })
        return JSONResponse({"error": "Failed to load session."}, status_code=500)

    if data is None:
        return JSONResponse({"error": "Not found."}, status_code=404)

    return JSONResponse({
        "session_id": session_id,
        "started_at": data.get("started_at", ""),
        "ended_at": data.get("ended_at", ""),
        "total_duration": data.get("total_duration", ""),
        "summary": data.get("summary", ""),
        "raw_turns": data.get("raw_turns", []),
    })


async def _delete_session(uid: str, session_id: str) -> bool:
    try:
        await asyncio.to_thread(_sessions_collection(uid).document(session_id).delete)
        return True
    except Exception as exc:
        logger.warn("History: delete failed", {"user_id": uid, "error": str(exc)})
        return False


async def handle_delete_session(request: Request, session_id: str) -> JSONResponse:
    """DELETE /history/sessions/{session_id} — forget one voice session. Always
    allowed for the owner, mirrors handlers/aura.py's handle_delete_memory: hard
    delete, no tombstone."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    session_id = (session_id or "").strip()
    if not session_id:
        return JSONResponse({"error": "Missing session id."}, status_code=400)

    ok = await _delete_session(user_id, session_id)
    logger.info("History: deleted", {"user_id": user_id, "session_id": session_id, "ok": ok})
    return JSONResponse({"ok": ok})
