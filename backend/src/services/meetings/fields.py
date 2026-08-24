"""Meeting Recording V2 Firestore contract and stable public enums.

The meeting document is the public projection. Immutable ingest evidence lives
under capture runs and deterministic segment documents; processing, audit, and
deletion each have their own durable collection:

    users/{uid}/meetings/{meeting_id}
      /capture_runs/{capture_run_id}/segments/{seq:06d}
      /audit_events/{sequence-event}
    users/{uid}/meeting_claims/{event_key}
    users/{uid}/meeting_jobs/{job_id}
    users/{uid}/meeting_job_outbox/{job_id}
    users/{uid}/meeting_deletions/{meeting_id}

Claims bind ownership to ``installation_id`` and advance ``capture_fence`` on
recovery. Completion verifies the finalized run, persisted upload receipts,
contiguous identities, manifest digest, and duration before creating its job,
outbox, and audit event in the same transaction.

The monthly cap is charged only at claim, so worker retries never double-bill.
Free/Companion public meeting rows receive the seven-day Firestore TTL; source
audio is not deleted by successful processing. See
``backend/docs/meeting-recording-v2.md`` and the canonical sibling-repository
``MEETING_RECORDING_V2_ARCHITECTURE.md``.
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
INSTALLATION_ID = "installation_id"
RUNTIME_INSTANCE_ID = "runtime_instance_id"
PROTOCOL_VERSION = "protocol_version"
CAPTURE_RUN_ID = "capture_run_id"
CAPTURE_FENCE = "capture_fence"
LEASE_EXPIRES_AT = "lease_expires_at"
DELETION_STATE = "deletion_state"
DELETED_AT = "deleted_at"
AUDIT_SEQUENCE = "audit_sequence"
STATUS = "status"
CAP_MINUTES = "cap_minutes"
SEGMENT_COUNT = "segment_count"
TOTAL_DURATION_MS = "total_duration_ms"
CREATED_AT = "created_at"
UPDATED_AT = "updated_at"
NOTE = "note"
NOTE_TRANSCRIPT = "transcript"
TRANSCRIPT_SPEAKER = "speaker"
TRANSCRIPT_TEXT = "text"
EXPIRES_AT = "expires_at"
COMPLETE_REASON = "complete_reason"
MANIFEST_SHA256 = "manifest_sha256"
COMPLETION_RECEIPT = "completion_receipt"
ARTIFACT_REVISION = "artifact_revision"
ARTIFACTS = "artifacts"
QUALITY_OUTCOME = "quality_outcome"
QUALITY_POLICY_VERSION = "quality_policy_version"

# --- durable processing metadata (explains the coarse STATUS) -------------------
# These fields let the desktop show a stage, a safe reason, and a Retry
# affordance without reading logs. Every status-changing write bumps
# STATUS_REVISION, which is used in notification dedup keys.
PROCESSING_STAGE = "processing_stage"
FAILURE_CODE = "failure_code"
FAILURE_MESSAGE = "failure_message"
RETRYABLE = "retryable"
ATTEMPT_COUNT = "attempt_count"
LAST_ERROR_AT = "last_error_at"
STATUS_REVISION = "status_revision"

# --- claim-lock fields ----------------------------------------------------------
CLAIM_EVENT_ID = "event_id"
CLAIM_MEETING_ID = "meeting_id"
CLAIM_INSTALLATION_ID = "installation_id"
CLAIM_RUNTIME_INSTANCE_ID = "runtime_instance_id"
CLAIM_CAPTURE_RUN_ID = "capture_run_id"
CLAIM_CAPTURE_FENCE = "capture_fence"
CLAIM_LEASE_EXPIRES_AT = "lease_expires_at"
CLAIM_EXPIRES_AT_MS = "expires_at_ms"

# --- statuses --------------------------------------------------------------------
STATUS_CAPTURING = "capturing"
STATUS_UPLOADED = "uploaded"
STATUS_SYNTHESIZING = "synthesizing"
STATUS_READY = "ready"
STATUS_EXCLUDED = "excluded"
STATUS_FAILED = "failed"
STATUS_NEEDS_ATTENTION = "needs_attention"
STATUS_DELETE_REQUESTED = "delete_requested"
STATUS_DELETE_COMPLETE = "delete_complete"
# Statuses during which an event's claim lock is honored and segment uploads
# are accepted.
ACTIVE_STATUSES = (STATUS_CAPTURING, STATUS_UPLOADED, STATUS_SYNTHESIZING)

# --- processing_stage values (finer-grained than STATUS; drives the UI row) ---
# One STATUS can span two stages: "synthesizing" is TRANSCRIBING then
# BUILDING_INSIGHTS, so the worker sets these explicitly at each sub-step.
STAGE_CAPTURING = "capturing"
STAGE_UPLOADING = "uploading"
STAGE_QUEUED = "queued"
STAGE_TRANSCRIBING = "transcribing"
STAGE_BUILDING_INSIGHTS = "building_insights"
STAGE_READY = "ready"
STAGE_UPLOADED_VERIFIED = "uploaded_verified"
STAGE_QUALITY = "quality_evaluation"
STAGE_NEEDS_ATTENTION = "needs_attention"
STAGE_DELETE_REQUESTED = "delete_requested"
STAGE_BLOCK_NEW_WORK = "block_new_work"
STAGE_CLOUD_AUDIO_DELETE = "cloud_audio_delete"
STAGE_TRANSCRIPT_DELETE = "transcript_delete"
STAGE_FIRESTORE_TOMBSTONE = "firestore_tombstone"
STAGE_DELETE_COMPLETE = "delete_complete"

# --- safe failure codes (a stable enum, NEVER a provider exception string) -----
# The desktop maps each to non-blaming copy + one allowed action. upload_* codes
# marked (client) are authored on the desktop from its local queue state; the
# rest are server-authored.
FAIL_UPLOAD_STORAGE_UNAVAILABLE = "upload_storage_unavailable"  # bucket/IAM/outage; retryable
FAIL_NO_AUDIO = "no_audio"  # nothing captured; terminal
FAIL_AUDIO_REJECTED = "audio_rejected"  # STT will reject forever; terminal
FAIL_TRANSCRIPTION_UNAVAILABLE = "transcription_unavailable"  # transient STT; retryable
FAIL_INSIGHT_GENERATION_FAILED = "insight_generation_failed"  # all LLM fallbacks failed; terminal
FAIL_EXCLUDED_SENSITIVE = "excluded_sensitive"  # private-meeting rule; terminal
FAIL_PROCESSING_TIMEOUT = "processing_timeout"  # stuck past threshold; retryable
FAIL_STALE_CAPTURE_FENCE = "stale_capture_fence"
FAIL_IMMUTABLE_OBJECT_CONFLICT = "immutable_object_conflict"
FAIL_SEGMENT_IDENTITY_CONFLICT = "segment_identity_conflict"
FAIL_COMPLETION_CONFLICT = "completion_conflict"
FAIL_MANIFEST_INTEGRITY = "manifest_integrity_failed"
FAIL_PROVIDER_MALFORMED = "provider_output_malformed"
FAIL_PROVIDER_EMPTY = "provider_output_empty"
FAIL_TRANSCRIPT_QUALITY = "transcript_quality_insufficient"
FAIL_DELETION_IN_PROGRESS = "deletion_in_progress"

# Statuses POST /meetings/{id}/retry may re-drive. A retry NEVER touches ready,
# excluded, an actively-leased synthesizing run, or a non-retryable failure.
RETRYABLE_STATUSES = (STATUS_UPLOADED, STATUS_FAILED)

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
# MAX_SEGMENTS_PER_MEETING = 100 (long-meeting target; not active in V2).
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

# How long a meeting may sit in a non-terminal state before reconciliation calls
# it stalled and stamps FAIL_PROCESSING_TIMEOUT. Deliberately far beyond the
# worst legitimate path (a 30 minute lease, three job attempts, up to an hour of
# backoff between them) so this can only ever fire on genuinely stuck work. The
# rule it enforces: zero rows and healthy must never look identical, and a
# spinner that renders forever is exactly that failure.
STALL_DEADLINE_MINUTES = 6 * 60

# --- V2 immutable evidence collections ---------------------------------------
CAPTURE_RUNS_SUBCOLLECTION = "capture_runs"
SEGMENTS_SUBCOLLECTION = "segments"
AUDIT_SUBCOLLECTION = "audit_events"
JOBS_SUBCOLLECTION = "meeting_jobs"
DELETIONS_SUBCOLLECTION = "meeting_deletions"
JOB_OUTBOX_SUBCOLLECTION = "meeting_job_outbox"

CAPTURE_RUN_STATE = "state"
CAPTURE_RUN_CAPTURING = "capturing"
CAPTURE_RUN_FINALIZED = "finalized"
CAPTURE_RUN_UPLOADED = "uploaded_verified"
CAPTURE_RUN_SPLIT_BRAIN = "split_brain"
CAPTURE_RUN_DELETED = "deleted"

JOB_PENDING = "pending"
JOB_DISPATCHED = "dispatched"
JOB_LEASED = "leased"
JOB_RETRY = "retry"
JOB_COMPLETE = "complete"
JOB_FAILED = "failed"
JOB_BLOCKED = "blocked"

MEETING_SCHEMA_VERSION = 2
MANIFEST_SCHEMA_VERSION = 2
TRANSCRIPT_SCHEMA_VERSION = "meeting-transcript-v2"
QUALITY_POLICY_V1 = "meeting-quality-v1"
AUDIT_SCHEMA_VERSION = "meeting-audit-v2"
SOFTWARE_COMPONENT = "juno-backend"

# A capture claim currently has no desktop heartbeat route. The lease therefore
# spans the supported capture clamp plus the existing rejoin grace; ownership
# recovery still advances CAPTURE_FENCE transactionally.
CAPTURE_LEASE_MINUTES = MAX_CAPTURE_MINUTES + CLAIM_GRACE_MINUTES

# Completion checks decoded FLAC duration against the signed manifest using the
# architecture's max(2 seconds, 1 percent) tolerance.
DURATION_TOLERANCE_FLOOR_MS = 2_000

# Machine codes the desktop client matches on (mirrors the /voice/token cap
# contract shape: 402 + {"detail": {"code": ..., "seconds_until_reset": ...}}).
MEETING_CAP_CODE = "meeting_cap_reached"
MEETING_CONFLICT_CODE = "meeting_already_claimed"
