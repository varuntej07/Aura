"""
POST /internal/signal-engine/content-ingest

Cloud Scheduler hits this endpoint every 4 hours ("0 */4 * * *") to populate
content_candidates from the existing data_fetchers. The handler is gated by
the same juno-scheduler OIDC check used by the other /internal/* endpoints.

Scoring is INGEST-TRIGGERED, not clock-triggered: after a successful ingest
this handler records the 4-hour generation (signal_engine/generation_store.py)
and enqueues ONE durable Cloud Task that runs the scoring pass against the
freshly refreshed pool. There is no recurring scoring cron anymore — that
design re-ran an identical 50-doc KNN per user every 15-30 min against a pool
that only changes here.

Ordering and failure semantics:
  * run_ingest raises (e.g. embedder quota exhausted) -> nothing is recorded
    or enqueued; the 5xx makes Cloud Scheduler retry the whole ingest, which
    is cheap (add_candidates de-dups by content_id).
  * Ingest succeeds with ZERO new writes -> the generation is still recorded
    and scoring still enqueued exactly once: user vectors changed since the
    last pass even if the pool did not, and the generation guard suppresses
    any duplicate.
  * The enqueue itself is SYNCHRONOUS, before the HTTP response, because Cloud
    Run may freeze the instance the moment the response is sent (never
    asyncio.create_task here). A scheduler retry after a failed enqueue is
    safe: the task name is deterministic per generation, so the retry either
    creates the task or collides with it (AlreadyExists -> treated as done).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from ..lib.logger import logger
from ..services.engagement.task_scheduler import get_task_scheduler
from ..services.signal_engine.content_ingest import run_ingest
from ..services.signal_engine.generation_store import (
    generation_id_for,
    record_ingest_completed,
)


async def handle_signal_content_ingest() -> dict:
    summary = await run_ingest()

    generation_id = generation_id_for(datetime.now(UTC))
    await record_ingest_completed(
        generation_id, new_candidates_written=summary.total_written
    )
    try:
        scoring_task_name = await asyncio.to_thread(
            get_task_scheduler().schedule_signal_scoring, generation_id
        )
    except Exception as exc:
        # Re-raise so the scheduler retries this ingest run; the generation
        # record persists and the retry converges on the same task name.
        logger.error("signal_content_ingest: scoring task enqueue FAILED", {
            "generation_id": generation_id,
            "error": str(exc),
        })
        raise

    logger.info("signal_content_ingest: completed", {
        "google_news": summary.google_news_fetched,
        "newsdata":    summary.newsdata_fetched,
        "written":     summary.total_written,
        "generation_id": generation_id,
        "scoring_task_name": scoring_task_name,
    })
    return {
        "google_news_fetched": summary.google_news_fetched,
        "newsdata_fetched":    summary.newsdata_fetched,
        "total_written":       summary.total_written,
        "generation_id":       generation_id,
        "scoring_task_name":   scoring_task_name,
    }
