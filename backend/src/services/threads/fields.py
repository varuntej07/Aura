"""Open-loop thread field names — the single source of truth.

Per the data-layer discipline in CLAUDE.md, every writer and every reader of a
thread document references these constants instead of hard-coding the string, so
a rename can never silently split the contract (the "zero rows looks healthy"
trap). ``backend/tests/test_thread_field_contract.py`` round-trips a write
through a read and fails CI if any name here drifts away from what the model
serialises.

Firestore layout:

    users/{uid}/threads/{thread_id}            # one open-loop / curiosity thread
    users/{uid}/threads_state/state            # per-user reflector send budget
"""

from __future__ import annotations

# Subcollection holding one document per open-loop thread.
THREADS_SUBCOLLECTION = "threads"

# Per-user reflector budget document (mirrors signal_store/state).
THREADS_STATE_SUBCOLLECTION = "threads_state"
THREADS_STATE_DOC_ID = "state"

# Server-authoritative conversation for a thread. Silent shade-replies (the user
# answers inside the notification without opening the app) are written here so
# the exchange survives and the client can reconcile it when the thread is
# opened — the main chat history stays client-owned and untouched.
THREAD_MESSAGES_SUBCOLLECTION = "messages"

# ── Thread document fields ──────────────────────────────────────────────────
FIELD_THREAD_ID = "thread_id"
FIELD_TRIGGER_TEXT = "trigger_text"          # the user's own words that opened the loop
FIELD_SOURCE = "source"                      # ThreadSource value
FIELD_SOURCE_REF = "source_ref"              # id of the originating reminder / message
FIELD_CATEGORY = "category"                  # UserAura taxonomy slug, when known
FIELD_KNOWN_SUMMARY = "known_summary"        # what Buddy already knows
FIELD_UNKNOWN = "unknown"                     # list[str] — the holes worth asking about
FIELD_STATUS = "status"                      # ThreadStatus value
FIELD_CREATED_AT = "created_at"
FIELD_LAST_TOUCHED_AT = "last_touched_at"    # last time the user referenced this loop
FIELD_EXPECTED_RESOLUTION_AT = "expected_resolution_at"
FIELD_FOLLOW_UPS_SENT = "follow_ups_sent"
FIELD_LAST_FOLLOW_UP_AT = "last_follow_up_at"

# ── threads_state document fields (reflector daily budget) ──────────────────
FIELD_FOLLOW_UPS_TODAY = "follow_ups_today"
FIELD_FOLLOW_UPS_TODAY_DATE = "follow_ups_today_date"   # "YYYY-MM-DD" in user-local tz
FIELD_STATE_LAST_FOLLOW_UP_AT = "last_follow_up_at"

# ── thread message document fields (server-authoritative conversation) ──────
MSG_ROLE = "role"          # "user" | "assistant"
MSG_CONTENT = "content"
MSG_CREATED_AT = "created_at"
MSG_ORIGIN = "origin"      # "notification_reply" — provenance for reconciliation
