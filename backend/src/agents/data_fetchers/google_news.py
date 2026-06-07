"""
Google News RSS fetcher — free, unlimited global headlines for the content pool.

Google News exposes topic and keyword feeds as plain RSS (no API key, no quota):
  topic:   https://news.google.com/rss/headlines/section/topic/<TOPIC>?hl=en-US&gl=US&ceid=US:en
  search:  https://news.google.com/rss/search?q=<query>&hl=en-US&gl=US&ceid=US:en

The signal engine only needs title + body + url to embed and score, so raw RSS
items are a perfect (and free) replacement for the paid Gemini-grounded search
that previously fed the pool. Each feed carries a content-pool category so the
recommender's category affinities keep working.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import quote_plus

from ...lib.logger import logger

_TOPIC_FEED_URL = (
    "https://news.google.com/rss/headlines/section/topic/{topic}?hl=en-US&gl=US&ceid=US:en"
)
_SEARCH_FEED_URL = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

# (Google News topic, content-pool category). Topics are free, broad, and fresh.
_TOPIC_FEEDS: list[tuple[str, str]] = [
    ("WORLD", "news"),
    ("BUSINESS", "business"),
    ("TECHNOLOGY", "tech"),
    ("SPORTS", "sports"),
    ("SCIENCE", "science"),
]

# (search query, category, sub_category) for league-specific coverage the broad
# topic feeds don't surface on their own.
_KEYWORD_FEEDS: list[tuple[str, str, str]] = [
    ("IPL cricket", "sports", "ipl"),
    ("Premier League football", "sports", "premier_league"),
    ("NBA basketball", "sports", "nba"),
]

_HTML_TAG = re.compile(r"<[^>]+>")
_DEFAULT_LIMIT_PER_FEED = 8


def _clean(text: str) -> str:
    return _HTML_TAG.sub("", text or "").strip()


async def fetch_google_news(limit_per_feed: int = _DEFAULT_LIMIT_PER_FEED) -> list[dict[str, Any]]:
    """Fetch global headlines across topic + keyword feeds. Free, no API key.

    Returns dicts with: title, body, url, category, sub_category. Never raises —
    a failing feed is skipped so one bad source never blocks the rest."""
    return await asyncio.to_thread(_fetch_sync, limit_per_feed)


def _fetch_sync(limit_per_feed: int) -> list[dict[str, Any]]:
    try:
        import feedparser  # type: ignore
    except ImportError:
        logger.warn("google_news: feedparser not installed — returning no items")
        return []

    feeds: list[tuple[str, str, str]] = [
        (_TOPIC_FEED_URL.format(topic=topic), category, "")
        for topic, category in _TOPIC_FEEDS
    ] + [
        (_SEARCH_FEED_URL.format(query=quote_plus(query)), category, sub_category)
        for query, category, sub_category in _KEYWORD_FEEDS
    ]

    items: list[dict[str, Any]] = []
    seen_titles: set[str] = set()

    for url, category, sub_category in feeds:
        try:
            feed = feedparser.parse(url)
        except Exception as exc:
            logger.warn("google_news: feed parse failed", {"url": url, "error": str(exc)})
            continue
        for entry in (feed.entries or [])[:limit_per_feed]:
            title = _clean(getattr(entry, "title", ""))
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            items.append({
                "title": title,
                "body": _clean(getattr(entry, "summary", "")),
                "url": str(getattr(entry, "link", "")).strip(),
                "category": category,
                "sub_category": sub_category,
            })

    logger.info("google_news: fetched", {"items": len(items), "feeds": len(feeds)})
    return items
