"""Coverage for the Brave News fallback fetcher.

Brave News is the pool's datacenter-reliable paid backstop (fired only when the free
sources leave the pool starved). These pin the three things content_ingest relies on:
the Brave JSON shape maps to the newsdata-compatible candidate dict, results dedupe by
title across the per-locale query fan-out, and a missing key is a safe no-op (never a
crash) so dev / an unconfigured env simply has no fallback.
"""

from __future__ import annotations

from datetime import datetime

from src.agents.data_fetchers import brave_news

_ONE_RESULT = {
    "results": [
        {
            "type": "news_result",
            "title": "Big global story",
            "url": "https://publisher.example/story",
            "description": "What happened",
            "page_age": "2026-06-14T07:38:58",
            "profile": {"name": "Publisher"},
            "thumbnail": {"src": "https://img.example/t.jpg"},
            "extra_snippets": ["extra detail one", "extra detail two"],
        }
    ]
}


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.content = b"non-empty"

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """Stands in for httpx.AsyncClient — every .get returns the same canned payload."""

    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        return _FakeResp(self._payload)


async def test_maps_brave_shape_and_dedups_by_title(monkeypatch):
    monkeypatch.setattr(brave_news.settings, "BRAVE_API_KEY", "test-key")
    monkeypatch.setattr(brave_news.httpx, "AsyncClient", lambda *a, **k: _FakeClient(_ONE_RESULT))

    items = await brave_news.fetch_brave_news(limit_per_query=5)

    # Every one of the 16 locale/category queries returns the same headline → one item.
    assert len(items) == 1
    item = items[0]
    assert item["title"] == "Big global story"
    assert item["url"] == "https://publisher.example/story"          # direct publisher URL
    assert "What happened" in item["body"] and "extra detail one" in item["body"]
    assert item["source_name"] == "Publisher"
    assert item["image_url"] == "https://img.example/t.jpg"
    assert isinstance(item["published_at"], datetime)                # page_age parsed
    assert item["published_at"].tzinfo is not None                   # tz-aware UTC


async def test_missing_key_is_a_safe_noop(monkeypatch):
    monkeypatch.setattr(brave_news.settings, "BRAVE_API_KEY", "")
    # No HTTP stub: if it tried to call out, this would blow up. It must not.
    assert await brave_news.fetch_brave_news() == []


async def test_one_failing_query_does_not_sink_the_rest(monkeypatch):
    monkeypatch.setattr(brave_news.settings, "BRAVE_API_KEY", "test-key")

    class _FlakyClient(_FakeClient):
        calls = {"n": 0}

        async def get(self, url, params=None, headers=None):
            _FlakyClient.calls["n"] += 1
            if _FlakyClient.calls["n"] == 1:
                raise RuntimeError("brave 503")
            return _FakeResp(_ONE_RESULT)

    monkeypatch.setattr(brave_news.httpx, "AsyncClient", lambda *a, **k: _FlakyClient(_ONE_RESULT))

    items = await brave_news.fetch_brave_news(limit_per_query=5)
    # One query raised, the rest still produced the item → fail-open per query.
    assert len(items) == 1


def test_query_specs_are_credit_bounded():
    specs = brave_news._build_query_specs()
    assert 0 < len(specs) <= brave_news._MAX_QUERIES_PER_RUN  # each spec = 1 credit
    # Region-sensitive queries carry a region tag; region-agnostic ones don't.
    assert any(s.region for s in specs) and any(not s.region for s in specs)
