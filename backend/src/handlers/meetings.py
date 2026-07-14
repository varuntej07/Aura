"""/meetings/* - meeting-notes capture ingest and delivery.

Desktop-only surface for now. Auth via the same Firebase ID token check as
handlers/drafts.py. The claim route is the gate: it answers 402 with the exact
{"detail": {"code", "seconds_until_reset"}} shape the desktop already parses
for the voice cap (src/lib/voice.ts), so the client-side mirror is trivial.

Ordering note for main.py: /meetings/recent must be registered BEFORE
/meetings/{meeting_id}, or "recent" resolves as a meeting id (same rule as
/memories/callback).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services.entitlement import EntitlementUnavailableError, get_user_effective_tier
from ..services.meetings import fields as F
from ..services.meetings import gcs_audio, store, synthesis, tasks
from ..services.request_auth import resolve_user_id_from_request

_ENTITLEMENT_UNAVAILABLE = JSONResponse(
    {"error": "Entitlement temporarily unavailable."}, status_code=503,
)


async def _json_body(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _meeting_response(meeting: dict[str, Any]) -> dict[str, Any]:
    """The client-facing shape of one meeting. device_id and per-segment
    offsets stay server-side (provenance only), matching how drafts omits
    session_id."""
    return {
        "meeting_id": meeting.get("meeting_id", ""),
        F.EVENT_ID: meeting.get(F.EVENT_ID, ""),
        F.TITLE: meeting.get(F.TITLE, ""),
        F.STATUS: meeting.get(F.STATUS, ""),
        F.CAP_MINUTES: meeting.get(F.CAP_MINUTES, 0),
        F.START_TIME: meeting.get(F.START_TIME, ""),
        F.END_TIME: meeting.get(F.END_TIME, ""),
        F.CREATED_AT: meeting.get(F.CREATED_AT, ""),
        F.UPDATED_AT: meeting.get(F.UPDATED_AT, ""),
        F.NOTE: meeting.get(F.NOTE) or None,
    }


async def handle_claim(request: Request) -> JSONResponse:
    """POST /meetings/claim - the capture gate. Charges the monthly counter
    transactionally on success; idempotent for a same-device rejoin."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    body = await _json_body(request)
    if body is None:
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)
    event_id = str(body.get("event_id") or "").strip()
    if not event_id:
        return JSONResponse({"error": "Missing event_id."}, status_code=400)
    device_id = str(body.get("device_id") or "").strip() or "unknown"

    try:
        effective_tier = await get_user_effective_tier(user_id)
    except EntitlementUnavailableError:
        return _ENTITLEMENT_UNAVAILABLE

    try:
        result = await store.claim_meeting(
            user_id,
            event_id=event_id,
            title=str(body.get("title") or "")[:300],
            start_time=str(body.get("start_time") or ""),
            end_time=str(body.get("end_time") or ""),
            device_id=device_id[:100],
            effective_tier=effective_tier,
        )
    except Exception as exc:
        # Fails closed: an allowed claim commits real STT+LLM spend, so an
        # outage denies with a retryable status instead of guessing.
        logger.warn("meetings: claim failed", {"user_id": user_id, "error": str(exc)})
        return JSONResponse({"error": "Claim temporarily unavailable."}, status_code=503)

    if result.denied_cap:
        return JSONResponse(
            {"detail": {
                "code": F.MEETING_CAP_CODE,
                "seconds_until_reset": result.seconds_until_reset,
            }},
            status_code=402,
        )
    if result.denied_conflict:
        return JSONResponse(
            {"detail": {"code": F.MEETING_CONFLICT_CODE}}, status_code=409,
        )
    return JSONResponse({
        "meeting_id": result.meeting_id,
        "cap_minutes": result.cap_minutes,
        "max_capture_minutes": F.MAX_CAPTURE_MINUTES,
        "rejoined": result.rejoined,
    })


