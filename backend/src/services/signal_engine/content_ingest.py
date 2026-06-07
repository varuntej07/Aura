"""
Content ingest — two entry points:

  run_ingest()         hourly — HN, arXiv, ESPN Cricinfo RSS, Google News (global)
  run_sports_ingest()  every 30 min — cricbuzz live match scores

All fetches run in parallel and use FREE sources (RSS + public scrapes). The pool
only needs title + body + url to embed and score, so the previous Gemini-grounded
web search was removed: it paid to synthesise prose we then truncated and embedded.
The embedder de-dups by content_id so re-running early is cheap. Fetcher failures
are isolated; one bad source never blocks the rest.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from ...agents.data_fetchers.arxiv_papers import fetch_recent_papers
from ...agents.data_fetchers.cricket_scores import fetch_live_matches, fetch_recent_results
from ...agents.data_fetchers.google_news import fetch_google_news
from ...agents.data_fetchers.hackernews import fetch_top_stories
from ...lib.logger import logger
from ..model_provider import is_quota_exhausted
from .content_pool import CandidateInput, add_candidates

HACKERNEWS_FETCH_LIMIT = 20
ARXIV_FETCH_LIMIT = 15
CRICKET_FETCH_LIMIT = 10
GOOGLE_NEWS_LIMIT_PER_FEED = 8

# Per-source TTL in hours.
SOURCE_TTL_HOURS: dict[str, int] = {
    "hackernews": 24,
    "arxiv": 96,
    "espncricinfo": 12,
    "cricbuzz_live": 2,    # live match content expires fast
    "google_news": 24,     # global headlines stay relevant ~a day
}


@dataclass
class IngestSummary:
    hackernews_fetched: int = 0
    arxiv_fetched: int = 0
    cricket_fetched: int = 0
    google_news_fetched: int = 0
    total_written: int = 0


@dataclass
class SportsSummary:
    live_cricket_fetched: int = 0
    total_written: int = 0


async def run_ingest() -> IngestSummary:
    """Hourly: fetch HN, arXiv, ESPN Cricinfo RSS, and global Google News in parallel."""
    summary = IngestSummary()
    now = datetime.now(UTC)

    hn, arxiv, cricket, news = await asyncio.gather(
        _safe_fetch(fetch_top_stories(limit=HACKERNEWS_FETCH_LIMIT), "hackernews"),
        _safe_fetch(fetch_recent_papers(max_results=ARXIV_FETCH_LIMIT), "arxiv"),
        _safe_fetch(fetch_recent_results(limit=CRICKET_FETCH_LIMIT), "espncricinfo"),
        _safe_fetch(fetch_google_news(limit_per_feed=GOOGLE_NEWS_LIMIT_PER_FEED), "google_news"),
    )

    summary.hackernews_fetched = len(hn)
    summary.arxiv_fetched = len(arxiv)
    summary.cricket_fetched = len(cricket)
    summary.google_news_fetched = len(news)

    candidates: list[CandidateInput] = []
    candidates.extend(_map_hackernews(hn, now))
    candidates.extend(_map_arxiv(arxiv, now))
    candidates.extend(_map_cricket_rss(cricket, now))
    candidates.extend(_map_google_news(news, now))

    if not candidates:
        logger.info("content_ingest: no items fetched")
        return summary

    written = await _embed_and_write(candidates)
    summary.total_written = written

    logger.info("content_ingest: complete", {
        "hackernews": summary.hackernews_fetched,
        "arxiv": summary.arxiv_fetched,
        "cricket": summary.cricket_fetched,
        "google_news": summary.google_news_fetched,
        "written": summary.total_written,
    })
    return summary


async def run_sports_ingest() -> SportsSummary:
    """Every 30 min: live cricket scores from cricbuzz (free scrape).

    Broader sports headlines (other leagues) now arrive via Google News in
    run_ingest; this path keeps only the free, freshness-sensitive live scores.
    """
    summary = SportsSummary()
    now = datetime.now(UTC)

    live_matches = await _safe_fetch(fetch_live_matches(), "cricbuzz_live")
    summary.live_cricket_fetched = len(live_matches)

    candidates = _map_live_cricket(live_matches, now)
    if candidates:
        written = await _embed_and_write(candidates)
        summary.total_written = written

    logger.info("content_ingest.sports: complete", {
        "live_cricket": summary.live_cricket_fetched,
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


def _map_google_news(items: list[dict], freshness_ts: datetime) -> list[CandidateInput]:
    """Map free Google News RSS items to candidates, carrying their feed category."""
    candidates: list[CandidateInput] = []
    for item in items:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        candidates.append(CandidateInput(
            source="google_news",
            category=str(item.get("category") or "news"),
            sub_category=str(item.get("sub_category") or ""),
            title=title,
            body=str(item.get("body") or "").strip(),
            url=str(item.get("url") or "").strip(),
            freshness_ts=freshness_ts,
            ttl_hours=SOURCE_TTL_HOURS["google_news"],
        ))
    return candidates


def _map_hackernews(items: list[dict], freshness_ts: datetime) -> list[CandidateInput]:
    candidates: list[CandidateInput] = []
    for item in items:
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title:
            continue
        points = item.get("points") or 0
        comments = item.get("comments") or 0
        body = f"{points} points, {comments} comments on Hacker News."
        candidates.append(CandidateInput(
            source="hackernews",
            category="tech",
            title=title,
            body=body,
            url=url,
            freshness_ts=freshness_ts,
            ttl_hours=SOURCE_TTL_HOURS["hackernews"],
            extra={"points": points, "comments": comments},
        ))
    return candidates


def _map_arxiv(items: list[dict], freshness_ts: datetime) -> list[CandidateInput]:
    candidates: list[CandidateInput] = []
    for item in items:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        summary = str(item.get("summary") or "").strip()
        url = str(item.get("url") or "").strip()
        candidates.append(CandidateInput(
            source="arxiv",
            category="research",
            title=title,
            body=summary,
            url=url,
            freshness_ts=freshness_ts,
            ttl_hours=SOURCE_TTL_HOURS["arxiv"],
        ))
    return candidates


def _map_cricket_rss(items: list[dict], freshness_ts: datetime) -> list[CandidateInput]:
    candidates: list[CandidateInput] = []
    for item in items:
        headline = str(item.get("headline") or "").strip()
        if not headline:
            continue
        description = str(item.get("description") or "").strip()
        url = str(item.get("url") or "").strip()
        candidates.append(CandidateInput(
            source="espncricinfo",
            category="sports",
            sub_category="cricket",
            title=headline,
            body=description,
            url=url,
            freshness_ts=freshness_ts,
            ttl_hours=SOURCE_TTL_HOURS["espncricinfo"],
        ))
    return candidates


def _map_live_cricket(matches: list[dict], now: datetime) -> list[CandidateInput]:
    """Map cricbuzz live match dicts to CandidateInput with a 2h TTL."""
    candidates: list[CandidateInput] = []
    for m in matches:
        team1 = str(m.get("team1") or "").strip()
        team2 = str(m.get("team2") or "").strip()
        score = str(m.get("score") or "").strip()
        match_desc = str(m.get("match") or "").strip()

        if team1 and team2:
            title = f"LIVE: {team1} vs {team2}"
            body = f"{match_desc} — {team1} vs {team2}" + (f", Score: {score}" if score else "")
        elif match_desc:
            title = f"LIVE: {match_desc}"
            body = match_desc
        else:
            continue

        candidates.append(CandidateInput(
            source="cricbuzz_live",
            category="sports",
            sub_category="cricket_live",
            title=title,
            body=body.strip(),
            url="",
            freshness_ts=now,
            ttl_hours=SOURCE_TTL_HOURS["cricbuzz_live"],
        ))
    return candidates
