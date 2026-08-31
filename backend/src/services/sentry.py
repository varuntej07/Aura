"""Metadata-only Sentry integration shared by backend subsystems.

Owns ``sentry_sdk.init`` (called once at startup from main.py) and a
subsystem-parameterized ``capture_error``. Subsystem wrappers (for example
services/meetings/observability.py) supply their own tags; PII is never sent
and tracing stays off.
"""

from __future__ import annotations

from typing import Any

from ..config.settings import settings


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
    subsystem: str,
    error_code: str,
    tags: dict[str, Any] | None = None,
) -> None:
    """Capture one exception with metadata-only tags. Empty-string tags are dropped."""
    if not settings.SENTRY_DSN:
        return
    import sentry_sdk

    with sentry_sdk.push_scope() as scope:
        all_tags: dict[str, Any] = {
            "subsystem": subsystem,
            "error_code": error_code,
            **(tags or {}),
        }
        for key, value in all_tags.items():
            if value != "":
                scope.set_tag(key, value)
        sentry_sdk.capture_exception(exc)
