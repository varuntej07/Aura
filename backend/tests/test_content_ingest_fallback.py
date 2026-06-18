"""Coverage for the tiered, self-healing ingest (the 2026-06-14 outage fix).

The pool used to ride on one effective source; when Google News 503'd from the
datacenter IP it drained until vector search returned [] for everyone and 0
notifications went out, while the only signal was an INFO line. These pin the new
contract:
  1. Brave (paid) fires ONLY when the pool is below the fresh floor after the free
     sources — a healthy hour never spends a credit.
  2. One free source failing does NOT trigger the paid fallback (newsdata carries it).
  3. When the pool is starved, Brave IS fired and refills it.
  4. When ALL sources fail, ingest SCREAMS (logger.error), so a starved pool can
     never again look healthy.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from src.services.signal_engine import content_ingest

_NEWSDATA_ITEM = {
    "title": "A real headline",
    "url": "https://publisher.example/a",
    "body": "A body with enough real text to be push-worthy and embeddable.",
    "category": "news",
    "sub_category": "",
    "region": "",
    "source_name": "Pub",
    "image_url": "",
    "published_at": None,
}
_BRAVE_ITEM = {**_NEWSDATA_ITEM, "title": "A fallback headline", "url": "https://pub/b"}


def _patch_sources(monkeypatch, *, newsdata, google, brave, fresh_values):
    """Wire the four ingest seams. ``fresh_values`` is the sequence
    count_fresh_candidates returns on its successive calls (before / after Brave)."""
    monkeypatch.setattr(content_ingest, "fetch_newsdata_articles", AsyncMock(return_value=newsdata))
    monkeypatch.setattr(content_ingest, "fetch_google_news", AsyncMock(return_value=google))
    brave_mock = AsyncMock(return_value=brave)
    monkeypatch.setattr(content_ingest, "fetch_brave_news", brave_mock)
    # add_candidates returns the number of items it was handed (good enough for the
    # summary; the real embed/write is covered elsewhere).
    monkeypatch.setattr(content_ingest, "add_candidates", AsyncMock(side_effect=lambda items: len(items)))
    monkeypatch.setattr(
        content_ingest, "count_fresh_candidates", AsyncMock(side_effect=list(fresh_values))
    )
    return brave_mock


async def test_brave_not_fired_when_pool_healthy(monkeypatch):
    floor = content_ingest.MIN_FRESH_POOL_FLOOR
    brave_mock = _patch_sources(
        monkeypatch, newsdata=[_NEWSDATA_ITEM], google=[], brave=[], fresh_values=[floor],
    )
    summary = await content_ingest.run_ingest()
    brave_mock.assert_not_awaited()          # no credit spent on a healthy hour
    assert summary.brave_fetched == 0
    assert summary.fresh_after == floor


async def test_one_free_source_failing_does_not_fire_brave(monkeypatch):
    """Google News raising is the common case; newsdata alone keeps the pool fed,
    so the paid fallback must stay unused."""
    floor = content_ingest.MIN_FRESH_POOL_FLOOR
    monkeypatch.setattr(
        content_ingest, "fetch_google_news", AsyncMock(side_effect=RuntimeError("503 from datacenter"))
    )
    monkeypatch.setattr(
        content_ingest, "fetch_newsdata_articles", AsyncMock(return_value=[_NEWSDATA_ITEM])
    )
    brave_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(content_ingest, "fetch_brave_news", brave_mock)
    monkeypatch.setattr(content_ingest, "add_candidates", AsyncMock(side_effect=lambda items: len(items)))
    monkeypatch.setattr(content_ingest, "count_fresh_candidates", AsyncMock(return_value=floor))

    summary = await content_ingest.run_ingest()
    brave_mock.assert_not_awaited()
    assert summary.newsdata_fetched == 1


async def test_brave_fires_and_refills_when_pool_starved(monkeypatch):
    """Both free sources empty + pool below floor → Brave fires and refills."""
    floor = content_ingest.MIN_FRESH_POOL_FLOOR
    brave_mock = _patch_sources(
        monkeypatch, newsdata=[], google=[], brave=[_BRAVE_ITEM],
        fresh_values=[0, floor],   # starved before Brave, healthy after
    )
    summary = await content_ingest.run_ingest()
    brave_mock.assert_awaited_once()
    assert summary.brave_fetched == 1
    assert summary.fresh_after == floor


async def test_all_sources_empty_screams(monkeypatch):
    """The fail-loud guard: 0 fresh after every source must log an ERROR, not whisper."""
    errors: list[str] = []
    monkeypatch.setattr(content_ingest.logger, "error", lambda msg, *a, **k: errors.append(msg))
    _patch_sources(
        monkeypatch, newsdata=[], google=[], brave=[], fresh_values=[0, 0],
    )
    summary = await content_ingest.run_ingest()
    assert summary.fresh_after == 0
    assert any("ZERO fresh candidates" in e for e in errors)
