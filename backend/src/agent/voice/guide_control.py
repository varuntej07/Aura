"""Agent-requested Guide Mode arm/disarm, published to the desktop.

Guide Mode is armed natively on the desktop (Rust pins the cursor's monitor and
checks the signed-in session), so the worker can only REQUEST the change, never
force it. This module publishes a ``guide.request`` event over the data channel;
``useGuideMode`` on the desktop validates it and routes it to the native
``arm_guide``/``disarm_guide`` command, and the existing armed -> publish ->
activate loop lights the status dot once capture is truly live.

The spoken lines are honest by construction: they never claim Guide Mode is
already on, only that it is starting. Same fail-soft contract as
``visible_artifacts`` and ``draft_outbound`` - a lost packet degrades to a
spoken line that does NOT claim success, never a raised tool error mid-turn.
"""

from __future__ import annotations

from ...lib.logger import logger
from .transport import current_room, publish_client_event

GUIDE_REQUEST_TYPE = "guide.request"

SPOKEN_GUIDE_STARTING = (
    "Starting guide mode now, you'll see the dot light up once I'm watching your screen."
)
SPOKEN_GUIDE_STOPPING = "Okay, turning guide mode off."
SPOKEN_GUIDE_REQUEST_FAILED = (
    "I couldn't switch guide mode just now, try the Control Alt G shortcut."
)


async def request_guide_mode(*, user_id: str, session_id: str, enable: bool) -> str:
    """Ask the desktop to arm/disarm Guide Mode. Never raises (fail-soft)."""
    published = await publish_client_event(
        current_room(),
        GUIDE_REQUEST_TYPE,
        {"enable": enable},
        log_message="guide_control: request publish failed",
        log_fields={
            "user_id": user_id,
            "session_id": session_id,
            "enable": enable,
        },
    )
    if not published:
        return SPOKEN_GUIDE_REQUEST_FAILED
    logger.info(
        "guide_control: request published",
        {"user_id": user_id, "session_id": session_id, "enable": enable},
    )
    return SPOKEN_GUIDE_STARTING if enable else SPOKEN_GUIDE_STOPPING
