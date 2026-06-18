"""
RSS news client — fetches relevant headlines for the daily notification planner.

Used as a fallback when the user's query history doesn't have enough signal,
and as enrichment context the planner can reference even when signal is strong.

Three-level fallback chain (always returns at least one item):
  Level 1: Google News RSS queried with user-derived topic keywords
  Level 2: If < 2 results, broaden to generic "health fitness wellness nutrition"
  Level 3: If still empty, pull from static curated health/wellness feeds
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote_plus

from ...lib.logger import logger

# Static curated feeds used only if Google News returns nothing at all
_FALLBACK_FEED_URLS = [
    "https://www.healthline.com/rss/health-news",
    "https://www.medicalnewstoday.com/rss",
]

_GOOGLE_NEWS_RSS_URL = (
    "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
)

_BROAD_HEALTH_QUERY = "health fitness wellness nutrition"
_MIN_USABLE_RESULTS = 2
_MAX_RESULTS_TO_RETURN = 5


class NewsItem:
    """A single news headline with its summary and publish date."""

    def __init__(self, title: str, summary: str, published_at: str) -> None:
        self.title = title
        self.summary = summary
        self.published_at = published_at

    def to_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "summary": self.summary,
            "published_at": self.published_at,
        }


async def fetch_news(topic_keywords: list[str]) -> list[dict[str, str]]:
    """Fetch relevant news headlines.

    Args:
        topic_keywords: List of topic strings derived from user's query history.
                        e.g. ["protein", "weight loss"] or ["sleep", "stress"]
                        Pass an empty list to use broad health/wellness query.

    Returns:
        List of dicts with keys: title, summary, published_at.
        Always returns at least one item (falls back through all three levels).
    """
    return await asyncio.to_thread(_fetch_news_sync, topic_keywords)


def _fetch_news_sync(topic_keywords: list[str]) -> list[dict[str, str]]:
    try:
        import feedparser  # type: ignore
    except ImportError:
        logger.warn("rss_client: feedparser not installed, returning empty news")
        return [_empty_news_item()]

    # Level 1: user-specific keywords
    if topic_keywords:
        query = " ".join(topic_keywords[:3])  # cap at 3 keywords for clean RSS query
        items = _fetch_from_google_news(feedparser, query)
        if len(items) >= _MIN_USABLE_RESULTS:
            return [item.to_dict() for item in items[:_MAX_RESULTS_TO_RETURN]]

    # Level 2: broaden to generic health/wellness
    items = _fetch_from_google_news(feedparser, _BROAD_HEALTH_QUERY)
    if len(items) >= _MIN_USABLE_RESULTS:
        return [item.to_dict() for item in items[:_MAX_RESULTS_TO_RETURN]]

    # Level 3: static curated feeds
    for feed_url in _FALLBACK_FEED_URLS:
        try:
            feed = feedparser.parse(feed_url)
            items = _parse_feed_entries(feed.entries)
            if items:
                return [item.to_dict() for item in items[:_MAX_RESULTS_TO_RETURN]]
        except Exception as exc:
            logger.warn("rss_client: fallback feed failed", {"url": feed_url, "error": str(exc)})

    # If everything fails, return a placeholder so the planner always has something
    logger.warn("rss_client: all levels failed, using placeholder item")
    return [_empty_news_item()]


def _fetch_from_google_news(feedparser: Any, query: str) -> list[NewsItem]:
    url = _GOOGLE_NEWS_RSS_URL.format(query=quote_plus(query))
    try:
        feed = feedparser.parse(url)
        return _parse_feed_entries(feed.entries)
    except Exception as exc:
        logger.warn("rss_client: google news fetch failed", {"query": query, "error": str(exc)})
        return []


def _parse_feed_entries(entries: list[Any]) -> list[NewsItem]:
    items: list[NewsItem] = []
    for entry in entries:
        title: str = getattr(entry, "title", "").strip()
        summary: str = getattr(entry, "summary", "").strip()
        published: str = _parse_published(entry)
        if title:
            items.append(NewsItem(title=title, summary=summary, published_at=published))
    return items


def _parse_published(entry: Any) -> str:
    """Extract a human-readable publish date, defaulting to today."""
    try:
        t = getattr(entry, "published_parsed", None)
        if t:
            dt = datetime(*t[:6], tzinfo=UTC)
            return dt.strftime("%B %d, %Y")
    except Exception:
        pass
    return datetime.now(UTC).strftime("%B %d, %Y")


def _empty_news_item() -> dict[str, str]:
    return {
        "title": "New research on nutrition and daily habits",
        "summary": "Emerging studies highlight the connection between consistent daily habits and long-term health outcomes.",
        "published_at": datetime.now(UTC).strftime("%B %d, %Y"),
    }
