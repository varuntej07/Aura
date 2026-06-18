"""Tests for the BriefingAgent — the item write-up + grounding guarantee.

Covers the two behaviours that keep the briefing honest:
  1. ``_build_items_and_sources`` only ever yields items keyed to a real input index
     (a hallucinated / out-of-range / duplicate index can never inject a phantom
     source), with citations 1:1 with the sources array.
  2. ``generate`` skips (returns None) when the selector finds nothing and when the
     model keeps no items, and otherwise returns items whose sources are a strict
     subset of the selected input.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.services.briefing import briefing_agent
from src.services.briefing.briefing_agent import (
    BriefingDraft,
    _BriefingItemDraft,
    _build_items_and_sources,
)
from src.services.briefing.briefing_store import BriefingTargeting
from src.services.briefing.candidate_selector import SelectedItem

_LOCAL_NOW = datetime(2026, 6, 13, 6, 5, tzinfo=UTC)
_TARGETING = BriefingTargeting(consent_granted=True, language="English", display_name="Varun")


def _item(n: int, category: str = "sports") -> SelectedItem:
    return SelectedItem(
        content_id=f"c{n}",
        source="google_news",
        category=category,
        title=f"Title {n}",
        body=f"Real substantive body {n}",
        url=f"https://example.com/{n}",
        score=1.0,
    )


def _draft(*indexes: int) -> list[_BriefingItemDraft]:
    return [_BriefingItemDraft(source_index=i, blurb=f"blurb {i}") for i in indexes]


def test_build_items_and_sources_maps_only_real_items():
    selected = [_item(1), _item(2), _item(3)]
    # 1-based indexes; 9 is out of range, the second 2 is a duplicate.
    items, sources = _build_items_and_sources(selected, _draft(1, 3, 9, 2, 2))
    assert [s["title"] for s in sources] == ["Title 1", "Title 3", "Title 2"]
    # citation is each item's own position in the parallel sources array.
    assert [it["citation"] for it in items] == [0, 1, 2]
    assert [it["text"] for it in items] == ["blurb 1", "blurb 3", "blurb 2"]


def test_build_items_and_sources_drops_empty_blurb():
    selected = [_item(1), _item(2)]
    drafts = [
        _BriefingItemDraft(source_index=1, blurb="   "),
        _BriefingItemDraft(source_index=2, blurb="real"),
    ]
    items, sources = _build_items_and_sources(selected, drafts)
    assert [it["text"] for it in items] == ["real"]
    assert [s["title"] for s in sources] == ["Title 2"]


def test_build_items_and_sources_empty_when_no_valid_index():
    items, sources = _build_items_and_sources([_item(1)], _draft(0, -1, 5))
    assert items == []
    assert sources == []


class _FakeModels:
    def __init__(self, draft: BriefingDraft):
        self._draft = draft

    async def cheap(self, *_args, **_kwargs) -> BriefingDraft:
        return self._draft


@pytest.fixture(autouse=True)
def _no_aura_read(monkeypatch):
    async def _empty(_uid):
        return {}
    monkeypatch.setattr(briefing_agent, "_read_user_aura", _empty)


def _patch_selection(monkeypatch, items: list[SelectedItem]):
    async def _select(_uid, *, region, now):
        return items
    monkeypatch.setattr(briefing_agent, "select_briefing_items", _select)


async def test_generate_skips_when_no_candidates(monkeypatch):
    _patch_selection(monkeypatch, [])
    result = await briefing_agent.generate(
        _FakeModels(BriefingDraft()), "u1", _TARGETING, _LOCAL_NOW,
    )
    assert result is None


async def test_generate_skips_when_model_keeps_no_items(monkeypatch):
    _patch_selection(monkeypatch, [_item(1), _item(2)])
    result = await briefing_agent.generate(
        _FakeModels(BriefingDraft(items=[], chat_seed_message="")),
        "u1", _TARGETING, _LOCAL_NOW,
    )
    assert result is None


async def test_generate_returns_items_with_sources_subset(monkeypatch):
    _patch_selection(monkeypatch, [_item(1, "sports"), _item(2, "technology_ai"), _item(3, "world")])
    draft = BriefingDraft(
        items=_draft(1, 3),
        chat_seed_message="The two above. Want the detail?",
        push_title="your corner",
        push_body="two worth a peek",
    )
    result = await briefing_agent.generate(_FakeModels(draft), "u1", _TARGETING, _LOCAL_NOW)

    assert result is not None
    assert result.push_title == "your corner"
    assert result.push_body == "two worth a peek"
    assert [s["title"] for s in result.sources] == ["Title 1", "Title 3"]
    assert len(result.items) == 2
    # narrative is the blurbs joined, kept for back-compat.
    assert "blurb 1" in result.narrative and "blurb 3" in result.narrative


async def test_generate_falls_back_to_default_push_copy(monkeypatch):
    _patch_selection(monkeypatch, [_item(1)])
    draft = BriefingDraft(items=_draft(1), chat_seed_message="want it?")
    result = await briefing_agent.generate(_FakeModels(draft), "u1", _TARGETING, _LOCAL_NOW)
    assert result is not None
    assert result.push_title == briefing_agent.DEFAULT_PUSH_TITLE
    assert result.push_body == briefing_agent.DEFAULT_PUSH_BODY
