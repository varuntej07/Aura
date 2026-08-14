"""Fail-closed semantic privacy policy for unsolicited curiosity outreach."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.memory import graph_fields as GF
from src.services.threads import sensitivity


@pytest.mark.parametrize(
    ("subject", "category"),
    [
        ("adjusting a medication or hormone dose", "health_medical"),
        ("a physical symptom mentioned in conversation", "health_medical"),
        ("a private gender identity question", "gender_identity"),
        ("grief after a loss", "grief_loss"),
        ("an intimate relationship conflict", "relationships_social"),
        ("personal debt and money distress", "personal_finance"),
        ("pending legal trouble", "law_legal"),
        ("a traumatic experience", "trauma_abuse"),
    ],
)
async def test_semantic_private_categories_are_suppressed(monkeypatch, subject, category):
    models = MagicMock()
    models.cheap = AsyncMock(return_value=sensitivity._Judgment(
        sensitive=True,
        categories=[category],
        reason="private subject",
    ))
    monkeypatch.setattr(sensitivity, "get_model_provider", lambda: models)

    decision = await sensitivity.classify_proactive_subject(subject)

    assert decision.status == "sensitive"
    assert decision.categories == [category]
    assert decision.allows_proactive is False


async def test_vague_plausibly_sensitive_subject_fails_closed(monkeypatch):
    models = MagicMock()
    models.cheap = AsyncMock(return_value=sensitivity._Judgment(
        sensitive=False,
        plausibly_sensitive=True,
        categories=[],
        reason="insufficient context",
    ))
    monkeypatch.setattr(sensitivity, "get_model_provider", lambda: models)

    decision = await sensitivity.classify_proactive_subject("the private thing")

    assert decision.status == "sensitive"
    assert decision.categories == ["ambiguous_private"]


async def test_classifier_outage_returns_unknown_and_suppresses(monkeypatch):
    models = MagicMock()
    models.cheap = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    monkeypatch.setattr(sensitivity, "get_model_provider", lambda: models)

    decision = await sensitivity.classify_proactive_subject("a subject")

    assert decision.status == "unknown"
    assert decision.allows_proactive is False
    assert decision.source == "classifier_unavailable"


async def test_canonical_graph_flag_suppresses_without_model_call(monkeypatch):
    models = MagicMock()
    models.cheap = AsyncMock()
    monkeypatch.setattr(sensitivity, "get_model_provider", lambda: models)

    decision = await sensitivity.classify_proactive_subject(
        "innocuous label",
        graph_nodes=[{GF.INFERRED_SENSITIVE: True}],
    )

    assert decision.status == "sensitive"
    assert decision.source == "structured_signal"
    models.cheap.assert_not_called()


async def test_ordinary_project_subject_remains_eligible(monkeypatch):
    models = MagicMock()
    models.cheap = AsyncMock(return_value=sensitivity._Judgment(
        sensitive=False,
        plausibly_sensitive=False,
        reason="ordinary project",
    ))
    monkeypatch.setattr(sensitivity, "get_model_provider", lambda: models)

    decision = await sensitivity.classify_proactive_subject(
        "building a game level for a weekend project"
    )

    assert decision.status == "clear"
    assert decision.allows_proactive is True
