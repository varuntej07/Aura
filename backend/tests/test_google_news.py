"""Coverage for the free Google News RSS fetcher that replaced paid grounding.

Pins three things the content pool relies on: titles are deduped across feeds,
HTML is stripped from the body before embedding, and each item carries the
content-pool category from its feed so the recommender's affinities keep working.
"""

from __future__ import annotations


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
    # Default locales en-US,en-IN,en-GB → region-sensitive feeds carry IN and GB,
    # not just US. Region-agnostic feeds (tech/science/keyword) carry "".
    assert {"US", "IN", "GB"}.issubset(regions)


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
