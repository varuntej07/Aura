"""
Tiered topic fetcher — the live-state fetch for a tracker checkpoint.

ONE entrypoint, ``fetch_topic(query)``, runs a cost-ordered chain of INDEPENDENT
web sources and returns the first usable result, so a tracker's update never
depends on a single provider being up:

    1. rss       Google News RSS search   free, unlimited, no key   ← tried first
    2. newsdata  newsdata.io API           free tier, direct URLs
    3. brave     Brave Search API          fast (~1s), raw snippets
    4. grounded  Gemini grounded search    premium, search+synthesis ← last resort

Order is settings-driven (``settings.tracking_fetch_tier_order``), so a tier can
be reordered or dropped via env with no code change. Each tier has its OWN bounded
timeout + small retry; a tier that errors, times out, hits a quota (429), or
returns too little text FALLS THROUGH to the next. The whole chain is bounded,
never raises, and reports which tier served the result (``FetchResult.tier``) so a
degraded provider is visible in logs, not silent.

Reused as-is: :func:`brave_search` and :meth:`ModelProvider.grounded`. The RSS and
newsdata helpers are kept LOCAL (not added to the signal-engine fetchers) so this
feature can never regress the content-pool ingest path that shares those modules.

The result for a topic query is identical for every subscriber, so it is cached
per normalized query for ``settings.TRACKING_LIVE_CACHE_TTL_SECONDS`` — one fetch
serves the whole fan-out for a topic-moment (the scale lever: cost tracks topics,
not users).
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import quote_plus

import httpx

from ...config.settings import settings
from ...lib.logger import logger
from .fields import TIER_BRAVE, TIER_GROUNDED, TIER_NEWSDATA, TIER_NONE, TIER_RSS

# ── Per-tier timeouts + retries (sized so the chain is bounded even all-failing) ─
# Worst case (every tier falls through) ≈ 8*2 + 12 + 9 + 45 ≈ 81s, but that only
# happens when ALL four providers fail; the typical path returns from tier 1 in
# ~1-2s. These are background checkpoint fetches, never a user-facing wait.
_RSS_TIMEOUT_S = 8.0
_RSS_ATTEMPTS = 2
_RSS_RETRY_BASE_SLEEP_S = 0.5

_NEWSDATA_TIMEOUT_S = 12.0          # one call; a 429 skips straight to the next tier (no retry)

_BRAVE_TIMEOUT_S = 9.0              # background, so a touch more generous than the chat path's 7s

# grounded() carries its OWN 45s timeout + 3 retries inside ModelProvider, so the
# chain makes a single outer call and lets that handle transient errors.

# A tier "succeeded" only if it returned at least this much text — a 2-word snippet
# is not a usable live state and should fall through to a richer source.
_MIN_USABLE_TEXT_CHARS = 40

# How many results to pull per cheap source (enough to compose from, not exhaustive).
_RSS_RESULT_COUNT = 6
_NEWSDATA_RESULT_COUNT = 8

# Shared in-process result cache, keyed by normalized query. Bounds repeat fetches
# within a single tick's fan-out; the authoritative cross-tick cache is the
# tracked_topics.live_summary field in Firestore.
_CACHE_MAX_ENTRIES = 256
_cache: dict[str, tuple[float, "FetchResult"]] = {}

_HTML_TAG = re.compile(r"<[^>]+>")

_GOOGLE_NEWS_SEARCH_URL = (
    "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
)
# A real-browser UA — Google News RSS serves a datacenter bot UA less reliably.
_FEED_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class FetchResult:
    """Normalized output of the fetch chain. ``tier`` is the source that served it
    (one of the ``TIER_*`` constants, or ``TIER_NONE`` when every tier failed);
    ``text`` is the raw factual material a compose/synthesis LLM turns into the
    update; ``sources`` are ``{title, url}`` citations in source order."""

    text: str
    sources: list[dict[str, str]] = field(default_factory=list)
    tier: str = TIER_NONE
    latency_ms: int = 0
    cached: bool = False

    @property
    def ok(self) -> bool:
        return self.tier != TIER_NONE and len(self.text) >= _MIN_USABLE_TEXT_CHARS


def _clean(text: str) -> str:
    return _HTML_TAG.sub("", text or "").strip()


def _normalize_query(query: str) -> str:
    return " ".join(query.lower().strip().split())


def _cache_get(key: str) -> FetchResult | None:
    entry = _cache.get(key)
    if not entry:
        return None
    expires_at, value = entry
    if time.monotonic() >= expires_at:
        _cache.pop(key, None)
        return None
    return value


def _cache_put(key: str, value: FetchResult) -> None:
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)
    _cache[key] = (time.monotonic() + settings.TRACKING_LIVE_CACHE_TTL_SECONDS, value)


# ── Tier 1: Google News RSS search (free, no key) ────────────────────────────
def _fetch_rss_sync(query: str) -> tuple[str, list[dict[str, str]]]:
    """Blocking RSS search fetch + parse. Run via asyncio.to_thread. Kept local to
    this module (not the signal-engine google_news fetcher) so a change here can
    never regress the content-pool ingest. Bounded timeout + small retry for a
    transient 503/blip; returns ("", []) on persistent failure (the chain falls on)."""
    try:
        import feedparser  # type: ignore
    except ImportError:
        logger.warn("topic_fetcher.rss: feedparser not installed — skipping tier")
        return "", []

    url = _GOOGLE_NEWS_SEARCH_URL.format(query=quote_plus(query))
    resp = None
    for attempt in range(1, _RSS_ATTEMPTS + 1):
        try:
            resp = httpx.get(
                url,
                timeout=_RSS_TIMEOUT_S,
                follow_redirects=True,   # Google News RSS 3xx-redirects (CLAUDE.md httpx rule)
                headers={"User-Agent": _FEED_USER_AGENT},
            )
            resp.raise_for_status()
            break
        except Exception as exc:
            if attempt >= _RSS_ATTEMPTS:
                logger.warn("topic_fetcher.rss: fetch failed", {
                    "query": query, "attempts": attempt, "error": str(exc),
                })
                return "", []
            time.sleep(_RSS_RETRY_BASE_SLEEP_S * attempt)

    if resp is None:
        return "", []
    try:
        feed = feedparser.parse(resp.content)
    except Exception as exc:
        logger.warn("topic_fetcher.rss: parse failed", {"query": query, "error": str(exc)})
        return "", []

    blocks: list[str] = []
    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in (feed.entries or [])[:_RSS_RESULT_COUNT]:
        title = _clean(getattr(entry, "title", ""))
        summary = _clean(getattr(entry, "summary", ""))
        if title or summary:
            blocks.append(f"{title}: {summary}".strip(": ").strip())
        link = str(getattr(entry, "link", "")).strip()
        if link and link not in seen:
            seen.add(link)
            sources.append({"title": title or link, "url": link})
    return "\n\n".join(blocks), sources


async def _fetch_rss(query: str) -> tuple[str, list[dict[str, str]]]:
    return await asyncio.to_thread(_fetch_rss_sync, query)


# ── Tier 2: newsdata.io query search (free tier, direct publisher URLs) ───────
async def _fetch_newsdata(query: str) -> tuple[str, list[dict[str, str]]]:
    """newsdata.io 'latest' endpoint with a free-text ``q``. A minimal, query-shaped
    call kept local here — the signal-engine newsdata fetcher is category-shaped and
    must not be reshaped for this. A 429 (free-tier quota) returns empty so the chain
    immediately falls through to Brave rather than hammering an exhausted key."""
    if not settings.newsdata_configured:
        return "", []
    params = {
        "apikey": settings.NEWSDATA_API_KEY.strip(),
        "q": query,
        "language": (settings.NEWSDATA_LANGUAGE or "en").strip() or "en",
    }
    try:
        async with httpx.AsyncClient(timeout=_NEWSDATA_TIMEOUT_S, follow_redirects=True) as client:
            resp = await client.get(settings.NEWSDATA_BASE_URL, params=params)
    except Exception as exc:
        logger.warn("topic_fetcher.newsdata: request failed", {"query": query, "error": str(exc)})
        return "", []

    if resp.status_code == 429:
        logger.warn(
            "topic_fetcher.newsdata: 429 quota/rate-limited — falling through to next tier",
            {"query": query},
        )
        return "", []
    if resp.status_code != 200:
        logger.warn("topic_fetcher.newsdata: non-200", {"query": query, "status": resp.status_code})
        return "", []

    payload = resp.json() if resp.content else {}
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return "", []

    blocks: list[str] = []
    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in (payload.get("results") or [])[:_NEWSDATA_RESULT_COUNT]:
        if not isinstance(item, dict):
            continue
        title = _clean(str(item.get("title", "")))
        body = _clean(str(item.get("description", "")))
        if title or body:
            blocks.append(f"{title}: {body}".strip(": ").strip())
        link = str(item.get("link", "")).strip()
        if link and link not in seen:
            seen.add(link)
            sources.append({"title": title or link, "url": link})
    return "\n\n".join(blocks), sources


# ── Tier 3: Brave Search (fast, raw snippets) ────────────────────────────────
async def _fetch_brave(query: str) -> tuple[str, list[dict[str, str]]]:
    """Reuse the existing Brave primitive. recency='fresh' biases to the last 24h
    (live scores/results). A missing key raises ValueError inside brave_search; we
    treat that as 'tier unavailable' and fall through. uid is a constant so every
    subscriber shares Brave's own in-process cache for the same topic query."""
    from ...agents.data_fetchers.brave_search import brave_search

    try:
        result = await brave_search(
            query, uid="topic_tracking", recency="fresh", timeout_s=_BRAVE_TIMEOUT_S,
        )
    except ValueError:
        # BRAVE_API_KEY unset — tier unavailable, fall through (not an error worth raising).
        return "", []
    except Exception as exc:
        logger.warn("topic_fetcher.brave: failed", {"query": query, "error": str(exc)})
        return "", []
    return str(result.get("text", "")), list(result.get("sources", []))


