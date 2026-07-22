"""
Query-relevant memory retrieval.

Verifies: the v1 intent gate skips acks/empties; composite ranking orders by relevance
and drops sub-floor atoms; near-duplicates are de-duped; retrieval fails OPEN on embed
error and on the wall-clock budget; it fails LOUD when the store is non-empty but search
returns nothing; the render block de-dupes against the static <interests> already shown;
and the per-turn memory block is NOT baked into the cached system suffix (the prompt-cache
regression guard). Firestore + the embedder are faked.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime

import pytest

from src.services.memory import fields as F
from src.services.memory import retrieval
from src.services.memory.retrieval import (
    RetrievedAtom,
    render_relevant_memory_block,
    retrieve_relevant_memory,
    should_retrieve_for_message,
)

NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_circuit():
    """The retrieval circuit breaker is process-local module state; reset it around every
    test so a failure in one test can't leak an open circuit into another."""
    retrieval.reset_circuit()
    yield
    retrieval.reset_circuit()


# --- fakes ----------------------------------------------------------------
class _Snap:
    def __init__(self, data, id_="atom-test"):
        self._d = data
        self.id = id_

    def to_dict(self):
        return self._d


class _Nearest:
    def __init__(self, snaps):
        self._snaps = snaps

    def stream(self):
        return iter(self._snaps)


class _Coll:
    def __init__(self, near, probe):
        self._near = near
        self._probe = probe
        self._lim = None

    def find_nearest(self, **_kw):
        return _Nearest(self._near)

    def limit(self, n):
        self._lim = n
        return self

    def stream(self):
        return iter(self._probe[: self._lim] if self._lim else self._probe)


class _Doc:
    def __init__(self, near, probe):
        self._near, self._probe = near, probe

    def collection(self, _name):
        return _Coll(self._near, self._probe)


class _Parent:
    def __init__(self, near, probe):
        self._near, self._probe = near, probe

    def document(self, _uid):
        return _Doc(self._near, self._probe)


class _Db:
    def __init__(self, near, probe):
        self._near, self._probe = near, probe

    def collection(self, _name):
        return _Parent(self._near, self._probe)


def _cand(text, distance, *, emb=None, atom_type=F.ATOM_TYPE_FACT,
          categories=None, weight=1.0, importance=0.5):
    return _Snap({
        "cosine_distance": distance,
        F.TEXT: text,
        F.ATOM_TYPE: atom_type,
        F.EMBEDDING: emb or [1.0, 0.0, 0.0],
        F.CATEGORIES: categories or [],
        F.WEIGHT: weight,
        F.IMPORTANCE: importance,
        F.DECAY_KIND: "durable",
        F.LAST_SEEN: NOW.isoformat(),
    })


async def _default_embed(_q):
    return [1.0, 0.0, 0.0]


def _install(monkeypatch, near, probe=None, *, embed=None):
    monkeypatch.setattr(retrieval, "admin_firestore", lambda: _Db(near, probe or []))
    monkeypatch.setattr(retrieval, "embed_text", embed or _default_embed)


# --- intent gate ----------------------------------------------------------
def test_intent_gate_skips_acks_and_empties():
    assert should_retrieve_for_message("") is False
    assert should_retrieve_for_message("ok") is False
    assert should_retrieve_for_message("thanks!") is False
    assert should_retrieve_for_message("lol") is False
    assert should_retrieve_for_message("what was my doctor's name again?") is True


def test_gate_short_circuits_without_touching_firestore(monkeypatch):
    # An ack must not even reach embed/find_nearest.
    called = {"embed": False}

    async def _embed(_q):
        called["embed"] = True
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(retrieval, "embed_text", _embed)
    out = asyncio.run(retrieve_relevant_memory("u1", "ok", now=NOW))
    assert out == []
    assert called["embed"] is False


