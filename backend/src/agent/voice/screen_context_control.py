"""Screen-context availability signalling and the agent's enable request.

Two halves of one contract with the desktop (ECOSYSTEM.md):

* Inbound ``screen_context.unavailable``: the client publishes this once per
  turn when it SKIPS capture (setting off, macOS permission missing, Guide
  Mode owns the screen, signed out, capture crashed). Before this signal the
  worker could not tell "user disabled screen context" from "client crashed"
  from "capture in flight" - every one was silence. The reason is a closed
  vocabulary; anything else is recorded as ``capture_failed``.

* Outbound ``screen_context.request``: mirrors ``guide.request``'s shape. The
  desktop stays the sole authority over the privacy setting: it shows an
  explicit consent prompt and only the user's own click flips
  ``voiceScreenContext``. This module can only ask, and the spoken line never
  claims the change already happened.
"""

from __future__ import annotations

from ...lib.logger import logger
from .transport import current_room, publish_client_event

SCREEN_CONTEXT_UNAVAILABLE_TYPE = "screen_context.unavailable"
SCREEN_CONTEXT_REQUEST_TYPE = "screen_context.request"

# Closed vocabulary for the client's skip reason. Mirrors the desktop's
# security decision points (generalSettings voiceScreenContext, security.rs
# denials, macOS screen_capture_permitted).
REASON_DISABLED = "screen_context_disabled"
REASON_PERMISSION = "permission_denied"
REASON_MODE_CONFLICT = "mode_conflict"
REASON_SIGNED_OUT = "signed_out"
REASON_CAPTURE_FAILED = "capture_failed"
UNAVAILABLE_REASONS = frozenset(
    {
        REASON_DISABLED,
        REASON_PERMISSION,
        REASON_MODE_CONFLICT,
        REASON_SIGNED_OUT,
        REASON_CAPTURE_FAILED,
    }
)

_GENERIC_NO_SCREEN_LINE = (
    "I can't see your screen right now, so there's nothing to save."
)

# reason -> the specific spoken line. Only reasons with a user-actionable next
# step get bespoke copy; the rest stay generic on purpose.
_NO_SCREEN_LINES = {
    REASON_DISABLED: (
        "I can't see your screen because screen sharing is off in your Aura "
        "settings. Want me to ask the app to turn it on?"
    ),
    REASON_PERMISSION: (
        "I can't see your screen because macOS hasn't given Aura screen "
        "recording permission. You can grant it under System Settings, "
        "Privacy and Security, Screen Recording."
    ),
    REASON_MODE_CONFLICT: (
        "I can't grab your screen while Guide Mode has it. Stop Guide Mode "
        "and ask me again."
    ),
}

SPOKEN_ENABLE_REQUESTED = (
    "Done - Aura is asking for your okay to share your screen. Approve it and "
    "I'll be able to see what you see."
)
SPOKEN_ENABLE_REQUEST_FAILED = (
    "I couldn't reach the Aura app to ask. You can turn screen sharing on "
    "yourself in Aura's settings."
)


def normalize_reason(reason: str) -> str:
    return reason if reason in UNAVAILABLE_REASONS else REASON_CAPTURE_FAILED


def spoken_no_screen_line(reason: str) -> str:
    """The line Buddy speaks when a save finds no screen, keyed on the freshest
    client-reported reason (empty string when no signal arrived)."""
    return _NO_SCREEN_LINES.get(reason, _GENERIC_NO_SCREEN_LINE)


async def request_screen_context(*, user_id: str, session_id: str) -> str:
    """Ask the desktop to prompt the user to enable screen sharing.

    Never raises (fail-soft, same contract as request_guide_mode). The desktop
    shows the consent prompt; nothing here or there flips the setting without
    the user's explicit click.
    """
    published = await publish_client_event(
        current_room(),
        SCREEN_CONTEXT_REQUEST_TYPE,
        {},
        log_message="screen_context_control: request publish failed",
        log_fields={"user_id": user_id, "session_id": session_id},
    )
    if not published:
        return SPOKEN_ENABLE_REQUEST_FAILED
    logger.info(
        "screen_context_control: enable request published",
        {"user_id": user_id, "session_id": session_id},
    )
    return SPOKEN_ENABLE_REQUESTED
