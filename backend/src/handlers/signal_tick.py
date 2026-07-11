"""
POST /internal/signal-engine/tick

The scoring endpoint. No recurring Cloud Scheduler job targets it anymore:
each completed 4-hour content ingest enqueues ONE durable Cloud Task that
lands here (see handlers/signal_content_ingest.py), and the same endpoint
doubles as the authenticated manual-recovery path. The scheduler auth guard
in main.py verifies the OIDC token from the juno-scheduler service account
before this handler runs.

The request body may carry {"generation_id": "20260709T1200Z"} (Cloud Task
path). A manual recovery call may omit it — the current 4-hour UTC bucket is
derived instead, so "just re-run scoring" converges on the same generation
the ingest would have produced.

Idempotency (generation_store.claim_for_scoring, atomic):
  * already complete      -> 200 no-op (duplicate Cloud Task delivery).
  * running, live lease   -> 409; the retry lands after the holder finished
    (then no-ops) or after its lease expired (then reclaims).
  * pending / failed / expired-lease running -> claimed and scored here; a
    failure marks the generation failed and re-raises so Cloud Tasks retries.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from ..lib.logger import logger
from ..services.signal_engine.content_pool import MAX_NEAREST_CANDIDATES
from ..services.signal_engine.generation_store import (
    ClaimOutcome,
    ScoringRunStats,
    claim_for_scoring,
    generation_id_for,
    mark_scoring_complete,
    mark_scoring_failed,
)
from ..services.signal_engine.scoring_loop import run_tick


async def handle_signal_tick(body: dict[str, Any] | None = None) -> dict:
    payload = body or {}
    requested_generation_id = str(payload.get("generation_id", "") or "")
    generation_id = requested_generation_id or generation_id_for(datetime.now(UTC))

    claim = await claim_for_scoring(generation_id)
    if claim is ClaimOutcome.ALREADY_COMPLETE:
        logger.info("signal_tick: generation already scored, no-op", {
            "generation_id": generation_id,
        })
        return {"status": "skipped_already_complete", "generation_id": generation_id}
    if claim is ClaimOutcome.LEASE_HELD:
        logger.info("signal_tick: generation lease held by a live worker, deferring", {
            "generation_id": generation_id,
        })
        raise HTTPException(
            status_code=409,
            detail=f"generation {generation_id} is being scored by another worker",
        )

    try:
        summary = await run_tick()
    except Exception as exc:
        await mark_scoring_failed(generation_id, error=str(exc))
        logger.error("signal_tick: scoring failed, generation left retryable", {
            "generation_id": generation_id,
            "error": str(exc),
        })
        raise

    users_skipped = summary.users_skipped_no_state + summary.users_skipped_no_candidates
    await mark_scoring_complete(
        generation_id,
        ScoringRunStats(
            users_considered=summary.users_considered,
            users_scored=summary.users_scored,
            users_skipped=users_skipped,
            knn_query_count=summary.knn_queries,
        ),
    )
    logger.info("signal_tick: completed", {
        "generation_id": generation_id,
        "users_considered": summary.users_considered,
        "users_scored": summary.users_scored,
        "users_skipped": users_skipped,
        "knn_query_count": summary.knn_queries,
        "estimated_knn_documents_returned": summary.knn_queries * MAX_NEAREST_CANDIDATES,
        "notifications_sent": summary.notifications_sent,
        "timeouts_swept": summary.timeouts_swept,
    })
    return {
        "status": "complete",
        "generation_id": generation_id,
        "users_considered": summary.users_considered,
        "users_scored": summary.users_scored,
        "users_skipped_no_state": summary.users_skipped_no_state,
        "users_skipped_no_candidates": summary.users_skipped_no_candidates,
        "knn_query_count": summary.knn_queries,
        "notifications_sent": summary.notifications_sent,
        "blocked_below_threshold": summary.blocked_below_threshold,
        "blocked_daily_cap": summary.blocked_daily_cap,
        "timeouts_swept": summary.timeouts_swept,
    }
