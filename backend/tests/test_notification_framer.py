"""
Framer evals — relevance gate, localisation, and the gender-tone guardrail.

These pin the three product-critical behaviours of the upgraded framer:
  1. Relevance (Gate B) is parsed and a not-relevant verdict survives end-to-end.
  2. A non-English user's copy is requested in their language; a non-US/non-tech
     user is judged on THEIR interests (relevance is interest-based, not US-tech).
  3. Gender changes nothing about the content or register (no stereotyping) — the
     content the model sees is byte-identical regardless of gender.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.services.signal_engine.content_pool import ScoredCandidate
from src.services.signal_engine.notification_framer import (
    CONTENT_KIND_DISCUSS,
    CONTENT_KIND_READ,
    FRAMER_UNAVAILABLE_REASON,
    FramedNotification,
    UserFramingContext,
    _build_framer_prompt,
    _safe_fallback,
    frame_notification,
)


def _candidate(*, source="google_news", category="news", url="https://x/1", title="A story") -> ScoredCandidate:
    return ScoredCandidate(
        content_id="c1", source=source, category=category, title=title,
        body="body text", url=url, embedding=[0.1, 0.2, 0.3],
        freshness_ts=datetime.now(UTC), cosine_similarity=0.9,
    )


class _FakeModels:
    """Records the prompt/system passed to cheap() and returns a canned result."""

    def __init__(self, result: FramedNotification):
        self._result = result
        self.prompt = ""
        self.system = ""

    async def cheap(self, prompt, *, system, response_model, temperature):
        self.prompt = prompt
        self.system = system
        return self._result


def test_schema_parses_relevance_and_content_kind():
    fr = FramedNotification.model_validate({
        "title": "t", "body": "b", "opening_chat_message": "o",
        "is_relevant": False, "relevance_reason": "off topic",
        "content_kind": "read",
    })
    assert fr.is_relevant is False
    assert fr.content_kind == "read"
    assert fr.relevance_reason == "off topic"


def test_schema_defaults_fail_open_on_missing_keys():
    # A model that omits is_relevant must default to True (never silently mute).
    fr = FramedNotification.model_validate({"title": "t", "body": "b", "opening_chat_message": "o"})
    assert fr.is_relevant is True
    assert fr.content_kind == CONTENT_KIND_DISCUSS


def test_fallback_is_not_relevant_and_infers_content_kind_by_source():
    # A framer outage must NOT fire hollow fallback copy. The fallback is marked
    # not-relevant with the FRAMER_UNAVAILABLE_REASON sentinel so the scoring loop
    # defers the send (and logs an outage) rather than sending vapor.
    with_url = _safe_fallback(_candidate(url="https://x/1"))
    assert with_url.is_relevant is False
    assert with_url.relevance_reason == FRAMER_UNAVAILABLE_REASON
    assert with_url.content_kind == CONTENT_KIND_READ

    no_url = _safe_fallback(_candidate(url=""))
    assert no_url.content_kind == CONTENT_KIND_DISCUSS  # nothing to open -> discuss


async def test_content_kind_clamped_to_discuss_when_no_url():
    """A model claiming 'read' for a urlless live-score item is clamped to discuss."""
    models = _FakeModels(FramedNotification(
        title="t", body="b", opening_chat_message="o",
        is_relevant=True, content_kind="read",
    ))
    framed = await frame_notification(models, _candidate(url=""), UserFramingContext())
    assert framed.content_kind == CONTENT_KIND_DISCUSS


async def test_content_kind_forced_read_when_url_present():
    """An article tap must open the source every time. A model that mislabels a
    url-bearing item as 'discuss' (the bug that opened chat instead of the piece)
    is deterministically overridden to 'read'."""
    models = _FakeModels(FramedNotification(
        title="t", body="b", opening_chat_message="o",
        is_relevant=True, content_kind="discuss",
    ))
    framed = await frame_notification(models, _candidate(url="https://x/1"), UserFramingContext())
    assert framed.content_kind == CONTENT_KIND_READ


async def test_non_english_user_copy_requested_in_their_language():
    """A Telugu-speaking IN cricket+regional user: the framer is told to write in
    Telugu, and relevance is judged on THEIR interests, not US tech."""
    models = _FakeModels(FramedNotification(
        title="t", body="b", opening_chat_message="o", is_relevant=True, content_kind="read",
    ))
    ctx = UserFramingContext(
        top_interests=["cricket", "Telangana"], language="Telugu",
    )
    await frame_notification(models, _candidate(category="sports"), ctx)
    assert "Telugu" in models.prompt
    assert "language" in models.system.lower()
    # The model sees the user's actual interests so it can judge relevance on them.
    assert "cricket" in models.prompt


async def test_off_interest_not_relevant_verdict_survives():
    """A US-tech story for the cricket+regional user: a not-relevant verdict from
    the model is returned intact so the scoring loop can skip the send."""
    models = _FakeModels(FramedNotification(
        title="t", body="b", opening_chat_message="o",
        is_relevant=False, relevance_reason="US tech, user follows cricket",
        content_kind="read",
    ))
    ctx = UserFramingContext(top_interests=["cricket", "Telangana"], language="Telugu")
    framed = await frame_notification(models, _candidate(category="tech"), ctx)
    assert framed.is_relevant is False


def test_gender_never_changes_content_or_register():
    """The CONTENT section the model sees must be byte-identical regardless of
    gender, and the system prompt must forbid gender stereotyping — so the same
    article can never diverge in topic or register by gender."""
    cand = _candidate(title="Election results are in", category="news")
    male_prompt = _build_framer_prompt(cand, UserFramingContext(gender="male"))
    female_prompt = _build_framer_prompt(cand, UserFramingContext(gender="female"))

    def _content_section(prompt: str) -> str:
        return prompt.split("CONTENT", 1)[1]

    # Everything from the CONTENT marker on is identical: gender touches only the
    # USER CONTEXT block, never the content or the writing instruction.
    assert _content_section(male_prompt) == _content_section(female_prompt)

    from src.services.signal_engine.notification_framer import _FRAMER_SYSTEM_PROMPT
    lowered = _FRAMER_SYSTEM_PROMPT.lower()
    assert "stereotype" in lowered
    assert "regardless of gender" in lowered
