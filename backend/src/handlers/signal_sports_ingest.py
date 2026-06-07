"""
POST /internal/signal-engine/sports-ingest

Cloud Scheduler hits this endpoint every 30 minutes to push free cricbuzz live
match scores into the content pool. Gated by the same juno-scheduler OIDC check
used by the other /internal/* endpoints.

Runs independently of the hourly full ingest so live content (2h TTL) stays
fresh during matches without blocking or delaying the HN/arXiv pipeline.
"""

from __future__ import annotations

from ..lib.logger import logger
from ..services.signal_engine.content_ingest import run_sports_ingest


async def handle_signal_sports_ingest() -> dict:
    summary = await run_sports_ingest()
    logger.info("signal_sports_ingest: completed", {
        "live_cricket": summary.live_cricket_fetched,
        "written": summary.total_written,
    })
    return {
        "live_cricket_fetched": summary.live_cricket_fetched,
        "total_written": summary.total_written,
    }
