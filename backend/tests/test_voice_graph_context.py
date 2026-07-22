"""Phase 3 graph digest and cache-safe voice context coverage."""

from __future__ import annotations

import asyncio

from src.agent import voice_prompt
from src.agent.voice import context, fetchers
from src.services.memory import graph_fields as GF
from src.services.memory import retrieval
from src.services.tool_executor import ToolExecutor


def test_normalized_salience_dampens_huge_components_and_gates_status():
    focused = {GF.WEIGHT: 2.0, GF.DEGREE: 0, GF.STATUS: GF.NODE_STATUS_ACTIVE}
    huge = {GF.WEIGHT: 10.0, GF.DEGREE: 100, GF.STATUS: GF.NODE_STATUS_ACTIVE}
    assert fetchers.normalized_graph_salience(focused) > fetchers.normalized_graph_salience(huge)
    for status in (GF.NODE_STATUS_COMPLETED, GF.NODE_STATUS_ABANDONED):
        assert fetchers.normalized_graph_salience({
            GF.WEIGHT: 100.0,
            GF.DEGREE: 0,
            GF.STATUS: status,
        }) == 0.0


def test_voice_context_always_fetches_and_renders_graph_digest(monkeypatch):
    """Graph read is always on (flags removed 2026-07-20): the digest is fetched
    exactly once per session and rendered into the {graph_context} slot."""
    graph_calls = 0

    async def _profile(_uid):
        return {"name": "V", "timezone": "UTC"}

    async def _memory(_uid):
        return "- city: Hyderabad"

    async def _last(_uid):
        return {"summary": "caught up", "last_session_at": "yesterday"}

    async def _archive(_uid):
        return {"archive_summary": "old context"}

    async def _aura(_uid):
        return {"summary": "casual", "dominant_tone": "casual", "dominant_emotion": ""}

    async def _tier(_uid):
        return "free"

    async def _remaining(_uid):
        return 30

    async def _graph(_uid):
        nonlocal graph_calls
        graph_calls += 1
        return "- pursuing an SDE role at Annapurna"

    monkeypatch.setattr(context, "fetch_user_profile", _profile)
    monkeypatch.setattr(context, "fetch_memory_summary", _memory)
    monkeypatch.setattr(context, "fetch_last_session_summary", _last)
    monkeypatch.setattr(context, "fetch_archive_context", _archive)
    monkeypatch.setattr(context, "fetch_user_aura_profile", _aura)
    monkeypatch.setattr(context, "get_user_effective_tier", _tier)
    monkeypatch.setattr(context, "get_remaining_free_voice_seconds", _remaining)
    monkeypatch.setattr(context, "fetch_graph_digest", _graph)

    session_context = asyncio.run(context.gather_session_context("u1", "s1"))
    rendered_prompt = voice_prompt.VOICE_PROMPT.format(
        **session_context.prompt_context_vars,
        surface="",
        screen_sight="",
    )

    assert graph_calls == 1
    assert "Related long-term memory:" in session_context.graph_context
    assert "pursuing an SDE role at Annapurna" in rendered_prompt


async def test_query_memory_keeps_contract_and_excludes_inactive_nodes(monkeypatch):
    async def _retrieve(*_args, **_kwargs):
        return [
            retrieval.RetrievedAtom(
                "active memory", "fact", 0.9, 0.9,
                node_id="active", status=GF.NODE_STATUS_ACTIVE,
            ),
            retrieval.RetrievedAtom(
                "finished memory", "fact", 0.8, 0.8,
                node_id="finished", status=GF.NODE_STATUS_COMPLETED,
            ),
            retrieval.RetrievedAtom(
                "abandoned memory", "fact", 0.7, 0.7,
                node_id="abandoned", status=GF.NODE_STATUS_ABANDONED,
            ),
        ]

    monkeypatch.setattr(retrieval, "retrieve_relevant_subgraph", _retrieve)
    result = await ToolExecutor("u1")._query_memory({
        "query": "memory",
        "category_filter": "all",
    })

    assert list(result) == ["matches"]
    assert result == {"matches": [{
        "memory_id": "active",
        "key": "fact",
        "value": "active memory",
        "category": "fact",
    }]}
