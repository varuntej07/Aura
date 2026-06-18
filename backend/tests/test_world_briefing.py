"""Tests for the on-demand world briefing (world_briefing.generate_world).

Covers the behaviours that keep it honest and cheap:
  1. The grounded free text is split into (narrative, chat_seed) on the sentinel, with
     a tolerant fallback when the model omits it.
  2. A successful generate returns the DailyBriefing-shaped payload and maps grounded
     refs into source tiles.
  3. An empty narrative returns None (the screen shows its empty state, never a blank).
  4. The per-region cache serves a second open without a second grounded call, and a
     force-refresh inside the cooldown is coalesced back to that cache.
"""

from __future__ import annotations

import pytest

from src.services.briefing import world_briefing
from src.services.briefing.world_briefing import (
    _assign_citations,
    _parse_output,
    _split_items,
    generate_world,
)
from src.services.model_provider import GroundedResult


class _FakeModels:
    """Stands in for ModelProvider; counts grounded() calls and returns canned output."""

    def __init__(
        self,
        text: str,
        sources: list[dict[str, str]] | None = None,
        supports: list[dict] | None = None,
    ):
        self.text = text
        self.sources = sources or []
        self.supports = supports or []
        self.calls = 0

    async def grounded(self, *_args, **_kwargs) -> GroundedResult:
        self.calls += 1
        return GroundedResult(text=self.text, sources=self.sources, supports=self.supports)


@pytest.fixture(autouse=True)
def _clear_caches():
    """The caches are module-level; clear them around each test so cache state from one
    test never leaks into another."""
    world_briefing._region_cache.clear()
    world_briefing._user_refresh_at.clear()
    yield
    world_briefing._region_cache.clear()
    world_briefing._user_refresh_at.clear()


# --- _parse_output ---------------------------------------------------------------

def test_parse_output_splits_on_marker():
    narrative, seed = _parse_output(
        "Big stuff happened today. And more.\nCHAT_SEED: want the cricket details?"
    )
    assert "Big stuff happened today" in narrative
    assert "CHAT_SEED" not in narrative
    assert seed == "want the cricket details?"


def test_parse_output_missing_marker_uses_default_seed():
    narrative, seed = _parse_output("Just a narrative, no marker.")
    assert narrative == "Just a narrative, no marker."
    assert seed  # non-empty default, never blank


def test_parse_output_empty_text():
    assert _parse_output("   ") == ("", "")


# --- generate_world --------------------------------------------------------------

async def test_generate_returns_payload_and_preserves_source_indices():
    models = _FakeModels(
        "Here is the world today, woven warmly.\nCHAT_SEED: dig into any of these?",
        sources=[
            {"title": "A", "url": "https://x.test/a"},
            {"title": "", "url": "https://x.test/b"},  # blank title falls back to url
            {"title": "C", "url": ""},                  # empty url KEPT as a placeholder
        ],
    )
    result = await generate_world("u1", timezone="Asia/Kolkata", models=models)
    assert result is not None
    assert result.region_code == "IN"
    assert result.chat_seed_message == "dig into any of these?"
    # 1:1 with the grounding chunks (no skipping), so citation indices stay valid.
    urls = [s["url"] for s in result.sources]
    assert urls == ["https://x.test/a", "https://x.test/b", ""]
    assert result.sources[1]["title"] == "https://x.test/b"
    # Source shape the Flutter client renders.
    assert set(result.sources[0]) == {"title", "url", "source", "category"}


# --- items + citations -----------------------------------------------------------

def test_split_items_breaks_on_blank_lines_and_caps():
    text = "\n\n".join(f"Item {i}" for i in range(10))
    items = _split_items(text)
    assert items[0] == "Item 0"
    assert len(items) == world_briefing.MAX_ITEMS  # capped


def test_assign_citations_matches_support_text_to_item():
    items = ["The cricket chase was wild.", "A small-model paper dropped."]
    supports = [
        {"text": "cricket chase", "source_indices": [1]},
        {"text": "small-model paper", "source_indices": [0]},
    ]
    assert _assign_citations(items, supports, n_sources=3) == [1, 0]


def test_assign_citations_none_when_no_match_or_out_of_range():
    items = ["Totally unrelated blurb."]
    # support text not in the item -> no citation
    assert _assign_citations(items, [{"text": "nope", "source_indices": [0]}], 3) == [None]
    # matching text but the source index is out of range -> no citation
    assert _assign_citations(["has cricket"], [{"text": "cricket", "source_indices": [9]}], 3) == [None]


async def test_generate_builds_items_with_citations():
    models = _FakeModels(
        "The cricket chase was wild today.\n\n"
        "A small-model paper is making waves.\n\n"
        "CHAT_SEED: which one first?",
        sources=[
            {"title": "Paper", "url": "https://x.test/paper"},
            {"title": "Cricket", "url": "https://x.test/cricket"},
        ],
        supports=[
            {"text": "cricket chase", "source_indices": [1]},
            {"text": "small-model paper", "source_indices": [0]},
        ],
    )
    result = await generate_world("u1", timezone="Asia/Kolkata", models=models)
    assert result is not None
    assert [it["text"] for it in result.items] == [
        "The cricket chase was wild today.",
        "A small-model paper is making waves.",
    ]
    assert [it["citation"] for it in result.items] == [1, 0]


async def test_empty_narrative_returns_none():
    models = _FakeModels("CHAT_SEED: nothing above this line")
    result = await generate_world("u1", timezone="Asia/Kolkata", models=models)
    assert result is None


async def test_region_cache_serves_second_open_without_second_call():
    models = _FakeModels("World narrative.\nCHAT_SEED: more?")
    first = await generate_world("u1", timezone="Asia/Kolkata", models=models)
    second = await generate_world("u2", timezone="Asia/Kolkata", models=models)
    assert first is not None and second is not None
    # Same region → the second user is served from cache, no second grounded call.
    assert models.calls == 1


async def test_force_refresh_within_cooldown_coalesces_to_cache():
    models = _FakeModels("World narrative.\nCHAT_SEED: more?")
    await generate_world("u1", timezone="Asia/Kolkata", models=models)  # warms cache + stamps cooldown
    await generate_world("u1", timezone="Asia/Kolkata", force=True, models=models)
    # Force-refresh inside the cooldown must not trigger a fresh grounded call.
    assert models.calls == 1


async def test_unknown_timezone_generates_global():
    models = _FakeModels("Global only narrative.\nCHAT_SEED: more?")
    result = await generate_world("u1", timezone="Mars/Olympus_Mons", models=models)
    assert result is not None
    assert result.region_code == "GLOBAL"
    assert models.calls == 1
