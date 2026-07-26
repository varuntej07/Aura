"""Safe structured operational telemetry shared by provider integrations.

Operational events intentionally contain no prompts, transcripts, search text,
email addresses, user ids, or credentials. Cloud Run and the LiveKit log drain
ship these JSON records to Cloud Logging, where Aura Ops aggregates them.
"""

from __future__ import annotations

import re
from typing import Any

from ..lib.logger import logger

_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "email",
    "prompt",
    "query",
    "secret",
    "token",
    "transcript",
    "user_id",
    "uid",
)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


def redact_operational_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Drop sensitive keys and sanitize short scalar values.

    Operational metadata is deliberately shallow. Nested structures are omitted
    rather than recursively copied because provider responses can contain user
    content under unexpected keys.
    """
    safe: dict[str, Any] = {}
    for raw_key, raw_value in fields.items():
        key = str(raw_key)
        lowered = key.lower()
        if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
            continue
        if raw_value is None or isinstance(raw_value, (bool, int, float)):
            safe[key] = raw_value
            continue
        if isinstance(raw_value, str):
            value = _EMAIL_RE.sub("<redacted-email>", raw_value)
            value = _BEARER_RE.sub("Bearer <redacted>", value)
            safe[key] = value[:240]
    return safe


def log_provider_request(
    *,
    provider: str,
    operation: str,
    feature: str,
    outcome: str,
    billable: bool,
    latency_ms: int | None = None,
    status_code: int | None = None,
    result_count: int | None = None,
    cache_hit: bool = False,
) -> None:
    """Emit one countable event for one provider attempt.

    A cache hit is emitted for reliability analysis but is never billable.
    """
    payload: dict[str, Any] = {
        "provider": provider,
        "operation": operation,
        "feature": feature,
        "outcome": outcome,
        "billable": bool(billable and not cache_hit),
        "cache_hit": cache_hit,
        "latency_ms": latency_ms,
        "status_code": status_code,
        "result_count": result_count,
    }
    payload = redact_operational_fields(payload)
    if outcome in {"rate_limited", "provider_error"}:
        logger.error("provider_request", payload)
    elif outcome in {"timeout", "network_error"}:
        logger.warn("provider_request", payload)
    else:
        logger.info("provider_request", payload)