# --- ranking --------------------------------------------------------------
def test_ranking_orders_by_relevance_and_drops_below_floor(monkeypatch):
    near = [
        _cand("near", 0.10, emb=[1.0, 0.0, 0.0]),   # sim 0.90
        _cand("mid", 0.30, emb=[0.0, 1.0, 0.0]),    # sim 0.70
        _cand("far", 0.60, emb=[0.0, 0.0, 1.0]),    # sim 0.40 < floor 0.55 -> dropped
    ]
    _install(monkeypatch, near)
    out = asyncio.run(retrieve_relevant_memory("u1", "a real question", k=5, now=NOW))
    assert [a.text for a in out] == ["near", "mid"]


def test_near_duplicate_is_deduped(monkeypatch):
    near = [
        _cand("a", 0.10, emb=[1.0, 0.0, 0.0]),
        _cand("b", 0.15, emb=[1.0, 0.0, 0.0]),  # identical embedding -> dup of "a"
    ]
    _install(monkeypatch, near)
    out = asyncio.run(retrieve_relevant_memory("u1", "a real question", k=5, now=NOW))
    assert [a.text for a in out] == ["a"]


# --- resilience -----------------------------------------------------------
def test_fail_open_on_embed_error(monkeypatch):
    async def _boom(_q):
        raise RuntimeError("embed down")

    _install(monkeypatch, [_cand("x", 0.1)], embed=_boom)
    assert asyncio.run(retrieve_relevant_memory("u1", "a real question", now=NOW)) == []


def test_fail_open_on_budget_timeout(monkeypatch):
    monkeypatch.setattr(retrieval.settings, "MEMORY_RETRIEVAL_BUDGET_S", 0.05)

    async def _slow(_q):
        await asyncio.sleep(0.3)
        return [1.0, 0.0, 0.0]

    _install(monkeypatch, [_cand("x", 0.1)], embed=_slow)
    assert asyncio.run(retrieve_relevant_memory("u1", "a real question", now=NOW)) == []


def test_fail_loud_on_empty_over_nonempty_store(monkeypatch):
    warnings: list = []
    monkeypatch.setattr(retrieval.logger, "warn", lambda *a, **k: warnings.append(a))
    # find_nearest returns nothing, but the existence probe sees a doc.
    _install(monkeypatch, near=[], probe=[_Snap({})])
    out = asyncio.run(retrieve_relevant_memory("u1", "a real question", now=NOW))
    assert out == []
    assert warnings, "expected a loud warning when 0 results over a non-empty store"


# --- render ---------------------------------------------------------------
def test_render_dedups_against_static_interests():
    shown = {"KCR"}
    only_dupe = [RetrievedAtom("KCR", F.ATOM_TYPE_INTEREST_SUBJECT, 0.9, 0.9)]
    assert render_relevant_memory_block(only_dupe, already_shown=shown) == ""

    fresh = [RetrievedAtom("doctor is Dr. Reddy", F.ATOM_TYPE_FACT, 0.9, 0.9)]
    block = render_relevant_memory_block(fresh, already_shown=shown)
    assert "Dr. Reddy" in block
    assert block.startswith("<relevant_memory>")


def test_render_empty_returns_blank():
    assert render_relevant_memory_block([]) == ""


# --- prompt-cache regression guard ----------------------------------------
def test_memory_block_is_not_in_cached_suffix():
    # The per-turn <relevant_memory> must NEVER live in the cached aura_suffix, or it
    # invalidates the ~10min system-prompt cache every turn. Proven by: the suffix
    # builder has no concept of relevant_memory.
    from src.handlers.chat import _build_injected_system_prompt_suffix

    profile = {"explicit_facts": ["lives in Hyderabad"], "dominant_tone": "casual"}
    suffix = _build_injected_system_prompt_suffix(profile, [], "u1")
    assert "relevant_memory" not in suffix


