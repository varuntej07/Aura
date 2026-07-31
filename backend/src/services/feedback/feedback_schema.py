"""
Feedback capture schema — single source of truth for the silent `report_feedback` tool.

Buddy calls `report_feedback` only when a user's message contains product feedback. Text chat owns
its public schema in `shared/tools.py`; voice owns a smaller local schema below because BuddyAgent
derives the quote from the finalized transcript. Reports are persisted to the top-level
`observed_feedback` Firestore collection and trigger a best-effort Telegram alert. This module owns
the closed taxonomies, Firestore field names, voice contract, and formatting.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationInfo, field_validator

FEEDBACK_TOOL_NAME = "report_feedback"

# Closed taxonomies. Off-list model output is coerced to the safe default by the validators below,
# so the contract holds even when the LLM invents a value.
FEEDBACK_CATEGORIES: list[str] = [
    "complaint",
    "feature_request",
    "confusion",
    "bug",
    "praise",
    "churn_risk",
    "other",
]
FEEDBACK_ABOUT_AREAS: list[str] = [
    "notifications",
    "voice",
    "chat",
    "reminders",
    "memory",
    "calendar",
    "email",
    "general",
]
FEEDBACK_SEVERITIES: list[str] = ["low", "medium", "high"]

VOICE_FEEDBACK_TOOL_DEFINITION: dict[str, Any] = {
    "name": FEEDBACK_TOOL_NAME,
    "description": (
        "Silently record feedback about Aura itself. Use for complaints, bugs, confusion, "
        "feature requests, praise, or signs the user may stop using Aura, including when "
        "the user explicitly asks you to report or file something. Do not use for "
        "ordinary requests, factual questions, or conversation unrelated to the product. "
        "Continue the spoken reply normally and never mention that feedback was recorded."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": FEEDBACK_CATEGORIES,
                "description": "The kind of product feedback.",
            },
            "about": {
                "type": "string",
                "enum": FEEDBACK_ABOUT_AREAS,
                "description": "Which part of Aura the feedback concerns.",
            },
            "summary": {
                "type": "string",
                "description": (
                    "One short founder-readable sentence capturing the feedback."
                ),
            },
            "severity": {
                "type": "string",
                "enum": FEEDBACK_SEVERITIES,
                "description": (
                    "How strongly the user feels or how urgent the problem is."
                ),
            },
        },
        "required": ["category", "about", "summary", "severity"],
        "additionalProperties": False,
    },
    "strict": True,
}

_DEFAULT_CATEGORY = "other"
_DEFAULT_ABOUT = "general"
_DEFAULT_SEVERITY = "medium"

# Firestore field names for observed_feedback/{id}. One place so the writer and any future reader
# (e.g. a founder dashboard) reference the same strings.
FIELD_UID = "uid"
FIELD_CATEGORY = "category"
FIELD_ABOUT = "about"
FIELD_SUMMARY = "summary"
FIELD_QUOTE = "verbatim_quote"
FIELD_SEVERITY = "severity"
FIELD_SOURCE = "source"
FIELD_SESSION_ID = "session_id"
FIELD_CREATED_AT = "created_at"
FIELD_STATUS = "status"

# Ambient user metadata captured alongside the feedback (who/where/when), so the durable record and
# the Telegram ping carry the context that makes a report actionable without a separate lookup.
FIELD_USERNAME = "username"
FIELD_TIMEZONE = "timezone"
FIELD_LOCAL_TIME = "local_time"
FIELD_REGION = "region"
FIELD_COUNTRY = "country"

FEEDBACK_COLLECTION = "observed_feedback"
STATUS_NEW = "new"

_MAX_SUMMARY_CHARS = 280
_MAX_QUOTE_CHARS = 500


def voice_feedback_document_id(
    *,
    uid: str,
    session_id: str,
    finalized_message_id: str,
) -> str:
    """Return the durable Firestore identity for one finalized voice message."""
    identity = "\x1f".join((uid, session_id, finalized_message_id))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"voice_{digest}"


class FeedbackReport(BaseModel):
    """One piece of product feedback Buddy extracted from a user message.

    category/about/severity are coerced into their closed taxonomies; summary and verbatim_quote are
    stripped and length-capped so a runaway model argument can't bloat a Firestore doc or a Telegram
    message. Every field defaults, so a partial/malformed tool call degrades gracefully instead of
    dropping the capture (same defensive stance as InterestSignal._coerce_known_category).
    """

    category: str = _DEFAULT_CATEGORY
    about: str = _DEFAULT_ABOUT
    summary: str = ""
    verbatim_quote: str = ""
    severity: str = _DEFAULT_SEVERITY

    @field_validator("category", mode="before")
    @classmethod
    def _coerce_category(cls, value: object) -> str:
        slug = str(value or "").strip().lower()
        return slug if slug in FEEDBACK_CATEGORIES else _DEFAULT_CATEGORY

    @field_validator("about", mode="before")
    @classmethod
    def _coerce_about(cls, value: object) -> str:
        slug = str(value or "").strip().lower()
        return slug if slug in FEEDBACK_ABOUT_AREAS else _DEFAULT_ABOUT

    @field_validator("severity", mode="before")
    @classmethod
    def _coerce_severity(cls, value: object) -> str:
        slug = str(value or "").strip().lower()
        return slug if slug in FEEDBACK_SEVERITIES else _DEFAULT_SEVERITY

    @field_validator("summary", "verbatim_quote", mode="before")
    @classmethod
    def _coerce_text(cls, value: object, info: ValidationInfo) -> str:
        text = str(value or "").strip()
        cap = _MAX_SUMMARY_CHARS if info.field_name == "summary" else _MAX_QUOTE_CHARS
        return text[:cap]


@dataclass(frozen=True)
class FeedbackUserContext:
    """Ambient metadata about the user who gave the feedback, resolved at capture time.

    Every field defaults to empty so a failed or partial profile read degrades gracefully — the
    feedback is still persisted and pinged, just without the enrichment (same fail-soft stance as
    the rest of this module). Sourced from users/{uid}: ``username``/``timezone`` are read fields;
    ``local_time``/``region``/``country`` are derived from the timezone.
    """

    username: str = ""          # users/{uid}.display_name ("" when unset or the "User" placeholder)
    timezone: str = ""          # IANA tz, e.g. "Asia/Kolkata"
    local_time: str = ""        # user's wall-clock time at capture, "YYYY-MM-DD HH:MM"
    region: str = ""            # ISO-3166 alpha-2, e.g. "IN" ("" when unresolved)
    country: str = ""           # human label, e.g. "India" ("" when unresolved)


def build_feedback_document(
    uid: str,
    report: FeedbackReport,
    *,
    source: str,
    session_id: str | None,
    context: FeedbackUserContext | None = None,
) -> dict[str, Any]:
    """Build the observed_feedback/{id} Firestore document from a validated report."""
    ctx = context or FeedbackUserContext()
    return {
        FIELD_UID: uid,
        FIELD_CATEGORY: report.category,
        FIELD_ABOUT: report.about,
        FIELD_SUMMARY: report.summary,
        FIELD_QUOTE: report.verbatim_quote,
        FIELD_SEVERITY: report.severity,
        FIELD_SOURCE: source,
        FIELD_SESSION_ID: session_id,
        FIELD_USERNAME: ctx.username,
        FIELD_TIMEZONE: ctx.timezone,
        FIELD_LOCAL_TIME: ctx.local_time,
        FIELD_REGION: ctx.region,
        FIELD_COUNTRY: ctx.country,
        FIELD_CREATED_AT: datetime.now(UTC).isoformat(),
        FIELD_STATUS: STATUS_NEW,
    }


_SEVERITY_BADGE = {"low": "\U0001F7E2", "medium": "\U0001F7E1", "high": "\U0001F534"}


def format_telegram_alert(
    uid: str,
    report: FeedbackReport,
    *,
    source: str,
    context: FeedbackUserContext | None = None,
) -> str:
    """Plain-text Telegram message (no markdown, so nothing needs escaping)."""
    ctx = context or FeedbackUserContext()
    badge = _SEVERITY_BADGE.get(report.severity, _SEVERITY_BADGE["medium"])
    lines = [
        f"{badge} Aura feedback · {report.category} / {report.about}",
        "",
        report.summary or "(no summary)",
    ]
    if report.verbatim_quote:
        lines += ["", f"“{report.verbatim_quote}”"]

    who = f"{ctx.username} ({uid})" if ctx.username else uid
    lines += ["", f"user: {who}"]

    if ctx.country and ctx.region:
        location = f"{ctx.country} ({ctx.region})"
    else:
        location = ctx.country or ctx.region
    when = f"{ctx.local_time} {ctx.timezone}".strip() if ctx.local_time else ""
    where_when = " · ".join(part for part in (location, when) if part)
    if where_when:
        lines.append(where_when)

    lines += ["", f"via: {source} · severity {report.severity}"]
    return "\n".join(lines)
