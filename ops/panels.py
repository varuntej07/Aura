"""Composes the data providers into the per-endpoint payloads the UI fetches.

Every gated /api/* route calls exactly one function here. Config is read from
the environment so the providers stay pure (config in, data out).

READ DISCIPLINE (this module is the cost gate for the whole dashboard):
the UI NEVER auto-refreshes — each tab loads once on first view and only the
Refresh button (rate-limited to one hit per 60s client-side) re-fetches. On
top of that, every non-interactive payload goes through _cached() with a TTL
matched to how fast that source actually changes, so N open devices (or
anything curling the API in a loop) cost ONE provider fetch per TTL window.
One uncached Overview load is ~140 Firestore doc reads (users 16 + messages
60 + voice 30 + notifications 50 + feedback 20); the cache makes that the
per-minute ceiling, not the per-request price. The interactive log search is
deliberately NOT cached.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from providers import (
    cost_provider,
    crashlytics_provider,
    firestore_provider,
    github_releases_provider,
    langfuse_provider,
    logging_provider,
    monitoring_provider,
    posthog_provider,
)

PROJECT_ID = os.environ.get("GCP_PROJECT", "juno-2ea45")
UTC_OFFSET_HOURS = float(os.environ.get("OPS_UTC_OFFSET_HOURS", "0") or 0)
POSTHOG_HOST = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")
POSTHOG_PROJECT_ID = os.environ.get("POSTHOG_PROJECT_ID", "")
POSTHOG_KEY = os.environ.get("POSTHOG_PERSONAL_KEY", "")
# aura-web may live in a different PostHog project than the app (unverified,
# see ECOSYSTEM.md "Known gaps"); defaults to the app's project id.
POSTHOG_WEB_PROJECT_ID = os.environ.get("OPS_POSTHOG_WEB_PROJECT_ID", "") or POSTHOG_PROJECT_ID
LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "https://us.cloud.langfuse.com")
LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
CRASHLYTICS_BQ_DATASET = os.environ.get("OPS_CRASHLYTICS_BQ_DATASET", "firebase_crashlytics")
GCP_BILLING_TABLE = os.environ.get("OPS_GCP_BILLING_TABLE", "")
PROVIDER_MONTHLY_COSTS_JSON = os.environ.get("OPS_PROVIDER_MONTHLY_COSTS_JSON", "")

try:
    BRAVE_COST_PER_QUERY_USD = float(os.environ["OPS_BRAVE_COST_PER_QUERY_USD"])
except (KeyError, TypeError, ValueError):
    BRAVE_COST_PER_QUERY_USD = None

# ── In-process TTL cache (the read-cost gate; see module docstring) ──────────
# The UI never auto-refreshes (data loads once per tab; the Refresh button is
# rate-limited to one hit per 60s), so these TTLs are defense-in-depth against
# multiple open devices and anything hitting the API directly. Feeds sit just
# under the button cooldown (55s) so every ALLOWED refresh is fresh; the users
# directory churns on signups only (120s); analytics aggregates move slowly
# (120s); the BigQuery crash scan bills per byte scanned (300s).
TTL_FEEDS_S = 55.0
TTL_USERS_S = 120.0
TTL_ANALYTICS_S = 120.0
TTL_CRASH_SCAN_S = 300.0

_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def _cached(key: str, ttl_seconds: float, producer: Callable[[], Any]) -> Any:
    """Serve `key` from the in-process cache when fresh, else produce + store.

    The producer runs OUTSIDE the lock (providers do network I/O; holding the
    lock would serialize every panel behind the slowest source). Two threads
    racing the same expired key may both produce once; that is an accepted,
    bounded cost, far cheaper than a lock-held fetch. Providers fail soft, so
    a produced value is always servable (an empty section caches too, which
    stops a dead source from being hammered every tick).
    """
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and (now - hit[0]) < ttl_seconds:
            return hit[1]
    value = producer()
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)
    return value


def _user_directory() -> dict[str, dict]:
    """The shared users/{uid} read, cached: every panel maps uid -> name off
    this one load (see firestore_read_audit_20260706 for why it must never be
    fetched per panel)."""
    return _cached("users", TTL_USERS_S, firestore_provider.load_user_directory)


def build_dashboard() -> dict:
    """Assemble the Overview core. Each provider already fails soft, so a dead
    source yields an empty section rather than a 500, and every section rides
    the TTL cache so the browser's 30s poll costs at most one provider fetch
    per TTL window regardless of how many devices are watching.
    """
    now = datetime.now(timezone.utc)
    users = _user_directory()

    metrics_and_table = firestore_provider.user_metrics_and_table(users, now, UTC_OFFSET_HOURS)
    metrics = metrics_and_table["metrics"]
    metrics["messages_today"] = _cached(
        "messages_today", TTL_FEEDS_S,
        lambda: firestore_provider.messages_today_count(now, UTC_OFFSET_HOURS),
    )

    latency = _cached(
        "latency", TTL_FEEDS_S, lambda: monitoring_provider.latency_percentiles(PROJECT_ID)
    )
    metrics["p95_ms"] = latency.get("p95")
    metrics["p99_ms"] = latency.get("p99")
    metrics["server_errors"] = _cached(
        "server_errors", TTL_FEEDS_S, lambda: monitoring_provider.server_error_count(PROJECT_ID)
    )

    return {
        "generated_at": now.isoformat(),
        "metrics": metrics,
        "messages": _cached(
            "messages", TTL_FEEDS_S, lambda: firestore_provider.latest_text_messages(users, 60)
        ),
        "voice": _cached(
            "voice", TTL_FEEDS_S, lambda: firestore_provider.latest_voice_sessions(users, 30)
        ),
        "users": metrics_and_table["users"],
        "feedback": _cached(
            "feedback", TTL_FEEDS_S, lambda: firestore_provider.recent_feedback(20)
        ),
        "latency": latency,
        "errors": _cached(
            "errors", TTL_FEEDS_S, lambda: logging_provider.recent_errors(PROJECT_ID)
        ),
        "screens": _cached(
            "screens", TTL_ANALYTICS_S,
            lambda: posthog_provider.top_screens(POSTHOG_HOST, POSTHOG_PROJECT_ID, POSTHOG_KEY),
        ),
        # Recommendation trace: what each user was actually sent + why + did it land,
        # and the recommender's own per-tick health line for the silent ticks.
        "recommendations": _cached(
            "recommendations", TTL_FEEDS_S,
            lambda: firestore_provider.latest_notifications(users),
        ),
        "recommender_health": _cached(
            "recommender_health", TTL_FEEDS_S,
            lambda: logging_provider.recent_recommender_health(PROJECT_ID),
        ),
    }


def build_llm_cost(range_key: str = "7d") -> dict:
    """LLM spend by model over the range (Langfuse Metrics API). Cached per
    range so flipping the selector back and forth costs one query per window."""
    return _cached(f"llm_cost:{range_key}", TTL_ANALYTICS_S, lambda: langfuse_provider.cost_by_model(
        LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY,
        range_key=range_key, utc_offset_hours=UTC_OFFSET_HOURS,
    ))


def build_llm_tools(range_key: str = "7d", tool_filter: str = "") -> dict:
    """Tool-call analytics over the range (Langfuse tool:<name> spans). The
    tool filter is applied AFTER the (cached) aggregate query, so typing in the
    filter box never multiplies API calls."""
    return _cached(f"llm_tools:{range_key}:{tool_filter.strip().lower()}", TTL_ANALYTICS_S,
        lambda: langfuse_provider.tool_call_stats(
            LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY,
            range_key=range_key, tool_filter=tool_filter, utc_offset_hours=UTC_OFFSET_HOURS,
        ))


def build_provider_costs(range_key: str = "7d") -> dict:
    """Provider spend and usage with actual/estimated/manual labels."""
    days = {"today": 1, "7d": 7, "30d": 30}.get(range_key, 7)

    def _produce() -> dict:
        usage = _cached(
            f"provider_usage:{days}",
            TTL_ANALYTICS_S,
            lambda: logging_provider.provider_usage_stats(PROJECT_ID, days=days),
        )
        gcp_cost = _cached(
            f"gcp_billing:{days}",
            TTL_CRASH_SCAN_S,
            lambda: cost_provider.gcp_billing_cost(PROJECT_ID, GCP_BILLING_TABLE, days),
        )
        result = cost_provider.build_provider_costs(
            range_key=range_key,
            llm_cost={"configured": False, "models": []},
            usage=usage,
            manual_monthly_costs_json=PROVIDER_MONTHLY_COSTS_JSON,
            brave_cost_per_query_usd=BRAVE_COST_PER_QUERY_USD,
            gcp_cost=gcp_cost,
        )
        result["generated_at"] = datetime.now(timezone.utc).isoformat()
        result["usage"] = usage
        return result

    return _cached(f"provider_costs:{range_key}", TTL_ANALYTICS_S, _produce)


def build_overview_analytics() -> dict:
    """The slower Overview panels (retention, funnels, default LLM views);
    each section is a network call to PostHog or Langfuse, so the whole
    payload rides one analytics-TTL cache entry. Every provider fails soft,
    so one dead source yields one empty section, never a 500."""
    def _produce() -> dict:
        users = _user_directory()
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "retention": posthog_provider.retention_summary(POSTHOG_HOST, POSTHOG_PROJECT_ID, POSTHOG_KEY),
            "notification_funnel": posthog_provider.notification_funnel(
                POSTHOG_HOST, POSTHOG_PROJECT_ID, POSTHOG_KEY, days=7,
            ),
            "paywall_funnel": posthog_provider.paywall_funnel(
                POSTHOG_HOST, POSTHOG_PROJECT_ID, POSTHOG_KEY, days=30,
            ),
            "payment_intents": firestore_provider.payment_intents(users),
            "llm_cost": {"configured": False},
            "llm_tools": {"configured": False},
        }
    return _cached("overview_analytics", TTL_ANALYTICS_S, _produce)


def _platform_latency_block(platform_keys: list[str]) -> dict:
    """Backend p95/p99 per platform from the request_metric log-based
    distribution metric (None until the metric exists and clients send the
    X-Aura-Platform header, which the UI states honestly)."""
    return {
        platform: monitoring_provider.latency_percentiles_by_platform(PROJECT_ID, platform)
        for platform in platform_keys
    }


def build_mobile_tab() -> dict:
    """Mobile tab: Crashlytics crash feed, per-platform latency, client E2E
    latency, voice first-response, and the honest downloads placeholder.
    The BigQuery scan is the expensive piece (billed per byte), so it gets the
    long crash-scan TTL; the rest rides the analytics TTL."""
    def _produce() -> dict:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "crashes": _cached(
                "crashes_mobile", TTL_CRASH_SCAN_S,
                lambda: crashlytics_provider.mobile_crashes(PROJECT_ID, CRASHLYTICS_BQ_DATASET),
            ),
            "backend_latency": _platform_latency_block(["android", "ios"]),
            "chat_latency": posthog_provider.chat_latency_percentiles(
                POSTHOG_HOST, POSTHOG_PROJECT_ID, POSTHOG_KEY, days=7, platform="mobile",
            ),
            "voice_first_response": posthog_provider.voice_first_response_stats(
                POSTHOG_HOST, POSTHOG_PROJECT_ID, POSTHOG_KEY, days=7, platform="mobile",
            ),
            "voice_worker_latency": _cached(
                "voice_worker_latency:7d",
                TTL_ANALYTICS_S,
                lambda: logging_provider.voice_latency_stats(PROJECT_ID, days=7),
            ),
            # TODO(store launch): wire Play Console / App Store Connect APIs once
            # the apps are live. Both are still in review; an honest empty state
            # beats querying real APIs against nothing.
            "downloads": {
                "available": False,
                "note": "Not available yet: both store listings are still in review, no live downloads exist.",
            },
        }
    return _cached("tab_mobile", TTL_ANALYTICS_S, _produce)


def build_desktop_tab() -> dict:
    """Desktop tab: Sentry crash feed (Aura-Desktop Tauri client), per-platform
    latency, client E2E latency, voice first-response, GitHub release downloads
    (which carry their own 15-min in-provider cache on top)."""
    def _produce() -> dict:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "crashes": {
                "available": False,
                "note": (
                    "Sentry is intentionally disabled. Desktop operational errors "
                    "are available in Logs; Firebase Crashlytics powers the mobile app."
                ),
                "crashes": [],
            },
            "backend_latency": _platform_latency_block(["windows"]),
            "chat_latency": posthog_provider.chat_latency_percentiles(
                POSTHOG_HOST, POSTHOG_PROJECT_ID, POSTHOG_KEY, days=7, platform="desktop",
            ),
            "voice_first_response": posthog_provider.voice_first_response_stats(
                POSTHOG_HOST, POSTHOG_PROJECT_ID, POSTHOG_KEY, days=7, platform="desktop",
            ),
            "voice_worker_latency": _cached(
                "voice_worker_latency:7d",
                TTL_ANALYTICS_S,
                lambda: logging_provider.voice_latency_stats(PROJECT_ID, days=7),
            ),
            "downloads": github_releases_provider.desktop_downloads(GITHUB_TOKEN),
        }
    return _cached("tab_desktop", TTL_ANALYTICS_S, _produce)


def build_web_tab() -> dict:
    """Web tab: auravoiceapp.com marketing analytics only (pageviews, referrers,
    download funnel). Installs are proxied by GitHub release download counts."""
    def _produce() -> dict:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "analytics": posthog_provider.web_analytics(
                POSTHOG_HOST, POSTHOG_WEB_PROJECT_ID, POSTHOG_KEY, days=30,
            ),
            "installs": github_releases_provider.desktop_downloads(GITHUB_TOKEN),
        }
    return _cached("tab_web", TTL_ANALYTICS_S, _produce)


def search_logs(services: str = "", severity: str = "ERROR", text: str = "", hours: int = 24, limit: int = 100) -> dict:
    """Merge Cloud Logging and redacted client error events."""
    requested = [s.strip() for s in services.split(",") if s.strip()]
    selected = [s for s in requested if s in logging_provider.KNOWN_SERVICES]
    include_client = not requested or "mobile-client" in requested
    if not requested:
        selected = list(logging_provider.KNOWN_SERVICES)
    entries = logging_provider.search_logs(
        PROJECT_ID, services=selected, min_severity=severity,
        text=text, hours=hours, limit=limit,
    ) if selected else []
    if include_client:
        entries.extend(posthog_provider.client_log_entries(
            POSTHOG_HOST,
            POSTHOG_PROJECT_ID,
            POSTHOG_KEY,
            min_severity=severity,
            hours=hours,
            text=text,
            limit=limit,
        ))
    entries.sort(key=lambda item: item.get("at") or "", reverse=True)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "services": [*logging_provider.KNOWN_SERVICES, "mobile-client"],
        "voice_note": (
            "LiveKit worker errors appear here after its GCP log drain is configured. "
            "Mobile client warnings/errors are redacted before they reach PostHog."
        ),
        "entries": entries[:limit],
    }
