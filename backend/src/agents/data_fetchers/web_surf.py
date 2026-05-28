"""Web surfing primitive — Gemini google_search grounding with timeout, retry, cache, citations.

Used by the LLM-facing `web_surf` tool (chat + voice) and by internal scheduled agents that
previously imported `web_search` (re-exported by web_search.py as a backwards-compatible shim).
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from ...config.settings import settings
from ...lib.logger import logger

_REQUEST_TIMEOUT_S = 6.0
_CACHE_TTL_S = 300.0  # 5 minutes
_CACHE_MAX_ENTRIES = 256

_PROMPT_INJECTION_PATTERN = re.compile(
    r"(?im)^\s*(ignore (all )?previous|system:|<\|.*\|>).*$"
)

# Module-level in-process cache: {(uid, normalized_query, recency): (expires_at_monotonic, result_dict)}.
_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}


def _normalize_query(query: str) -> str:
    return " ".join(query.lower().strip().split())


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
        # Evict the oldest expiring entry. Cheap because the cache is small.
        oldest_key = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest_key, None)
    _cache[key] = (time.monotonic() + _CACHE_TTL_S, value)


def _strip_prompt_injection(text: str) -> str:
    return _PROMPT_INJECTION_PATTERN.sub("", text).strip()


def _extract_sources(response: Any) -> list[dict[str, str]]:
    """Pull title + url for each grounding chunk Gemini cited."""
    sources: list[dict[str, str]] = []
    try:
        candidates = getattr(response, "candidates", None) or []
        for candidate in candidates:
            grounding = getattr(candidate, "grounding_metadata", None)
            if not grounding:
                continue
            chunks = getattr(grounding, "grounding_chunks", None) or []
            for chunk in chunks:
                web = getattr(chunk, "web", None)
                if not web:
                    continue
                uri = getattr(web, "uri", None)
                title = getattr(web, "title", None)
                if uri:
                    sources.append({"title": str(title or uri), "url": str(uri)})
    except Exception as exc:
        logger.warn("web_surf: source extraction failed", {"error": str(exc)})
    
    # De-dup while preserving order
    seen: set[str] = set()
    deduped: list[dict[str, str]] = []
    
    for s in sources:
        if s["url"] in seen:
            continue
        seen.add(s["url"])
        deduped.append(s)
    return deduped


def _search_sync(query: str) -> tuple[str, list[dict[str, str]]]:
    if not settings.GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY not configured — web search unavailable")

    from google import genai  # type: ignore
    from google.genai import types  # type: ignore

    client = genai.Client(api_key=settings.GEMINI_API_KEY)
    tool = types.Tool(google_search=types.GoogleSearch())
    config = types.GenerateContentConfig(tools=[tool], temperature=1.0)

    response = client.models.generate_content(
        model=settings.GEMINI_MODEL,
        contents=query,
        config=config,
    )

    text = response.text or ""
    sources = _extract_sources(response)
    return text, sources


async def web_surf(
    query: str,
    *,
    uid: str,
    recency: str = "any",
) -> dict[str, Any]:
    """Grounded web search. Returns {text, sources, query, cached}.

    recency: 'fresh' appends today's date hint so Gemini prefers up-to-date sources.
             'any' (default) sends the query as-is.
    """
    query = query.strip()
    if not query:
        raise ValueError("query is required")

    effective_query = query
    if recency == "fresh":
        from datetime import UTC, datetime
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        effective_query = f"{query} (as of {today})"

    cache_key = (uid, _normalize_query(effective_query), recency)
    cached = _cache_get(cache_key)

    if cached is not None:
        logger.info("web_surf cache hit", {
            "uid": uid, "query_len": len(query), "source_count": len(cached.get("sources", [])),
        })
        return {**cached, "cached": True}

    started = time.monotonic()
    last_exc: Exception | None = None
    
    for attempt in range(2):  # one retry
        try:
            text, sources = await asyncio.wait_for(
                asyncio.to_thread(_search_sync, effective_query),
                timeout=_REQUEST_TIMEOUT_S,
            )
            break
        except (TimeoutError, asyncio.TimeoutError) as exc:
            last_exc = exc
            logger.warn("web_surf timeout", {"uid": uid, "attempt": attempt})
            continue
        except Exception as exc:
            # Only retry on transient service errors. Anything else (auth, bad query) is final.
            name = type(exc).__name__
            if "ServiceUnavailable" in name or "DeadlineExceeded" in name:
                last_exc = exc
                logger.warn("web_surf transient error", {"uid": uid, "attempt": attempt, "error": str(exc)})
                continue
            logger.error("web_surf failed", {"uid": uid, "query": query, "error": str(exc), "error_type": name})
            raise
    else:
        logger.error("web_surf exhausted retries", {"uid": uid, "error": str(last_exc)})
        raise last_exc or RuntimeError("web_surf failed with no exception captured")

    text = _strip_prompt_injection(text)
    result = {
        "text": text,
        "sources": sources,
        "query": query,
        "cached": False,
    }
    _cache_put(cache_key, result)

    latency_ms = int((time.monotonic() - started) * 1000)
    logger.info("web_surf OK", {
        "uid": uid,
        "query_len": len(query),
        "result_len": len(text),
        "source_count": len(sources),
        "latency_ms": latency_ms,
        "recency": recency,
    })
    return result
