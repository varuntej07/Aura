"""
Fetches recent papers from arXiv using the public API.
No API key required. Used by BytePulse for AI/ML research content.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx

from ...lib.logger import logger

ARXIV_API_URL = "https://export.arxiv.org/api/query"
NS = "{http://www.w3.org/2005/Atom}"


async def fetch_recent_papers(
    categories: list[str] | None = None,
    max_results: int = 10,
) -> list[dict]:
    """
    Fetches the most recent arXiv papers in the given categories.
    Defaults to cs.LG (machine learning) and cs.AI (artificial intelligence).
    """
    cats = categories or ["cs.LG", "cs.AI"]
    search_query = " OR ".join(f"cat:{c}" for c in cats)
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(
                ARXIV_API_URL,
                params={
                    "search_query": search_query,
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                    "max_results": max_results,
                },
            )
            r.raise_for_status()

        root = ET.fromstring(r.text)
        papers = []
        for entry in root.findall(f"{NS}entry"):
            title_el = entry.find(f"{NS}title")
            summary_el = entry.find(f"{NS}summary")
            id_el = entry.find(f"{NS}id")
            if title_el is None:
                continue
            title = (title_el.text or "").replace("\n", " ").strip()
            summary = (summary_el.text or "").replace("\n", " ").strip()[:300]
            url = (id_el.text or "").strip()
            papers.append(
                {
                    "title": title,
                    "summary": summary,
                    "url": url,
                    "source": "arxiv",
                }
            )
        return papers

    except Exception as e:
        logger.error("arXiv: failed to fetch papers", {"error": str(e)})
        return []