def test_chat_graph_first_falls_back_to_legacy_when_graph_empty(monkeypatch):
    """Graph read is always on (flags removed 2026-07-20). The graph fills
    organically from new turns, so an empty subgraph must fall back to flat
    atom retrieval instead of erasing recall of pre-graph memories."""
    from src.services.chat_completion import prompt_builder

    profile = {"explicit_facts": ["lives in Hyderabad"], "dominant_tone": "casual"}
    atom = RetrievedAtom("doctor is Dr. Reddy", F.ATOM_TYPE_FACT, 0.9, 0.9)
    calls = {"legacy": 0, "graph": 0}

    async def _datetime(*_args):
        return "Saturday, July 18, 2026 at 1:00 PM PDT"

    async def _aura(*_args):
        return profile, []

    async def _legacy(*_args, **_kwargs):
        calls["legacy"] += 1
        return [atom]

    async def _graph(*_args, **_kwargs):
        calls["graph"] += 1
        return []

    monkeypatch.setattr(prompt_builder, "get_user_local_datetime", _datetime)
    monkeypatch.setattr(prompt_builder, "fetch_cached_aura_data", _aura)
    monkeypatch.setattr(prompt_builder, "retrieve_relevant_memory", _legacy)
    monkeypatch.setattr(prompt_builder, "retrieve_relevant_subgraph", _graph)

    actual = asyncio.run(prompt_builder.build_turn_system_blocks(
        "u1", "who was my doctor?", "because you asked", user_doc={},
    ))
    aura_suffix = prompt_builder.build_injected_system_prompt_suffix(profile, [], "u1")
    expected = prompt_builder.build_system_blocks(
        prompt_builder.settings.BUDDY_CHAT_SYSTEM_PROMPT,
        aura_suffix,
        "Saturday, July 18, 2026 at 1:00 PM PDT",
        "because you asked",
    )
    expected.append({
        "type": "text",
        "text": render_relevant_memory_block([atom]),
    })
    assert json.dumps(actual, ensure_ascii=False) == json.dumps(expected, ensure_ascii=False)
    assert calls == {"legacy": 1, "graph": 1}


def test_chat_graph_hit_skips_legacy_retrieval(monkeypatch):
    """When the subgraph returns atoms, legacy retrieval is never consulted."""
    from src.services.chat_completion import prompt_builder

    profile = {"explicit_facts": ["lives in Hyderabad"], "dominant_tone": "casual"}
    graph_atom = RetrievedAtom("doctor is Dr. Reddy", F.ATOM_TYPE_FACT, 0.9, 0.9)
    calls = {"legacy": 0, "graph": 0}

    async def _datetime(*_args):
        return "Saturday, July 18, 2026 at 1:00 PM PDT"

    async def _aura(*_args):
        return profile, []

    async def _legacy(*_args, **_kwargs):
        calls["legacy"] += 1
        return []

    async def _graph(*_args, **_kwargs):
        calls["graph"] += 1
        return [graph_atom]

    monkeypatch.setattr(prompt_builder, "get_user_local_datetime", _datetime)
    monkeypatch.setattr(prompt_builder, "fetch_cached_aura_data", _aura)
    monkeypatch.setattr(prompt_builder, "retrieve_relevant_memory", _legacy)
    monkeypatch.setattr(prompt_builder, "retrieve_relevant_subgraph", _graph)

    blocks = asyncio.run(prompt_builder.build_turn_system_blocks(
        "u1", "who was my doctor?", "because you asked", user_doc={},
    ))

    assert calls == {"legacy": 0, "graph": 1}
    assert any("Dr. Reddy" in str(block.get("text", "")) for block in blocks)


