"""
Tests for src/services/daily_notification/suggestion_pills_agent.py
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestParsePills:
    def test_parse_valid_json_array(self):
        from src.services.daily_notification.suggestion_pills_agent import _parse_pills

        raw = '["IPL today", "Points table", "Next match"]'

        assert _parse_pills(raw, "sports") == ["IPL today", "Points table", "Next match"]

    def test_strips_markdown_fences_and_filters_long_pills(self):
        from src.services.daily_notification.suggestion_pills_agent import _parse_pills

        raw = '```json\n["Short pill", "This pill has too many words to fit well"]\n```'

        assert _parse_pills(raw, "posts") == ["Short pill"]

    def test_returns_empty_on_invalid_json(self):
        from src.services.daily_notification.suggestion_pills_agent import _parse_pills

        assert _parse_pills("not json", "technews") == []


class TestSuggestionPillsAgent:
    @pytest.mark.asyncio
    async def test_generates_all_agents_and_writes_firestore_shape(self):
        from src.services.daily_notification.suggestion_pills_agent import SuggestionPillsAgent

        model = MagicMock()
        model.cheap = AsyncMock(
            side_effect=[
                '["IPL today", "Points table", "Next match", "Top scorer"]',
                '["AI news", "Open source", "Startup funding", "Dev tools"]',
                '["Draft a tweet", "Thread starter", "LinkedIn idea", "Hot take"]',
            ]
        )
        doc_ref = MagicMock()
        db = MagicMock()
        db.collection.return_value.document.return_value = doc_ref

        with patch(
            "src.services.daily_notification.suggestion_pills_agent.rss_client.fetch_news",
            new=AsyncMock(return_value=[{"title": "Headline"}]),
        ):
            with patch(
                "src.services.daily_notification.suggestion_pills_agent.admin_firestore",
                return_value=db,
            ):
                await SuggestionPillsAgent(model).generate_all_agent_suggestion_pills(
                    "uid1",
                    [{"text": "find remote ml jobs"}],
                )

        assert model.cheap.call_count == 3
        db.collection.assert_called_once_with("agent_suggestion_pills")
        db.collection.return_value.document.assert_called_once_with("uid1")
        written = doc_ref.set.call_args[0][0]
        assert written["sports"] == ["IPL today", "Points table", "Next match", "Top scorer"]
        assert written["technews"] == ["AI news", "Open source", "Startup funding", "Dev tools"]
        assert written["posts"] == ["Draft a tweet", "Thread starter", "LinkedIn idea", "Hot take"]
        assert "updated_at" in written

    @pytest.mark.asyncio
    async def test_rss_failure_does_not_block_query_context_agents(self):
        from src.services.daily_notification.suggestion_pills_agent import SuggestionPillsAgent

        model = MagicMock()
        model.cheap = AsyncMock(
            side_effect=[
                "[]",
                "[]",
                '["Draft a tweet", "Thread starter", "LinkedIn idea", "Hot take"]',
            ]
        )
        doc_ref = MagicMock()
        db = MagicMock()
        db.collection.return_value.document.return_value = doc_ref

        with patch(
            "src.services.daily_notification.suggestion_pills_agent.rss_client.fetch_news",
            new=AsyncMock(side_effect=RuntimeError("rss down")),
        ):
            with patch(
                "src.services.daily_notification.suggestion_pills_agent.admin_firestore",
                return_value=db,
            ):
                await SuggestionPillsAgent(model).generate_all_agent_suggestion_pills(
                    "uid1",
                    [{"text": "write a post about my job search"}],
                )

        written = doc_ref.set.call_args[0][0]
        assert set(written) >= {"posts", "updated_at"}
        assert "sports" not in written
        assert "technews" not in written
