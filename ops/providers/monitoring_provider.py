"""Cloud Run request-latency percentiles + 5xx count for the juno-backend HTTP service.

IMPORTANT: the voice worker is a LiveKit worker, NOT request/response, so it has no
`run.googleapis.com/request_latencies` metric. This module covers the API service only;
voice latency comes from PostHog `voice_first_response` (see posthog_provider).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("ops.monitoring")

_BACKEND_SERVICE = "juno-backend"


def latency_percentiles(
    project_id: str,
    service_name: str = _BACKEND_SERVICE,
    window_minutes: int = 60,
) -> dict:
    """p50/p95/p99 request latency (ms) over the last `window_minutes` for the API service.

    request_latencies is a DISTRIBUTION metric; ALIGN_PERCENTILE_* extracts the percentile
    of that distribution over the alignment period. Each percentile is its own value, never
    None on success; a value is None only when its query failed.
    """
    try:
        from google.cloud import monitoring_v3
    except ImportError:
        logger.error("google-cloud-monitoring not installed; latency panel disabled")
        return {"p50": None, "p95": None, "p99": None}

    client = monitoring_v3.MetricServiceClient()
    project_name = f"projects/{project_id}"
    now = datetime.now(timezone.utc)
    interval = monitoring_v3.TimeInterval(
        end_time=now,
        start_time=now - timedelta(minutes=window_minutes),
    )
    metric_filter = (
        'metric.type="run.googleapis.com/request_latencies" '
        f'AND resource.labels.service_name="{service_name}"'
    )
    aligners = {
        "p50": monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_50,
        "p95": monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_95,
        "p99": monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_99,
    }

    out: dict[str, float | None] = {}
    for label, aligner in aligners.items():
        try:
            aggregation = monitoring_v3.Aggregation(
                alignment_period={"seconds": window_minutes * 60},
                per_series_aligner=aligner,
                cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_MEAN,
            )
            series = client.list_time_series(request={
                "name": project_name,
                "filter": metric_filter,
                "interval": interval,
                "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                "aggregation": aggregation,
            })
            value: float | None = None
            for one in series:
                for point in one.points:
                    value = round(point.value.double_value, 1)
                    break
                if value is not None:
                    break
            out[label] = value
        except Exception as exc:
            logger.error("latency %s query failed: %s", label, exc)
            out[label] = None
    return out


def latency_percentiles_by_platform(
    project_id: str,
    platform: str,
    window_minutes: int = 60,
    metric_type: str = "logging.googleapis.com/user/request_latency_by_platform",
) -> dict:
    """p95/p99 backend latency for ONE client platform (android/ios/windows).

    Cloud Run's built-in request_latencies metric cannot see custom headers, so
    this reads the log-based DISTRIBUTION metric fed by the backend's
    request-metric middleware (backend/src/main.py logs one request_metric line
    per request with the X-Aura-Platform value; the metric is created once via
    gcloud, see ops/README.md). Until that metric exists or has data, every
    value is None and the UI renders an honest empty state.
    """
    empty = {"p95": None, "p99": None}
    try:
        from google.cloud import monitoring_v3
    except ImportError:
        logger.error("google-cloud-monitoring not installed; platform latency disabled")
        return empty

    client = monitoring_v3.MetricServiceClient()
    now = datetime.now(timezone.utc)
    interval = monitoring_v3.TimeInterval(
        end_time=now,
        start_time=now - timedelta(minutes=window_minutes),
    )
    metric_filter = (
        f'metric.type="{metric_type}" '
        f'AND metric.labels.platform="{platform}"'
    )
    aligners = {
        "p95": monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_95,
        "p99": monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_99,
    }
    out: dict[str, float | None] = {}
    for label, aligner in aligners.items():
        try:
            aggregation = monitoring_v3.Aggregation(
                alignment_period={"seconds": window_minutes * 60},
                per_series_aligner=aligner,
                cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_MEAN,
            )
            series = client.list_time_series(request={
                "name": f"projects/{project_id}",
                "filter": metric_filter,
                "interval": interval,
                "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                "aggregation": aggregation,
            })
            value: float | None = None
            for one in series:
                for point in one.points:
                    value = round(point.value.double_value, 1)
                    break
                if value is not None:
                    break
            out[label] = value
        except Exception as exc:
            logger.error("platform latency %s query failed (%s): %s", label, platform, exc)
            out[label] = None
    return out


def server_error_count(
    project_id: str,
    service_name: str = _BACKEND_SERVICE,
    window_minutes: int = 60,
) -> int | None:
    """Sum of 5xx responses over the window. None on error so the UI can flag it."""
    try:
        from google.cloud import monitoring_v3
    except ImportError:
        return None

    client = monitoring_v3.MetricServiceClient()
    now = datetime.now(timezone.utc)
    interval = monitoring_v3.TimeInterval(
        end_time=now,
        start_time=now - timedelta(minutes=window_minutes),
    )
    metric_filter = (
        'metric.type="run.googleapis.com/request_count" '
        f'AND resource.labels.service_name="{service_name}" '
        'AND metric.labels.response_code_class="5xx"'
    )
    try:
        aggregation = monitoring_v3.Aggregation(
            alignment_period={"seconds": window_minutes * 60},
            per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_SUM,
            cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_SUM,
        )
        series = client.list_time_series(request={
            "name": f"projects/{project_id}",
            "filter": metric_filter,
            "interval": interval,
            "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
            "aggregation": aggregation,
        })
        total = 0
        for one in series:
            for point in one.points:
                total += int(point.value.int64_value or point.value.double_value or 0)
        return total
    except Exception as exc:
        logger.error("server_error_count query failed: %s", exc)
        return None