# --- circuit breaker (protects time-to-first-token during an embed outage) -
def test_circuit_opens_after_repeated_failures_and_skips_embed(monkeypatch):
    calls = {"n": 0}

    async def _boom(_q):
        calls["n"] += 1
        raise RuntimeError("embed down (e.g. billing 403)")

    _install(monkeypatch, [_cand("x", 0.1)], embed=_boom)
    # Three consecutive failures open the circuit.
    for _ in range(3):
        assert asyncio.run(retrieve_relevant_memory("u1", "a real question", now=NOW)) == []
    assert calls["n"] == 3
    # Fourth call is short-circuited: no embed attempted, returns [] immediately.
    assert asyncio.run(retrieve_relevant_memory("u1", "a real question", now=NOW)) == []
    assert calls["n"] == 3  # embed NOT called again -> no wasted budget on the hot path


def test_success_resets_the_failure_counter(monkeypatch):
    async def _boom(_q):
        raise RuntimeError("down")

    # Two failures (below the threshold of 3).
    _install(monkeypatch, [_cand("x", 0.1)], embed=_boom)
    asyncio.run(retrieve_relevant_memory("u1", "a real question", now=NOW))
    asyncio.run(retrieve_relevant_memory("u1", "a real question", now=NOW))
    # A success resets the counter back to zero.
    _install(monkeypatch, [_cand("near", 0.1)])
    assert asyncio.run(retrieve_relevant_memory("u1", "a real question", now=NOW))[0].text == "near"
    # Two more failures still must NOT open the circuit (counter was reset by the success).
    _install(monkeypatch, [_cand("x", 0.1)], embed=_boom)
    asyncio.run(retrieve_relevant_memory("u1", "a real question", now=NOW))
    asyncio.run(retrieve_relevant_memory("u1", "a real question", now=NOW))
    _install(monkeypatch, [_cand("near", 0.1)])
    assert asyncio.run(retrieve_relevant_memory("u1", "a real question", now=NOW))[0].text == "near"


# --- graph expansion ------------------------------------------------------
def test_graph_traversal_timeout_returns_ranked_seeds(monkeypatch):
    seed = RetrievedAtom("seed", F.ATOM_TYPE_FACT, 0.9, 0.9, node_id="seed")

    async def _seeds(*_args, **_kwargs):
        return [1.0, 0.0], [seed]

    async def _slow_graph(*_args, **_kwargs):
        await asyncio.sleep(0.2)
        return []

    monkeypatch.setattr(retrieval, "_gather_seed_result", _seeds)
    monkeypatch.setattr(retrieval, "_traverse_graph", _slow_graph)
    started = time.monotonic()
    out = asyncio.run(retrieval.retrieve_relevant_subgraph(
        "u1", "a real question", now=NOW, budget_s=0.03,
    ))
    elapsed = time.monotonic() - started
    assert out == [seed]
    assert elapsed < 0.12


def test_graph_seed_error_keeps_established_empty_behavior(monkeypatch):
    async def _boom(*_args, **_kwargs):
        raise RuntimeError("embed down")

    monkeypatch.setattr(retrieval, "_gather_seed_result", _boom)
    assert asyncio.run(retrieval.retrieve_relevant_subgraph(
        "u1", "a real question", now=NOW,
    )) == []


def test_inactive_nodes_are_not_expanded_but_seed_is_directly_recallable(monkeypatch):
    seed = RetrievedAtom("finished goal", "goal", 0.9, 0.9, node_id="seed")
    nodes = {
        "seed": {"status": "completed", "display": "finished goal"},
        "abandoned": {"status": "abandoned", "display": "old plan"},
    }

    async def _nodes(_uid, ids):
        return {node_id: nodes[node_id] for node_id in ids if node_id in nodes}

    async def _adjacency(_uid, ids):
        return {node_id: ("abandoned",) for node_id in ids}

    monkeypatch.setattr(retrieval, "_read_graph_nodes", _nodes)
    monkeypatch.setattr(retrieval, "_read_adjacency", _adjacency)
    out = asyncio.run(retrieval._traverse_graph(
        "u1", [seed], [1.0, 0.0], set(), NOW,
    ))
    assert [atom.text for atom in out] == ["finished goal"]
    assert out[0].status == "completed"
