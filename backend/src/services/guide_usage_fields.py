"""Guide Mode rollup contract - the single source of truth for the ``guide_*``
field names on ``users/{uid}``.

Three writers share this rollup (the store itself, ``handlers/guide_usage.py``
for the desktop's POST, and the voice worker's ``GuideCoordinator``); each
imports its field names from HERE, never inlines a string literal, so a rename
can never silently split the writer/reader contract. The rollup lives as flat
fields on the user document (no subcollection): lifetime counters plus one
latest-session snapshot.
"""

from __future__ import annotations

# --- writers ------------------------------------------------------------------
WRITER_DESKTOP = "desktop"
WRITER_VOICE = "voice"

# --- snapshot bookkeeping (written by the store on every accepted snapshot) ---
LAST_SESSION_ID = "guide_last_session_id"
LAST_ENDED_MS = "guide_last_ended_ms"
LAST_UPDATED_AT = "guide_last_updated_at"


def counted_marker(writer: str) -> str:
    """The idempotency marker for one writer's additive counters: the last
    ``guide_session_id`` whose increments were applied. A replayed call for
    the same session finds its own marker and skips the counters."""
    return f"guide_counted_{writer}_session_id"


# --- desktop snapshot fields (handlers/guide_usage.py) ------------------------
LAST_STARTED_AT = "guide_last_started_at"
LAST_ENDED_AT = "guide_last_ended_at"
LAST_DURATION_MS = "guide_last_duration_ms"
LAST_OUTCOME = "guide_last_outcome"
LAST_FRAMES_SENT = "guide_last_frames_sent"
LAST_STEPS_RECEIVED = "guide_last_steps_received"
LAST_AGENT_TIMEOUTS = "guide_last_agent_timeouts"

# --- desktop lifetime counters ------------------------------------------------
SESSIONS_COUNT = "guide_sessions_count"
TOTAL_MS = "guide_total_ms"
COMPLETED_COUNT = "guide_completed_count"
FRAMES_SENT_TOTAL = "guide_frames_sent_total"

# --- voice-worker snapshot fields (agent/voice/guide_mode.py) -----------------
LAST_VOICE_SESSION_ID = "guide_last_voice_session_id"
LAST_MODEL = "guide_last_model"
LAST_PROVIDER = "guide_last_provider"
LAST_AVG_TTFT_MS = "guide_last_avg_ttft_ms"
LAST_TOOLS_USED = "guide_last_tools_used"
LAST_USER_TURN = "guide_last_user_turn"
LAST_FRAMES_PROCESSED = "guide_last_frames_processed"
