"""In-process provider-health circuits for latency-critical fallback loops.

Owns the failure classifier (provider-SDK exception knowledge that used to
live inside handlers/interview_companion.py) and the short-lived circuit
state that lets a fallback loop skip a model that just failed.

Scope is PER-PROCESS and PER-INSTANCE by design, and callers must treat it
that way: on a multi-instance Cloud Run service each worker keeps its own
view, entries are evicted lazily on read, and one user's 401/429 opens the
circuit for every user on that instance for the window below. That is an
intentional local heuristic - a cheap "don't immediately retry a provider
that just told us no" - not shared state; putting Firestore on this hot path
would cost more latency than the circuit saves. Every open/skip is logged by
the caller, so the behaviour is observable per instance.
"""

from __future__ import annotations

import time

import anthropic
import openai

from .model_provider import is_quota_exhausted

# model_id -> (monotonic deadline, reason)
_CIRCUITS: dict[str, tuple[float, str]] = {}
_SLOW_STARTS: dict[str, int] = {}

_SLOW_START_TRIP_COUNT = 2
_SHORT_OPEN_S = 30.0
_LONG_OPEN_S = 600.0


def open_reason(model_id: str) -> str | None:
    """The reason this model's circuit is open, or None when it is healthy.
    Expired entries are evicted here (lazy, on read)."""
    circuit = _CIRCUITS.get(model_id)
    if circuit is None:
        return None
    open_until, reason = circuit
    if time.monotonic() < open_until:
        return reason
    _CIRCUITS.pop(model_id, None)
    return None


def record_success(model_id: str) -> None:
    _SLOW_STARTS.pop(model_id, None)
    _CIRCUITS.pop(model_id, None)


def _open(model_id: str, seconds: float, reason: str) -> None:
    _CIRCUITS[model_id] = (time.monotonic() + seconds, reason)


def record_failure(model_id: str, exc: Exception, *, slow_start: bool = False) -> str:
    """Classify one provider failure, open the circuit accordingly, and
    return the coded reason for the caller's log line.

    ``slow_start`` marks a first-token timeout (a caller-defined condition);
    two in a row open a short circuit. Quota/credit exhaustion and access
    errors (401/403/404) open a long one - they will not clear in seconds.
    """
    if slow_start:
        slow_starts = _SLOW_STARTS.get(model_id, 0) + 1
        _SLOW_STARTS[model_id] = slow_starts
        if slow_starts >= _SLOW_START_TRIP_COUNT:
            _open(model_id, _SHORT_OPEN_S, "slow_first_token")
        return "slow_first_token"
    if is_quota_exhausted(exc):
        _open(model_id, _LONG_OPEN_S, "credits_or_quota")
        return "credits_or_quota"
    status_code = getattr(exc, "status_code", None)
    if status_code in (401, 403, 404):
        _open(model_id, _LONG_OPEN_S, "provider_access")
        return "provider_access"
    if status_code == 429:
        _open(model_id, _SHORT_OPEN_S, "rate_limited")
        return "rate_limited"
    if isinstance(status_code, int) and status_code >= 500:
        _open(model_id, _SHORT_OPEN_S, "provider_outage")
        return "provider_outage"
    if isinstance(
        exc,
        (anthropic.APIConnectionError, openai.APIConnectionError, ConnectionError, OSError),
    ):
        _open(model_id, _SHORT_OPEN_S, "connection_failure")
        return "connection_failure"
    return "request_failure"
