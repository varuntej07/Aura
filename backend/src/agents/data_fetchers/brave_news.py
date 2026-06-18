"""
Brave News fetcher — the signal-engine pool's reliable FALLBACK source.

WHY THIS EXISTS:
  newsdata.io is the PRIMARY general-news source and Google News RSS is a free
  best-effort top-up, but Google News returns 503 from datacenter (Cloud Run) IPs
  as a rule, and newsdata can exhaust its free-tier daily credits — so on a bad
  hour BOTH free sources return nothing and the shared pool drains until vector
  search returns [] for every user (the 2026-06-14 zero-notifications outage).
  Brave News is the datacenter-reliable paid backstop: a keyed API (no
  IP-reputation 503s) that returns DIRECT publisher URLs + a real publish date +
  source name + image, so it maps into the exact same candidate shape as newsdata.

COST DISCIPLINE — this fetcher is NOT run every hour.
  ``content_ingest.run_ingest`` calls it ONLY when the pool is below its
  fresh-content floor after the free sources, i.e. during an actual primary-source
  outage. Each Brave query costs ONE API credit, so the query set is bounded by
  ``_MAX_QUERIES_PER_RUN``. The free Brave tier (~2000 queries/month) therefore
  lasts for many outage-hours; a SUSTAINED outage is exactly when spending a credit
  to keep users served is worth it, and content_ingest raises a loud alarm so the
  underlying primary-source failure gets fixed rather than silently masked.

Returns the same dict shape as the newsdata fetcher (title, body, url, category,
sub_category, region, source_name, image_url, published_at) so
``content_ingest._map_brave_news`` can mirror ``_map_newsdata``. Never raises: a
missing key or one failing query is isolated so the rest still returns. Brave items
carry salience 0 (single-source, no cross-edition overlap signal), so like newsdata
they only ever flow through the personal lane, never the breaking lane.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from ...config.settings import settings
from ...lib.logger import logger

_BRAVE_NEWS_URL = "https://api.search.brave.com/res/v1/news/search"

# Per-request timeout. Brave is a fast JSON API; this is a fallback path (no user
# waiting on it), but every external wait is still bounded per the project doctrine.
_REQUEST_TIMEOUT_S = 8.0

# Concurrent Brave requests. Modest so a fallback burst stays well under Brave's
# rate limit (1 req/s on the free tier — the semaphore + the small spec count keep
# the whole run comfortably inside it).
_MAX_CONCURRENCY = 3

# Hard ceiling on Brave queries fired in ONE fallback run. Each query is 1 credit,
# so this is the per-run credit budget. Region-sensitive queries fire once per
# configured locale, region-agnostic ones once; the built list is then capped here.
_MAX_QUERIES_PER_RUN = 18

# Recency window. 'pd' = past day — aligns with the pool's ~24h TTL so the fallback
# refills with genuinely fresh news, not stale pages (also keeps the result set
# tight and the credit spend meaningful).
_FRESHNESS = "pd"

# Broad taxonomy coverage, region-SENSITIVE: fired once per configured locale and
# tagged with that locale's region so the scoring loop's soft region preference
# works (an India user gets the IN edition gently preferred). (query, category, sub).
_REGION_QUERIES: list[tuple[str, str, str]] = [
    ("top headlines today", "news", ""),
    ("business and economy news", "business", ""),
    ("sports news today", "sports", ""),
    ("entertainment and celebrity news", "entertainment", ""),
]

# Region-AGNOSTIC: fired once (primary locale's language), no region tag, so the
# scoring loop never penalises them by region. Tech/science/health read the same
# worldwide.
_GLOBAL_QUERIES: list[tuple[str, str, str]] = [
    ("technology news", "tech", ""),
    ("science news", "science", ""),
    ("health and wellness news", "health", ""),
    ("world politics news", "news", ""),
]

_DEFAULT_LIMIT_PER_QUERY = 15


@dataclass(frozen=True)
class _QuerySpec:
    query: str
    category: str
    sub_category: str
    country: str       # Brave lowercase 2-letter (us / in / gb)
    search_lang: str   # Brave language code (en)
    region: str        # uppercased region tag stored on the candidate ("" = agnostic)


def _build_query_specs() -> list[_QuerySpec]:
    """Ordered, credit-bounded Brave query list. Region-sensitive queries first
    (per locale), then region-agnostic, capped at ``_MAX_QUERIES_PER_RUN`` so one
    fallback run can never spend more than that many credits."""
    locales = settings.signal_news_locales or [("en", "US")]
    specs: list[_QuerySpec] = []
    for hl, gl in locales:
        for query, category, sub in _REGION_QUERIES:
            specs.append(_QuerySpec(
                query=query, category=category, sub_category=sub,
                country=gl.lower(), search_lang=hl.lower(), region=gl.upper(),
            ))
    primary_hl, primary_gl = locales[0]
    for query, category, sub in _GLOBAL_QUERIES:
        specs.append(_QuerySpec(
            query=query, category=category, sub_category=sub,
            country=primary_gl.lower(), search_lang=primary_hl.lower(), region="",
        ))
    return specs[:_MAX_QUERIES_PER_RUN]


def _parse_page_age(raw: Any) -> datetime | None:
    """Brave's page_age is ISO-8601 like '2026-06-14T07:38:58' (UTC, no offset).
    Returns tz-aware UTC, or None so the pool falls back to ingest time."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _item_body(description: str, extra_snippets: Any) -> str:
    """Join the description with any extra snippets into one body blob, so a thin
    Brave description still clears the pool's push-substance floor when snippets
    carry the detail."""
    parts = [str(description or "").strip()]
    if isinstance(extra_snippets, list):
        parts.extend(str(s).strip() for s in extra_snippets if str(s).strip())
    return " ".join(p for p in parts if p).strip()


