"""The scheduler-driven engine: cut jobs from the queue, then advance open jobs.

Called once per tick from ``handlers/scheduler.py``. Two independent phases run
in order; either can fail without stopping the other, and neither may raise into
the tick — the same tick drives unrelated subsystems.

Phase 1 (submit) packs pending items into provider jobs **by model**, not by
``job_kind``: dispatch is per item via its key, so one job can carry several
kinds and packs better for it.

Phase 2 (poll) advances every open job. The important asymmetry is between "the
job failed" and "we could not reach the provider" — the latter leaves everything
untouched and retries, mirroring how ``threads/sensitivity.py`` separates a
verdict from an outage.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime

from ...lib.logger import logger
from . import client as provider
from . import models as m
from . import registry, store

# How many items one provider job may carry. Well under any documented ceiling;
# the real constraint is that a single job's failure should not take down an
# unbounded amount of work.
MAX_ITEMS_PER_JOB = 200


async def run_batch_tick() -> dict[str, int]:
    """One full pass. Returns a small stats dict for logging and tests.

    Never raises.
    """
    stats = {"submitted_jobs": 0, "submitted_items": 0, "polled": 0, "dispatched": 0}
    try:
        submitted = await _submit_phase()
        stats.update(submitted)
    except Exception as exc:
        logger.error("llm_batch.poller: submit phase failed", {"error": str(exc)})
    try:
        polled = await _poll_phase()
        stats.update(polled)
    except Exception as exc:
        logger.error("llm_batch.poller: poll phase failed", {"error": str(exc)})
    return stats


# ---------------------------------------------------------------------------
# Phase 1 — submit
# ---------------------------------------------------------------------------


async def _submit_phase() -> dict[str, int]:
    pending_count, oldest = await store.pending_summary()
    if pending_count == 0:
        return {"submitted_jobs": 0, "submitted_items": 0}

    open_jobs = await store.count_open_jobs()
    now = m.utcnow()
    if not m.should_submit(
        pending_count=pending_count,
        oldest_pending_at=oldest,
        open_job_count=open_jobs,
        now=now,
    ):
        return {"submitted_jobs": 0, "submitted_items": 0}

    # Costs money past this point: take the lease before reading work to submit.
    lease = await store.acquire_submit_lease()
    if not lease:
        logger.info("llm_batch.poller: submit lease held elsewhere, standing down", {
            "pending": pending_count,
        })
        return {"submitted_jobs": 0, "submitted_items": 0}

    jobs_made = 0
    items_sent = 0
    try:
        items = await store.take_pending_items(MAX_ITEMS_PER_JOB * 2)
        by_model: dict[str, list[m.BatchItem]] = defaultdict(list)
        for item in items:
            if not item.model:
                # Unroutable: no model means no job can carry it. Terminal, loud.
                logger.error("llm_batch.poller: item has no model, abandoning", {
                    "key": item.key,
                    "job_kind": item.job_kind,
                })
                await store.mark_item_terminal(
                    item.key, state=m.ITEM_ABANDONED, error="item has no model"
                )
                continue
            by_model[item.model].append(item)

        for model, group in by_model.items():
            chunk = group[:MAX_ITEMS_PER_JOB]
            job_id = uuid.uuid4().hex[:20]
            display_name = f"llm_batch-{model}-{job_id[:8]}"

            provider_job_name, error = await provider.submit(
                model=model, items=chunk, display_name=display_name
            )
            if not provider_job_name:
                # Nothing was accepted; items stay pending and retry next tick.
                logger.warn("llm_batch.poller: submit rejected, items stay pending", {
                    "model": model,
                    "items": len(chunk),
                    "error": error,
                })
                continue

            created = await store.create_job(
                m.BatchJob(
                    job_id=job_id,
                    job_kind=",".join(sorted({i.job_kind for i in chunk})),
                    model=model,
                    provider_job_name=provider_job_name,
                    state=m.JOB_PENDING,
                    item_count=len(chunk),
                    display_name=display_name,
                    submitted_at=m.utcnow(),
                )
            )
            if not created:
                # The provider has the work but we failed to record it. Cancel so
                # nobody pays for results nothing will ever collect.
                logger.error("llm_batch.poller: job record failed, cancelling remote job", {
                    "provider_job_name": provider_job_name,
                    "job_id": job_id,
                })
                await provider.cancel(provider_job_name)
                continue

            await store.attach_items_to_job([i.key for i in chunk], job_id=job_id)
            jobs_made += 1
            items_sent += len(chunk)
    finally:
        # Token-fenced: if this tick overran the lease TTL and someone else now
        # owns the slot, this is a no-op rather than clearing their lease.
        await store.release_submit_lease(lease)

    if jobs_made:
        logger.info("llm_batch.poller: submitted", {
            "jobs": jobs_made,
            "items": items_sent,
        })
    return {"submitted_jobs": jobs_made, "submitted_items": items_sent}


# ---------------------------------------------------------------------------
# Phase 2 — poll and dispatch
# ---------------------------------------------------------------------------


async def _poll_phase() -> dict[str, int]:
    jobs = await store.list_pollable_jobs()
    if not jobs:
        return {"polled": 0, "dispatched": 0}

    now = m.utcnow()
    polled = 0
    dispatched = 0
    for job in jobs:
        if not m.should_poll(job, now=now):
            continue
        polled += 1
        try:
            dispatched += await _advance_job(job, now=now)
        except Exception as exc:
            logger.error("llm_batch.poller: advancing job failed", {
                "job_id": job.job_id,
                "provider_job_name": job.provider_job_name,
                "error": str(exc),
            })
    return {"polled": polled, "dispatched": dispatched}


async def _advance_job(job: m.BatchJob, *, now: datetime) -> int:
    """Poll one job and, if it has landed, dispatch its results. Returns the
    number of items successfully handed to a handler."""

    # A job already SUCCEEDED locally needs fetching, not re-polling.
    if job.state == m.JOB_SUCCEEDED:
        return await _dispatch_job(job)

    result = await provider.poll(job.provider_job_name)

    if not result.reachable:
        # Could not ask. Record ONE poll — writing twice here would double-count
        # `poll_attempts`, which feeds the backoff schedule, so an unreachable job
        # would back off twice as fast as intended.
        expired = m.is_locally_expired(job, now=now)
        if expired:
            logger.error("llm_batch.poller: job unreachable past expiry, abandoning", {
                "job_id": job.job_id,
                "provider_job_name": job.provider_job_name,
                "submitted_at": str(job.submitted_at),
            })
        await store.record_poll(
            job.job_id,
            state=m.JOB_EXPIRED if expired else job.state,
            error="unreachable past expiry" if expired else result.error,
        )
        if expired:
            await _resolve_unfinished_items(job, m.JOB_EXPIRED)
        return 0

    await store.record_poll(
        job.job_id,
        state=result.state,
        failed_request_count=result.failed_count,
        error=result.error,
        result_file_name=result.result_file_name,
    )

    if result.state == m.JOB_SUCCEEDED:
        job.state = m.JOB_SUCCEEDED
        job.result_file_name = result.result_file_name
        if result.failed_count:
            # Documented and expected: a job can finish partially successful.
            logger.warn("llm_batch.poller: job finished with failed keys", {
                "job_id": job.job_id,
                "failed": result.failed_count,
                "succeeded": result.succeeded_count,
            })
        return await _dispatch_job(job)

    if result.state in (m.JOB_FAILED, m.JOB_EXPIRED, m.JOB_CANCELLED):
        logger.warn("llm_batch.poller: job reached terminal failure", {
            "job_id": job.job_id,
            "state": result.state,
            "error": result.error,
        })
        await _resolve_unfinished_items(job, result.state)
        return 0

    # Still pending/running. Nothing to do but let backoff carry it.
    return 0


async def _dispatch_job(job: m.BatchJob) -> int:
    """Fetch a finished job's results and hand each to its registered handler."""
    fetched = await provider.fetch_results(job.result_file_name)
    if not fetched.reachable:
        # Results exist but we could not download them. Leave the job SUCCEEDED
        # so the next tick retries; they are retained for six weeks.
        return 0

    items = await store.list_items_for_job(job.job_id)
    if not items:
        logger.error("llm_batch.poller: job has no items to dispatch", {
            "job_id": job.job_id,
            "result_keys": len(fetched.lines),
        })
        await store.mark_job_dispatched(job.job_id)
        return 0

    if not fetched.lines:
        logger.error("llm_batch.poller: job returned zero usable result lines", {
            "job_id": job.job_id,
            "item_count": len(items),
        })

    # Every handler side effect runs first; the bookkeeping for all of them is
    # committed in ONE batched write afterwards. That keeps the CLAUDE.md ordering
    # rule (never write an "already did this" marker before the side effect
    # succeeds) while collapsing O(items) sequential round trips into one — which
    # matters because this runs inside an awaited request against a 180s Cloud
    # Scheduler attempt deadline, with no lease held.
    #
    # The trade: a crash mid-loop now replays every handler run so far, not just
    # the last one. Handlers are required to be idempotent (registry docstring),
    # and `mark_job_dispatched` below already accepted exactly this window.
    updates: list[tuple[str, dict]] = []
    dispatched = 0
    for item in items:
        if item.state in m.TERMINAL_ITEM_STATES:
            continue
        line = fetched.lines.get(item.key)

        if line is None or line.error:
            reason = line.error if line is not None else "no result line for key"
            outcome = m.item_outcome_for_terminal_job(
                m.ITEM_SUBMITTED, m.JOB_SUCCEEDED, item.attempt
            )
            if outcome == m.ITEM_PENDING:
                updates.append(
                    (item.key, store.item_requeue_update(attempt=item.attempt + 1, error=reason))
                )
            else:
                updates.append(
                    (item.key, store.item_terminal_update(state=m.ITEM_FAILED, error=reason))
                )
            continue

        handler = registry.get_handler(item.job_kind)
        if handler is None:
            # Retiring loudly beats silently dropping: a renamed job_kind that
            # leaves results undeliverable should be visible, not mysterious.
            logger.error("llm_batch.poller: no handler registered for job_kind", {
                "job_kind": item.job_kind,
                "key": item.key,
                "registered": registry.registered_kinds(),
            })
            updates.append(
                (
                    item.key,
                    store.item_terminal_update(
                        state=m.ITEM_ABANDONED, error="no handler registered"
                    ),
                )
            )
            continue

        try:
            consumed = await handler(item, line)
        except Exception as exc:
            logger.error("llm_batch.poller: handler raised", {
                "job_kind": item.job_kind,
                "key": item.key,
                "error": str(exc),
            })
            consumed = False

        if consumed:
            updates.append((item.key, store.item_dispatched_update()))
            dispatched += 1
        elif item.attempt < m.MAX_ITEM_ATTEMPTS:
            updates.append(
                (
                    item.key,
                    store.item_requeue_update(
                        attempt=item.attempt + 1, error="handler declined"
                    ),
                )
            )
        else:
            updates.append(
                (
                    item.key,
                    store.item_terminal_update(
                        state=m.ITEM_ABANDONED, error="handler declined"
                    ),
                )
            )

    await store.apply_item_updates(updates)
    await store.mark_job_dispatched(job.job_id)
    logger.info("llm_batch.poller: dispatched job", {
        "job_id": job.job_id,
        "items": len(items),
        "dispatched": dispatched,
    })
    return dispatched


