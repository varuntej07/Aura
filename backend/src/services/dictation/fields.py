"""Stable Firestore fields, limits, and wire constants for dictation traces."""

from __future__ import annotations

import re

PARENT_COLLECTION = "users"
TRACE_SUBCOLLECTION = "dictation_traces"
USAGE_SUBCOLLECTION = "usage"

TRACE_SCHEMA_VERSION = 1
CONSENT_VERSION = 1
MONTHLY_TRACE_CAP = 500
AUDIO_RETENTION_DAYS = 180

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
DELETION_STATE = "deletion_state"
DELETION_REQUESTED_AT = "deletion_requested_at"
DELETED_AT = "deleted_at"
QUOTA_MONTH = "quota_month"

DELETION_PENDING = "pending"
DELETION_COMPLETE = "complete"

EDIT_CLASSES = ("verbatim", "casing", "punctuation", "disfluency", "style")
GROUND_TRUTH_EDIT_CLASSES = frozenset(("verbatim", "casing", "punctuation"))
STYLE_EDIT_CLASSES = frozenset(("disfluency", "style"))


def validate_trace_id(trace_id: str) -> bool:
    return bool(TRACE_ID_PATTERN.fullmatch(trace_id))


def validate_sha256(value: str) -> bool:
    return bool(SHA256_PATTERN.fullmatch(value))

