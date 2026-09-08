"""Searchable operational logs and structured usage aggregates."""
from __future__ import annotations

import functools
import json
import logging
import math

logger = logging.getLogger("ops.logging")

_BACKEND_SERVICE = "juno-backend"


@functools.lru_cache(maxsize=4)
def _client(project_id: str):
    """One Cloud Logging client per project, built once.

    Five functions in this module each constructed their own client per call, so
    a single cold dashboard load paid several gRPC channel setups and credential
    acquisitions before any query ran. The client is thread-safe and the process
    is a short-lived Cloud Run revision, so holding it for the process lifetime
    is safe; the trade is that rotated ADC credentials need a restart to be
    picked up, which a new revision already is.
    """
    from google.cloud import logging as cloud_logging

    return cloud_logging.Client(project=project_id)


# Sources the dashboard can read after the LiveKit Google Cloud log drain is set.
KNOWN_SERVICES = ("juno-backend", "juno-ops", "livekit-worker")

_SEVERITIES = ("DEFAULT", "DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY")


def _service_clause(services: list[str] | tuple[str, ...]) -> str:
    clauses = [
        f'resource.labels.service_name="{s}"'
        for s in services
        if s and s != "livekit-worker"
    ]
    if "livekit-worker" in services:
        clauses.extend([
            'jsonPayload.agent_id:*',
            'jsonPayload.message:"VoiceSession:"',
            'textPayload:"VoiceSession:"',
        ])
    return f"({' OR '.join(clauses)})" if clauses else 'resource.labels.service_name="juno-backend"'


def _payload_dict(entry) -> dict:
    payload = entry.payload
    if isinstance(payload, dict):
        nested = payload.get("message")
        if isinstance(nested, str) and nested.lstrip().startswith("{"):
            try:
                decoded = json.loads(nested)
                if isinstance(decoded, dict):
                    return {**payload, **decoded}
            except json.JSONDecodeError:
                pass
        return payload
    if isinstance(payload, str) and payload.lstrip().startswith("{"):
        try:
            decoded = json.loads(payload)
            return decoded if isinstance(decoded, dict) else {"message": payload}
        except json.JSONDecodeError:
            pass
    return {"message": str(payload)}


def _service_name(entry, payload: dict) -> str:
    try:
        service = entry.resource.labels.get("service_name", "")
        if service:
            return service
    except Exception:
        pass
    if payload.get("agent_id") or str(payload.get("message", "")).startswith("VoiceSession:"):
        return "livekit-worker"
    return str(payload.get("service") or "")


def recent_errors(
    project_id: str,
    services: list[str] | tuple[str, ...] = KNOWN_SERVICES,
    min_severity: str = "ERROR",
    limit: int = 50,
) -> list[dict]:
    """Newest min_severity+ log lines across the Cloud Run services. Each row
    carries its service name. Empty list on error (logged)."""
    try:
        from google.cloud import logging as cloud_logging
    except ImportError:
        logger.error("google-cloud-logging not installed; error panel disabled")
        return []

    severity = min_severity.upper() if min_severity.upper() in _SEVERITIES else "ERROR"
    try:
        client = _client(project_id)
        log_filter = f"{_service_clause(services)} AND severity>={severity}"
        entries = client.list_entries(
            filter_=log_filter,
            order_by=cloud_logging.DESCENDING,
            max_results=limit,
        )
        out: list[dict] = []
        for entry in entries:
            payload = _payload_dict(entry)
            message = payload.get("message") or payload.get("msg") or str(payload)
            service = _service_name(entry, payload)
            out.append({
                "at": entry.timestamp.isoformat() if entry.timestamp else "",
                "severity": str(entry.severity),
                "service": service,
                "message": str(message)[:300],
            })
        return out
    except Exception as exc:
        logger.error("recent_errors query failed: %s", exc)
        return []