async def handle_upload_segment(
    request: Request, meeting_id: str, seq: int,
) -> JSONResponse:
    """POST /meetings/{meeting_id}/segments/{seq} - one raw FLAC segment body.
    Offsets ride headers so the body stays pure audio. Re-uploading the same
    segment is idempotent (GCS overwrite + ArrayUnion no-op)."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if seq < 0 or seq >= F.MAX_SEGMENTS_PER_MEETING:
        return JSONResponse({"error": "Segment seq out of range."}, status_code=400)

    # Offsets are client-supplied and feed the synthesis cap, so they get hard
    # range checks here AND a cumulative-duration cap in the worker - neither
    # alone survives a modified client.
    try:
        start_ms = int(request.headers["X-Segment-Start-Ms"])
        duration_ms = int(request.headers["X-Segment-Duration-Ms"])
    except (KeyError, ValueError):
        return JSONResponse(
            {"error": "Missing or invalid X-Segment-Start-Ms/X-Segment-Duration-Ms."},
            status_code=400,
        )
    if not (0 <= start_ms <= F.MAX_SEGMENT_START_MS):
        return JSONResponse({"error": "Segment start out of range."}, status_code=400)
    if not (0 < duration_ms <= F.MAX_SEGMENT_DURATION_MS):
        return JSONResponse({"error": "Segment duration out of range."}, status_code=400)
    incomplete = request.headers.get("X-Segment-Incomplete", "") == "true"

    try:
        meeting = await store.get_meeting(user_id, meeting_id)
    except Exception as exc:
        logger.warn("meetings: upload ownership check failed", {
            "user_id": user_id, "meeting_id": meeting_id, "error": str(exc),
        })
        return JSONResponse({"error": "Temporarily unavailable."}, status_code=503)
    if meeting is None:
        return JSONResponse({"error": "Unknown meeting."}, status_code=404)
    if meeting.get(F.STATUS) not in (F.STATUS_CAPTURING, F.STATUS_UPLOADED):
        return JSONResponse(
            {"error": f"Meeting is {meeting.get(F.STATUS)}, not accepting audio."},
            status_code=409,
        )
    existing = meeting.get(F.SEGMENTS, [])
    already_known = any(int(seg.get("seq", -1)) == seq for seg in existing)
    if not already_known and len(existing) >= F.MAX_SEGMENTS_PER_MEETING:
        return JSONResponse({"error": "Too many segments."}, status_code=413)

    data = await request.body()
    if not data:
        return JSONResponse({"error": "Empty segment body."}, status_code=400)
    if len(data) > F.MAX_SEGMENT_BYTES:
        return JSONResponse({"error": "Segment too large."}, status_code=413)

    try:
        await gcs_audio.upload_segment(user_id, meeting_id, seq, data)
        await store.append_segment_meta(
            user_id, meeting_id, seq=seq, start_ms=start_ms,
            duration_ms=duration_ms, incomplete=incomplete,
        )
    except Exception as exc:
        logger.warn("meetings: segment upload failed", {
            "user_id": user_id, "meeting_id": meeting_id, "seq": seq,
            "error": str(exc),
        })
        return JSONResponse({"error": "Upload failed."}, status_code=503)
    return JSONResponse({"ok": True})


async def handle_complete(request: Request, meeting_id: str) -> JSONResponse:
    """POST /meetings/{meeting_id}/complete - capture finished, hand off to
    synthesis. The enqueue happens synchronously before the response (Cloud
    Run durability rule) and is idempotent via the deterministic task name;
    a client retry of an already-completed meeting answers 200."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    body = await _json_body(request) or {}

    try:
        meeting = await store.get_meeting(user_id, meeting_id)
        if meeting is None:
            return JSONResponse({"error": "Unknown meeting."}, status_code=404)

        # A capture that never produced a segment (mic init failure, sub-2s
        # call) has nothing to synthesize: resolve it to "failed" instead of
        # enqueueing a job that would burn a task run to conclude the same,
        # or worse, leaving the doc "capturing" forever.
        if int(body.get("segment_count") or 0) == 0 and not meeting.get(F.SEGMENTS):
            _, status_now = await store.transition_status(
                user_id, meeting_id,
                from_statuses=(F.STATUS_CAPTURING,),
                to_status=F.STATUS_FAILED,
                extra={F.COMPLETE_REASON: str(body.get("reason") or "")[:100]},
            )
            return JSONResponse({"ok": True, "status": status_now or F.STATUS_FAILED})

        transitioned, status_now = await store.transition_status(
            user_id, meeting_id,
            from_statuses=(F.STATUS_CAPTURING,),
            to_status=F.STATUS_UPLOADED,
            extra={
                F.SEGMENT_COUNT: int(body.get("segment_count") or 0),
                F.TOTAL_DURATION_MS: int(body.get("total_duration_ms") or 0),
                F.COMPLETE_REASON: str(body.get("reason") or "")[:100],
            },
        )
        # Enqueue whenever the meeting sits at "uploaded", including the retry
        # where the transition landed earlier but the first enqueue call died.
        if transitioned or status_now == F.STATUS_UPLOADED:
            await asyncio.to_thread(tasks.enqueue_synthesis, user_id, meeting_id)
            status_now = F.STATUS_UPLOADED
    except Exception as exc:
        logger.warn("meetings: complete failed", {
            "user_id": user_id, "meeting_id": meeting_id, "error": str(exc),
        })
        return JSONResponse({"error": "Complete failed."}, status_code=503)

    return JSONResponse({"ok": True, "status": status_now})


async def handle_get_meeting(request: Request, meeting_id: str) -> JSONResponse:
    """GET /meetings/{meeting_id} - status + note poll target."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        meeting = await store.get_meeting(user_id, meeting_id)
    except Exception:
        return JSONResponse({"error": "Temporarily unavailable."}, status_code=503)
    if meeting is None:
        return JSONResponse({"error": "Unknown meeting."}, status_code=404)
    return JSONResponse(_meeting_response(meeting))


async def handle_list_recent(request: Request) -> JSONResponse:
    """GET /meetings/recent - newest first, expired rows dropped. Fails closed
    (empty list), matching the drafts read path."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        limit = int(request.query_params.get("limit", str(F.LIST_LIMIT)))
    except ValueError:
        limit = F.LIST_LIMIT

    items = await store.list_recent(user_id, limit=limit)
    return JSONResponse({"items": [_meeting_response(m) for m in items]})


async def handle_internal_synthesize(request: Request) -> JSONResponse:
    """POST /internal/meetings/synthesize - the Cloud Tasks target. Terminal
    outcomes answer 200 (the queue must stop); retryable infrastructure
    failures propagate as 500 so the queue tries again with audio intact."""
    body = await _json_body(request) or {}
    user_id = str(body.get("user_id") or "").strip()
    meeting_id = str(body.get("meeting_id") or "").strip()
    if not user_id or not meeting_id:
        return JSONResponse({"error": "Missing user_id/meeting_id."}, status_code=400)

    status = await synthesis.run_synthesis(user_id, meeting_id)
    return JSONResponse({"status": status})
