"""
Content ingest — two entry points:

  run_ingest()         hourly — HN, arXiv, ESPN Cricinfo RSS
  run_sports_ingest()  every 30 min — cricbuzz live matches + web-searched leagues

All fetches run in parallel. The embedder de-dups by content_id so re-running
early is cheap. Fetcher failures are isolated; one bad source never blocks the
rest.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from ...lib.logger import logger
from ...agents.data_fetchers.arxiv_papers import fetch_recent_papers
from ...agents.data_fetchers.cricket_scores import fetch_live_matches, fetch_recent_results
from ...agents.data_fetchers.hackernews import fetch_top_stories
from ...agents.data_fetchers.web_search import web_search
from .content_pool import CandidateInput, add_candidates

HACKERNEWS_FETCH_LIMIT = 20
ARXIV_FETCH_LIMIT = 15
CRICKET_FETCH_LIMIT = 10

# Per-source TTL in hours.
SOURCE_TTL_HOURS: dict[str, int] = {
    "hackernews": 24,
    "arxiv": 96,
    "espncricinfo": 12,
    "cricbuzz_live": 2,       # live match content expires fast
    "sports_web_search": 6,   # web-fetched league scores go stale within a session
}

# Each entry: (web search query, sub_category tag for cosine matching).
# Capped at 8 queries — each is a Gemini Flash call; parallelised but not free.
SPORTS_WEB_SEARCH_QUERIES: list[tuple[str, str]] = [
    ("IPL cricket live score match highlights today", "ipl"),
    ("Premier League football result today", "premier_league"),
    ("cricket international match score result today", "cricket"),
    ("NBA basketball game score tonight", "nba"),
    ("La Liga football result today", "la_liga"),
    ("Formula 1 F1 race result today", "formula1"),
    ("tennis grand slam match result today", "tennis"),
    ("NFL football game score tonight", "nfl"),
]

_SPORTS_SUB_CATEGORY_TITLES: dict[str, str] = {
    "ipl": "IPL cricket latest",
    "premier_league": "Premier League latest",
    "cricket": "Cricket international latest",
    "nba": "NBA basketball latest",
    "la_liga": "La Liga latest",
    "formula1": "Formula 1 latest",
    "tennis": "Tennis latest",
    "nfl": "NFL football latest",
}


@dataclass
class IngestSummary:
    hackernews_fetched: int = 0
    arxiv_fetched: int = 0
    cricket_fetched: int = 0
    total_written: int = 0


@dataclass
class SportsSummary:
    live_cricket_fetched: int = 0
    sports_web_searches_fetched: int = 0
    total_written: int = 0


async def run_ingest() -> IngestSummary:
    """Hourly: fetch from HN, arXiv, and ESPN Cricinfo RSS in parallel."""
    summary = IngestSummary()
    now = datetime.now(timezone.utc)

    hn, arxiv, cricket = await asyncio.gather(
        _safe_fetch(fetch_top_stories(limit=HACKERNEWS_FETCH_LIMIT), "hackernews"),
        _safe_fetch(fetch_recent_papers(max_results=ARXIV_FETCH_LIMIT), "arxiv"),
        _safe_fetch(fetch_recent_results(limit=CRICKET_FETCH_LIMIT), "espncricinfo"),
    )

    summary.hackernews_fetched = len(hn)
    summary.arxiv_fetched = len(arxiv)
    summary.cricket_fetched = len(cricket)

    candidates: list[CandidateInput] = []
    candidates.extend(_map_hackernews(hn, now))
    candidates.extend(_map_arxiv(arxiv, now))
    candidates.extend(_map_cricket_rss(cricket, now))

    if not candidates:
        logger.info("content_ingest: no items fetched")
        return summary

    written = await add_candidates(candidates)
    summary.total_written = written

    logger.info("content_ingest: complete", {
        "hackernews": summary.hackernews_fetched,
        "arxiv": summary.arxiv_fetched,
        "cricket": summary.cricket_fetched,
        "written": summary.total_written,
    })
    return summary


async def run_sports_ingest() -> SportsSummary:
    """Every 30 min: live cricket scores + web-searched league results."""
    summary = SportsSummary()
    now = datetime.now(timezone.utc)

    live_matches, web_search_results = await asyncio.gather(
        _safe_fetch(fetch_live_matches(), "cricbuzz_live"),
        asyncio.gather(*[
            _safe_sports_web_search(query, sub_cat)
            for query, sub_cat in SPORTS_WEB_SEARCH_QUERIES
        ]),
    )
    web_search_candidates: list[CandidateInput] = [
        item for batch in web_search_results for item in batch
    ]

    summary.live_cricket_fetched = len(live_matches)
    summary.sports_web_searches_fetched = len(web_search_candidates)

    candidates: list[CandidateInput] = []
    candidates.extend(_map_live_cricket(live_matches, now))
    candidates.extend(web_search_candidates)

    if candidates:
        written = await add_candidates(candidates)
        summary.total_written = written

    logger.info("content_ingest.sports: complete", {
        "live_cricket": summary.live_cricket_fetched,
        "web_search_items": summary.sports_web_searches_fetched,
        "written": summary.total_written,
    })
    return summary


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _safe_fetch(coro, source_name: str) -> list[dict]:
    try:
        return await coro
    except Exception as exc:
        logger.warn("content_ingest: source fetch failed", {
            "source": source_name,
            "error": str(exc),
        })
        return []


async def _safe_sports_web_search(query: str, sub_category: str) -> list[CandidateInput]:
    """One web search query -> one CandidateInput. Returns [] on any failure."""
    now = datetime.now(timezone.utc)
    try:
        text = await web_search(query, uid="sports_ingest")
        if not text or not text.strip():
            return []
        title = _SPORTS_SUB_CATEGORY_TITLES.get(sub_category, " ".join(query.split()[:5]))
        return [CandidateInput(
            source="sports_web_search",
            category="sports",
            sub_category=sub_category,
            title=title,
            body=text[:500].strip(),
            url="",
            freshness_ts=now,
            ttl_hours=SOURCE_TTL_HOURS["sports_web_search"],
        )]
    except Exception as exc:
        logger.warn("content_ingest: sports web search failed", {
            "query": query,
            "sub_category": sub_category,
            "error": str(exc),
        })
        return []


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