def search_logs(
    project_id: str,
    services: list[str] | tuple[str, ...] = KNOWN_SERVICES,
    min_severity: str = "DEFAULT",
    text: str = "",
    hours: int = 24,
    limit: int = 100,
) -> list[dict]:
    """The log viewer: text search + severity + time range + service filter over
    Cloud Run logs. A bare quoted term in a Cloud Logging filter is a global
    restriction (searches every field), which is exactly the grep-like behavior
    wanted here. Empty list on error (logged)."""
    try:
        from google.cloud import logging as cloud_logging
    except ImportError:
        logger.error("google-cloud-logging not installed; log viewer disabled")
        return []

    from datetime import datetime, timedelta, timezone

    severity = min_severity.upper() if min_severity.upper() in _SEVERITIES else "DEFAULT"
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, min(int(hours), 24 * 30)))
    parts = [
        _service_clause(services),
        f'timestamp>="{since.isoformat()}"',
    ]
    if severity != "DEFAULT":
        parts.append(f"severity>={severity}")
    term = text.strip().replace('"', '\\"')
    if term:
        parts.append(f'"{term}"')

    try:
        client = _client(project_id)
        entries = client.list_entries(
            filter_=" AND ".join(parts),
            order_by=cloud_logging.DESCENDING,
            max_results=max(1, min(int(limit), 300)),
        )
        out: list[dict] = []
        for entry in entries:
            payload = _payload_dict(entry)
            message = payload.get("message") or payload.get("msg") or str(payload)
            service = _service_name(entry, payload)
            out.append({
                "at": entry.timestamp.isoformat() if entry.timestamp else "",
                "severity": str(entry.severity),
                "service": service,
                "message": str(message)[:500],
            })
        return out
    except Exception as exc:
        logger.error("search_logs query failed: %s", exc)
        return []


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return round(ordered[rank], 1)


def voice_latency_stats(project_id: str, days: int = 7, limit: int = 3000) -> dict:
    """Aggregate redacted worker latency records delivered by the LiveKit log drain."""
    # These records are emitted by the LiveKit voice worker, which runs on LiveKit
    # Cloud rather than in this GCP project. They only reach Cloud Logging after a
    # log drain is configured in LiveKit Cloud pointing here (see ops/README.md).
    # Until then this is legitimately empty, and that is a SETUP gap rather than a
    # slow or broken worker: the note travels with the payload so the UI can say so.
    empty = {
        "count": 0,
        "worker_first_talk": {"p50": None, "p95": None, "p99": None},
        "token_to_first_talk": {"p50": None, "p95": None, "p99": None},
        "reply_to_first_talk": {"p50": None, "p95": None, "p99": None},
        "note": (
            "Needs the LiveKit Cloud log drain into this GCP project, plus "
            "roles/logging.viewer for the ops service account."
        ),
    }
    try:
        from google.cloud import logging as cloud_logging
        from datetime import datetime, timedelta, timezone

        since = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 30)))
        client = _client(project_id)
        entries = client.list_entries(
            # The bare quoted terms below are a GLOBAL RESTRICTION, which makes
            # Cloud Logging search every field of every log in the project for
            # the window. Anchoring on resource.type first lets the index cut the
            # candidate set before the text match, so cost tracks worker volume
            # rather than total project log volume.
            filter_=(
                'resource.type="cloud_run_revision" AND '
                f'timestamp>="{since.isoformat()}" AND '
                '("VoiceSession: first talk metrics" OR "VoiceSession: turn metrics")'
            ),
            order_by=cloud_logging.DESCENDING,
            max_results=limit,
        )
        worker: list[float] = []
        startup: list[float] = []
        reply: list[float] = []
        for entry in entries:
            payload = _payload_dict(entry)
            for field, target in (
                ("worker_first_talk_ms", worker),
                ("token_minted_to_first_talk_ms", startup),
                ("eou_to_first_audio_ms", reply),
            ):
                value = payload.get(field)
                if isinstance(value, (int, float)) and 0 <= value <= 120_000:
                    target.append(float(value))
        def stats(values: list[float]) -> dict:
            return {
                "p50": _percentile(values, 0.50),
                "p95": _percentile(values, 0.95),
                "p99": _percentile(values, 0.99),
            }
        found = max(len(worker), len(startup), len(reply))
        return {
            **empty,
            "count": found,
            "worker_first_talk": stats(worker),
            "token_to_first_talk": stats(startup),
            "reply_to_first_talk": stats(reply),
            "note": "" if found else empty["note"],
        }
    except Exception as exc:
        logger.error("voice_latency_stats query failed: %s", exc)
        return empty