async def fetch_brave_news(
    *,
    limit_per_query: int = _DEFAULT_LIMIT_PER_QUERY,
) -> list[dict[str, Any]]:
    """Fetch fresh news across the taxonomy from Brave's News API. Returns the
    newsdata-compatible dict shape; de-duplicated by normalised title. Never raises
    and returns [] when the key is unset or every query fails."""
    if not settings.BRAVE_API_KEY:
        logger.info("brave_news: BRAVE_API_KEY unset, skipping (fallback unavailable)")
        return []

    specs = _build_query_specs()
    headers = {"X-Subscription-Token": settings.BRAVE_API_KEY, "Accept": "application/json"}
    semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def _fetch_one(client: httpx.AsyncClient, spec: _QuerySpec) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "q": spec.query,
            "count": limit_per_query,
            "country": spec.country,
            "search_lang": spec.search_lang,
            "freshness": _FRESHNESS,
            "spellcheck": 0,
        }
        async with semaphore:
            try:
                resp = await client.get(_BRAVE_NEWS_URL, params=params, headers=headers)
                resp.raise_for_status()
                payload = resp.json() if resp.content else {}
            except Exception as exc:
                logger.warn("brave_news: query failed", {"query": spec.query, "error": str(exc)})
                return []

        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return []
        out: list[dict[str, Any]] = []
        for entry in results[:limit_per_query]:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title") or "").strip()
            url = str(entry.get("url") or "").strip()
            if not title or not url:
                continue
            profile = entry.get("profile") if isinstance(entry.get("profile"), dict) else {}
            thumbnail = entry.get("thumbnail") if isinstance(entry.get("thumbnail"), dict) else {}
            out.append({
                "title": title,
                "body": _item_body(entry.get("description", ""), entry.get("extra_snippets")),
                "url": url,
                "category": spec.category,
                "sub_category": spec.sub_category,
                "region": spec.region,
                "source_name": str((profile or {}).get("name") or "").strip(),
                "image_url": str((thumbnail or {}).get("src") or "").strip(),
                "published_at": _parse_page_age(entry.get("page_age")),
            })
        return out

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_S, follow_redirects=True) as client:
            per_query = await asyncio.gather(*[_fetch_one(client, spec) for spec in specs])
    except Exception as exc:
        logger.warn("brave_news: fetch batch failed", {"error": str(exc)})
        return []

    items: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for query_items in per_query:
        for item in query_items:
            key = item["title"].strip().lower()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            items.append(item)

    logger.info("brave_news: fetched", {"items": len(items), "queries": len(specs)})
    return items
