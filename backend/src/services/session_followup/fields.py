"""Firestore and policy contract for Phase 6 session follow-ups."""

from __future__ import annotations

from datetime import timedelta

SESSIONS = "sessions"
TURNS = "turns"
SESSION_TOPICS = "session_topics"

STATE_ACTIVE = "active"
STATE_DISCONNECT_GRACE = "disconnect_grace"
STATE_FINALIZED = "finalized"

SURFACE_VOICE = "voice"
SURFACE_CHAT = "chat"
ORIGIN_ORGANIC = "organic"
ORIGIN_NOTIFICATION_TAP = "notification_tap"

VOICE_DISCONNECT_GRACE = timedelta(seconds=90)
VOICE_IDLE_TIMEOUT = timedelta(minutes=5)
CHAT_IDLE_TIMEOUT = timedelta(minutes=30)
# The client says "the user left the chat", which is a hint, not an end. Android
# fires `paused` for a notification-shade pull, a permission dialog, and a
# screenshot, so a backgrounded chat waits out this grace before it can finalize.
# A turn arriving inside the window returns the session to active for free.
CHAT_BACKGROUND_GRACE = timedelta(minutes=2)
FOLLOWUP_MIN_DELAY = timedelta(minutes=55)
FOLLOWUP_MAX_DELAY = timedelta(minutes=75)
FOLLOWUP_MAX_AGE = timedelta(hours=6)
OTHER_TOPIC_DEFER = timedelta(minutes=15)
QUIET_HOURS_DEFER = timedelta(minutes=30)

EVALUATOR_VERSION = "session-followup-v1"
MIN_MEANINGFUL_TURNS = 3
# Sessions a user must have before a topic can earn a follow-up WITHOUT an
# explicit intent signal. Note _prior_finalized_count runs after the current
# session is already marked finalized, so a first-time user counts 1 — meaning
# 1 lets a first conversation qualify, which is the whole point: a user with one
# session was the exact cohort this gate silently locked out of every proactive
# surface. SCORE_THRESHOLD below is still the quality bar.
COLD_START_SESSION_COUNT = 1
SCORE_THRESHOLD = 0.45

SOURCE_SESSION_FOLLOWUP = "session_followup"
NOTIFICATION_TYPE = "session_followup"

VALUE_TYPES = frozenset({
    "new_information",
    "prepared_artifact",
    "unresolved_action",
    "deadline",
    "cross_memory_connection",
    "next_step",
})

FINALIZATION_DEFAULTS = {
    "voice_disconnect_grace_s": int(VOICE_DISCONNECT_GRACE.total_seconds()),
    "voice_idle_s": int(VOICE_IDLE_TIMEOUT.total_seconds()),
    "chat_idle_s": int(CHAT_IDLE_TIMEOUT.total_seconds()),
    "chat_background_grace_s": int(CHAT_BACKGROUND_GRACE.total_seconds()),
    "cross_surface_session_ids_separate": True,
}
