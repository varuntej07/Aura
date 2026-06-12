"""Icebreaker Firestore field names — the single source of truth.

Per the data-layer discipline in CLAUDE.md, every writer and reader of the
icebreaker state document references these constants instead of hard-coding the
string, so a rename can never silently break the weekly-roll / daily-claim
contract (the "zero rows looks healthy" trap). The store round-trips a write
through a read in ``backend/tests/test_icebreaker_store.py``.

Firestore layout (one document per user, no collection-group query, no index):

    users/{uid}/icebreaker_state/state
"""

from __future__ import annotations

# Per-user icebreaker state document (mirrors signal_store/state, threads_state/state).
ICEBREAKER_STATE_SUBCOLLECTION = "icebreaker_state"
ICEBREAKER_STATE_DOC_ID = "state"

# ── icebreaker_state document fields ────────────────────────────────────────
# The user-local Sunday ("YYYY-MM-DD") that SCHEDULED_DATES was rolled for. 
# When the current week differs, the engine re-rolls (lazily, on the first tick it sees
# a new week, robust to a missed Sunday).
FIELD_WEEK_START_DATE = "week_start_date"

# The 3 user-local dates ("YYYY-MM-DD") chosen for this week by the dice roll.
FIELD_SCHEDULED_DATES = "scheduled_dates"

# The user-local date of the LAST send. The atomic claim sets this to today, which
# is what makes "at most one icebreaker per day" idempotent under overlapping
# ticks, a second tick reads today already stamped and stands down.
FIELD_LAST_SENT_DATE = "last_sent_date"

# Compact topics of previously-sent openers, newest last. Fed back into the
# planner prompt so Buddy never asks the same thing twice. Capped to
# MAX_RECENT_OPENER_TOPICS (FIFO) so the document can never approach Firestore's
# 1 MiB limit — at ~3 sends/week the cap still covers months of history.
FIELD_RECENT_OPENER_TOPICS = "recent_opener_topics"

# Lifetime count of icebreakers sent to this user (audit / analytics only).
FIELD_TOTAL_SENT = "total_sent"

# Last send timestamp (UTC datetime) — audit only.
FIELD_LAST_SENT_AT = "last_sent_at"

# Newest-last history of opener topics is capped here. Generous: at 3/week this is
# ~4 months of memory, effectively "every opener" for the beta while bounding the
# document size.
MAX_RECENT_OPENER_TOPICS = 50

# Client routing key in the FCM data payload. The Flutter app switches on this to
# open chat seeded with the opener (mirrors signal_engine / thread_followup).
NOTIFICATION_TYPE_ICEBREAKER = "icebreaker"