async def _resolve_unfinished_items(job: m.BatchJob, job_state: str) -> None:
    """Requeue or abandon the items of a job that failed/expired/was cancelled.

    Committed as ONE atomic batch. Nothing here has a preceding side effect —
    every write is an unconditional state stamp — so unlike dispatch there is no
    ordering to preserve, and batching is strictly more correct: the per-item loop
    this replaces could fail halfway and leave the job's items half-resolved on a
    job `record_poll` had already stamped terminal, which means
    `list_pollable_jobs` would never return it again and the remainder would be
    stranded silently (CLAUDE.md: zero rows and healthy must never look
    identical).
    """
    items = await store.list_items_for_job(job.job_id)
    updates: list[tuple[str, dict]] = []
    requeued = 0
    abandoned = 0
    for item in items:
        if item.state in m.TERMINAL_ITEM_STATES:
            continue
        outcome = m.item_outcome_for_terminal_job(item.state, job_state, item.attempt)
        if outcome == m.ITEM_PENDING:
            updates.append(
                (
                    item.key,
                    store.item_requeue_update(
                        attempt=item.attempt + 1, error=f"job {job_state}"
                    ),
                )
            )
            requeued += 1
        else:
            updates.append(
                (
                    item.key,
                    store.item_terminal_update(
                        state=m.ITEM_ABANDONED, error=f"job {job_state}"
                    ),
                )
            )
            abandoned += 1

    written = await store.apply_item_updates(updates)
    log = logger.error if written != len(updates) else logger.info
    log("llm_batch.poller: resolved items of terminal job", {
        "job_id": job.job_id,
        "job_state": job_state,
        "requeued": requeued,
        "abandoned": abandoned,
        "written": written,
        "expected": len(updates),
    })




__all__ = ["run_batch_tick", "MAX_ITEMS_PER_JOB"]
