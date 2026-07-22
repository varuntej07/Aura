"""Meeting-notes Firestore store - claim, status transitions, note persistence.

Claim is the money path and mirrors entitlement.py's transactional counter
idiom: one Firestore transaction reads the event's claim lock plus the monthly
counter, then either returns the existing claim (same device rejoining), denies
(cap or cross-device conflict), or creates the meeting doc, sets the lock, and
charges the counter atomically. The counter is charged HERE and never by the
synthesis worker, so Cloud Tasks retries can never double-bill.

Unlike the chat/web-surf daily counters (which fail open because they meter a
cheap resource), claim FAILS CLOSED: a Firestore outage raises and the handler
answers 503, because every allowed claim commits real STT+LLM spend.

All Firestore work runs in ``asyncio.to_thread`` so the event loop stays
unblocked, matching every other store in this backend.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud import firestore as gcloud_firestore

from ...lib.logger import logger
from ..firebase import admin_firestore
from . import fields as F


def _meetings_ref(uid: str):
    return (
        admin_firestore()
        .collection(F.PARENT_COLLECTION).document(uid)
        .collection(F.SUBCOLLECTION)
    )


def _claim_ref(uid: str, event_key: str):
    return (
        admin_firestore()
        .collection(F.PARENT_COLLECTION).document(uid)
        .collection(F.CLAIMS_SUBCOLLECTION).document(event_key)
    )


def _usage_ref(uid: str, month_key: str):
    return (
        admin_firestore()
        .collection(F.PARENT_COLLECTION).document(uid)
        .collection(F.USAGE_SUBCOLLECTION).document(f"meetings_{month_key}")
    )


def event_key_for(event_id: str) -> str:
    """Deterministic, Firestore-safe doc id for an event's claim lock.
    Calendar instance ids and manual ids can carry characters we'd rather not
    trust in a doc path, so the key is always the sha1 hex of the raw id."""
    return hashlib.sha1(event_id.encode("utf-8")).hexdigest()


def _seconds_until_next_month(now: datetime) -> int:
    if now.month == 12:
        reset = datetime(now.year + 1, 1, 1, tzinfo=UTC)
    else:
        reset = datetime(now.year, now.month + 1, 1, tzinfo=UTC)
    return max(0, int((reset - now).total_seconds()))


@dataclass
class ClaimResult:
    meeting_id: str = ""
    cap_minutes: int = 0
    denied_cap: bool = False
    denied_conflict: bool = False
    seconds_until_reset: int = 0
    rejoined: bool = False


async def claim_meeting(
    uid: str,
    *,
    event_id: str,
    title: str,
    start_time: str,
    end_time: str,
    device_id: str,
    effective_tier: str,
) -> ClaimResult:
    """Atomically claim one meeting capture slot. Raises on Firestore failure
    (fails closed; the handler answers 503 and the client backs off).

    The claim lock self-expires at the event's end plus CLAIM_GRACE_MINUTES:
    a drop-and-rejoin inside that window returns the same meeting_id with no
    second charge, while a fresh capture of the same event much later gets a
    new meeting and a new charge."""
    now = datetime.now(UTC)
    now_ms = int(now.timestamp() * 1000)
    month_key = now.strftime("%Y%m")
    event_key = event_key_for(event_id)
    is_capped_tier = effective_tier != "pro"
    cap_minutes = (
        F.FREE_SYNTHESIS_CAP_MINUTES if is_capped_tier else F.PRO_SYNTHESIS_CAP_MINUTES
    )

    # The lock expires at the event's scheduled end plus grace, or (for manual
    # captures and unparseable times) a full capture-length window from now.
    try:
        end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
        expires_at_ms = int(end_dt.timestamp() * 1000) + F.CLAIM_GRACE_MINUTES * 60_000
    except (ValueError, AttributeError):
        expires_at_ms = now_ms + (F.MAX_CAPTURE_MINUTES + F.CLAIM_GRACE_MINUTES) * 60_000
    expires_at_ms = max(expires_at_ms, now_ms + F.CLAIM_GRACE_MINUTES * 60_000)

    def _run() -> ClaimResult:
        db = admin_firestore()
        lock_ref = _claim_ref(uid, event_key)
        usage_ref = _usage_ref(uid, month_key)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> ClaimResult:
            lock_snap = lock_ref.get(transaction=txn)
            usage_snap = usage_ref.get(transaction=txn)

            lock = lock_snap.to_dict() or {}
            if lock and lock.get(F.CLAIM_EXPIRES_AT_MS, 0) > now_ms:
                if lock.get(F.CLAIM_DEVICE_ID) != device_id:
                    return ClaimResult(denied_conflict=True)
                # Same-device rejoin is only a continuation while the meeting
                # can still accept audio. Once /complete moved it past
                # "capturing" (synthesis may already be running), reusing the
                # id would record into a meeting whose uploads 409 forever -
                # fall through and mint a fresh meeting instead.
                locked_meeting_id = lock.get(F.CLAIM_MEETING_ID, "")
                meeting_snap = _meetings_ref(uid).document(locked_meeting_id).get(
                    transaction=txn,
                )
                meeting_status = (meeting_snap.to_dict() or {}).get(F.STATUS, "")
                if meeting_status == F.STATUS_CAPTURING:
                    return ClaimResult(
                        meeting_id=locked_meeting_id,
                        cap_minutes=int(lock.get(F.CAP_MINUTES, cap_minutes)),
                        rejoined=True,
                    )

            count = int((usage_snap.to_dict() or {}).get("count", 0))
            if is_capped_tier and count >= F.MONTHLY_MEETING_CAP:
                return ClaimResult(
                    denied_cap=True,
                    seconds_until_reset=_seconds_until_next_month(now),
                )

            meeting_id = uuid.uuid4().hex
            txn.set(_meetings_ref(uid).document(meeting_id), {
                F.EVENT_ID: event_id,
                F.TITLE: title,
                F.START_TIME: start_time,
                F.END_TIME: end_time,
                F.DEVICE_ID: device_id,
                F.STATUS: F.STATUS_CAPTURING,
                F.CAP_MINUTES: cap_minutes,
                F.SEGMENTS: [],
                F.CREATED_AT: now.isoformat(),
                F.UPDATED_AT: now.isoformat(),
                F.PROCESSING_STAGE: F.STAGE_CAPTURING,
                F.STATUS_REVISION: 0,
                F.ATTEMPT_COUNT: 0,
            })
            txn.set(lock_ref, {
                F.CLAIM_EVENT_ID: event_id,
                F.CLAIM_MEETING_ID: meeting_id,
                F.CLAIM_DEVICE_ID: device_id,
                F.CLAIM_EXPIRES_AT_MS: expires_at_ms,
                F.CAP_MINUTES: cap_minutes,
            })
            txn.set(usage_ref, {"count": count + 1})
            return ClaimResult(meeting_id=meeting_id, cap_minutes=cap_minutes)

        return _execute(transaction)

    result = await asyncio.to_thread(_run)
    logger.info("meetings.store: claim", {
        "user_id": uid, "event_key": event_key, "meeting_id": result.meeting_id,
        "denied_cap": result.denied_cap, "denied_conflict": result.denied_conflict,
        "rejoined": result.rejoined, "tier": effective_tier,
    })
    return result


async def get_meeting(uid: str, meeting_id: str) -> dict[str, Any] | None:
    """One meeting doc, or None when missing. Raises on Firestore failure so
    ownership checks in the handlers never silently pass on an outage."""
    def _read() -> dict[str, Any] | None:
        snap = _meetings_ref(uid).document(meeting_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        data["meeting_id"] = snap.id
        return data

    return await asyncio.to_thread(_read)


async def append_segment_meta(
    uid: str,
    meeting_id: str,
    *,
    seq: int,
    start_ms: int,
    duration_ms: int,
    incomplete: bool,
) -> None:
    """Record one uploaded segment's offsets on the meeting doc. ArrayUnion
    makes a client upload retry idempotent (an identical element is a no-op).
    `incomplete` marks a segment that may contain a silent hole (device
    re-bind mid-segment) so synthesis can caveat the note honestly."""
    def _update() -> None:
        _meetings_ref(uid).document(meeting_id).update({
            F.SEGMENTS: gcloud_firestore.ArrayUnion([
                {
                    "seq": seq,
                    "start_ms": start_ms,
                    "duration_ms": duration_ms,
                    "incomplete": incomplete,
                },
            ]),
            F.UPDATED_AT: datetime.now(UTC).isoformat(),
            F.PROCESSING_STAGE: F.STAGE_UPLOADING,
            F.RETRYABLE: False,
            F.FAILURE_CODE: gcloud_firestore.DELETE_FIELD,
            F.FAILURE_MESSAGE: gcloud_firestore.DELETE_FIELD,
        })

    await asyncio.to_thread(_update)


async def transition_status(
    uid: str,
    meeting_id: str,
    *,
    from_statuses: tuple[str, ...],
    to_status: str,
    stage: str | None = None,
    extra: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Transactional compare-and-set on ``status`` - the worker's idempotency
    primitive. Returns (transitioned, status_now); a doc already past the
    transition reports its current status so callers can treat re-runs as
    settled instead of failed. Every successful transition bumps
    ``status_revision`` and, when given, ``processing_stage``. Raises on
    Firestore failure."""
    def _run() -> tuple[bool, str]:
        db = admin_firestore()
        doc_ref = _meetings_ref(uid).document(meeting_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> tuple[bool, str]:
            snap = doc_ref.get(transaction=txn)
            if not snap.exists:
                return False, ""
            current = (snap.to_dict() or {}).get(F.STATUS, "")
            if current not in from_statuses:
                return False, current
            update: dict[str, Any] = {
                F.STATUS: to_status,
                F.UPDATED_AT: datetime.now(UTC).isoformat(),
                F.STATUS_REVISION: gcloud_firestore.Increment(1),
            }
            if stage is not None:
                update[F.PROCESSING_STAGE] = stage
            if extra:
                update.update(extra)
            txn.update(doc_ref, update)
            return True, to_status

        return _execute(transaction)

    transitioned, status_now = await asyncio.to_thread(_run)
    logger.info("meetings.store: transition", {
        "user_id": uid, "meeting_id": meeting_id, "to": to_status,
        "transitioned": transitioned, "status_now": status_now,
    })
    return transitioned, status_now


def failure_meta(*, code: str, retryable: bool, message: str = "") -> dict[str, Any]:
    """The failure fields to stamp alongside a transition into a terminal or
    recoverable-failed state. ``code`` is a safe FAIL_* enum, never a raw
    provider exception string."""
    return {
        F.FAILURE_CODE: code,
        F.FAILURE_MESSAGE: message,
        F.RETRYABLE: retryable,
        F.LAST_ERROR_AT: datetime.now(UTC).isoformat(),
    }


def clear_failure_meta() -> dict[str, Any]:
    """The inverse of failure_meta: drop the failure signal when a meeting is
    re-driven from a recoverable state (POST /meetings/{id}/retry)."""
    return {
        F.RETRYABLE: False,
        F.FAILURE_CODE: gcloud_firestore.DELETE_FIELD,
        F.FAILURE_MESSAGE: gcloud_firestore.DELETE_FIELD,
    }


async def record_upload_failure(uid: str, meeting_id: str, *, code: str) -> None:
    """Persist a safe upload problem without changing the coarse status.

    The encrypted desktop queue remains authoritative until /complete, so the
    meeting must continue accepting segment retries while the backend exposes a
    durable reason to newer clients.
    """
    update = {
        F.PROCESSING_STAGE: F.STAGE_UPLOADING,
        F.UPDATED_AT: datetime.now(UTC).isoformat(),
        **failure_meta(code=code, retryable=True),
    }
    await asyncio.to_thread(_meetings_ref(uid).document(meeting_id).update, update)


async def mark_failed(
    uid: str,
    meeting_id: str,
    *,
    from_statuses: tuple[str, ...],
    code: str,
    retryable: bool,
    message: str = "",
) -> tuple[bool, str]:
    """Transition to ``failed`` and stamp the safe failure metadata in one CAS."""
    return await transition_status(
        uid, meeting_id,
        from_statuses=from_statuses,
        to_status=F.STATUS_FAILED,
        extra=failure_meta(code=code, retryable=retryable, message=message),
    )


async def set_stage(uid: str, meeting_id: str, stage: str) -> None:
    """Mark a finer processing_stage WITHOUT a status change (e.g. transcribing
    -> building_insights inside one synthesizing lease). Best-effort: a failed
    stage marker must not abort the run it annotates."""
    def _update() -> None:
        _meetings_ref(uid).document(meeting_id).update({
            F.PROCESSING_STAGE: stage,
            F.UPDATED_AT: datetime.now(UTC).isoformat(),
        })

    try:
        await asyncio.to_thread(_update)
    except Exception as exc:
        logger.warn("meetings.store: set_stage failed", {
            "user_id": uid, "meeting_id": meeting_id, "stage": stage, "error": str(exc),
        })


def synthesis_lease_is_fresh(meeting: dict[str, Any], *, now_ms: int | None = None) -> bool:
    """Whether a synthesizing meeting is still owned by a live worker."""
    if meeting.get(F.STATUS) != F.STATUS_SYNTHESIZING:
        return False
    current_ms = now_ms if now_ms is not None else int(datetime.now(UTC).timestamp() * 1000)
    started_ms = int(meeting.get(F.SYNTHESIS_STARTED_AT_MS, 0))
    return current_ms - started_ms < F.SYNTHESIS_LEASE_MS


async def claim_synthesis(uid: str, meeting_id: str) -> tuple[bool, str]:
    """Transactional synthesis lease. Grants the run when the meeting sits at
    "uploaded", or when a previous "synthesizing" claim is older than the
    lease (crashed worker). A concurrent Cloud Tasks duplicate arriving while
    a fresh lease is held is refused, so one meeting can never pay for STT+LLM
    twice at once. Returns (claimed, status_now). Raises on Firestore failure."""
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    def _run() -> tuple[bool, str]:
        db = admin_firestore()
        doc_ref = _meetings_ref(uid).document(meeting_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> tuple[bool, str]:
            snap = doc_ref.get(transaction=txn)
            if not snap.exists:
                return False, ""
            data = snap.to_dict() or {}
            current = data.get(F.STATUS, "")
            lease_fresh = synthesis_lease_is_fresh(data, now_ms=now_ms)
            if current == F.STATUS_SYNTHESIZING and lease_fresh:
                return False, current
            if current not in (F.STATUS_UPLOADED, F.STATUS_SYNTHESIZING):
                return False, current
            txn.update(doc_ref, {
                F.STATUS: F.STATUS_SYNTHESIZING,
                F.SYNTHESIS_STARTED_AT_MS: now_ms,
                F.UPDATED_AT: datetime.now(UTC).isoformat(),
                F.PROCESSING_STAGE: F.STAGE_TRANSCRIBING,
                F.ATTEMPT_COUNT: gcloud_firestore.Increment(1),
                F.STATUS_REVISION: gcloud_firestore.Increment(1),
            })
            return True, F.STATUS_SYNTHESIZING

        return _execute(transaction)

    claimed, status_now = await asyncio.to_thread(_run)
    logger.info("meetings.store: synthesis claim", {
        "user_id": uid, "meeting_id": meeting_id,
        "claimed": claimed, "status_now": status_now,
    })
    return claimed, status_now


async def save_note(
    uid: str,
    meeting_id: str,
    note: dict[str, Any],
    *,
    effective_tier: str,
) -> None:
    """Persist the synthesized note and flip status to ready. Non-pro notes get
    the RETENTION_DAYS TTL stamp; pro notes carry no expiry (full history is
    the paid feature). Raises on failure so the worker marks the run failed
    instead of deleting audio for a note that never landed."""
    now = datetime.now(UTC)
    update: dict[str, Any] = {
        F.NOTE: note,
        F.STATUS: F.STATUS_READY,
        F.UPDATED_AT: now.isoformat(),
        F.PROCESSING_STAGE: F.STAGE_READY,
        F.STATUS_REVISION: gcloud_firestore.Increment(1),
        # A successful (re)run clears any earlier failure signal.
        F.RETRYABLE: False,
        F.FAILURE_CODE: gcloud_firestore.DELETE_FIELD,
        F.FAILURE_MESSAGE: gcloud_firestore.DELETE_FIELD,
    }
    if effective_tier != "pro":
        update[F.EXPIRES_AT] = now + timedelta(days=F.RETENTION_DAYS)

    await asyncio.to_thread(_meetings_ref(uid).document(meeting_id).update, update)
    logger.info("meetings.store: note saved", {
        "user_id": uid, "meeting_id": meeting_id, "tier": effective_tier,
        "summary_chars": len(note.get("summary", "")),
        "action_items": len(note.get("action_items", [])),
        "transcript_turns": len(note.get(F.NOTE_TRANSCRIPT, [])),
        "transcript_chars": sum(
            len(turn.get(F.TRANSCRIPT_TEXT, ""))
            for turn in note.get(F.NOTE_TRANSCRIPT, [])
            if isinstance(turn, dict)
        ),
    })


async def list_recent(uid: str, *, limit: int = F.LIST_LIMIT) -> list[dict[str, Any]]:
    """Recent meetings, newest first, expired rows dropped (TTL sweeper can lag
    ~72h). Fails closed to an empty list, matching drafts' read path."""
    if not uid:
        return []
    limit = max(1, min(limit, F.LIST_LIMIT))

    def _read() -> list[dict[str, Any]]:
        query = (
            _meetings_ref(uid)
            .order_by(F.CREATED_AT, direction="DESCENDING")
            .limit(limit)
        )
        now = datetime.now(UTC)
        rows: list[dict[str, Any]] = []
        for snap in query.stream():
            data = snap.to_dict() or {}
            expires_at = data.get(F.EXPIRES_AT)
            if expires_at is not None and expires_at < now:
                continue
            data["meeting_id"] = snap.id
            rows.append(data)
        return rows

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("meetings.store: list failed", {"user_id": uid, "error": str(exc)})
        return []


async def get_exclude_keywords(uid: str) -> list[str]:
    """The user's sensitive-meeting exclude list. An absent doc is an empty
    list; a READ FAILURE raises. Failing open here would send a meeting the
    user explicitly excluded to a third-party STT vendor because of a
    transient Firestore blip - an irreversible disclosure. The caller treats
    the raise as retryable infrastructure (audio stays put, the task retries)."""
    def _read() -> list[str]:
        snap = (
            admin_firestore()
            .collection(F.PARENT_COLLECTION).document(uid)
            .collection(F.SETTINGS_SUBCOLLECTION).document(F.SETTINGS_DOC)
            .get()
        )
        raw = (snap.to_dict() or {}).get("exclude_keywords", [])
        return [str(k).strip().lower() for k in raw if str(k).strip()]

    return await asyncio.to_thread(_read)
