"""Metadata-only Sentry integration for Meeting Recording V2.

Thin wrapper over services/sentry.py that pins subsystem="meeting_recording_v2"
and the meeting-specific tag set. Signatures are unchanged so meetings callers
are untouched.
"""

from __future__ import annotations

from typing import Any

from .. import sentry


def configure_sentry() -> None:
    sentry.configure_sentry()


def capture_error(
    exc: Exception,
    *,
    error_code: str,
    correlation_id: str = "",
    meeting_id: str = "",
    capture_run_id: str = "",
    capture_fence: int | None = None,
    job_id: str = "",
) -> None:
    tags: dict[str, Any] = {
        "correlation_id": correlation_id,
        "meeting_id": meeting_id,
        "capture_run_id": capture_run_id,
        "job_id": job_id,
    }
    if capture_fence is not None:
        tags["capture_fence"] = capture_fence
    sentry.capture_error(
        exc,
        subsystem="meeting_recording_v2",
        error_code=error_code,
        tags=tags,
    )
