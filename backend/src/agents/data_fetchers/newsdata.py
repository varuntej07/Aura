"""
newsdata.io fetcher — free-tier general-news source for the signal-engine pool.

WHY THIS EXISTS (vs Google News RSS, which also feeds the pool):
  * Google News RSS gives a `news.google.com/rss/articles/…` REDIRECT wrapper as
    the link, so the "read" notification tap lands on a Google interstitial, not
    the real article. newsdata returns the DIRECT publisher URL + source name, so
    the citation a user taps is the actual story.
  * Google News stays as the real-time + cross-edition-overlap (salience) source;
    newsdata is the citation-quality general-news source.

FREE-TIER CONSTRAINTS (https://newsdata.io/blog/newsdata-rate-limit/):
  * 200 API credits/day, 1 credit ≈ 10 articles, 30 credits/15 min.
  * News is ~12h delayed on the free plan — fine for personalised "did you hear
    about X" content, which is why newsdata never powers the time-critical
    BREAKING lane (that runs on real-time Google News overlap).

This module never raises: a missing key, a quota 429, or one bad category is
isolated so the rest of ingest is unaffected (Google News alone keeps the pool
fed). Per CLAUDE.md httpx rule, follow_redirects=True is set explicitly.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from ...config.settings import settings
from ...lib.logger import logger

# newsdata's own category vocabulary -> the pool's source-category vocab (which
# content_category_map then normalises to a closed taxonomy slug). Only the
# categories we actually request (settings.NEWSDATA_CATEGORIES) need to be here;
# anything unexpected falls back to "news" (a producible general bucket) so a
# surprise category is never silently dropped or coerced to the muted "other".
_CATEGORY_TO_SOURCE_VOCAB: dict[str, str] = {
    "top": "news",
    "world": "news",
    "politics": "news",
    "business": "business",
    "technology": "tech",
    "science": "science",
    "environment": "science",
    "sports": "sports",
    "entertainment": "entertainment",
    "health": "health",
}

# newsdata returns full country names; map the ones our locale editions cover to
# the region codes the scoring loop uses for its soft region preference. Unknown
# countries map to "" (region-agnostic — never penalised).
_COUNTRY_TO_REGION: dict[str, str] = {
    "united states of america": "US",
    "united states": "US",
    "usa": "US",
    "india": "IN",
    "united kingdom": "GB",
}

_HTML_TAG = re.compile(r"<[^>]+>")

# Per-request timeout. Each category is one HTTP call.
_REQUEST_TIMEOUT_S = 15.0


def _clean(text: str) -> str:
    return _HTML_TAG.sub("", text or "").strip()


def _to_source_category(raw_categories: Any) -> str:
    """First newsdata category mapped to source vocab; 'news' when unknown/empty."""
    if isinstance(raw_categories, list) and raw_categories:
        first = str(raw_categories[0]).strip().lower()
    else:
        first = str(raw_categories or "").strip().lower()
    return _CATEGORY_TO_SOURCE_VOCAB.get(first, "news")


def _to_region(raw_country: Any) -> str:
    """First recognised country mapped to a region code; '' when unknown/empty."""
    countries = raw_country if isinstance(raw_country, list) else [raw_country]
    for c in countries:
        region = _COUNTRY_TO_REGION.get(str(c or "").strip().lower())
        if region:
            return region
    return ""


def _parse_pub_date(raw: Any) -> datetime | None:
    """newsdata pubDate is 'YYYY-MM-DD HH:MM:SS' (UTC). Returns tz-aware UTC or
    None so the pool falls back to ingest time for freshness."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


async def fetch_newsdata_articles(
    *,
    categories: list[str] | None = None,
    limit_per_category: int = 10,
) -> list[dict[str, Any]]:
    """Fetch latest articles per category from newsdata.io.

    Returns dicts with: title, body, url (DIRECT publisher link), category
    (source vocab), sub_category, region, source_name, image_url, published_at.
    Cross-category de-duplicated by normalised title. Never raises and returns
    an empty list when the key is unset or every call fails (Google News then
    carries the pool)."""
    if not settings.newsdata_configured:
        logger.info("newsdata: NEWSDATA_API_KEY unset — skipping (Google News carries the pool)")
        return []

    cats = categories or settings.newsdata_categories
    if not cats:
        return []

    api_key = settings.NEWSDATA_API_KEY.strip()
    base_url = settings.NEWSDATA_BASE_URL
    language = settings.NEWSDATA_LANGUAGE.strip() or "en"

    async def _fetch_category(client: httpx.AsyncClient, category: str) -> list[dict[str, Any]]:
        params = {
            "apikey": api_key,
            "category": category,
            "language": language,
        }
        try:
            resp = await client.get(base_url, params=params)
        except Exception as exc:
            logger.warn("newsdata: request failed", {"category": category, "error": str(exc)})
            return []

        if resp.status_code == 429:
            # Free-tier quota / rate limit hit. Fail open — return nothing for this
            # call; the rest of ingest (and Google News) proceeds. Loud so an
            # exhausted key is visible, not a silent empty pool.
            logger.warn(
                "newsdata: 429 rate-limited / quota exhausted — skipping this fetch "
                "(Google News still feeds the pool). Check the free-tier 200 credits/day cap.",
                {"category": category},
            )
            return []
        try:
            resp.raise_for_status()
        except Exception as exc:
            logger.warn("newsdata: non-200 response", {
                "category": category,
                "status": resp.status_code,
                "error": str(exc),
            })
            return []

        payload = resp.json() if resp.content else {}
        if not isinstance(payload, dict) or payload.get("status") != "success":
            logger.warn("newsdata: unexpected payload", {
                "category": category,
                "status_field": (payload or {}).get("status") if isinstance(payload, dict) else None,
            })
            return []

        results = payload.get("results") or []
        out: list[dict[str, Any]] = []
        for item in results[:limit_per_category]:
            if not isinstance(item, dict):
                continue
            title = _clean(str(item.get("title", "")))
            url = str(item.get("link", "")).strip()
            if not title or not url:
                continue
            out.append({
                "title": title,
                "body": _clean(str(item.get("description", ""))),
                "url": url,
                "category": _to_source_category(item.get("category")),
                "sub_category": "",
                "region": _to_region(item.get("country")),
                "source_name": str(item.get("source_name") or item.get("source_id") or "").strip(),
                "image_url": str(item.get("image_url") or "").strip(),
                "published_at": _parse_pub_date(item.get("pubDate")),
            })
        return out

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_S, follow_redirects=True) as client:
            per_category = await asyncio.gather(*[_fetch_category(client, c) for c in cats])
    except Exception as exc:
        logger.warn("newsdata: fetch batch failed", {"error": str(exc)})
        return []

    items: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for cat_items in per_category:
        for item in cat_items:
            key = item["title"].lower()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            items.append(item)

    logger.info("newsdata: fetched", {"items": len(items), "categories": len(cats)})
    return items
