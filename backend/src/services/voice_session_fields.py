"""Voice-session document contract - the single source of truth for the
identity, surface, and structured-memory field names on
``users/{uid}/voice_sessions/{voice_run_id}`` and the companion
``users/{uid}/voice_session_state/latest`` digest.

The writer (``voice_session_summarizer._write_session_doc``) and every reader
(``handlers/history.py``) reference these constants so a rename can never
silently desync the two sides (see CLAUDE.md's data-layer discipline rule).

Canonical identity fields (added at schema_version 2):

    voice_run_id     the voice run's own id. EQUALS the id the worker mints
                     (still called ``session_id`` inside the worker) and stays
                     the ``voice_sessions`` doc id. Persisted redundantly under
                     this key so a reader never has to infer it from the doc id.
    conversation_id  the client-generated chat thread id, handed to
                     /voice/token and stamped into the LiveKit participant
                     metadata. "" for legacy/older clients that didn't send it.
    surface          one of "app" | "keyboard" | "desktop"; legacy/missing
                     reads default to "unknown".
    schema_version   integer 2 for any doc written with these new fields.

Structured-memory fields (added at schema_version 2):

    recap    1-2 friendly sentences for the history UI.
    actions  the SAFE structured action receipts (tool_name/call_id/
             occurred_at) for tools that actually executed successfully this
             run. Built ONLY from executor receipts, never from the LLM, so a
             summary can never assert a reminder/calendar write that never ran.
"""

from __future__ import annotations

# --- identity + surface (schema_version 2) -----------------------------------
SCHEMA_VERSION = "schema_version"
VOICE_RUN_ID = "voice_run_id"
CONVERSATION_ID = "conversation_id"
SURFACE = "surface"

# --- structured memory (schema_version 2) ------------------------------------
RECAP = "recap"
ACTIONS = "actions"
OPEN_LOOPS = "open_loops"
DECISIONS = "decisions"
EMOTIONAL_CONTEXT = "emotional_context"
FACTS = "facts"
FOLLOW_UP = "follow_up"
MEMORY_CONTEXT = "memory_context"

# --- session health (added alongside the zero-turn alarm) --------------------
# Why this is a persisted FIELD and not only a log line: a five-minute run that
# captured zero turns was indistinguishable from a healthy short call in every
# query we could write, which is exactly the trap CLAUDE.md names. A log can be
# grepped for a fortnight; a field can be counted for all time.
HEALTH = "health"
# Whether the 5-minute no-transcript watchdog ended the call rather than the user.
CLOSED_BY_IDLE_TIMEOUT = "closed_by_idle_timeout"

# --- values ------------------------------------------------------------------
# Any doc written with the identity + structured-memory fields above.
SCHEMA_VERSION_V2 = 2
# Reader fallback when a legacy doc predates the surface field.
SURFACE_UNKNOWN = "unknown"

# HEALTH values. "ok" is any run that captured at least one turn, or one too short
# to judge. The rest are defects, and each names what was actually observed rather
# than guessing at a cause.
HEALTH_OK = "ok"
HEALTH_ZERO_TURNS = "zero_turns"          # ran long enough to matter, captured nothing
HEALTH_NO_USER_AUDIO = "no_user_audio"    # participant published no audio track
HEALTH_NO_TRANSCRIPT = "no_transcript"    # audio arrived, STT produced nothing

# Action receipt keys (persisted verbatim in the ACTIONS list). Deliberately a
# closed set: never store unrestricted tool outputs, only these four fields.
ACTION_TOOL_NAME = "tool_name"
ACTION_CALL_ID = "call_id"
ACTION_SUCCESS = "success"
ACTION_OCCURRED_AT = "occurred_at"
ACTION_RESULT = "result"
