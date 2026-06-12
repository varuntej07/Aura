"""Turn a user action into an open-loop thread.

v1 wedge: only user-set reminders. The reminder's own text *is* the thread —
no LLM call at creation, so this adds zero model cost on the hot path. The
curiosity question is generated lazily, much later, by the reflector.

The whole feature is gated behind ``settings.THREAD_ENGINE_ENABLED`` so threads
only start accumulating once the end-to-end path (reflector + client) is live.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ...config.settings import settings
from ...lib.logger import logger
from . import thread_store
from .models import Thread, ThreadSource

# Generic curiosity angles for a reminder. The reflector's framer picks ONE and
# turns it into a specific, warm question — these are only the holes to aim at,
# never shown to the user verbatim.
REMINDER_CURIOSITY_ANGLES = [
    "what this is really about",
    "why it matters to them",
    "how it is going for them",
]


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


async def record_reminder_thread(
    user_id: str,
    *,
    reminder_id: str,
    message: str,
    trigger_at_iso: str,
) -> None:
    """Open a curiosity thread for a freshly created reminder.

    Safe to call fire-and-forget: it never raises (the store swallows write
    errors) and is a no-op while the engine is disabled, so the chat/voice tool
    path is never affected.
    """
    if not settings.THREAD_ENGINE_ENABLED:
        return

    message = (message or "").strip()
    if not message:
        return

    now = datetime.now(UTC)
    thread = Thread(
        thread_id=reminder_id,                       # 1:1 with the reminder; idempotent
        trigger_text=message,
        source=ThreadSource.REMINDER,
        source_ref=reminder_id,
        known_summary=f"The user set a reminder about: {message}",
        unknown=list(REMINDER_CURIOSITY_ANGLES),
        created_at=now,
        last_touched_at=now,
        expected_resolution_at=_parse_iso(trigger_at_iso),
    )
    await thread_store.create_thread(user_id, thread)
    logger.info("threads.thread_writer: opened reminder thread", {
        "user_id": user_id,
        "thread_id": reminder_id,
    })
