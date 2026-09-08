"""Unified provider cost view with explicit actual/estimated/manual labels.

Three kinds of number live here and are never mixed:

  actual    - billed figures from a billing export (GCP).
  estimated - derived from observed usage and a price table (the LLM ledger,
              Brave queries x a configured unit rate).
  manual    - a subscription the founder typed in, prorated over the range.

A provider with no source stays `unavailable` and renders n/a. It must never
show a zero, because a zero is indistinguishable from "cheap" at a glance.

Every row also carries `console_url`, the provider's own usage dashboard, so
the numbers this file cannot obtain are one click away rather than absent.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import ranges

logger = logging.getLogger("ops.costs")

_KNOWN_PROVIDERS = (
    # "llm" is the combined model spend the Firestore ledger can actually prove;
    # the three model vendors below it carry consoles, not invented splits.
    "llm", "anthropic", "gemini", "openai", "brave", "livekit", "cartesia",
    "deepgram", "gcp", "firebase", "posthog", "newsdata",
)
_TABLE_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_*]+$")

# Each provider's own usage/billing console. These are the authoritative numbers
# for anything this dashboard can only estimate, and the per-model split the
# Firestore LLM ledger structurally cannot give (it stores no model field).
# Deep links, not homepages: one click should land on usage, not a marketing page.
PROVIDER_CONSOLES: dict[str, tuple[str, str]] = {
    "anthropic": ("Anthropic Console usage", "https://console.anthropic.com/settings/usage"),
    "gemini": ("Google AI Studio usage", "https://aistudio.google.com/app/usage"),
    "openai": ("OpenAI usage", "https://platform.openai.com/usage"),
    "llm": ("", ""),
    "brave": ("Brave Search API usage", "https://api-dashboard.search.brave.com/app/usage"),
    "livekit": ("LiveKit Cloud usage", "https://cloud.livekit.io/"),
    "cartesia": ("Cartesia usage", "https://play.cartesia.ai/subscription"),
    "deepgram": ("Deepgram usage", "https://console.deepgram.com/usage"),
    "gcp": ("Cloud Billing reports", "https://console.cloud.google.com/billing"),
    "firebase": ("Firebase usage", "https://console.firebase.google.com/project/juno-2ea45/usage"),
    "posthog": ("PostHog billing", "https://us.posthog.com/organization/billing"),
    "newsdata": ("NewsData dashboard", "https://newsdata.io/dashboard"),
}


def _blank_row(provider: str) -> dict[str, Any]:
    """The canonical shape of one provider row, so every construction site agrees."""
    label, url = PROVIDER_CONSOLES.get(provider, ("", ""))
    return {
        "provider": provider,
        "cost": None,
        "cost_kind": "unavailable",
        "usage": None,
        "status": "needs setup",
        "source": "",
        "console_label": label,
        "console_url": url,
    }


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
    llm_spend: dict,
    usage: dict,
    manual_monthly_costs_json: str,
    brave_cost_per_query_usd: float | None,
    gcp_cost: dict,
) -> dict[str, Any]:
    days = ranges.days(range_key)
    rows: dict[str, dict[str, Any]] = {
        provider: {
            "provider": provider,
            "cost": None,
            "cost_kind": "unavailable",
            "usage": None,
            "status": "needs setup",
            "source": "",
            "console_label": PROVIDER_CONSOLES.get(provider, ("", ""))[0],
            "console_url": PROVIDER_CONSOLES.get(provider, ("", ""))[1],
        }
        for provider in _KNOWN_PROVIDERS
    }

    # LLM spend comes from the per-user daily ledger the backend writes on every
    # call (users/{uid}/cost/{date}). It is a real measurement, but an ESTIMATE by
    # construction: the backend priced it with estimate_microusd() rather than
    # reading an invoice, so it is labelled estimated and never actual.
    #
    # The ledger records no model field, so it cannot be split across
    # anthropic/gemini/openai. Attributing the whole total to any one of them
    # would be a fabrication, so the combined figure sits on its own `llm` row and
    # the three provider rows point at their consoles for the real split.
    if llm_spend.get("available"):
        rows["llm"] = {
            **_blank_row("llm"),
            "cost": llm_spend.get("est_usd"),
            "cost_kind": "estimated",
            "usage": llm_spend.get("generations"),
            "status": "connected",
            "source": "Firestore per-user cost ledger (all models combined)",
        }
        for provider in ("anthropic", "gemini", "openai"):
            rows[provider]["status"] = "in the combined LLM total; open the console for the split"
            rows[provider]["source"] = "no per-model field in the ledger"
    else:
        for provider in ("anthropic", "gemini", "openai"):
            rows[provider]["status"] = "no ledger rows in range"
            rows[provider]["source"] = "backend/src/services/analytics/llm_cost_ledger.py"

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
        row = rows.setdefault(provider, _blank_row(provider))
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