def provider_usage_stats(project_id: str, days: int = 7, limit: int = 5000) -> dict:
    """Aggregate `provider_request` records, currently led by Brave usage."""
    try:
        from google.cloud import logging as cloud_logging
        from datetime import datetime, timedelta, timezone

        since = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 30)))
        client = _client(project_id)
        entries = client.list_entries(
            # Same reasoning as voice_latency_stats: anchor on the emitting
            # service before the text term. Only the backend calls
            # observability.log_provider_request, so constraining to it cannot
            # lose a record, and it stops a 30-day range from scanning the whole
            # project's logs to find a few thousand Brave calls.
            filter_=(
                'resource.type="cloud_run_revision" AND '
                f'resource.labels.service_name="{_BACKEND_SERVICE}" AND '
                f'timestamp>="{since.isoformat()}" AND "provider_request"'
            ),
            order_by=cloud_logging.DESCENDING,
            max_results=limit,
        )
        rows: dict[tuple[str, str], dict] = {}
        for entry in entries:
            payload = _payload_dict(entry)
            provider = str(payload.get("provider") or "unknown")
            feature = str(payload.get("feature") or "unknown")
            row = rows.setdefault((provider, feature), {
                "provider": provider,
                "feature": feature,
                "attempts": 0,
                "billable": 0,
                "success": 0,
                "failures": 0,
                "rate_limited": 0,
                "cache_hits": 0,
            })
            row["attempts"] += 1
            if payload.get("billable") is True:
                row["billable"] += 1
            outcome = str(payload.get("outcome") or "")
            if outcome == "success":
                row["success"] += 1
            elif outcome == "rate_limited":
                row["rate_limited"] += 1
                row["failures"] += 1
            elif outcome == "cache_hit":
                row["cache_hits"] += 1
            else:
                row["failures"] += 1
        return {"configured": True, "days": days, "rows": list(rows.values())}
    except Exception as exc:
        logger.error("provider_usage_stats query failed: %s", exc)
        return {"configured": False, "days": days, "rows": []}


def _entry_message(entry) -> str:
    payload = entry.payload
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("msg") or payload)
    return str(payload)


def recent_recommender_health(
    project_id: str,
    service_name: str = _BACKEND_SERVICE,
    limit: int = 6,
) -> list[dict]:
    """The recommender's own self-report of each 15-min tick: did it send anything,
    and if not, why. This is the "why is it silent" half of the trace.

    The scoring loop already logs one self-explanatory health line per tick
    ("tick health: sent=X/Y considered | blocked: below_threshold=... no_candidates=...").
    We just surface the newest few here instead of writing anything new, so a quiet
    notification system shows its reason at a glance (starved pool vs weak matches vs
    nobody bootstrapped) rather than looking identical to "all healthy, nothing to
    send". Best-effort: an empty list (logged) never blanks the dashboard.
    """
    try:
        from google.cloud import logging as cloud_logging
    except ImportError:
        logger.error("google-cloud-logging not installed; recommender health disabled")
        return []

    try:
        client = _client(project_id)
        log_filter = (
            'resource.type="cloud_run_revision" '
            f'AND resource.labels.service_name="{service_name}" '
            'AND ("tick health" OR jsonPayload.message:"tick health")'
        )
        entries = client.list_entries(
            filter_=log_filter,
            order_by=cloud_logging.DESCENDING,
            max_results=limit,
        )
        out: list[dict] = []
        for entry in entries:
            message = _entry_message(entry)
            # Keep only the readable tail after our log prefix so the panel shows
            # "sent=1/14 considered | blocked: ..." not the module path noise.
            if "tick health:" in message:
                message = message.split("tick health:", 1)[1].strip()
            out.append({
                "at": entry.timestamp.isoformat() if entry.timestamp else "",
                "message": str(message)[:300],
            })
        return out
    except Exception as exc:
        logger.error("recent_recommender_health query failed: %s", exc)
        return []
