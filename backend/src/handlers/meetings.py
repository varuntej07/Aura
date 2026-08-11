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
import uuid
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services.entitlement import EntitlementUnavailableError, get_user_effective_tier
from ..services.meetings import (
    deletion,
    evidence,
    gcs_audio,
    observability,
    store,
    synthesis,
    tasks,
)
from ..services.meetings import fields as F
from ..services.request_auth import resolve_user_id_from_request

_ENTITLEMENT_UNAVAILABLE = JSONResponse(
    {"error": "Entitlement temporarily unavailable."},
    status_code=503,
)


def _classify_upload_error(exc: Exception) -> tuple[str, bool]:
    """Map a segment-upload exception to (safe failure_code, is_config_failure).
    A missing bucket is a configuration failure the deploy preflight now blocks,
    but a bucket deleted or an IAM grant revoked AFTER deploy can still surface
    here at runtime, so this stays a live classifier, not a dead branch. Both
    map the client to the same durable "recording is safe, upload deferred"
    state (upload_storage_unavailable); it just gets logged distinctly so a
    config failure can be alerted instead of buried among transient 503s."""
    from google.api_core import exceptions as gexc  # type: ignore

    if isinstance(exc, (gexc.NotFound, gexc.Forbidden)):
        return F.FAIL_UPLOAD_STORAGE_UNAVAILABLE, True
    if isinstance(exc, gexc.ServiceUnavailable):
        return F.FAIL_UPLOAD_STORAGE_UNAVAILABLE, False
    return F.FAIL_UPLOAD_STORAGE_UNAVAILABLE, False


