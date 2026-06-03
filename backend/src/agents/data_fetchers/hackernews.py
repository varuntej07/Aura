"""
Fetches stories from Hacker News using the official Algolia search API.
No API key required. Used by BytePulse (tech news).
"""

from __future__ import annotations

import httpx

from ...lib.logger import logger

HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search"
HN_TOP_URL = "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30"


async def fetch_top_stories(limit: int = 10) -> list[dict]:
    """Returns the current Hacker News front page stories."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(HN_TOP_URL)
            r.raise_for_status()
            hits = r.json().get("hits", [])
            return [
                {
                    "title": h.get("title", ""),
                    "url": h.get("url", ""),
                    "points": h.get("points", 0),
                    "comments": h.get("num_comments", 0),
                    "source": "hackernews",
                }
                for h in hits[:limit]
                if h.get("title")
            ]
    except Exception as e:
        logger.error("HN: failed to fetch top stories", {"error": str(e)})
        return []


async def fetch_stories_by_topic(query: str, limit: int = 8) -> list[dict]:
    """Searches HN stories matching a topic query."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                HN_SEARCH_URL,
                params={"query": query, "tags": "story", "hitsPerPage": limit},
            )
            r.raise_for_status()
            hits = r.json().get("hits", [])
            return [
                {
                    "title": h.get("title", ""),
                    "url": h.get("url", ""),
                    "points": h.get("points", 0),
                    "source": "hackernews",
                    "topic": query,
                }
                for h in hits
                if h.get("title")
            ]
    except Exception as e:
        logger.error("HN: failed to search stories", {"query": query, "error": str(e)})
        return []


async def fetch_who_is_hiring_thread() -> list[dict]:
    """
    Fetches the latest 'Ask HN: Who is hiring?' thread.
    Returns top-level comments as job postings.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Find the latest Who's Hiring thread
            search_r = await client.get(
                HN_SEARCH_URL,
                params={
                    "query": "Ask HN: Who is hiring?",
                    "tags": "ask_hn",
                    "hitsPerPage": 1,
                },
            )
            search_r.raise_for_status()
            hits = search_r.json().get("hits", [])
            if not hits:
                return []

            thread_id = hits[0].get("objectID")
            if not thread_id:
                return []

            # Fetch top-level comments (job postings)
            comments_r = await client.get(
                HN_SEARCH_URL,
                params={
                    "tags": f"comment,story_{thread_id}",
                    "hitsPerPage": 50,
                },
            )
            comments_r.raise_for_status()
            comments = comments_r.json().get("hits", [])
            return [
                {
                    "text": c.get("comment_text", ""),
                    "author": c.get("author", ""),
                    "source": "hn_who_is_hiring",
                }
                for c in comments
                if c.get("comment_text")
            ]
    except Exception as e:
        logger.error("HN: failed to fetch Who's Hiring thread", {"error": str(e)})
        return []
