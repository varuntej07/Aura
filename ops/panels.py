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

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

import ranges
from providers import (
    cost_provider,
    crashlytics_provider,
    firestore_provider,
    github_releases_provider,
    logging_provider,
    monitoring_provider,
    posthog_provider,
)

logger = logging.getLogger("ops.panels")

PROJECT_ID = os.environ.get("GCP_PROJECT", "juno-2ea45")
UTC_OFFSET_HOURS = float(os.environ.get("OPS_UTC_OFFSET_HOURS", "0") or 0)
POSTHOG_HOST = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")
POSTHOG_PROJECT_ID = os.environ.get("POSTHOG_PROJECT_ID", "")
POSTHOG_KEY = os.environ.get("POSTHOG_PERSONAL_KEY", "")
# aura-web may live in a different PostHog project than the app (unverified,
# see ECOSYSTEM.md "Known gaps"); defaults to the app's project id.
POSTHOG_WEB_PROJECT_ID = os.environ.get("OPS_POSTHOG_WEB_PROJECT_ID", "") or POSTHOG_PROJECT_ID
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


# Panel work is almost entirely WAITING: each provider call is one blocking HTTP
# or gRPC round trip to Firestore, PostHog, Cloud Logging, Cloud Monitoring or
# BigQuery. A cold Overview load makes about twenty of them and they were issued
# strictly one after another, so page latency was the SUM of every source's
# response time instead of the slowest one.
#
# The pool is deliberately small. These calls are I/O-bound, not CPU-bound, so a
# handful of threads covers the fan-out; a large pool would only multiply open
# connections and make a slow provider harder to attribute. The TTL cache below
# is unchanged and still the read-cost gate: concurrency changes WHEN calls
# happen, never HOW MANY.
_FANOUT_WORKERS = 8


def _gather(tasks: dict[str, Callable[[], Any]]) -> dict[str, Any]:
    """Run independent provider reads concurrently, keyed by section name.

    Every provider already fails soft and returns an empty section rather than
    raising, so this only has to guard against the unexpected: an exception here
    is logged and becomes None for that one section, exactly as a sequential
    build would have degraded, never a 500 for the whole page.
    """
    if not tasks:
        return {}
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=min(_FANOUT_WORKERS, len(tasks))) as pool:
        futures = {name: pool.submit(task) for name, task in tasks.items()}
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except Exception as exc:
                logger.error("panel section %r failed: %s", name, exc)
                results[name] = None
    return results


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

    # Everything below is an independent round trip to a different system, so it
    # is issued concurrently rather than in sequence (see _gather).
    sections = _gather({
        "messages_today": lambda: _cached(
            "messages_today", TTL_FEEDS_S,
            lambda: firestore_provider.messages_today_count(now, UTC_OFFSET_HOURS),
        ),
        "latency": lambda: _cached(
            "latency", TTL_FEEDS_S,
            lambda: monitoring_provider.latency_percentiles(PROJECT_ID),
        ),
        "server_errors": lambda: _cached(
            "server_errors", TTL_FEEDS_S,
            lambda: monitoring_provider.server_error_count(PROJECT_ID),
        ),
        "messages": lambda: _cached(
            "messages", TTL_FEEDS_S,
            lambda: firestore_provider.latest_text_messages(users, 60),
        ),
        "voice": lambda: _cached(
            "voice", TTL_FEEDS_S,
            lambda: firestore_provider.latest_voice_sessions(users, 30),
        ),
        "feedback": lambda: _cached(
            "feedback", TTL_FEEDS_S, lambda: firestore_provider.recent_feedback(20),
        ),
        "errors": lambda: _cached(
            "errors", TTL_FEEDS_S, lambda: logging_provider.recent_errors(PROJECT_ID),
        ),
        "screens": lambda: _cached(
            "screens", TTL_ANALYTICS_S,
            lambda: posthog_provider.top_screens(POSTHOG_HOST, POSTHOG_PROJECT_ID, POSTHOG_KEY),
        ),
        # Recommendation trace: what each user was actually sent + why + did it
        # land, and the recommender's own per-tick health line for silent ticks.
        "recommendations": lambda: _cached(
            "recommendations", TTL_FEEDS_S,
            lambda: firestore_provider.latest_notifications(users),
        ),
        "recommender_health": lambda: _cached(
            "recommender_health", TTL_FEEDS_S,
            lambda: logging_provider.recent_recommender_health(PROJECT_ID),
        ),
        # Real desktop adoption, so the Overview can flag installer fetches that
        # never became a signed-in install.
        "desktop": lambda: _cached(
            "desktop_installs", TTL_USERS_S,
            lambda: firestore_provider.desktop_installs(users, now),
        ),
        "llm_spend": lambda: _cached(
            "llm_spend:7", TTL_ANALYTICS_S,
            lambda: firestore_provider.llm_spend(users, now, days=7),
        ),
    })

    latency = sections.get("latency") or {}
    metrics["messages_today"] = sections.get("messages_today")
    metrics["p95_ms"] = latency.get("p95")
    metrics["p99_ms"] = latency.get("p99")
    metrics["server_errors"] = sections.get("server_errors")

    return {
        "generated_at": now.isoformat(),
        "metrics": metrics,
        # The uid sets behind each count, so the UI can answer "who are these 6?"
        "cohorts": metrics_and_table["cohorts"],
        "messages": sections.get("messages") or [],
        "voice": sections.get("voice") or [],
        "users": metrics_and_table["users"],
        "feedback": sections.get("feedback") or [],
        "latency": latency,
        "errors": sections.get("errors") or [],
        "screens": sections.get("screens") or [],
        "recommendations": sections.get("recommendations") or [],
        "recommender_health": sections.get("recommender_health") or [],
        "desktop": sections.get("desktop") or {},
        "llm_spend": sections.get("llm_spend") or {},
    }


