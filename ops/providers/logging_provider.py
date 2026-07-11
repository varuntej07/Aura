"""Recent ERROR-level Cloud Logging entries for the juno-backend Cloud Run service."""
from __future__ import annotations

import logging

logger = logging.getLogger("ops.logging")

_BACKEND_SERVICE = "juno-backend"


# Cloud Run services this dashboard can read logs for. The voice worker is NOT
# here: it runs on LiveKit Cloud Agents, whose logs live in LiveKit's own
# dashboard, not GCP Cloud Logging (a real gap the Logs tab states honestly).
KNOWN_SERVICES = ("juno-backend", "juno-ops")

_SEVERITIES = ("DEFAULT", "DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY")


def _service_clause(services: list[str] | tuple[str, ...]) -> str:
    quoted = " OR ".join(f'resource.labels.service_name="{s}"' for s in services if s)
    return f"({quoted})" if quoted else 'resource.labels.service_name="juno-backend"'


def recent_errors(
    project_id: str,
    services: list[str] | tuple[str, ...] = (_BACKEND_SERVICE, "juno-ops"),
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
        client = cloud_logging.Client(project=project_id)
        log_filter = (
            'resource.type="cloud_run_revision" '
            f"AND {_service_clause(services)} "
            f"AND severity>={severity}"
        )
        entries = client.list_entries(
            filter_=log_filter,
            order_by=cloud_logging.DESCENDING,
            max_results=limit,
        )
        out: list[dict] = []
        for entry in entries:
            payload = entry.payload
            if isinstance(payload, dict):
                message = payload.get("message") or payload.get("msg") or str(payload)
            else:
                message = str(payload)
            service = ""
            try:
                service = entry.resource.labels.get("service_name", "")
            except Exception:
                pass
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
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, min(int(hours), 24 * 14)))
    parts = [
        'resource.type="cloud_run_revision"',
        _service_clause(services),
        f'timestamp>="{since.isoformat()}"',
    ]
    if severity != "DEFAULT":
        parts.append(f"severity>={severity}")
    term = text.strip().replace('"', '\\"')
    if term:
        parts.append(f'"{term}"')

    try:
        client = cloud_logging.Client(project=project_id)
        entries = client.list_entries(
            filter_=" AND ".join(parts),
            order_by=cloud_logging.DESCENDING,
            max_results=max(1, min(int(limit), 300)),
        )
        out: list[dict] = []
        for entry in entries:
            payload = entry.payload
            if isinstance(payload, dict):
                message = payload.get("message") or payload.get("msg") or str(payload)
            else:
                message = str(payload)
            service = ""
            try:
                service = entry.resource.labels.get("service_name", "")
            except Exception:
                pass
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
        client = cloud_logging.Client(project=project_id)
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
