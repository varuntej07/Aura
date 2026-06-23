"""Coverage for the tiered topic fetch chain (rss -> newsdata -> brave -> grounded).

Pins the two guarantees a tracker's live update depends on: the chain FALLS THROUGH
to the next source when a tier is empty / too short / raising, and reports TIER_NONE
(never raising) when every source fails — so a single provider outage degrades to a
skipped checkpoint, not a crash.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.services.tracking import topic_fetcher
from src.services.tracking.fields import (
    TIER_BRAVE,
    TIER_GROUNDED,
    TIER_NEWSDATA,
    TIER_NONE,
    TIER_RSS,
)

_LONG = "USA beat Australia 2-1 in a thriller, advancing to the Round of 16 today."


def _stub(text, sources=None, published=None):
    # Tiers return (text, sources, latest_published) — the freshest article pubDate
    # (UTC) or None when the source carries no date (brave/grounded).
    async def _f(query, locale):
        return text, (sources or []), published
    return _f


def _raises():
    async def _f(query, locale):
        raise RuntimeError("provider exploded")
    return _f


async def test_first_usable_tier_serves(monkeypatch):
    monkeypatch.setitem(topic_fetcher._TIER_FETCHERS, TIER_RSS, _stub(_LONG, [{"title": "t", "url": "u"}]))
    res = await topic_fetcher.fetch_topic("USA world cup", use_cache=False)
    assert res.tier == TIER_RSS
    assert res.ok
    assert "Australia" in res.text


async def test_falls_through_empty_and_too_short(monkeypatch):
    monkeypatch.setitem(topic_fetcher._TIER_FETCHERS, TIER_RSS, _stub(""))            # empty -> skip
    monkeypatch.setitem(topic_fetcher._TIER_FETCHERS, TIER_NEWSDATA, _stub("0-0"))    # < MIN chars -> skip
    monkeypatch.setitem(topic_fetcher._TIER_FETCHERS, TIER_BRAVE, _stub(_LONG))
    res = await topic_fetcher.fetch_topic("x", use_cache=False)
    assert res.tier == TIER_BRAVE


async def test_raising_tier_falls_through(monkeypatch):
    monkeypatch.setitem(topic_fetcher._TIER_FETCHERS, TIER_RSS, _raises())
    monkeypatch.setitem(topic_fetcher._TIER_FETCHERS, TIER_NEWSDATA, _stub(_LONG))
    res = await topic_fetcher.fetch_topic("x", use_cache=False)
    assert res.tier == TIER_NEWSDATA


async def test_latest_published_is_surfaced_for_freshness_gate(monkeypatch):
    # The serving tier's freshest article date must ride out on the FetchResult so the
    # orchestrator's freshness gate can drop a stale-by-a-day tracker push.
    published = datetime(2026, 6, 22, 9, 0, tzinfo=UTC)
    monkeypatch.setitem(topic_fetcher._TIER_FETCHERS, TIER_RSS, _stub(_LONG, published=published))
    monkeypatch.setattr(topic_fetcher, "_cache", {})
    res = await topic_fetcher.fetch_topic("USA world cup", use_cache=False)
    assert res.tier == TIER_RSS
    assert res.latest_published == published


async def test_latest_published_none_when_source_has_no_date(monkeypatch):
    # A dateless fallback tier (brave/grounded) must surface None — the gate then can't
    # drop it, which is correct: never suppress a tier for lacking a date it never had.
    monkeypatch.setitem(topic_fetcher._TIER_FETCHERS, TIER_RSS, _stub(_LONG, published=None))
    monkeypatch.setattr(topic_fetcher, "_cache", {})
    res = await topic_fetcher.fetch_topic("USA world cup", use_cache=False)
    assert res.latest_published is None


def test_struct_to_utc_and_newsdata_parse():
    import time as _time

    # feedparser published_parsed is a UTC struct_time.
    struct = _time.strptime("2026-06-22 09:00:00", "%Y-%m-%d %H:%M:%S")
    assert topic_fetcher._struct_to_utc(struct) == datetime(2026, 6, 22, 9, 0, tzinfo=UTC)
    assert topic_fetcher._struct_to_utc(None) is None
    # newsdata pubDate is 'YYYY-MM-DD HH:MM:SS' UTC.
    assert topic_fetcher._parse_newsdata_date("2026-06-22 09:00:00") == datetime(
        2026, 6, 22, 9, 0, tzinfo=UTC
    )
    assert topic_fetcher._parse_newsdata_date("") is None
    assert topic_fetcher._parse_newsdata_date("not-a-date") is None


async def test_all_tiers_fail_returns_tier_none(monkeypatch):
    for tier in (TIER_RSS, TIER_NEWSDATA, TIER_BRAVE, TIER_GROUNDED):
        monkeypatch.setitem(topic_fetcher._TIER_FETCHERS, tier, _stub(""))
    res = await topic_fetcher.fetch_topic("x", use_cache=False)
    assert res.tier == TIER_NONE
    assert not res.ok
    assert res.text == ""


# ── locale resolution + localized Google News edition (the geographic fix) ────
def test_build_locale_defaults_to_us_en():
    loc = topic_fetcher._build_locale(None, None)
    assert (loc.country, loc.language) == ("US", "en")
    assert topic_fetcher._build_locale("", "") == loc


def test_build_locale_normalizes_case_and_strips_junk():
    loc = topic_fetcher._build_locale("in", "TE")
    assert (loc.country, loc.language) == ("IN", "te")
    # Garbage codes fall back rather than producing a broken edition.
    assert topic_fetcher._build_locale("1!", "##") == topic_fetcher._build_locale(None, None)
    # Over-long codes are truncated to two letters.
    assert topic_fetcher._build_locale("BRA", "por").country == "BR"


def test_google_news_url_localizes_edition():
    url = topic_fetcher._google_news_url("pushpa 2", topic_fetcher._build_locale("in", "te"))
    assert "hl=te-IN" in url and "gl=IN" in url and "ceid=IN:te" in url and "q=pushpa+2" in url


def test_google_news_url_default_matches_legacy_us_english():
    # The default edition must be byte-for-byte the old hardcoded one (no US regression).
    url = topic_fetcher._google_news_url("world cup", topic_fetcher._build_locale(None, None))
    assert url == (
        "https://news.google.com/rss/search?q=world+cup&hl=en-US&gl=US&ceid=US:en"
    )


async def test_locale_is_part_of_cache_identity(monkeypatch):
    # Same query, different region => a genuinely different fetch, must not collide.
    monkeypatch.setitem(topic_fetcher._TIER_FETCHERS, TIER_RSS, _stub(_LONG))
    monkeypatch.setattr(topic_fetcher, "_cache", {})
    await topic_fetcher.fetch_topic("election", country="US", language="en", use_cache=True)
    await topic_fetcher.fetch_topic("election", country="IN", language="hi", use_cache=True)
    keys = set(topic_fetcher._cache.keys())
    assert any(k.endswith("|US:en") for k in keys)
    assert any(k.endswith("|IN:hi") for k in keys)
