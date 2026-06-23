"""Tests for the briefing engine's on-demand generation (persistence + refresh).

The evening briefing now always sends (the tracker-collision push suppression was
removed when it moved to the unified orchestrator), so the engine-level logic worth
pinning here is generate_on_demand: return an existing briefing without regenerating,
generate + persist exactly once when missing, keep the existing one when the pool is
empty, and debounce a forced refresh.
"""

from __future__ import annotations

import time

import pytest

import src.services.briefing.briefing_engine as engine
from src.services.briefing import briefing_agent, briefing_store
from src.services.briefing.briefing_agent import BriefingResult
from src.services.briefing.briefing_engine import generate_on_demand
from src.services.briefing.briefing_store import BriefingTargeting, StoredBriefing


# ── generate_on_demand: persistence + refresh ────────────────────────────────

def _result(n: int = 3) -> BriefingResult:
    items = [{"text": f"b{i}", "citation": i, "category": "Sports"} for i in range(n)]
    sources = [{"title": f"t{i}", "url": f"u{i}", "source": "s", "category": "sports"} for i in range(n)]
    return BriefingResult(
        items=items, narrative="\n\n".join(it["text"] for it in items),
        chat_seed_message="seed", push_title="t", push_body="b", sources=sources,
    )


def _stored(items: list | None = None, status: str = "ready") -> StoredBriefing:
    return StoredBriefing(
        local_date="2026-06-16", status=status, narrative="x",
        chat_seed_message="y", sources=[], items=items if items is not None else [{"text": "x"}],
    )


@pytest.fixture
def _consent(monkeypatch):
    async def _targeting(_uid):
        return BriefingTargeting(consent_granted=True, timezone="UTC")
    monkeypatch.setattr(briefing_store, "read_user_targeting", _targeting)
    monkeypatch.setattr(engine, "get_model_provider", lambda: object())
    engine._user_refresh_at.clear()


async def test_on_demand_returns_existing_without_regenerating(monkeypatch, _consent):
    existing = _stored()

    async def _get(_uid, *, local_date):
        return existing
    monkeypatch.setattr(briefing_store, "get_briefing", _get)

    gen_calls = 0

    async def _gen(*_a, **_k):
        nonlocal gen_calls
        gen_calls += 1
        return _result()
    monkeypatch.setattr(briefing_agent, "generate", _gen)

    out = await generate_on_demand("u1", force=False)
    assert out is existing
    assert gen_calls == 0


async def test_on_demand_generates_and_persists_when_missing(monkeypatch, _consent):
    async def _get(_uid, *, local_date):
        return None
    monkeypatch.setattr(briefing_store, "get_briefing", _get)

    async def _gen(*_a, **_k):
        return _result(4)
    monkeypatch.setattr(briefing_agent, "generate", _gen)

    writes: list[int] = []

    async def _write(_uid, *, local_date, narrative, chat_seed_message, sources, items):
        writes.append(len(items))
    monkeypatch.setattr(briefing_store, "write_briefing", _write)

    out = await generate_on_demand("u1", force=False)
    assert out is not None and out.status == "ready" and len(out.items) == 4
    assert writes == [4]  # persisted exactly once


async def test_on_demand_empty_pool_keeps_existing_and_does_not_write(monkeypatch, _consent):
    async def _get(_uid, *, local_date):
        return None
    monkeypatch.setattr(briefing_store, "get_briefing", _get)

    async def _gen(*_a, **_k):
        return None
    monkeypatch.setattr(briefing_agent, "generate", _gen)

    wrote = False

    async def _write(*_a, **_k):
        nonlocal wrote
        wrote = True
    monkeypatch.setattr(briefing_store, "write_briefing", _write)

    out = await generate_on_demand("u1", force=False)
    assert out is None
    assert wrote is False


async def test_force_regenerates_even_when_ready(monkeypatch, _consent):
    async def _get(_uid, *, local_date):
        return _stored(items=[], status="ready")
    monkeypatch.setattr(briefing_store, "get_briefing", _get)

    async def _gen(*_a, **_k):
        return _result(5)
    monkeypatch.setattr(briefing_agent, "generate", _gen)

    async def _write(*_a, **_k):
        return None
    monkeypatch.setattr(briefing_store, "write_briefing", _write)

    out = await generate_on_demand("u1", force=True)
    assert out is not None and len(out.items) == 5


async def test_force_debounced_returns_existing(monkeypatch, _consent):
    existing = _stored()

    async def _get(_uid, *, local_date):
        return existing
    monkeypatch.setattr(briefing_store, "get_briefing", _get)

    gen_calls = 0

    async def _gen(*_a, **_k):
        nonlocal gen_calls
        gen_calls += 1
        return _result()
    monkeypatch.setattr(briefing_agent, "generate", _gen)

    engine._user_refresh_at["u1"] = time.monotonic()  # a refresh just happened
    out = await generate_on_demand("u1", force=True)
    assert out is existing
    assert gen_calls == 0
