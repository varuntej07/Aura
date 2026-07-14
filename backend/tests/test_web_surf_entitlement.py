"""Regression tests for web_surf entitlement metering (Step 2.1).

The bug: the free-tier daily counter was incremented BEFORE the cache was consulted,
so a cached repeat query still burned a search. The fix: a cache hit is served without
touching the counter; only a real network call (cache miss) goes through the unchanged
atomic increment, which stays the hard cap.
"""

from __future__ import annotations

import pytest

import src.agents.data_fetchers.brave_search as brave_module
import src.services.entitlement as entitlement_module
from src.services.tool_executor import ToolExecutor


@pytest.fixture
def executor():
    return ToolExecutor("user-1", created_via="voice")


def _patch(monkeypatch, *, peek, tier, increment, brave):
    # _web_surf imports these names inside the method, so patch them on their source modules.
    monkeypatch.setattr(brave_module, "peek_cache", peek)
    monkeypatch.setattr(brave_module, "brave_search", brave)
    monkeypatch.setattr(entitlement_module, "get_user_effective_tier", tier)
    monkeypatch.setattr(entitlement_module, "check_and_increment_daily_web_surf_usage", increment)


async def _tier_free(_uid):
    return "free"


async def _brave_must_not_run(*_a, **_k):
    raise AssertionError("brave_search must not run")


async def test_cache_hit_does_not_increment_counter(executor, monkeypatch):
    calls = {"increment": 0}

    def peek(query, *, uid, recency="any"):
        return {"text": "cached answer", "sources": [], "query": query, "cached": True}

    async def increment(_uid):
        calls["increment"] += 1
        return True, 1

    _patch(monkeypatch, peek=peek, tier=_tier_free, increment=increment, brave=_brave_must_not_run)

    result = await executor._web_surf({"query": "what's the score"})

    assert result["cached"] is True
    assert result["text"] == "cached answer"
    assert calls["increment"] == 0  # the whole point: a cache hit burns no daily search


async def test_cache_miss_free_tier_increments_then_searches(executor, monkeypatch):
    calls = {"increment": 0, "brave": 0}

    def peek(query, *, uid, recency="any"):
        return None

    async def increment(_uid):
        calls["increment"] += 1
        return True, 1

    async def brave(query, *, uid, recency="any"):
        calls["brave"] += 1
        return {"text": "live answer", "sources": [], "query": query, "cached": False}

    _patch(monkeypatch, peek=peek, tier=_tier_free, increment=increment, brave=brave)

    result = await executor._web_surf({"query": "latest news"})

    assert result["text"] == "live answer"
    assert calls["increment"] == 1
    assert calls["brave"] == 1


async def test_free_tier_at_cap_blocks_without_searching(executor, monkeypatch):
    def peek(query, *, uid, recency="any"):
        return None

    async def increment(_uid):
        return False, 25  # at the cap

    _patch(monkeypatch, peek=peek, tier=_tier_free, increment=increment, brave=_brave_must_not_run)

    result = await executor._web_surf({"query": "anything"})

    assert result.get("error") is True
    assert result.get("limit_reached") is True


async def test_paid_tier_never_touches_counter(executor, monkeypatch):
    calls = {"increment": 0}

    def peek(query, *, uid, recency="any"):
        return None

    async def tier(_uid):
        return "pro"

    async def increment(_uid):
        calls["increment"] += 1
        return True, 1

    async def brave(query, *, uid, recency="any"):
        return {"text": "ok", "sources": [], "query": query, "cached": False}

    _patch(monkeypatch, peek=peek, tier=tier, increment=increment, brave=brave)

    result = await executor._web_surf({"query": "x"})

    assert result["text"] == "ok"
    assert calls["increment"] == 0


async def test_empty_query_rejected(executor):
    with pytest.raises(ValueError, match="query is required"):
        await executor._web_surf({"query": "   "})


async def test_entitlement_outage_degrades_to_free_and_still_searches(executor, monkeypatch):
    # The fail-open-to-pro fix: a Firestore outage gates the caller as free, and
    # the counter's own fail-open (True, 0) keeps the search working. Degraded,
    # never hard-blocked, never silently pro.
    calls = {"increment": 0, "brave": 0}

    def peek(query, *, uid, recency="any"):
        return None

    async def tier(_uid):
        raise entitlement_module.EntitlementUnavailableError("firestore down")

    async def increment(_uid):
        calls["increment"] += 1
        return True, 0  # the counter's real behavior on the same outage

    async def brave(query, *, uid, recency="any"):
        calls["brave"] += 1
        return {"text": "ok", "sources": [], "query": query, "cached": False}

    _patch(monkeypatch, peek=peek, tier=tier, increment=increment, brave=brave)

    result = await executor._web_surf({"query": "x"})

    assert result["text"] == "ok"
    assert calls["increment"] == 1  # treated exactly like a free-tier caller
    assert calls["brave"] == 1
