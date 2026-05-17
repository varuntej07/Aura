"""
Tests for src/services/notification_rewriter.py

Covers: _get_client lazy init, rewrite_reminder_notification (success,
retryable error with retry, retries exhausted, non-retryable error,
non-TextBlock response block).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import anthropic


def _make_text_response(text: str):
    from anthropic.types import TextBlock
    resp = MagicMock()
    resp.content = [TextBlock(type="text", text=text)]
    return resp


def _make_non_text_response():
    block = MagicMock()
    block.__class__ = type("NotTextBlock", (), {})  # not a TextBlock
    resp = MagicMock()
    resp.content = [block]
    return resp


class TestGetClient:
    def test_lazy_init_creates_client_once(self):
        import src.services.notification_rewriter as rw
        original = rw._client
        try:
            rw._client = None
            mock_client = MagicMock()
            with patch("src.services.notification_rewriter.anthropic.AsyncAnthropic", return_value=mock_client):
                with patch("src.services.notification_rewriter.wrap_anthropic", side_effect=lambda c: c):
                    c1 = rw._get_client()
                    c2 = rw._get_client()
            assert c1 is c2  # same instance returned on second call
        finally:
            rw._client = original

    def test_returns_existing_client_if_already_set(self):
        import src.services.notification_rewriter as rw
        original = rw._client
        try:
            sentinel = MagicMock()
            rw._client = sentinel
            assert rw._get_client() is sentinel
        finally:
            rw._client = original


class TestRewriteReminderNotification:
    @pytest.mark.asyncio
    async def test_success_returns_rewritten_text(self):
        from src.services.notification_rewriter import rewrite_reminder_notification

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=_make_text_response("Take your meds"))

        with patch("src.services.notification_rewriter._get_client", return_value=mock_client):
            result = await rewrite_reminder_notification("take medication")

        assert result == "Take your meds"

    @pytest.mark.asyncio
    async def test_non_text_block_returns_original(self):
        """If the first content block is not a TextBlock, return the original message."""
        from src.services.notification_rewriter import rewrite_reminder_notification

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=_make_non_text_response())

        with patch("src.services.notification_rewriter._get_client", return_value=mock_client):
            result = await rewrite_reminder_notification("take medication")

        assert result == "take medication"

    @pytest.mark.asyncio
    async def test_retryable_error_retries_and_succeeds(self):
        from src.services.notification_rewriter import rewrite_reminder_notification

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(side_effect=[
            anthropic.RateLimitError("429", response=MagicMock(), body={}),
            _make_text_response("Rewritten on retry"),
        ])

        with patch("src.services.notification_rewriter._get_client", return_value=mock_client):
            with patch("asyncio.sleep", new=AsyncMock()):
                result = await rewrite_reminder_notification("original message")

        assert result == "Rewritten on retry"
        assert mock_client.messages.create.call_count == 2

    @pytest.mark.asyncio
    async def test_retries_exhausted_returns_original(self):
        """After _MAX_RETRIES retryable failures, return the original message."""
        from src.services.notification_rewriter import rewrite_reminder_notification

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(
            side_effect=anthropic.RateLimitError("429", response=MagicMock(), body={})
        )

        with patch("src.services.notification_rewriter._get_client", return_value=mock_client):
            with patch("asyncio.sleep", new=AsyncMock()):
                result = await rewrite_reminder_notification("original message")

        assert result == "original message"

    @pytest.mark.asyncio
    async def test_non_retryable_error_returns_original_immediately(self):
        """A non-retryable exception must return immediately without retry."""
        from src.services.notification_rewriter import rewrite_reminder_notification

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(side_effect=ValueError("unexpected"))

        with patch("src.services.notification_rewriter._get_client", return_value=mock_client):
            result = await rewrite_reminder_notification("original message")

        assert result == "original message"
        assert mock_client.messages.create.call_count == 1

    @pytest.mark.asyncio
    async def test_internal_server_error_is_retryable(self):
        from src.services.notification_rewriter import rewrite_reminder_notification

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(side_effect=[
            anthropic.InternalServerError("500", response=MagicMock(), body={}),
            _make_text_response("OK"),
        ])

        with patch("src.services.notification_rewriter._get_client", return_value=mock_client):
            with patch("asyncio.sleep", new=AsyncMock()):
                result = await rewrite_reminder_notification("msg")

        assert result == "OK"

    @pytest.mark.asyncio
    async def test_api_connection_error_is_retryable(self):
        from src.services.notification_rewriter import rewrite_reminder_notification

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(side_effect=[
            anthropic.APIConnectionError(request=MagicMock()),
            _make_text_response("Connected"),
        ])

        with patch("src.services.notification_rewriter._get_client", return_value=mock_client):
            with patch("asyncio.sleep", new=AsyncMock()):
                result = await rewrite_reminder_notification("msg")

        assert result == "Connected"

    @pytest.mark.asyncio
    async def test_zero_max_retries_returns_original(self):
        """With _MAX_RETRIES=0 the loop body never runs; hits the safety-net return."""
        from src.services.notification_rewriter import rewrite_reminder_notification
        import src.services.notification_rewriter as rw

        with patch.object(rw, "_MAX_RETRIES", 0):
            result = await rewrite_reminder_notification("no retries")

        assert result == "no retries"
