"""The accountability-voice VERIFY gate must catch progress-check phrasing
regardless of exact wording (a semantic judge, not a fixed phrase list), must
fail open (ship it) on any judge error, and must never spend an LLM call on an
already-disqualified (empty/too-few-replies) result.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from src.services.reactive.agent import VerdictKind
from src.services.reactive.agents import curiosity
from src.services.reactive.agents.curiosity import (
    _AccountabilityVoiceJudgment,
    _Result,
    CuriosityThreadFollowUpAgent,
)
from src.services.threads.models import Thread, ThreadSource
from src.services.threads.thread_framer import FramedFollowUp

AGENT = CuriosityThreadFollowUpAgent()


def _thread() -> Thread:
    return Thread(
        thread_id="t1",
        trigger_text="call mom",
        source=ThreadSource.REMINDER,
    )


def _result(body: str, suggested_replies: list[str] | None = None) -> _Result:
    return _Result(
        thread=_thread(),
        framed=FramedFollowUp(
            title="hey",
            body=body,
            suggested_replies=suggested_replies or ["a lot honestly", "not much"],
        ),
        local_date="2026-06-10",
    )


def _models_returning(is_accountability_voice: bool) -> MagicMock:
    models = MagicMock()
    models.cheap = AsyncMock(
        return_value=_AccountabilityVoiceJudgment(is_accountability_voice=is_accountability_voice)
    )
    return models


async def test_accountability_voice_is_flagged_low_quality(monkeypatch):
    monkeypatch.setattr(curiosity, "get_model_provider", lambda: _models_returning(True))
    verdict = await AGENT.verify(_result("how's calling your mom going?"))
    assert verdict.kind == VerdictKind.LOW_QUALITY
    assert verdict.reason == "accountability_voice"


async def test_curious_phrasing_is_ok(monkeypatch):
    monkeypatch.setattr(curiosity, "get_model_provider", lambda: _models_returning(False))
    verdict = await AGENT.verify(_result("what's the story with calling your mom?"))
    assert verdict.is_ok


async def test_judge_failure_fails_open_to_ok(monkeypatch):
    models = MagicMock()
    models.cheap = AsyncMock(side_effect=RuntimeError("gemini down"))
    monkeypatch.setattr(curiosity, "get_model_provider", lambda: models)
    verdict = await AGENT.verify(_result("how's calling your mom going?"))
    assert verdict.is_ok


async def test_empty_body_short_circuits_before_judge(monkeypatch):
    models = _models_returning(False)
    monkeypatch.setattr(curiosity, "get_model_provider", lambda: models)
    verdict = await AGENT.verify(_result(""))
    assert verdict.kind == VerdictKind.EMPTY
    models.cheap.assert_not_called()


async def test_too_few_replies_short_circuits_before_judge(monkeypatch):
    models = _models_returning(False)
    monkeypatch.setattr(curiosity, "get_model_provider", lambda: models)
    verdict = await AGENT.verify(_result("what's going on?", suggested_replies=["ok"]))
    assert verdict.kind == VerdictKind.LOW_QUALITY
    assert verdict.reason == "too_few_replies"
    models.cheap.assert_not_called()
