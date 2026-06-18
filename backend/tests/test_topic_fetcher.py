"""Coverage for the tiered topic fetch chain (rss -> newsdata -> brave -> grounded).

Pins the two guarantees a tracker's live update depends on: the chain FALLS THROUGH
to the next source when a tier is empty / too short / raising, and reports TIER_NONE
(never raising) when every source fails — so a single provider outage degrades to a
skipped checkpoint, not a crash.
"""

from __future__ import annotations

from src.services.tracking import topic_fetcher
from src.services.tracking.fields import (
    TIER_BRAVE,
    TIER_GROUNDED,
    TIER_NEWSDATA,
    TIER_NONE,
    TIER_RSS,
)

_LONG = "USA beat Australia 2-1 in a thriller, advancing to the Round of 16 today."


def _stub(text, sources=None):
    async def _f(query, locale):
        return text, (sources or [])
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
