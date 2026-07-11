"""Desktop crash feed from the Sentry Issues API (Aura-Desktop, Tauri).

Aura-Desktop reports native Rust panics + webview JS errors to Sentry via
tauri-plugin-sentry (see ECOSYSTEM.md for the shared Sentry identity). This
reads the grouped, deduped issue list and maps it into the SAME crash-feed row
shape the Crashlytics panel uses, so the Mobile and Desktop tabs render one
component.

Needs SENTRY_ORG / SENTRY_PROJECT / SENTRY_AUTH_TOKEN (token scopes:
project:read + event:read). Unconfigured -> configured=false; any API failure
-> empty rows plus a log line, never an exception.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("ops.sentry")


def desktop_crashes(
    org: str,
    project: str,
    auth_token: str,
    stats_period: str = "14d",
    limit: int = 25,
) -> dict[str, Any]:
    """Unresolved issues sorted by frequency, in the shared crash-feed shape:
    {title, subtitle, events, users, last_seen, os, app_version, level}."""
    if not (org and project and auth_token):
        logger.warning("Sentry not configured (SENTRY_ORG/SENTRY_PROJECT/SENTRY_AUTH_TOKEN); desktop crash panel empty")
        return {"configured": False, "crashes": []}

    out: dict[str, Any] = {"configured": True, "crashes": []}
    try:
        response = httpx.get(
            f"https://sentry.io/api/0/projects/{org}/{project}/issues/",
            params={
                "query": "is:unresolved",
                "statsPeriod": stats_period,
                "sort": "freq",
                "limit": limit,
            },
            headers={"Authorization": f"Bearer {auth_token}"},
            timeout=20.0,
            follow_redirects=True,
        )
        response.raise_for_status()
        issues = response.json()
    except Exception as exc:
        logger.error("Sentry issues query failed: %s", exc)
        return out

    crashes = []
    for issue in issues if isinstance(issues, list) else []:
        metadata = issue.get("metadata") or {}
        crashes.append({
            "title": str(issue.get("title") or metadata.get("type") or ""),
            "subtitle": str(issue.get("culprit") or metadata.get("value") or "")[:200],
            "events": int(issue.get("count") or 0),
            "users": int(issue.get("userCount") or 0),
            "last_seen": str(issue.get("lastSeen") or ""),
            "os": "windows",
            "app_version": "",
            "level": str(issue.get("level") or ""),
            "permalink": str(issue.get("permalink") or ""),
        })
    out["crashes"] = crashes
    return out
