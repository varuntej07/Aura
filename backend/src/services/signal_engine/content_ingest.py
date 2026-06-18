"""
Content ingest — one entry point that fills the shared content pool.

  run_ingest()   every 3 hours (Cloud Scheduler)

A tiered, self-healing source strategy fills ONE shared pool; personalisation
happens later at scoring time, not here. Sources by role:
  * newsdata.io — PRIMARY general-news source, runs every hour across broad
    categories. Returns DIRECT publisher URLs (the "read" tap lands on the real
    article). Personal-lane only; never breaking (free tier is ~12h delayed).
  * Google News RSS — FREE best-effort top-up. When it is not 503-blocked from the
    datacenter IP it adds variety + the cross-edition overlap that is the free
    GLOBAL SALIENCE signal powering the breaking lane. The pool never depends on it.
  * Brave News — PAID, datacenter-reliable FALLBACK. Fired ONLY when the pool is
    still below MIN_FRESH_POOL_FLOOR after the free sources (an actual outage), so
    no healthy hour ever spends a Brave credit (see data_fetchers/brave_news.py).

Why a floor gate instead of "fetch everything every hour": the pool is shared and
its cost is independent of user count, so the only thing that matters is that it
never runs dry. The free sources carry it; Brave is the graceful-degradation buffer
that keeps users served through a primary-source outage, and a loud alarm fires if
even that cannot fill the pool — so a starved pool screams instead of silently
ending all notifications (the 2026-06-14 outage). Fetcher failures are isolated;
the embedder de-dups by content_id so re-running early is cheap.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from ...agents.data_fetchers.brave_news import fetch_brave_news
from ...agents.data_fetchers.google_news import fetch_google_news
from ...agents.data_fetchers.newsdata import fetch_newsdata_articles
from ...lib.logger import logger
from ..model_provider import is_quota_exhausted
from .content_pool import CandidateInput, add_candidates, count_fresh_candidates
from .salience import compute_salience

GOOGLE_NEWS_LIMIT_PER_FEED = 8
NEWSDATA_LIMIT_PER_CATEGORY = 10
BRAVE_NEWS_LIMIT_PER_QUERY = 15

# Per-source TTL in hours.
SOURCE_TTL_HOURS: dict[str, int] = {
    "google_news": 24,   # global headlines stay relevant ~a day
    "newsdata": 24,      # 12h-delayed already; a day of pool life is plenty
    "brave_news": 24,    # fresh fallback news; same one-day pool life
}

# Minimum NON-expired candidates for a healthy pool. Below this the per-user vector
# search (find_nearest pulls 50 and drops expired) starts returning [] and
# notifications stop, so the paid Brave fallback tops the pool up. The free sources
# keep the pool well above this on a normal hour, so Brave stays unused. Tunable:
# higher = more source diversity per scoring tick but more fallback credit spend
# during an outage; lower = cheaper but a thinner pool to personalise from.
MIN_FRESH_POOL_FLOOR = 30

# A candidate is push-eligible only if it carries a real body to build a curiosity
# hook from. Title-only / near-empty items (e.g. an RSS entry whose summary is just
# the headline echoed back) may still rank in the in-app feed, but must never fire a
# notification — a push with nothing to tease becomes the vapid "this article is
# about X, what do you think" failure. The framer's own no-substance gate is the
# backstop; this keeps the thinnest items out of the notification set up front.
# Tunable: raising it trades push volume for copy quality (no notification beats a
# bad one). Google News RSS summaries are often thin, so most pushes come from
# newsdata (full publisher bodies); Google News still feeds the feed + salience.
MIN_PUSH_BODY_CHARS = 40


def _is_push_worthy(title: str, body: str) -> bool:
    """True if the body has enough real text to frame a hook from (not title-only)."""
    cleaned = (body or "").strip()
    if len(cleaned) < MIN_PUSH_BODY_CHARS:
        return False
    if cleaned.lower() == (title or "").strip().lower():
        return False
    return True


@dataclass
class IngestSummary:
    google_news_fetched: int = 0
    newsdata_fetched: int = 0
    brave_fetched: int = 0
    total_written: int = 0
    # Non-expired pool size after this ingest (capped at MIN_FRESH_POOL_FLOOR — it is
    # a health gate, not an exact pool count). 0 means the pool cannot serve anyone.
    fresh_after: int = 0


async def run_ingest(*, now: datetime | None = None) -> IngestSummary:
    """Fill the shared pool, escalating to the paid Brave fallback only on an outage.

    Phase 1 — free sources (newsdata PRIMARY + Google News best-effort).
    Phase 2 — Brave fallback, ONLY if the pool is still below the fresh floor.
    Phase 3 — fail loud if the pool still cannot serve.
    See the module docstring for the full rationale.
    """
    summary = IngestSummary()
    now = now or datetime.now(UTC)

    # ── Phase 1: free sources, concurrent. Failures are isolated per source. ──
    newsdata, news = await asyncio.gather(
        _safe_fetch(
            fetch_newsdata_articles(limit_per_category=NEWSDATA_LIMIT_PER_CATEGORY), "newsdata"
        ),
        _safe_fetch(fetch_google_news(limit_per_feed=GOOGLE_NEWS_LIMIT_PER_FEED), "google_news"),
    )
    summary.newsdata_fetched = len(newsdata)
    summary.google_news_fetched = len(news)

    # Google News mapped first so a cross-source duplicate keeps the salience-bearing
    # variant (newsdata items are salience 0). The shared seen-title set also lets the
    # phase-2 Brave fallback skip stories already taken from the free sources.
    seen_titles: set[str] = set()
    primary = _dedup_cross_source_by_title(
        _map_google_news(news, now) + _map_newsdata(newsdata, now),
        seen=seen_titles,
    )
    summary.total_written += await _embed_and_write(primary)

    # ── Phase 2: pool-health gate → Brave fallback (paid, fires ONLY here). ───
    # The gate reads the POOL (not just this run's writes), so existing fresh docs
    # keep Brave unused even when a re-run wrote nothing new.
    fresh = await count_fresh_candidates(limit=MIN_FRESH_POOL_FLOOR, now=now)
    if fresh < MIN_FRESH_POOL_FLOOR:
        logger.warn(
            "content_ingest: pool below fresh floor after free sources, firing the "
            "Brave fallback (newsdata + Google News did not keep the pool fed)",
            {"fresh": fresh, "floor": MIN_FRESH_POOL_FLOOR,
             "newsdata": summary.newsdata_fetched, "google_news": summary.google_news_fetched},
        )
        brave = await _safe_fetch(
            fetch_brave_news(limit_per_query=BRAVE_NEWS_LIMIT_PER_QUERY), "brave_news"
        )
        summary.brave_fetched = len(brave)
        fallback = _dedup_cross_source_by_title(_map_brave_news(brave, now), seen=seen_titles)
        summary.total_written += await _embed_and_write(fallback)
        fresh = await count_fresh_candidates(limit=MIN_FRESH_POOL_FLOOR, now=now)

    # ── Phase 3: fail loud on a pool that still cannot serve. ─────────────────
    summary.fresh_after = fresh
    if fresh == 0:
        logger.error(
            "content_ingest: pool has ZERO fresh candidates after ALL sources, "
            "notifications cannot send for any user. newsdata, Google News, and the "
            "Brave fallback all failed. Check NEWSDATA_API_KEY value/quota and the "
            "BRAVE_API_KEY plan/credits.",
            {"newsdata": summary.newsdata_fetched, "google_news": summary.google_news_fetched,
             "brave": summary.brave_fetched, "written": summary.total_written},
        )
    elif fresh < MIN_FRESH_POOL_FLOOR:
        logger.warn(
            "content_ingest: pool below fresh floor even after the Brave fallback, "
            "degraded but serving; recovers when a source returns",
            {"fresh": fresh, "floor": MIN_FRESH_POOL_FLOOR},
        )

    logger.info("content_ingest: complete", {
        "newsdata": summary.newsdata_fetched,
        "google_news": summary.google_news_fetched,
        "brave": summary.brave_fetched,
        "written": summary.total_written,
        "fresh_after": fresh,
        "floor": MIN_FRESH_POOL_FLOOR,
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
                "content_ingest: Gemini quota/credits EXHAUSTED, content pool is NOT "
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
        body = str(item.get("body") or "").strip()
        candidates.append(CandidateInput(
            source="google_news",
            category=str(item.get("category") or "news"),
            sub_category=str(item.get("sub_category") or ""),
            title=title,
            body=body,
            url=str(item.get("url") or "").strip(),
            freshness_ts=freshness_ts,
            ttl_hours=SOURCE_TTL_HOURS["google_news"],
            extra={"region": region} if region else None,
            salience=salience,
            push_eligible=_is_push_worthy(title, body),
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
        body = str(item.get("body") or "").strip()
        candidates.append(CandidateInput(
            source="newsdata",
            category=str(item.get("category") or "news"),
            sub_category=str(item.get("sub_category") or ""),
            title=title,
            body=body,
            url=url,
            freshness_ts=published_at if isinstance(published_at, datetime) else freshness_ts,
            ttl_hours=SOURCE_TTL_HOURS["newsdata"],
            extra=extra or None,
            salience=0.0,
            push_eligible=_is_push_worthy(title, body),
        ))
    return candidates


def _map_brave_news(items: list[dict], freshness_ts: datetime) -> list[CandidateInput]:
    """Map Brave News items to candidates — same shape as newsdata: direct publisher
    URL for a clean "read" citation, source name + image carried in extra, salience 0
    (single-source → personal lane only, never breaking). Freshness uses Brave's real
    page_age when available, else ingest time."""
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
        body = str(item.get("body") or "").strip()
        candidates.append(CandidateInput(
            source="brave_news",
            category=str(item.get("category") or "news"),
            sub_category=str(item.get("sub_category") or ""),
            title=title,
            body=body,
            url=url,
            freshness_ts=published_at if isinstance(published_at, datetime) else freshness_ts,
            ttl_hours=SOURCE_TTL_HOURS["brave_news"],
            extra=extra or None,
            salience=0.0,
            push_eligible=_is_push_worthy(title, body),
        ))
    return candidates


_WS = re.compile(r"\s+")


def _norm_title(title: str) -> str:
    """Normalised title for cross-source de-dup: drop a trailing ' - Publisher',
    lowercase, collapse whitespace. Mirrors google_news._overlap_key so the same
    story arriving from both Google News and newsdata is stored once."""
    base = title.rsplit(" - ", 1)[0] if " - " in title else title
    return _WS.sub(" ", base).strip().lower()


def _dedup_cross_source_by_title(
    candidates: list[CandidateInput], *, seen: set[str] | None = None
) -> list[CandidateInput]:
    """Keep the first candidate per normalised title across sources. Google News is
    mapped first, so a story present in both sources keeps the salience-bearing
    Google News variant; the de-dup by content_id in add_candidates only catches
    same-source repeats, so this cross-source pass is needed to avoid sending the
    same story twice via two different source docs.

    An optional pre-seeded ``seen`` set (mutated in place) lets a later phase — the
    Brave fallback — de-dup against titles already taken from the free sources in the
    same ingest, so the fallback never re-stores a story the pool already has."""
    seen = seen if seen is not None else set()
    out: list[CandidateInput] = []
    for cand in candidates:
        key = _norm_title(cand.title)
        if key in seen:
            continue
        seen.add(key)
        out.append(cand)
    return out
