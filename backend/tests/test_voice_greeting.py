"""Memory-seeded opener (voice/greeting.py).

The seeded line races the static greeting under a hard budget and fails open
to "" (static fallback) on timeout, error, empty digest, or a NONE answer.
"""

from __future__ import annotations

import asyncio

from src.agent.voice import greeting
from src.agent.voice.context import SessionContext


def _session_context(**overrides) -> SessionContext:
    values = dict(
        profile={"name": "V", "timezone": "UTC"},
        memory_summary="- training for a marathon",
        last_session_summary="talked about the Annapurna interview",
        last_session_at="yesterday",
        archive_context="",
        aura_summary="ambitious, casual tone",
        dominant_tone="casual",
        dominant_emotion="",
        user_tier="free",
        remaining_free_voice_seconds=None,
    )
    values.update(overrides)
    return SessionContext(**values)


class _FakeProvider:
    def __init__(self, reply: str = "", delay_s: float = 0.0, raises: bool = False):
        self._reply = reply
        self._delay_s = delay_s
        self._raises = raises
        self.prompts: list[str] = []

    async def cheap(self, prompt: str, *, system: str | None = None, **_kw) -> str:
        self.prompts.append(prompt)
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if self._raises:
            raise RuntimeError("provider down")
        return self._reply


async def test_seeded_line_wins_inside_budget(monkeypatch):
    provider = _FakeProvider(reply="yo, how'd the annapurna thing go?")
    monkeypatch.setattr(greeting, "get_model_provider", lambda: provider)
    task = greeting.start_opener_task(_session_context(), session_id="s1", user_id="u1")
    line = await greeting.resolve_opener(task, budget_s=1.0)
    assert line == "yo, how'd the annapurna thing go?"
    # The digest actually seeds the prompt.
    assert "Annapurna" in provider.prompts[0]


async def test_slow_opener_falls_back_within_budget(monkeypatch):
    provider = _FakeProvider(reply="too late", delay_s=0.2)
    monkeypatch.setattr(greeting, "get_model_provider", lambda: provider)
    task = greeting.start_opener_task(_session_context(), session_id="s1", user_id="u1")
    line = await greeting.resolve_opener(task, budget_s=0.01)
    assert line == ""
    task.cancel()


async def test_provider_failure_fails_open(monkeypatch):
    provider = _FakeProvider(raises=True)
    monkeypatch.setattr(greeting, "get_model_provider", lambda: provider)
    task = greeting.start_opener_task(_session_context(), session_id="s1", user_id="u1")
    assert await greeting.resolve_opener(task, budget_s=1.0) == ""


async def test_none_answer_and_empty_digest_fall_back(monkeypatch):
    provider = _FakeProvider(reply="NONE")
    monkeypatch.setattr(greeting, "get_model_provider", lambda: provider)
    task = greeting.start_opener_task(_session_context(), session_id="s1", user_id="u1")
    assert await greeting.resolve_opener(task, budget_s=1.0) == ""

    calls_after_none_case = len(provider.prompts)
    empty_context = _session_context(
        memory_summary="", last_session_summary="", aura_summary=""
    )
    task = greeting.start_opener_task(empty_context, session_id="s1", user_id="u1")
    assert await greeting.resolve_opener(task, budget_s=1.0) == ""
    # No LLM call was spent on an empty digest.
    assert len(provider.prompts) == calls_after_none_case


async def test_missing_task_resolves_to_static_fallback():
    assert await greeting.resolve_opener(None, budget_s=1.0) == ""
