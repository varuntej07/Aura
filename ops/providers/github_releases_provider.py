"""Installer FETCH counts from the public GitHub Releases feed.

Aura-Desktop ships via GitHub Releases on AuraVoice/Aura-Desktop (see
ECOSYSTEM.md): .msi/.exe for Windows and a notarized universal .dmg for macOS
since 0.13.2, plus latest.json and .sig updater plumbing.

READ THE NUMBER THIS RETURNS CORRECTLY. GitHub's per-asset `download_count` is a
raw HTTP counter, not an install count, and it cannot be turned into one from
this API:

  - crawlers, mirrors and security scanners fetch release assets;
  - the Tauri updater re-fetches the SAME .msi on every auto-update of every
    EXISTING install, so a single happy user generates a download per release;
  - a fetch that is never run, or is run and never signed into, still counts.

Excluding latest.json and .sig removes only the obvious plumbing; it does NOT
make the remainder human. The dashboard therefore labels this "installer
fetches" and gets its real adoption number from
`firestore_provider.desktop_installs` (one doc per installation that actually
reached a signed-in state). Presenting this count as "downloads" next to zero
users is exactly the confusion that motivated the split.

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

# .dmg included deliberately: Aura-Desktop has shipped a notarized universal
# macOS build since 0.13.2 (ECOSYSTEM.md), and counting only .msi/.exe silently
# dropped every macOS fetch from the total.
_INSTALLER_SUFFIXES = (".msi", ".exe", ".dmg")

_CAVEAT = (
    "Raw HTTP fetches of release assets. Includes crawlers, scanners and every "
    "Tauri auto-update re-fetch by existing installs, so it is an upper bound on "
    "interest, never an install count."
)


def _is_installer_asset(name: str) -> bool:
    """True for a user-facing installer asset, false for updater plumbing.

    This is a filename check, which is legitimate here: it classifies an asset
    NAME the release process produces, not anything a person said or meant.
    """
    lowered = name.lower()
    if lowered.endswith(".sig") or lowered == "latest.json":
        return False
    return lowered.endswith(_INSTALLER_SUFFIXES)


def desktop_downloads(github_token: str = "", repo: str = _REPO) -> dict[str, Any]:
    """Total + per-release installer FETCH counts. Cached 15 min in-process.

    See the module docstring: these are HTTP fetches, not installs. The payload
    carries its own caveat text so no caller can render the number bare.
    """
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
        return _cache or {
            "available": False,
            "installer_fetches": 0,
            "total_downloads": 0,
            "latest_version": "",
            "releases": [],
            "caveat": _CAVEAT,
        }

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
        "available": True,
        "installer_fetches": total,
        # Kept as an alias so an older cached browser payload keeps rendering
        # through a deploy; the UI reads installer_fetches.
        "total_downloads": total,
        "latest_version": releases[0]["tag"] if releases else "",
        "releases": releases,
        "caveat": _CAVEAT,
    }
    _cache, _cache_at = result, now
    return result
