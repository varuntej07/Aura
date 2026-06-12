"""
Google News RSS fetcher — free, unlimited, multi-region headlines for the pool.

Google News exposes topic and keyword feeds as plain RSS (no API key, no quota):
  topic:   https://news.google.com/rss/headlines/section/topic/<TOPIC>?hl=<hl>&gl=<gl>&ceid=<gl>:<hl>
  search:  https://news.google.com/rss/search?q=<query>&hl=<hl>&gl=<gl>&ceid=<gl>:<hl>

The signal engine only needs title + body + url to embed and score, so raw RSS
items are a perfect (and free) replacement for the paid Gemini-grounded search
that previously fed the pool.

De-biasing the world: the feed used to hardcode hl=en-US&gl=US, so a non-US user
could never get equally good content. Region-sensitive topics (WORLD, NATION,
BUSINESS, SPORTS, ENTERTAINMENT, HEALTH) are now fetched per locale edition
(settings.signal_news_locales) and each item is tagged with its region ("US" |
"IN" | "GB"); the scoring loop uses that as a soft preference, never a hard
filter, so a global story still reaches everyone. Region-agnostic topics
(TECHNOLOGY, SCIENCE) are fetched once.

Each feed carries a content-pool category (later normalised to a taxonomy slug at
the pool write choke point), so the recommender's category affinities keep working.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote_plus

from ...config.settings import settings
from ...lib.logger import logger

_TOPIC_FEED_URL = (
    "https://news.google.com/rss/headlines/section/topic/{topic}"
    "?hl={hl}&gl={gl}&ceid={gl}:{hl}"
)
_SEARCH_FEED_URL = (
    "https://news.google.com/rss/search?q={query}&hl={hl}&gl={gl}&ceid={gl}:{hl}"
)

# Region-sensitive topics: fetched once PER locale edition and tagged with that
# edition's region, so the pool carries IN/GB/US variants instead of US-only.
# (Google News topic, content-pool category).
_TOPIC_FEEDS: list[tuple[str, str]] = [
    ("WORLD", "news"),
    ("NATION", "nation"),
    ("BUSINESS", "business"),
    ("SPORTS", "sports"),
    ("ENTERTAINMENT", "entertainment"),
    ("HEALTH", "health"),
]

# Region-agnostic topics: fetched once (no locale variation worth the extra
# feeds), region left blank.
_GLOBAL_TOPIC_FEEDS: list[tuple[str, str]] = [
    ("TECHNOLOGY", "tech"),
    ("SCIENCE", "science"),
]

# (search query, category, sub_category) for league-specific coverage the broad
# topic feeds don't surface on their own. Region-agnostic.
_KEYWORD_FEEDS: list[tuple[str, str, str]] = [
    ("IPL cricket", "sports", "ipl"),
    ("Premier League football", "sports", "premier_league"),
    ("NBA basketball", "sports", "nba"),
]

_HTML_TAG = re.compile(r"<[^>]+>")
_DEFAULT_LIMIT_PER_FEED = 8

# Cap on concurrent feed parses so 20+ feeds don't run sequentially for ~20s.
_MAX_FEED_WORKERS = 8


def _clean(text: str) -> str:
    return _HTML_TAG.sub("", text or "").strip()


def _overlap_key(title: str) -> str:
    """Normalised headline used to detect the SAME story across locale editions.

    Google News titles are usually 'Headline - Publisher'; different editions of
    one event often carry the same headline under different publishers, so we drop
    the trailing ' - Publisher', lowercase, and collapse whitespace. This is an
    exact-ish match (high precision, low recall): when two editions agree on a
    headline it is almost certainly the same story, which is what the salience
    signal needs (see services/signal_engine/salience.py)."""
    base = title.rsplit(" - ", 1)[0] if " - " in title else title
    return re.sub(r"\s+", " ", base).strip().lower()


async def fetch_google_news(limit_per_feed: int = _DEFAULT_LIMIT_PER_FEED) -> list[dict[str, Any]]:
    """Fetch multi-region headlines across topic + keyword feeds. Free, no API key.

    Returns dicts with: title, body, url, category, sub_category, region. Never
    raises — a failing feed is skipped so one bad source never blocks the rest."""
    import asyncio

    return await asyncio.to_thread(_fetch_sync, limit_per_feed)


def _build_feed_specs() -> list[tuple[str, str, str, str, bool]]:
    """Build the ordered (url, category, sub_category, region, is_world) feed list.

    Order is deterministic and region-sensitive feeds come first so the cross-feed
    overlap counting keeps the localised variant of a shared headline as the kept
    item. ``is_world`` marks the global WORLD section, which feeds the salience
    signal (a WORLD-section lead story is the edition's most important news).
    """
    specs: list[tuple[str, str, str, str, bool]] = []
    for hl, gl in settings.signal_news_locales:
        for topic, category in _TOPIC_FEEDS:
            specs.append(
                (_TOPIC_FEED_URL.format(topic=topic, hl=hl, gl=gl), category, "", gl, topic == "WORLD")
            )
    # Region-agnostic topic + keyword feeds use the first configured locale's
    # language but carry no region tag and are never the WORLD section.
    default_hl, default_gl = settings.signal_news_locales[0]
    for topic, category in _GLOBAL_TOPIC_FEEDS:
        specs.append(
            (_TOPIC_FEED_URL.format(topic=topic, hl=default_hl, gl=default_gl), category, "", "", False)
        )
    for query, category, sub_category in _KEYWORD_FEEDS:
        specs.append((
            _SEARCH_FEED_URL.format(query=quote_plus(query), hl=default_hl, gl=default_gl),
            category,
            sub_category,
            "",
            False,
        ))
    return specs


def _fetch_sync(limit_per_feed: int) -> list[dict[str, Any]]:
    try:
        import feedparser  # type: ignore
    except ImportError:
        logger.warn("google_news: feedparser not installed — returning no items")
        return []

    specs = _build_feed_specs()

    def _parse_one(spec: tuple[str, str, str, str, bool]) -> list[dict[str, Any]]:
        url, category, sub_category, region, is_world = spec
        try:
            feed = feedparser.parse(url)
        except Exception as exc:
            logger.warn("google_news: feed parse failed", {"url": url, "error": str(exc)})
            return []
        out: list[dict[str, Any]] = []
        for rank, entry in enumerate((feed.entries or [])[:limit_per_feed]):
            title = _clean(getattr(entry, "title", ""))
            if not title:
                continue
            out.append({
                "title": title,
                "body": _clean(getattr(entry, "summary", "")),
                "url": str(getattr(entry, "link", "")).strip(),
                "category": category,
                "sub_category": sub_category,
                "region": region,
                # feed_rank (0 = lead story) and is_world feed the salience score;
                # edition_count is filled during the cross-feed merge below.
                "feed_rank": rank,
                "is_world": is_world,
            })
        return out

    # Parse feeds concurrently (feedparser is blocking I/O), but merge results in
    # the original spec order so the kept item for a shared headline is deterministic.
    with ThreadPoolExecutor(max_workers=_MAX_FEED_WORKERS) as pool:
        per_feed = list(pool.map(_parse_one, specs))

    # Cross-feed merge that COUNTS edition overlap instead of discarding it. A
    # headline appearing across multiple locale editions (US/IN/GB) is, by
    # construction, globally important — that count (edition_count) is the free
    # salience signal the breaking lane runs on (see services/signal_engine/salience.py).
    items: list[dict[str, Any]] = []
    index_by_key: dict[str, int] = {}
    regions_by_key: dict[str, set[str]] = {}
    for feed_items in per_feed:
        for item in feed_items:
            key = _overlap_key(item["title"])
            region = item["region"]
            if key in index_by_key:
                kept = items[index_by_key[key]]
                if region:
                    regions_by_key[key].add(region)
                kept["edition_count"] = max(1, len(regions_by_key[key]))
                kept["feed_rank"] = min(kept["feed_rank"], item["feed_rank"])
                kept["is_world"] = kept["is_world"] or item["is_world"]
                continue
            regions_by_key[key] = {region} if region else set()
            item["edition_count"] = max(1, len(regions_by_key[key]))
            items.append(item)
            index_by_key[key] = len(items) - 1

    logger.info("google_news: fetched", {"items": len(items), "feeds": len(specs)})
    return items
