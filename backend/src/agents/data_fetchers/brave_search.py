"""Brave Search primitive — fast raw web search for the real-time chat + voice path.

Returns the same {text, sources, query, cached} shape as web_surf.py (Gemini grounding),
so ToolExecutor can swap providers without touching downstream rendering or citations.

Why a separate primitive: Gemini grounding searches AND synthesizes a full answer before
returning, which costs seconds — fine for the background sports ingest (45s budget, no one
waiting) but too slow when a user is mid-conversation. Brave returns raw snippets in ~1s;
the agent LLM (Claude in chat, the voice model) does the synthesis as part of its normal
streamed reply. Gemini grounding stays the engine for sports ingest; this is the user path.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

from ...config.settings import settings
from ...lib.logger import logger
from .web_surf import _REQUEST_TIMEOUT_S, _normalize_query, _strip_prompt_injection

_BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
_RESULT_COUNT = 5  # "random recent news", not exhaustive research
_CACHE_TTL_S = 300.0  # 5 minutes
_CACHE_MAX_ENTRIES = 256

# Brave's freshness filter. 'fresh' -> last 24h; 'any' omits the param entirely.
_FRESHNESS_BY_RECENCY = {"fresh": "pd"}

_HTML_TAG = re.compile(r"<[^>]+>")

# Module-level in-process cache: {(uid, normalized_query, recency): (expires_at_monotonic, result_dict)}.
_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}


def _cache_get(key: tuple[str, str, str]) -> dict[str, Any] | None:
    entry = _cache.get(key)
    if not entry:
        return None
    expires_at, value = entry
    if time.monotonic() >= expires_at:
        _cache.pop(key, None)
        return None
    return value


def _cache_put(key: tuple[str, str, str], value: dict[str, Any]) -> None:
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        oldest_key = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest_key, None)
    _cache[key] = (time.monotonic() + _CACHE_TTL_S, value)


def _parse_brave_response(payload: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    """Flatten Brave web results into one text blob + deduped citation list.

    text:    one block per result — "<title>: <description> <extra snippets>".
    sources: [{title, url}] in result order, deduped by url.
    """
    results = ((payload.get("web") or {}).get("results")) or []
    blocks: list[str] = []
    sources: list[dict[str, str]] = []
    seen: set[str] = set()

    for result in results:
        title = _HTML_TAG.sub("", str(result.get("title") or "")).strip()
        description = _HTML_TAG.sub("", str(result.get("description") or "")).strip()
        extras = [
            _HTML_TAG.sub("", str(snippet)).strip()
            for snippet in (result.get("extra_snippets") or [])
            if str(snippet).strip()
        ]
        snippet = " ".join([description, *extras]).strip()
        if title or snippet:
            blocks.append(f"{title}: {snippet}".strip(": ").strip())

        url = str(result.get("url") or "").strip()
        if url and url not in seen:
            seen.add(url)
            sources.append({"title": title or url, "url": url})

    return "\n\n".join(blocks), sources


async def brave_search(
    query: str,
    *,
    uid: str,
    recency: str = "any",
    timeout_s: float = _REQUEST_TIMEOUT_S,
) -> dict[str, Any]:
    """Raw web search via Brave. Returns {text, sources, query, cached}.

    recency: 'fresh' restricts to the last 24h (Brave freshness=pd). 'any' (default)
             searches without a date filter.

    Network/timeout/non-200 failures degrade to an empty result rather than raising, so a
    flaky search never breaks the chat or voice turn. A missing API key raises, because that
    is a deploy misconfiguration the developer must see.
    """
    query = query.strip()
    if not query:
        raise ValueError("query is required")
    if not settings.BRAVE_API_KEY:
        raise ValueError("BRAVE_API_KEY not configured — web search unavailable")

    recency = recency if recency in {"any", "fresh"} else "any"

    cache_key = (uid, _normalize_query(query), recency)
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.info("brave_search cache hit", {
            "uid": uid, "query_len": len(query), "source_count": len(cached.get("sources", [])),
        })
        return {**cached, "cached": True}

    params: dict[str, Any] = {"q": query, "count": _RESULT_COUNT, "extra_snippets": "true"}
    freshness = _FRESHNESS_BY_RECENCY.get(recency)
    if freshness:
        params["freshness"] = freshness
    headers = {"X-Subscription-Token": settings.BRAVE_API_KEY, "Accept": "application/json"}

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_s) as client:
            response = await client.get(_BRAVE_SEARCH_URL, params=params, headers=headers)
    except httpx.HTTPError as exc:
        logger.warn("brave_search request failed", {"uid": uid, "error": str(exc)})
        return {"text": "", "sources": [], "query": query, "cached": False}

    if response.status_code != 200:
        logger.warn("brave_search non-200", {"uid": uid, "status": response.status_code})
        return {"text": "", "sources": [], "query": query, "cached": False}

    text, sources = _parse_brave_response(response.json())
    text = _strip_prompt_injection(text)
    result = {"text": text, "sources": sources, "query": query, "cached": False}
    _cache_put(cache_key, result)

    latency_ms = int((time.monotonic() - started) * 1000)
    logger.info("brave_search OK", {
        "uid": uid,
        "query_len": len(query),
        "result_len": len(text),
        "source_count": len(sources),
        "latency_ms": latency_ms,
        "recency": recency,
    })
    return result
