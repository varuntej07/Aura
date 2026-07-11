"""Mobile crash feed from the Firebase Crashlytics -> BigQuery export.

Crashlytics has NO public REST API for listing crashes; the only supported
programmatic access is the BigQuery export (Firebase console -> Integrations ->
BigQuery). Once enabled, Firebase writes one date-partitioned events table per
app into the `firebase_crashlytics` dataset (table names derive from the bundle
id, e.g. com_example_app_ANDROID / _IOS, plus _REALTIME variants).

Read discipline (this is billed per byte scanned): every query filters the
event_timestamp partition column and LIMITs; one query per app table per
dashboard load, and the crash tab is lazy-loaded so nothing polls this
in the background.

Fail-soft: export not enabled (dataset missing) -> an honest
{"available": false, note} instead of a fake-empty crash list; any other
failure -> empty rows plus a log line, never an exception.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("ops.crashlytics")

_QUERY = """
SELECT
  issue_id,
  ANY_VALUE(issue_title) AS title,
  ANY_VALUE(issue_subtitle) AS subtitle,
  COUNT(*) AS events,
  COUNT(DISTINCT installation_uuid) AS users,
  MAX(event_timestamp) AS last_seen,
  ANY_VALUE(device.model) AS device_model,
  ANY_VALUE(operating_system.display_version) AS os_version,
  ANY_VALUE(application.display_version) AS app_version,
  LOGICAL_OR(is_fatal) AS fatal
FROM `{project}.{dataset}.{table}`
WHERE event_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
GROUP BY issue_id
ORDER BY events DESC
LIMIT @limit
"""


def _platform_tables(client: Any, project: str, dataset: str) -> list[str]:
    """The per-app batch export tables (skip _REALTIME: same events, fewer
    fields, and querying both would double-count)."""
    tables = []
    for table in client.list_tables(f"{project}.{dataset}"):
        table_id = table.table_id
        if table_id.endswith("_REALTIME"):
            continue
        if table_id.endswith("_ANDROID") or table_id.endswith("_IOS"):
            tables.append(table_id)
    return tables


def mobile_crashes(
    project_id: str,
    dataset: str = "firebase_crashlytics",
    days: int = 7,
    limit: int = 25,
) -> dict[str, Any]:
    """Grouped crash issues across the Android + iOS export tables, in the
    shared crash-feed shape (same rows the Sentry desktop panel emits)."""
    try:
        from google.cloud import bigquery
    except ImportError:
        logger.error("google-cloud-bigquery not installed; mobile crash panel disabled")
        return {"available": False, "note": "google-cloud-bigquery not installed", "crashes": []}

    try:
        client = bigquery.Client(project=project_id)
        tables = _platform_tables(client, project_id, dataset)
    except Exception as exc:
        # The by-far most likely cause is the export never being enabled; say so
        # honestly instead of rendering an empty list that reads as "no crashes".
        logger.warning("Crashlytics BigQuery dataset unavailable (%s.%s): %s", project_id, dataset, exc)
        return {
            "available": False,
            "note": "Crashlytics BigQuery export not enabled (Firebase console -> Integrations -> BigQuery)",
            "crashes": [],
        }

    if not tables:
        return {
            "available": False,
            "note": "Crashlytics export dataset exists but has no app tables yet (first export lands within ~24h of enabling)",
            "crashes": [],
        }

    crashes: list[dict[str, Any]] = []
    for table in tables:
        platform = "android" if table.endswith("_ANDROID") else "ios"
        try:
            job = client.query(
                _QUERY.format(project=project_id, dataset=dataset, table=table),
                job_config=bigquery.QueryJobConfig(query_parameters=[
                    bigquery.ScalarQueryParameter("days", "INT64", days),
                    bigquery.ScalarQueryParameter("limit", "INT64", limit),
                ]),
            )
            for row in job.result(timeout=30):
                crashes.append({
                    "title": str(row["title"] or ""),
                    "subtitle": str(row["subtitle"] or "")[:200],
                    "events": int(row["events"] or 0),
                    "users": int(row["users"] or 0),
                    "last_seen": row["last_seen"].isoformat() if row["last_seen"] else "",
                    "os": platform,
                    "os_version": str(row["os_version"] or ""),
                    "device": str(row["device_model"] or ""),
                    "app_version": str(row["app_version"] or ""),
                    "level": "fatal" if row["fatal"] else "non-fatal",
                })
        except Exception as exc:
            logger.error("Crashlytics query failed for table %s: %s", table, exc)

    crashes.sort(key=lambda c: c["events"], reverse=True)
    return {"available": True, "note": "", "crashes": crashes[:limit]}
