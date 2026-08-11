"""Metadata-only Sentry integration for Meeting Recording V2."""

from __future__ import annotations

from typing import Any

from ...config.settings import settings


def configure_sentry() -> None:
    if not settings.SENTRY_DSN:
        return
    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        send_default_pii=False,
        traces_sample_rate=0.0,
    )


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
    if not settings.SENTRY_DSN:
        return
    import sentry_sdk

    with sentry_sdk.push_scope() as scope:
        tags: dict[str, Any] = {
            "subsystem": "meeting_recording_v2",
            "error_code": error_code,
            "correlation_id": correlation_id,
            "meeting_id": meeting_id,
            "capture_run_id": capture_run_id,
            "job_id": job_id,
        }
        if capture_fence is not None:
            tags["capture_fence"] = capture_fence
        for key, value in tags.items():
            if value != "":
                scope.set_tag(key, value)
        sentry_sdk.capture_exception(exc)