def build_provider_costs(range_key: str = "7d") -> dict:
    """Provider spend and usage with actual/estimated/manual labels."""
    days = ranges.days(range_key)

    def _produce() -> dict:
        now = datetime.now(timezone.utc)
        parts = _gather({
            "usage": lambda: _cached(
                f"provider_usage:{days}", TTL_ANALYTICS_S,
                lambda: logging_provider.provider_usage_stats(PROJECT_ID, days=days),
            ),
            "gcp_cost": lambda: _cached(
                f"gcp_billing:{days}", TTL_CRASH_SCAN_S,
                lambda: cost_provider.gcp_billing_cost(PROJECT_ID, GCP_BILLING_TABLE, days),
            ),
            # Real LLM spend from the ledger the backend writes on every call.
            "llm_spend": lambda: _cached(
                f"llm_spend:{days}", TTL_ANALYTICS_S,
                lambda: firestore_provider.llm_spend(_user_directory(), now, days=days),
            ),
        })
        usage = parts.get("usage") or {"configured": False, "days": days, "rows": []}
        llm_spend = parts.get("llm_spend") or {"available": False}
        result = cost_provider.build_provider_costs(
            range_key=range_key,
            llm_spend=llm_spend,
            usage=usage,
            manual_monthly_costs_json=PROVIDER_MONTHLY_COSTS_JSON,
            brave_cost_per_query_usd=BRAVE_COST_PER_QUERY_USD,
            gcp_cost=parts.get("gcp_cost") or {"configured": False, "cost": None},
        )
        result["generated_at"] = now.isoformat()
        result["usage"] = usage
        result["llm_spend"] = llm_spend
        result["ranges"] = list(ranges.RANGE_KEYS)
        return result

    return _cached(f"provider_costs:{range_key}", TTL_ANALYTICS_S, _produce)


