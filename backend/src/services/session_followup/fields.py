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
FOLLOWUP_MIN_DELAY = timedelta(minutes=55)
FOLLOWUP_MAX_DELAY = timedelta(minutes=75)
FOLLOWUP_MAX_AGE = timedelta(hours=6)
OTHER_TOPIC_DEFER = timedelta(minutes=15)
QUIET_HOURS_DEFER = timedelta(minutes=30)

EVALUATOR_VERSION = "session-followup-v1"
MIN_MEANINGFUL_TURNS = 3
COLD_START_SESSION_COUNT = 3
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
    "cross_surface_session_ids_separate": True,
}


def feature_enabled(settings: object) -> bool:
    return bool(
        getattr(settings, "FOLLOWUP_SHADOW", False)
        or getattr(settings, "PROACTIVE_FOLLOWUP_SEND", False)
    )
