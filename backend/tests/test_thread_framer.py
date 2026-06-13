"""The curiosity framer must never raise, must enforce its caps, and must keep
the tone a friend's question rather than an auditor's checklist.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from src.services.threads.models import Thread, ThreadSource
from src.services.threads.thread_framer import (
    FOLLOW_UP_BODY_MAX_CHARS,
    FOLLOW_UP_TITLE_MAX_CHARS,
    MAX_SUGGESTED_REPLIES,
    MIN_SUGGESTED_REPLIES,
    SUGGESTED_REPLY_MAX_CHARS,
    FollowUpFramingContext,
    FramedFollowUp,
    frame_follow_up,
)


def _thread() -> Thread:
    now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    return Thread(
        thread_id="t1",
        trigger_text="implement live fetch instead of stale cache",
        source=ThreadSource.REMINDER,
        known_summary="The user set a reminder about a caching change",
        unknown=["what the project is"],
        created_at=now,
        last_touched_at=now,
    )


def _models_returning(framed: FramedFollowUp) -> MagicMock:
    models = MagicMock()
    models.cheap = AsyncMock(return_value=framed)
    return models


async def test_valid_output_is_passed_through_within_caps():
    framed = FramedFollowUp(
        title="that thing you're building",
        body="what are you making that needs live data over cache?",
        suggested_replies=["a side project", "for work", "i'll show you"],
    )
    result = await frame_follow_up(
        _models_returning(framed), _thread(), FollowUpFramingContext()
    )
    assert result.body == framed.body
    assert result.suggested_replies == framed.suggested_replies


async def test_overlong_fields_are_truncated():
    framed = FramedFollowUp(
        title="x" * 200,
        body="y" * 200,
        suggested_replies=["z" * 200, "ok"],
    )
    result = await frame_follow_up(
        _models_returning(framed), _thread(), FollowUpFramingContext()
    )
    assert len(result.title) <= FOLLOW_UP_TITLE_MAX_CHARS
    assert len(result.body) <= FOLLOW_UP_BODY_MAX_CHARS
    assert all(len(r) <= SUGGESTED_REPLY_MAX_CHARS for r in result.suggested_replies)


async def test_too_few_replies_falls_back_to_minimum():
    # A model that returns a single lonely chip must be backfilled, never shipped.
    framed = FramedFollowUp(title="hey", body="what's that about?", suggested_replies=["ok"])
    result = await frame_follow_up(
        _models_returning(framed), _thread(), FollowUpFramingContext()
    )
    assert MIN_SUGGESTED_REPLIES <= len(result.suggested_replies) <= MAX_SUGGESTED_REPLIES


async def test_llm_failure_returns_safe_fallback_not_exception():
    models = MagicMock()
    models.cheap = AsyncMock(side_effect=RuntimeError("gemini down"))
    result = await frame_follow_up(models, _thread(), FollowUpFramingContext())
    assert isinstance(result, FramedFollowUp)
    assert result.body  # non-empty question
    assert MIN_SUGGESTED_REPLIES <= len(result.suggested_replies) <= MAX_SUGGESTED_REPLIES


async def test_aura_gap_fallback_is_a_question_not_an_audit():
    gap_thread = Thread(
        thread_id="g1",
        trigger_text="follows cricket a lot",
        source=ThreadSource.AURA_GAP,
        unknown=["which team"],
    )
    models = MagicMock()
    models.cheap = AsyncMock(side_effect=RuntimeError("down"))
    result = await frame_follow_up(models, gap_thread, FollowUpFramingContext())
    lowered = result.body.lower()
    assert "did you" not in lowered  # never an accountability prompt
    assert result.suggested_replies
