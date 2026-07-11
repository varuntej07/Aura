"""PostHog HogQL reads: top screens by view count (the "where do users spend time" panel).

Needs a PERSONAL API key (phx_...) with read scope. The public phc_ project key the app
embeds is WRITE-ONLY and cannot read, so it will not work here. Set POSTHOG_PERSONAL_KEY
and POSTHOG_PROJECT_ID (the numeric project id, not the phc_ key).

CONFIRM BEFORE RELYING ON THIS PANEL: the screen event name below must match what the
Flutter AppRouteObserver actually emits. PostHog's mobile default is the `$screen` event
with a `$screen_name` property; if the app logs a custom event name, change the two
constants and the HogQL keys to match. This is the one contract in the dashboard not yet
verified against a writer.
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("ops.posthog")

# TODO(confirm): match these to lib/core/analytics + AppRouteObserver before trusting the panel.
SCREEN_EVENT = "$screen"
SCREEN_NAME_PROPERTY = "$screen_name"


def _run_hogql(host: str, project_id: str, personal_key: str, hogql: str) -> list[list]:
    url = f"{host.rstrip('/')}/api/projects/{project_id}/query/"
    response = httpx.post(
        url,
        headers={"Authorization": f"Bearer {personal_key}"},
        json={"query": {"kind": "HogQLQuery", "query": hogql}},
        timeout=20.0,
    )
    response.raise_for_status()
    return response.json().get("results", [])


# Platform filter fragments for HogQL. Mobile events come from posthog_flutter
# (SDK auto-sets $os to Android/iOS); Windows desktop events come from the app's
# raw-HTTP capture client which explicitly injects platform='windows'
# (lib/core/analytics/posthog_http_analytics.dart).
_PLATFORM_FRAGMENTS = {
    "mobile": "properties.$os IN ('Android', 'iOS')",
    "desktop": "(properties.platform = 'windows' OR properties.$os = 'Windows')",
}


def _platform_clause(platform: str) -> str:
    fragment = _PLATFORM_FRAGMENTS.get(platform)
    return f"AND {fragment} " if fragment else ""


def top_screens(
    host: str,
    project_id: str,
    personal_key: str,
    days: int = 7,
    limit: int = 12,
) -> list[dict]:
    """Most-viewed screens over the last `days`. Empty list when unconfigured or on error.

    v1 ranks by view count, which is reliable. True dwell time ("most time spent") needs
    per-session windowing over consecutive screen events and is a deliberate later refinement,
    view count is the honest first cut, not a fabricated duration.
    """
    if not (personal_key and project_id):
        logger.warning("PostHog not configured (POSTHOG_PERSONAL_KEY / POSTHOG_PROJECT_ID); screens panel empty")
        return []

    hogql = (
        f"SELECT properties.{SCREEN_NAME_PROPERTY} AS screen, count() AS views "
        f"FROM events "
        f"WHERE event = '{SCREEN_EVENT}' AND timestamp > now() - INTERVAL {days} DAY "
        f"GROUP BY screen ORDER BY views DESC LIMIT {limit}"
    )
    try:
        rows = _run_hogql(host, project_id, personal_key, hogql)
        return [{"screen": str(row[0]), "views": int(row[1])} for row in rows if row and row[0]]
    except Exception as exc:
        logger.error("top_screens query failed: %s", exc)
        return []


def retention_summary(host: str, project_id: str, personal_key: str) -> dict:
    """DAU/WAU/MAU + a 30-day daily-active series + an 8-week cohort grid.

    PostHog is the ONLY historical activity source: Firestore's last_active_at
    is a single overwritten scalar with no per-day history, so computing
    retention here adds ZERO Firestore reads by construction. Person counting
    uses person_id distincts. Empty when unconfigured or on error.
    """
    empty = {"dau": None, "wau": None, "mau": None, "daily": [], "cohorts": []}
    if not (personal_key and project_id):
        logger.warning("PostHog not configured; retention panel empty")
        return empty

    out = dict(empty)
    try:
        rows = _run_hogql(host, project_id, personal_key, (
            "SELECT toDate(timestamp) AS d, count(DISTINCT person_id) AS actives "
            "FROM events WHERE timestamp > now() - INTERVAL 30 DAY "
            "GROUP BY d ORDER BY d"
        ))
        out["daily"] = [{"day": str(r[0]), "actives": int(r[1])} for r in rows if r and r[0]]
    except Exception as exc:
        logger.error("retention daily series failed: %s", exc)

    for key, days in (("dau", 1), ("wau", 7), ("mau", 30)):
        try:
            rows = _run_hogql(host, project_id, personal_key, (
                "SELECT count(DISTINCT person_id) FROM events "
                f"WHERE timestamp > now() - INTERVAL {days} DAY"
            ))
            out[key] = int(rows[0][0]) if rows and rows[0] else 0
        except Exception as exc:
            logger.error("retention %s query failed: %s", key, exc)

    try:
        # Cohort = the week a person was first seen (within the 90d window);
        # cell = distinct persons from that cohort active N weeks later.
        rows = _run_hogql(host, project_id, personal_key, (
            "SELECT first_seen.w0 AS cohort_week, "
            "dateDiff('week', first_seen.w0, toStartOfWeek(e.timestamp)) AS week_offset, "
            "count(DISTINCT e.person_id) AS actives "
            "FROM events e INNER JOIN ("
            "  SELECT person_id, toStartOfWeek(min(timestamp)) AS w0 FROM events "
            "  WHERE timestamp > now() - INTERVAL 90 DAY GROUP BY person_id"
            ") AS first_seen ON e.person_id = first_seen.person_id "
            "WHERE e.timestamp > now() - INTERVAL 90 DAY AND week_offset >= 0 AND week_offset < 8 "
            "GROUP BY cohort_week, week_offset ORDER BY cohort_week, week_offset"
        ))
        out["cohorts"] = [
            {"cohort_week": str(r[0])[:10], "week": int(r[1]), "actives": int(r[2])}
            for r in rows if r and r[0] is not None
        ]
    except Exception as exc:
        logger.error("retention cohort query failed: %s", exc)

    return out


def notification_funnel(host: str, project_id: str, personal_key: str, days: int = 7) -> dict:
    """The signal-notification 4-step funnel. Event names mirror
    backend/src/services/analytics/funnel_events.py L21-24 exactly (that file is
    the single source of truth; test_funnel_event_contract.py guards the pair).
    """
    empty = {"sent": None, "tapped": None, "session": None, "action": None}
    if not (personal_key and project_id):
        logger.warning("PostHog not configured; notification funnel empty")
        return empty
    try:
        rows = _run_hogql(host, project_id, personal_key, (
            "SELECT "
            "countIf(event = 'signal_notification_sent'), "
            "countIf(event = 'notification_tapped' AND properties.notification_origin = 'signal_engine'), "
            "countIf(event = 'signal_session_from_notification'), "
            "countIf(event = 'signal_action_after_notification') "
            f"FROM events WHERE timestamp > now() - INTERVAL {int(days)} DAY"
        ))
        if rows and rows[0]:
            r = rows[0]
            return {"sent": int(r[0]), "tapped": int(r[1]), "session": int(r[2]), "action": int(r[3])}
    except Exception as exc:
        logger.error("notification funnel query failed: %s", exc)
    return empty


def paywall_funnel(host: str, project_id: str, personal_key: str, days: int = 30) -> dict:
    """Paywall interest funnel: paywall_viewed count, then paywall_intent taps
    broken out by tier + billing_period (writer: subscription_service.dart)."""
    empty: dict = {"viewed": None, "intents": []}
    if not (personal_key and project_id):
        logger.warning("PostHog not configured; paywall funnel empty")
        return empty
    out = dict(empty)
    try:
        rows = _run_hogql(host, project_id, personal_key, (
            "SELECT count() FROM events WHERE event = 'paywall_viewed' "
            f"AND timestamp > now() - INTERVAL {int(days)} DAY"
        ))
        out["viewed"] = int(rows[0][0]) if rows and rows[0] else 0
    except Exception as exc:
        logger.error("paywall viewed query failed: %s", exc)
    try:
        rows = _run_hogql(host, project_id, personal_key, (
            "SELECT properties.tier, properties.billing_period, count() "
            "FROM events WHERE event = 'paywall_intent' "
            f"AND timestamp > now() - INTERVAL {int(days)} DAY "
            "GROUP BY properties.tier, properties.billing_period ORDER BY 3 DESC"
        ))
        out["intents"] = [
            {"tier": str(r[0] or "?"), "period": str(r[1] or "?"), "count": int(r[2])}
            for r in rows if r
        ]
    except Exception as exc:
        logger.error("paywall intent query failed: %s", exc)
    return out


def chat_latency_percentiles(
    host: str, project_id: str, personal_key: str, days: int = 7, platform: str = "mobile"
) -> dict:
    """Client-observed chat end-to-end latency (the chat_e2e_latency event the
    app emits: ttft_ms = send -> first token, total_ms = send -> done). History
    only exists from the client build that ships the event onward; count=0 is
    rendered honestly by the UI, not as zero latency."""
    empty = {"count": 0, "ttft_p95": None, "total_p95": None, "total_p99": None}
    if not (personal_key and project_id):
        return empty
    try:
        rows = _run_hogql(host, project_id, personal_key, (
            "SELECT count(), "
            "quantile(0.95)(toFloat(properties.ttft_ms)), "
            "quantile(0.95)(toFloat(properties.total_ms)), "
            "quantile(0.99)(toFloat(properties.total_ms)) "
            "FROM events WHERE event = 'chat_e2e_latency' "
            f"{_platform_clause(platform)}"
            f"AND timestamp > now() - INTERVAL {int(days)} DAY"
        ))
        if rows and rows[0]:
            r = rows[0]

            def _ms(value):
                return round(float(value), 1) if isinstance(value, (int, float)) else None

            return {
                "count": int(r[0] or 0),
                "ttft_p95": _ms(r[1]),
                "total_p95": _ms(r[2]),
                "total_p99": _ms(r[3]),
            }
    except Exception as exc:
        logger.error("chat latency query failed (%s): %s", platform, exc)
    return empty


def voice_first_response_stats(
    host: str, project_id: str, personal_key: str, days: int = 7, platform: str = "mobile"
) -> dict:
    """voice_first_response occurrences + p95 of its elapsed_ms property.
    HONEST CAVEAT: the event historically carried NO properties (it marks that
    the agent spoke at all, once per session); elapsed_ms only exists from the
    client build that adds it, so p95 may be null while count is not."""
    empty = {"count": 0, "elapsed_p95": None}
    if not (personal_key and project_id):
        return empty
    try:
        rows = _run_hogql(host, project_id, personal_key, (
            "SELECT count(), quantile(0.95)(toFloat(properties.elapsed_ms)) "
            "FROM events WHERE event = 'voice_first_response' "
            f"{_platform_clause(platform)}"
            f"AND timestamp > now() - INTERVAL {int(days)} DAY"
        ))
        if rows and rows[0]:
            r = rows[0]
            p95 = round(float(r[1]), 1) if isinstance(r[1], (int, float)) else None
            return {"count": int(r[0] or 0), "elapsed_p95": p95}
    except Exception as exc:
        logger.error("voice_first_response query failed (%s): %s", platform, exc)
    return empty


def web_analytics(host: str, project_id: str, personal_key: str, days: int = 30) -> dict:
    """auravoiceapp.com marketing-site analytics (aura-web's posthog-js client).

    Uses aura-web's own event names (src/lib/analytics.ts): the Windows download
    funnel is download_page_viewed -> download_clicked, both already live on the
    site. project_id may be a DIFFERENT PostHog project than the app's (set
    OPS_POSTHOG_WEB_PROJECT_ID); the personal key must have read access to it.
    """
    empty = {
        "pageviews_daily": [], "top_referrers": [],
        "download_page_viewed": None, "download_clicked": None,
        "waitlist_submitted": None, "pricing_viewed": None,
    }
    if not (personal_key and project_id):
        logger.warning("PostHog not configured; web tab empty")
        return empty
    out = dict(empty)
    try:
        rows = _run_hogql(host, project_id, personal_key, (
            "SELECT toDate(timestamp) AS d, count() FROM events "
            f"WHERE event = '$pageview' AND timestamp > now() - INTERVAL {int(days)} DAY "
            "GROUP BY d ORDER BY d"
        ))
        out["pageviews_daily"] = [{"day": str(r[0]), "views": int(r[1])} for r in rows if r and r[0]]
    except Exception as exc:
        logger.error("web pageviews query failed: %s", exc)
    try:
        rows = _run_hogql(host, project_id, personal_key, (
            "SELECT properties.$referring_domain AS ref, count() FROM events "
            f"WHERE event = '$pageview' AND timestamp > now() - INTERVAL {int(days)} DAY "
            "AND ref IS NOT NULL AND ref != '$direct' "
            "GROUP BY ref ORDER BY 2 DESC LIMIT 10"
        ))
        out["top_referrers"] = [{"referrer": str(r[0]), "views": int(r[1])} for r in rows if r and r[0]]
    except Exception as exc:
        logger.error("web referrers query failed: %s", exc)
    try:
        rows = _run_hogql(host, project_id, personal_key, (
            "SELECT "
            "countIf(event = 'download_page_viewed'), "
            "countIf(event = 'download_clicked'), "
            "countIf(event = 'waitlist_submitted'), "
            "countIf(event = 'pricing_viewed') "
            f"FROM events WHERE timestamp > now() - INTERVAL {int(days)} DAY"
        ))
        if rows and rows[0]:
            r = rows[0]
            out["download_page_viewed"] = int(r[0])
            out["download_clicked"] = int(r[1])
            out["waitlist_submitted"] = int(r[2])
            out["pricing_viewed"] = int(r[3])
    except Exception as exc:
        logger.error("web funnel query failed: %s", exc)
    return out
