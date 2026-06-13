"""Tests for the icebreaker opener normalisation + fail-closed reject gate.

No LLM is called — these cover the deterministic guards around the model output:
truncation, the "affirmed but no reason -> do not send" rule, and the safe-skip
returned when the model is unavailable.
"""

from __future__ import annotations

from src.services.icebreaker.icebreaker_framer import (
    ICEBREAKER_BODY_MAX_CHARS,
    ICEBREAKER_TITLE_MAX_CHARS,
    IcebreakerOpener,
    _normalise,
    _safe_skip,
)


def test_send_worthy_with_reason_passes():
    opener = IcebreakerOpener(
        title="Hot one today",
        body="Looks like a scorcher where you are. Staying cool?",
        opening_chat_message="It's shaping up hot today. Got any way to stay cool?",
        topic="hot weather",
        is_send_worthy=True,
        reason="It is the first hot day of the week in the user's region, a natural stay-cool opener.",
    )
    out = _normalise(opener)
    assert out.is_send_worthy is True
    assert out.topic == "hot weather"


def test_affirmed_but_empty_reason_is_downgraded_to_not_send():
    opener = IcebreakerOpener(
        title="Hey",
        body="Just checking in",
        opening_chat_message="Hey, how's it going?",
        topic="generic",
        is_send_worthy=True,
        reason="   ",  # no real justification
    )
    out = _normalise(opener)
    assert out.is_send_worthy is False  # fail closed on a missing reason


def test_title_and_body_are_truncated():
    opener = IcebreakerOpener(
        title="x" * 200,
        body="y" * 400,
        opening_chat_message="z" * 50,
        topic="t",
        is_send_worthy=True,
        reason="a valid one-sentence reason that explains the specific hook used here.",
    )
    out = _normalise(opener)
    assert len(out.title) <= ICEBREAKER_TITLE_MAX_CHARS
    assert len(out.body) <= ICEBREAKER_BODY_MAX_CHARS


def test_safe_skip_never_sends():
    out = _safe_skip("gemini timeout")
    assert out.is_send_worthy is False
    assert "icebreaker_framer_unavailable" in out.reason
