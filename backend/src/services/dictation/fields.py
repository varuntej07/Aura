"""Stable Firestore fields, limits, and wire constants for dictation traces."""

from __future__ import annotations

import re

PARENT_COLLECTION = "users"
TRACE_SUBCOLLECTION = "dictation_traces"
USAGE_SUBCOLLECTION = "usage"

TRACE_SCHEMA_VERSION = 2
CONSENT_VERSION = 2
MONTHLY_TRACE_CAP = 500
AUDIO_RETENTION_DAYS = 180
METADATA_RETENTION_DAYS = 180

MAX_METADATA_BYTES = 256 * 1024
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_DURATION_MS = 120_000
MAX_TEXT_CHARS = 32_000
MAX_EDITS = 1_024
RECONCILE_BATCH_LIMIT = 500

TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{24}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

TRACE_ID = "trace_id"
METADATA_SHA256 = "metadata_sha256"
UPLOADED_AT = "uploaded_at"
HAS_AUDIO = "has_audio"
AUDIO_PATH = "audio_path"
AUDIO_GENERATION = "audio_generation"
AUDIO_UPLOADED_AT = "audio_uploaded_at"
AUDIO_EXPIRES_AT = "audio_expires_at"
METADATA_EXPIRES_AT = "expires_at"
AUDIO_BYTES = "audioBytes"
# camelCase like AUDIO_BYTES: written by the client's trace metadata payload,
# read server-side for the audio identity checks.
AUDIO_SHA256 = "audioSha256"
DURATION_MS = "durationMs"
AUDIO_MISSING_CONFIRMED_AT = "audio_missing_confirmed_at"
DELETION_STATE = "deletion_state"
DELETION_REQUESTED_AT = "deletion_requested_at"
DELETED_AT = "deleted_at"
QUOTA_MONTH = "quota_month"

DELETION_PENDING = "pending"
DELETION_COMPLETE = "complete"

# The only label qualities a client may assert. "human_gold" is deliberately
# absent: it means a reviewer listened to the audio and confirmed the
# transcript, which no device can attest to about its own data. Only the
# reviewer path, server-side, may write it. "unobserved" and "rejected" are
# absent because a trace carrying either is never uploaded at all.
CLIENT_LABEL_QUALITIES = ("unchanged_silver", "corrected_silver")

EDIT_CLASSES = ("verbatim", "casing", "punctuation", "disfluency", "style")
GROUND_TRUTH_EDIT_CLASSES = frozenset(("verbatim", "casing", "punctuation"))
STYLE_EDIT_CLASSES = frozenset(("disfluency", "style"))


def validate_trace_id(trace_id: str) -> bool:
    return bool(TRACE_ID_PATTERN.fullmatch(trace_id))


def validate_sha256(value: str) -> bool:
    return bool(SHA256_PATTERN.fullmatch(value))

