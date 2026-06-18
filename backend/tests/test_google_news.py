"""Coverage for the free Google News RSS fetcher that replaced paid grounding.

Pins three things the content pool relies on: titles are deduped across feeds,
HTML is stripped from the body before embedding, and each item carries the
content-pool category from its feed so the recommender's affinities keep working.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _stub_feed_http(monkeypatch):
    """Feed bytes are now fetched through a bounded httpx.get before feedparser
    parses them, so unit tests must not touch the network. Stub the fetch for every
    test (each test still controls the parsed result via its own feedparser.parse
    stub). The timeout test below overrides this with its own httpx.get stub."""
    from src.agents.data_fetchers import google_news

    class _Resp:
        def __init__(self, url: str):
            # Carry the request URL as the body so per-feed feedparser.parse stubs
            # that key off the URL still produce distinct items — feedparser.parse
            # now receives bytes (the fetched body), not the URL string.
            self.content = url.encode()

        def raise_for_status(self):
            return None

    monkeypatch.setattr(google_news.httpx, "get", lambda url, *a, **k: _Resp(url))


async def test_fetch_google_news_dedups_strips_html_and_tags_category(monkeypatch):
    import feedparser

    class _Entry:
        def __init__(self, title, summary="", link=""):
            self.title, self.summary, self.link = title, summary, link

    class _Feed:
        def __init__(self, entries):
            self.entries = entries

    # Every feed returns the same headline → it must collapse to one item.
    def fake_parse(_url):
        return _Feed([_Entry("Shared headline", "<b>cleaned</b>", "https://x/1")])

    monkeypatch.setattr(feedparser, "parse", fake_parse)

    from src.agents.data_fetchers import google_news

    items = await google_news.fetch_google_news(limit_per_feed=5)

    assert len(items) == 1                  # deduped by title across all feeds
    assert items[0]["body"] == "cleaned"    # HTML stripped before embedding
    assert items[0]["category"] == "news"   # carried from the first (WORLD) feed
    assert items[0]["url"] == "https://x/1"


async def test_entertainment_and_health_topics_are_fetched(monkeypatch):
    """The de-bias additions (entertainment + health) must reach the pool."""
    import feedparser

    from src.agents.data_fetchers import google_news

    class _Entry:
        def __init__(self, title, summary="", link=""):
            self.title, self.summary, self.link = title, summary, link

    class _Feed:
        def __init__(self, entries):
            self.entries = entries

    # Give every feed a URL-unique headline so nothing de-dups away.
    def fake_parse(url):
        return _Feed([_Entry(f"headline for {url}", "body", "https://x")])

    monkeypatch.setattr(feedparser, "parse", fake_parse)

    items = await google_news.fetch_google_news(limit_per_feed=1)
    categories = {i["category"] for i in items}
    assert "entertainment" in categories
    assert "health" in categories
    assert "nation" in categories


async def test_multi_locale_feeds_tag_region(monkeypatch):
    """NATION/WORLD/etc are fetched per locale edition and tagged with region."""
    import feedparser

    from src.agents.data_fetchers import google_news

    class _Entry:
        def __init__(self, title, summary="", link=""):
            self.title, self.summary, self.link = title, summary, link

    class _Feed:
        def __init__(self, entries):
            self.entries = entries

    def fake_parse(url):
        return _Feed([_Entry(f"headline for {url}", "body", "https://x")])

    monkeypatch.setattr(feedparser, "parse", fake_parse)

    items = await google_news.fetch_google_news(limit_per_feed=1)
    regions = {i["region"] for i in items}
    # Default locales en-US,en-IN → region-sensitive feeds carry IN, not just US.
    # Region-agnostic feeds (tech/science) carry "".
    assert {"US", "IN"}.issubset(regions)


async def test_fetch_google_news_skips_a_broken_feed(monkeypatch):
    """One feed raising must not sink the rest — ingest stays resilient."""
    import feedparser

    class _Entry:
        title = "Good item"
        summary = ""
        link = ""

    class _Feed:
        entries = [_Entry()]

    calls = {"n": 0}

    def flaky_parse(_url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("feed down")
        return _Feed()

    monkeypatch.setattr(feedparser, "parse", flaky_parse)

    from src.agents.data_fetchers import google_news

    items = await google_news.fetch_google_news(limit_per_feed=5)

    assert any(i["title"] == "Good item" for i in items)


async def test_a_hung_feed_is_bounded_and_skipped(monkeypatch):
    """A feed whose fetch times out is skipped (bounded by an explicit per-feed
    timeout), never hangs the ingest, and the remaining feeds still return items.
    Pins the fix for the unbounded urllib wait feedparser.parse(url) used to have:
    with ~14 feeds (2 locales), one stalled Google News endpoint must not stall the
    whole ingest."""
    import httpx as _httpx

    import feedparser

    from src.agents.data_fetchers import google_news

    class _Entry:
        title, summary, link = "a shared headline", "", "https://x"

    class _Feed:
        entries = [_Entry()]

    class _Resp:
        content = b"<rss/>"

        def raise_for_status(self):
            return None

    timeouts_seen: list = []

    def fake_get(url, **kwargs):
        # Every fetch must receive a finite, positive timeout — the bound we added.
        timeouts_seen.append(kwargs.get("timeout"))
        # Make the HEALTH editions hang deterministically (URL-based, not a racy
        # counter — _parse_one runs across a thread pool).
        if "topic/HEALTH" in url:
            raise _httpx.TimeoutException("read timed out")
        return _Resp()

    monkeypatch.setattr(google_news.httpx, "get", fake_get)
    monkeypatch.setattr(feedparser, "parse", lambda _raw: _Feed())

    items = await google_news.fetch_google_news(limit_per_feed=1)

    assert timeouts_seen, "httpx.get was never called — the bounded fetch path is gone"
    assert all(isinstance(t, (int, float)) and t > 0 for t in timeouts_seen), (
        "every per-feed fetch must pass a finite positive timeout (the unbounded-wait fix)"
    )
    # The hung HEALTH feeds were skipped, not fatal: the other feeds still produced
    # an item, so the ingest survives a stalled endpoint.
    assert items, "a timed-out feed must not sink the whole ingest"
