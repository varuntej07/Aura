"""Unified provider cost view with explicit actual/estimated/manual labels."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("ops.costs")

_KNOWN_PROVIDERS = (
    "anthropic", "gemini", "openai", "brave", "livekit", "cartesia",
    "deepgram", "gcp", "firebase", "posthog", "newsdata",
)
_MODEL_PROVIDER_PREFIXES = {
    "claude": "anthropic", "gemini": "gemini", "gpt": "openai",
    "o1": "openai", "o3": "openai", "o4": "openai",
}
_TABLE_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_*]+$")


def _model_provider(model: str) -> str:
    lowered = model.lower()
    for prefix, provider in _MODEL_PROVIDER_PREFIXES.items():
        if prefix in lowered:
            return provider
    return "other_llm"


def _manual_monthly_costs(raw: str) -> dict[str, float]:
    try:
        payload = json.loads(raw or "{}")
        if not isinstance(payload, dict):
            return {}
        return {
            str(key).lower(): max(0.0, float(value))
            for key, value in payload.items()
            if isinstance(value, (int, float))
        }
    except Exception:
        logger.warning("OPS_PROVIDER_MONTHLY_COSTS_JSON invalid; manual costs omitted")
        return {}


def gcp_billing_cost(project_id: str, table: str, days: int) -> dict[str, Any]:
    """Actual net GCP cost from an optional standard/detailed billing export."""
    if not table:
        return {"configured": False, "cost": None}
    if not _TABLE_RE.fullmatch(table):
        logger.error("Invalid OPS_GCP_BILLING_TABLE identifier")
        return {"configured": False, "cost": None}
    try:
        from google.cloud import bigquery

        query = f"""
        SELECT
          SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0))
            AS net_cost
        FROM `{table}`
        WHERE project.id = @project_id
          AND usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
        """
        job = bigquery.Client(project=project_id).query(
            query,
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("project_id", "STRING", project_id),
                bigquery.ScalarQueryParameter("days", "INT64", days),
            ]),
        )
        row = next(iter(job.result(timeout=30)), None)
        return {
            "configured": True,
            "cost": round(float(row["net_cost"] or 0), 4) if row else 0.0,
        }
    except Exception as exc:
        logger.error("GCP billing query failed: %s", exc)
        return {"configured": True, "cost": None, "error": type(exc).__name__}


def build_provider_costs(
    *,
    range_key: str,
    llm_cost: dict,
    usage: dict,
    manual_monthly_costs_json: str,
    brave_cost_per_query_usd: float | None,
    gcp_cost: dict,
) -> dict[str, Any]:
    days = {"today": 1, "7d": 7, "30d": 30}.get(range_key, 7)
    rows: dict[str, dict[str, Any]] = {
        provider: {
            "provider": provider,
            "cost": None,
            "cost_kind": "unavailable",
            "usage": None,
            "status": (
                "usage tracking intentionally disabled"
                if provider in {"anthropic", "gemini", "openai"}
                else "needs setup"
            ),
            "source": "",
        }
        for provider in _KNOWN_PROVIDERS
    }

    for model in llm_cost.get("models", []):
        provider = _model_provider(str(model.get("model") or ""))
        row = rows.setdefault(provider, {
            "provider": provider, "cost": None, "cost_kind": "unavailable",
            "usage": None, "status": "needs setup", "source": "",
        })
        row["cost"] = round(
            float(row.get("cost") or 0) + float(model.get("cost") or 0), 4
        )
        row["usage"] = int(row.get("usage") or 0) + int(model.get("calls") or 0)
        row["cost_kind"] = "estimated"
        row["status"] = "connected"
        row["source"] = "Langfuse token pricing"

    brave_rows = [row for row in usage.get("rows", []) if row.get("provider") == "brave"]
    brave_billable = sum(int(row.get("billable") or 0) for row in brave_rows)
    brave = rows["brave"]
    brave["usage"] = brave_billable
    brave["status"] = "connected" if usage.get("configured") else "needs log access"
    brave["source"] = "structured provider_request events"
    if brave_cost_per_query_usd is not None:
        brave["cost"] = round(brave_billable * brave_cost_per_query_usd, 4)
        brave["cost_kind"] = "estimated"

    if gcp_cost.get("configured"):
        rows["gcp"].update({
            "cost": gcp_cost.get("cost"),
            "cost_kind": "actual",
            "status": "connected" if gcp_cost.get("cost") is not None else "source error",
            "source": "Cloud Billing BigQuery export",
        })

    manual = _manual_monthly_costs(manual_monthly_costs_json)
    fraction = min(days, 30) / 30
    for provider, monthly_cost in manual.items():
        row = rows.setdefault(provider, {
            "provider": provider, "cost": None, "cost_kind": "unavailable",
            "usage": None, "status": "needs setup", "source": "",
        })
        if row["cost"] is None:
            row.update({
                "cost": round(monthly_cost * fraction, 4),
                "cost_kind": "manual subscription",
                "status": "configured",
                "source": "prorated monthly plan",
            })

    ordered = sorted(rows.values(), key=lambda row: (
        row["cost"] is None, -float(row["cost"] or 0), row["provider"],
    ))
    actual_rows = [row for row in ordered if row["cost_kind"] == "actual"]
    estimated_rows = [
        row for row in ordered
        if row["cost_kind"] in {"estimated", "manual subscription"}
    ]
    actual = sum(float(row["cost"] or 0) for row in actual_rows)
    estimated = sum(float(row["cost"] or 0) for row in estimated_rows)
    return {
        "range": range_key,
        "days": days,
        "providers": ordered,
        "actual_total": round(actual, 4) if actual_rows else None,
        "estimated_total": round(estimated, 4) if estimated_rows else None,
    }
