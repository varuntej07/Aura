"""Firestore access layer for open-loop threads.

All Firebase Admin SDK calls are blocking, so every public function here is an
``async`` wrapper that dispatches the sync work via ``asyncio.to_thread`` —
matching ``signal_engine.feature_store``. Read failures are logged and degrade
to an empty / default result so the reflector can never crash a scheduler tick.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from google.cloud import firestore as fs
from google.cloud.firestore_v1.base_query import FieldFilter

from ...lib.logger import logger
from ..firebase import admin_firestore
from . import fields as f
from .models import Thread, ThreadStatus

# A user accumulates few open loops at a time; cap the read so a runaway profile
# can never pull an unbounded page into memory.
MAX_OPEN_THREADS_READ = 25


def _threads_ref(user_id: str) -> fs.CollectionReference:
    return (
        admin_firestore()
        .collection("users")
        .document(user_id)
        .collection(f.THREADS_SUBCOLLECTION)
    )


def _threads_state_ref(user_id: str) -> fs.DocumentReference:
    return (
        admin_firestore()
        .collection("users")
        .document(user_id)
        .collection(f.THREADS_STATE_SUBCOLLECTION)
        .document(f.THREADS_STATE_DOC_ID)
    )


async def create_thread(user_id: str, thread: Thread) -> None:
    """Idempotent upsert keyed by ``thread.thread_id`` (re-recording is a no-op overwrite)."""

    def _write() -> None:
        _threads_ref(user_id).document(thread.thread_id).set(thread.to_dict())

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.warn("threads.thread_store: create_thread failed", {
            "user_id": user_id,
            "thread_id": thread.thread_id,
            "error": str(exc),
        })


async def list_threads_for_subject_dedup(user_id: str) -> list[Thread]:
    """All of a user's threads, ANY status, for subject-level dedup at creation.

    Unlike ``list_open_threads`` this ignores status on purpose: a DORMANT or
    RESOLVED thread about the same subject means Buddy already explored (or was
    ignored on) that loop, so a fresh reminder on the SAME subject must NOT open a
    parallel thread and re-arm its follow-up budget. Collection-scope read, no
    filter, so no composite index is needed. Capped like ``list_open_threads``
    (a user holds few loops) and fails open to ``[]`` — a read error must never
    block a legitimate new thread from being created.
    """

    def _fetch() -> list[Thread]:
        snaps = _threads_ref(user_id).limit(MAX_OPEN_THREADS_READ).stream()
        return [Thread.from_dict(s.to_dict() or {}) for s in snaps]

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("threads.thread_store: list_threads_for_subject_dedup failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        return []


async def touch_thread(user_id: str, thread_id: str, when: datetime) -> None:
    """Stamp ``last_touched_at`` when the user re-mentions an existing open loop.

    A fresh mention makes the loop the most natural one to ask about next, so
    reusing a thread (instead of forking a new one) bumps its recency for
    ``select_thread_to_follow_up``. Never raises.
    """

    def _update() -> None:
        _threads_ref(user_id).document(thread_id).update({f.FIELD_LAST_TOUCHED_AT: when})

    try:
        await asyncio.to_thread(_update)
    except Exception as exc:
        logger.warn("threads.thread_store: touch_thread failed", {
            "user_id": user_id,
            "thread_id": thread_id,
            "error": str(exc),
        })


async def list_open_threads(user_id: str) -> list[Thread]:
    """All threads still in the OPEN state for a user.

    Single-field equality filter (``status == open``) at collection scope — auto
    indexed by Firestore, no composite declaration needed. Ordering/selection is
    done in pure Python by the reflector so this stays index-free.
    """

    def _fetch() -> list[Thread]:
        snaps = (
            _threads_ref(user_id)
            .where(filter=FieldFilter(f.FIELD_STATUS, "==", str(ThreadStatus.OPEN)))
            .limit(MAX_OPEN_THREADS_READ)
            .stream()
        )
        return [Thread.from_dict(s.to_dict() or {}) for s in snaps]

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("threads.thread_store: list_open_threads failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        return []


async def get_thread(user_id: str, thread_id: str) -> Thread | None:
    """Load one thread, or None if it does not exist / read fails."""

    def _fetch() -> Thread | None:
        snap = _threads_ref(user_id).document(thread_id).get()
        if not snap.exists:
            return None
        return Thread.from_dict(snap.to_dict() or {})

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("threads.thread_store: get_thread failed", {
            "user_id": user_id,
            "thread_id": thread_id,
            "error": str(exc),
        })
        return None


async def append_message(
    user_id: str,
    thread_id: str,
    *,
    role: str,
    content: str,
    created_at: datetime,
    origin: str = "notification_reply",
) -> None:
    """Append one turn to a thread's server-authoritative conversation.

    Ordered by ``created_at`` at collection scope (auto-indexed). The client
    reconciles these into its chat view when the thread is opened.
    """

    def _write() -> None:
        (
            _threads_ref(user_id)
            .document(thread_id)
            .collection(f.THREAD_MESSAGES_SUBCOLLECTION)
            .add({
                f.MSG_ROLE: role,
                f.MSG_CONTENT: content,
                f.MSG_CREATED_AT: created_at,
                f.MSG_ORIGIN: origin,
            })
        )

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.warn("threads.thread_store: append_message failed", {
            "user_id": user_id,
            "thread_id": thread_id,
            "role": role,
            "error": str(exc),
        })


async def list_messages(user_id: str, thread_id: str) -> list[dict]:
    """The thread's server-authoritative conversation, oldest first.

    Ordered by ``created_at`` at collection scope (auto-indexed, no composite
    declaration needed). Used by the client to reconcile a shade exchange into
    its chat view when the thread is opened. Returns ISO timestamps so the
    payload is JSON-serialisable.
    """

    def _fetch() -> list[dict]:
        snaps = (
            _threads_ref(user_id)
            .document(thread_id)
            .collection(f.THREAD_MESSAGES_SUBCOLLECTION)
            .order_by(f.MSG_CREATED_AT)
            .stream()
        )
        out: list[dict] = []
        for s in snaps:
            data = s.to_dict() or {}
            created_at = data.get(f.MSG_CREATED_AT)
            out.append({
                "role": str(data.get(f.MSG_ROLE, "")),
                "content": str(data.get(f.MSG_CONTENT, "")),
                "created_at": created_at.isoformat() if isinstance(created_at, datetime) else None,
            })
        return out

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("threads.thread_store: list_messages failed", {
            "user_id": user_id,
            "thread_id": thread_id,
            "error": str(exc),
        })
        return []


async def mark_follow_up_sent(user_id: str, thread_id: str, sent_at: datetime) -> None:
    """Increment the follow-up counter and stamp the time after a push is delivered."""

    def _update() -> None:
        _threads_ref(user_id).document(thread_id).update({
            f.FIELD_FOLLOW_UPS_SENT: fs.Increment(1),
            f.FIELD_LAST_FOLLOW_UP_AT: sent_at,
        })

    try:
        await asyncio.to_thread(_update)
    except Exception as exc:
        logger.warn("threads.thread_store: mark_follow_up_sent failed", {
            "user_id": user_id,
            "thread_id": thread_id,
            "error": str(exc),
        })


async def set_status(user_id: str, thread_id: str, status: ThreadStatus) -> None:
    """Move a thread to a new lifecycle state."""

    def _update() -> None:
        _threads_ref(user_id).document(thread_id).update({f.FIELD_STATUS: str(status)})

    try:
        await asyncio.to_thread(_update)
    except Exception as exc:
        logger.warn("threads.thread_store: set_status failed", {
            "user_id": user_id,
            "thread_id": thread_id,
            "status": str(status),
            "error": str(exc),
        })


async def read_follow_ups_today(user_id: str, user_local_date: str) -> int:
    """How many curiosity follow-ups the reflector has already sent today.

    Returns 0 when the stored date is not today's user-local date (the counter
    has rolled over) or on any read failure (fail-open is safe here — the daily
    cap plus quiet hours still bound the worst case to a handful).
    """

    def _fetch() -> int:
        snap = _threads_state_ref(user_id).get()
        if not snap.exists:
            return 0
        data = snap.to_dict() or {}
        if data.get(f.FIELD_FOLLOW_UPS_TODAY_DATE) != user_local_date:
            return 0
        return int(data.get(f.FIELD_FOLLOW_UPS_TODAY, 0) or 0)

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("threads.thread_store: read_follow_ups_today failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        return 0


async def record_follow_up_in_budget(
    user_id: str, user_local_date: str, sent_at: datetime
) -> None:
    """Bump the per-user daily follow-up budget, resetting on a new local date.

    Read-modify-write inside a Firestore transaction so two overlapping ticks
    can never both think they are the first send of the day.
    """

    def _txn_update() -> None:
        db = admin_firestore()
        ref = _threads_state_ref(user_id)
        transaction = db.transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> None:
            snap = ref.get(transaction=txn)
            data = (snap.to_dict() or {}) if snap.exists else {}
            same_day = data.get(f.FIELD_FOLLOW_UPS_TODAY_DATE) == user_local_date
            current = int(data.get(f.FIELD_FOLLOW_UPS_TODAY, 0) or 0) if same_day else 0
            txn.set(ref, {
                f.FIELD_FOLLOW_UPS_TODAY: current + 1,
                f.FIELD_FOLLOW_UPS_TODAY_DATE: user_local_date,
                f.FIELD_STATE_LAST_FOLLOW_UP_AT: sent_at,
            }, merge=True)

        _apply(transaction)

    try:
        await asyncio.to_thread(_txn_update)
    except Exception as exc:
        logger.warn("threads.thread_store: record_follow_up_in_budget failed", {
            "user_id": user_id,
            "error": str(exc),
        })