# ── Tier 4: Gemini grounded search (premium, last resort) ────────────────────
async def _fetch_grounded(query: str) -> tuple[str, list[dict[str, str]]]:
    """Last resort: grounded search SYNTHESIZES from a live web search in one call,
    so it works even when the cheap snippet sources are empty or unhelpful. Carries
    its own 45s timeout + retries inside ModelProvider."""
    from ..model_provider import get_model_provider

    prompt = (
        "Search the web and report the very latest factual update on this topic. "
        "Be concise and lead with the most recent concrete facts (scores, times, "
        f"outcomes, status). Topic: {query}"
    )
    try:
        result = await get_model_provider().grounded(prompt)
    except Exception as exc:
        logger.warn("topic_fetcher.grounded: failed", {"query": query, "error": str(exc)})
        return "", []
    return result.text, [{"title": s.get("title", ""), "url": s.get("url", "")} for s in result.sources]


_TIER_FETCHERS: dict[str, Callable[[str], Awaitable[tuple[str, list[dict[str, str]]]]]] = {
    TIER_RSS: _fetch_rss,
    TIER_NEWSDATA: _fetch_newsdata,
    TIER_BRAVE: _fetch_brave,
    TIER_GROUNDED: _fetch_grounded,
}


async def fetch_topic(query: str, *, use_cache: bool = True) -> FetchResult:
    """Run the cost-ordered fetch chain for ``query`` and return the first usable
    result. Never raises — every tier failing yields a ``FetchResult`` with
    ``tier == TIER_NONE`` (``.ok`` False), which the caller treats as 'no live
    state this fetch' (skip the checkpoint, retry next reconcile), never a crash.

    The chain order, the cache TTL, and which tiers exist are all settings-driven.
    """
    query = (query or "").strip()
    if not query:
        return FetchResult(text="", tier=TIER_NONE)

    cache_key = _normalize_query(query)
    if use_cache:
        hit = _cache_get(cache_key)
        if hit is not None:
            logger.info("topic_fetcher: cache hit", {"query": query, "tier": hit.tier})
            return FetchResult(
                text=hit.text, sources=hit.sources, tier=hit.tier,
                latency_ms=hit.latency_ms, cached=True,
            )

    started = time.monotonic()
    tried: list[str] = []
    for tier in settings.tracking_fetch_tier_order:
        fetcher = _TIER_FETCHERS.get(tier)
        if fetcher is None:
            continue
        tried.append(tier)
        try:
            text, sources = await fetcher(query)
        except Exception as exc:
            # Defensive: each fetcher already swallows its own errors, but never let
            # one tier's surprise abort the whole chain — fall through to the next.
            logger.warn("topic_fetcher: tier raised, falling through", {
                "tier": tier, "query": query, "error": str(exc),
            })
            continue

        if text and len(text.strip()) >= _MIN_USABLE_TEXT_CHARS:
            latency_ms = int((time.monotonic() - started) * 1000)
            result = FetchResult(
                text=text.strip(), sources=sources, tier=tier,
                latency_ms=latency_ms, cached=False,
            )
            if use_cache:
                _cache_put(cache_key, result)
            logger.info("topic_fetcher: served", {
                "query": query, "tier": tier, "tried": tried,
                "chars": len(result.text), "sources": len(sources), "latency_ms": latency_ms,
            })
            return result

    # Every configured tier failed or returned too little. Loud, because a topic with
    # zero fetchable state across FOUR independent sources is a real signal (a dead
    # query, or all providers down), not a normal empty.
    latency_ms = int((time.monotonic() - started) * 1000)
    logger.warn("topic_fetcher: ALL tiers returned nothing usable", {
        "query": query, "tried": tried, "latency_ms": latency_ms,
    })
    return FetchResult(text="", tier=TIER_NONE, latency_ms=latency_ms)
