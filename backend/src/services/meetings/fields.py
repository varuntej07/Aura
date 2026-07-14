"""Meeting-notes document contract - the single source of truth for field
names, statuses, and caps on ``users/{uid}/meetings/{meeting_id}`` and its
companion docs.

Stored shape (written only by this backend, read by desktop over REST):

    users/{uid}/meetings/{meeting_id} = {
        "event_id":       str,        # calendar instance id, or "manual:{uuid}"
        "title":          str,
        "start_time":     iso8601,    # calendar event window, client-supplied
        "end_time":       iso8601,
        "device_id":      str,        # hostname of the capturing device
        "status":         str,        # capturing -> uploaded -> synthesizing
                                      #   -> ready | excluded | failed
        "cap_minutes":    int,        # synthesis cap frozen at claim time
        "segments":       [ { "seq": int, "start_ms": int, "duration_ms": int } ],
        "segment_count":  int,        # client-reported at /complete
        "total_duration_ms": int,     # client-reported at /complete
        "created_at":     iso8601,
        "updated_at":     iso8601,
        "note":           { "summary": str, "decisions": [str],
                            "action_items": [str], "open_questions": [str],
                            "language": str, "one_sided": bool },   # ready only
        "expires_at":     timestamp,  # native datetime; set for non-pro tiers
                                      #   only, drives the Firestore TTL policy
    }

    users/{uid}/meeting_claims/{event_key} = {
        "event_id":   str,            # raw id (event_key is its sha1 hex)
        "meeting_id": str,
        "device_id":  str,
        "expires_at_ms": int,         # event end + grace; a live lock past this
                                      #   is stale and can be re-claimed
    }

    users/{uid}/usage/meetings_{YYYYMM} = { "count": int }
        The monthly cap counter. Charged transactionally at claim (never at
        synthesis, so worker retries can never double-bill). The doc id carries
        the month, so there is no rollover logic - a new month is a new doc.

    users/{uid}/settings/meeting_notes = { "exclude_keywords": [str] }
        Sensitive-meeting exclude list, matched against the meeting title
        before any STT runs. Absent doc means an empty list. A free feature,
        not a paywall lever (MEETING_NOTES_PLAN.md section 4).

Retention: ``expires_at`` = now + RETENTION_DAYS for free/companion; pro notes
carry no TTL. One-time infra step (mirrors drafts): ``gcloud firestore fields
ttls update expires_at --collection-group=meetings --enable-ttl``. TTL deletion
can lag ~72h, so ``store.list_recent`` also drops expired rows itself.
"""

from __future__ import annotations

# --- Firestore locations -----------------------------------------------------
PARENT_COLLECTION = "users"
SUBCOLLECTION = "meetings"
CLAIMS_SUBCOLLECTION = "meeting_claims"
USAGE_SUBCOLLECTION = "usage"
SETTINGS_SUBCOLLECTION = "settings"
SETTINGS_DOC = "meeting_notes"

# --- meeting doc fields --------------------------------------------------------
EVENT_ID = "event_id"
TITLE = "title"
START_TIME = "start_time"
END_TIME = "end_time"
DEVICE_ID = "device_id"
STATUS = "status"
CAP_MINUTES = "cap_minutes"
SEGMENTS = "segments"
SEGMENT_COUNT = "segment_count"
TOTAL_DURATION_MS = "total_duration_ms"
CREATED_AT = "created_at"
UPDATED_AT = "updated_at"
NOTE = "note"
EXPIRES_AT = "expires_at"
COMPLETE_REASON = "complete_reason"

# --- claim-lock fields ----------------------------------------------------------
CLAIM_EVENT_ID = "event_id"
CLAIM_MEETING_ID = "meeting_id"
CLAIM_DEVICE_ID = "device_id"
CLAIM_EXPIRES_AT_MS = "expires_at_ms"

# --- statuses --------------------------------------------------------------------
STATUS_CAPTURING = "capturing"
STATUS_UPLOADED = "uploaded"
STATUS_SYNTHESIZING = "synthesizing"
STATUS_READY = "ready"
STATUS_EXCLUDED = "excluded"
STATUS_FAILED = "failed"
# Statuses during which an event's claim lock is honored and segment uploads
# are accepted.
ACTIVE_STATUSES = (STATUS_CAPTURING, STATUS_UPLOADED, STATUS_SYNTHESIZING)

# --- caps / retention ----------------------------------------------------------
# Free AND companion tiers share the meeting cap; only pro is unlimited
# (user decision 2026-07-11, resolving the GROWTH_PLAN/SUBSCRIPTION_PLAN
# tier-map conflict). Effective tier "pro" includes trial users.
MONTHLY_MEETING_CAP = 5

# TEMPORARY 60-MINUTE CLAMP (product decision 2026-07-11): meeting notes only
# supports meetings up to one hour FOR NOW, on every tier. Longer meetings
# (multi-hour classes, workshops) are out of scope until a long-meeting cost
# model and UX exist; the desktop mirrors this (auto-arm eligibility ceiling
# plus a 60-minute capture hard stop), so these server caps are the
# defense-in-depth layer against modified clients, not the primary gate.
# Design values to restore when long-meeting support lands:
# PRO_SYNTHESIS_CAP_MINUTES = 240, MAX_CAPTURE_MINUTES = 240,
# MAX_SEGMENTS_PER_MEETING = 100 (see MEETING_NOTES_PLAN.md section 4).
FREE_SYNTHESIS_CAP_MINUTES = 60
PRO_SYNTHESIS_CAP_MINUTES = 60
MAX_CAPTURE_MINUTES = 60
RETENTION_DAYS = 7
LIST_LIMIT = 20

# A claim lock is honored until the calendar event's end plus this grace, so a
# drop-and-rejoin lands on the same meeting while a brand-new capture of the
# same event hours later gets a fresh one.
CLAIM_GRACE_MINUTES = 30

# Segment upload ceiling: Cloud Run caps request bodies at 32 MB; the client
# closes segments around 10-12 MB (5 min of 2ch 16 kHz FLAC), so anything near
# this limit is malformed, not just large.
MAX_SEGMENT_BYTES = 30 * 1024 * 1024

# Upload-side abuse bounds. The honest client writes ~12 five-minute segments
# for a 60-minute capture; early closes (pause boundaries, 24 MB early cuts)
# can roughly double that. Anything past these is a modified client, not a
# long meeting - offsets and durations are client-supplied and MUST be
# range-checked because the synthesis cap keys off them.
MAX_SEGMENTS_PER_MEETING = 30
MAX_SEGMENT_DURATION_MS = 6 * 60_000
MAX_SEGMENT_START_MS = MAX_CAPTURE_MINUTES * 60_000

# One synthesis run may hold the "synthesizing" status this long before a
# Cloud Tasks redelivery is allowed to re-claim it (crashed-worker recovery
# without letting a concurrent duplicate double-run STT+LLM).
SYNTHESIS_LEASE_MS = 30 * 60_000
SYNTHESIS_STARTED_AT_MS = "synthesis_started_at_ms"

# Machine codes the desktop client matches on (mirrors the /voice/token cap
# contract shape: 402 + {"detail": {"code": ..., "seconds_until_reset": ...}}).
MEETING_CAP_CODE = "meeting_cap_reached"
MEETING_CONFLICT_CODE = "meeting_already_claimed"
