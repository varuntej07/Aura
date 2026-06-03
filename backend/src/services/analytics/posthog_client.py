"""
Fail-safe, fire-and-forget PostHog capture for server-side product analytics.

The scoring loop uses this to emit the top of the re-engagement funnel
(``signal_notification_sent``). Capture must NEVER raise into, or block, a
scoring tick: a PostHog outage, a missing key, an import error, or a bad payload
degrades to a no-op plus a log line, nothing more.

Configuration comes from ``settings.POSTHOG_API_KEY`` / ``POSTHOG_HOST``. When
the key is blank (e.g. local dev), every call is a silent no-op, mirroring the
Flutter client which only captures outside dev.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ...config.settings import settings
from ...lib.logger import logger

# Memoised client. _init_attempted ensures we only try (and only log) once.
_client: Any | None = None
_init_attempted = False


def _get_client() -> Any | None:
    global _client, _init_attempted
    if _init_attempted:
        return _client
    _init_attempted = True

    if not settings.posthog_configured:
        logger.info("posthog_client: no POSTHOG_API_KEY — server analytics disabled")
        return None

    try:
        from posthog import Posthog

        _client = Posthog(
            project_api_key=settings.POSTHOG_API_KEY,
            host=settings.POSTHOG_HOST,
        )
        logger.info("posthog_client: initialised", {"host": settings.POSTHOG_HOST})
    except Exception as exc:
        logger.warn("posthog_client: init failed — server analytics disabled", {
            "error": str(exc),
        })
        _client = None
    return _client


async def capture_event(
    *,
    distinct_id: str,
    event: str,
    properties: dict[str, Any] | None = None,
) -> None:
    """Fire-and-forget capture. Never raises, never blocks the caller's logic."""
    client = _get_client()
    if client is None:
        return

    def _send() -> None:
        try:
            client.capture(
                distinct_id=distinct_id,
                event=event,
                properties=properties or {},
            )
        except Exception as exc:
            logger.warn("posthog_client: capture failed", {
                "event": event,
                "error": str(exc),
            })

    try:
        await asyncio.to_thread(_send)
    except Exception:
        # Scheduling the thread itself failed so, analytics is best-effort to swallow.
        pass


async def flush() -> None:
    """Drain the SDK's background queue to the server before the caller returns.

    ``capture_event`` hands each event to the PostHog SDK's background consumer
    thread; on Cloud Run the container is frozen the moment a scoring tick
    returns, so any still-queued events are lost (a silent step-1 undercount).
    Calling this at the end of ``run_tick`` forces the queue out the door first.

    ``Posthog.flush()`` blocks on ``queue.join()`` until the consumer drains, so
    it runs on a worker thread to keep the event loop free. Never raises — a
    flush failure degrades to a log line, like every other call here.
    """
    if not _init_attempted:
        # Nothing was ever captured this process, so there is no client/queue to
        # drain. Avoid lazily constructing one just to flush an empty queue.
        return
    client = _client
    if client is None:
        return

    def _flush() -> None:
        try:
            client.flush()
        except Exception as exc:
            logger.warn("posthog_client: flush failed", {"error": str(exc)})

    try:
        await asyncio.to_thread(_flush)
    except Exception:
        pass
