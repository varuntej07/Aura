"""Regression tests for the sports web-search ingest path.

These lock in the fix for the every-30-min timeout storm: background web_surf calls
must use the longer ingest timeout budget, and the fan-out must be concurrency-capped
so queries don't starve the thread pool and time out while queued.
"""

from __future__ import annotations

import asyncio

from src.services.signal_engine import content_ingest
from src.agents.data_fetchers.web_surf import _INGEST_REQUEST_TIMEOUT_S


async def test_sports_search_uses_ingest_timeout_budget(monkeypatch):
    """The background path must pass the longer ingest timeout, not the tight 6s default."""
    captured: dict[str, float] = {}

    async def fake_web_search(query: str, uid: str, timeout_s: float) -> str:
        captured["timeout_s"] = timeout_s
        return "some result text"

    monkeypatch.setattr(content_ingest, "web_search", fake_web_search)

    semaphore = asyncio.Semaphore(content_ingest.SPORTS_WEB_SEARCH_CONCURRENCY)
    await content_ingest._safe_sports_web_search("IPL score today", "ipl", semaphore)

    assert captured["timeout_s"] == _INGEST_REQUEST_TIMEOUT_S


async def test_sports_search_fan_out_respects_concurrency_cap(monkeypatch):
    """All queries fire at once, but the semaphore must keep in-flight calls <= cap."""
    in_flight = 0
    max_in_flight = 0

    async def fake_web_search(query: str, uid: str, timeout_s: float) -> str:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(0.02)  # hold the slot so overlap is observable
            return "result"
        finally:
            in_flight -= 1

    monkeypatch.setattr(content_ingest, "web_search", fake_web_search)

    semaphore = asyncio.Semaphore(content_ingest.SPORTS_WEB_SEARCH_CONCURRENCY)
    await asyncio.gather(*[
        content_ingest._safe_sports_web_search(query, sub_cat, semaphore)
        for query, sub_cat in content_ingest.SPORTS_WEB_SEARCH_QUERIES
    ])

    assert len(content_ingest.SPORTS_WEB_SEARCH_QUERIES) > content_ingest.SPORTS_WEB_SEARCH_CONCURRENCY
    assert max_in_flight <= content_ingest.SPORTS_WEB_SEARCH_CONCURRENCY


async def test_sports_search_swallows_failure_and_returns_empty(monkeypatch):
    """A failing search must not propagate — it returns [] so the rest of ingest survives."""
    async def failing_web_search(query: str, uid: str, timeout_s: float) -> str:
        raise TimeoutError()  # the empty-string error that started this investigation

    monkeypatch.setattr(content_ingest, "web_search", failing_web_search)

    semaphore = asyncio.Semaphore(content_ingest.SPORTS_WEB_SEARCH_CONCURRENCY)
    result = await content_ingest._safe_sports_web_search("x", "ipl", semaphore)

    assert result == []
