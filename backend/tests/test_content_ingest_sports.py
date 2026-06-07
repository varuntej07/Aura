"""Coverage for the (now free) sports ingest path.

The paid Gemini-grounded league search was removed — broader sports headlines now
arrive via Google News RSS in run_ingest, and run_sports_ingest keeps only the free
cricbuzz live scores. These pin that the grounded path is gone and cricbuzz still flows.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from src.services.signal_engine import content_ingest


async def test_run_sports_ingest_uses_only_free_cricbuzz(monkeypatch):
    """Sports ingest maps cricbuzz live matches and embeds them — no web search."""
    live = [{"team1": "IND", "team2": "AUS", "score": "201/3", "match": "1st ODI"}]
    monkeypatch.setattr(content_ingest, "fetch_live_matches", AsyncMock(return_value=live))
    embed = AsyncMock(return_value=1)
    monkeypatch.setattr(content_ingest, "_embed_and_write", embed)

    summary = await content_ingest.run_sports_ingest()

    assert summary.live_cricket_fetched == 1
    embed.assert_awaited_once()
    candidates = embed.await_args.args[0]
    assert candidates and candidates[0].source == "cricbuzz_live"


def test_grounded_sports_search_symbols_removed():
    """The paid Gemini-grounded sports search must stay gone (cost regression guard)."""
    assert not hasattr(content_ingest, "_safe_sports_web_search")
    assert not hasattr(content_ingest, "SPORTS_WEB_SEARCH_QUERIES")
