"""Buddy's shade reply must never raise and must stay short and warm."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from src.services.threads.models import Thread, ThreadSource
from src.services.threads.thread_responder import (
    RESPONSE_MAX_CHARS,
    generate_thread_reply,
)


def _thread() -> Thread:
    return Thread(
        thread_id="t1",
        trigger_text="building a feature with a caching change",
        source=ThreadSource.REMINDER,
    )


async def test_returns_model_text_trimmed():
    models = MagicMock()
    models.cheap = AsyncMock(return_value="  oh nice, what's it for?  ")
    out = await generate_thread_reply(
        models, _thread(), question="what are you building?", user_reply="a side project",
    )
    assert out == "oh nice, what's it for?"


async def test_overlong_reply_is_truncated():
    models = MagicMock()
    models.cheap = AsyncMock(return_value="x" * 1000)
    out = await generate_thread_reply(
        models, _thread(), question="q", user_reply="r",
    )
    assert len(out) <= RESPONSE_MAX_CHARS


async def test_llm_failure_returns_fallback_not_exception():
    models = MagicMock()
    models.cheap = AsyncMock(side_effect=RuntimeError("gemini down"))
    out = await generate_thread_reply(
        models, _thread(), question="q", user_reply="a side project",
    )
    assert isinstance(out, str) and out  # non-empty, no exception


async def test_empty_model_text_falls_back():
    models = MagicMock()
    models.cheap = AsyncMock(return_value="   ")
    out = await generate_thread_reply(
        models, _thread(), question="q", user_reply="r",
    )
    assert out  # never returns an empty reply
