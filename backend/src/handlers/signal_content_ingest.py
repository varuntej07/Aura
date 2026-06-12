"""
POST /internal/signal-engine/content-ingest

Cloud Scheduler hits this endpoint hourly to populate content_candidates
from the existing data_fetchers. The handler is gated by the same
juno-scheduler OIDC check used by the other /internal/* endpoints.
"""

from __future__ import annotations

from ..lib.logger import logger
from ..services.signal_engine.content_ingest import run_ingest


async def handle_signal_content_ingest() -> dict:
    summary = await run_ingest()
    logger.info("signal_content_ingest: completed", {
        "google_news": summary.google_news_fetched,
        "newsdata":    summary.newsdata_fetched,
        "written":     summary.total_written,
    })
    return {
        "google_news_fetched": summary.google_news_fetched,
        "newsdata_fetched":    summary.newsdata_fetched,
        "total_written":       summary.total_written,
    }
