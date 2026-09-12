"""Firestore persistence for the LLM batch queue.

Every Firebase Admin SDK call is blocking, so each public function is an async
wrapper dispatching via ``asyncio.to_thread`` — matching ``briefing.briefing_store``
and ``icebreaker.icebreaker_store``.

Layout:

- ``llm_batch_items/{key}``   one queued request. ``job_id`` is empty while it
                              waits, set when a job is cut from the queue. Living
                              in ONE collection (rather than moving docs between a
                              queue and a job subcollection) means an item's
                              history never spans two documents and a crash
                              mid-move cannot lose or duplicate work.
- ``llm_batch_jobs/{job_id}`` one submitted provider job.

Reads degrade to empty rather than raising: a Firestore blip must never abort a
scheduler tick that also drives unrelated work. Writes that advance state are
ordered so the side effect lands BEFORE the bookkeeping that would let us skip it
on retry (CLAUDE.md: write an "already did this" cache only after the side effect
succeeds).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore as fs

from ...lib.logger import logger
from ..firebase import admin_firestore
from . import models as m

ITEMS_COLLECTION = "llm_batch_items"
JOBS_COLLECTION = "llm_batch_jobs"

# Items and jobs are operational records, not user content. They are kept long
# enough to investigate a bad week and no longer.
ITEM_TTL_DAYS = 30
JOB_TTL_DAYS = 60


def _items() -> Any:
    return admin_firestore().collection(ITEMS_COLLECTION)


def _jobs() -> Any:
    return admin_firestore().collection(JOBS_COLLECTION)


def _coerce_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


def _item_from_doc(doc_id: str, data: dict) -> m.BatchItem:
    return m.BatchItem(
        key=doc_id,
        job_kind=str(data.get("job_kind", "") or ""),
        owner_uid=str(data.get("owner_uid", "") or ""),
        request=data.get("request") or {},
        model=str(data.get("model", "") or ""),
        context_ref=str(data.get("context_ref", "") or ""),
        state=str(data.get("state", m.ITEM_PENDING) or m.ITEM_PENDING),
        attempt=int(data.get("attempt", 1) or 1),
        error=str(data.get("error", "") or ""),
        job_id=str(data.get("job_id", "") or ""),
        created_at=_coerce_utc(data.get("created_at")),
        dispatched_at=_coerce_utc(data.get("dispatched_at")),
    )


def _job_from_doc(doc_id: str, data: dict) -> m.BatchJob:
    return m.BatchJob(
        job_id=doc_id,
        job_kind=str(data.get("job_kind", "") or ""),
        model=str(data.get("model", "") or ""),
        provider_job_name=str(data.get("provider_job_name", "") or ""),
        state=str(data.get("state", m.JOB_PENDING) or m.JOB_PENDING),
        item_count=int(data.get("item_count", 0) or 0),
        failed_request_count=int(data.get("failed_request_count", 0) or 0),
        poll_attempts=int(data.get("poll_attempts", 0) or 0),
        display_name=str(data.get("display_name", "") or ""),
        submitted_at=_coerce_utc(data.get("submitted_at")),
        last_polled_at=_coerce_utc(data.get("last_polled_at")),
        terminal_at=_coerce_utc(data.get("terminal_at")),
        error=str(data.get("error", "") or ""),
        result_file_name=str(data.get("result_file_name", "") or ""),
        stats=data.get("stats") or {},
    )


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


async def enqueue_items(items: list[m.BatchItem]) -> int:
    """Persist items as pending work. Idempotent per key.

    Uses Firestore ``create``, which fails with ``AlreadyExists`` rather than
    overwriting — the same choice, for the same reason, as this feature's other
    writer (``activity_store.py:44-48``): it gives retry idempotency without a
    preliminary read, and it is ATOMIC. A get-then-set guard costs one sequential
    read per item AND still races, because a concurrent enqueue can land between
    the read and the write and get clobbered — which would reset an item already
    in flight, exactly what this function promises not to do.

    Returns the number actually written; keys that already existed are skipped.
    """
    if not items:
        return 0

    def _write() -> int:
        written = 0
        now = m.utcnow()
        expires = now + timedelta(days=ITEM_TTL_DAYS)
        for item in items:
            try:
                _items().document(item.key).create(
                    {
                        "job_kind": item.job_kind,
                        "owner_uid": item.owner_uid,
                        "request": item.request,
                        "model": item.model,
                        "context_ref": item.context_ref,
                        "state": m.ITEM_PENDING,
                        "attempt": item.attempt,
                        "error": "",
                        "job_id": "",
                        "created_at": now,
                        "expires_at": expires,
                    },
                )
                written += 1
            except AlreadyExists:
                # Already queued, in flight, or already done. Idempotent by
                # design: a replayed enqueue must never reset live work.
                continue
        return written

    try:
        count = await asyncio.to_thread(_write)
        logger.info("llm_batch.store: enqueued items", {
            "requested": len(items),
            "written": count,
            "job_kind": items[0].job_kind if items else "",
        })
        return count
    except Exception as exc:
        logger.error("llm_batch.store: enqueue_items failed", {
            "count": len(items),
            "error": str(exc),
        })
        return 0


# ---------------------------------------------------------------------------
# Queue inspection
# ---------------------------------------------------------------------------


async def pending_summary() -> tuple[int, datetime | None]:
    """(pending_count, oldest_pending_created_at). Degrades to (0, None).

    Counting is capped: the submit decision only needs to know whether the queue
    has crossed a threshold, so reading the whole backlog to produce an exact
    number would be waste that grows with the backlog.
    """

    def _read() -> tuple[int, datetime | None]:
        query = (
            _items()
            .where(filter=fs.FieldFilter("state", "==", m.ITEM_PENDING))
            .order_by("created_at")
            .limit(m.SUBMIT_MIN_ITEMS * 4)
        )
        docs = list(query.stream())
        if not docs:
            return 0, None
        oldest = _coerce_utc((docs[0].to_dict() or {}).get("created_at"))
        return len(docs), oldest

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.error("llm_batch.store: pending_summary failed", {"error": str(exc)})
        return 0, None


async def count_open_jobs() -> int:
    """How many jobs are still occupying provider concurrency. Degrades to the
    cap, so a read failure makes us hold off submitting rather than pile on."""

    def _read() -> int:
        query = _jobs().where(
            filter=fs.FieldFilter("state", "in", sorted(m.OPEN_JOB_STATES))
        ).limit(m.MAX_CONCURRENT_JOBS + 1)
        return len(list(query.stream()))

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.error("llm_batch.store: count_open_jobs failed (assuming at cap)", {
            "error": str(exc),
        })
        return m.MAX_CONCURRENT_JOBS


async def take_pending_items(limit: int) -> list[m.BatchItem]:
    """Read the oldest pending items, newest-last, for packing into a job.

    Deliberately does NOT lease them. The lease is the ``job_id`` stamp written by
    :func:`attach_items_to_job` after the provider has accepted the job — until
    the provider has the work, an item that is merely "reserved" by a tick that
    then crashed would be stranded. Concurrent ticks are prevented upstream by the
    submitter's own lock, not here.
    """

    def _read() -> list[m.BatchItem]:
        query = (
            _items()
            .where(filter=fs.FieldFilter("state", "==", m.ITEM_PENDING))
            .order_by("created_at")
            .limit(max(1, limit))
        )
        return [_item_from_doc(d.id, d.to_dict() or {}) for d in query.stream()]

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.error("llm_batch.store: take_pending_items failed", {"error": str(exc)})
        return []


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


CONTROL_COLLECTION = "llm_batch_control"
SUBMIT_LEASE_DOC = "submitter"
SUBMIT_LEASE_SECONDS = 300


async def acquire_submit_lease() -> str:
    """Take a short exclusive lease on cutting new provider jobs.

    Without this, two overlapping ticks can both read the same pending items and
    submit them as two jobs — the second ``attach_items_to_job`` overwrites the
    first's ``job_id``, and the work is generated (and billed) twice. The tick is
    normally single-flight, but Cloud Scheduler retries and multiple Cloud Run
    instances make "normally" the wrong thing to rely on for something that costs
    money.

    Returns the lease TOKEN on success, or "" when the lease is held elsewhere or
    could not be read. The token must be passed back to
    :func:`release_submit_lease`, which releases ONLY if it still matches — the
    same fence ``reactive/lease.py:58-117`` uses.

    Without the token: a tick that overruns ``SUBMIT_LEASE_SECONDS`` (two provider
    uploads plus two ``batches.create`` calls against a slow provider is entirely
    plausible) loses its lease to a second tick, then its own ``finally`` clears
    the lease the second tick is holding, and a third tick submits concurrently —
    which is exactly the double-submit this lease exists to prevent, and it costs
    money.

    Fails CLOSED: a read error returns "" and we simply do not submit this tick.
    Pending items keep, and the next tick tries again.
    """
    token = uuid.uuid4().hex

    def _txn() -> str:
        now = m.utcnow()
        ref = admin_firestore().collection(CONTROL_COLLECTION).document(SUBMIT_LEASE_DOC)
        transaction = admin_firestore().transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> str:
            snap = ref.get(transaction=txn)
            data = (snap.to_dict() or {}) if snap.exists else {}
            held_at = _coerce_utc(data.get("held_at"))
            if held_at is not None and now - held_at < timedelta(seconds=SUBMIT_LEASE_SECONDS):
                return ""
            txn.set(ref, {"held_at": now, "token": token}, merge=True)
            return token

        return _apply(transaction)

    try:
        return await asyncio.to_thread(_txn)
    except Exception as exc:
        logger.error("llm_batch.store: acquire_submit_lease failed (not submitting)", {
            "error": str(exc),
        })
        return ""


async def release_submit_lease(token: str) -> None:
    """Release the lease early, but ONLY if this caller still holds it.

    A no-op when the token has moved on: that means our lease already expired and
    somebody else owns the slot, and clearing theirs would let a third tick submit
    the same work concurrently.
    """
    if not token:
        return

    def _txn() -> bool:
        ref = admin_firestore().collection(CONTROL_COLLECTION).document(SUBMIT_LEASE_DOC)
        transaction = admin_firestore().transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> bool:
            snap = ref.get(transaction=txn)
            data = (snap.to_dict() or {}) if snap.exists else {}
            if str(data.get("token") or "") != token:
                return False
            txn.set(ref, {"held_at": None, "token": None}, merge=True)
            return True

        return _apply(transaction)

    try:
        released = await asyncio.to_thread(_txn)
        if not released:
            logger.warn("llm_batch.store: lease not released — token no longer ours", {
                "token": token[:8],
            })
    except Exception as exc:
        logger.warn("llm_batch.store: release_submit_lease failed (will expire)", {
            "error": str(exc),
        })


async def create_job(job: m.BatchJob) -> bool:
    """Record a submitted provider job. Written AFTER the provider accepted it,
    so a job doc always corresponds to real remote work."""

    def _write() -> bool:
        now = m.utcnow()
        _jobs().document(job.job_id).set({
            "schema_version": 1,
            "job_kind": job.job_kind,
            "model": job.model,
            "provider_job_name": job.provider_job_name,
            "state": job.state,
            "item_count": job.item_count,
            "failed_request_count": 0,
            "poll_attempts": 0,
            "display_name": job.display_name,
            "submitted_at": job.submitted_at or now,
            "last_polled_at": None,
            "terminal_at": None,
            "error": "",
            "result_file_name": "",
            "stats": {},
            "expires_at": now + timedelta(days=JOB_TTL_DAYS),
        })
        return True

    try:
        return await asyncio.to_thread(_write)
    except Exception as exc:
        logger.error("llm_batch.store: create_job failed", {
            "job_id": job.job_id,
            "provider_job_name": job.provider_job_name,
            "error": str(exc),
        })
        return False


async def attach_items_to_job(keys: list[str], *, job_id: str) -> int:
    """Stamp items as submitted under ``job_id``."""
    if not keys:
        return 0

    def _write() -> int:
        db = admin_firestore()
        written = 0
        for chunk_start in range(0, len(keys), 400):
            chunk = keys[chunk_start : chunk_start + 400]
            batch = db.batch()
            for key in chunk:
                batch.set(
                    _items().document(key),
                    {"state": m.ITEM_SUBMITTED, "job_id": job_id},
                    merge=True,
                )
            batch.commit()
            written += len(chunk)
        return written

    try:
        return await asyncio.to_thread(_write)
    except Exception as exc:
        logger.error("llm_batch.store: attach_items_to_job failed", {
            "job_id": job_id,
            "count": len(keys),
            "error": str(exc),
        })
        return 0


async def list_pollable_jobs() -> list[m.BatchJob]:
    """Jobs that are not yet finished with us (including succeeded-not-dispatched)."""

    def _read() -> list[m.BatchJob]:
        query = _jobs().where(
            filter=fs.FieldFilter("state", "in", sorted(m.OPEN_JOB_STATES))
        ).limit(m.MAX_CONCURRENT_JOBS * 2)
        return [_job_from_doc(d.id, d.to_dict() or {}) for d in query.stream()]

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.error("llm_batch.store: list_pollable_jobs failed", {"error": str(exc)})
        return []


async def record_poll(
    job_id: str,
    *,
    state: str,
    failed_request_count: int = 0,
    error: str = "",
    result_file_name: str = "",
) -> None:
    """Persist what a poll learned."""

    def _write() -> None:
        now = m.utcnow()
        update: dict[str, Any] = {
            "state": state,
            "last_polled_at": now,
            "poll_attempts": fs.Increment(1),
            "failed_request_count": int(failed_request_count or 0),
        }
        if error:
            update["error"] = error[:500]
        if result_file_name:
            update["result_file_name"] = result_file_name
        if m.is_terminal_job(state):
            update["terminal_at"] = now
        _jobs().document(job_id).set(update, merge=True)

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.error("llm_batch.store: record_poll failed", {
            "job_id": job_id,
            "state": state,
            "error": str(exc),
        })


async def mark_job_dispatched(job_id: str) -> None:
    """Terminal for us: results have been handed to the handler.

    Written only AFTER every item in the job reached a terminal item state, so a
    crash mid-dispatch leaves the job pollable and the remaining items are picked
    up on the next tick rather than silently dropped.
    """

    def _write() -> None:
        _jobs().document(job_id).set(
            {"state": m.JOB_DISPATCHED, "terminal_at": m.utcnow()}, merge=True
        )

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.error("llm_batch.store: mark_job_dispatched failed", {
            "job_id": job_id,
            "error": str(exc),
        })


# ---------------------------------------------------------------------------
# Items within a job
# ---------------------------------------------------------------------------


async def list_items_for_job(job_id: str) -> list[m.BatchItem]:
    def _read() -> list[m.BatchItem]:
        query = _items().where(filter=fs.FieldFilter("job_id", "==", job_id))
        return [_item_from_doc(d.id, d.to_dict() or {}) for d in query.stream()]

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.error("llm_batch.store: list_items_for_job failed", {
            "job_id": job_id,
            "error": str(exc),
        })
        return []


# ── Pure update builders ────────────────────────────────────────────────────
# Separated from the writers so a caller can decide every item's fate first and
# commit the lot in ONE batched write. Writing these one document at a time is
# O(items) sequential round trips: at MAX_ITEMS_PER_JOB that is 6-10s of pure
# bookkeeping inside an AWAITED scheduler request whose Cloud Scheduler attempt
# deadline is the HTTP default of 180s (deploy.sh sets none). The poll/dispatch
# phase holds no lease, so blowing that deadline means a retry re-enters dispatch
# for the same job concurrently.


def item_dispatched_update() -> dict[str, Any]:
    return {"state": m.ITEM_DISPATCHED, "dispatched_at": m.utcnow(), "error": ""}


def item_requeue_update(*, attempt: int, error: str) -> dict[str, Any]:
    return {
        "state": m.ITEM_PENDING,
        "job_id": "",
        "attempt": attempt,
        "error": error[:500],
        # Re-dated so it queues behind current work rather than jumping ahead of
        # items that have never been tried.
        "created_at": m.utcnow(),
    }


def item_terminal_update(*, state: str, error: str) -> dict[str, Any]:
    return {"state": state, "job_id": "", "error": error[:500]}


async def apply_item_updates(updates: list[tuple[str, dict[str, Any]]]) -> int:
    """Commit many item state changes atomically per chunk.

    Returns the number of documents written. Chunked at 400 against Firestore's
    500-operation batch ceiling.

    Atomicity is the point as much as the speed: the per-item loop this replaces
    could fail midway and leave a job whose items were half-resolved, on a job
    already stamped terminal and therefore never polled again — stranding the
    remainder with no error anywhere.
    """
    if not updates:
        return 0

    def _write() -> int:
        db = admin_firestore()
        written = 0
        for start in range(0, len(updates), 400):
            chunk = updates[start : start + 400]
            batch = db.batch()
            for key, payload in chunk:
                batch.set(_items().document(key), payload, merge=True)
            batch.commit()
            written += len(chunk)
        return written

    try:
        return await asyncio.to_thread(_write)
    except Exception as exc:
        logger.error("llm_batch.store: apply_item_updates failed", {
            "count": len(updates),
            "error": str(exc),
        })
        return 0


async def mark_item_dispatched(key: str) -> None:
    """Terminal success for one item — written only after its handler returned."""

    def _write() -> None:
        _items().document(key).set(
            {"state": m.ITEM_DISPATCHED, "dispatched_at": m.utcnow(), "error": ""},
            merge=True,
        )

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.error("llm_batch.store: mark_item_dispatched failed", {
            "key": key,
            "error": str(exc),
        })


async def requeue_item(key: str, *, attempt: int, error: str) -> None:
    """Return an item to the pending queue for one more attempt."""

    def _write() -> None:
        _items().document(key).set(
            {
                "state": m.ITEM_PENDING,
                "job_id": "",
                "attempt": attempt,
                "error": error[:500],
                # Re-dated so it queues behind current work rather than jumping
                # ahead of items that have never been tried.
                "created_at": m.utcnow(),
            },
            merge=True,
        )

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.error("llm_batch.store: requeue_item failed", {
            "key": key,
            "error": str(exc),
        })


async def mark_item_terminal(key: str, *, state: str, error: str) -> None:
    """Abandon or fail an item permanently. The caller must fall back."""

    def _write() -> None:
        _items().document(key).set(
            {"state": state, "job_id": "", "error": error[:500]}, merge=True
        )

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.error("llm_batch.store: mark_item_terminal failed", {
            "key": key,
            "state": state,
            "error": str(exc),
        })
