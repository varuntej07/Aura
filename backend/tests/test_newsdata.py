"""
Coverage for the newsdata.io fetcher.

Pins the things the pool relies on: DIRECT publisher URLs (not Google redirect
wrappers), HTML stripped, newsdata categories mapped onto the source vocab,
country mapped to a region, cross-category title de-dup, and fail-open behaviour
(unset key / 429 quota → empty, never a crash, so Google News carries the pool).
"""

from __future__ import annotations

from datetime import datetime

from src.agents.data_fetchers import newsdata
from src.config.settings import settings


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = b"x"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, responder):
        self._responder = responder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, _url, params=None):
        return self._responder(params or {})


async def test_returns_direct_urls_strips_html_maps_category_and_dedups(monkeypatch):
    monkeypatch.setattr(settings, "NEWSDATA_API_KEY", "k")

    def responder(params):
        cat = params["category"]
        return _Resp(200, {"status": "success", "results": [
            {
                "title": "Big story", "description": "<b>desc</b>",
                "link": "https://publisher.com/a", "source_name": "BBC",
                "country": ["india"], "category": [cat],
                "pubDate": "2026-06-12 10:00:00", "image_url": "https://img/1",
            },
            {  # same title → must de-dup away
                "title": "Big story", "description": "dup",
                "link": "https://publisher.com/dup", "category": [cat],
                "country": ["united states"],
            },
        ]})

    monkeypatch.setattr(newsdata.httpx, "AsyncClient", lambda *a, **k: _FakeClient(responder))

    items = await newsdata.fetch_newsdata_articles(categories=["technology"], limit_per_category=10)

    assert len(items) == 1                         # deduped by title
    it = items[0]
    assert it["url"] == "https://publisher.com/a"  # DIRECT publisher url, not a redirect
    assert it["body"] == "desc"                    # HTML stripped
    assert it["category"] == "tech"                # technology → source vocab
    assert it["region"] == "IN"                    # india → region code
    assert it["source_name"] == "BBC"
    assert isinstance(it["published_at"], datetime)


async def test_unset_key_returns_empty(monkeypatch):
    monkeypatch.setattr(settings, "NEWSDATA_API_KEY", "")
    assert await newsdata.fetch_newsdata_articles(categories=["technology"]) == []


async def test_429_quota_fails_open_to_empty(monkeypatch):
    monkeypatch.setattr(settings, "NEWSDATA_API_KEY", "k")
    monkeypatch.setattr(
        newsdata.httpx, "AsyncClient",
        lambda *a, **k: _FakeClient(lambda _p: _Resp(429, {})),
    )
    items = await newsdata.fetch_newsdata_articles(categories=["technology", "sports"])
    assert items == []


async def test_unknown_category_and_country_degrade_safely(monkeypatch):
    monkeypatch.setattr(settings, "NEWSDATA_API_KEY", "k")

    def responder(_params):
        return _Resp(200, {"status": "success", "results": [
            {"title": "X", "description": "d", "link": "https://p/x",
             "category": ["food"], "country": ["france"]},
        ]})

    monkeypatch.setattr(newsdata.httpx, "AsyncClient", lambda *a, **k: _FakeClient(responder))
    items = await newsdata.fetch_newsdata_articles(categories=["food"])
    assert len(items) == 1
    assert items[0]["category"] == "news"  # unknown category → safe general bucket
    assert items[0]["region"] == ""        # unknown country → region-agnostic
