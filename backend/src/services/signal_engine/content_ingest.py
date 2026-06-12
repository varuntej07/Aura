"""
Content ingest — one entry point that fills the shared content pool.

  run_ingest()   hourly (Cloud Scheduler) — Google News RSS + newsdata.io

Two free general-news sources feed ONE shared pool; personalisation happens later
at scoring time, not here:
  * Google News RSS — real-time, multi-region. Its cross-edition overlap is the
    free GLOBAL SALIENCE signal that powers the breaking lane (a headline carried
    by every locale edition is, by construction, worldwide news).
  * newsdata.io — ~12h delayed on the free tier, but returns DIRECT publisher URLs
    (Google News only gives redirect wrappers), so the "read" notification tap
    lands on the real article. Personal-lane only; never breaking (it's delayed).

This replaced an earlier mix that also pulled Hacker News, arXiv, ESPN Cricinfo
RSS and cricbuzz live scores — all personal/niche sources from when the app was a
single-user tech/cricket tool, irrelevant to a general global userbase. Sports now
arrives via the region-aware Google News + newsdata SPORTS category, so it surfaces
cricket for an India user and NFL/football for others, instead of cricket for all.

newsdata is gated to even UTC hours so the free-tier 200 credits/day cap is safe
even if the Cloud Scheduler job stays hourly (~8 categories × 12 fetches ≈ 96/day).
Google News (free, unlimited) runs every ingest. Fetcher failures are isolated;
one bad source never blocks the rest. The embedder de-dups by content_id so
re-running early is cheap.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from ...agents.data_fetchers.google_news import fetch_google_news
from ...agents.data_fetchers.newsdata import fetch_newsdata_articles
from ...lib.logger import logger
from ..model_provider import is_quota_exhausted
from .content_pool import CandidateInput, add_candidates
from .salience import compute_salience

GOOGLE_NEWS_LIMIT_PER_FEED = 8
NEWSDATA_LIMIT_PER_CATEGORY = 10

# Per-source TTL in hours.
SOURCE_TTL_HOURS: dict[str, int] = {
    "google_news": 24,   # global headlines stay relevant ~a day
    "newsdata": 24,      # 12h-delayed already; a day of pool life is plenty
}


@dataclass
class IngestSummary:
    google_news_fetched: int = 0
    newsdata_fetched: int = 0
    total_written: int = 0


async def run_ingest(*, now: datetime | None = None) -> IngestSummary:
    """Hourly: fetch Google News RSS (always) + newsdata.io (even UTC hours), map
    to candidates with a global-salience score, de-dup across sources, embed+write."""
    summary = IngestSummary()
    now = now or datetime.now(UTC)

    # newsdata is gated to even hours to stay under the free-tier daily credit cap
    # regardless of the scheduler cadence; Google News (free) runs every time.
    fetch_newsdata = now.hour % 2 == 0

    news, newsdata = await asyncio.gather(
        _safe_fetch(fetch_google_news(limit_per_feed=GOOGLE_NEWS_LIMIT_PER_FEED), "google_news"),
        _safe_fetch(
            fetch_newsdata_articles(limit_per_category=NEWSDATA_LIMIT_PER_CATEGORY), "newsdata"
        ) if fetch_newsdata else _noop_fetch(),
    )

    summary.google_news_fetched = len(news)
    summary.newsdata_fetched = len(newsdata)

    # Google News mapped first so that, on a cross-source duplicate, the variant
    # we keep is the one carrying the salience score (newsdata items are salience 0).
    candidates: list[CandidateInput] = []
    candidates.extend(_map_google_news(news, now))
    candidates.extend(_map_newsdata(newsdata, now))
    candidates = _dedup_cross_source_by_title(candidates)

    if not candidates:
        logger.info("content_ingest: no items fetched")
        return summary

    written = await _embed_and_write(candidates)
    summary.total_written = written

    logger.info("content_ingest: complete", {
        "google_news": summary.google_news_fetched,
        "newsdata": summary.newsdata_fetched,
        "newsdata_fetched_this_hour": fetch_newsdata,
        "written": summary.total_written,
    })
    return summary


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _embed_and_write(candidates: list[CandidateInput]) -> int:
    """Embed + upsert candidates, but make a quota/credits failure scream.

    add_candidates embeds via Gemini (gemini-embedding-001), which has no non-Gemini
    fallback. When prepaid credits are exhausted it raises 429 RESOURCE_EXHAUSTED, the
    content pool stops refreshing, and signal-engine notifications dry up as candidates
    expire. We log that in plain terms instead of a silent 5xx, then re-raise so the
    Cloud Scheduler job still reports the failure."""
    try:
        return await add_candidates(candidates)
    except Exception as exc:
        if is_quota_exhausted(exc):
            logger.error(
                "content_ingest: Gemini quota/credits EXHAUSTED — content pool is NOT "
                "refreshing; signal-engine notifications will dry up as candidates expire. "
                "Check GEMINI_API_KEY billing at https://ai.studio/projects.",
                {"error": str(exc)},
            )
        raise


async def _safe_fetch(coro, source_name: str) -> list[dict]:
    try:
        return await coro
    except Exception as exc:
        logger.warn("content_ingest: source fetch failed", {
            "source": source_name,
            "error": str(exc),
        })
        return []


async def _noop_fetch() -> list[dict]:
    """Stand-in for a skipped source (newsdata on odd hours) so asyncio.gather keeps
    a uniform shape without a real network call."""
    return []


def _map_google_news(items: list[dict], freshness_ts: datetime) -> list[CandidateInput]:
    """Map Google News RSS items to candidates, carrying their feed category, locale
    region, and a global-salience score derived from cross-edition overlap. The
    region is stored in extra so the scoring loop can softly prefer a user's own
    region without a hard filter; salience drives the breaking lane."""
    candidates: list[CandidateInput] = []
    for item in items:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        region = str(item.get("region") or "").strip()
        salience = compute_salience(
            edition_count=int(item.get("edition_count", 1) or 1),
            feed_rank=int(item.get("feed_rank", 0) or 0),
            is_world_section=bool(item.get("is_world", False)),
        )
        candidates.append(CandidateInput(
            source="google_news",
            category=str(item.get("category") or "news"),
            sub_category=str(item.get("sub_category") or ""),
            title=title,
            body=str(item.get("body") or "").strip(),
            url=str(item.get("url") or "").strip(),
            freshness_ts=freshness_ts,
            ttl_hours=SOURCE_TTL_HOURS["google_news"],
            extra={"region": region} if region else None,
            salience=salience,
        ))
    return candidates


def _map_newsdata(items: list[dict], freshness_ts: datetime) -> list[CandidateInput]:
    """Map newsdata.io items to candidates. Direct publisher URL → a clean "read"
    citation. Salience stays 0 (single-source, 12h-delayed) so newsdata can never
    fire the breaking lane; it only ever flows through the personal lane. Freshness
    uses the article's real pubDate when available."""
    candidates: list[CandidateInput] = []
    for item in items:
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title or not url:
            continue
        region = str(item.get("region") or "").strip()
        published_at = item.get("published_at")
        extra: dict = {}
        if region:
            extra["region"] = region
        source_name = str(item.get("source_name") or "").strip()
        if source_name:
            extra["source_name"] = source_name
        image_url = str(item.get("image_url") or "").strip()
        if image_url:
            extra["image_url"] = image_url
        candidates.append(CandidateInput(
            source="newsdata",
            category=str(item.get("category") or "news"),
            sub_category=str(item.get("sub_category") or ""),
            title=title,
            body=str(item.get("body") or "").strip(),
            url=url,
            freshness_ts=published_at if isinstance(published_at, datetime) else freshness_ts,
            ttl_hours=SOURCE_TTL_HOURS["newsdata"],
            extra=extra or None,
            salience=0.0,
        ))
    return candidates


_WS = re.compile(r"\s+")


def _norm_title(title: str) -> str:
    """Normalised title for cross-source de-dup: drop a trailing ' - Publisher',
    lowercase, collapse whitespace. Mirrors google_news._overlap_key so the same
    story arriving from both Google News and newsdata is stored once."""
    base = title.rsplit(" - ", 1)[0] if " - " in title else title
    return _WS.sub(" ", base).strip().lower()


def _dedup_cross_source_by_title(candidates: list[CandidateInput]) -> list[CandidateInput]:
    """Keep the first candidate per normalised title across sources. Google News is
    mapped first, so a story present in both sources keeps the salience-bearing
    Google News variant; the de-dup by content_id in add_candidates only catches
    same-source repeats, so this cross-source pass is needed to avoid sending the
    same story twice via two different source docs."""
    seen: set[str] = set()
    out: list[CandidateInput] = []
    for cand in candidates:
        key = _norm_title(cand.title)
        if key in seen:
            continue
        seen.add(key)
        out.append(cand)
    return out