async def _json_body(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _note_response(note: Any, *, include_transcript: bool) -> dict[str, Any] | None:
    """Public note contract with an explicit allowlist.

    Firestore documents are internal records. Keeping this projection explicit
    prevents future internal note fields from becoming accidental API fields.
    """
    if not isinstance(note, dict):
        return None

    def _strings(value: Any) -> list[str]:
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    response: dict[str, Any] = {
        "summary": note.get("summary", "") if isinstance(note.get("summary"), str) else "",
        "decisions": _strings(note.get("decisions")),
        "action_items": _strings(note.get("action_items")),
        "open_questions": _strings(note.get("open_questions")),
        "language": note.get("language", "") if isinstance(note.get("language"), str) else "",
        "one_sided": note.get("one_sided") is True,
        "partial": note.get("partial") is True,
    }
    if include_transcript:
        raw_turns = note.get(F.NOTE_TRANSCRIPT)
        response[F.NOTE_TRANSCRIPT] = (
            [
                {
                    F.TRANSCRIPT_SPEAKER: turn[F.TRANSCRIPT_SPEAKER],
                    F.TRANSCRIPT_TEXT: turn[F.TRANSCRIPT_TEXT],
                }
                for turn in raw_turns
                if isinstance(turn, dict)
                and isinstance(turn.get(F.TRANSCRIPT_SPEAKER), str)
                and isinstance(turn.get(F.TRANSCRIPT_TEXT), str)
            ]
            if isinstance(raw_turns, list)
            else []
        )
    return response


def _meeting_response(
    meeting: dict[str, Any],
    *,
    include_transcript: bool = True,
) -> dict[str, Any]:
    """The client-facing shape of one meeting. device_id and per-segment
    offsets stay server-side (provenance only), matching how drafts omits
    session_id."""
    status = meeting.get(F.STATUS, "")
    if (
        int(meeting.get(F.PROTOCOL_VERSION, 1)) >= 2
        and status == F.STATUS_READY
        and (
            not (meeting.get(F.ARTIFACTS) or {}).get("canonical")
            or meeting.get(F.QUALITY_OUTCOME) not in ("quality_passed", "verified_silence")
        )
    ):
        status = F.STATUS_NEEDS_ATTENTION
    return {
        "meeting_id": meeting.get("meeting_id", ""),
        F.EVENT_ID: meeting.get(F.EVENT_ID, ""),
        F.TITLE: meeting.get(F.TITLE, ""),
        F.STATUS: status,
        F.CAP_MINUTES: meeting.get(F.CAP_MINUTES, 0),
        F.START_TIME: meeting.get(F.START_TIME, ""),
        F.END_TIME: meeting.get(F.END_TIME, ""),
        F.CREATED_AT: meeting.get(F.CREATED_AT, ""),
        F.UPDATED_AT: meeting.get(F.UPDATED_AT, ""),
        F.NOTE: _note_response(
            meeting.get(F.NOTE),
            include_transcript=include_transcript,
        ),
        # Additive processing metadata. Old clients ignore these; new clients
        # render a stage, a safe reason, and a Retry affordance. Defaults keep
        # pre-migration docs (no metadata yet) well-formed.
        F.PROCESSING_STAGE: meeting.get(F.PROCESSING_STAGE, ""),
        F.FAILURE_CODE: meeting.get(F.FAILURE_CODE) or None,
        F.FAILURE_MESSAGE: meeting.get(F.FAILURE_MESSAGE) or None,
        F.RETRYABLE: bool(meeting.get(F.RETRYABLE, False)),
        F.ATTEMPT_COUNT: int(meeting.get(F.ATTEMPT_COUNT, 0)),
        F.LAST_ERROR_AT: meeting.get(F.LAST_ERROR_AT) or None,
        F.STATUS_REVISION: int(meeting.get(F.STATUS_REVISION, 0)),
        F.QUALITY_OUTCOME: meeting.get(F.QUALITY_OUTCOME) or None,
        F.QUALITY_POLICY_VERSION: meeting.get(F.QUALITY_POLICY_VERSION) or None,
        F.ARTIFACT_REVISION: int(meeting.get(F.ARTIFACT_REVISION, 0)),
        F.DELETION_STATE: meeting.get(F.DELETION_STATE) or None,
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
    installation_id = str(body.get("installation_id") or device_id).strip()
    runtime_instance_id = str(body.get("runtime_instance_id") or "").strip()
    try:
        evidence.require_identity(installation_id, "installation_id")
        if runtime_instance_id:
            evidence.require_identity(runtime_instance_id, "runtime_instance_id")
    except evidence.EvidenceValidationError as exc:
        return JSONResponse({"detail": {"code": exc.code}}, status_code=400)
    correlation_id = request.headers.get("X-Correlation-ID", "").strip() or uuid.uuid4().hex

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
            installation_id=installation_id,
            runtime_instance_id=runtime_instance_id,
            correlation_id=correlation_id,
        )
    except Exception as exc:
        # Fails closed: an allowed claim commits real STT+LLM spend, so an
        # outage denies with a retryable status instead of guessing.
        logger.warn("meetings: claim failed", {"user_id": user_id, "error": str(exc)})
        return JSONResponse({"error": "Claim temporarily unavailable."}, status_code=503)

    if result.denied_cap:
        return JSONResponse(
            {
                "detail": {
                    "code": F.MEETING_CAP_CODE,
                    "seconds_until_reset": result.seconds_until_reset,
                }
            },
            status_code=402,
        )
    if result.denied_conflict:
        return JSONResponse(
            {"detail": {"code": F.MEETING_CONFLICT_CODE}},
            status_code=409,
        )
    return JSONResponse(
        {
            "meeting_id": result.meeting_id,
            "capture_run_id": result.capture_run_id,
            "capture_fence": result.capture_fence,
            "lease_expires_at": result.lease_expires_at,
            "protocol_version": 2,
            "cap_minutes": result.cap_minutes,
            "max_capture_minutes": F.MAX_CAPTURE_MINUTES,
            "rejoined": result.rejoined,
        }
    )


def _integrity_error(code: str, status_code: int = 409) -> JSONResponse:
    return JSONResponse({"detail": {"code": code}}, status_code=status_code)


def _strict_int_header(
    request: Request,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(request.headers[name])
    except (KeyError, ValueError) as exc:
        raise evidence.EvidenceValidationError(
            "invalid_integrity_header", f"Missing or invalid {name}."
        ) from exc
    if not minimum <= value <= maximum:
        raise evidence.EvidenceValidationError(
            "invalid_integrity_header", f"{name} is out of range."
        )
    return value


async def handle_upload_segment_v2(
    request: Request,
    meeting_id: str,
    capture_run_id: str,
    seq: int,
) -> JSONResponse:
    """PUT V2 immutable segment ingest with receipt-bound reconciliation."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    correlation_id = request.headers.get("X-Correlation-ID", "").strip() or uuid.uuid4().hex
    try:
        evidence.require_identity(meeting_id, "meeting_id")
        evidence.require_identity(capture_run_id, "capture_run_id")
        if not 0 <= seq < F.MAX_SEGMENTS_PER_MEETING:
            raise evidence.EvidenceValidationError(
                "invalid_segment_sequence", "Segment sequence is out of range."
            )
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if not idempotency_key or len(idempotency_key) > 512:
            raise evidence.EvidenceValidationError(
                "invalid_idempotency_key", "Missing or invalid Idempotency-Key."
            )
        capture_fence = _strict_int_header(
            request,
            "X-Capture-Fence",
            minimum=1,
            maximum=2**63 - 1,
        )
        content_sha256 = evidence.require_sha256(
            request.headers.get("X-Content-SHA256", ""),
        )
        byte_length = _strict_int_header(
            request,
            "X-Byte-Length",
            minimum=1,
            maximum=F.MAX_SEGMENT_BYTES,
        )
        start_ms = _strict_int_header(
            request,
            "X-Start-Ms",
            minimum=0,
            maximum=F.MAX_SEGMENT_START_MS,
        )
        duration_ms = _strict_int_header(
            request,
            "X-Duration-Ms",
            minimum=1,
            maximum=F.MAX_SEGMENT_DURATION_MS,
        )
        channel_count = _strict_int_header(
            request,
            "X-Channel-Count",
            minimum=1,
            maximum=8,
        )
        sample_rate_hz = _strict_int_header(
            request,
            "X-Sample-Rate-Hz",
            minimum=8_000,
            maximum=192_000,
        )
        incomplete_header = request.headers.get("X-Incomplete", "").lower()
        if incomplete_header not in ("true", "false"):
            raise evidence.EvidenceValidationError(
                "invalid_integrity_header", "X-Incomplete must be true or false."
            )
        incomplete = incomplete_header == "true"
    except evidence.EvidenceValidationError as exc:
        return _integrity_error(exc.code, 400)

    try:
        meeting = await store.get_meeting(user_id, meeting_id)
    except Exception:
        return JSONResponse({"error": "Temporarily unavailable."}, status_code=503)
    if meeting is None:
        return JSONResponse({"error": "Unknown meeting."}, status_code=404)
    if meeting.get(F.DELETION_STATE):
        return _integrity_error(F.FAIL_DELETION_IN_PROGRESS)
    if (
        meeting.get(F.CAPTURE_RUN_ID) != capture_run_id
        or int(meeting.get(F.CAPTURE_FENCE, -1)) != capture_fence
    ):
        return _integrity_error(F.FAIL_STALE_CAPTURE_FENCE)

    data = await request.body()
    if len(data) != byte_length or evidence.sha256_hex(data) != content_sha256:
        return _integrity_error("content_identity_mismatch", 400)
    try:
        stream_info = await asyncio.to_thread(evidence.decode_flac_info, data)
    except evidence.EvidenceValidationError as exc:
        return _integrity_error(exc.code, 400)
    if (
        stream_info.channel_count != channel_count
        or stream_info.sample_rate_hz != sample_rate_hz
        or channel_count != 2
        or sample_rate_hz != 16_000
        or abs(stream_info.duration_ms - duration_ms) > evidence.duration_tolerance_ms(duration_ms)
    ):
        return _integrity_error("audio_format_or_duration_mismatch", 400)

    segment = {
        "seq": seq,
        "start_ms": start_ms,
        "duration_ms": duration_ms,
        "incomplete": incomplete,
        "content_sha256": content_sha256,
        "byte_length": byte_length,
        "channel_count": channel_count,
        "sample_rate_hz": sample_rate_hz,
        "decoded_duration_ms": stream_info.duration_ms,
    }
    runtime_instance_id = str(meeting.get(F.RUNTIME_INSTANCE_ID, ""))
    try:
        immutable_object = await gcs_audio.create_v2_segment(
            user_id,
            meeting_id,
            capture_run_id,
            seq,
            content_sha256,
            data,
            metadata={
                "capture_fence": str(capture_fence),
                "start_ms": str(start_ms),
                "duration_ms": str(duration_ms),
                "channel_count": str(channel_count),
                "sample_rate_hz": str(sample_rate_hz),
                "incomplete": str(incomplete).lower(),
                "runtime_instance_id": runtime_instance_id,
                "correlation_id": correlation_id,
            },
        )
        receipt = await store.persist_v2_segment(
            user_id,
            meeting_id,
            capture_run_id,
            capture_fence=capture_fence,
            segment=segment,
            immutable_object=immutable_object,
            idempotency_key=idempotency_key,
            runtime_instance_id=runtime_instance_id,
            correlation_id=correlation_id,
        )
    except gcs_audio.ImmutableObjectConflict as exc:
        try:
            await store.record_upload_conflict(
                user_id,
                meeting_id,
                capture_run_id,
                capture_fence=capture_fence,
                seq=seq,
                incoming_digest=content_sha256,
                object_path=exc.path,
                runtime_instance_id=runtime_instance_id,
                correlation_id=correlation_id,
            )
        except Exception as audit_exc:
            logger.error(
                "meetings.v2: immutable conflict audit failed",
                {
                    "meeting_id": meeting_id,
                    "capture_run_id": capture_run_id,
                    "seq": seq,
                    "correlation_id": correlation_id,
                    "error": str(audit_exc),
                },
            )
        return _integrity_error(F.FAIL_IMMUTABLE_OBJECT_CONFLICT)
    except store.StaleCaptureFenceError:
        return _integrity_error(F.FAIL_STALE_CAPTURE_FENCE)
    except store.DeletedMeetingError:
        return _integrity_error(F.FAIL_DELETION_IN_PROGRESS)
    except store.MeetingIntegrityError as exc:
        return _integrity_error(exc.code)
    except Exception as exc:
        code, is_config = _classify_upload_error(exc)
        observability.capture_error(
            exc,
            error_code=code,
            correlation_id=correlation_id,
            meeting_id=meeting_id,
            capture_run_id=capture_run_id,
            capture_fence=capture_fence,
        )
        (logger.error if is_config else logger.warn)(
            "meetings.v2: upload failed",
            {
                "meeting_id": meeting_id,
                "capture_run_id": capture_run_id,
                "capture_fence": capture_fence,
                "seq": seq,
                "correlation_id": correlation_id,
                "error_code": code,
                "error": str(exc),
            },
        )
        return JSONResponse({"error": "Upload failed.", "code": code}, status_code=503)
    return JSONResponse(receipt.public_dict())


async def handle_upload_segment(
    request: Request,
    meeting_id: str,
    seq: int,
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
        logger.warn(
            "meetings: upload ownership check failed",
            {
                "user_id": user_id,
                "meeting_id": meeting_id,
                "error": str(exc),
            },
        )
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
            user_id,
            meeting_id,
            seq=seq,
            start_ms=start_ms,
            duration_ms=duration_ms,
            incomplete=incomplete,
        )
    except gcs_audio.ImmutableObjectConflict:
        return _integrity_error(F.FAIL_IMMUTABLE_OBJECT_CONFLICT)
    except Exception as exc:
        code, is_config = _classify_upload_error(exc)
        try:
            await store.record_upload_failure(user_id, meeting_id, code=code)
        except Exception as state_exc:
            logger.error(
                "meetings: could not persist upload failure state",
                {
                    "user_id": user_id,
                    "meeting_id": meeting_id,
                    "failure_code": code,
                    "error": str(state_exc),
                },
            )
        # A configuration failure (missing bucket / revoked access) is NOT a
        # routine transient 503 - surface it loudly so it can be alerted, not
        # buried. See the 2026-07-14 bucket-not-provisioned incident.
        (logger.error if is_config else logger.warn)(
            "meetings: segment upload failed",
            {
                "user_id": user_id,
                "meeting_id": meeting_id,
                "seq": seq,
                "failure_code": code,
                "config_failure": is_config,
                "error": str(exc),
            },
        )
        return JSONResponse({"error": "Upload failed.", "code": code}, status_code=503)
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
                user_id,
                meeting_id,
                from_statuses=(F.STATUS_CAPTURING,),
                to_status=F.STATUS_FAILED,
                extra={
                    F.COMPLETE_REASON: str(body.get("reason") or "")[:100],
                    **store.failure_meta(code=F.FAIL_NO_AUDIO, retryable=False),
                },
            )
            return JSONResponse({"ok": True, "status": status_now or F.STATUS_FAILED})

        transitioned, status_now = await store.transition_status(
            user_id,
            meeting_id,
            from_statuses=(F.STATUS_CAPTURING,),
            to_status=F.STATUS_UPLOADED,
            stage=F.STAGE_QUEUED,
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
        logger.warn(
            "meetings: complete failed",
            {
                "user_id": user_id,
                "meeting_id": meeting_id,
                "error": str(exc),
            },
        )
        return JSONResponse({"error": "Complete failed."}, status_code=503)

    return JSONResponse({"ok": True, "status": status_now})


async def handle_complete_v2(
    request: Request,
    meeting_id: str,
    capture_run_id: str,
) -> JSONResponse:
    """POST V2 verified completion; Firestore job/outbox commit precedes dispatch."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    correlation_id = request.headers.get("X-Correlation-ID", "").strip() or uuid.uuid4().hex
    idempotency_key = request.headers.get("Idempotency-Key", "").strip()
    if not idempotency_key or len(idempotency_key) > 512:
        return _integrity_error("invalid_idempotency_key", 400)
    body = await _json_body(request)
    if body is None:
        return _integrity_error("invalid_manifest", 400)
    try:
        evidence.require_identity(meeting_id, "meeting_id")
        evidence.require_identity(capture_run_id, "capture_run_id")
        capture_fence = int(body["capture_fence"])
        segment_count = int(body["segment_count"])
        total_duration_ms = int(body["total_duration_ms"])
        reason = str(body.get("reason") or "")[:100]
        manifest_sha256 = evidence.require_sha256(
            str(body["manifest_sha256"]),
            "manifest_sha256",
        )
        raw_digests = body["segment_digests"]
        raw_segments = body["segments"]
        if not isinstance(raw_digests, list) or not isinstance(raw_segments, list):
            raise evidence.EvidenceValidationError("invalid_manifest", "Invalid arrays.")
        segment_digests = [evidence.require_sha256(str(value)) for value in raw_digests]
        segments = [evidence.parse_completion_segment(value) for value in raw_segments]
        if (
            capture_fence < 1
            or not 0 <= segment_count <= F.MAX_SEGMENTS_PER_MEETING
            or segment_count != len(segments)
            or not 0 <= total_duration_ms <= F.MAX_CAPTURE_MINUTES * 60_000
        ):
            raise evidence.EvidenceValidationError("invalid_manifest", "Invalid counts.")
    except (KeyError, TypeError, ValueError, evidence.EvidenceValidationError) as exc:
        code = getattr(exc, "code", "invalid_manifest")
        return _integrity_error(code, 400)
    try:
        meeting = await store.get_meeting(user_id, meeting_id)
        if meeting is None:
            return JSONResponse({"error": "Unknown meeting."}, status_code=404)
        await store.finalize_capture_run(
            user_id,
            meeting_id,
            capture_run_id,
            capture_fence=capture_fence,
            runtime_instance_id=str(meeting.get(F.RUNTIME_INSTANCE_ID, "")),
            correlation_id=correlation_id,
        )
        result = await store.verify_v2_completion(
            user_id,
            meeting_id,
            capture_run_id,
            capture_fence=capture_fence,
            segments=segments,
            segment_digests=segment_digests,
            segment_count=segment_count,
            total_duration_ms=total_duration_ms,
            reason=reason,
            manifest_sha256=manifest_sha256,
            idempotency_key=idempotency_key,
            runtime_instance_id=str(meeting.get(F.RUNTIME_INSTANCE_ID, "")),
            correlation_id=correlation_id,
        )
    except store.StaleCaptureFenceError:
        return _integrity_error(F.FAIL_STALE_CAPTURE_FENCE)
    except store.DeletedMeetingError:
        return _integrity_error(F.FAIL_DELETION_IN_PROGRESS)
    except Exception as exc:
        observability.capture_error(
            exc,
            error_code="completion_verification_unavailable",
            correlation_id=correlation_id,
            meeting_id=meeting_id,
            capture_run_id=capture_run_id,
            capture_fence=capture_fence,
        )
        logger.error(
            "meetings.v2: completion verification failed",
            {
                "meeting_id": meeting_id,
                "capture_run_id": capture_run_id,
                "capture_fence": capture_fence,
                "correlation_id": correlation_id,
                "error_code": "completion_verification_unavailable",
                "error": str(exc),
            },
        )
        return JSONResponse({"error": "Complete failed."}, status_code=503)
    if result.conflict_code:
        return _integrity_error(result.conflict_code)
    if result.receipt is None:
        return JSONResponse({"error": "Complete failed."}, status_code=503)
    try:
        await tasks.dispatch_job(user_id, result.job_id)
    except Exception as exc:
        # The durable outbox is already committed. The scheduler sweeper owns
        # recovery; never revoke or manufacture the real completion receipt.
        logger.error(
            "meetings.v2: inline dispatch deferred to sweeper",
            {
                "meeting_id": meeting_id,
                "capture_run_id": capture_run_id,
                "job_id": result.job_id,
                "correlation_id": correlation_id,
                "error_code": "outbox_dispatch_deferred",
                "error": str(exc),
            },
        )
    return JSONResponse(result.receipt)


async def handle_retry(request: Request, meeting_id: str) -> JSONResponse:
    """POST /meetings/{meeting_id}/retry - re-drive a recoverable meeting.

    Idempotent and safe: it re-enqueues synthesis only from a recoverable state
    and NEVER resets a ready, excluded, expired, or actively-synthesizing
    meeting. A retryable=false failure (no audio, audio rejected) stays terminal.
    The re-enqueue carries an attempt-count dedup suffix so Cloud Tasks does not
    swallow it as a duplicate of the original /complete task."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        meeting = await store.get_meeting(user_id, meeting_id)
    except Exception:
        return JSONResponse({"error": "Temporarily unavailable."}, status_code=503)
    if meeting is None:
        return JSONResponse({"error": "Unknown meeting."}, status_code=404)

    status = meeting.get(F.STATUS, "")
    if int(meeting.get(F.PROTOCOL_VERSION, 1)) >= 2:
        if status == F.STATUS_SYNTHESIZING:
            return JSONResponse(
                {"error": "Meeting processing is already active.", "status": status},
                status_code=409,
            )
        if status not in (F.STATUS_NEEDS_ATTENTION, F.STATUS_FAILED):
            return JSONResponse(
                {"error": "Meeting is not retryable.", "status": status},
                status_code=409,
            )
        try:
            job_id = await store.retry_v2_job(user_id, meeting_id)
            if not job_id:
                return JSONResponse(
                    {"error": "Meeting is not retryable.", "status": status},
                    status_code=409,
                )
        except Exception as exc:
            logger.error(
                "meetings.v2: retry state transition failed",
                {
                    "meeting_id": meeting_id,
                    "error_code": "meeting_retry_state_failed",
                    "error": str(exc),
                },
            )
            return JSONResponse({"error": "Retry failed."}, status_code=503)
        try:
            await tasks.dispatch_job(user_id, job_id)
        except Exception as exc:
            logger.error(
                "meetings.v2: retry dispatch deferred",
                {
                    "meeting_id": meeting_id,
                    "error_code": "meeting_retry_dispatch_failed",
                    "error": str(exc),
                },
            )
        return JSONResponse({"ok": True, "status": F.STATUS_UPLOADED})
    if status == F.STATUS_SYNTHESIZING and store.synthesis_lease_is_fresh(meeting):
        return JSONResponse(
            {"error": "Meeting processing is already active.", "status": status},
            status_code=409,
        )
    # Settled and terminal states a retry must never disturb.
    if status in (F.STATUS_READY, F.STATUS_EXCLUDED):
        return JSONResponse(
            {"error": "Meeting is not retryable.", "status": status},
            status_code=409,
        )
    if status == F.STATUS_CAPTURING:
        return JSONResponse(
            {"error": "Meeting is still capturing.", "status": status},
            status_code=409,
        )
    if status == F.STATUS_FAILED and not meeting.get(F.RETRYABLE):
        return JSONResponse(
            {"error": "This meeting cannot be recovered.", "status": status},
            status_code=409,
        )

    # Status is now uploaded, stale-synthesizing, or failed+retryable. A
    # failed+retryable meeting is
    # moved back to uploaded (clearing the failure signal) before the enqueue,
    # because the synthesis lease only grants from uploaded/synthesizing.
    attempt = int(meeting.get(F.ATTEMPT_COUNT, 0))
    try:
        if status == F.STATUS_FAILED:
            _, status_now = await store.transition_status(
                user_id,
                meeting_id,
                from_statuses=(F.STATUS_FAILED,),
                to_status=F.STATUS_UPLOADED,
                stage=F.STAGE_QUEUED,
                extra=store.clear_failure_meta(),
            )
            # A concurrent retry may have already advanced it; report and stop.
            if status_now != F.STATUS_UPLOADED:
                return JSONResponse({"ok": True, "status": status_now})
        await asyncio.to_thread(
            tasks.enqueue_synthesis,
            user_id,
            meeting_id,
            dedup_suffix=f"r{attempt}",
        )
    except Exception as exc:
        logger.warn(
            "meetings: retry failed",
            {
                "user_id": user_id,
                "meeting_id": meeting_id,
                "error": str(exc),
            },
        )
        return JSONResponse({"error": "Retry failed."}, status_code=503)

    return JSONResponse({"ok": True, "status": status_now if status == F.STATUS_FAILED else status})


async def handle_delete_meeting(request: Request, meeting_id: str) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    correlation_id = request.headers.get("X-Correlation-ID", "").strip() or uuid.uuid4().hex
    try:
        evidence.require_identity(meeting_id, "meeting_id")
        await deletion.request_deletion(
            user_id,
            meeting_id,
            actor_identity=user_id,
            correlation_id=correlation_id,
        )
        result = await deletion.run_deletion(user_id, meeting_id)
    except evidence.EvidenceValidationError as exc:
        return _integrity_error(exc.code, 400)
    except Exception as exc:
        observability.capture_error(
            exc,
            error_code="meeting_deletion_retry_required",
            correlation_id=correlation_id,
            meeting_id=meeting_id,
        )
        logger.error(
            "meetings.v2: deletion saga retry required",
            {
                "meeting_id": meeting_id,
                "correlation_id": correlation_id,
                "error_code": "meeting_deletion_retry_required",
                "error": str(exc),
            },
        )
        return JSONResponse(
            {
                "detail": {
                    "code": "meeting_deletion_retry_required",
                    "retryable": True,
                },
            },
            status_code=503,
        )
    if result.get("state") == "missing":
        return JSONResponse({"error": "Unknown meeting."}, status_code=404)
    return JSONResponse(
        {
            "ok": result.get("state") == F.STAGE_DELETE_COMPLETE,
            "state": result.get("state"),
            "deletion_id": result.get("deletion_id"),
            "completed_at": result.get("completed_at"),
        }
    )


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
    return JSONResponse(
        {
            "items": [_meeting_response(m, include_transcript=False) for m in items],
        }
    )


async def handle_internal_synthesize(request: Request) -> JSONResponse:
    """POST /internal/meetings/synthesize - the Cloud Tasks target. Terminal
    outcomes answer 200 (the queue must stop); lease contention answers 409 and
    other retryable failures propagate as 500 so the queue tries again with
    audio intact."""
    body = await _json_body(request) or {}
    user_id = str(body.get("user_id") or "").strip()
    meeting_id = str(body.get("meeting_id") or "").strip()
    job_id = str(body.get("job_id") or "").strip()
    if not user_id or not meeting_id:
        return JSONResponse({"error": "Missing user_id/meeting_id."}, status_code=400)

    try:
        status = await synthesis.run_synthesis(user_id, meeting_id, job_id=job_id)
    except synthesis.SynthesisLeaseBusyError:
        # Cloud Tasks can deliver the same job more than once. A current worker
        # still owns this lease (or lost it while finishing), so keep the
        # delivery retryable without turning expected contention into an
        # unhandled application error. Cloud Tasks retries every non-2xx
        # response; 409 expresses this job-scoped conflict without signaling
        # queue-wide overload via 429/503.
        logger.info(
            "meetings.synthesis: delivery deferred, lease busy",
            {
                "user_id": user_id,
                "meeting_id": meeting_id,
                "job_id": job_id,
            },
        )
        return JSONResponse(
            {"status": "lease_busy", "retryable": True},
            status_code=409,
        )
    return JSONResponse({"status": status})
