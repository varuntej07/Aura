"""Desktop download counts from the public GitHub Releases feed.

Aura-Desktop (the live Tauri Windows client) ships via GitHub Releases on
AuraVoice/Aura-Desktop (see ECOSYSTEM.md): each release carries .msi/.exe
installers whose per-asset download_count is the real install-download number.
latest.json + .sig files are updater plumbing, not user downloads, so they are
excluded from the counts.

Public repo, no credentials needed. The unauthenticated GitHub API allows 60
requests/hour per IP, so results are cached in-process for 15 minutes (the
dashboard polls far more often than release data changes). Set GITHUB_TOKEN to
lift the limit; not required at this scale. Fail-soft: any API failure serves
the last cached value if one exists, else an empty payload, never an exception.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger("ops.github")

_REPO = "AuraVoice/Aura-Desktop"
_CACHE_TTL_S = 900.0

_cache: dict[str, Any] | None = None
_cache_at: float = 0.0

_INSTALLER_SUFFIXES = (".msi", ".exe")


def _is_installer_asset(name: str) -> bool:
    lowered = name.lower()
    if lowered.endswith(".sig"):
        return False
    return lowered.endswith(_INSTALLER_SUFFIXES)


def desktop_downloads(github_token: str = "", repo: str = _REPO) -> dict[str, Any]:
    """Total + per-release installer download counts. Cached 15 min in-process."""
    global _cache, _cache_at
    now = time.monotonic()
    if _cache is not None and (now - _cache_at) < _CACHE_TTL_S:
        return _cache

    headers = {"Accept": "application/vnd.github+json"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    try:
        response = httpx.get(
            f"https://api.github.com/repos/{repo}/releases",
            params={"per_page": 20},
            headers=headers,
            timeout=15.0,
            follow_redirects=True,
        )
        response.raise_for_status()
        raw_releases = response.json()
    except Exception as exc:
        logger.error("GitHub releases query failed (serving cache if any): %s", exc)
        return _cache or {"total_downloads": 0, "latest_version": "", "releases": []}

    releases = []
    total = 0
    for release in raw_releases if isinstance(raw_releases, list) else []:
        assets = []
        release_downloads = 0
        for asset in release.get("assets", []) or []:
            name = str(asset.get("name") or "")
            if not _is_installer_asset(name):
                continue
            count = int(asset.get("download_count") or 0)
            release_downloads += count
            assets.append({"name": name, "downloads": count})
        total += release_downloads
        releases.append({
            "tag": str(release.get("tag_name") or ""),
            "name": str(release.get("name") or ""),
            "published_at": str(release.get("published_at") or ""),
            "downloads": release_downloads,
            "assets": assets,
        })

    result = {
        "total_downloads": total,
        "latest_version": releases[0]["tag"] if releases else "",
        "releases": releases,
    }
    _cache, _cache_at = result, now
    return result
