"""
Tests for src/services/daily_notification/suggestion_pills_agent.py
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestParsePills:
    def test_parse_valid_json_array(self):
        from src.services.daily_notification.suggestion_pills_agent import _parse_pills

        raw = '["help me prep", "back to the gym", "next match plan"]'

        assert _parse_pills(raw) == ["help me prep", "back to the gym", "next match plan"]

    def test_strips_markdown_fences_and_filters_long_pills(self):
        from src.services.daily_notification.suggestion_pills_agent import _parse_pills

        raw = '```json\n["Short pill", "This pill has too many words to fit well"]\n```'

        assert _parse_pills(raw) == ["Short pill"]

    def test_returns_empty_on_invalid_json(self):
        from src.services.daily_notification.suggestion_pills_agent import _parse_pills

        assert _parse_pills("not json") == []


class TestSuggestionPillsAgent:
    @pytest.mark.asyncio
    async def test_generate_buddy_pills_writes_buddy_set(self):
        from src.services.daily_notification.suggestion_pills_agent import SuggestionPillsAgent

        model = MagicMock()
        model.balanced = AsyncMock(
            return_value='["help me prep for my interview", "hold me to the gym today", "recommend me a sci-fi book"]'
        )
        doc_ref = MagicMock()
        db = MagicMock()
        db.collection.return_value.document.return_value = doc_ref

        with patch(
            "src.services.daily_notification.suggestion_pills_agent.admin_firestore",
            return_value=db,
        ):
            pills = await SuggestionPillsAgent(model).generate_buddy_pills(
                "uid1",
                [{"text": "find remote ml jobs"}],
                ["machine learning"],
            )

        assert pills == [
            "help me prep for my interview",
            "hold me to the gym today",
            "recommend me a sci-fi book",
        ]
        assert model.balanced.call_count == 1
        db.collection.assert_called_once_with("agent_suggestion_pills")
        db.collection.return_value.document.assert_called_once_with("uid1")
        written = doc_ref.set.call_args[0][0]
        assert written["buddy"] == pills
        assert "buddy_generated_at" in written
        assert "updated_at" in written
        # merge=True so any legacy per-agent keys for old clients are never clobbered.
        assert doc_ref.set.call_args.kwargs.get("merge") is True

    @pytest.mark.asyncio
    async def test_generate_buddy_pills_skips_write_when_empty(self):
        from src.services.daily_notification.suggestion_pills_agent import SuggestionPillsAgent

        model = MagicMock()
        model.balanced = AsyncMock(return_value="not json")
        doc_ref = MagicMock()
        db = MagicMock()
        db.collection.return_value.document.return_value = doc_ref

        with patch(
            "src.services.daily_notification.suggestion_pills_agent.admin_firestore",
            return_value=db,
        ):
            pills = await SuggestionPillsAgent(model).generate_buddy_pills("uid1", [], None)

        assert pills == []
        doc_ref.set.assert_not_called()
