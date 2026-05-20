"""
POST /internal/signal-engine/tick

Cloud Scheduler hits this endpoint every 15 minutes. The scheduler auth
guard in main.py verifies the OIDC token from the juno-scheduler service
account before this handler runs.
"""

from __future__ import annotations

from ..lib.logger import logger
from ..services.signal_engine.scoring_loop import run_tick


async def handle_signal_tick() -> dict:
    summary = await run_tick()
    logger.info("signal_tick: completed", {
        "users_considered": summary.users_considered,
        "notifications_sent": summary.notifications_sent,
        "timeouts_swept": summary.timeouts_swept,
    })
    return {
        "users_considered": summary.users_considered,
        "users_skipped_no_state": summary.users_skipped_no_state,
        "notifications_sent": summary.notifications_sent,
        "blocked_below_threshold": summary.blocked_below_threshold,
        "blocked_daily_cap": summary.blocked_daily_cap,
        "timeouts_swept": summary.timeouts_swept,
    }
