"""
Fetches cricket scores and match data using the free ESPN Cricinfo RSS feed
and the unofficial cricbuzz-python library (no API key required).
Used by MatchPoint.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx

from ...lib.logger import logger

# ESPN Cricinfo RSS — no auth required
CRICINFO_RSS_URL = "https://www.espncricinfo.com/rss/content/story/feeds/0.xml"


async def fetch_recent_results(limit: int = 5) -> list[dict]:
    """Fetches recent cricket match headlines from ESPN Cricinfo RSS."""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(CRICINFO_RSS_URL, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()

        root = ET.fromstring(r.text)
        items = root.findall(".//item")
        results = []
        for item in items[:limit]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            desc = (item.findtext("description") or "").strip()
            if title:
                results.append(
                    {
                        "headline": title,
                        "description": desc,
                        "url": link,
                        "source": "espncricinfo",
                    }
                )
        return results

    except Exception as e:
        logger.error("Cricket: failed to fetch RSS", {"error": str(e)})
        return []


async def fetch_live_matches() -> list[dict]:
    """
    Attempts to fetch live match info via cricbuzz-python.
    Falls back to empty list gracefully if the library is not installed.
    """
    try:
        import asyncio

        from cricbuzz_python import Cricbuzz  # type: ignore[import]

        cb = Cricbuzz()
        matches = await asyncio.to_thread(cb.get_matches)
        live = [m for m in (matches or []) if m.get("matchInfo", {}).get("state") == "Live"]
        return [
            {
                "match": m.get("matchInfo", {}).get("matchDesc", ""),
                "team1": m.get("matchInfo", {}).get("team1", {}).get("teamSName", ""),
                "team2": m.get("matchInfo", {}).get("team2", {}).get("teamSName", ""),
                "score": (
                    m.get("matchScore", {}).get("team1Score", {}).get("inngs1", {}).get("runs", "")
                ),
                "source": "cricbuzz",
            }
            for m in live
        ]
    except ImportError:
        logger.info("Cricket: cricbuzz-python not installed, skipping live match fetch")
        return []
    except Exception as e:
        logger.error("Cricket: failed to fetch live matches", {"error": str(e)})
        return []
