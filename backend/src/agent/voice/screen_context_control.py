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


# ── The model-facing half of the same signal ─────────────────────────────────
#
# Everything above tells the USER there is no screen, and only ever runs inside a
# save tool. The model itself was never told anything: the no-evidence branch of
# on_user_turn_completed injected NOTHING, so "no screen this turn" and "a screen
# arrived and was dull" were byte-identical from where the model sits. Asked
# "can you see my screen", it read a prompt that says screen evidence usually
# arrives, a successful enable_screen_context call, and no contradicting fact,
# and answered yes - then flip-flopped for five turns. Absence is not a signal.
# A live 2026-09 macOS session is the write-up.
#
# Only the NEGATIVE is rendered. A live turn already carries <screen_ui_context>,
# which asserts its own presence, so a matching positive marker would be tokens
# on the common path buying nothing.
SCREEN_STATE_OPEN_TAG = "<screen_state>"

# reason -> the cause clause. Distinct from _NO_SCREEN_LINES on purpose: that is
# copy to be spoken verbatim, this is a fact for the model to reason from and
# phrase itself. Both key off the same closed client vocabulary above, so they
# can never disagree about what "no screen" means.
_STATE_CAUSES = {
    REASON_DISABLED: " because screen sharing is off in their Aura settings",
    REASON_PERMISSION: (
        " because macOS has not given Aura screen recording permission"
    ),
    REASON_MODE_CONFLICT: " because Guide Mode currently owns the screen",
    REASON_SIGNED_OUT: " because the Aura desktop app is signed out",
    REASON_CAPTURE_FAILED: " because capture failed on their device",
}


def render_screen_state(reason: str) -> str:
    """The absent-screen marker injected into a turn that carries no evidence.

    ``reason`` is the freshest client-reported skip reason, or "" when the client
    reported nothing at all. Unknown is still a negative: no evidence reached this
    turn either way, and only the actionable next step differs.
    """
    return (
        f"{SCREEN_STATE_OPEN_TAG}\n"
        f"No screen evidence reached you this turn"
        f"{_STATE_CAUSES.get(reason, '')}. You cannot see their screen right now. "
        "Say so plainly if they ask, and never claim otherwise until a screen "
        "block appears.\n"
        "</screen_state>"
    )


def remove_screen_state_messages(chat_ctx) -> int:
    """Drop every ``<screen_state>`` system message. Returns the count.

    Exactly one may be live, for the same reason exactly one screen context may
    be (see ``collapse_stale_contexts``): two markers means two answers to "can
    you see me" with nothing saying which is current. Unlike a screen context
    there is no placeholder, because a spent absence records nothing.

    Removing a list ENTRY is safe on a shallow ``ChatContext.copy()`` and editing
    a message's content list in place is NOT, since copies share the same
    ``ChatMessage`` objects. So this only ever deletes, never rewrites.
    """
    items = getattr(chat_ctx, "items", None)
    if items is None:
        return 0
    marked = [
        index
        for index, item in enumerate(items)
        if getattr(item, "role", None) == "system"
        and isinstance(getattr(item, "content", None), list)
        and any(
            isinstance(part, str) and SCREEN_STATE_OPEN_TAG in part
            for part in item.content
        )
    ]
    # Reverse order so earlier indices stay valid as entries are removed.
    for index in reversed(marked):
        del items[index]
    return len(marked)


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
