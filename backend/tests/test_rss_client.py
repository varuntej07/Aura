"""
Tests for src/services/daily_notification/rss_client.py

Covers: fetch_news, _fetch_news_sync (all three fallback levels),
_fetch_from_google_news, _parse_feed_entries, _parse_published, _empty_news_item,
NewsItem.to_dict.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_entry(title="Test story", summary="Test summary", published_parsed=None):
    entry = MagicMock()
    entry.title = title
    entry.summary = summary
    entry.published_parsed = published_parsed
    return entry


def _make_feed(entries=None):
    feed = MagicMock()
    feed.entries = entries or []
    return feed


class TestFetchNews:
    @pytest.mark.asyncio
    async def test_async_wrapper_returns_sync_result(self):
        """fetch_news delegates to asyncio.to_thread(_fetch_news_sync)."""
        from src.services.daily_notification.rss_client import fetch_news

        fake_result = [{"title": "T", "summary": "S", "published_at": ""}]
        with patch("src.services.daily_notification.rss_client._fetch_news_sync", return_value=fake_result):
            with patch("asyncio.to_thread", new=AsyncMock(return_value=fake_result)):
                result = await fetch_news(["nutrition"])

        assert result == fake_result


class TestNewsItem:
    def test_to_dict_returns_correct_keys(self):
        from src.services.daily_notification.rss_client import NewsItem
        item = NewsItem("Title", "Summary", "May 03, 2026")
        d = item.to_dict()
        assert d == {"title": "Title", "summary": "Summary", "published_at": "May 03, 2026"}


class TestParsePublished:
    def test_with_published_parsed_tuple(self):
        from src.services.daily_notification.rss_client import _parse_published
        entry = MagicMock()
        entry.published_parsed = (2026, 5, 3, 8, 0, 0, 0, 0, 0)
        result = _parse_published(entry)
        assert "2026" in result
        assert "May" in result

    def test_without_published_parsed_returns_today(self):
        from src.services.daily_notification.rss_client import _parse_published
        entry = MagicMock()
        entry.published_parsed = None
        result = _parse_published(entry)
        assert len(result) > 0

    def test_exception_returns_today(self):
        from src.services.daily_notification.rss_client import _parse_published
        entry = MagicMock()
        entry.published_parsed = "not-a-tuple"  # will cause datetime(*t[:6]) to fail
        result = _parse_published(entry)
        assert len(result) > 0


class TestParseFeedEntries:
    def test_parses_entries_with_titles(self):
        from src.services.daily_notification.rss_client import _parse_feed_entries
        entries = [_make_entry("Story A"), _make_entry("Story B")]
        result = _parse_feed_entries(entries)
        assert len(result) == 2
        assert result[0].title == "Story A"

    def test_skips_entries_without_title(self):
        from src.services.daily_notification.rss_client import _parse_feed_entries
        entries = [_make_entry(""), _make_entry("Valid")]
        result = _parse_feed_entries(entries)
        assert len(result) == 1
        assert result[0].title == "Valid"

    def test_empty_entries_returns_empty(self):
        from src.services.daily_notification.rss_client import _parse_feed_entries
        assert _parse_feed_entries([]) == []


class TestFetchFromGoogleNews:
    def test_successful_fetch_returns_items(self):
        from src.services.daily_notification.rss_client import _fetch_from_google_news
        mock_fp = MagicMock()
        mock_fp.parse.return_value = _make_feed([_make_entry("Health news")])
        result = _fetch_from_google_news(mock_fp, "nutrition")
        assert len(result) == 1
        assert result[0].title == "Health news"

    def test_exception_returns_empty(self):
        from src.services.daily_notification.rss_client import _fetch_from_google_news
        mock_fp = MagicMock()
        mock_fp.parse.side_effect = Exception("network error")
        result = _fetch_from_google_news(mock_fp, "nutrition")
        assert result == []


class TestFetchNewsSync:
    def test_feedparser_not_installed_returns_placeholder(self):
        from src.services.daily_notification.rss_client import _fetch_news_sync
        import sys
        with patch.dict(sys.modules, {"feedparser": None}):
            result = _fetch_news_sync([])
        assert len(result) == 1
        assert "title" in result[0]

    def test_level1_keywords_sufficient(self):
        """Level 1 returns ≥2 results → use them, skip levels 2 and 3."""
        from src.services.daily_notification.rss_client import _fetch_news_sync
        mock_fp = MagicMock()
        mock_fp.parse.return_value = _make_feed([
            _make_entry("Story 1"), _make_entry("Story 2"), _make_entry("Story 3"),
        ])
        with patch("src.services.daily_notification.rss_client.feedparser", mock_fp, create=True):
            with patch.dict("sys.modules", {"feedparser": mock_fp}):
                result = _fetch_news_sync(["nutrition"])
        # Should have called parse once (for keyword query) and returned ≤5 items
        assert 1 <= len(result) <= 5

    def test_level2_fallback_when_level1_thin(self):
        """Level 1 returns <2 results → fall through to level 2 broad query."""
        from src.services.daily_notification.rss_client import _fetch_news_sync, _BROAD_HEALTH_QUERY, _GOOGLE_NEWS_RSS_URL
        from urllib.parse import quote_plus

        keyword_url = _GOOGLE_NEWS_RSS_URL.format(query=quote_plus("nutrition"))
        broad_url = _GOOGLE_NEWS_RSS_URL.format(query=quote_plus(_BROAD_HEALTH_QUERY))

        mock_fp = MagicMock()

        def parse_side_effect(url):
            if url == keyword_url:
                return _make_feed([_make_entry("Only one")])
            if url == broad_url:
                return _make_feed([_make_entry("Broad 1"), _make_entry("Broad 2")])
            return _make_feed([])

        mock_fp.parse.side_effect = parse_side_effect

        with patch.dict("sys.modules", {"feedparser": mock_fp}):
            result = _fetch_news_sync(["nutrition"])

        titles = [r["title"] for r in result]
        assert any("Broad" in t for t in titles)

    def test_level3_fallback_when_levels_1_and_2_thin(self):
        """Levels 1 and 2 both return <2 results → try static curated feeds."""
        from src.services.daily_notification.rss_client import _fetch_news_sync, _FALLBACK_FEED_URLS

        mock_fp = MagicMock()

        def parse_side_effect(url):
            if url in _FALLBACK_FEED_URLS:
                return _make_feed([_make_entry("Curated 1"), _make_entry("Curated 2")])
            return _make_feed([_make_entry("Only one")])  # level 1 and 2 return <2

        mock_fp.parse.side_effect = parse_side_effect

        with patch.dict("sys.modules", {"feedparser": mock_fp}):
            result = _fetch_news_sync(["nutrition"])

        titles = [r["title"] for r in result]
        assert any("Curated" in t for t in titles)

    def test_all_levels_fail_returns_placeholder(self):
        """If all three levels fail or return empty, returns placeholder item."""
        from src.services.daily_notification.rss_client import _fetch_news_sync

        mock_fp = MagicMock()
        mock_fp.parse.return_value = _make_feed([])  # always empty

        with patch.dict("sys.modules", {"feedparser": mock_fp}):
            result = _fetch_news_sync(["nutrition"])

        assert len(result) == 1
        assert "title" in result[0]

    def test_level3_exception_continues_to_next_feed(self):
        """A failing static feed must be skipped; next feed is tried."""
        from src.services.daily_notification.rss_client import _fetch_news_sync, _FALLBACK_FEED_URLS

        mock_fp = MagicMock()
        call_count = [0]

        def parse_side_effect(url):
            if url in _FALLBACK_FEED_URLS:
                call_count[0] += 1
                if call_count[0] == 1:
                    raise Exception("feed down")
                return _make_feed([_make_entry("Backup 1"), _make_entry("Backup 2")])
            return _make_feed([])  # levels 1+2 thin

        mock_fp.parse.side_effect = parse_side_effect

        with patch.dict("sys.modules", {"feedparser": mock_fp}):
            result = _fetch_news_sync(["nutrition"])

        titles = [r["title"] for r in result]
        assert any("Backup" in t for t in titles)

    def test_no_keywords_skips_level1_goes_to_level2(self):
        """Empty keywords list must skip Level 1 keyword query."""
        from src.services.daily_notification.rss_client import _fetch_news_sync, _BROAD_HEALTH_QUERY, _GOOGLE_NEWS_RSS_URL
        from urllib.parse import quote_plus

        broad_url = _GOOGLE_NEWS_RSS_URL.format(query=quote_plus(_BROAD_HEALTH_QUERY))
        mock_fp = MagicMock()

        def parse_side_effect(url):
            if url == broad_url:
                return _make_feed([_make_entry("L2 Story 1"), _make_entry("L2 Story 2")])
            return _make_feed([])

        mock_fp.parse.side_effect = parse_side_effect

        with patch.dict("sys.modules", {"feedparser": mock_fp}):
            result = _fetch_news_sync([])

        titles = [r["title"] for r in result]
        assert any("L2 Story" in t for t in titles)


class TestEmptyNewsItem:
    def test_returns_dict_with_required_keys(self):
        from src.services.daily_notification.rss_client import _empty_news_item
        item = _empty_news_item()
        assert "title" in item
        assert "summary" in item
        assert "published_at" in item
        assert len(item["title"]) > 0
