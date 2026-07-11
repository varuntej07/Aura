"""Langfuse Metrics API reads: LLM cost per model + tool-call analytics.

Read side of the observability pair: the backend writes one Langfuse generation
per actual LLM provider attempt and one span named "tool:<name>" per tool call
(backend/src/services/analytics/llm_telemetry.py). This module aggregates them
via the public Metrics API (GET /api/public/metrics, basic auth public:secret).

Pure config-in data-out, fail-soft: unconfigured keys or a dead Langfuse yield
an empty payload (with `configured` false) and a log line, never an exception.
Langfuse computes cost server-side from model id + token usage using its managed
price tables for claude-*/gpt-*/gemini-* models.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger("ops.langfuse")

_TOOL_SPAN_PREFIX = "tool:"

RANGE_DAYS = {"today": 1, "7d": 7, "30d": 30}


def _range_window(range_key: str, utc_offset_hours: float = 0.0) -> tuple[datetime, datetime]:
    """Resolve a UI range key to [from, to) datetimes. "today" starts at local
    midnight per the dashboard's configured offset; 7d/30d are rolling windows."""
    now = datetime.now(timezone.utc)
    if range_key == "today":
        local = now + timedelta(hours=utc_offset_hours)
        local_midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return local_midnight - timedelta(hours=utc_offset_hours), now
    days = RANGE_DAYS.get(range_key, 7)
    return now - timedelta(days=days), now


def _run_metrics_query(
    host: str, public_key: str, secret_key: str, query: dict[str, Any]
) -> list[dict[str, Any]]:
    url = f"{host.rstrip('/')}/api/public/metrics"
    response = httpx.get(
        url,
        params={"query": json.dumps(query)},
        auth=(public_key, secret_key),
        timeout=20.0,
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data", [])
    return data if isinstance(data, list) else []


def _row_number(row: dict[str, Any], *candidate_keys: str) -> float:
    """Pull the first numeric value under any candidate key. The Metrics API
    names result columns like "sum_totalCost" / "count_count"; scanning a few
    candidates keeps this resilient to naming variants."""
    for key in candidate_keys:
        value = row.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def cost_by_model(
    host: str,
    public_key: str,
    secret_key: str,
    range_key: str = "7d",
    utc_offset_hours: float = 0.0,
) -> dict[str, Any]:
    """Spend broken out by model over the range, plus a per-day series for the
    stacked chart. Empty (configured=false) when keys are unset; empty rows plus
    a log line on any API failure."""
    if not (public_key and secret_key):
        logger.warning("Langfuse not configured (LANGFUSE_PUBLIC_KEY/SECRET_KEY); cost panel empty")
        return {"configured": False, "models": [], "daily": [], "total_cost": 0.0}

    start, end = _range_window(range_key, utc_offset_hours)
    base_filters = [
        {"column": "type", "operator": "=", "value": "GENERATION", "type": "string"},
    ]
    out: dict[str, Any] = {"configured": True, "models": [], "daily": [], "total_cost": 0.0}

    try:
        totals = _run_metrics_query(host, public_key, secret_key, {
            "view": "observations",
            "metrics": [
                {"measure": "totalCost", "aggregation": "sum"},
                {"measure": "totalTokens", "aggregation": "sum"},
                {"measure": "count", "aggregation": "count"},
            ],
            "dimensions": [{"field": "providedModelName"}],
            "filters": base_filters,
            "fromTimestamp": start.isoformat(),
            "toTimestamp": end.isoformat(),
        })
        models = []
        for row in totals:
            model = str(row.get("providedModelName") or "").strip()
            if not model:
                continue
            cost = _row_number(row, "sum_totalCost", "totalCost")
            models.append({
                "model": model,
                "cost": round(cost, 4),
                "tokens": int(_row_number(row, "sum_totalTokens", "totalTokens")),
                "calls": int(_row_number(row, "count_count", "count")),
            })
        models.sort(key=lambda m: m["cost"], reverse=True)
        out["models"] = models
        out["total_cost"] = round(sum(m["cost"] for m in models), 4)
    except Exception as exc:
        logger.error("Langfuse cost_by_model totals query failed: %s", exc)
        return out

    try:
        series = _run_metrics_query(host, public_key, secret_key, {
            "view": "observations",
            "metrics": [{"measure": "totalCost", "aggregation": "sum"}],
            "dimensions": [{"field": "providedModelName"}],
            "filters": base_filters,
            "timeDimension": {"granularity": "day"},
            "fromTimestamp": start.isoformat(),
            "toTimestamp": end.isoformat(),
        })
        daily: list[dict[str, Any]] = []
        for row in series:
            model = str(row.get("providedModelName") or "").strip()
            # The bucket column name varies by version ("time_dimension" today);
            # find the first ISO-date-looking string value instead of pinning it.
            day = ""
            for key, value in row.items():
                if key == "providedModelName" or not isinstance(value, str):
                    continue
                if len(value) >= 10 and value[4:5] == "-" and value[7:8] == "-":
                    day = value[:10]
                    break
            if not (model and day):
                continue
            daily.append({
                "day": day,
                "model": model,
                "cost": round(_row_number(row, "sum_totalCost", "totalCost"), 4),
            })
        daily.sort(key=lambda r: r["day"])
        out["daily"] = daily
    except Exception as exc:
        logger.error("Langfuse cost_by_model series query failed: %s", exc)

    return out


def tool_call_stats(
    host: str,
    public_key: str,
    secret_key: str,
    range_key: str = "7d",
    tool_filter: str = "",
    utc_offset_hours: float = 0.0,
) -> dict[str, Any]:
    """Most-called tools over the range (count, error count, p95 latency),
    filterable by tool name. The backend names every tool span "tool:<name>",
    so aggregate by name and keep only that prefix (filtering the prefix here,
    not in the API query, avoids depending on string-operator support)."""
    if not (public_key and secret_key):
        logger.warning("Langfuse not configured (LANGFUSE_PUBLIC_KEY/SECRET_KEY); tools panel empty")
        return {"configured": False, "tools": []}

    start, end = _range_window(range_key, utc_offset_hours)
    out: dict[str, Any] = {"configured": True, "tools": []}
    try:
        rows = _run_metrics_query(host, public_key, secret_key, {
            "view": "observations",
            "metrics": [
                {"measure": "count", "aggregation": "count"},
                {"measure": "latency", "aggregation": "p95"},
            ],
            "dimensions": [{"field": "name"}],
            "filters": [
                {"column": "type", "operator": "=", "value": "SPAN", "type": "string"},
            ],
            "fromTimestamp": start.isoformat(),
            "toTimestamp": end.isoformat(),
        })
    except Exception as exc:
        logger.error("Langfuse tool_call_stats query failed: %s", exc)
        return out

    tools = []
    wanted = tool_filter.strip().lower()
    for row in rows:
        name = str(row.get("name") or "")
        if not name.startswith(_TOOL_SPAN_PREFIX):
            continue
        tool_name = name[len(_TOOL_SPAN_PREFIX):]
        if wanted and wanted not in tool_name.lower():
            continue
        tools.append({
            "tool": tool_name,
            "calls": int(_row_number(row, "count_count", "count")),
            "p95_ms": round(_row_number(row, "p95_latency", "latency") * 1000, 1) or None,
        })
    tools.sort(key=lambda t: t["calls"], reverse=True)
    out["tools"] = tools
    return out
