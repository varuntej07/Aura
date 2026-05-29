"""Tests for the Brave Search primitive used by the real-time web_surf tool.

Locks the response mapping ({text, sources}), the recency->freshness behavior, and the
graceful-degradation contract (a flaky search must never raise into a chat/voice turn).
"""

from __future__ import annotations

import httpx
import pytest

from src.agents.data_fetchers import brave_search as brave_module
from src.agents.data_fetchers.brave_search import brave_search

# Bound before any monkeypatching so the mock factory can build a real client
# without recursing into its own patch.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


_SAMPLE_RESPONSE = {
    "web": {
        "results": [
            {
                "title": "India beat Australia",
                "url": "https://example.com/cricket",
                "description": "India won by <strong>5</strong> wickets.",
                "extra_snippets": ["Player of the match: Kohli.", "Series level 1-1."],
            },
            {
                "title": "Match report",
                "url": "https://example.com/cricket",  # duplicate url -> deduped
                "description": "Full scorecard inside.",
            },
        ]
    }
}


@pytest.fixture(autouse=True)
def _set_brave_key(monkeypatch):
    monkeypatch.setattr(brave_module.settings, "BRAVE_API_KEY", "test-key")
    # Each test starts with a clean cache so cache hits don't leak across tests.
    brave_module._cache.clear()


def _install_mock(monkeypatch, captured: dict, payload=None, status_code=200):
    """Point brave_search's httpx.AsyncClient at a MockTransport that records the request."""
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["params"] = dict(request.url.params)
        captured["token"] = request.headers.get("X-Subscription-Token")
        return httpx.Response(status_code, json=payload if payload is not None else _SAMPLE_RESPONSE)

    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(transport=transport, **kwargs)

    monkeypatch.setattr(brave_module.httpx, "AsyncClient", factory)


@pytest.mark.asyncio
async def test_maps_results_to_text_and_deduped_sources(monkeypatch):
    captured: dict = {}
    _install_mock(monkeypatch, captured)

    result = await brave_search("India cricket score", uid="u1", recency="any")

    assert "India beat Australia" in result["text"]
    assert "Kohli" in result["text"]  # extra_snippets folded in
    assert "<strong>" not in result["text"]  # html stripped
    assert result["sources"] == [{"title": "India beat Australia", "url": "https://example.com/cricket"}]
    assert result["cached"] is False
    assert captured["token"] == "test-key"
    assert captured["params"]["extra_snippets"] == "true"


@pytest.mark.asyncio
async def test_fresh_recency_sets_freshness_param(monkeypatch):
    captured: dict = {}
    _install_mock(monkeypatch, captured)

    await brave_search("breaking news today", uid="u1", recency="fresh")

    assert captured["params"].get("freshness") == "pd"


@pytest.mark.asyncio
async def test_any_recency_omits_freshness_param(monkeypatch):
    captured: dict = {}
    _install_mock(monkeypatch, captured)

    await brave_search("history of rome", uid="u1", recency="any")

    assert "freshness" not in captured["params"]


@pytest.mark.asyncio
async def test_non_200_degrades_to_empty_result(monkeypatch):
    captured: dict = {}
    _install_mock(monkeypatch, captured, payload={}, status_code=429)

    result = await brave_search("anything", uid="u1")

    assert result == {"text": "", "sources": [], "query": "anything", "cached": False}


@pytest.mark.asyncio
async def test_missing_api_key_raises(monkeypatch):
    monkeypatch.setattr(brave_module.settings, "BRAVE_API_KEY", "")

    with pytest.raises(ValueError, match="BRAVE_API_KEY"):
        await brave_search("anything", uid="u1")
