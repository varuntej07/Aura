"""
Tests for src/services/notification_rewriter.py

The rewriter now routes through model_provider.cheap() (Gemini Flash + its fallback chain)
instead of a bespoke raw-Anthropic client. The rewriter now returns a ReminderCopy
(title + body) rather than a bare string. These cover: success returns the rewritten copy,
a total cheap() failure degrades to the normalised original, unparseable model output
degrades to the original, overlong output is capped, and _normalise's formatting.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _provider_with_cheap(cheap_mock: AsyncMock) -> MagicMock:
    """A stand-in ModelProvider whose .cheap() is the supplied AsyncMock."""
    provider = MagicMock()
    provider.cheap = cheap_mock
    return provider


class TestRewriteReminderNotification:
    @pytest.mark.asyncio
    async def test_success_returns_rewritten_text(self):
        from src.services.notification_rewriter import rewrite_reminder_notification

        cheap = AsyncMock(return_value='{"title":"Meds","body":"Take your meds"}')
        with patch(
            "src.services.notification_rewriter.get_model_provider",
            return_value=_provider_with_cheap(cheap),
        ):
            result = await rewrite_reminder_notification("take medication")

        assert result.body == "Take your meds"
        assert result.title == "Meds"
        cheap.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cheap_failure_returns_original(self):
        """cheap() only raises once its whole chain is exhausted — degrade to the original."""
        from src.services.notification_rewriter import rewrite_reminder_notification

        cheap = AsyncMock(side_effect=RuntimeError("entire fallback chain exhausted"))
        with patch(
            "src.services.notification_rewriter.get_model_provider",
            return_value=_provider_with_cheap(cheap),
        ):
            result = await rewrite_reminder_notification("take medication")

        assert result.body == "take medication"
        assert result.title == ""

    @pytest.mark.asyncio
    async def test_unparseable_output_returns_original(self):
        """An empty/whitespace/non-JSON response must not ship a blank push."""
        from src.services.notification_rewriter import rewrite_reminder_notification

        cheap = AsyncMock(return_value="   ")
        with patch(
            "src.services.notification_rewriter.get_model_provider",
            return_value=_provider_with_cheap(cheap),
        ):
            result = await rewrite_reminder_notification("walk the dog")

        assert result.body == "walk the dog"
        assert result.title == ""

    @pytest.mark.asyncio
    async def test_overlong_model_output_is_capped(self):
        """A model that ignores the body cap is truncated at a word boundary."""
        from src.services.notification_rewriter import (
            rewrite_reminder_notification,
            _BODY_MAX_CHARS,
        )

        long_copy = (
            "Hey, check in with Varun on the Neuron Collectives stuff today and get the "
            "specifics from him before the review lands on everyone"
        )
        cheap = AsyncMock(return_value=json.dumps({"title": "Varun", "body": long_copy}))
        with patch(
            "src.services.notification_rewriter.get_model_provider",
            return_value=_provider_with_cheap(cheap),
        ):
            result = await rewrite_reminder_notification("neuron collectives note")

        assert len(result.body) <= _BODY_MAX_CHARS
        assert not result.body.endswith(" ")
        # Truncation cuts whole words, never mid-word, so the original prefix is preserved.
        assert long_copy.startswith(result.body)


class TestNormalise:
    def test_strips_wrapping_quotes(self):
        from src.services.notification_rewriter import _normalise
        assert _normalise('"call your mom"') == "call your mom"
        assert _normalise("“send that email”") == "send that email"

    def test_collapses_to_single_line(self):
        from src.services.notification_rewriter import _normalise
        assert _normalise("meds time.\n  don't skip it") == "meds time. don't skip it"

    def test_replaces_long_dashes(self):
        from src.services.notification_rewriter import _normalise
        # Em dash and en dash both become ", " (reused from the signal framer).
        assert "—" not in _normalise("budget time — before payday")
        assert "–" not in _normalise("gym now – no excuses")

    def test_caps_body_on_word_boundary(self):
        from src.services.notification_rewriter import _normalise, _BODY_MAX_CHARS
        out = _normalise(
            "one two three four five six seven eight nine ten eleven twelve thirteen "
            "fourteen fifteen sixteen seventeen eighteen nineteen twenty"
        )
        assert len(out) <= _BODY_MAX_CHARS
        # No partial trailing word.
        assert not out.endswith("thi")

    def test_short_clean_text_is_unchanged(self):
        from src.services.notification_rewriter import _normalise
        assert _normalise("call your mom. she's pretending she's not waiting.") == (
            "call your mom. she's pretending she's not waiting."
        )

    def test_empty_input_is_safe(self):
        from src.services.notification_rewriter import _normalise
        assert _normalise("") == ""