def build_overview_analytics() -> dict:
    """The slower Overview panels (retention and the two funnels);
    each section is a network call to PostHog or Firestore, so the whole
    payload rides one analytics-TTL cache entry. Every provider fails soft,
    so one dead source yields one empty section, never a 500."""
    def _produce() -> dict:
        users = _user_directory()
        sections = _gather({
            "retention": lambda: posthog_provider.retention_summary(
                POSTHOG_HOST, POSTHOG_PROJECT_ID, POSTHOG_KEY,
            ),
            "notification_funnel": lambda: posthog_provider.notification_funnel(
                POSTHOG_HOST, POSTHOG_PROJECT_ID, POSTHOG_KEY, days=7,
            ),
            "paywall_funnel": lambda: posthog_provider.paywall_funnel(
                POSTHOG_HOST, POSTHOG_PROJECT_ID, POSTHOG_KEY, days=30,
            ),
            "payment_intents": lambda: firestore_provider.payment_intents(users),
        })
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "retention": sections.get("retention") or {},
            "notification_funnel": sections.get("notification_funnel") or {},
            "paywall_funnel": sections.get("paywall_funnel") or {},
            "payment_intents": sections.get("payment_intents") or [],
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
        sections = _gather({
            "crashes": lambda: _cached(
                "crashes_mobile", TTL_CRASH_SCAN_S,
                lambda: crashlytics_provider.mobile_crashes(PROJECT_ID, CRASHLYTICS_BQ_DATASET),
            ),
            "backend_latency": lambda: _platform_latency_block(["android", "ios"]),
            "chat_latency": lambda: posthog_provider.chat_latency_percentiles(
                POSTHOG_HOST, POSTHOG_PROJECT_ID, POSTHOG_KEY, days=7, platform="mobile",
            ),
            "voice_first_response": lambda: posthog_provider.voice_first_response_stats(
                POSTHOG_HOST, POSTHOG_PROJECT_ID, POSTHOG_KEY, days=7, platform="mobile",
            ),
            "voice_worker_latency": lambda: _cached(
                "voice_worker_latency:7d", TTL_ANALYTICS_S,
                lambda: logging_provider.voice_latency_stats(PROJECT_ID, days=7),
            ),
        })
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            **{key: (value or {}) for key, value in sections.items()},
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
    """Desktop tab: adoption (web click -> installer fetches -> real signed-in
    installs), per-platform backend latency, client E2E latency, and voice
    first-response. GitHub fetch counts carry their own 15-min provider cache."""
    def _produce() -> dict:
        now = datetime.now(timezone.utc)
        users = _user_directory()
        sections = _gather({
            "backend_latency": lambda: _platform_latency_block(["windows"]),
            "chat_latency": lambda: posthog_provider.chat_latency_percentiles(
                POSTHOG_HOST, POSTHOG_PROJECT_ID, POSTHOG_KEY, days=7, platform="desktop",
            ),
            "voice_first_response": lambda: posthog_provider.voice_first_response_stats(
                POSTHOG_HOST, POSTHOG_PROJECT_ID, POSTHOG_KEY, days=7, platform="desktop",
            ),
            "voice_worker_latency": lambda: _cached(
                "voice_worker_latency:7d", TTL_ANALYTICS_S,
                lambda: logging_provider.voice_latency_stats(PROJECT_ID, days=7),
            ),
            # Installer FETCHES (bot- and updater-inflated, see the provider).
            "downloads": lambda: github_releases_provider.desktop_downloads(GITHUB_TOKEN),
            # Real INSTALLS that reached a signed-in state. The gap between this
            # and the number above is the whole point of showing both.
            "installs": lambda: _cached(
                "desktop_installs", TTL_USERS_S,
                lambda: firestore_provider.desktop_installs(users, now),
            ),
            "web_funnel": lambda: _cached(
                "web_analytics", TTL_ANALYTICS_S,
                lambda: posthog_provider.web_analytics(
                    POSTHOG_HOST, POSTHOG_WEB_PROJECT_ID, POSTHOG_KEY, days=30,
                ),
            ),
        })
        return {
            "generated_at": now.isoformat(),
            "crashes": {
                "available": False,
                "note": (
                    "Desktop crash reporting is not read here. Operational errors "
                    "are in Logs; Firebase Crashlytics powers the mobile app."
                ),
                "crashes": [],
            },
            **{key: (value or {}) for key, value in sections.items()},
        }
    return _cached("tab_desktop", TTL_ANALYTICS_S, _produce)


def build_web_tab() -> dict:
    """Web tab: auravoiceapp.com marketing analytics (pageviews, referrers) plus
    the full download funnel. The last step is real signed-in installs from
    Firestore, NOT GitHub's fetch counter, which counts bots and auto-updates."""
    def _produce() -> dict:
        now = datetime.now(timezone.utc)
        users = _user_directory()
        sections = _gather({
            "analytics": lambda: _cached(
                "web_analytics", TTL_ANALYTICS_S,
                lambda: posthog_provider.web_analytics(
                    POSTHOG_HOST, POSTHOG_WEB_PROJECT_ID, POSTHOG_KEY, days=30,
                ),
            ),
            "fetches": lambda: github_releases_provider.desktop_downloads(GITHUB_TOKEN),
            "installs": lambda: _cached(
                "desktop_installs", TTL_USERS_S,
                lambda: firestore_provider.desktop_installs(users, now),
            ),
        })
        return {
            "generated_at": now.isoformat(),
            "analytics": sections.get("analytics") or {},
            "fetches": sections.get("fetches") or {},
            "installs": sections.get("installs") or {},
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
